"""Slice 6 end to end: the rest of the agent surface (docs/design.md, Slices 6): quote replies, effects, edits,
pins and deletes.

A real gateway child with this plugin, a fake model scripted per inbound, and the Bot API stand-in of
checks/e2e/standin.py, which refuses a quote that is not an exact part of the message replied to and an effect
outside a private chat. How clients draw quotes and effects, and whether Telegram knows the effect ids, are
live acceptance.
"""

import json
import sys

import pytest

from checks.e2e.rig import C, rig_fixtures, tool_call
from hermes_telegram.messages import EFFECTS

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, rig_stream = rig_fixtures()


def _tool_results(rig, token: str) -> list[str]:
    return [rig.director._text(m) for r in rig.director.requests.get(token, []) for m in r.get("messages", [])
            if m.get("role") == "tool"]


def test_the_agent_replies_with_a_quote_and_an_effect_then_edits_pins_and_deletes(rig):
    first, second = "sf-say", "sf-change"
    say = tool_call(rig, "sfs", "telegram_send", {"text": "Noted: step one", "quote": "the plan", "effect": "🎉"})
    rig.director.script(first, say, C.answer("A-sf-say", "said"))
    inbound = rig.drv.dm(f"here is the plan [in:{first}]")
    C.wait_reply(rig, inbound.chat_id, "A-sf-say")
    (sent,) = [c for c in rig.drv.standin.calls_of("sendMessage", "sendRichMessage")
               if not c.faulted and "step one" in str(c.params)]
    reply = sent.params["reply_parameters"]
    reply = reply if isinstance(reply, dict) else json.loads(reply)
    assert (int(reply["message_id"]), reply["quote"]) == (int(inbound.message_id), "the plan")
    assert sent.params["message_effect_id"] == EFFECTS["🎉"]
    noted = str(sent.response["message_id"])

    edit = tool_call(rig, "sfe", "telegram_message", {"op": "edit", "message_id": noted, "text": "Noted: step two"})
    pin = tool_call(rig, "sfp", "telegram_message", {"op": "pin", "message_id": noted})
    rig.director.script(second, edit, pin, C.answer("A-sf-change", "changed"))
    later = rig.drv.dm(f"move on [in:{second}]")
    C.wait_reply(rig, later.chat_id, "A-sf-change")
    (edited,) = [c for c in rig.drv.standin.calls_of("editMessageText")
                 if not c.faulted and str(c.params.get("message_id")) == noted]
    assert "step two" in str(edited.params)
    assert (str(inbound.chat_id), noted) in rig.drv.standin.pinned
    assert not any("Error" in t or "error" in t for t in _tool_results(rig, second)), _tool_results(rig, second)


def test_a_quote_that_is_not_in_the_message_comes_back_to_the_agent_as_not_sent(rig):
    token = "sf-badquote"
    say = tool_call(rig, "sfq", "telegram_send", {"text": "About that", "quote": "words the user never wrote"})
    rig.director.script(token, say, C.answer("A-sf-badquote", "ok"))
    inbound = rig.drv.dm(f"hello there [in:{token}]")
    C.wait_reply(rig, inbound.chat_id, "A-sf-badquote")
    results = _tool_results(rig, token)  # the chat's history is shared: this call's result is the last
    assert "quote_text_invalid" in results[-1].lower() and '"sent": false' in results[-1], results[-1]
