#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         resources/demo/samples/generate.py
#  Purpose:      Regenerate the demo's Cisco ASA and FortiGate sample sets
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Write cisco_asa/demo.log and fortinet/demo.log beside this file. Seeded, so a rerun writes the same bytes, on RFC 1918 and RFC 5737 addresses only."""

import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINES = 6000
SEED = 20261027

INSIDE = [
    f"10.{a}.{b}.{c}"
    for a in (10, 20)
    for b in (10, 20, 30, 40, 50)
    for c in range(5, 250, 9)
]
SERVERS = [f"192.168.{a}.{b}" for a in (50, 60, 70) for b in range(10, 60, 3)]
OUTSIDE = [f"198.51.100.{n}" for n in range(2, 254)] + [
    f"203.0.113.{n}" for n in range(2, 254)
]
NAT = [f"192.0.2.{n}" for n in range(10, 30)]
USERS = [
    "alice",
    "bob",
    "carol",
    "dave",
    "erin",
    "frank",
    "grace",
    "heidi",
    "ivan",
    "judy",
    "mallory",
    "oscar",
    "peggy",
    "trent",
    "victor",
    "walter",
]
ACLS = ["outside_access_in", "inside_access_out", "dmz_access_in", "guest_access_in"]
ZONES = [
    ("outside", "inside"),
    ("outside", "dmz"),
    ("guest", "outside"),
    ("inside", "dmz"),
]
TCP_PORTS = [
    22,
    25,
    80,
    110,
    143,
    389,
    443,
    445,
    587,
    636,
    993,
    1433,
    3306,
    3389,
    5432,
    8080,
    8443,
]
UDP_PORTS = [53, 123, 161, 500, 514, 1812, 4500]
SERVICES = {
    22: "SSH",
    25: "SMTP",
    53: "DNS",
    80: "HTTP",
    123: "NTP",
    143: "IMAP",
    161: "SNMP",
    389: "LDAP",
    443: "HTTPS",
    445: "SMB",
    993: "IMAPS",
    3389: "RDP",
    8080: "HTTP-8080",
}
SITES = [
    "www.example.com",
    "mail.example.org",
    "files.example.net",
    "portal.example.com",
    "updates.example.org",
    "cdn.example.net",
]


def _asa_lines(*, rng: random.Random) -> list[str]:
    """The Cisco ASA sample set, connection pairs mixed with denies, AAA and scan drops."""

    def dur() -> str:
        """A connection duration as ASA prints it."""
        return f"{rng.randint(0, 2)}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"

    def eph() -> int:
        """An ephemeral port."""
        return rng.randint(1024, 65535)

    out = []
    conn = rng.randint(100000, 400000)
    while len(out) < LINES:
        conn += rng.randint(1, 37)
        outer, inner = rng.choice(ZONES)
        src, dst, nat, cli = (
            rng.choice(OUTSIDE),
            rng.choice(SERVERS),
            rng.choice(NAT),
            rng.choice(INSIDE),
        )
        port, sport = rng.choice(TCP_PORTS), eph()
        roll = rng.random()
        if roll < 0.30:
            out += [
                f"%ASA-6-302013: Built inbound TCP connection {conn} for {outer}:{src}/{sport} ({src}/{sport}) to {inner}:{dst}/{port} ({nat}/{port})",
                f"%ASA-6-302014: Teardown TCP connection {conn} for {outer}:{src}/{sport} to {inner}:{dst}/{port} duration {dur()} bytes {rng.randint(60, 2_000_000)} TCP FINs",
            ]
        elif roll < 0.55:
            reason = rng.choice(
                [
                    "TCP FINs",
                    "TCP Reset-O",
                    "TCP Reset-I",
                    "Conn-timeout",
                    "SYN Timeout",
                ]
            )
            out += [
                f"%ASA-6-302013: Built outbound TCP connection {conn} for outside:{src}/{port} ({src}/{port}) to inside:{cli}/{sport} ({nat}/{sport})",
                f"%ASA-6-302014: Teardown TCP connection {conn} for outside:{src}/{port} to inside:{cli}/{sport} duration {dur()} bytes {rng.randint(60, 2_000_000)} {reason}",
            ]
        # No 302020/302021 ICMP pairs: a 302021 line OOM-kills the dfe-transform-elastic v1.1.20 instance
        elif roll < 0.69:
            uport = rng.choice(UDP_PORTS)
            out += [
                f"%ASA-6-302015: Built outbound UDP connection {conn} for outside:{src}/{uport} ({src}/{uport}) to inside:{cli}/{sport} ({nat}/{sport})",
                f"%ASA-6-302016: Teardown UDP connection {conn} for outside:{src}/{uport} to inside:{cli}/{sport} duration 0:00:{rng.randint(0, 59):02d} bytes {rng.randint(40, 4000)}",
            ]
        elif roll < 0.74:
            out += [
                f"%ASA-6-305011: Built dynamic TCP translation from inside:{cli}/{sport} to outside:{nat}/{eph()}",
                f"%ASA-6-305012: Teardown dynamic TCP translation from inside:{cli}/{sport} to outside:{nat}/{eph()} duration {dur()}",
            ]
        elif roll < 0.78:
            out += [
                f"%ASA-7-609001: Built local-host inside:{cli}",
                f"%ASA-7-609002: Teardown local-host inside:{cli} duration {dur()}",
            ]
        elif roll < 0.84:
            proto = rng.choice(["tcp", "udp"])
            dport = port if proto == "tcp" else rng.choice(UDP_PORTS)
            out.append(
                f'%ASA-4-106023: Deny {proto} src {outer}:{src}/{sport} dst {inner}:{dst}/{dport} by access-group "{rng.choice(ACLS)}" [0x0, 0x0]'
            )
        elif roll < 0.88:
            verdict = rng.choice(["permitted", "permitted", "denied"])
            out.append(
                f"%ASA-6-106100: access-list {rng.choice(ACLS)} {verdict} tcp {outer}/{src}({sport}) -> {inner}/{dst}({port}) hit-cnt {rng.randint(1, 40)} first hit [0x{rng.getrandbits(32):08x}, 0x0]"
            )
        elif roll < 0.91:
            out.append(
                f"%ASA-2-106001: Inbound TCP connection denied from {src}/{sport} to {dst}/{port} flags SYN on interface {outer}"
            )
        elif roll < 0.93:
            out.append(
                f"%ASA-2-106006: Deny inbound UDP from {src}/{sport} to {dst}/{rng.choice(UDP_PORTS)} on interface {outer}"
            )
        elif roll < 0.945:
            out.append(
                f"%ASA-3-710003: TCP access denied by ACL from {src}/{sport} to outside:{nat}/{rng.choice((22, 23, 443))}"
            )
        elif roll < 0.975:
            out.append(
                f"%ASA-6-113004: AAA user authentication Successful : server =  10.10.5.{rng.randint(5, 9)} : user = {rng.choice(USERS)}"
            )
        elif roll < 0.995:
            out.append(
                f"%ASA-6-113005: AAA user authentication Rejected : reason = AAA failure : server = 10.10.5.{rng.randint(5, 9)} : user = {rng.choice(USERS)} : user IP = {cli}"
            )
        else:
            burst = rng.randint(11, 60)
            out.append(
                f"%ASA-4-733100: [ Scanning] drop rate-1 exceeded. Current burst rate is {burst} per second, max configured rate is 10; Current average rate is {rng.randint(5, 9)} per second, max configured rate is 5; Cumulative total count is {rng.randint(1000, 90000)}"
            )
    return out[:LINES]


def _fortinet_lines(*, rng: random.Random) -> list[str]:
    """The FortiGate sample set, forward and local-in traffic with some web filter lines."""
    out = []
    session = rng.randint(1_000_000, 4_000_000)
    devices = [
        ("fgt-edge-01", "FGT60E0000000001"),
        ("fgt-edge-02", "FGT60E0000000002"),
        ("fgt-branch-01", "FGT40F0000000003"),
    ]
    for index in range(LINES):
        session += rng.randint(1, 23)
        devname, devid = rng.choice(devices)
        stamp = f"date=2026-10-27 time={index // 3600 % 24:02d}:{index // 60 % 60:02d}:{index % 60:02d}"
        head = f'{stamp} devname="{devname}" devid="{devid}" vd="root"'
        src, sport = rng.choice(INSIDE), rng.randint(1024, 65535)
        roll = rng.random()
        if roll < 0.04:
            site = rng.choice(SITES)
            out.append(
                f'{head} logid="0317013312" type="utm" subtype="webfilter" eventtype="ftgd_allow" level="notice" policyid={rng.randint(1, 30)} sessionid={session} user="" '
                f'srcip={src} srcport={sport} srcintf="port{rng.randint(2, 5)}" dstip={rng.choice(OUTSIDE)} dstport=443 proto=6 service="HTTPS" hostname="{site}" '
                f'profile="default" action="passthrough" reqtype="direct" url="/" sentbyte={rng.randint(100, 4000)} rcvdbyte={rng.randint(0, 60000)} direction="outgoing" '
                f'msg="URL belongs to an allowed category in policy" method="domain" cat=52 catdesc="Information Technology"'
            )
            continue
        local = roll < 0.12
        if local:
            dst, dport = (
                f"192.168.1.{rng.choice((1, 99))}",
                rng.choice((22, 443, 161, 123)),
            )
            action, subtype, policy = rng.choice(["accept", "deny"]), "local", 0
        else:
            proto_udp = rng.random() < 0.25
            dst = rng.choice(OUTSIDE)
            dport = rng.choice(UDP_PORTS) if proto_udp else rng.choice(TCP_PORTS)
            action = rng.choices(
                ["close", "accept", "timeout", "deny"], weights=[55, 20, 15, 10]
            )[0]
            subtype, policy = "forward", rng.randint(1, 30)
        proto = 17 if dport in UDP_PORTS else 6
        service = SERVICES.get(dport, f"{'udp' if proto == 17 else 'tcp'}/{dport}")
        sent = 0 if action == "deny" else rng.randint(40, 3_000_000)
        rcvd = 0 if action == "deny" else rng.randint(0, 9_000_000)
        out.append(
            f'{head} logid="00000000{13 if subtype == "forward" else 15}" type="traffic" subtype="{subtype}" level="notice" '
            f'srcip={src} srcport={sport} srcintf="port{rng.randint(2, 5)}" srcintfrole="lan" dstip={dst} dstport={dport} '
            f'dstintf="{"root" if local else "wan1"}" dstintfrole="{"undefined" if local else "wan"}" sessionid={session} proto={proto} action="{action}" '
            f'policyid={policy} policytype="{"local-in-policy" if local else "policy"}" service="{service}" dstcountry="Reserved" srccountry="Reserved" '
            f'trandisp="{"noop" if local else "snat"}" duration={0 if action == "deny" else rng.randint(1, 3600)} sentbyte={sent} rcvdbyte={rcvd} '
            f'sentpkt={sent // 1400 + (sent > 0)} rcvdpkt={rcvd // 1400 + (rcvd > 0)} appcat="unscanned"'
        )
    return out


def main() -> int:
    rng = random.Random(SEED)
    for package, lines in (
        ("cisco_asa", _asa_lines(rng=rng)),
        ("fortinet", _fortinet_lines(rng=rng)),
    ):
        path = HERE / package / "demo.log"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
