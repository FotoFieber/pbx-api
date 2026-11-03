# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pbx_api.domain.call_builder import build_calls
from pbx_api.domain.models import Direction, Outcome
from pbx_api.domain.summary import summarize
from pbx_api.domain.topology import Overrides, parse_channel

from . import fixtures as fx

TZ = ZoneInfo("UTC")


def one(rows, **kw):
    calls = build_calls(rows, fx.topology(**kw), TZ)
    assert len(calls) == 1
    return calls[0]


def test_parse_channel():
    assert parse_channel("PJSIP/gw-a-0000001c") == ("PJSIP", "gw-a")
    assert parse_channel("Local/123@from-pstn-00000000;2") == ("Local", "123@from-pstn")
    assert parse_channel("") is None


def test_internal_unanswered():
    c = one(fx.INTERNAL_UNANSWERED)
    assert c.direction is Direction.internal
    assert c.outcome is Outcome.no_answer
    assert (c.caller.extension_id, c.caller.name) == ("201", "Kitchen")
    assert (c.callee.extension_id, c.callee.name) == ("202", "Office")
    assert c.line is None
    assert c.ringing_extensions == ["202"]
    assert c.ring_sec == 12 and c.talk_sec == 0


def test_internal_answered():
    c = one(fx.INTERNAL_ANSWERED)
    assert c.outcome is Outcome.answered
    assert c.answered_by == "201"
    assert (c.ring_sec, c.talk_sec) == (4, 30)


def test_inbound_answered():
    c = one(fx.INBOUND_ANSWERED)
    assert c.direction is Direction.inbound
    assert c.outcome is Outcome.answered
    assert c.caller.number == fx.EXTERNAL and c.caller.name is None
    assert c.callee.extension_id == "201" and c.callee.number == "15550100"
    assert c.line.id == "gw-a" and c.line.number == "15550100"
    assert c.dialed_number == "15550100"


def test_inbound_gateway_double_is_merged():
    c = one(fx.INBOUND_MISSED_DOUBLE)
    assert c.outcome is Outcome.missed
    assert c.id == "1003.1"


def test_inbound_double_not_merged_when_dedup_disabled():
    calls = build_calls(fx.INBOUND_MISSED_DOUBLE, fx.topology(), TZ, dedup_window_sec=0)
    assert len(calls) == 2


def test_inbound_ring_group():
    c = one(fx.INBOUND_RING_GROUP)
    assert c.direction is Direction.inbound
    assert c.ringing_extensions == ["201", "202"]
    assert c.answered_by == "202"
    assert c.callee.extension_id == "202"
    assert c.caller.name == "Dr. Example"  # from phone book
    assert c.line.id == "gw-b"
    assert c.talk_sec == 19 and c.ring_sec == 6


def test_outbound_answered():
    c = one(fx.OUTBOUND_ANSWERED)
    assert c.direction is Direction.outbound
    assert c.caller.extension_id == "202"
    assert c.callee.number == "15550188"
    assert c.line.id == "provider"
    assert c.outcome is Outcome.answered and c.talk_sec == 33


def test_outbound_loop_trunk():
    c = one(fx.OUTBOUND_LOOP)
    assert c.direction is Direction.outbound
    assert c.line.id == "loop"
    assert c.caller.extension_id == "201"
    assert c.dialed_number == "15550101"
    assert c.ringing_extensions == ["202"]
    assert c.outcome is Outcome.no_answer


def test_outbound_busy():
    c = one(fx.OUTBOUND_BUSY)
    assert c.outcome is Outcome.busy
    assert c.callee.name == "Dr. Example"


def test_all_sorted_newest_first_and_summary():
    calls = build_calls(fx.ALL_ROWS, fx.topology(), TZ)
    assert len(calls) == 8
    assert calls[0].started_at > calls[-1].started_at
    s = summarize(calls, datetime(2030, 1, 10, tzinfo=timezone.utc), datetime(2030, 1, 11, tzinfo=timezone.utc), "line")
    assert s.totals.total == 8
    assert s.totals.inbound == 3 and s.totals.outbound == 3 and s.totals.internal == 2
    assert s.totals.missed_inbound == 1
    assert {g.key for g in s.groups} == {"none", "gw-a", "gw-b", "provider", "loop"}


def test_overrides_and_topology():
    topo = fx.topology(Overrides(line_labels={"gw-a": "Kitchen line"}, extension_names={"203": "Laptop"},
                                 hidden_lines={"loop"}, default_line_label="number"))
    assert topo.lines["gw-a"].label == "Kitchen line"
    assert topo.lines["gw-b"].label == "15550101"  # default label = number
    assert topo.lines["provider"].label == "provider"  # no number -> name
    assert "loop" not in topo.lines
    assert topo.extensions["203"].name == "Laptop"
    lines = {l.id: l for l in topo.line_models()}
    assert lines["gw-a"].inbound_destination.type == "extension"
    assert lines["gw-b"].inbound_destination.type == "ring_group"
    assert lines["gw-a"].outbound_routes == ["kitchen-out"]
