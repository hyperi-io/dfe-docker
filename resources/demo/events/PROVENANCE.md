# Where the demo's captured polls come from

`feodo-c2/demo.log` is one real poll of the abuse.ch Feodo Tracker botnet C2 blocklist, captured 2026-11-16 05:12:57Z. A line is one poll: `polled_ms`, the capture time in epoch milliseconds, and `body`, the blocklist array from `https://feodotracker.abuse.ch/downloads/ipblocklist.json` under a `rows` key so the replay unit reads it at `/rows`. Nothing else was changed.

Feodo Tracker data is free to use for commercial and non-commercial purposes under CC0, with no key ([abuse.ch terms of use](https://abuse.ch/terms-of-use/), section 3.4).
