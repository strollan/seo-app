#!/usr/bin/env python3
"""Run one explicit connector pass. No background mode is provided."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.actii_file_store import StorageError
from agents.actii_pull_connector import ConnectorError, PullConnector, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Owner-only connector JSON configuration")
    parser.add_argument("--once", action="store_true", required=True, help="Explicitly perform at most one import")
    args = parser.parse_args()
    try:
        print(PullConnector(load_config(args.config)).run_once())
    except (ConnectorError, StorageError, OSError, ValueError, KeyError, TypeError):
        # Never dump an exception, response, configuration, credential or prospect.
        print("Connector stopped. Check private configuration, journal, and server status; do not clear recovery records.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
