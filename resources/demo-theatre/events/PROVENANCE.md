# Where the theatre demo's events come from

Each `<source>/demo.log` here is ten minutes of real polls of a public feed, captured by [`capture.py`](capture.py) from 2026-11-13 21:35:17Z to 21:45:07Z, 60 polls 10 seconds apart. A line is one poll: `polled_ms`, the capture time in epoch milliseconds, and `body`, the feed's answer as it came back.

| Upstream | Licence | Endpoint | Used for |
|---|---|---|---|
| [adsb.lol](https://adsb.lol) | ODbL 1.0, [text](LICENSES/ODbL-1.0-adsb-lol.txt) | `https://api.adsb.lol/v2/point/-33.94/151.18/100`, aircraft within 100 nm of Sydney Kingsford Smith | `adsb-tracks/` |
| [Fintraffic Digitraffic](https://www.digitraffic.fi) | CC BY 4.0, [text](LICENSES/CC-BY-4.0-digitraffic.txt) | `https://meri.digitraffic.fi/api/ais/v1/locations?radius=60&latitude=60.1&longitude=24.95&from=<poll time - 10 minutes>`, vessels within 60 km of Helsinki | `ais-tracks/` |

Nothing in the files was changed after capture. The gzip body Digitraffic sends is stored inflated. When the stack replays them, [`scripts/replay_fetch.py`](../../../scripts/replay_fetch.py) moves every epoch-millisecond time within a day of the poll forward to now, and leaves every other value as it was.

The aircraft and vessel identifiers (ICAO hex, callsign, registration, MMSI) are what the transponders broadcast in the clear. Both licences require attribution, and this table with the licence texts is the record. The ODbL also asks that a database derived from this one be offered under the ODbL.
