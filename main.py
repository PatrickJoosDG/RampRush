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
docs/documentation.md. The *extraction* (turning noisy email / audio / photo
docs into structured fields) is the hard AI part — it lives in extract_truck()
and is deliberately isolated so you can grow it without touching the loop.

Usage:
    python main.py --team-id my-team
    python main.py --team-id my-team --once    # answer one truck and stop
"""
import argparse
import asyncio
import json
import re
from difflib import get_close_matches

import requests
import websockets

from parse_suppliers import parse_suppliers

BASE = "https://truckgenerator-production.up.railway.app"
WS_URL = "wss://truckgenerator-production.up.railway.app/ws?team_id={team_id}"

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

_DAMAGE_WORDS = (
    "damage", "damaged", "broken", "crushed",
    "beschädigt", "schaden", "kaputt",
    "endommagé", "endommage", "cassé", "casse", "abîmé",
    "danneggiato", "rotto",
)
_PERISHABLE_WORDS = (
    "perishable", "refrigerated", "frozen", "chilled", "cold chain",
    "kühl", "kuhl", "gekühlt", "tiefkühl", "frisch",
    "frais", "réfrigéré", "refrigere", "surgelé", "surgele",
    "deperibile", "refrigerato", "surgelato",
)
_OVERSIZED_WORDS = (
    "oversized", "oversize", "bulky", "sperrgut", "übergroß", "ubergross",
    "encombrant", "hors gabarit", "ingombrante",
)
_PARCEL_WORDS = ("parcel", "parcels", "colis", "paket", "pakete", "pacchi", "pacco")
_PALLET_WORDS = ("pallet", "pallets", "palette", "paletten", "palettes")


def _email_text(msg: dict) -> str:
    for doc in msg.get("documentation", []):
        if doc.get("type") == "email":
            return doc.get("text", "") or ""
    return ""


def _match_supplier(text: str) -> tuple[int | None, str]:
    """Best-effort supplier resolution from free text. Returns (id, name)."""
    low = text.lower()
    # 1) exact substring hit on a known supplier name
    for name_low, sid in _NAME_TO_ID.items():
        if name_low and name_low in low:
            return sid, _SUPPLIERS[sid]
    # 2) fuzzy match on capitalised-looking name fragments
    candidates = re.findall(r"[A-ZÄÖÜ][\w&.\- ]{3,40}", text)
    for frag in candidates:
        hit = get_close_matches(frag.lower(), _NAME_TO_ID.keys(), n=1, cutoff=0.85)
        if hit:
            sid = _NAME_TO_ID[hit[0]]
            return sid, _SUPPLIERS[sid]
    return None, ""


def _count_and_unit(text: str) -> tuple[int, str]:
    low = text.lower()
    unit = "parcels" if any(w in low for w in _PARCEL_WORDS) else "pallets"
    # number sitting next to a unit word, e.g. "30 colis" / "12 pallets"
    unit_words = _PARCEL_WORDS if unit == "parcels" else _PALLET_WORDS
    pat = r"(\d+)\s*(?:%s)" % "|".join(map(re.escape, unit_words))
    m = re.search(pat, low)
    if not m:  # fall back to any number in the text
        m = re.search(r"\b(\d{1,4})\b", low)
    count = int(m.group(1)) if m else 0
    return count, unit


def _goods_type(text: str, unit: str, count: int) -> str:
    low = text.lower()
    if any(w in low for w in _PERISHABLE_WORDS):
        return "perishable"
    if any(w in low for w in _OVERSIZED_WORDS):
        return "oversized"
    return "standard"


def extract_truck(msg: dict) -> dict:
    """Turn a raw truck message into the structured fields the API expects.

    TODO: this currently reads the *email* only. To raise the extraction score,
    add photo (damage detection / vision) and audio (transcription) handling —
    they hang off the same `documentation` list. Keep returning this same dict.
    """
    text = _email_text(msg)
    sid, sname = _match_supplier(text)
    count, unit = _count_and_unit(text)
    goods = _goods_type(text, unit, count)
    has_damage = any(w in text.lower() for w in _DAMAGE_WORDS)
    return {
        "supplier_id": sid if sid is not None else 0,
        "supplier_name": sname,
        "parcel_count": count,
        "unit": unit,
        "has_damage": has_damage,
        "goods_type": goods,
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
            path, payload = decide(msg)
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
    args = p.parse_args()
    try:
        asyncio.run(run(args.team_id, args.once))
    except KeyboardInterrupt:
        print("\ninterrupted, bye.")


if __name__ == "__main__":
    main()
