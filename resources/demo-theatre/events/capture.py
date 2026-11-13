#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resources/demo-theatre/events/capture.py
#  Purpose:      Capture the theatre pack's replay, ten minutes of each live feed's polls
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Write adsb-tracks/demo.log and ais-tracks/demo.log beside this file, one line per poll: the poll time in milliseconds and the body the feed answered.

The areas, headers and interval are the ones the sources under ../sources poll with, so scripts/replay_fetch.py serves the fetcher the same bodies the live feeds would. Run it on a host with egress, with no arguments for the shipped ten minutes.
"""

import argparse
import gzip
import json
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER = "DFE-demo (https://github.com/hyperi-io/dfe-docker)"
# Sydney, 100 nautical miles round Kingsford Smith
ADSB_URL = "https://api.adsb.lol/v2/point/-33.94/151.18/100"
# The Gulf of Finland, 60 km round Helsinki, reports from the last ten minutes
AIS_URL = "https://meri.digitraffic.fi/api/ais/v1/locations?radius=60&latitude=60.1&longitude=24.95&from={since}"
AIS_WINDOW_MS = 600_000
HEADERS = {
    "adsb-tracks": {"Accept": "application/json", "User-Agent": USER},
    "ais-tracks": {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Digitraffic-User": "DFE-demo",
        "User-Agent": USER,
    },
}
TIMEOUT_SECONDS = 20


def _poll(*, headers: dict[str, str], url: str) -> object:
    """One GET, its gzip body inflated, as JSON."""
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as answer:  # noqa: S310
        raw = answer.read()
        if answer.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minutes", default=10.0, type=float, help="how long to capture"
    )
    parser.add_argument(
        "--interval", default=10.0, type=float, help="seconds between polls"
    )
    args = parser.parse_args()
    for name in HEADERS:
        (HERE / name).mkdir(exist_ok=True)
    files = {
        name: (HERE / name / "demo.log").open("w", encoding="utf-8", newline="\n")
        for name in HEADERS
    }
    deadline = time.monotonic() + args.minutes * 60
    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            polled_ms = int(time.time() * 1000)
            urls = {
                "adsb-tracks": ADSB_URL,
                "ais-tracks": AIS_URL.format(since=polled_ms - AIS_WINDOW_MS),
            }
            for name, url in urls.items():
                body = _poll(headers=HEADERS[name], url=url)
                line = json.dumps(
                    {"polled_ms": polled_ms, "body": body}, separators=(",", ":")
                )
                files[name].write(line + "\n")
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    finally:
        for handle in files.values():
            handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
