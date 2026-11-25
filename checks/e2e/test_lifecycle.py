"""Slice 3 end to end: the preview of a turn, from the first status to its final answer, Stop or failure
(rows 6 to 10 and 17).

A real gateway child with this plugin, a fake model scripted per inbound, and the Bot API stand-in of
checks/e2e/standin.py. What a scenario asserts is what Telegram received; whether a client draws it is live
acceptance.
"""

import sys

import pytest

from checks.e2e.rig import rig_fixtures

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, _rig_stream = rig_fixtures()


def test_the_gateway_asks_telegram_for_stop_presses(rig):
    # Telegram delivers only the update kinds a bot asks for; PTB 22.8's list predates the Stop button.
    polls = [c for c in rig.drv.standin.calls_of("getUpdates") if c.params.get("allowed_updates")]
    assert polls, "the gateway never polled with an update list"
    assert "stopped_message_generation" in polls[-1].params["allowed_updates"]
    assert "message" in polls[-1].params["allowed_updates"]  # PTB's own kinds are still asked for
