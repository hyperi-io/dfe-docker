#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resources/demo/events/capture.py
#  Purpose:      Capture the demo pack's replay of the Feodo Tracker blocklist
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Write feodo-c2/demo.log beside this file, one line holding the poll time in milliseconds and the blocklist the tracker answered.

The URL and headers are the ones ../sources/feodo-c2.yaml polls with, so scripts/replay_fetch.py serves the fetcher the body the live tracker would. The source is a dump, which takes the whole list each poll, so one poll is enough. Run it on a host with egress.
"""

import json
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = "https://feodotracker.abuse.ch/downloads/ipblocklist.json"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "DFE-demo (https://github.com/hyperi-io/dfe-docker)",
}
TIMEOUT_SECONDS = 20


def main() -> int:
    polled_ms = int(time.time() * 1000)
    request = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as answer:  # noqa: S310
        body = json.loads(answer.read())
    line = json.dumps({"polled_ms": polled_ms, "body": body}, separators=(",", ":"))
    (HERE / "feodo-c2" / "demo.log").write_text(
        line + "\n", encoding="utf-8", newline="\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
