"""Answers as Telegram rich messages (docs/design.md, Agent interface, Answers; slice 1).

The harvested outbound path already sends one rich message for a few constructs, behind the ``rich_messages``
opt-in, and falls back to MarkdownV2 when Telegram refuses it. This mixin changes what goes there and how
much: rich is on unless ``rich_messages: false``; any structure counts (``rich.needs_rich``); the Markdown
sent is ``rich.prepare_markdown``'s; and an answer over one message's limits is cut into consecutive rich
messages instead of falling back to MarkdownV2 chunks.
"""

from collections import deque
from typing import Any, Optional

from gateway.platforms.base import SendResult, utf16_len

from . import rich
from .harvested.adapter_module import _flood_cap_result, _rich_normalize_linebreaks

# An item of a cut answer still to send: ("rich", markdown, splits so far) or ("plain", MarkdownV2 chunk, 0).
Item = tuple[str, str, int]
_MAX_RESPLITS = 2


class RichAnswersMixin:
    """Overrides of the harvested outbound methods for answers; listed before the harvested mixins."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # The bundled default is off (opt-in); here rich is the default and the key opts out.
        self._rich_messages_enabled = self._coerce_bool_extra("rich_messages", True)

    # --- what is rich, and what it sends -----------------------------------------------------------------

    def _needs_rich_rendering(self, content: str) -> bool:
        return rich.needs_rich(content)

    def _rich_markdown(self, content: str) -> str:
        return _rich_normalize_linebreaks(rich.prepare_markdown(content))

    def _rich_message_payload(self, content: str, *, skip_entity_detection: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"markdown": self._rich_markdown(content)}
        if skip_entity_detection:
            payload["skip_entity_detection"] = True
        return payload

    def _content_fits_rich_limits(self, content: str) -> bool:
        """The message Telegram receives fits: its prepared Markdown, not the agent's text, is measured."""
        return len(self._rich_markdown(content)) <= rich.MAX_CHARS and rich.estimate_blocks(content) <= rich.MAX_BLOCKS

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

    def _rich_parts(self, content: str) -> list[str]:
        """``content`` cut so that each part's prepared Markdown fits one rich message."""
        return [fitted for part in rich.split_markdown(content) for fitted in self._fit_part(part, rich.CHAR_BUDGET)]

    def _fit_part(self, part: str, budget: int, depth: int = 0) -> list[str]:
        # Escapes and hard line breaks make the sent Markdown longer than the part: cut again if they push
        # it over the limit.
        size = len(self._rich_markdown(part))
        if size <= rich.MAX_CHARS or depth >= 3:
            return [part]
        smaller = max(1000, int(budget * rich.MAX_CHARS / size * 0.9))
        return [p for sub in rich.split_markdown(part, char_budget=smaller)
                for p in self._fit_part(sub, smaller, depth + 1)]

    # --- sending ----------------------------------------------------------------------------------------

    async def _send_text_locked(
            self, chat_id: str, content: str, reply_to: Optional[str], metadata: Optional[dict[str, Any]]
    ) -> SendResult:
        """A long rich answer goes as several rich messages; everything else takes the harvested path."""
        if (metadata or {}).get("expect_edits") or not self._rich_eligible_any_length(content):
            return await super()._send_text_locked(chat_id, content, reply_to, metadata)
        parts = self._rich_parts(content)
        if len(parts) == 1:
            return await super()._send_text_locked(chat_id, content, reply_to, metadata)
        thread_id = self._metadata_thread_id(metadata)
        if self._compute_single_send_routing(chat_id, reply_to, metadata, thread_id) is None:
            # A DM topic send without its anchor: the harvested path owns that refusal.
            return await super()._send_text_locked(chat_id, content, reply_to, metadata)
        cooldown = self._send_flood_cooldown_remaining(chat_id)
        if cooldown is not None:
            return _flood_cap_result(cooldown)
        return await self._send_items(chat_id, deque(("rich", p, 0) for p in parts), [], reply_to, metadata)

    async def _pace(self, chat_id: str) -> None:
        import asyncio

        remaining = self._chat_outbound_slot_remaining(chat_id)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._hold_chat_outbound_slot(chat_id)

    async def _send_items(
            self, chat_id: str, items: "deque[Item]", delivered: list[str], reply_to: Optional[str],
            metadata: Optional[dict[str, Any]]
    ) -> SendResult:
        """Send ``items`` in order. A part Telegram refuses is cut smaller and, failing that, sent as
        MarkdownV2 chunks; a failure after something landed is a partial delivery the caller can resume."""
        thread_id = self._metadata_thread_id(metadata)
        dm_topic = self._is_private_dm_topic_send(chat_id, thread_id, metadata)
        error_types = self._telegram_error_types()
        while items:
            kind, text, splits = items[0]
            index = len(delivered)
            await self._pace(chat_id)
            if kind == "plain":
                try:
                    outcome = await self._send_chunk_with_retries(
                        chat_id, text, index, reply_to, metadata, thread_id, False, error_types)
                except Exception as exc:
                    classified = self._classify_send_exception(exc, error_types)
                    return self._partial(classified, items, delivered, tail_certain=classified.retryable)
                if isinstance(outcome, SendResult):  # a definite non-delivery
                    return self._partial(outcome, items, delivered)
                delivered.append(str(outcome[0].message_id))
                items.popleft()
                continue
            anchor = reply_to if (dm_topic or self._should_thread_reply(reply_to, index)) else None
            result = await self._try_send_rich(chat_id, text, anchor, metadata)
            if result is None:
                # Telegram refused this part for good (limits it counts differently, a parse error): it
                # was not delivered, so sending it again in another form cannot duplicate it.
                items.popleft()
                smaller = (rich.split_markdown(text, char_budget=max(1000, len(text) // 2),
                                               block_budget=max(10, rich.estimate_blocks(text) // 2))
                           if splits < _MAX_RESPLITS else [text])
                if len(smaller) > 1:
                    items.extendleft(reversed([("rich", s, splits + 1) for s in smaller]))
                else:
                    chunks = self.truncate_message(self.format_message(text), self.MAX_MESSAGE_LENGTH, len_fn=utf16_len)
                    items.extendleft(reversed([("plain", c, 0) for c in chunks]))
                continue
            if not result.success:
                return self._partial(result, items, delivered, tail_certain=result.retryable)
            delivered.append(result.message_id or "")
            items.popleft()
        await self._retrigger_typing(chat_id, metadata)
        return SendResult(success=True, message_id=delivered[0] if delivered else None, raw_response={
            "message_ids": list(delivered), "requested_thread_id": self._message_thread_id_for_send(thread_id),
            "thread_fallback": False})

    @staticmethod
    def _partial(result: SendResult, items: "deque[Item]", delivered: list[str], *,
                 tail_certain: bool = True) -> SendResult:
        """The harvested ``partial_overflow`` contract, with the unsent items for ``_resume_partial_send``.

        Nothing delivered: the result is returned as it is, and a retry sends the whole answer again."""
        if not delivered:
            return result
        raw = dict(result.raw_response) if isinstance(result.raw_response, dict) else {}
        raw.update({
            "partial_overflow": True, "delivered_chunks": len(delivered),
            "total_chunks": len(delivered) + len(items), "last_message_id": delivered[-1],
            "continuation_message_ids": tuple(delivered[1:])})
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
            if cooldown is not None:
                return self._partial(_flood_cap_result(cooldown), deque(items), delivered)
            return await self._send_items(chat_id, deque(items), delivered, reply_to, metadata)
