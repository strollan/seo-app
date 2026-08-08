"""
Entry point for a single Lead Finder live-scan job, run as its own OS
process (see agents.lead_live_job_agent.create_job()).

Scan work used to run on a background thread inside the shared FastAPI/
Uvicorn web process. That thread shared the web process's GIL and single
asyncio event loop with every other request the server was handling, so
heavy or stuck crawl work could starve unrelated routes -- including a
plain `/` or `/login` request -- and a stuck crawl thread could never be
forcibly stopped, only asked nicely via a flag it might not check for a
long time. Running each scan as its own process makes cancellation a real
OS-level operation (SIGTERM/SIGKILL) instead of a cooperative request that
a stuck thread can ignore indefinitely, and means nothing this process does
can ever block the web process from answering other users.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.lead_live_job_agent import run_job_in_subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()

    run_job_in_subprocess(args.job_id)


if __name__ == "__main__":
    main()
