# Where the demo's fetcher replay comes from

`feodo-c2/demo.log` is one poll of the abuse.ch Feodo Tracker botnet command-and-control IP blocklist, captured by [`capture.py`](capture.py) at 2026-11-17T02:37:08Z. The line holds `polled_ms`, the capture time in epoch milliseconds, and `body`, the 5 servers the tracker answered with from `https://feodotracker.abuse.ch/downloads/ipblocklist.json`, using the URL and headers [`feodo-c2.yaml`](../sources/feodo-c2.yaml) polls with.

Nothing in the file was changed after capture. The source is a dump, which takes the whole list each poll, so one poll is the whole store. [`replay_fetch.py`](../../../scripts/replay_fetch.py) moves integers within a day of the poll time forward to now. The blocklist holds none, since its dates are strings, so every value is served as captured.

| Upstream | Licence | Used for |
|---|---|---|
| [abuse.ch Feodo Tracker](https://feodotracker.abuse.ch/blocklist/) | CC0, per section 3.4 of the [abuse.ch terms of use](https://abuse.ch/terms-of-use/): Feodo Tracker is "free to use without limitations" for commercial and non-commercial purposes "according to Creative Commons principles (CC0)" | `feodo-c2/` |

CC0 needs no attribution and no licence text, so none is copied in. The terms ask for no attribution. Section 7.3 of the same terms restricts copying and derivative works of the platforms without consent, and says nothing specific about the CC0 feeds. The addresses are the tracker's published botnet servers, not personal data.
