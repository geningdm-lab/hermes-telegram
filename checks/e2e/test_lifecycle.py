"""Slice 3 end to end: the preview of a turn, from the first status to its final answer, Stop or failure
(rows 6 to 10 and 17).

A real gateway child with this plugin, a fake model scripted per inbound, and the Bot API stand-in of
checks/e2e/standin.py. ``rig`` runs turns without streaming (and with the terminal, for approvals);
``rig_stream`` streams answers through drafts. What a scenario asserts is what Telegram received, in order;
whether a client draws it is live acceptance.
"""

import re
import sys
import time
from typing import Any

import pytest

from checks.e2e.rig import C, rig_fixtures
from tests.fakes.fake_llm_provider import Error, ToolCall
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, rig_stream = rig_fixtures()

FRAMES = ("sendRichMessageDraft", "sendMessageDraft")
MESSAGES = ("sendMessage", "sendRichMessage", "sendPhoto", "sendDocument", "sendMediaGroup")
QUIET = 3.0  # long enough for a stray frame or message to show up


class _Chat:
    """The stand-in calls for one private chat from now on."""

    def __init__(self, rig: Any, chat_id: str) -> None:
        self.rig, self.chat_id = rig, str(chat_id)
        self.since = len(rig.drv.standin.calls)

    def calls(self, *methods: str) -> list[Any]:
        return [c for c in self.rig.drv.standin.calls[self.since:]
                if c.method in methods and str(c.params.get("chat_id")) == self.chat_id and not c.faulted]

    def frames(self, after: float = 0.0) -> list[Any]:
        return [c for c in self.calls(*FRAMES) if c.at > after]

    def messages(self) -> list[Any]:
        return self.calls(*MESSAGES)


def _replies(chat: "_Chat") -> list[Any]:
    """The chat's messages other than Hermes' diagnostic lines."""
    return [m for m in chat.messages() if not _text(m).startswith("❌")]


def _frame_text(call: Any) -> str:
    return str((call.params.get("rich_message") or {}).get("markdown") or call.params.get("text") or "")


def _status(call: Any) -> str:
    found = re.search(r"<tg-thinking>(.*?)</tg-thinking>", _frame_text(call), re.DOTALL)
    return found.group(1) if found else ""


def _text(call: Any) -> str:
    return C.norm(str(call.params.get("text") or (call.params.get("rich_message") or {}).get("markdown") or ""))


def _words(text: str, prefix: str) -> list[str]:
    return re.findall(rf"\b{prefix}\d{{3,5}}\b", text)


def _distinct(values: list[str]) -> list[str]:
    return [v for i, v in enumerate(values) if i == 0 or v != values[i - 1]]


def _tool(rig: Any, tag: str, name: str, args: dict[str, Any]) -> ToolCall:
    """A call of a telegram tool as a real model makes it: directly, or through Hermes' tool search bridge."""
    offered = C._offered_tools(rig, tag)
    return (ToolCall(name, args) if name in offered
            else ToolCall("tool_call", {"calls": [{"name": name, "arguments": args}]}))


def test_the_gateway_asks_telegram_for_stop_presses(rig):
    # Telegram delivers only the update kinds a bot asks for; PTB 22.8's list predates the Stop button.
    polls = [c for c in rig.drv.standin.calls_of("getUpdates") if c.params.get("allowed_updates")]
    assert polls, "the gateway never polled with an update list"
    assert "stopped_message_generation" in polls[-1].params["allowed_updates"]
    assert "message" in polls[-1].params["allowed_updates"]  # PTB's own kinds are still asked for


def test_the_preview_reads_contemplating_then_the_status_the_agent_set_last(rig):
    # Row 6.
    token, aid = "lc-status", "A-lc-status"
    first = _tool(rig, "lcs1", "telegram_status", {"text": "Reading the report..."})
    second = _tool(rig, "lcs2", "telegram_status", {"text": "Writing the summary..."})
    rig.director.script(token, first, second, C.answer(aid, "summary done"))
    user = rig.fresh_user()
    chat = _Chat(rig, user)
    inbound = rig.drv.dm(f"summarise it [in:{token}]", user_id=user)
    C.wait_reply(rig, inbound.chat_id, aid)
    frames = chat.frames()
    assert _distinct([_status(f) for f in frames]) == [
        "Contemplating...", "Reading the report...", "Writing the summary..."], [_frame_text(f) for f in frames]
    assert all(f.params.get("can_stop") is True for f in frames), "a frame without the Stop button"


def test_an_approval_during_the_preview_stays_visible_and_the_preview_comes_back_under_it(rig, tmp_path):
    # Row 17, approvals: the prompt removes the draft; the preview is shown again while the user decides.
    token, aid = "lc-approve", "A-lc-approve"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f.txt").write_text("x")
    rig.director.script(token, ToolCall("terminal", {"command": f"rm -rf {victim}"}), C.answer(aid, "cleaned up"))
    chat = _Chat(rig, rig.drv.user_id)
    inbound = rig.drv.dm(f"clean up [in:{token}]")
    approve = C._button(rig, inbound.chat_id, r"once")
    prompt = next(c for c in chat.calls("sendMessage") if c.params.get("reply_markup"))
    wait_until(lambda: chat.frames(after=prompt.at), "the preview shown again under the approval prompt",
               timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)
    rig.drv.click(inbound.chat_id, approve)
    C.wait_reply(rig, inbound.chat_id, aid, "the turn completed after the approval")
    assert not victim.exists(), "the approved command did not run"
    prompt_id = str(prompt.response["message_id"])
    assert prompt_id in {v.message_id for v in rig.drv.visible(inbound.chat_id)}, "the approval prompt is gone"
    final = next(c for c in chat.messages() if C.head(aid) in _text(c))
    time.sleep(QUIET)
    assert not chat.frames(after=final.at), "the preview came back after the final answer"


def test_a_failed_turn_leaves_one_failure_message_and_no_preview(rig):
    # Row 10.
    token = "lc-fail"
    rig.director.script(token, *[Error(400, "scripted failure") for _ in range(4)])
    user = rig.fresh_user()
    chat = _Chat(rig, user)
    rig.drv.dm(f"this will fail [in:{token}]", user_id=user)
    wait_until(lambda: _replies(chat), "the failure message", timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)
    time.sleep(QUIET)
    # Hermes also shows its own diagnostic line ("❌ ... rejected the request ...") unless the profile sets
    # display.suppress_warning_notifications (gateway/warning_notifications.py:90): not the turn's reply.
    (failure,) = _replies(chat)
    assert chat.frames(), "no preview was shown while the turn ran"
    assert not chat.frames(after=max(m.at for m in chat.messages())), "the preview came back after the failure"


def test_the_preview_grows_with_the_answer_and_the_final_answer_replaces_it(rig_stream):
    # Rows 7 and 8.
    token, aid = "lc-grow", "A-lc-grow"
    body = " ".join(f"g{i:03d}" for i in range(120))
    rig_stream.director.script(token, C.answer(aid, body, chunk_chars=12, delay_per_chunk=0.05))
    user = rig_stream.fresh_user()
    chat = _Chat(rig_stream, user)
    inbound = rig_stream.drv.dm(f"grow it [in:{token}]", user_id=user)
    C.wait_reply(rig_stream, inbound.chat_id, aid)
    time.sleep(QUIET)
    shown = [_words(_frame_text(f), "g") for f in chat.frames()]
    partial = [w for w in shown if w]
    assert len({len(w) for w in partial}) >= 2, "the preview did not grow"
    assert all(w == _words(body, "g")[:len(w)] for w in partial), "a frame is not the beginning of the answer"
    (final,) = chat.messages()
    assert _words(_text(final), "g") == _words(body, "g")
    assert not chat.frames(after=final.at), "the preview came back after the final answer"


def test_stop_on_the_preview_stops_the_turn_and_keeps_stopped_and_the_partial_answer(rig_stream):
    # Row 9.
    token, aid = "lc-stop", "A-lc-stop"
    body = " ".join(f"p{i:03d}" for i in range(400))
    rig_stream.director.script(token, C.answer(aid, body, chunk_chars=6, delay_per_chunk=0.1))  # ~35 s
    user = rig_stream.fresh_user()
    chat = _Chat(rig_stream, user)
    inbound = rig_stream.drv.dm(f"write at length [in:{token}]", user_id=user)
    frame = wait_until(lambda: next((f for f in reversed(chat.frames()) if len(_words(_frame_text(f), "p")) >= 10),
                                    None), "a preview holding part of the answer", timeout=C.TURN_TIMEOUT,
                       on_timeout=rig_stream.ctx)
    pressed = time.monotonic()
    rig_stream.drv.standin.stop_generation(inbound.chat_id, int(frame.params["draft_id"]))
    stopped = wait_until(lambda: next((m for m in chat.messages() if "Stopped" in _text(m)), None),
                         "the Stopped message", timeout=5, on_timeout=rig_stream.ctx)
    assert stopped.at - pressed < 5.0
    time.sleep(QUIET)
    after = [m for m in chat.messages() if m.at >= stopped.at]
    assert after[0] is stopped
    partial = [_words(_text(m), "p") for m in after[1:]]
    assert len(partial) == 1 and partial[0] and partial[0] == _words(body, "p")[:len(partial[0])], (
        [_text(m) for m in after])
    assert len(partial[0]) < len(_words(body, "p")), "the whole answer arrived after Stop"
    assert not any(C.foot(aid) in _text(m) for m in chat.messages()), "the answer's end arrived after Stop"
    assert not chat.frames(after=stopped.at), "the preview changed after Stop"


def test_a_long_streamed_answer_arrives_as_rich_messages_in_order(rig_stream):
    # Row 1 while streaming: heads cut past the overflow limit go as rich messages, cut where the rich
    # cutter cuts (moved from slice 1, docs/review-log.md).
    token, aid = "lc-long", "A-lc-long"
    body = "\n\n".join(f"## Part {i}\n\n" + " ".join(f"q{i:02d}{j:03d}" for j in range(300)) for i in range(18))
    rig_stream.director.script(token, C.answer(aid, body, chunk_chars=400))
    user = rig_stream.fresh_user()
    chat = _Chat(rig_stream, user)
    inbound = rig_stream.drv.dm(f"the long one [in:{token}]", user_id=user)
    C.wait_reply(rig_stream, inbound.chat_id, aid)
    time.sleep(QUIET)
    messages = chat.messages()
    assert len(messages) >= 2 and {m.method for m in messages} == {"sendRichMessage"}, [m.method for m in messages]
    assert [w for m in messages for w in _words(_text(m), "q")] == _words(body, "q")


def test_a_clarify_question_during_the_preview_stays_and_its_answer_reaches_the_agent(rig):
    # Clarify prompts go through the prompt sender this slice wraps (docs/design.md, Slices, 0).
    token, aid = "lc-clarify", "A-lc-clarify"
    ask = ToolCall("clarify", {"questions": [{"question": "Which format?", "choices": ["PDF", "Markdown"]}]})
    rig.director.script(token, ask, C.answer(aid, "Markdown it is"))
    chat = _Chat(rig, rig.drv.user_id)
    inbound = rig.drv.dm(f"export it [in:{token}]")
    second = C._button(rig, inbound.chat_id, r"^2$")
    prompt = next(c for c in chat.calls("sendMessage") if "Which format?" in _text(c))
    wait_until(lambda: chat.frames(after=prompt.at), "the preview shown again under the clarify question",
               timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)
    rig.drv.click(inbound.chat_id, second)
    C.wait_reply(rig, inbound.chat_id, aid, "the turn completed after the answer")
    asked_again = rig.director.requests[token][-1]
    assert "Markdown" in str(asked_again.get("messages", [])[-1]), "the chosen answer did not reach the agent"
    assert str(prompt.response["message_id"]) in {v.message_id for v in rig.drv.visible(inbound.chat_id)}
