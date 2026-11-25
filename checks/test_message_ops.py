"""The agent's own messages after they are sent, and what a tool message carries (slice 6: edits, deletes,
pins, quote replies, link previews, effects; hermes_telegram/messages.py, buttons.py, tools.py), on the composed
adapter with the recording Bot API client of checks/fake_bot.py.
"""

from typing import Any

import pytest
from telegram.error import BadRequest

from checks.fake_bot import OWNER, build_adapter, message, tap
from hermes_telegram import tools
from hermes_telegram.adapter import HermesTelegramAdapter
from hermes_telegram.buttons import EXPIRED, PREFIX
from hermes_telegram.messages import EFFECTS
from hermes_telegram.turn import AdmittedTurn, Turn, TurnRefused


@pytest.fixture
def adapter(tmp_path) -> HermesTelegramAdapter:
    return build_adapter(tmp_path)


def _turn(adapter, thread_id: Any = None) -> Turn:
    turn = Turn(session_key="s", chat_id=OWNER, thread_id=thread_id, inbound_message_id="40", profile="")
    admitted = AdmittedTurn(chat_id=OWNER, thread_id=thread_id, message_id="40",
                            metadata={"thread_id": thread_id} if thread_id else {})
    adapter.admitted_turn = lambda t: admitted
    return turn


async def _say(adapter, turn: Turn, text: str, **args: Any) -> dict[str, Any]:
    """telegram_send with this text and these arguments, on the adapter."""
    message_part = tools._parse_message({"text": text, **args})
    extras = tools._parse_extras({"text": text, **args})
    return (await tools._deliver(adapter, turn, [], False, "", message_part, extras))["message"]


async def _change(adapter, turn: Turn, **args: Any) -> dict[str, Any]:
    return await tools._change_message(adapter, turn, args)


def _tokens(kwargs: dict[str, Any]) -> list[str]:
    return [row[0]["callback_data"][len(PREFIX):] for row in kwargs["reply_markup"]["inline_keyboard"]]


# --- edit, delete, pin -----------------------------------------------------------------------------------


async def test_an_edit_replaces_the_text_and_keeps_its_buttons(adapter):
    # An edit without a keyboard would remove the one the message has.
    turn = _turn(adapter)
    sent = await _say(adapter, turn, "Pick a city", buttons=[{"label": "Paris", "value": "paris"}])
    (shown,) = adapter.bot_stub.of("sendMessage") or adapter.bot_stub.of("sendRichMessage")
    assert (await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="Pick a city, quickly"))
    (edited,) = adapter.bot_stub.of("editMessageText")
    assert edited["message_id"] == int(sent["message_id"]) and edited["reply_markup"] == shown["reply_markup"]
    assert "quickly" in str(edited.get("text") or edited.get("rich_message"))


async def test_an_edit_with_new_buttons_retires_the_old_ones(adapter):
    turn = _turn(adapter)
    sent = await _say(adapter, turn, "Pick a city", buttons=[{"label": "Paris", "value": "paris"}])
    (old,) = _tokens((adapter.bot_stub.of("sendMessage") or adapter.bot_stub.of("sendRichMessage"))[0])
    await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="Pick again",
                  buttons=[{"label": "Rome", "value": "rome"}])
    (new,) = _tokens(adapter.bot_stub.of("editMessageText")[0])
    shown = message(OWNER, sent["message_id"])
    assert (await tap(adapter, OWNER, shown, old)).answers == [EXPIRED]
    await tap(adapter, OWNER, shown, new)
    assert [e.text for e in adapter.received] == [f'[button] "Rome" value="rome" on message {sent["message_id"]}']


@pytest.mark.parametrize(("target", "op"), [
    ("another topic's message", "edit"),
    ("the user's message", "pin"),  # the design: the bot's own messages
    ("a message never sent", "delete"),
    ("a widget", "edit"),  # its text is not a message text: op=widget_state
])
async def test_only_the_bots_own_text_in_this_conversation_is_changed(adapter, target, op):
    turn = _turn(adapter, thread_id="7")
    other = await adapter.send_single(OWNER, "in topic 8", None, {"thread_id": "8"})
    widget = await adapter.send_widget(OWNER, "<p>w</p>", {"thread_id": "7"})
    message_id = {"another topic's message": other.message_id, "the user's message": "40",
                  "a message never sent": "999", "a widget": widget.message_id}[target]
    before = len(adapter.bot_stub.calls)
    with pytest.raises(TurnRefused):
        await _change(adapter, turn, op=op, message_id=message_id, text="changed")
    assert adapter.bot_stub.calls[before:] == []


async def test_a_text_the_message_already_has_is_an_edit_made(adapter):
    # Telegram answers "message is not modified"; the message shows what the agent asked for.
    turn = _turn(adapter)
    sent = await _say(adapter, turn, "Same")
    adapter.bot_stub.fail["editMessageText"] = [BadRequest("Message is not modified")]
    assert (await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="Same"))["edited"]
    adapter.bot_stub.fail["editMessageText"] = [BadRequest("Message to edit not found")]
    with pytest.raises(TurnRefused, match="not edited"):
        await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="Other")


async def test_an_edit_longer_than_one_message_is_refused_unsent(tmp_path):
    adapter = build_adapter(tmp_path, rich_messages=False)
    turn = _turn(adapter)
    sent = await _say(adapter, turn, "Short")
    with pytest.raises(TurnRefused, match="too long for one message"):
        await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="word " * 1000)
    assert adapter.bot_stub.of("editMessageText") == []


async def test_a_deleted_message_is_no_longer_the_bots_to_change(adapter):
    turn = _turn(adapter)
    sent = await _say(adapter, turn, "Temporary")
    await _change(adapter, turn, op="delete", message_id=sent["message_id"])
    assert adapter.bot_stub.of("deleteMessage") == [{"chat_id": int(OWNER), "message_id": int(sent["message_id"])}]
    with pytest.raises(TurnRefused):
        await _change(adapter, turn, op="pin", message_id=sent["message_id"])


async def test_a_pin_is_silent_unless_the_agent_asks_and_unpin_unpins(adapter):
    turn = _turn(adapter)
    sent = await _say(adapter, turn, "The plan")
    await _change(adapter, turn, op="pin", message_id=sent["message_id"])
    await _change(adapter, turn, op="pin", message_id=sent["message_id"], notify=True)
    await _change(adapter, turn, op="unpin", message_id=sent["message_id"])
    assert [k["disable_notification"] for k in adapter.bot_stub.of("pinChatMessage")] == [True, False]
    assert [k["message_id"] for k in adapter.bot_stub.of("unpinChatMessage")] == [int(sent["message_id"])]


# --- what a tool message carries ---------------------------------------------------------------------------


def _sent(adapter) -> dict[str, Any]:
    (kwargs,) = adapter.bot_stub.of("sendMessage") + adapter.bot_stub.of("sendRichMessage")
    return kwargs


async def test_a_quote_replies_to_the_users_current_message_by_default(adapter):
    await _say(adapter, _turn(adapter), "That is the plan", quote="the plan")
    assert _sent(adapter)["reply_parameters"] == {"message_id": 40, "quote": "the plan"}


async def test_a_reply_goes_to_one_of_the_bots_messages_in_this_conversation_only(adapter):
    turn = _turn(adapter, thread_id="7")
    mine = await adapter.send_single(OWNER, "in topic 7", None, {"thread_id": "7"})
    other = await adapter.send_single(OWNER, "in topic 8", None, {"thread_id": "8"})
    adapter.bot_stub.calls.clear()
    with pytest.raises(TurnRefused, match="in this conversation"):
        await _say(adapter, turn, "About that", reply_to=other.message_id)
    assert adapter.bot_stub.calls == []
    await _say(adapter, turn, "About that", reply_to=mine.message_id)
    assert _sent(adapter)["reply_parameters"] == {"message_id": int(mine.message_id)}


@pytest.mark.parametrize(("extra", "preview", "expected"), [
    ({}, {"small": True, "url": "https://example.com"}, {"prefer_small_media": True, "url": "https://example.com"}),
    ({"disable_link_previews": True}, None, {"is_disabled": True}),  # the profile's default
    ({}, None, None),
])
async def test_link_preview_options_go_with_an_ordinary_message(tmp_path, extra, preview, expected):
    adapter = build_adapter(tmp_path)
    adapter._disable_link_previews = bool(extra.get("disable_link_previews"))
    await _say(adapter, _turn(adapter), "See https://example.com",
               **({"link_preview": preview} if preview else {}))
    assert _sent(adapter).get("link_preview_options") == expected


async def test_a_rich_message_says_it_took_no_link_preview_options(adapter):
    out = await _say(adapter, _turn(adapter), "| a | b |\n|---|---|\n| 1 | 2 |", link_preview={"disabled": True})
    assert "link_preview_options" not in _sent(adapter) and "no link preview" in out["note"]


@pytest.mark.parametrize(("effect", "expected"), [("🎉", EFFECTS["🎉"]), ("❤", EFFECTS["❤️"]),
                                                  ("5104841245755180586", "5104841245755180586")])
async def test_an_effect_is_named_by_its_emoji_or_its_id(adapter, effect, expected):
    await _say(adapter, _turn(adapter), "Done!", effect=effect)
    assert _sent(adapter)["message_effect_id"] == expected


async def test_an_effect_telegram_has_no_name_for_or_outside_a_private_chat_is_refused(adapter):
    with pytest.raises(TurnRefused, match="effect must be one of"):
        tools._parse_extras({"text": "Done", "effect": "🦄"})
    result = await adapter.send_single("-1001", "Done", None, {}, effect=EFFECTS["🎉"])
    assert not result.success and "private chats" in result.error and adapter.bot_stub.calls == []


def test_reply_quote_preview_and_effect_need_a_text():
    with pytest.raises(TurnRefused, match="apply to a text"):
        tools._parse_extras({"files": ["/tmp/a.png"], "effect": "🎉"})
