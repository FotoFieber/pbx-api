# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timedelta, timezone

from pbx_api.domain.live import LiveModel
from pbx_api.domain.models import Direction, ExtensionStatus

from . import fixtures as fx


class Clock:
    def __init__(self):
        self.t = datetime(2030, 1, 10, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(milliseconds=10)
        return self.t


def model():
    topo = fx.topology()
    return LiveModel(topology=lambda: topo, clock=Clock())


def chan(event, uid, name, linkedid, state, **kw):
    return {"Event": event, "Uniqueid": uid, "Channel": name, "Linkedid": linkedid, "ChannelStateDesc": state, **kw}


def test_device_states_and_registration():
    m = model()
    changes = m.handle({"Event": "DeviceStateChange", "Device": "PJSIP/201", "State": "NOT_INUSE"})
    assert [c.kind for c in changes] == ["extension"]
    assert m.extension_status("201") == (ExtensionStatus.idle, True)
    m.handle({"Event": "DeviceStateChange", "Device": "PJSIP/202", "State": "UNAVAILABLE"})
    assert m.extension_status("202") == (ExtensionStatus.unavailable, False)
    assert m.extension_status("203") == (ExtensionStatus.unknown, None)
    changes = m.handle({"Event": "DeviceStateChange", "Device": "PJSIP/gw-a", "State": "NOT_INUSE"})
    assert changes[0].kind == "line" and changes[0].id == "gw-a"
    assert m.line_registered("gw-a") is True
    assert m.line_registered("loop") is None


def test_inbound_call_lifecycle():
    m = model()
    c1 = m.handle(chan("Newchannel", "1.1", "PJSIP/gw-b-00000001", "1.1", "Ring",
                       CallerIDNum="15550177", CallerIDName="<unknown>", Exten="15550101"))
    assert c1[0].detail == "ringing"
    m.handle(chan("Newchannel", "1.2", "PJSIP/201-00000002", "1.1", "Down"))
    m.handle(chan("Newstate", "1.2", "PJSIP/201-00000002", "1.1", "Ringing"))
    m.handle(chan("Newchannel", "1.3", "PJSIP/202-00000003", "1.1", "Ringing"))
    call = m.active_calls()[0]
    assert call.direction is Direction.inbound and call.state == "ringing"
    assert call.ringing_extensions == ["201", "202"]
    assert call.caller.number == "15550177" and call.caller.name == "Dr. Example"
    assert call.line.id == "gw-b"

    changes = m.handle(chan("Newstate", "1.3", "PJSIP/202-00000003", "1.1", "Up"))
    assert changes[0].detail == "connected"
    m.handle({"Event": "Hangup", "Uniqueid": "1.2"})
    call = m.active_call("1.1")
    assert call.connected_extensions == ["202"] and call.callee.extension_id == "202"

    m.handle({"Event": "Hangup", "Uniqueid": "1.3"})
    ended = m.handle({"Event": "Hangup", "Uniqueid": "1.1"})
    assert [(c.kind, c.id) for c in ended] == [("call_ended", "1.1")]
    assert m.active_calls() == []


def test_connected_call_does_not_ring_again_during_hangup():
    # callee hangs up first: the remaining caller leg must not look like a new ringing call
    m = model()
    m.handle(chan("Newchannel", "4.1", "PJSIP/201-00000020", "4.1", "Ring", CallerIDNum="201", Exten="202"))
    m.handle(chan("Newchannel", "4.2", "PJSIP/202-00000021", "4.1", "Ringing"))
    assert m.handle(chan("Newstate", "4.2", "PJSIP/202-00000021", "4.1", "Up"))[0].detail == "connected"
    m.handle(chan("Newstate", "4.1", "PJSIP/201-00000020", "4.1", "Up"))
    changes = m.handle({"Event": "Hangup", "Uniqueid": "4.2"})
    assert all(c.detail is None for c in changes)  # no state transition event
    assert m.active_call("4.1").state == "connected"
    assert m.handle({"Event": "Hangup", "Uniqueid": "4.1"})[0].kind == "call_ended"


def test_outbound_and_internal():
    m = model()
    m.handle(chan("Newchannel", "2.1", "PJSIP/202-00000004", "2.1", "Ring", CallerIDNum="202", Exten="15550188"))
    m.handle(chan("Newchannel", "2.2", "PJSIP/provider-00000005", "2.1", "Ring"))
    call = m.active_call("2.1")
    assert call.direction is Direction.outbound and call.line.id == "provider"
    assert call.caller.extension_id == "202" and call.callee.number == "15550188"
    m.handle(chan("Newstate", "2.2", "PJSIP/provider-00000005", "2.1", "Up"))
    assert m.active_call("2.1").state == "connected"

    m.handle(chan("Newchannel", "3.1", "PJSIP/201-00000006", "3.1", "Ring", CallerIDNum="201", Exten="203"))
    m.handle(chan("Newchannel", "3.2", "PJSIP/203-00000007", "3.1", "Ringing"))
    call = m.active_call("3.1")
    assert call.direction is Direction.internal
    assert call.callee.extension_id == "203" and call.callee.name == "Softphone"
