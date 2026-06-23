#!/usr/bin/env python3
"""Parse suppliers.json into a {supplier_id: supplier_name} dictionary."""

import json
import sys
from pathlib import Path


def parse_suppliers(path: str | Path) -> dict[int, str]:
    """Parse a suppliers JSON file into a {supplier_id: supplier_name} dict.

    The file is expected to contain a list of objects, each with a numeric
    "supplier_id" and a string "supplier_name".
    """
    with open(path, encoding="utf-8") as f:
        suppliers = json.load(f)

    return {entry["supplier_id"]: entry["supplier_name"] for entry in suppliers}


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "suppliers.json"
    suppliers = parse_suppliers(path)
    print(f"Parsed {len(suppliers)} suppliers from {path}")
    # Print as JSON so the result is easy to consume/pipe elsewhere.
    print(json.dumps(suppliers, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
