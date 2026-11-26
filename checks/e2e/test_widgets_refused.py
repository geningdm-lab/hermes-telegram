"""Telegram refusing the bot widgets, end to end (hermes_telegram/widgets.py, ``WidgetsMixin._refuse_widgets``).

Since 2026-11-13 Telegram refuses widgets to general bots with INPUT_MEDIA_WIDGET_NOT_ALLOWED, even in the
private chat (tdlib/telegram-bot-api#947). A module of its own: its gateway stays refused after the first
test, so it is not the one test_widgets.py sends widgets through.
"""

import sys

import pytest

from checks.e2e.rig import C, rig_fixtures, tool_call

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, rig_stream = rig_fixtures()

HTML = '<svg viewBox="0 0 10 10"><rect width="5" height="5"/></svg>'
REFUSAL = {"ok": False, "error_code": 400, "description": "Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED"}
NEW_USER = "113"  # allowlisted (stream_users): a private chat of its own, so a session of its own


def _tool_results(rig, token: str) -> list[str]:
    """The tool results of the turn, as the model saw them last: each request repeats the earlier ones."""
    (*_, last) = rig.director.requests[token]
    return [rig.director._text(m) for m in last.get("messages", []) if m.get("role") == "tool"]


def test_after_telegram_refuses_the_bot_widgets_it_asks_once_and_a_new_session_has_no_widget_tool(rig):
    # Live run 2026-11-26, row 12: five refused sendWidget calls in one turn.
    from tests.fakes.fake_llm_provider import ToolCall  # on the path once checks.e2e.rig is imported

    rig.drv.standin.fail("sendWidget", REFUSAL, status=400, times=5)
    token = "wr-refused"
    rig.director.script(token, tool_call(rig, "wr1", "telegram_widget", {"html": HTML}),
                        tool_call(rig, "wr2", "telegram_widget", {"html": HTML}), C.answer("A-wr-refused", "in text"))
    inbound = rig.drv.dm(f"draw the plan [in:{token}]")
    C.wait_reply(rig, inbound.chat_id, "A-wr-refused")
    assert len(rig.drv.standin.calls_of("sendWidget")) == 1  # the second call never reached Telegram
    results = _tool_results(rig, token)
    assert len(results) == 2 and all("does not let this bot send widgets" in r for r in results), results

    # A session that starts now is built without the tool: the bridge cannot run it, so the agent's call is
    # refused by Hermes, not by the plugin.
    fresh = "wr-fresh"
    rig.director.script(fresh, ToolCall("tool_call", {"calls": [{"name": "telegram_widget",
                                                                 "arguments": {"html": HTML}}]}),
                        C.answer("A-wr-fresh", "in text"))
    second = rig.drv.dm(f"draw it again [in:{fresh}]", user_id=NEW_USER)
    C.wait_reply(rig, second.chat_id, "A-wr-fresh")
    (result,) = _tool_results(rig, fresh)
    assert "'telegram_widget' is not available in this session" in result, result
    assert len(rig.drv.standin.calls_of("sendWidget")) == 1
