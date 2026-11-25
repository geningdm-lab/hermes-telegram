"""Slice 4 end to end: the agent's action buttons and what comes back from them (row 11).

A real gateway child with this plugin, a fake model scripted per inbound, and the Bot API stand-in of
checks/e2e/standin.py, whose ``callback`` is a user's tap. A tap comes back to the agent as a new message
carrying the button's value; the scripts here put the next turn's ``[in:token]`` in the value so the fake
model can answer it. What a scenario asserts is what Telegram received and what reached the model.
"""

import json
import sys
import time
from typing import Any

import pytest

from checks.e2e.rig import C, PluginTelegramDriver, rig_fixtures, tool_call
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

STICKER_SET = "hermes_test_set"


class ActionsDriver(PluginTelegramDriver):
    """The profile names a sticker set (``extra.sticker_sets``), which the stand-in holds."""

    def __init__(self) -> None:
        super().__init__()
        self.standin.sticker_sets = {STICKER_SET: [{"file_id": "set-thumbs-up", "emoji": "👍"},
                                                   {"file_id": "set-party", "emoji": "🎉"}]}

    def gateway_config(self) -> dict[str, Any]:
        return C.merge(super().gateway_config(),
                       {"platforms": {"telegram": {"extra": {"sticker_sets": [STICKER_SET]}}}})


rig, rig_stream = rig_fixtures(ActionsDriver)


def _last_user_text(rig, token: str) -> str:
    request = rig.director.requests[token][0]
    users = [m for m in request.get("messages", []) if m.get("role") == "user"]
    return rig.director._text(users[-1]) if users else ""


def test_a_choice_reaches_the_agent_once_and_a_second_tap_says_already_answered(rig):
    # Row 11: three fixed choices; tap one, then another of the same set.
    offer, chosen = "ac-offer", "ac-chosen"
    buttons = [{"label": city, "value": f"{city.lower()} [in:{chosen}]"} for city in ("Paris", "Rome", "Oslo")]
    rig.director.script(offer, tool_call(rig, "acb", "telegram_send", {"text": "Pick a city", "buttons": buttons}),
                        C.answer("A-ac-offer", "offered"))
    rig.director.script(chosen, C.answer("A-ac-chosen", "You chose Paris"))
    inbound = rig.drv.dm(f"offer me cities [in:{offer}]")
    C.wait_reply(rig, inbound.chat_id, "A-ac-offer")
    paris = C._button(rig, inbound.chat_id, r"^Paris$")
    rome = C._button(rig, inbound.chat_id, r"^Rome$")
    rig.drv.click(inbound.chat_id, paris)
    C.wait_reply(rig, inbound.chat_id, "A-ac-chosen", "the agent's answer to the tap")
    second = rig.drv.click(inbound.chat_id, rome)
    wait_until(lambda: rig.drv.click_answered(second), "the second tap answered", timeout=C.TURN_TIMEOUT,
               on_timeout=rig.ctx)
    time.sleep(3.0)  # a second turn would have started by now
    assert C.turns(rig, chosen) == 1, "the agent received the choice more than once"
    assert _last_user_text(rig, chosen).endswith(
        f'[button] "Paris" value="paris [in:{chosen}]" on message {paris["message_id"]}')
    texts = {str(c.params.get("callback_query_id")): c.params.get("text") for c in rig.drv.callback_answers()}
    assert texts[second] == "Already answered"


def test_every_tap_of_a_set_that_is_not_single_use_reaches_the_agent(rig):
    # Not every choice is single-use: "Again" may be tapped twice, and each tap is its own message.
    offer, again = "ac-offer2", "ac-again"
    button = {"label": "Again", "value": f"again [in:{again}]"}
    rig.director.script(offer, tool_call(rig, "acb2", "telegram_send",
                                         {"text": "Roll the dice", "buttons": [button], "single_use": False}),
                        C.answer("A-ac-offer2", "offered"))
    rig.director.script(again, C.answer("A-ac-again-1", "rolled 4"), C.answer("A-ac-again-2", "rolled 2"))
    inbound = rig.drv.dm(f"dice please [in:{offer}]")
    C.wait_reply(rig, inbound.chat_id, "A-ac-offer2")
    roll = C._button(rig, inbound.chat_id, r"^Again$")
    rig.drv.click(inbound.chat_id, roll)
    C.wait_reply(rig, inbound.chat_id, "A-ac-again-1", "the first roll")
    rig.drv.click(inbound.chat_id, roll)
    C.wait_reply(rig, inbound.chat_id, "A-ac-again-2", "the second roll")
    assert C.turns(rig, again) == 2



def _requests_with_user_text(rig, text: str) -> list[dict]:
    return [r for reqs in rig.director.requests.values() for r in reqs
            if any(m.get("role") == "user" and rig.director._text(m) == text for m in r.get("messages", [])[-1:])]


def test_a_poll_answer_reaches_the_agent_as_the_option_chosen(rig):
    offer, answered = "ac-poll", "ac-voted"
    options = [f"Paris [in:{answered}]", f"Rome [in:{answered}]"]
    rig.director.script(offer, tool_call(rig, "acp", "telegram_poll", {"question": "Capital?", "options": options}),
                        C.answer("A-ac-poll", "asked"))
    rig.director.script(answered, C.answer("A-ac-voted", "Paris it is"))
    inbound = rig.drv.dm(f"ask me [in:{offer}]")
    C.wait_reply(rig, inbound.chat_id, "A-ac-poll")
    (sent,) = [c for c in rig.drv.standin.calls_of("sendPoll") if not c.faulted]
    assert sent.params.get("is_anonymous") is False  # else Telegram never says who answered
    rig.drv.standin.poll_answer(int(rig.drv.user_id), sent.response["poll"]["id"], [0])
    C.wait_reply(rig, inbound.chat_id, "A-ac-voted", "the agent's answer to the vote")
    assert _last_user_text(rig, answered) == f'[poll] "Capital?": "Paris [in:{answered}]"'


def test_a_quiz_carries_its_answer_as_telegram_now_names_it(rig):
    token = "ac-quiz"
    quiz = {"question": "2 + 2?", "options": ["3", "4"], "quiz_answer": 1}
    rig.director.script(token, tool_call(rig, "acq", "telegram_poll", quiz), C.answer("A-ac-quiz", "quizzed"))
    inbound = rig.drv.dm(f"quiz me [in:{token}]")
    C.wait_reply(rig, inbound.chat_id, "A-ac-quiz")
    (sent,) = [c for c in rig.drv.standin.calls_of("sendPoll") if c.params.get("type") == "quiz" and not c.faulted]
    assert sent.params["correct_option_ids"] == [1]


def test_a_reaction_to_the_agents_message_reaches_it_and_the_agent_can_react(rig):
    token = "ac-react"
    rig.director.script(token, tool_call(rig, "acr", "telegram_react", {"emoji": "👀"}), C.answer("A-ac-react", "noted"))
    inbound = rig.drv.dm(f"look at this [in:{token}]")
    C.wait_reply(rig, inbound.chat_id, "A-ac-react")
    (reacted,) = [c for c in rig.drv.standin.calls_of("setMessageReaction") if not c.faulted
                  and str(c.params.get("message_id")) == inbound.message_id]
    assert "👀" in json.dumps(reacted.params.get("reaction"), ensure_ascii=False)
    answer = next(v for v in rig.drv.visible(inbound.chat_id) if C.head("A-ac-react") in C.norm(v.text))
    rig.drv.standin.react(int(rig.drv.user_id), inbound.chat_id, int(answer.message_id), "🔥")
    expected = f"[reaction] 🔥 on message {answer.message_id}"
    wait_until(lambda: _requests_with_user_text(rig, expected), "the reaction reaching the agent",
               timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)


def test_a_reaction_to_the_users_own_message_does_not_reach_the_agent(rig):
    token = "ac-own"
    rig.director.script(token, C.answer("A-ac-own", "ok"))
    inbound = rig.drv.dm(f"hello [in:{token}]")
    C.wait_reply(rig, inbound.chat_id, "A-ac-own")
    rig.drv.standin.react(int(rig.drv.user_id), inbound.chat_id, int(inbound.message_id), "❤")
    time.sleep(3.0)
    assert not _requests_with_user_text(rig, f"[reaction] ❤ on message {inbound.message_id}")


def test_the_agent_finds_a_sticker_of_the_profiles_set_and_sends_it(rig):
    token = "ac-sticker"
    find = tool_call(rig, "acs1", "telegram_sticker", {"op": "find", "query": "👍"})
    send = tool_call(rig, "acs2", "telegram_sticker", {"op": "send", "sticker_id": "set-thumbs-up"})
    rig.director.script(token, find, send, C.answer("A-ac-sticker", "sent"))
    inbound = rig.drv.dm(f"a sticker please [in:{token}]")
    C.wait_reply(rig, inbound.chat_id, "A-ac-sticker")
    results = [rig.director._text(m) for r in rig.director.requests[token] for m in r.get("messages", [])
               if m.get("role") == "tool" and "candidates" in rig.director._text(m)]  # the chat's history is shared
    assert results and "set-thumbs-up" in results[-1]
    (sent,) = [c for c in rig.drv.standin.calls_of("sendSticker") if not c.faulted]
    assert sent.params["sticker"] == "set-thumbs-up"
