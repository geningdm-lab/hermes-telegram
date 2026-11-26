"""A widget's requests coming back, and the agent changing its state (hermes_telegram/widgets.py, tools.py), on
the composed adapter with the recording Bot API client of checks/fake_bot.py.
"""

import itertools
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from checks.fake_bot import KID, OWNER, STRANGER, build_adapter
from hermes_telegram import tools
from hermes_telegram.adapter import HermesTelegramAdapter
from hermes_telegram.turn import AdmittedTurn, Turn, TurnRefused

HTML = '<svg viewBox="0 0 10 10"><rect width="5" height="5"/></svg>'
_ids = itertools.count(1)


@pytest.fixture
def adapter(tmp_path) -> HermesTelegramAdapter:
    return build_adapter(tmp_path)


async def _widget(adapter, thread_id: Any = None, profile: str = "") -> str:
    sent = await adapter.send_widget(OWNER, HTML, {"thread_id": thread_id} if thread_id else {}, profile=profile)
    assert sent.success
    return sent.message_id


async def _query(adapter, message_id: Any, data: str, *, user_id: str = OWNER, chat_id: str = OWNER,
                 chat_type: str = "private", query_id: str | None = None) -> str:
    query_id = query_id or f"wq-{next(_ids)}"
    update = SimpleNamespace(update_id=next(_ids), api_kwargs={"widget_query": {
        "id": query_id, "from": {"id": int(user_id), "is_bot": False, "first_name": "User"},
        "chat": {"id": int(chat_id), "type": chat_type}, "message_id": int(message_id), "chat_instance": "ci",
        "type": {"type": "data"}, "data": data}})
    await adapter._on_widget_query(update, None)
    return query_id


def _answered(adapter) -> list[str]:
    return [k["widget_query_id"] for k in adapter.bot_stub.of("answerWidgetQuery")]


def _turn(thread_id: Any = None, profile: str = "") -> tuple[Turn, AdmittedTurn]:
    turn = Turn(session_key="s", chat_id=OWNER, thread_id=thread_id, inbound_message_id="40", profile=profile)
    return turn, AdmittedTurn(chat_id=OWNER, thread_id=thread_id, message_id="40", metadata={})


# --- requests ---------------------------------------------------------------------------------------------


async def test_a_request_by_a_user_the_adapter_does_not_admit_is_answered_and_reaches_nobody(adapter):
    # Review slice 5, P1: the widget gets its answer within 15 s; the agent gets only what text intake admits.
    widget = await _widget(adapter)
    refused = await _query(adapter, widget, "next", user_id=STRANGER)
    assert adapter.received == [] and _answered(adapter) == [refused]
    await _query(adapter, widget, "next")
    assert [e.text for e in adapter.received] == [f'[widget] data="next" on widget {widget}']


async def test_a_request_reaches_the_topic_the_widget_is_in_after_a_restart(tmp_path):
    # Review slice 5, P1 and P2: WidgetQuery has the chat and the message, not the topic.
    widget = await _widget(build_adapter(tmp_path), thread_id="7")
    after = build_adapter(tmp_path)  # a new gateway process
    await _query(after, widget, "next")
    (event,) = after.received
    assert (event.source.thread_id, event.reply_anchor_override) == ("7", widget)


@pytest.mark.parametrize("target", ["a message that is not a widget", "a widget of another chat", "a group"])
async def test_a_request_for_anything_but_this_bots_widget_reaches_nobody(adapter, target):
    widget = await _widget(adapter)
    text = await adapter.send_single(OWNER, "plain text", None, {})
    if target == "a message that is not a widget":
        await _query(adapter, text.message_id, "next")
    elif target == "a widget of another chat":
        await _query(adapter, widget, "next", user_id=KID, chat_id=KID)
    else:
        await _query(adapter, widget, "next", chat_type="supergroup")
    assert adapter.received == [] and len(_answered(adapter)) == 1


async def test_a_widget_sent_by_one_profile_takes_no_request_routed_to_another(tmp_path):
    adapter = build_adapter(tmp_path)
    adapter.gateway_runner = SimpleNamespace(
        _profile_name_for_source=lambda source, adapter_profile=None: "family")
    widget = await _widget(adapter, profile="")
    await _query(adapter, widget, "next")
    assert adapter.received == []


async def test_two_presses_with_the_same_data_are_two_actions_and_a_repeated_query_is_one(adapter):
    # Review slice 5, P1: pressing "next" twice is two moves; the same query delivered twice is one.
    widget = await _widget(adapter)
    first = await _query(adapter, widget, "next")
    await _query(adapter, widget, "next")
    await _query(adapter, widget, "next", query_id=first)
    assert [e.text for e in adapter.received] == [f'[widget] data="next" on widget {widget}'] * 2
    assert _answered(adapter).count(first) == 1


@pytest.mark.parametrize(("errors", "answers"), [
    ([RetryAfter(1)], 2),  # a flood wait inside the 15 s: answered again after it
    ([RetryAfter(30)], 1),  # a flood wait past the deadline: no point
    ([TimedOut("read timed out")], 1),  # Telegram may have taken it: not answered twice
])
async def test_an_answer_telegram_certainly_did_not_take_is_sent_again_within_the_deadline(adapter, errors, answers):
    # Review slice 5, P2.
    widget = await _widget(adapter)
    adapter.bot_stub.fail["answerWidgetQuery"] = list(errors)
    await _query(adapter, widget, "next")
    assert len(_answered(adapter)) == answers and len(adapter.received) == 1


# --- sending and state -----------------------------------------------------------------------------------


async def test_the_agent_changes_only_its_widgets_in_its_own_conversation(adapter):
    # Review slice 5, P1: editMessageWidgetData takes a chat and a message, not a topic.
    in_seven = await _widget(adapter, thread_id="7")
    in_eight = await _widget(adapter, thread_id="8")
    text = await adapter.send_single(OWNER, "plain", None, {"thread_id": "7"})
    turn, admitted = _turn("7")
    adapter.admitted_turn = lambda t: admitted
    for target in (in_eight, text.message_id):
        with pytest.raises(TurnRefused, match="not one of your widgets in this conversation"):
            await tools._change_message(adapter, turn, {"op": "widget_state", "message_id": target, "state": "x"})
    await tools._change_message(adapter, turn, {"op": "widget_state", "message_id": in_seven, "state": "x"})
    assert [k["message_id"] for k in adapter.bot_stub.of("editMessageWidgetData")] == [int(in_seven)]


async def test_a_state_change_needs_a_state_and_an_empty_one_clears_it(adapter):
    # Review slice 5, P2: Telegram reads an omitted chat_data and "" differently.
    widget = await _widget(adapter)
    turn, admitted = _turn()
    adapter.admitted_turn = lambda t: admitted
    with pytest.raises(TurnRefused, match="state is required"):
        await tools._change_message(adapter, turn, {"op": "widget_state", "message_id": widget})
    await tools._change_message(adapter, turn, {"op": "widget_state", "message_id": widget, "state": ""})
    assert [k["chat_data"] for k in adapter.bot_stub.of("editMessageWidgetData")] == [""]


async def test_a_state_the_widget_already_has_is_a_change_made(adapter):
    # Review slice 5, P2: Telegram answers "message is not modified"; bots/widgets says take it as done.
    widget = await _widget(adapter)
    adapter.bot_stub.fail["editMessageWidgetData"] = [BadRequest("Message is not modified"),
                                                      BadRequest("Message to edit not found")]
    assert (await adapter.set_widget_state(OWNER, widget, "x")).success
    assert not (await adapter.set_widget_state(OWNER, widget, "x")).success


@pytest.mark.parametrize(("args", "why"), [
    ({"state": "a\x00b"}, "NUL"),  # Review slice 5, P3: one validator for the send and the edit
    ({"state": "\ud800"}, "UTF-8"),
    ({"height": 20}, "50 to 600"),
])
async def test_a_widget_telegram_would_refuse_is_refused_unsent(adapter, args, why):
    turn, admitted = _turn()
    adapter.admitted_turn = lambda t: admitted
    with pytest.raises(TurnRefused, match=why):
        await tools._send_widget(adapter, turn, {"html": HTML, **args})
    assert adapter.bot_stub.calls == [] and admitted.sent == 0


async def test_height_zero_is_the_contents_own_height(adapter):
    # Review slice 5, P3: Telegram's automatic height.
    turn, admitted = _turn()
    adapter.admitted_turn = lambda t: admitted
    await tools._send_widget(adapter, turn, {"html": HTML, "height": 0})
    await tools._send_widget(adapter, turn, {"html": HTML, "height": 300})
    assert [k.get("height") for k in adapter.bot_stub.of("sendWidget")] == [None, 300]


@pytest.mark.parametrize(("error", "kept"), [(TimedOut("read timed out"), 1), (BadRequest("WIDGET_TOO_LARGE"), 0)])
async def test_an_uncertain_widget_keeps_its_slot_and_says_not_to_send_it_again(adapter, error, kept):
    # Review slice 5, P2: a retry after a lost response duplicates the widget and passes the cap.
    turn, admitted = _turn()
    adapter.admitted_turn = lambda t: admitted
    adapter.bot_stub.fail["sendWidget"] = [error]
    with pytest.raises(TurnRefused) as refused:
        await tools._send_widget(adapter, turn, {"html": HTML})
    assert admitted.sent == kept and ("do not send it again" in str(refused.value)) is bool(kept)


# --- Telegram refusing the bot widgets ---------------------------------------------------------------------


@pytest.mark.parametrize(("error", "withdrawn"), [
    (BadRequest("Input_media_widget_not_allowed"), True),  # PTB's wording of Telegram's refusal to the bot
    (BadRequest("Widget_too_large"), False),  # a refusal of this widget only
    (TimedOut("read timed out"), False),
])
async def test_once_telegram_refuses_the_bot_widgets_none_is_sent_or_offered_again(adapter, monkeypatch, error,
                                                                                  withdrawn):
    # Live run 2026-11-26, row 12: five sendWidget calls in one turn, each refused; since 2026-11-13 Telegram
    # refuses widgets to general bots (tdlib/telegram-bot-api#947).
    # Hermes' memo of tool lists (model_tools.py); importing the real module registers Hermes' platforms and
    # tools in this process, and checks/e2e/test_widgets_refused.py drops the real one.
    memo = {("memo",): [{"function": {"name": "telegram_widget"}}]}
    monkeypatch.setitem(sys.modules, "model_tools", SimpleNamespace(_clear_tool_defs_cache=memo.clear))
    monkeypatch.setattr(tools, "_available", lambda: True)
    monkeypatch.setattr(tools, "live_adapter", lambda: (None, adapter))
    turn, admitted = _turn()
    adapter.admitted_turn = lambda t: admitted
    adapter.bot_stub.fail["sendWidget"] = [error]
    with pytest.raises(TurnRefused) as first:
        await tools._send_widget(adapter, turn, {"html": HTML})
    assert ("does not let this bot send widgets" in str(first.value)) is withdrawn
    if not withdrawn:
        await tools._send_widget(adapter, turn, {"html": HTML})
    else:
        with pytest.raises(TurnRefused, match="do not call telegram_widget again"):
            await tools._send_widget(adapter, turn, {"html": HTML})
    assert len(adapter.bot_stub.of("sendWidget")) == (1 if withdrawn else 2)
    # A session built from now on: no widget tool, and Hermes' memo of tool lists no longer holds one with it.
    assert tools.widgets_available() is not withdrawn
    assert (("memo",) in memo) is not withdrawn
