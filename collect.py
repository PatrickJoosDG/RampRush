#!/usr/bin/env python3
"""
RampRush data collector.

Connects to the truck event WebSocket and saves each truck's documentation
into folders separated by type:

    data/
      photos/   TRK-XXX.jpg
      emails/   TRK-XXX.txt
      audio/    TRK-XXX.mp3
      raw/      TRK-XXX.json   (full server message, for reference)

Usage:
    python collect.py                      # collect, wait after each truck
    python collect.py --advance --limit 50 # auto-advance to pull more trucks

Note: the server only sends the *next* truck once you have answered the
current one (POST /assign-ramp or /reject-truck). With --advance the script
sends a placeholder reply just to keep the stream flowing for data gathering.
This placeholder is NOT a real decision and will score poorly, so only use it
with a throwaway team_id.
"""
import argparse
import asyncio
import json
from pathlib import Path

from urllib.parse import urljoin

import requests
import websockets

BASE = "https://truckgenerator-production.up.railway.app"
WS_URL = "wss://truckgenerator-production.up.railway.app/ws?team_id={team_id}"

DATA_DIR = Path(__file__).parent / "data"
DIRS = {
    "photo": DATA_DIR / "photos",
    "email": DATA_DIR / "emails",
    "audio": DATA_DIR / "audio",
    "raw": DATA_DIR / "raw",
}


def ensure_dirs() -> None:
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)


def download(url: str, dest: Path) -> None:
    url = urljoin(BASE + "/", url.lstrip("/"))
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f"    saved {dest.name} ({len(r.content)} bytes)")
    except Exception as e:  # noqa: BLE001
        print(f"    !! failed {url}: {e}")


def save_truck(msg: dict, counts: dict, cap: int) -> None:
    """Save a truck's docs, honoring the per-type cap. Updates counts in place."""
    truck_id = msg.get("truck_id", "UNKNOWN")
    print(f"[{truck_id}] priority={msg.get('priority')} "
          f"(photo={counts['photo']} email={counts['email']} audio={counts['audio']})")

    # full raw message for later reference
    (DIRS["raw"] / f"{truck_id}.json").write_text(
        json.dumps(msg, ensure_ascii=False, indent=2)
    )

    for doc in msg.get("documentation", []):
        dtype = doc.get("type")
        if dtype == "photo":
            if counts["photo"] >= cap:
                continue
            ext = Path(doc.get("url", "")).suffix or ".jpg"
            download(doc["url"], DIRS["photo"] / f"{truck_id}{ext}")
            counts["photo"] += 1
        elif dtype == "audio":
            if counts["audio"] >= cap:
                continue
            ext = Path(doc.get("url", "")).suffix or ".mp3"
            download(doc["url"], DIRS["audio"] / f"{truck_id}{ext}")
            counts["audio"] += 1
        elif dtype == "email":
            if counts["email"] >= cap:
                continue
            (DIRS["email"] / f"{truck_id}.txt").write_text(
                doc.get("text", ""), encoding="utf-8"
            )
            counts["email"] += 1
            print(f"    saved {truck_id}.txt")
        else:
            print(f"    ?? unknown doc type: {dtype}")


def advance(truck_id: str, team_id: str) -> None:
    """Send a placeholder reply so the server emits the next truck."""
    payload = {
        "truck_id": truck_id,
        "team_id": team_id,
        "supplier_id": 0,
        "supplier_name": "",
        "parcel_count": 0,
        "has_damage": False,
        "unit": "pallets",
        "assigned_ramp": "R03",
    }
    try:
        requests.post(f"{BASE}/assign-ramp", json=payload, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"    !! advance failed: {e}")


async def run(team_id: str, do_advance: bool, cap: int) -> None:
    ensure_dirs()
    # resume-aware: count files already on disk so re-runs don't overshoot
    counts = {
        "photo": len(list(DIRS["photo"].glob("*"))),
        "email": len(list(DIRS["email"].glob("*"))),
        "audio": len(list(DIRS["audio"].glob("*"))),
    }
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

            save_truck(msg, counts, cap)

            if do_advance:
                advance(msg.get("truck_id", ""), team_id)

            if all(counts[t] >= cap for t in ("photo", "email", "audio")):
                print(f"\nreached cap of {cap} for every type, stopping. "
                      f"photo={counts['photo']} email={counts['email']} audio={counts['audio']}")
                break


def main() -> None:
    p = argparse.ArgumentParser(description="RampRush truck data collector")
    p.add_argument("--team-id", default="100")
    p.add_argument("--advance", action="store_true",
                   help="auto-reply with placeholder to pull the next truck")
    p.add_argument("--cap", type=int, default=100,
                   help="max files to save per type (photo/email/audio)")
    args = p.parse_args()
    try:
        asyncio.run(run(args.team_id, args.advance, args.cap))
    except KeyboardInterrupt:
        print("\ninterrupted, bye.")


if __name__ == "__main__":
    main()
