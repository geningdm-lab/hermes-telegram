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


def _turn(adapter, thread_id: Any = None, *, inbound: str = "40", text: str | None = "here is the plan",
          entities: list[dict[str, Any]] | None = None, profile: str = "") -> Turn:
    """A turn the user's message ``inbound`` (``text`` with ``entities``) started."""
    turn = Turn(session_key="s", chat_id=OWNER, thread_id=thread_id, inbound_message_id=inbound, profile=profile)
    admitted = AdmittedTurn(chat_id=OWNER, thread_id=thread_id, message_id=inbound,
                            metadata={"thread_id": thread_id} if thread_id else {}, text=text,
                            entities=entities or [])
    adapter.admitted_turn = lambda t: admitted
    adapter.admitted = admitted
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
    assert _sent(adapter)["reply_parameters"] == {"message_id": 40, "quote": "the plan", "quote_position": 8}


async def test_a_quote_carries_the_formatting_telegram_matches(adapter):
    # Review slice 6, P2: Telegram matches a quote with its bold, italic and other entities.
    text = "Meet at noon, not at 9"  # "noon" is bold, "Meet" italic (UTF-16 offsets)
    entities = [{"type": "italic", "offset": 0, "length": 4}, {"type": "bold", "offset": 8, "length": 4},
                {"type": "url", "offset": 8, "length": 4}]  # a URL entity is not part of a quote
    await _say(adapter, _turn(adapter, text=text, entities=entities), "Noon it is", quote="at noon")
    assert _sent(adapter)["reply_parameters"] == {
        "message_id": 40, "quote": "at noon", "quote_position": 5,
        "quote_entities": [{"type": "bold", "offset": 3, "length": 4}]}


@pytest.mark.parametrize(("case", "why"), [
    ("not in the message", "exact part of the user's message"),
    ("a turn a tap started", "no message text"),  # Review slice 6, P2: its id is an event id, not a message
    ("the bot's own message", "current message only"),  # its entities and rich blocks are not kept
])
async def test_a_quote_telegram_would_not_match_is_refused_before_anything_is_reserved(adapter, case, why):
    mine = await adapter.send_single(OWNER, "the plan in full", None, {})
    adapter.bot_stub.calls.clear()
    turn = _turn(adapter, inbound="cb12", text=None) if case == "a turn a tap started" else _turn(adapter)
    args = {"quote": "words never written"} if case == "not in the message" else {"quote": "the plan"}
    if case == "the bot's own message":
        args["reply_to"] = mine.message_id
    with pytest.raises(TurnRefused, match=why):
        await _say(adapter, turn, "About that", **args)
    assert adapter.bot_stub.calls == [] and adapter.admitted.sent == 0


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


# --- review of slice 6 -------------------------------------------------------------------------------------


async def test_a_message_another_bot_sent_in_the_same_chat_is_not_this_bots(tmp_path):
    # Review slice 6, P1: the profile's home kept, its bot replaced; the chat and the message ids repeat.
    before = build_adapter(tmp_path, token="1:x")
    sent = await before.send_single(OWNER, "from bot 1", None, {})
    after = build_adapter(tmp_path, token="2:y")
    with pytest.raises(TurnRefused, match="one of your messages"):
        await _change(after, _turn(after), op="delete", message_id=sent.message_id)
    assert after.bot_stub.calls == []


async def test_a_media_send_that_fell_back_to_the_root_chat_is_not_in_the_topic(adapter):
    # Review slice 6, P1: Hermes retries a DM-topic media send without its topic when the anchor is gone.
    from gateway.platforms.base import MessageEvent, _thread_metadata_for_event

    source = adapter.build_source(chat_id=OWNER, chat_type="dm", user_id=OWNER, thread_id="7", message_id="40")
    metadata = dict(_thread_metadata_for_event(MessageEvent(text="hi", source=source, message_id="40")) or {})
    adapter.bot_stub.fail["sendPoll"] = [BadRequest("Message to be replied not found")]
    sent = await adapter.send_poll(OWNER, "Capital?", ["Paris", "Rome"], metadata)  # an answer's, not a tool's
    assert adapter.bot_stub.of("sendPoll")[-1].get("message_thread_id") is None  # it landed in the root chat
    with pytest.raises(TurnRefused, match="one of your messages"):
        await _change(adapter, _turn(adapter, thread_id="7"), op="delete", message_id=sent.message_id)


async def test_a_message_sent_for_another_profile_is_not_this_turns(tmp_path):
    # Review slice 6, P1: the chat was routed to another profile since.
    from types import SimpleNamespace

    adapter = build_adapter(tmp_path)
    sent = await adapter.send_single(OWNER, "for the owner", None, {})
    adapter.gateway_runner = SimpleNamespace(
        _profile_name_for_source=lambda source, adapter_profile=None: "family")
    with pytest.raises(TurnRefused, match="one of your messages"):
        await _change(adapter, _turn(adapter, profile="family"), op="delete", message_id=sent.message_id)


async def test_a_message_recorded_before_kinds_existed_is_not_edited(adapter):
    # Review slice 6, P1: slice 5 kept prompts as {"thread": ...} too; editing one would drop its approval buttons.
    adapter._store("bot_messages", 4000).put(f"{OWNER}:77", {"thread": None, "bot": "1", "profile": ""})
    with pytest.raises(TurnRefused, match="Only a text message you sent"):
        await _change(adapter, _turn(adapter), op="edit", message_id="77", text="changed")
    assert adapter.bot_stub.calls == []


async def test_an_ordinary_message_is_edited_as_one_and_a_rich_one_as_rich(adapter):
    # Review slice 6, P2: Telegram refuses a rich edit of an ordinary message.
    from gateway import rich_sent_store

    turn = _turn(adapter)
    plain = await _say(adapter, turn, "Plain words")
    table = "| a | b |\n|---|---|\n| 1 | 2 |"
    out = await _change(adapter, turn, op="edit", message_id=plain["message_id"], text=table)
    rich_one = await _say(adapter, turn, table)
    await _change(adapter, turn, op="edit", message_id=rich_one["message_id"], text="| a | b |\n|---|---|\n| 3 | 4 |")
    first, second = adapter.bot_stub.of("editMessageText")
    assert "parse_mode" in first and "rich_message" not in first and "simplified" in out["note"]
    assert "rich_message" in second
    # Review slice 6, P2: a reply to the edited rich message gets its new text as context.
    assert "3 | 4" in rich_sent_store.lookup(OWNER, rich_one["message_id"])


async def test_an_edit_telegram_did_not_confirm_retires_the_old_choices(adapter):
    # Review slice 6, P2: the new keyboard may be showing; the old taps must not reach the agent.
    from telegram.error import TimedOut

    turn = _turn(adapter)
    sent = await _say(adapter, turn, "Pick", buttons=[{"label": "Paris", "value": "paris"}])
    (old,) = _tokens(_sent(adapter))
    adapter.bot_stub.fail["editMessageText"] = [TimedOut("read timed out")]
    with pytest.raises(TurnRefused, match="may not have been applied"):
        await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="Pick again",
                      buttons=[{"label": "Rome", "value": "rome"}])
    (new,) = _tokens(adapter.bot_stub.of("editMessageText")[0])
    shown = message(OWNER, sent["message_id"])
    assert (await tap(adapter, OWNER, shown, old)).answers == [EXPIRED]
    await tap(adapter, OWNER, shown, new)
    assert len(adapter.received) == 1
    await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="Pick, last call")
    assert _tokens(adapter.bot_stub.of("editMessageText")[-1]) == [new]  # the keyboard it may show, not the old


async def test_a_deleted_messages_taps_widget_requests_and_votes_reach_nobody(adapter):
    # Review slice 6, P2: an event already on its way when the message is deleted.
    from types import SimpleNamespace

    turn = _turn(adapter)
    choice = await _say(adapter, turn, "Pick", buttons=[{"label": "Paris", "value": "paris"}])
    (token,) = _tokens(_sent(adapter))
    widget = await adapter.send_widget(OWNER, "<p>w</p>", {})
    poll = await adapter.send_poll(OWNER, "Capital?", ["Paris", "Rome"], {})
    for message_id in (choice["message_id"], widget.message_id, poll.message_id):
        await _change(adapter, turn, op="delete", message_id=message_id)
    assert (await tap(adapter, OWNER, message(OWNER, choice["message_id"]), token)).answers == [EXPIRED]
    await adapter._on_widget_query(SimpleNamespace(update_id=1, api_kwargs={"widget_query": {
        "id": "wq-x", "from": {"id": int(OWNER), "is_bot": False, "first_name": "U"},
        "chat": {"id": int(OWNER), "type": "private"}, "message_id": int(widget.message_id), "data": "next"}}), None)
    await adapter._on_poll_answer(SimpleNamespace(update_id=2, poll_answer=SimpleNamespace(
        poll_id=f"poll-{poll.message_id}", user=SimpleNamespace(id=int(OWNER), is_bot=False, full_name="U"),
        option_ids=[0])), None)
    assert adapter.received == []


async def test_an_empty_button_list_removes_the_keyboard(adapter):
    # Review slice 6, P2: "Voting closed" with buttons=[] must not leave the choices active.
    turn = _turn(adapter)
    sent = await _say(adapter, turn, "Vote", buttons=[{"label": "Yes", "value": "yes"}])
    (token,) = _tokens(_sent(adapter))
    await _change(adapter, turn, op="edit", message_id=sent["message_id"], text="Voting closed", buttons=[])
    assert adapter.bot_stub.of("editMessageText")[0]["reply_markup"] == {"inline_keyboard": []}
    assert (await tap(adapter, OWNER, message(OWNER, sent["message_id"]), token)).answers == [EXPIRED]


async def test_an_effect_telegram_did_not_apply_is_reported_and_no_effect_is_refused(adapter):
    # Review slice 6, P2.
    with pytest.raises(TurnRefused, match="effect must be one of"):
        tools._parse_extras({"text": "Done", "effect": "0"})  # the id of no effect
    adapter.bot_stub.drop_effect = True
    out = await _say(adapter, _turn(adapter), "Done!", effect="🎉")
    assert out["sent"] and "without the effect" in out["note"]
    adapter.bot_stub.drop_effect = False
    assert "note" not in await _say(adapter, _turn(adapter), "Done again!", effect="🎉")
