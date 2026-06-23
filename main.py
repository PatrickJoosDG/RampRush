#!/usr/bin/env python3
"""
RampRush ramp-manager agent.

Event loop:
    1. connect to the truck WebSocket
    2. read each truck message, parse the JSON body
    3. extract the structured fields from the documentation
    4. decide: reject (damage) or assign to a ramp
    5. POST the decision -> the server then sends the next truck

The decision / ramp-selection logic is fully implemented from the rules in
docs/documentation.md. The *extraction* combines every signal for a truck —
the supplier email, the locally transcribed audio message (faster-whisper) and
an image description (currently a stub) — and hands all of it to the local
`claude` CLI, which returns the structured fields plus a most-likely supplier
*name*. The supplier-id mapping is done afterwards in _match_supplier(); the
model never returns ids itself. It all lives in extract_truck().

Usage:
    python main.py --team-id my-team
    python main.py --team-id my-team --once    # answer one truck and stop
"""
import argparse
import asyncio
import json
import re
import subprocess
import tempfile
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import urljoin

import requests
import websockets

from parse_suppliers import parse_suppliers

BASE = "https://truckgenerator-production.up.railway.app"
WS_URL = "wss://truckgenerator-production.up.railway.app/ws?team_id={team_id}"

# Local `claude` CLI (uses your Claude subscription) — the extraction model.
CLAUDE_BIN = "claude"
CLAUDE_MODEL = "opus"
CLAUDE_TIMEOUT = 120

# Scratch dir for downloaded audio/photo assets.
WORKDIR = Path(tempfile.gettempdir()) / "ramprush"
WORKDIR.mkdir(parents=True, exist_ok=True)

# Whisper model used to transcribe audio messages; size set from the CLI.
_WHISPER = {"model": None, "name": "medium"}

# Ramp categories, ordered by preference within each category (see docs).
RAMP_CATEGORIES = {
    "parcels":    ["R01", "R02"],   # parcel lanes (non-perishable)
    "perishable": ["R07"],          # cold chain — perishable MUST go here
    "oversized":  ["R05", "R06"],   # heavy lanes
    "double":     ["R08"],          # double truck lane (pallets, count > 32)
    "standard":   ["R03", "R04"],   # standard lanes (pallets <= 32)
}


# --------------------------------------------------------------------------- #
# Extraction  (the AI part — currently a basic email-text heuristic)
# --------------------------------------------------------------------------- #
_SUPPLIERS = parse_suppliers("suppliers.json")
_NAME_TO_ID = {name.lower(): sid for sid, name in _SUPPLIERS.items()}

# Single alternation of all supplier names, longest first so the regex engine
# prefers the most specific (longest) name at any given position. Word-boundary
# lookarounds stop short names from matching inside unrelated words
# (e.g. "EME" inside "cordialement").
_SUPPLIER_RE = re.compile(
    r"(?<!\w)(?:%s)(?!\w)"
    % "|".join(re.escape(n) for n in sorted(_NAME_TO_ID, key=len, reverse=True) if n)
)

def _match_supplier(text: str) -> tuple[int | None, str]:
    """Best-effort supplier resolution from a name/free text. Returns (id, name).

    Used for the *supplier-id mapping* step, after the model has extracted a
    most-likely supplier name — the model never returns ids itself.
    """
    low = text.lower()
    # 1) word-bounded hit on a known supplier name; prefer the longest match
    #    so a specific name beats a short one that happens to also appear.
    hits = _SUPPLIER_RE.findall(low)
    if hits:
        best = max(hits, key=len)
        sid = _NAME_TO_ID[best]
        return sid, _SUPPLIERS[sid]
    # 2) fuzzy match on capitalised-looking name fragments
    candidates = re.findall(r"[A-ZÄÖÜ][\w&.\- ]{3,40}", text)
    for frag in candidates:
        hit = get_close_matches(frag.lower(), _NAME_TO_ID.keys(), n=1, cutoff=0.85)
        if hit:
            sid = _NAME_TO_ID[hit[0]]
            return sid, _SUPPLIERS[sid]
    return None, ""


# --------------------------------------------------------------------------- #
# Signal gathering  (email + audio transcript + image description)
# --------------------------------------------------------------------------- #
def _download(url: str, dest: Path) -> bool:
    full = urljoin(BASE + "/", url.lstrip("/"))
    try:
        r = requests.get(full, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    !! download failed {full}: {e}")
        return False


def _get_whisper():
    """Lazily load the faster-whisper model (first transcription only)."""
    if _WHISPER["model"] is None:
        from faster_whisper import WhisperModel

        print(f"loading whisper '{_WHISPER['name']}' ...")
        _WHISPER["model"] = WhisperModel(
            _WHISPER["name"], device="cpu", compute_type="int8"
        )
        print("whisper ready")
    return _WHISPER["model"]


def transcribe_audio(url: str) -> str:
    """Download an audio message and transcribe it locally with Whisper."""
    dest = WORKDIR / Path(url).name
    if not _download(url, dest):
        return ""
    try:
        segments, _info = _get_whisper().transcribe(str(dest), beam_size=5)
        return "".join(seg.text for seg in segments).strip()
    except Exception as e:  # noqa: BLE001
        print(f"    !! transcription failed: {e}")
        return ""


def describe_image(url: str) -> str:
    """STUB — image description is added later (vision call on the photo).

    Returns an empty description for now so the extraction prompt simply has
    no visual signal to work with. When implemented this should return a short
    natural-language description of the parcel photo (contents + any visible
    transport damage) so the extraction model can reason about it.
    """
    return ""


def gather_signals(
    msg: dict,
    *,
    transcribe_fn=transcribe_audio,
    describe_fn=describe_image,
) -> dict:
    """Collect every documentation signal for one truck into plain text."""
    email = ""
    transcript = ""
    image_description = ""
    for doc in msg.get("documentation", []):
        kind = doc.get("type")
        if kind == "email" and not email:
            email = doc.get("text", "") or ""
        elif kind == "audio" and doc.get("url") and not transcript:
            transcript = transcribe_fn(doc["url"])
        elif kind == "photo" and doc.get("url") and not image_description:
            image_description = describe_fn(doc["url"])
    return {
        "email": email,
        "transcript": transcript,
        "image_description": image_description,
    }


# --------------------------------------------------------------------------- #
# Extraction  (a single local `claude` CLI call over all the signals)
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT = (
    "You extract structured logistics data about ONE inbound truck from its "
    "noisy documentation. You are given up to three signals: a supplier EMAIL, "
    "a speech-to-text TRANSCRIPT of an audio message, and a DESCRIPTION of a "
    "parcel photo. The audio description is the least reliable data source. Any of them may be empty, multilingual, contain typos/accents and irrelevant "
    "small-talk which you MUST ignore. Combine the signals; if they conflict, "
    'prefer the most specific value. Convert spoken number words to digits '
    '(e.g. German "vierunddreissig" = 34). Output ONLY one compact JSON '
    "object, no markdown, with keys:\n"
    "  supplier_name : string, your single MOST LIKELY delivering company "
    "name. Do NOT return any id — id mapping is done separately.\n"
    "  parcel_count  : integer, the announced number of units.\n"
    '  unit          : "parcels" or "pallets". colis/paquets/paquetes/Pakete/'
    "pacchi/packages/cartons = parcels; palettes/palets/Paletten/pallet/"
    "pallets = pallets. Use the noun the COUNT refers to.\n"
    '  goods_type    : "standard" | "oversized" | "perishable". perishable = '
    "refrigerated/frozen/chilled/frais/Kuhlware/verderblich/deperibile. "
    "oversized = bulky/Sperrgut/encombrant/voluminoso/ingombrante. otherwise "
    "standard.\n"
    '  has_damage    : boolean, true ONLY if the photo DESCRIPTION reports '
    "visible transport damage (crushed/torn/dented/broken/open boxes). With no "
    "photo description, return false.\n"
)


def _claude(prompt: str) -> str:
    """Run the local `claude` CLI once and return its stdout."""
    try:
        out = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL, prompt],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
        )
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        print(f"    !! claude call failed: {e}")
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
        cleaned = re.sub(r",\s*}", "}", m.group(0))
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}


def build_extraction_prompt(signals: dict) -> str:
    """Assemble the EXTRACT_PROMPT plus every available signal."""
    return (
        f"{EXTRACT_PROMPT}\n"
        f"EMAIL:\n{signals.get('email', '')}\n\n"
        f"TRANSCRIPT:\n{signals.get('transcript', '')}\n\n"
        f"DESCRIPTION:\n{signals.get('image_description', '')}\n"
    )


def parse_extraction(reply: str) -> dict:
    """Normalise the model's JSON reply into our structured field dict."""
    data = _first_json(reply)
    out = {
        "supplier_name": str(data.get("supplier_name") or "").strip(),
        "parcel_count": 0,
        "unit": "pallets",
        "goods_type": "standard",
        "has_damage": bool(data.get("has_damage", False)),
    }
    try:
        out["parcel_count"] = int(data.get("parcel_count"))
    except (TypeError, ValueError):
        out["parcel_count"] = 0
    u = str(data.get("unit") or "").lower()
    parcel_words = ("parcel", "colis", "paquet", "paquete", "paket", "pacch", "pacco",
                    "package", "carton")
    out["unit"] = "parcels" if any(w in u for w in parcel_words) else "pallets"
    g = str(data.get("goods_type") or "").lower()
    if any(w in g for w in ("perish", "perissable", "frais", "kuhl", "kühl",
                            "refriger", "frozen", "chilled", "deperibile",
                            "verderb", "perecedero")):
        out["goods_type"] = "perishable"
    elif any(w in g for w in ("over", "size", "bulky", "sperr", "encombrant",
                              "voluminos", "ingombrante")):
        out["goods_type"] = "oversized"
    else:
        out["goods_type"] = "standard"
    return out


def extract_truck(
    msg: dict,
    *,
    transcribe_fn=transcribe_audio,
    describe_fn=describe_image,
    claude_fn=_claude,
) -> dict:
    """Turn a raw truck message into the structured fields the API expects.

    Combines the email, the transcribed audio message and the image
    description, hands all of it to the local `claude` CLI for extraction, and
    only then maps the model's most-likely supplier *name* to a canonical
    supplier *id*. The model never returns ids itself.
    """
    signals = gather_signals(msg, transcribe_fn=transcribe_fn, describe_fn=describe_fn)
    fields = parse_extraction(claude_fn(build_extraction_prompt(signals)))

    sid, canon = _match_supplier(fields["supplier_name"])
    return {
        "supplier_id": sid if sid is not None else 0,
        "supplier_name": canon if sid is not None else fields["supplier_name"],
        "parcel_count": fields["parcel_count"],
        "unit": fields["unit"],
        "has_damage": fields["has_damage"],
        "goods_type": fields["goods_type"],
    }


# --------------------------------------------------------------------------- #
# Decision  (fully implemented from the ramp rules)
# --------------------------------------------------------------------------- #
def _category(ext: dict) -> str:
    """Map extracted goods info to a ramp category."""
    if ext["goods_type"] == "perishable":
        return "perishable"          # cold chain always wins
    if ext["unit"] == "parcels":
        return "parcels"
    if ext["unit"] == "pallets" and ext["parcel_count"] > 32:
        return "double"
    if ext["goods_type"] == "oversized":
        return "oversized"
    return "standard"


def choose_ramp(ext: dict, ramp_status: list[dict]) -> str:
    """Pick the freest ramp in the correct category (most queue-friendly)."""
    candidates = RAMP_CATEGORIES[_category(ext)]
    status = {r["ramp"]: r for r in ramp_status}

    def rank(ramp: str):
        s = status.get(ramp, {})
        is_free = 0 if s.get("status") == "free" else 1
        return (is_free, s.get("queue_length", 10**9))

    return min(candidates, key=rank)


def decide(msg: dict) -> tuple[str, dict]:
    """Return (endpoint_path, payload) for a truck message."""
    ext = extract_truck(msg)
    truck_id = msg.get("truck_id", "")

    payload = {
        "truck_id": truck_id,
        "supplier_id": ext["supplier_id"],
        "supplier_name": ext["supplier_name"],
        "parcel_count": ext["parcel_count"],
        "has_damage": ext["has_damage"],
        "unit": ext["unit"],
    }

    if ext["has_damage"]:
        return "/reject-truck", payload

    payload["assigned_ramp"] = choose_ramp(ext, msg.get("ramp_status", []))
    return "/assign-ramp", payload


# --------------------------------------------------------------------------- #
# Event loop
# --------------------------------------------------------------------------- #
def send_decision(path: str, payload: dict) -> None:
    try:
        r = requests.post(f"{BASE}{path}", json=payload, timeout=30)
        r.raise_for_status()
        result = r.json()
        print(f"    -> {path}  total={result.get('total')}  "
              f"extraction={result.get('extraction_score')} "
              f"decision={result.get('decision_score')}")
    except Exception as e:  # noqa: BLE001
        print(f"    !! POST {path} failed: {e}")


async def run(team_id: str, once: bool) -> None:
    url = WS_URL.format(team_id=team_id)
    print(f"connecting to {url}")
    async with websockets.connect(url, max_size=None) as ws:
        print("connected. waiting for trucks...\n")
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"non-JSON message: {raw[:200]!r}")
                continue

            truck_id = msg.get("truck_id", "UNKNOWN")
            # extraction blocks (whisper + claude CLI); run it off the loop.
            path, payload = await asyncio.to_thread(decide, msg)
            payload["team_id"] = team_id
            print(f"[{truck_id}] priority={msg.get('priority')} "
                  f"-> {'REJECT' if path == '/reject-truck' else payload.get('assigned_ramp')}")
            send_decision(path, payload)

            if once:
                break


def main() -> None:
    p = argparse.ArgumentParser(description="RampRush ramp-manager agent")
    p.add_argument("--team-id", required=True)
    p.add_argument("--once", action="store_true", help="answer one truck and stop")
    p.add_argument("--whisper-model", default="medium",
                   help="faster-whisper model size (tiny/base/small/medium/large-v3)")
    args = p.parse_args()
    _WHISPER["name"] = args.whisper_model
    try:
        asyncio.run(run(args.team_id, args.once))
    except KeyboardInterrupt:
        print("\ninterrupted, bye.")


if __name__ == "__main__":
    main()
