"""The preview's timing at the real Telegram boundary (hermes_telegram/preview.py): the frame it sends
again before Telegram lets a draft expire, and how a frame shows a status.

The composed adapter with a real python-telegram-bot ``Bot`` against the stand-in (checks/e2e/wire.py); its
processing hooks are called as the gateway calls them. The refresh interval is shortened for the test.
"""

import asyncio

import pytest

from checks.e2e.wire import CHAT, sender_fixture  # noqa: F401  (the sender fixture)
from gateway.platforms.base import MessageEvent, ProcessingOutcome
from hermes_telegram import preview

pytestmark = pytest.mark.e2e


def _event(adapter) -> MessageEvent:
    source = adapter.build_source(chat_id=CHAT, chat_type="dm", user_id=CHAT, message_id="9")
    return MessageEvent(text="work on it", source=source, message_id="9")


async def test_the_preview_is_sent_again_before_the_draft_expires_while_the_turn_runs(sender, monkeypatch):
    monkeypatch.setattr(preview, "REFRESH_SECS", 0.3)
    monkeypatch.setattr(preview, "_KEEPALIVE_TICK", 0.05)
    event = _event(sender.adapter)
    await sender.adapter.on_processing_start(event)
    await asyncio.sleep(1.0)
    during = len(sender.standin.calls_of("sendRichMessageDraft"))
    assert during >= 3, "the preview was not refreshed while the turn ran"
    await sender.adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await asyncio.sleep(0.6)
    assert len(sender.standin.calls_of("sendRichMessageDraft")) == during, "refreshed after the turn ended"


def test_a_status_is_shown_as_text_never_as_markup():
    # The agent writes the status; a "<" in it must not open a tag in the thinking block.
    assert preview.frame_markdown("Comparing <a> & <b>...", "") == (
        "<tg-thinking>Comparing &lt;a&gt; &amp; &lt;b&gt;...</tg-thinking>")


async def test_a_message_during_the_turn_brings_the_preview_back(sender, monkeypatch):
    # Row 17: a cron report or a tool's message removes the draft; the preview is shown again after it.
    monkeypatch.setattr(preview, "RESHOW_DELAY", 0.1)
    await sender.adapter.on_processing_start(_event(sender.adapter))
    before = len(sender.standin.calls_of("sendRichMessageDraft"))
    await sender.adapter.send(CHAT, "A cron report")
    await asyncio.sleep(0.4)
    assert len(sender.standin.calls_of("sendRichMessageDraft")) == before + 1


async def test_once_typing_pauses_for_the_final_a_message_does_not_bring_the_preview_back(sender, monkeypatch):
    # Row 8: Hermes pauses typing before it delivers a turn's final; nothing may show the preview after it.
    monkeypatch.setattr(preview, "RESHOW_DELAY", 0.1)
    await sender.adapter.on_processing_start(_event(sender.adapter))
    before = len(sender.standin.calls_of("sendRichMessageDraft"))
    sender.adapter.pause_typing_for_chat(CHAT)
    await sender.adapter.send(CHAT, "The final answer")
    await sender.adapter.send(CHAT, "An attachment's caption")  # what follows the final text
    await asyncio.sleep(0.4)
    assert len(sender.standin.calls_of("sendRichMessageDraft")) == before


async def test_after_stop_the_preview_no_longer_changes(sender):
    # Row 9: frames the stopped turn still streams before its cancellation lands are not shown.
    from types import SimpleNamespace

    await sender.adapter.on_processing_start(_event(sender.adapter))
    before = len(sender.standin.calls_of("sendRichMessageDraft"))
    pressed = SimpleNamespace(api_kwargs={"stopped_message_generation": {"chat": {"id": int(CHAT)}, "draft_id": 7}})
    await sender.adapter._on_generation_stopped(pressed, None)  # no gateway here: /stop itself goes nowhere
    result = await sender.adapter.send_draft(CHAT, 7, "more of the answer")
    assert result.success  # the stream carries on without falling back to edited messages
    assert len(sender.standin.calls_of("sendRichMessageDraft")) == before
