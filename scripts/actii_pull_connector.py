#!/usr/bin/env python3
"""Run one explicit connector pass. No background mode is provided."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.actii_file_store import StorageError
from agents.actii_pull_connector import ConnectorError, PullConnector, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Owner-only connector JSON configuration")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--once", action="store_true", help="Explicitly claim at most one new import")
    operation.add_argument("--recover-only", action="store_true", help="Acknowledge saved receipts; never claim or import")
    args = parser.parse_args(argv)
    try:
        connector = PullConnector(load_config(args.config))
        print(connector.recover_only() if args.recover_only else connector.run_once())
    except (ConnectorError, StorageError, OSError, ValueError, KeyError, TypeError):
        # Never dump an exception, response, configuration, credential or prospect.
        print("Connector stopped. Check private configuration, journal, and server status; do not clear recovery records.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
