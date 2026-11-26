"""The preview at the real Telegram boundary (hermes_telegram/preview.py): when frames go, what they show,
and what a Stop press, a message, a flood wait or a refusing topic does to them.

The composed adapter with a real python-telegram-bot ``Bot`` against the stand-in (checks/e2e/wire.py); its
processing hooks are called as the gateway calls them, for a user its allowlist admits. With no gateway
here, the ``/stop`` a Stop press dispatches goes nowhere: these tests check the adapter's side of it.
Regressions from the Codex review of slice 3 (docs/review-log.md).
"""

import asyncio
import re
from types import SimpleNamespace

import pytest

from checks.e2e.wire import CHAT, sender_fixture  # noqa: F401  (the sender fixture)
from gateway.platforms.base import MessageEvent, ProcessingOutcome
from hermes_telegram import preview

pytestmark = pytest.mark.e2e

FRAMES = ("sendRichMessageDraft", "sendMessageDraft")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(preview, "RESHOW_DELAY", 0.1)


def _event(adapter, *, message_id: str = "9", thread_id: str | None = None, user_id: str = CHAT) -> MessageEvent:
    source = adapter.build_source(chat_id=CHAT, chat_type="dm", user_id=user_id, message_id=message_id,
                                  thread_id=thread_id)
    return MessageEvent(text="work on it", source=source, message_id=message_id)


def _frames(sender, thread: str | None = None) -> list:
    calls = [c for c in sender.standin.calls_of(*FRAMES) if not c.faulted]
    return calls if thread is None else [c for c in calls if str(c.params.get("message_thread_id")) == thread]


def _stop(sender, draft_id: int, thread_id: int | None = None):
    pressed = {"chat": {"id": int(CHAT)}, "draft_id": draft_id, **({"message_thread_id": thread_id} if thread_id else {})}
    return sender.adapter._on_generation_stopped(SimpleNamespace(api_kwargs={"stopped_message_generation": pressed}),
                                                 None)


def _shown(sender) -> list[str]:
    """The text messages the chat shows, as the user reads them (MarkdownV2 escapes undone)."""
    return [re.sub(r"\\(.)", r"\1", v.text) for v in sender.shown(CHAT) if v.extra.get("kind") in ("text", "rich")]


# --- refresh ---------------------------------------------------------------------------------------------------


def test_the_production_refresh_comes_before_telegram_lets_a_draft_expire():
    # sendRichMessageDraft: "a temporary 30-second preview"; a refresh may wait up to one tick late.
    assert preview.REFRESH_SECS + preview._KEEPALIVE_TICK < preview.DRAFT_LIFETIME == 30.0


async def test_the_preview_is_sent_again_while_the_turn_runs_and_not_after(sender, monkeypatch):
    monkeypatch.setattr(preview, "REFRESH_SECS", 0.3)
    monkeypatch.setattr(preview, "_KEEPALIVE_TICK", 0.05)
    event = _event(sender.adapter)
    await sender.adapter.on_processing_start(event)
    await asyncio.sleep(1.0)
    during = len(_frames(sender))
    assert during >= 3, "the preview was not refreshed while the turn ran"
    await sender.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await asyncio.sleep(0.6)
    assert len(_frames(sender)) == during, "refreshed after the turn ended"


async def test_a_user_the_adapter_does_not_admit_gets_no_preview(sender):
    await sender.adapter.on_processing_start(_event(sender.adapter, user_id="666"))
    assert _frames(sender) == []


# --- messages during the turn ----------------------------------------------------------------------------------


async def test_a_cron_report_during_the_turn_arrives_and_the_preview_comes_back_under_it(sender):
    # Row 17: cron delivers through the adapter's send (gateway/delivery.py:59); it removes the draft.
    await sender.adapter.on_processing_start(_event(sender.adapter))
    before = len(_frames(sender))
    result = await sender.adapter.send(CHAT, "Daily report: all green", metadata={"job_id": "j1"})
    await asyncio.sleep(0.4)
    assert result.success and "Daily report: all green" in _shown(sender)
    assert len(_frames(sender)) == before + 1


async def test_after_stop_a_cron_report_arrives_and_no_preview_comes_back(sender):
    await sender.adapter.on_processing_start(_event(sender.adapter))
    draft = next(iter(sender.adapter._preview(CHAT).draft_ids))
    await _stop(sender, draft)
    before = len(_frames(sender))
    result = await sender.adapter.send(CHAT, "Daily report: all green", metadata={"job_id": "j1"})
    await asyncio.sleep(0.4)
    assert result.success and "Daily report: all green" in _shown(sender)
    assert len(_frames(sender)) == before


async def test_once_typing_pauses_for_the_final_nothing_brings_the_preview_back(sender):
    # Row 8: Hermes pauses typing before it delivers a turn's final.
    await sender.adapter.on_processing_start(_event(sender.adapter))
    before = len(_frames(sender))
    sender.adapter.pause_typing_for_chat(CHAT)
    await sender.adapter.send(CHAT, "The final answer")
    await sender.adapter.send(CHAT, "An attachment's caption")  # what follows the final text
    await asyncio.sleep(0.4)
    assert len(_frames(sender)) == before


async def test_one_topic_finishing_does_not_stop_another_topics_preview(sender):
    adapter = sender.adapter
    await adapter.on_processing_start(_event(adapter, message_id="9", thread_id="5"))
    await adapter.on_processing_start(_event(adapter, message_id="10", thread_id="6"))
    adapter.pause_typing_for_chat(CHAT)  # one flag for the chat: topic 5 is finalizing
    await adapter.send(CHAT, "Topic 5's final answer", metadata={"thread_id": "5"})
    await adapter.send_draft(CHAT, 77, "topic 6 still streaming", metadata={"thread_id": "6"})
    await adapter.send(CHAT, "A cron report in topic 6", metadata={"thread_id": "6"})  # not topic 6's final
    await asyncio.sleep(0.4)
    shown = [str(c.params) for c in _frames(sender, "6")]
    assert sum("topic 6 still streaming" in f for f in shown) >= 2, "topic 6's preview did not come back"


async def test_a_frame_in_flight_lands_before_the_final_and_none_after(sender):
    adapter = sender.adapter
    await adapter.on_processing_start(_event(adapter))
    running = adapter._preview(CHAT)
    gate = asyncio.Event()
    sender.wire.before = lambda method: None
    await running.lock.acquire()  # a frame is on its way

    async def release_later():
        await asyncio.sleep(0.2)
        running.lock.release()
        gate.set()
    asyncio.get_running_loop().create_task(release_later())
    adapter.pause_typing_for_chat(CHAT)
    await adapter.send(CHAT, "The final answer")
    assert gate.is_set(), "the final was sent while a frame was still on its way"
    final = next(c for c in sender.standin.calls if c.method in ("sendMessage", "sendRichMessage"))
    assert not [c for c in _frames(sender) if c.at > final.at]


async def test_a_segment_already_in_the_chat_is_neither_shown_again_nor_sent_again_after_stop(sender):
    adapter = sender.adapter
    await adapter.on_processing_start(_event(adapter))
    await adapter.send_draft(CHAT, 41, "Let me look that up for you.")
    await adapter.send(CHAT, "Let me look that up for you.")  # the stream seals the segment before a tool
    await asyncio.sleep(0.4)
    assert "Let me look that up" not in str(_frames(sender)[-1].params)
    await _stop(sender, 41)
    assert _shown(sender).count("Let me look that up for you.") == 1


# --- Stop --------------------------------------------------------------------------------------------------------


async def test_stop_on_another_turns_draft_is_ignored(sender):
    await sender.adapter.on_processing_start(_event(sender.adapter))
    await _stop(sender, 123456)  # a stale press: a draft of an earlier turn
    assert sender.adapter._preview(CHAT).stopped is False


async def test_stop_from_a_user_no_longer_admitted_is_ignored(sender, monkeypatch):
    await sender.adapter.on_processing_start(_event(sender.adapter))
    draft = next(iter(sender.adapter._preview(CHAT).draft_ids))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999")  # access revoked during the turn
    await _stop(sender, draft)
    assert sender.adapter._preview(CHAT).stopped is False


async def test_after_stop_the_preview_no_longer_changes(sender):
    # Row 9: frames the stopped turn still streams before its cancellation lands are not shown.
    await sender.adapter.on_processing_start(_event(sender.adapter))
    await sender.adapter.send_draft(CHAT, 7, "part of the answer")
    before = len(_frames(sender))
    await _stop(sender, 7)
    result = await sender.adapter.send_draft(CHAT, 7, "more of the answer")
    assert result.success  # the stream carries on without falling back to edited messages
    assert len(_frames(sender)) == before


# --- Telegram's limits --------------------------------------------------------------------------------------------


async def test_a_topic_that_refuses_drafts_lets_the_stream_fall_back_to_edited_messages(sender):
    await sender.adapter.on_processing_start(_event(sender.adapter))
    sender.refuse("sendRichMessageDraft", "Bad Request: method is not available for this chat")
    sender.refuse("sendMessageDraft", "Bad Request: method is not available for this chat")
    result = await sender.adapter.send_draft(CHAT, 8, "the answer so far")
    assert not result.success  # Hermes' stream consumer then edits a message instead


async def test_a_flood_wait_on_a_frame_holds_every_frame_until_it_passes(sender):
    sender.refuse("sendRichMessageDraft", "Too Many Requests: retry after 30", status=429, times=1, retry_after=30)
    await sender.adapter.on_processing_start(_event(sender.adapter))
    await sender.adapter.send_draft(CHAT, 9, "the answer so far")
    await sender.adapter.send(CHAT, "A cron report")
    await asyncio.sleep(0.4)
    assert _frames(sender) == []  # no plain retry, no re-show, inside the penalty
    assert len(sender.standin.calls_of("sendRichMessageDraft")) == 1


async def test_maths_inside_details_goes_as_a_plain_frame(sender):
    # The Telegram Desktop guard the final answer keeps (tdesktop#30808).
    await sender.adapter.on_processing_start(_event(sender.adapter))
    rich_before = len(sender.standin.calls_of("sendRichMessageDraft"))
    await sender.adapter.send_draft(CHAT, 10, "<details><summary>proof</summary>$$x^2$$</details>")
    assert len(sender.standin.calls_of("sendRichMessageDraft")) == rich_before
    assert "proof" in str(sender.standin.calls_of("sendMessageDraft")[-1].params)


async def test_a_chat_streamed_by_editing_messages_gets_no_preview_on_top(sender):
    # streaming.transport: edit — the stream's first message is one it will edit.
    await sender.adapter.on_processing_start(_event(sender.adapter))
    await sender.adapter.send(CHAT, "First words", metadata={"expect_edits": True})
    before = len(_frames(sender))
    await asyncio.sleep(0.4)
    assert len(_frames(sender)) == before and sender.adapter._preview(CHAT).live is False


def test_a_status_is_shown_as_text_never_as_markup():
    # The agent writes the status; a "<" in it must not open a tag in the thinking block.
    assert preview.frame_markdown("Comparing <a> & <b>...", "") == (
        "<tg-thinking>Comparing &lt;a&gt; &amp; &lt;b&gt;...</tg-thinking>")
