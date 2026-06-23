#!/usr/bin/env python3
"""Offline dry-run of the joel.py pipeline against locally cached data.

Uses the real extraction (claude CLI), real whisper transcription and real
damage detection, but reads email/audio/photo from the local folders instead
of the live stream, and prints the decision WITHOUT submitting anything.
"""
import json
import sys
from pathlib import Path

import full_run_v2 as joel

ROOT = Path(__file__).parent


def local_docs(truck_id: str, raw: dict):
    """Rewrite a raw message's doc URLs to point at local files where present."""
    docs = []
    for doc in raw.get("documentation", []):
        t = doc.get("type")
        if t == "email":
            docs.append(doc)
        elif t == "audio" and (ROOT / "audio" / f"{truck_id}.mp3").exists():
            docs.append({"type": "audio", "_local": str(ROOT / "audio" / f"{truck_id}.mp3")})
        elif t == "photo" and (ROOT / "photos" / f"{truck_id}.jpg").exists():
            docs.append({"type": "photo", "_local": str(ROOT / "photos" / f"{truck_id}.jpg")})
    return docs


# monkeypatch gather_text/gather_damage to use local files
def patch(agent):
    def gather_text(truck_id, docs):
        parts = []
        for doc in docs:
            if doc.get("type") == "email" and doc.get("text"):
                parts.append(doc["text"])
        for doc in docs:
            if doc.get("type") == "audio" and doc.get("_local"):
                txt = agent.transcriber.transcribe(Path(doc["_local"]))
                if txt:
                    print(f"  transcript: {txt[:120]}")
                    parts.append(txt)
        return "\n".join(parts).strip()

    def gather_damage(truck_id, docs):
        for doc in docs:
            if doc.get("type") == "photo" and doc.get("_local"):
                return joel.detect_damage(Path(doc["_local"]))
        return False

    agent.gather_text = gather_text
    agent.gather_damage = gather_damage


def main():
    ids = sys.argv[1:] or ["TRK-001", "TRK-008", "TRK-009", "TRK-010",
                           "TRK-016", "TRK-036", "TRK-042", "TRK-001"]
    suppliers = joel.load_suppliers()
    transcriber = joel.Transcriber("medium")
    agent = joel.Agent("offline", suppliers, transcriber)
    patch(agent)

    for tid in ids:
        raw_path = ROOT / "raw" / f"{tid}.json"
        if not raw_path.exists():
            continue
        raw = json.loads(raw_path.read_text())
        # load email text into the email doc
        email_path = ROOT / "emails" / f"{tid}.txt"
        for doc in raw.get("documentation", []):
            if doc.get("type") == "email" and email_path.exists():
                doc["text"] = email_path.read_text()
        msg = {
            "truck_id": tid,
            "priority": raw.get("priority"),
            "ramp_status": raw.get("ramp_status", []),
            "documentation": local_docs(tid, raw),
        }
        url_photo = next((d["url"] for d in raw.get("documentation", [])
                          if d.get("type") == "photo"), "")
        gt = "DAMAGED" if "/damaged/" in url_photo else (
            "undamaged" if url_photo else "no-photo")
        print(f"\n[{tid}] (url-truth: {gt})")
        result = agent.process(msg)
        print(f"  PAYLOAD -> {result['endpoint']}: "
              f"{json.dumps(result['payload'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
