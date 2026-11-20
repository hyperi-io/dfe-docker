# Where the cyber demo's events come from

Every `<source>/demo.log` here is written by [`generate.py`](generate.py), seeded, so a rerun writes the same bytes. The firewall lines are the demo's own generated set. The okta, crowdstrike and o365 lines are records from the public samples below, varied per line (user, address, place, time and ids) and wrapped in the envelope an Elastic Agent ships.

| Upstream | Licence | Used for |
|---|---|---|
| [panther-labs/panther-analysis](https://github.com/panther-labs/panther-analysis) | Apache-2.0, [text](LICENSES/Apache-2.0-panther-analysis.txt) | `okta/` from its Okta system log samples, `crowdstrike/` from its Falcon streaming samples (the `EppDetectionSummaryEvent` and `AuthActivityAuditEvent` records) |
| [splunk/botsv3](https://github.com/splunk/botsv3) | CC0 1.0, [text](LICENSES/CC0-1.0-botsv3.txt) | `o365/` from its Microsoft 365 management activity records |

The samples were read from `tests/fixtures/unencumbered/` in the dfe-transform-elastic repo, which records the same upstreams. Their domains are moved onto `example.com`, `example.net` or `example.org` (RFC 2606). Their people are moved onto the demo's own staff and mailboxes, one name per person across okta, o365 and crowdstrike and each o365 record names one person in `UserId`, `Actor` and `UserKey`. Staff addresses sit in RFC 5737 space. The known-bad networks are eight /28 blocks of 192.0.2.0/24, outside every address pool the firewalls and staff use, with two hosts on each appearing as outside addresses on the firewalls and as failed sign-ins.

CC0 waives attribution. The Apache-2.0 material requires it and this table with its licence text is the record.
