"""Slice 3 end to end under the acceptance profile's display settings (rows 9 and 10).

The gateway's profile sets what the acceptance profile sets for these rows (docs/design.md, How slice 3 does
it): ``display.suppress_warning_notifications`` (Hermes' own diagnostic lines off), and an
``ephemeral_system_ttl``, under which Hermes deletes its system replies, "Stopped" among them, after a
while. What a scenario asserts is everything the chat shows.
"""

import sys
import time
from typing import Any

import pytest

from checks.e2e.rig import C, PluginTelegramDriver, rig_fixtures
from tests.fakes.fake_llm_provider import Error, ToolCall
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

TTL = 2  # seconds


class AcceptanceProfileDriver(PluginTelegramDriver):
    def gateway_config(self) -> dict[str, Any]:
        return C.merge(super().gateway_config(),
                       {"display": {"suppress_warning_notifications": True, "ephemeral_system_ttl": TTL}})


rig, rig_stream = rig_fixtures(AcceptanceProfileDriver)


def _shown(rig: Any, chat_id: str) -> list[str]:
    return [C.norm(v.text) for v in rig.drv.visible(chat_id)]


def _frames_after(rig: Any, chat_id: str, at: float) -> list[Any]:
    return [c for c in rig.drv.standin.calls_of("sendRichMessageDraft", "sendMessageDraft")
            if str(c.params.get("chat_id")) == str(chat_id) and c.at > at and not c.faulted]


def test_a_failed_turn_leaves_one_message_saying_the_answer_failed_and_no_preview(rig):
    # Row 10, everything the chat shows.
    token = "lp-fail"
    rig.director.script(token, *[Error(400, "scripted failure") for _ in range(4)])
    user = rig.fresh_user()
    inbound = rig.drv.dm(f"this will fail [in:{token}]", user_id=user)
    wait_until(lambda: _shown(rig, inbound.chat_id), "the failure message", timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)
    time.sleep(3.0)
    (shown,) = _shown(rig, inbound.chat_id)
    assert "didn't answer" in shown or "failed" in shown.lower(), shown
    sent = [c for c in rig.drv.standin.calls_of("sendMessage", "sendRichMessage")
            if str(c.params.get("chat_id")) == inbound.chat_id and not c.faulted]
    assert not _frames_after(rig, inbound.chat_id, sent[-1].at), "the preview came back after the failure message"


def test_stopped_stays_in_the_chat_when_hermes_deletes_its_system_replies(rig):
    # Row 9: the preview is replaced by a persisted "Stopped".
    token = "lp-stop"
    rig.director.script(token, ToolCall("terminal", {"command": "sleep 8"}), C.answer("A-lp-stop", "slept"))
    user = rig.fresh_user()
    inbound = rig.drv.dm(f"take a nap [in:{token}]", user_id=user)
    frame = wait_until(lambda: next((c for c in reversed(rig.drv.standin.calls_of("sendRichMessageDraft"))
                                     if str(c.params.get("chat_id")) == inbound.chat_id), None),
                       "the preview", timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)
    time.sleep(1.0)
    rig.drv.standin.stop_generation(inbound.chat_id, int(frame.params["draft_id"]))
    wait_until(lambda: any("Stopped" in t for t in _shown(rig, inbound.chat_id)), "the Stopped message", timeout=5,
               on_timeout=rig.ctx)
    time.sleep(TTL + 2.0)
    assert any("Stopped" in t for t in _shown(rig, inbound.chat_id)), "Stopped was deleted"
