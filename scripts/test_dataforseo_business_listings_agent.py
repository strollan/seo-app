from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agents.dataforseo_business_listings_agent import (
    enrich_export_copy,
    extract_items,
    request_business_listings,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", required=True)
    parser.add_argument("--coord", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--export", default="")
    parser.add_argument("--max-rows", type=int, default=10)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    if args.export:
        result = enrich_export_copy(
            export_path=Path(args.export),
            category=args.category,
            location_coordinate=args.coord,
            limit=args.limit,
            max_rows=args.max_rows,
        )

        print("===== ENRICH EXPORT COPY RESULT =====")
        print(json.dumps(result, indent=2)[:16000])
        return

    data = request_business_listings(
        category=args.category,
        location_coordinate=args.coord,
        limit=args.limit,
    )
    items = extract_items(data)

    print("===== API =====")
    print("status:", data.get("status_code"), data.get("status_message"))
    print("cost:", data.get("cost"))
    print("items:", len(items))

    if args.raw:
        print(json.dumps(data, indent=2)[:12000])
    else:
        for item in items[: args.limit]:
            print("---")
            print("title:", item.get("title"))
            print("address:", item.get("address"))
            print("phone:", item.get("phone"))
            print("domain:", item.get("domain"))
            print("url:", item.get("url"))


if __name__ == "__main__":
    main()
