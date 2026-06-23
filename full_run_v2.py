#!/usr/bin/env python3
"""
RampRush competition agent.

Connects to the live truck event stream, parses the noisy documentation that
comes with every truck (multilingual emails, accented/noisy audio messages and
parcel photos), extracts the structured fields the scorer cares about and makes
a ramp-assignment / rejection decision.

Pipeline per truck:
  1. Text signal  -> email text and/or audio transcript (local Whisper).
  2. Extraction   -> a local `claude` CLI call turns the noisy text into
                     {supplier_name, parcel_count, unit, goods_type}.
                     supplier_name is resolved to a canonical supplier_id via
                     fuzzy matching against GET /suppliers.
  3. Damage       -> if a photo is present it is downloaded and classified by a
                     local `claude` CLI vision call (NOT by the URL path and NOT
                     used to count parcels). No photo -> no damage.
  4. Decision     -> derive goods_type/unit/count into a valid ramp category,
                     pick a free ramp inside that category, then POST
                     /assign-ramp (or /reject-truck when damage is detected).

Usage:
    python joel.py --team-id joel_test
    python joel.py --team-id joel_test --whisper-model small   # faster
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import requests
import websockets
from rapidfuzz import fuzz, process

BASE = "https://truckgenerator-production.up.railway.app"
WS_URL = "wss://truckgenerator-production.up.railway.app/ws?team_id={team_id}"

CLAUDE_BIN = "claude"
CLAUDE_MODEL = "sonnet"
CLAUDE_TIMEOUT = 120

WORKDIR = Path(tempfile.gettempdir()) / "ramprush_joel"
WORKDIR.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# Suppliers (named-entity resolution target)
# ----------------------------------------------------------------------------


def _norm(name: str) -> str:
    """Light normalisation: lowercase + strip punctuation, but KEEP every
    distinguishing token (Corp/Group/Holdings/Fund...). Stripping legal-form
    words collapses different companies that share a root word (e.g.
    "Meridian Corp" vs "Meridian Holdings") and produces false matches.
    """
    s = name.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class SupplierIndex:
    def __init__(self, suppliers: list[dict]):
        self.suppliers = suppliers
        self.ids = [s["supplier_id"] for s in suppliers]
        self.names = [s["supplier_name"] for s in suppliers]
        self.norm = [_norm(n) for n in self.names]
        # map from normalised name -> index, for fast exact hits
        self._exact = {}
        for i, n in enumerate(self.norm):
            self._exact.setdefault(n, i)

    def candidates(self, raw_name: str, k: int = 8) -> list[tuple[int, str, float]]:
        """Top-k supplier matches as (supplier_id, canonical_name, score)."""
        if not raw_name:
            return []
        q = _norm(raw_name)
        # WRatio handles partial / suffix / substring differences well and,
        # because we no longer strip suffixes, exact full names win outright.
        hits = process.extract(q, self.norm, scorer=fuzz.WRatio, limit=k)
        return [(self.ids[i], self.names[i], float(sc)) for _, sc, i in hits]

    def match(self, raw_name: str) -> tuple[int, str, float]:
        """Return the single best (supplier_id, canonical_name, score 0-100)."""
        if not raw_name:
            return self.ids[0], self.names[0], 0.0
        q = _norm(raw_name)
        if q in self._exact:
            i = self._exact[q]
            return self.ids[i], self.names[i], 100.0
        cands = self.candidates(raw_name, k=1)
        if not cands:
            return self.ids[0], self.names[0], 0.0
        return cands[0]


def load_suppliers() -> SupplierIndex:
    cache = WORKDIR / "suppliers.json"
    data = None
    try:
        resp = requests.get(f"{BASE}/suppliers", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        cache.write_text(json.dumps(data))
    except Exception as e:  # noqa: BLE001
        print(f"  !! /suppliers fetch failed ({e}); trying cache")
        if cache.exists():
            data = json.loads(cache.read_text())
    if not data:
        raise SystemExit("could not load supplier list")
    print(f"loaded {len(data)} suppliers")
    return SupplierIndex(data)


# ----------------------------------------------------------------------------
# claude CLI helpers
# ----------------------------------------------------------------------------


def _claude(prompt: str) -> str:
    try:
        out = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL, prompt],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
        )
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  !! claude call failed: {e}")
        return ""


def _first_json(text: str) -> dict:
    """Pull the first JSON object out of a (possibly fenced) model reply."""
    if not text:
        return {}
    text = text.replace("```json", "```")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # tolerate trailing commas / minor noise
        cleaned = re.sub(r",\s*}", "}", m.group(0))
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}


EXTRACT_PROMPT = (
    "You extract structured logistics data from ONE noisy supplier message "
    "(an email or a speech-to-text transcript). It may be in German, French, "
    "Italian, Spanish or English and contain typos, accents and irrelevant "
    "small-talk / distractor sentences which you MUST ignore. Convert spoken "
    'number words to digits (e.g. German "vierunddreissig" or "vier und 30" = '
    "34). Output ONLY one compact JSON object, no markdown, with keys:\n"
    '  supplier_name : string, best guess of the delivering company name.\n'
    '  parcel_count  : integer, the announced number of units.\n'
    '  unit          : "parcels" or "pallets". colis/paquets/paquetes/Pakete/'
    "pacchi/packages/cartons = parcels; palettes/palets/Paletten/pallet/"
    "pallets = pallets. Use the noun that the COUNT refers to.\n"
    '  goods_type    : "standard" | "oversized" | "perishable". '
    "perishable = perishable/perissable/refrigere/frais/Kuhlware/verderblich/"
    "perecedero/deperibile/frozen/chilled. oversized = oversized/bulky/"
    "Sperrgut/encombrant/volumineux/voluminoso/voluminosa/ingombrante/"
    "uebergross. electronics/standard goods/marchandise standard = standard.\n"
    "Message:\n"
)


def extract_fields(text: str) -> dict:
    reply = _claude(EXTRACT_PROMPT + text)
    data = _first_json(reply)
    # normalise
    out = {
        "supplier_name": str(data.get("supplier_name") or "").strip(),
        "parcel_count": None,
        "unit": "pallets",
        "goods_type": "standard",
    }
    try:
        out["parcel_count"] = int(data.get("parcel_count"))
    except (TypeError, ValueError):
        out["parcel_count"] = None
    u = str(data.get("unit") or "").lower()
    out["unit"] = "parcels" if "parcel" in u else "pallets"
    g = str(data.get("goods_type") or "").lower()
    if "perish" in g:
        out["goods_type"] = "perishable"
    elif "over" in g or "size" in g:
        out["goods_type"] = "oversized"
    else:
        out["goods_type"] = "standard"
    return out


DAMAGE_PROMPT = (
    "Look at the image at absolute path {path} . It is a photo of parcels / "
    "packages / boxes on a pallet or inside a truck. Decide if there is "
    "VISIBLE TRANSPORT DAMAGE: crushed, torn, dented, punctured, broken, "
    "collapsed, open or spilled boxes / packaging. Clean, intact, neatly "
    "stacked boxes mean NO damage. Reply ONLY compact JSON: "
    '{{"has_damage": true}} or {{"has_damage": false}}.'
)


def detect_damage(image_path: Path) -> bool:
    reply = _claude(DAMAGE_PROMPT.format(path=image_path))
    data = _first_json(reply)
    return bool(data.get("has_damage", False))


# When the best fuzzy score is at/above this, trust it directly and skip the
# (slower) claude disambiguation call.
SUPPLIER_CONFIDENT = 95.0


def resolve_supplier(index: "SupplierIndex", extracted_name: str,
                     raw_text: str) -> tuple[int, str, float]:
    """Resolve a noisy supplier name to a canonical (id, name, score).

    Fast path: a confident fuzzy hit is taken directly. Otherwise ask the
    local claude CLI to pick the best supplier_id from the top fuzzy
    candidates, using the FULL original message as context (it disambiguates
    legal-form variants and lightly-garbled names better than fuzz alone).
    Claude may decline (null) -> we fall back to the top fuzzy candidate.
    """
    best = index.match(extracted_name)
    if best[2] >= SUPPLIER_CONFIDENT:
        return best

    cands = index.candidates(extracted_name, k=10)
    if not cands:
        return best
    listing = "\n".join(f"{cid}\t{cname}" for cid, cname, _ in cands)
    prompt = (
        "Resolve a delivering company to its canonical supplier from a "
        "candidate list. The detected name may be misspelled or garbled by "
        "speech-to-text. Use the full message for context. Pick the single "
        "best candidate, or null if none is a plausible match. Reply ONLY "
        'compact JSON: {"supplier_id": <id or null>}.\n'
        f"Detected name: {extracted_name!r}\n"
        f"Message:\n{raw_text[:600]}\n"
        f"Candidates (id<TAB>name):\n{listing}"
    )
    data = _first_json(_claude(prompt))
    chosen = data.get("supplier_id")
    try:
        chosen = int(chosen)
    except (TypeError, ValueError):
        return best
    for cid, cname, sc in cands:
        if cid == chosen:
            return cid, cname, max(sc, SUPPLIER_CONFIDENT)
    return best


# ----------------------------------------------------------------------------
# Audio (local Whisper)
# ----------------------------------------------------------------------------


class Transcriber:
    def __init__(self, model_name: str):
        from faster_whisper import WhisperModel

        print(f"loading whisper '{model_name}' ...")
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")
        print("whisper ready")

    def transcribe(self, audio_path: Path) -> str:
        try:
            segments, _info = self.model.transcribe(str(audio_path), beam_size=5)
            return "".join(seg.text for seg in segments).strip()
        except Exception as e:  # noqa: BLE001
            print(f"  !! transcription failed: {e}")
            return ""


# ----------------------------------------------------------------------------
# Download helper
# ----------------------------------------------------------------------------


def download(url: str, dest: Path) -> bool:
    full = urljoin(BASE + "/", url.lstrip("/"))
    try:
        r = requests.get(full, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  !! download failed {full}: {e}")
        return False


# ----------------------------------------------------------------------------
# Decision logic
# ----------------------------------------------------------------------------

RAMP_ORDER = ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08"]


def candidate_ramps(unit: str, goods_type: str, count: int | None) -> list[str]:
    """Valid ramp category for the truck, in preference order.

    Precedence (per ramp spec):
      perishable          -> R07 (cold chain, mandatory)
      pallets & count>32  -> R08 (double-truck lane)
      oversized           -> R05/R06 (heavy lanes)
      parcels             -> R01/R02 (parcel lanes)
      pallets (<=32)      -> R03/R04 (standard lanes)
    """
    if goods_type == "perishable":
        return ["R07"]
    if unit == "pallets" and count is not None and count > 32:
        return ["R08"]
    if goods_type == "oversized":
        return ["R05", "R06"]
    if unit == "parcels":
        return ["R01", "R02"]
    return ["R03", "R04"]


def choose_ramp(candidates: list[str], ramp_status: list[dict]) -> str:
    status = {r["ramp"]: r for r in ramp_status}

    def queue(ramp: str) -> int:
        return status.get(ramp, {}).get("queue_length", 0)

    def is_free(ramp: str) -> bool:
        return status.get(ramp, {}).get("status") == "free"

    free = [r for r in candidates if is_free(r)]
    pool = free if free else candidates
    # prefer the shortest queue; stable by ramp order
    pool = sorted(pool, key=lambda r: (queue(r), RAMP_ORDER.index(r)))
    return pool[0]


# ----------------------------------------------------------------------------
# Per-truck processing
# ----------------------------------------------------------------------------


class Agent:
    def __init__(self, team_id: str, suppliers: SupplierIndex, transcriber: Transcriber):
        self.team_id = team_id
        self.suppliers = suppliers
        self.transcriber = transcriber

    def gather_text(self, truck_id: str, docs: list[dict]) -> str:
        parts = []
        for doc in docs:
            if doc.get("type") == "email" and doc.get("text"):
                parts.append(doc["text"])
        for doc in docs:
            if doc.get("type") == "audio" and doc.get("url"):
                dest = WORKDIR / f"{truck_id}.mp3"
                if download(doc["url"], dest):
                    txt = self.transcriber.transcribe(dest)
                    if txt:
                        print(f"  transcript: {txt[:120]}")
                        parts.append(txt)
        return "\n".join(parts).strip()

    def gather_damage(self, truck_id: str, docs: list[dict]) -> bool:
        for doc in docs:
            if doc.get("type") == "photo" and doc.get("url"):
                dest = WORKDIR / f"{truck_id}.jpg"
                if download(doc["url"], dest):
                    return detect_damage(dest)
        return False

    def process(self, msg: dict) -> dict:
        truck_id = msg.get("truck_id", "UNKNOWN")
        docs = msg.get("documentation", [])
        ramp_status = msg.get("ramp_status", [])

        text = self.gather_text(truck_id, docs)
        fields = extract_fields(text) if text else {
            "supplier_name": "", "parcel_count": None,
            "unit": "pallets", "goods_type": "standard",
        }

        supplier_id, canon_name, score = resolve_supplier(
            self.suppliers, fields["supplier_name"], text)
        has_damage = self.gather_damage(truck_id, docs)

        count = fields["parcel_count"]
        unit = fields["unit"]
        goods = fields["goods_type"]

        payload = {
            "truck_id": truck_id,
            "team_id": self.team_id,
            "supplier_id": supplier_id,
            "supplier_name": canon_name,
            "parcel_count": count if count is not None else 0,
            "has_damage": has_damage,
            "unit": unit,
        }

        if has_damage:
            endpoint = "/reject-truck"
            decision = "REJECT(damage)"
        else:
            cands = candidate_ramps(unit, goods, count)
            ramp = choose_ramp(cands, ramp_status)
            payload["assigned_ramp"] = ramp
            endpoint = "/assign-ramp"
            decision = f"{ramp} <- {goods}/{unit}"

        print(
            f"  extracted: supplier='{fields['supplier_name']}' -> "
            f"{canon_name} (id={supplier_id}, fuzz={score:.0f}) | "
            f"count={count} unit={unit} goods={goods} damage={has_damage}"
        )
        print(f"  decision: {decision}")
        return {"endpoint": endpoint, "payload": payload}


# ----------------------------------------------------------------------------
# Main event loop
# ----------------------------------------------------------------------------


async def run(team_id: str, whisper_model: str, limit: int = 0) -> None:
    suppliers = load_suppliers()
    transcriber = Transcriber(whisper_model)
    agent = Agent(team_id, suppliers, transcriber)
    url = WS_URL.format(team_id=team_id)

    total_score = 0
    processed = 0

    while True:
        print(f"\nconnecting to {url}")
        try:
            async with websockets.connect(url, max_size=None) as ws:
                print("connected, waiting for trucks...\n")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        print(f"non-JSON: {raw[:200]!r}")
                        continue

                    if msg.get("finished"):
                        print(f"\nALL TRUCKS PROCESSED. "
                              f"processed={processed} total_score~={total_score}")
                        return

                    truck_id = msg.get("truck_id", "UNKNOWN")
                    print(f"[{truck_id}] priority={msg.get('priority')}")

                    # run blocking work (whisper + claude + http) off the loop
                    result = await asyncio.to_thread(agent.process, msg)

                    before = total_score
                    resp = _submit_capture(result["endpoint"], result["payload"])
                    total_score += resp
                    processed += 1
                    print(f"  running total: {total_score} "
                          f"(+{total_score - before}) over {processed} trucks")

                    if limit and processed >= limit:
                        print(f"\nreached --limit {limit}, stopping. "
                              f"total_score={total_score}")
                        return
        except (websockets.ConnectionClosed, OSError) as e:
            print(f"  connection issue ({e}), reconnecting in 2s...")
            await asyncio.sleep(2)


def _submit_capture(endpoint: str, payload: dict) -> int:
    """Submit and return the truck total (0 on failure)."""
    try:
        r = requests.post(f"{BASE}{endpoint}", json=payload, timeout=30)
        r.raise_for_status()
        body = r.json()
        total = body.get("total", 0) or 0
        print(
            f"  -> score total={total} "
            f"(extract={body.get('extraction_score')} "
            f"decision={body.get('decision_score')})"
        )
        bd = body.get("breakdown", {})
        for k, v in bd.items():
            if v.get("earned", 0) < 0:
                print(f"     LOST {k}: {v.get('result')}")
        return int(total)
    except Exception as e:  # noqa: BLE001
        print(f"  !! submit failed: {e}")
        return 0


def main() -> None:
    p = argparse.ArgumentParser(description="RampRush competition agent")
    p.add_argument("--team-id", default="joel_test")
    p.add_argument("--whisper-model", default="medium",
                   help="faster-whisper model size (tiny/base/small/medium/large-v3)")
    p.add_argument("--limit", type=int, default=0,
                   help="stop after N trucks (0 = run until the stream ends)")
    args = p.parse_args()
    try:
        asyncio.run(run(args.team_id, args.whisper_model, args.limit))
    except KeyboardInterrupt:
        print("\ninterrupted, bye.")


if __name__ == "__main__":
    main()
