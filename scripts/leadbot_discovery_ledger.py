#!/usr/bin/env python3
"""Operator tool for the Lead Finder discovery spend ledger.

  init   create an empty ledger if none exists (never overwrites)
  show   print today's (or --day) committed spend and request counts

Paid discovery (Maps and organic) refuses to run until `init` has been run,
because a missing ledger must never silently reset the daily cap.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents import discovery_spend_guard as guard  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["init", "show"])
    parser.add_argument("--ledger", default="", help="ledger path (default: LEADBOT_DISCOVERY_LEDGER_PATH or data/)")
    parser.add_argument("--day", default="", help="UTC day YYYY-MM-DD for show (default today)")
    args = parser.parse_args(argv)
    path = Path(args.ledger) if args.ledger else guard.ledger_path()

    if args.command == "init":
        created = guard.initialize_ledger(path)
        print(f"{'created' if created else 'already exists, unchanged'}: {path}")
        return 0

    try:
        print(json.dumps(guard.read_day_summary(args.day or None, path), indent=2))
    except guard.LedgerUnavailable as exc:
        print(f"ledger unavailable: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
