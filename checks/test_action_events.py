"""Taps, votes and reactions coming back, and the tools' own sends (hermes_telegram/buttons.py, actions.py,
turn.py, tools.py), on the composed adapter with a recording stand-in for the Bot API client.

The fake bot and the adapter come from checks/fake_bot.py.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import BadRequest, TimedOut

from checks.fake_bot import KID, OWNER, STRANGER, build_adapter, user
from gateway.platforms.base import MessageEvent, SendResult, _thread_metadata_for_event
from hermes_telegram import tools
from hermes_telegram.adapter import HermesTelegramAdapter
from hermes_telegram.buttons import ALREADY_ANSWERED, EXPIRED, PREFIX
from hermes_telegram.delivery import DELIVERED_IDS
from hermes_telegram.turn import AdmittedTurn, Turn, TurnRefused, as_tool_send

@pytest.fixture
def adapter(tmp_path) -> HermesTelegramAdapter:
    return build_adapter(tmp_path)


def _message(chat_id: str, message_id: Any, thread_id: Any = None, chat_type: str = "private") -> Any:
    return SimpleNamespace(message_id=int(message_id), chat_id=int(chat_id),
                           chat=SimpleNamespace(id=int(chat_id), type=chat_type),
                           message_thread_id=int(thread_id) if thread_id else None,
                           is_topic_message=bool(thread_id))


class Query(SimpleNamespace):
    """A tap; ``answer`` is Telegram's acknowledgement, which can fail."""

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)
        if self.answer_fails:
            raise TimedOut("query is too old")


_queries = iter(range(1, 10_000))


async def _tap(adapter, user_id: str, message: Any, token: str, *, answer_fails: bool = False) -> Query:
    query = Query(id=str(next(_queries)), data=PREFIX + token, from_user=user(user_id), message=message,
                  answers=[], answer_fails=answer_fails)
    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
    return query


def _offer(adapter, chat_id: str, message_id: Any, labels: list[str], *, thread_id: Any = None,
           profile: str = "", single_use: bool = True) -> list[str]:
    """Value buttons the agent sent on message ``message_id``: their tokens, bound as the tool binds them."""
    markup, set_id = adapter.button_markup(chat_id, [{"label": x, "value": x.lower()} for x in labels],
                                           single_use=single_use, profile=profile)
    adapter._button_store().bind(set_id, message_id, thread_id)
    return [row[0]["callback_data"][len(PREFIX):] for row in markup["inline_keyboard"]]


def _texts(adapter) -> list[str]:
    return [e.text for e in adapter.received]


# --- taps ------------------------------------------------------------------------------------------------


async def test_a_tap_by_a_user_the_adapter_does_not_admit_reaches_nobody_and_takes_nothing(adapter):
    # Review slice 4, P1: the adapter's own allowlist decides before the set changes. A stranger in the
    # owner's chat (a forwarded keyboard, a crafted query) taps first; the owner's tap is still the first.
    paris, _ = _offer(adapter, OWNER, 5, ["Paris", "Rome"])
    refused = await _tap(adapter, STRANGER, _message(OWNER, 5), paris)
    assert adapter.received == [] and refused.answers == ["You cannot use these buttons."]
    await _tap(adapter, OWNER, _message(OWNER, 5), paris)
    assert _texts(adapter) == ['[button] "Paris" value="paris" on message 5']


async def test_a_tap_in_a_group_reaches_nobody(adapter):
    paris, = _offer(adapter, "-100", 5, ["Paris"])
    query = await _tap(adapter, OWNER, _message("-100", 5, chat_type="supergroup"), paris)
    assert adapter.received == [] and query.answers[0].startswith("These buttons work in the private chat")


@pytest.mark.parametrize("presented", ["another message", "another topic", "another chat"])
async def test_a_token_presented_with_another_message_topic_or_chat_is_not_that_sets_tap(adapter, presented):
    # Review slice 4, P1: Telegram does not promise a query's message holds a button with that data.
    paris, = _offer(adapter, OWNER, 5, ["Paris"], thread_id="7")
    message = {"another message": _message(OWNER, 6, thread_id="7"),
               "another topic": _message(OWNER, 5, thread_id="8"),
               "another chat": _message(KID, 5, thread_id="7")}[presented]
    query = await _tap(adapter, KID if presented == "another chat" else OWNER, message, paris)
    assert adapter.received == [] and query.answers == [EXPIRED]
    await _tap(adapter, OWNER, _message(OWNER, 5, thread_id="7"), paris)
    assert len(adapter.received) == 1  # the set itself was not consumed


async def test_a_tap_reaches_the_agent_even_when_telegram_refuses_its_acknowledgement(adapter):
    # Review slice 4, P1: a query too old to answer still carries the user's choice.
    paris, rome = _offer(adapter, OWNER, 5, ["Paris", "Rome"])
    await _tap(adapter, OWNER, _message(OWNER, 5), paris, answer_fails=True)
    assert _texts(adapter) == ['[button] "Paris" value="paris" on message 5']
    second = await _tap(adapter, OWNER, _message(OWNER, 5), rome)
    assert second.answers == [ALREADY_ANSWERED] and len(adapter.received) == 1


async def test_a_tap_whose_hand_off_fails_leaves_the_set_open(adapter):
    paris, rome = _offer(adapter, OWNER, 5, ["Paris", "Rome"])
    delivered = adapter.handle_message

    async def broken(event: MessageEvent) -> None:
        raise RuntimeError("the gateway is shutting down")
    adapter.handle_message = broken
    await _tap(adapter, OWNER, _message(OWNER, 5), paris)
    adapter.handle_message = delivered
    await _tap(adapter, OWNER, _message(OWNER, 5), rome)
    assert _texts(adapter) == ['[button] "Rome" value="rome" on message 5']


async def test_a_tap_is_routed_by_the_user_who_tapped_with_the_topics_skill_and_prompt(tmp_path):
    # docs/design.md, Events: a tap by the routed family user reaches the family profile, in its topic, with
    # what a text there gets (review slice 4, P2: the topic's skill and prompt).
    adapter = build_adapter(tmp_path, channel_prompts={"7": "Answer as the travel planner."})
    adapter.gateway_runner = SimpleNamespace(
        _profile_name_for_source=lambda source, adapter_profile=None: "family" if source.user_id == KID else None)
    adapter._dm_topics[f"{KID}:Trips"] = 7
    adapter._dm_topics_config = [{"chat_id": KID, "topics": [{"name": "Trips", "skill": "travel"}]}]
    yes, = _offer(adapter, KID, 5, ["Yes"], thread_id="7", profile="family")
    query = await _tap(adapter, KID, _message(KID, 5, thread_id="7"), yes)
    (event,) = adapter.received
    assert (event.source.profile, event.source.user_id, event.source.thread_id, event.text) == (
        "family", KID, "7", '[button] "Yes" value="yes" on message 5')
    assert (event.source.chat_topic, event.auto_skill, event.channel_prompt) == (
        "Trips", "travel", "Answer as the travel planner.")
    assert event.message_id == f"cb{query.id}" and event.reply_anchor_override == "5"


async def test_a_set_the_owner_sent_does_not_take_a_tap_routed_to_another_profile(tmp_path):
    # Review slice 4, P1: on a shared bot a set belongs to the profile that sent it.
    adapter = build_adapter(tmp_path)
    adapter.gateway_runner = SimpleNamespace(
        _profile_name_for_source=lambda source, adapter_profile=None: "family")
    paris, = _offer(adapter, OWNER, 5, ["Paris"], profile="")
    query = await _tap(adapter, OWNER, _message(OWNER, 5), paris)
    assert adapter.received == [] and query.answers == [EXPIRED]


# --- votes and reactions ---------------------------------------------------------------------------------


def _vote(update_id: int, poll_id: str, user_id: str, options: list[int]) -> Any:
    return SimpleNamespace(update_id=update_id, poll_answer=SimpleNamespace(
        poll_id=poll_id, user=user(user_id), option_ids=options))


async def test_a_vote_that_arrives_before_sendpoll_returns_reaches_the_agent(adapter):
    # Review slice 4, P2: Telegram shows the poll, the user votes, and only then does the send return.
    adapter.bot_stub.hold["sendPoll"] = asyncio.Event()
    sending = asyncio.create_task(adapter.send_poll(OWNER, "Capital?", ["Paris", "Rome"], {}))
    await asyncio.sleep(0.05)
    voting = asyncio.create_task(adapter._on_poll_answer(_vote(1, "poll-900", OWNER, [1]), None))
    await asyncio.sleep(0.2)
    adapter.bot_stub.hold["sendPoll"].set()
    await sending
    await voting
    assert _texts(adapter) == ['[poll] "Capital?": "Rome"']


async def test_a_vote_by_a_user_the_adapter_does_not_admit_reaches_nobody(adapter):
    await adapter.send_poll(OWNER, "Capital?", ["Paris", "Rome"], {})
    await adapter._on_poll_answer(_vote(1, "poll-900", STRANGER, [0]), None)
    await adapter._on_poll_answer(_vote(2, "poll-900", OWNER, [0]), None)
    assert _texts(adapter) == ['[poll] "Capital?": "Paris"']


def _reaction(update_id: int, chat_id: str, message_id: Any, user_id: str, emoji: str,
              chat_type: str = "private") -> Any:
    return SimpleNamespace(update_id=update_id, message_reaction=SimpleNamespace(
        chat=SimpleNamespace(id=int(chat_id), type=chat_type), message_id=int(message_id), user=user(user_id),
        old_reaction=(), new_reaction=(SimpleNamespace(emoji=emoji),)))


async def test_a_reaction_in_a_topic_reaches_that_topic_after_a_restart(tmp_path):
    # Review slice 4, P1: Telegram's reaction update has no topic; the index the bot kept at send time does.
    before = build_adapter(tmp_path)
    metadata = {"thread_id": "7"}
    sent = await before.send_single(OWNER, "Here is the plan", None, metadata)
    after = build_adapter(tmp_path)  # a new gateway process
    await after._on_message_reaction(_reaction(1, OWNER, sent.message_id, OWNER, "🔥"), None)
    (event,) = after.received
    assert (event.text, event.source.thread_id) == (f"[reaction] 🔥 on message {sent.message_id}", "7")


async def test_a_reaction_by_a_user_not_admitted_or_in_a_group_reaches_nobody(adapter):
    sent = await adapter.send_single(OWNER, "Here is the plan", None, {})
    await adapter._on_message_reaction(_reaction(1, OWNER, sent.message_id, STRANGER, "🔥"), None)
    await adapter._on_message_reaction(_reaction(2, OWNER, sent.message_id, OWNER, "🔥", "supergroup"), None)
    assert adapter.received == []
    await adapter._on_message_reaction(_reaction(3, OWNER, sent.message_id, OWNER, "🔥"), None)
    assert len(adapter.received) == 1  # the same message, from the owner in the private chat


@pytest.mark.parametrize("tail_certain", [True, False])
def test_every_part_of_a_partly_delivered_answer_is_a_bot_message(adapter, tail_certain):
    # Review slice 4, P2: a long answer delivered its first parts, then failed; the user reacts to part one.
    for build in (adapter._with_partial_send, adapter._partial):
        result = SendResult(success=False, error="flood")
        if build is adapter._with_partial_send:
            result = build(result, ["tail"], ["11", "12"], tail_certain=tail_certain)
        else:
            from collections import deque
            result = build(result, deque(["tail"]), ["11", "12"], tail_certain=tail_certain)
        assert result.raw_response[DELIVERED_IDS] == ("11", "12")
        adapter._remember_bot_messages(OWNER, result)
    assert adapter._is_bot_message(OWNER, "11") and adapter._is_bot_message(OWNER, "12")


async def test_a_control_prompt_is_a_bot_message(adapter):
    async def sent(**kwargs: Any) -> Any:
        return SimpleNamespace(message_id=77)
    adapter._send_message_with_thread_fallback = sent
    await adapter._send_control_message(OWNER, "Approve?", parse_mode=None, thread_id=None, metadata=None)
    assert adapter._is_bot_message(OWNER, 77)


async def test_hermes_processing_reactions_leave_the_agents_reaction(adapter):
    # Review slice 4, P2: with extra.reactions, Hermes sets its done or failed emoji when the turn ends.
    await adapter.react(OWNER, "40", "🎉")
    await adapter._set_reaction(OWNER, "40", "✅")
    await adapter._set_reaction(OWNER, "40", None)
    await adapter._set_reaction(OWNER, "41", "✅")  # another message still gets it
    assert [(k["message_id"], k["reaction"]) for k in adapter.bot_stub.of("setMessageReaction")] == [
        (40, "🎉"), (41, "✅")]


# --- the tools' sends ------------------------------------------------------------------------------------


def _topic_metadata(adapter) -> dict[str, Any]:
    """What a turn in DM topic 7, started by message 40, sends with (Hermes' own builder)."""
    source = adapter.build_source(chat_id=OWNER, chat_type="dm", user_id=OWNER, thread_id="7", message_id="40")
    return dict(_thread_metadata_for_event(MessageEvent(text="hi", source=source, message_id="40")) or {})


async def test_a_tools_poll_fails_rather_than_leave_the_turns_topic(adapter):
    # Review slice 4, P1: the user deleted the message the poll replies to. Ordinary delivery retries in the
    # root chat; a tool's send must not, or the poll shows outside the topic it was recorded for.
    metadata = _topic_metadata(adapter)
    gone = BadRequest("Message to be replied not found")
    adapter.bot_stub.fail["sendPoll"] = [gone]
    result = await as_tool_send(lambda: adapter.send_poll(OWNER, "Capital?", ["Paris", "Rome"], metadata))
    assert not result.success and len(adapter.bot_stub.of("sendPoll")) == 1
    adapter.bot_stub.fail["sendPoll"] = [gone]
    await adapter.send_poll(OWNER, "Capital?", ["Paris", "Rome"], metadata)  # not a tool's: retried
    retried = adapter.bot_stub.of("sendPoll")[-1]
    assert len(adapter.bot_stub.of("sendPoll")) == 3 and "message_thread_id" not in retried


async def test_a_tools_notice_fails_rather_than_leave_a_forum_topic(adapter):
    # The same for text: a forum topic that is gone sends to the root chat, except for a tool.
    adapter.bot_stub.fail["sendMessage"] = [BadRequest("Message thread not found")] * 4
    metadata = {"thread_id": "9"}
    await as_tool_send(lambda: adapter.send("-1001", "a.pdf was not delivered", metadata=metadata))
    assert [k.get("message_thread_id") for k in adapter.bot_stub.of("sendMessage")] == [9]


def _turn(thread_id: Any = None) -> tuple[Turn, AdmittedTurn]:
    turn = Turn(session_key="s", chat_id=OWNER, thread_id=thread_id, inbound_message_id="40", profile="")
    return turn, AdmittedTurn(chat_id=OWNER, thread_id=thread_id, message_id="40", metadata={})


async def test_the_agent_reacts_only_in_its_own_conversation(adapter):
    # Review slice 4, P1: setMessageReaction takes a chat and a message, so a message of another topic of
    # the same chat would get the reaction.
    other = await adapter.send_single(OWNER, "in topic 8", None, {"thread_id": "8"})
    mine = await adapter.send_single(OWNER, "in topic 7", None, {"thread_id": "7"})
    turn, admitted = _turn("7")
    adapter.admitted_turn = lambda t: admitted
    for target in (other.message_id, "999"):
        with pytest.raises(TurnRefused, match="one of your messages in this conversation"):
            await tools._react(adapter, turn, "👍", target)
    await tools._react(adapter, turn, "👍", mine.message_id)
    await tools._react(adapter, turn, "👀", None)  # the user's current message
    assert [k["message_id"] for k in adapter.bot_stub.of("setMessageReaction")] == [int(mine.message_id), 40]


async def test_a_tool_text_longer_than_one_message_is_refused_unsent(tmp_path):
    # Review slice 4, P1: the cap reserves one message for the text, so the text must be one message.
    adapter = build_adapter(tmp_path, rich_messages=False)
    result = await adapter.send_single(OWNER, "word " * 1000, None, {})
    assert not result.success and "too long for one message" in result.error
    assert adapter.bot_stub.calls == []


@pytest.mark.parametrize(("extra", "text", "expected"), [
    ({}, "Pick one", "sendRichMessage"),
    ({"rich_messages": False}, "Pick one", "sendMessage"),  # the profile turned rich messages off
    ({}, "<details>\n<summary>Why</summary>\n\n$$x^2$$\n</details>", "sendMessage"),  # Desktop's crash guard
])
async def test_a_tool_message_with_buttons_is_rich_exactly_when_an_answer_would_be(tmp_path, extra, text, expected):
    # Review slice 4, P1: the same checks as the answer path, the keyboard kept either way.
    adapter = build_adapter(tmp_path, **extra)
    markup, _ = adapter.button_markup(OWNER, [{"label": "A", "value": "a"}], single_use=True)
    assert (await adapter.send_single(OWNER, text, markup, {})).success
    ((method, kwargs),) = adapter.bot_stub.calls
    assert method == expected and kwargs["reply_markup"] == markup


@pytest.mark.parametrize(("metadata", "silent"), [({}, True), ({"notify": True}, False)])
async def test_a_tool_message_follows_the_notification_policy(adapter, metadata, silent):
    # Review slice 4, P2: in the default "important" mode an extra message does not ring.
    await adapter.send_single(OWNER, "Pick one", None, metadata)
    ((_, kwargs),) = adapter.bot_stub.calls
    assert kwargs.get("disable_notification", False) is silent


@pytest.mark.parametrize(("error", "kept"), [
    (TimedOut("read timed out"), 1),  # Telegram may have shown it: the slot stays taken
    (BadRequest("Bad Request: poll options are too long"), 0),  # refused: the slot is free again
])
async def test_an_uncertain_poll_keeps_its_slot_and_says_not_to_send_it_again(adapter, error, kept):
    # Review slice 4, P2: a retry after a read timeout duplicates the poll and passes the cap.
    turn, admitted = _turn()
    adapter.admitted_turn = lambda t: admitted
    adapter.bot_stub.fail["sendPoll"] = [error]
    with pytest.raises(TurnRefused) as refused:
        await tools._send_poll(adapter, turn, ("Capital?", ["Paris", "Rome"], None, False))
    assert admitted.sent == kept
    assert ("do not send it again" in str(refused.value)) is bool(kept)


# --- stickers --------------------------------------------------------------------------------------------


async def test_a_sticker_set_telegram_failed_to_return_is_asked_for_again_later(tmp_path, monkeypatch):
    # Review slice 4, P2: a flood wait on the first lookup must not hide the set until a restart.
    from hermes_telegram import actions

    adapter = build_adapter(tmp_path, sticker_sets=["cats"])
    adapter.bot_stub.sticker_sets = {"cats": ["cat-1"]}
    adapter.bot_stub.fail["getStickerSet"] = [TimedOut("flood")]
    assert await adapter.sticker_catalog() == []
    assert await adapter.sticker_catalog() == []  # not asked again at once
    monkeypatch.setattr(actions, "_SET_RETRY_SECS", 0.0)
    assert [s["sticker_id"] for s in await adapter.sticker_catalog()] == ["cat-1"]


async def test_a_sticker_the_owner_sent_is_not_in_another_profiles_catalog(adapter, monkeypatch):
    # Review slice 4, P1: the family agent must not see what the owner sent in their private chat.
    from hermes_telegram.harvested.adapter_inbound import InboundMixin

    async def described(self, msg: Any, event: Any) -> None:
        return None
    monkeypatch.setattr(InboundMixin, "_handle_sticker", described)
    sticker = SimpleNamespace(file_unique_id="u1", file_id="owner-cat", emoji="🐱", set_name="")
    await adapter._handle_sticker(SimpleNamespace(sticker=sticker), SimpleNamespace(source=SimpleNamespace(profile="")))
    assert [s["sticker_id"] for s in await adapter.sticker_catalog("")] == ["owner-cat"]
    assert await adapter.sticker_catalog("family") == []
    assert not (await adapter.send_sticker(OWNER, "owner-cat", {}, "family")).success
