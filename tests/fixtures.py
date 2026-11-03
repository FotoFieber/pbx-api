# SPDX-License-Identifier: Apache-2.0
"""Synthetic PBX data for tests.

All names, numbers and identifiers are invented. Numbers use the 555-01xx range,
which is reserved for fictional use.
"""

from __future__ import annotations

from datetime import datetime

from pbx_api.domain.topology import Overrides, Topology

USERS = [
    {"extension": "201", "name": "Kitchen"},
    {"extension": "202", "name": "Office"},
    {"extension": "203", "name": "Softphone"},
]
DEVICES = [{"id": "201", "tech": "pjsip"}, {"id": "202", "tech": "pjsip"}, {"id": "203", "tech": "pjsip"}]
TRUNKS = [
    {"trunkid": 1, "name": "provider", "tech": "pjsip", "channelid": "provider", "outcid": "", "disabled": "off"},
    {"trunkid": 2, "name": "gw-a", "tech": "pjsip", "channelid": "gw-a", "outcid": "15550100", "disabled": "off"},
    {"trunkid": 3, "name": "gw-b", "tech": "pjsip", "channelid": "gw-b", "outcid": "15550101", "disabled": "off"},
    {"trunkid": 4, "name": "loop", "tech": "custom", "channelid": "Local/$OUTNUM$@from-pstn", "outcid": "", "disabled": "off"},
]
INCOMING = [
    {"extension": "15550100", "destination": "from-did-direct,201,1"},
    {"extension": "15550101", "destination": "ext-group,300,1"},
]
RING_GROUPS = [{"grpnum": "300", "strategy": "ringall", "grplist": "201-202", "description": "Everyone"}]
ROUTES = [
    {"route_name": "default", "trunk_name": "provider"},
    {"route_name": "kitchen-out", "trunk_name": "gw-a"},
    {"route_name": "own-numbers", "trunk_name": "loop"},
]
PHONEBOOK = [{"name": "Dr. Example", "number": "+1 555 0177"}]

EXTERNAL = "15550199"


def topology(overrides: Overrides | None = None) -> Topology:
    return Topology.from_rows(
        users=USERS,
        devices=DEVICES,
        trunks=TRUNKS,
        incoming=INCOMING,
        ring_groups=RING_GROUPS,
        routes=ROUTES,
        phonebook=PHONEBOOK,
        overrides=overrides,
        match_digits=7,
    )


def row(when: str, linkedid: str, seq: int, **kw) -> dict:
    base = {
        "calldate": datetime.fromisoformat(when),
        "src": "",
        "dst": "",
        "dcontext": "",
        "channel": "",
        "dstchannel": "",
        "disposition": "NO ANSWER",
        "duration": 0,
        "billsec": 0,
        "linkedid": linkedid,
        "sequence": seq,
        "did": "",
        "cnum": "",
        "cnam": "",
        "dst_cnam": "",
    }
    base.update(kw)
    return base


# Internal call 201 -> 202, not answered
INTERNAL_UNANSWERED = [
    row("2030-01-10 10:00:00", "1000.1", 1, src="201", dst="202", dcontext="ext-local",
        channel="PJSIP/201-00000001", dstchannel="PJSIP/202-00000002", duration=12, cnum="201", cnam="Kitchen"),
]

# Internal call 202 -> 201, answered after 4 s, 30 s talk
INTERNAL_ANSWERED = [
    row("2030-01-10 10:05:00", "1001.1", 1, src="202", dst="201", dcontext="ext-local",
        channel="PJSIP/202-00000003", dstchannel="PJSIP/201-00000004", disposition="ANSWERED",
        duration=34, billsec=30, cnum="202", cnam="Office"),
]

# Inbound via gw-a to its DID, answered by 201
INBOUND_ANSWERED = [
    row("2030-01-10 11:00:00", "1002.1", 1, src=EXTERNAL, dst="201", dcontext="ext-local",
        channel="PJSIP/gw-a-00000005", dstchannel="PJSIP/201-00000006", disposition="ANSWERED",
        duration=20, billsec=15, did="15550100", cnum=EXTERNAL, cnam=EXTERNAL),
]

# Inbound missed; the gateway delivered the same call twice in parallel
INBOUND_MISSED_DOUBLE = [
    row("2030-01-10 12:00:00", "1003.1", 1, src=EXTERNAL, dst="201", dcontext="ext-local",
        channel="PJSIP/gw-a-00000007", dstchannel="PJSIP/201-00000009", duration=8, did="15550100",
        cnum=EXTERNAL, cnam=EXTERNAL),
    row("2030-01-10 12:00:00", "1003.2", 1, src=EXTERNAL, dst="201", dcontext="ext-local",
        channel="PJSIP/gw-a-00000008", dstchannel="PJSIP/201-0000000a", duration=8, did="15550100",
        cnum=EXTERNAL, cnam=EXTERNAL),
]

# Inbound to the ring-group DID; both ring, 202 answers. Caller is in the phone book.
INBOUND_RING_GROUP = [
    row("2030-01-10 13:00:00", "1004.1", 1, src="15550177", dst="300", dcontext="ext-group",
        channel="PJSIP/gw-b-0000000b", dstchannel="Local/201@from-internal-00000001;1", duration=25,
        billsec=0, did="15550101", cnum="15550177", cnam="15550177"),
    row("2030-01-10 13:00:00", "1004.1", 2, src="15550177", dst="201", dcontext="from-internal",
        channel="Local/201@from-internal-00000001;2", dstchannel="PJSIP/201-0000000c", duration=6,
        did="15550101", cnum="15550177"),
    row("2030-01-10 13:00:00", "1004.1", 3, src="15550177", dst="202", dcontext="from-internal",
        channel="Local/202@from-internal-00000002;2", dstchannel="PJSIP/202-0000000d", disposition="ANSWERED",
        duration=25, billsec=19, did="15550101", cnum="15550177"),
]

# Outbound from 202 via the provider trunk, answered
OUTBOUND_ANSWERED = [
    row("2030-01-10 14:00:00", "1005.1", 1, src="15550101", dst="15550188", dcontext="from-internal",
        channel="PJSIP/202-0000000e", dstchannel="PJSIP/provider-0000000f", disposition="ANSWERED",
        duration=40, billsec=33, cnum="202", cnam="Office"),
]

# Outbound from 201 to the own DID of gw-b: routed back in through the loop trunk to 202
OUTBOUND_LOOP = [
    row("2030-01-10 15:00:00", "1006.1", 1, src="201", dst="15550101", dcontext="from-internal",
        channel="PJSIP/201-00000010", dstchannel="Local/15550101@from-pstn-00000003;1", duration=5,
        cnum="201", cnam="Kitchen"),
    row("2030-01-10 15:00:00", "1006.1", 2, src="201", dst="202", dcontext="ext-local",
        channel="Local/15550101@from-pstn-00000003;2", dstchannel="PJSIP/202-00000011", duration=5,
        did="15550101", cnum="201", cnam="Kitchen"),
]

# Outbound from 201 via gw-a, callee busy
OUTBOUND_BUSY = [
    row("2030-01-10 16:00:00", "1007.1", 1, src="15550100", dst="15550177", dcontext="from-internal",
        channel="PJSIP/201-00000012", dstchannel="PJSIP/gw-a-00000013", disposition="BUSY", duration=2,
        cnum="201", cnam="Kitchen"),
]

ALL_ROWS = (
    INTERNAL_UNANSWERED
    + INTERNAL_ANSWERED
    + INBOUND_ANSWERED
    + INBOUND_MISSED_DOUBLE
    + INBOUND_RING_GROUP
    + OUTBOUND_ANSWERED
    + OUTBOUND_LOOP
    + OUTBOUND_BUSY
)
