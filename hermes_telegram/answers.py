"""Answers as Telegram rich messages (docs/design.md, Agent interface, Answers; slice 1).

The harvested outbound path already sends one rich message for a few constructs, behind the ``rich_messages``
opt-in, and falls back to MarkdownV2 when Telegram refuses it. This mixin changes what goes there and how
much: rich is on unless ``rich_messages: false``; any structure counts (``rich.needs_rich``); the Markdown
sent is ``rich.prepare_markdown``'s; and every rich answer goes through ``_send_items``, which cuts it to
Telegram's limits, sends a refused part again smaller, and resumes only parts certainly not delivered.

Streaming (previews that the gateway edits, heads it seals while a long answer streams) still takes the
harvested path; slice 3 owns it (docs/review-log.md, slice 1).
"""

import asyncio
import contextvars
import re
from collections import deque
from datetime import timedelta
from typing import Any, Optional

from gateway.platforms.base import SendResult, utf16_len

from . import rich
from .harvested.adapter_module import (
    _flood_cap_result,
    _iter_exception_graph,
    _separate_chunk_indicator_from_fence,
)

# An item still to send: ("rich", prepared markdown, times cut so far) or ("plain", MarkdownV2 chunk, 0).
Item = tuple[str, str, int]
_MAX_RESPLITS = 2
# Below this a refused part is not cut further: it goes as MarkdownV2 chunks.
_MIN_PART_CHARS = 1000
# Set while _send_items sends a MarkdownV2 chunk of a rich answer (see _send_chunk_markdown_or_plain).
_FALLBACK_CHUNK: contextvars.ContextVar[bool] = contextvars.ContextVar("hermes_telegram_fallback_chunk",
                                                                      default=False)


class _FloodWait(Exception):
    """A flood refusal with its wait in seconds, as the harvested chunk sender reads one (``retry_after``)."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"Flood control exceeded. Retry in {seconds:g} seconds")
        self.retry_after = seconds


class RichAnswersMixin:
    """Overrides of the harvested outbound methods for answers; listed before the harvested mixins."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # The bundled default is off (opt-in); here rich is the default and the key opts out.
        self._rich_messages_enabled = self._coerce_bool_extra("rich_messages", True)

    # --- what is rich, and what it sends -----------------------------------------------------------------

    def _needs_rich_rendering(self, content: str) -> bool:
        return rich.needs_rich(content)

    def _rich_message_payload(self, content: str, *, skip_entity_detection: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"markdown": rich.prepare_markdown(content)}
        if skip_entity_detection:
            payload["skip_entity_detection"] = True
        return payload

    def _content_fits_rich_limits(self, content: str) -> bool:
        """The message Telegram receives fits: its prepared Markdown, not the agent's text, is measured."""
        prepared = rich.prepare_markdown(content)
        return len(prepared) <= rich.MAX_CHARS and rich.estimate_blocks(prepared) <= rich.MAX_BLOCKS

    def format_message(self, content: str) -> str:
        """The ordinary (MarkdownV2) path shows ``\\$`` as written, so the hint's escape is undone first."""
        return super().format_message(rich.unescape_dollars(content))

    def _rich_eligible_any_length(self, content: str) -> bool:
        """``_rich_eligible`` without the one-message size check: a long answer is cut, not degraded."""
        return bool(
            self._rich_delivery_enabled()
            and not getattr(self, "_rich_send_disabled", False)
            and content and content.strip()
            and self._needs_rich_rendering(content)
            and not self._has_telegram_desktop_details_math_crash_shape(content)
            and (getattr(self, "_allow_cjk_rich_messages", False)
                 or not self._has_telegram_desktop_cjk_rich_garble_shape(content))
            and self._bot_supports_rich())

    @staticmethod
    def _rich_parts(prepared: str) -> list[str]:
        return rich.split_markdown(prepared)

    def _certainly_unsent(self, exc: BaseException) -> bool:
        """Telegram certainly did not show the message: it refused it with a flood wait, or the request never
        left (the connection did not open, or no pooled connection was free). A timeout or a connection lost
        after the request went out is not certain: Telegram may have shown it."""
        try:  # imported here: the SDK may be installed after this module loads (ensure_deps_fn)
            from telegram.error import RetryAfter
        except ImportError:
            RetryAfter = None
        if RetryAfter is not None and isinstance(exc, RetryAfter):
            return True
        return bool(self._looks_like_connect_timeout(exc) or self._looks_like_pool_timeout(exc) or any(
            "connecterror" in cur.__class__.__name__.lower() or "connecterror" in str(cur).lower()
            for cur in _iter_exception_graph(exc)))

    def _rich_transient_result(self, exc: Exception, what: str, *, retry_after: Any = None) -> SendResult:
        """The harvested result, marked with whether the part certainly was not delivered.

        PTB may give a flood wait as a ``timedelta``; the cooldown and the base retry count seconds. A pool
        timeout never left (PTB says so), so it is retryable as a connect timeout is."""
        if isinstance(retry_after, timedelta):
            retry_after = retry_after.total_seconds()
        result = super()._rich_transient_result(exc, what, retry_after=retry_after)
        unsent = retry_after is not None or self._certainly_unsent(exc)
        if self._looks_like_pool_timeout(exc):
            result.retryable = True
        raw = dict(result.raw_response) if isinstance(result.raw_response, dict) else {}
        raw["certainly_unsent"] = bool(unsent)
        result.raw_response = raw
        return result

    async def _send_chunk_markdown_or_plain(self, chunk: str, send_kwargs: dict[str, Any]) -> Any:
        """The harvested single attempt, with two changes for the retry loop around it.

        A flood wait PTB gives as a ``timedelta`` is passed on in seconds: the loop computes
        ``float(retry_after)``. And for a chunk of a rich answer (``_FALLBACK_CHUNK``), an error after which
        Telegram may have shown the chunk is passed on as a ``TimedOut``, which the loop does not retry: the
        answer's sender then stops rather than show the chunk twice. Ordinary messages keep the harvested
        retry policy (docs/review-log.md, slice 1)."""
        try:
            return await super()._send_chunk_markdown_or_plain(chunk, send_kwargs)
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(retry_after, timedelta):
                raise _FloodWait(retry_after.total_seconds()) from exc
            if not _FALLBACK_CHUNK.get() or self._is_bad_request_error(exc) or self._certainly_unsent(exc):
                raise
            from telegram.error import NetworkError, TimedOut
            if isinstance(exc, NetworkError) and not isinstance(exc, TimedOut):
                raise TimedOut(f"Telegram's reply was lost after the chunk was sent: {exc}") from exc
            raise

    def _send_retry_is_final(self, result: SendResult) -> bool:
        """A part that may have been shown is never sent again: not by the base retry, which would resend the
        whole answer, and not by its plain-text fallback."""
        raw = result.raw_response if isinstance(result.raw_response, dict) else {}
        return bool(raw.get("ambiguous_delivery")) or super()._send_retry_is_final(result)

    # --- sending ----------------------------------------------------------------------------------------

    async def _send_text_locked(
            self, chat_id: str, content: str, reply_to: Optional[str], metadata: Optional[dict[str, Any]]
    ) -> SendResult:
        """Every rich answer goes through ``_send_items``; everything else takes the harvested path."""
        if (metadata or {}).get("expect_edits") or not self._rich_eligible_any_length(content):
            return await super()._send_text_locked(chat_id, content, reply_to, metadata)
        thread_id = self._metadata_thread_id(metadata)
        if self._compute_single_send_routing(chat_id, reply_to, metadata, thread_id) is None:
            # A DM topic send without its anchor: the harvested path owns that refusal.
            return await super()._send_text_locked(chat_id, content, reply_to, metadata)
        cooldown = self._send_flood_cooldown_remaining(chat_id)
        if cooldown is not None:
            return _flood_cap_result(cooldown)
        parts = self._rich_parts(rich.prepare_markdown(content))
        return await self._send_items(chat_id, deque(("rich", p, 0) for p in parts), [], reply_to, metadata)

    async def _pace(self, chat_id: str) -> None:
        remaining = self._chat_outbound_slot_remaining(chat_id)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._hold_chat_outbound_slot(chat_id)

    def _as_plain_chunks(self, markdown: str) -> list[Item]:
        """A prepared part as MarkdownV2 chunks, made from the agent's text (without the escapes preparation
        added) as the harvested sender makes its chunks."""
        source = rich.plain_source(markdown)
        chunks = self.truncate_message(self.format_message(source), self.MAX_MESSAGE_LENGTH, len_fn=utf16_len)
        if len(chunks) > 1:
            # truncate_message appends a raw " (1/2)"; MarkdownV2 reserves the parentheses.
            chunks = [_separate_chunk_indicator_from_fence(re.sub(r" \((\d+)/(\d+)\)$", r" \\(\1/\2\\)", c))
                      for c in chunks]
        return [("plain", c, 0) for c in chunks]

    async def _send_items(
            self, chat_id: str, items: "deque[Item]", delivered: list[str], reply_to: Optional[str],
            metadata: Optional[dict[str, Any]], *, thread_fallback: bool = False
    ) -> SendResult:
        """Send ``items`` in order. A rich part Telegram refuses is cut smaller and, failing that, sent as
        MarkdownV2 chunks; once rich messages are latched off, the rest go as MarkdownV2. A failure after
        something landed is a partial delivery, resumable only when the failed item certainly did not go.
        Once a chunk had to leave a forum topic Telegram no longer has, the rich parts after it leave it too."""
        thread_id = self._metadata_thread_id(metadata)
        dm_topic = self._is_private_dm_topic_send(chat_id, thread_id, metadata)
        error_types = self._telegram_error_types()
        while items:
            kind, text, splits = items[0]
            if kind == "rich" and getattr(self, "_rich_send_disabled", False):
                items.popleft()
                items.extendleft(reversed(self._as_plain_chunks(text)))
                continue
            index = len(delivered)
            await self._pace(chat_id)
            if kind == "plain":
                token = _FALLBACK_CHUNK.set(True)
                try:
                    outcome = await self._send_chunk_with_retries(
                        chat_id, text, index, reply_to, metadata, thread_id, thread_fallback, error_types)
                except Exception as exc:
                    classified = self._classify_send_exception(exc, error_types)
                    return self._partial(classified, items, delivered, tail_certain=self._certainly_unsent(exc),
                                         thread_fallback=thread_fallback)
                finally:
                    _FALLBACK_CHUNK.reset(token)
                if isinstance(outcome, SendResult):  # a definite non-delivery
                    return self._partial(outcome, items, delivered, thread_fallback=thread_fallback)
                msg, thread_fallback = outcome
                delivered.append(str(msg.message_id))
                items.popleft()
                continue
            anchor = reply_to if (dm_topic or self._should_thread_reply(reply_to, index)) else None
            result = await self._try_send_rich(
                chat_id, text, anchor, _without_thread(metadata) if thread_fallback else metadata)
            if result is None:
                # Refused for good (limits counted otherwise, a parse error) or the endpoint is gone: not
                # delivered, so sending it again in another form cannot duplicate it.
                items.popleft()
                smaller: list[str] = [text]
                if splits < _MAX_RESPLITS and len(text) >= _MIN_PART_CHARS and not getattr(
                        self, "_rich_send_disabled", False):
                    smaller = rich.split_markdown(text, char_budget=max(_MIN_PART_CHARS // 2, len(text) // 2),
                                                  block_budget=max(10, rich.estimate_blocks(text) // 2))
                if len(smaller) > 1:
                    items.extendleft(reversed([("rich", s, splits + 1) for s in smaller]))
                else:
                    items.extendleft(reversed(self._as_plain_chunks(text)))
                continue
            if not result.success:
                if result.retry_after is not None:
                    self._record_send_flood_cooldown(chat_id, result.retry_after)
                raw = result.raw_response if isinstance(result.raw_response, dict) else {}
                return self._partial(result, items, delivered, tail_certain=bool(raw.get("certainly_unsent")),
                                     thread_fallback=thread_fallback)
            delivered.append(result.message_id or "")
            items.popleft()
        await self._retrigger_typing(chat_id, metadata)
        return SendResult(success=True, message_id=delivered[0] if delivered else None, raw_response={
            "message_ids": list(delivered), "requested_thread_id": self._message_thread_id_for_send(thread_id),
            "thread_fallback": thread_fallback})

    @staticmethod
    def _partial(result: SendResult, items: "deque[Item]", delivered: list[str], *, tail_certain: bool = True,
                 thread_fallback: bool = False) -> SendResult:
        """The harvested ``partial_overflow`` contract, with the unsent items for ``_resume_partial_send``.

        A failed item that may have been shown marks the result ``ambiguous_delivery``: nothing sends it
        again (``_send_retry_is_final``), and its tail is not attached for resuming (Hermes'
        ``_resume_partial_send`` contract: resume only where non-delivery is certain). A certain failure
        with nothing delivered leaves the base retry free to send the whole answer again."""
        raw = dict(result.raw_response) if isinstance(result.raw_response, dict) else {}
        if not tail_certain:
            raw["ambiguous_delivery"] = True
        if delivered:
            raw.update({
                "partial_overflow": True, "delivered_chunks": len(delivered),
                "total_chunks": len(delivered) + len(items), "last_message_id": delivered[-1],
                "continuation_message_ids": tuple(delivered[1:]), "thread_fallback": thread_fallback})
            if tail_certain and items:
                raw["undelivered_rich_items"] = tuple(items)
                raw["delivered_message_ids"] = tuple(delivered)
        result.raw_response = raw
        return result

    async def _resume_partial_send(
            self, chat_id: str, result: SendResult, *, reply_to: Optional[str], metadata: Optional[dict[str, Any]]
    ) -> Optional[SendResult]:
        raw = result.raw_response if isinstance(result.raw_response, dict) else {}
        items = raw.get("undelivered_rich_items")
        if not items:
            return await super()._resume_partial_send(chat_id, result, reply_to=reply_to, metadata=metadata)
        if not self._bot:
            return None
        delivered = list(raw.get("delivered_message_ids") or ())
        async with self._chat_send_lock(chat_id):
            cooldown = self._send_flood_cooldown_remaining(chat_id)
            thread_fallback = bool(raw.get("thread_fallback"))
            if cooldown is not None:
                return self._partial(_flood_cap_result(cooldown), deque(items), delivered,
                                     thread_fallback=thread_fallback)
            return await self._send_items(chat_id, deque(items), delivered, reply_to, metadata,
                                          thread_fallback=thread_fallback)


def _without_thread(metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    """``metadata`` without the forum topic, as the harvested chunk sender drops it after a fallback."""
    return {k: v for k, v in (metadata or {}).items() if k not in ("thread_id", "message_thread_id")}
