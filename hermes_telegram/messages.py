"""The agent's own messages after they are sent, and what a tool message can carry (docs/design.md, Tools;
slice 6: edits, pins, quote replies, link previews, effects).

- ``telegram_message`` changes only the bot's own messages in the turn's conversation: the chat and topic the
  bot-message index (slice 4) recorded when each was sent. Telegram's methods take a chat and a message, not
  a topic, so that check is the plugin's.
  Each record names the bot that sent the message and the profile the chat was routed to; a record of
  another bot, another profile or an unknown one (kept before slice 6) authorizes nothing.
- ``edit`` replaces the text of one of the bot's text messages, one message long, in the form it was sent
  in: Telegram refuses a rich edit of an ordinary message, so an ordinary message stays MarkdownV2 (with a
  note when the text has structure only a rich message shows), and a rich one stays rich. Its buttons stay
  unless new ones are given (an edit without a keyboard would remove it); ``[]`` removes them. Telegram's
  "message is not modified" is a change made; a rich edit updates Hermes' reply context
  (``rich_sent_store``). Files, polls, stickers, widgets and Hermes' own prompts are not edited.
- ``delete`` removes the message and retires what came back from it: its buttons, widget and poll.
  ``pin`` pins it (silently unless asked), ``unpin`` unpins it.
- A tool message may reply to the user's current message or to one of the bot's messages in the same
  conversation. It may quote part of the user's current message: Telegram matches the quote with its
  formatting, so the quote carries that message's entities over its span (``quote_entities``,
  ``quote_position``). A quote of the bot's own messages is refused: their entities and rich blocks are not
  kept. Link preview options apply to an ordinary message (a rich message takes none), and a message effect
  to a private chat; a text Telegram delivered without the effect asked for says so.
- Effects: the Bot API has no method that lists them. ``EFFECTS`` names the ids Telegram's clients offer for
  six emoji; they are client evidence, to be confirmed in live acceptance. Any other id is passed on, and a
  refusal comes back to the agent.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

EFFECTS = {
    "🔥": "5104841245755180586",
    "👍": "5107584321108051014",
    "👎": "5104858069142078462",
    "❤️": "5159385139981059251",
    "🎉": "5046509860389126442",
    "💩": "5046589136895476101",
}
_QUOTE_ENTITIES = {"bold", "italic", "underline", "strikethrough", "spoiler", "custom_emoji", "date_time"}
_PREVIEW_KEYS = {"disabled": "is_disabled", "url": "url", "small": "prefer_small_media",
                 "large": "prefer_large_media", "above": "show_above_text"}


def effect_id(effect: str) -> Optional[str]:
    """The message effect id of ``effect`` (one of ``EFFECTS``' emoji, or an id), or ``None``."""
    effect = effect.strip()
    named = EFFECTS.get(effect) or EFFECTS.get(effect + "️") or EFFECTS.get(effect.replace("️", ""))
    if named:
        return named
    return effect if effect.isdigit() and int(effect) > 0 else None  # 0 is no effect


def quote_parts(text: str, entities: list[dict[str, Any]], quote: str) -> Optional[dict[str, Any]]:
    """ReplyParameters fields quoting ``quote`` in a message of ``text`` with ``entities`` (UTF-16 offsets),
    the formatting Telegram matches carried over; ``None`` when ``quote`` is not a part of ``text``."""
    from gateway.platforms.base import utf16_len

    at = text.find(quote)
    if not quote or at < 0:
        return None
    start = utf16_len(text[:at])
    end = start + utf16_len(quote)
    kept = []
    for entity in entities:
        offset, length = int(entity.get("offset", 0)), int(entity.get("length", 0))
        if entity.get("type") not in _QUOTE_ENTITIES or offset >= end or offset + length <= start:
            continue
        lo, hi = max(offset, start), min(offset + length, end)
        kept.append({**entity, "offset": lo - start, "length": hi - lo})
    return {"quote": quote, "quote_position": start, **({"quote_entities": kept} if kept else {})}


def preview_options(requested: Optional[dict[str, Any]], disabled_by_profile: bool) -> Optional[dict[str, Any]]:
    """LinkPreviewOptions for a tool message: the agent's, else the profile's ``disable_link_previews``."""
    if requested:
        return {_PREVIEW_KEYS[k]: v for k, v in requested.items() if k in _PREVIEW_KEYS and v not in (None, "")}
    return {"is_disabled": True} if disabled_by_profile else None


class MessagesMixin:
    """Edit, delete, pin and unpin the bot's own messages; listed before the harvested mixins. The callers
    (``tools.py``) check that the message is the bot's, in the turn's conversation."""

    async def edit_text(self, chat_id: str, message_id: str, text: str,
                        markup: Optional[dict[str, Any]] = None) -> Any:
        """Replace the text of a text message as ``send_single`` sends one; without ``markup`` its keyboard
        stays. A text it already has is a change made."""
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        from gateway.platforms.base import utf16_len

        from . import rich

        mine = self._bot_message(chat_id, message_id) or {}
        keyboard = markup if markup is not None else mine.get("markup")
        was_rich = mine.get("rich")
        if was_rich is None:  # recorded before the record said so, when Hermes indexed only the rich ones it sent
            try:
                from gateway import rich_sent_store
                was_rich = rich_sent_store.lookup(str(chat_id), str(message_id)) is not None
            except Exception:
                was_rich = False
        note = None
        if was_rich:
            body = rich.prepare_markdown(text)
            if not (self._rich_eligible_any_length(text, part=True) and len(body) <= rich.MAX_CHARS
                    and rich.estimate_blocks(body) <= rich.MAX_BLOCKS):
                return SendResult(success=False, error=(
                    "this message was sent as a rich message, and this text cannot go as one (too long, or a "
                    "shape Telegram Desktop cannot show): send a new message instead"))
        else:
            body = self.format_message(text)
            if utf16_len(body) > self.MAX_MESSAGE_LENGTH:
                return SendResult(success=False, error="the text is too long for one message: put it in your answer")
            if self._needs_rich_rendering(text):
                note = ("The message was an ordinary one, and Telegram does not turn it into a rich one: its "
                        "tables or other structure are shown simplified. Send a new message to show them.")
        target: dict[str, Any] = {"chat_id": normalize_telegram_chat_id(chat_id), "message_id": int(message_id)}
        extra: dict[str, Any] = {"reply_markup": keyboard} if keyboard is not None else {}
        try:
            if was_rich:
                await self._bot.do_api_request("editMessageText", api_kwargs={
                    **target, **extra, "rich_message": {"markdown": body}})
            else:
                preview = preview_options(None, getattr(self, "_disable_link_previews", False))
                await self._bot.edit_message_text(text=body, parse_mode="MarkdownV2", **target, api_kwargs={
                    **extra, **({"link_preview_options": preview} if preview else {})})
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                return self._send_failed(exc, _redact_telegram_error_text(exc))
        # Hermes' quote-reply context for this message, indexed as the bundled edit indexes a final text.
        from .harvested.telegram_split import quotable_text
        await self._record_sent_text(chat_id, message_id, text if was_rich else quotable_text(text))
        if markup is not None:
            self._keep_keyboard(chat_id, message_id, mine, markup)
        result = SendResult(success=True, message_id=str(message_id))
        result.raw_response = {"note": note} if note else None
        return result

    def _keep_keyboard(self, chat_id: str, message_id: str, mine: dict[str, Any],
                       markup: Optional[dict[str, Any]]) -> None:
        """The keyboard the message shows now, for the next edit."""
        self._remember_bot_messages(chat_id, [message_id], mine.get("thread"), kind="text", markup=markup,
                                    rich=mine.get("rich"))

    async def delete_own(self, chat_id: str, message_id: str) -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        try:
            await self._bot.delete_message(chat_id=normalize_telegram_chat_id(chat_id), message_id=int(message_id))
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        self._forget_bot_message(chat_id, message_id)
        self._retire_message(chat_id, message_id)
        return SendResult(success=True, message_id=str(message_id))

    def _retire_message(self, chat_id: str, message_id: str) -> None:
        """A deleted message takes no more taps, widget requests or votes, even ones already on their way."""
        from .actions import _POLL_MEMORY
        from .buttons import StoreError
        from .widgets import _WIDGET_MEMORY

        try:
            self._button_store().retire(chat_id, message_id)
            self._store("widgets", _WIDGET_MEMORY).drop(f"{chat_id}:{message_id}")
            polls = self._store("polls", _POLL_MEMORY)
            for poll_id, poll in list(polls.data.items()):
                if poll.get("chat") == str(chat_id) and poll.get("message_id") == str(message_id):
                    polls.drop(poll_id)
        except StoreError:
            logger.warning("[%s] a deleted message's choices could not be retired", self.name, exc_info=True)

    async def pin(self, chat_id: str, message_id: str, *, pinned: bool, notify: bool = False) -> Any:
        """Pin (silently unless ``notify``) or unpin a message."""
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        target = {"chat_id": normalize_telegram_chat_id(chat_id), "message_id": int(message_id)}
        try:
            if pinned:
                await self._bot.pin_chat_message(**target, disable_notification=not notify)
            else:
                await self._bot.unpin_chat_message(**target)
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        return SendResult(success=True, message_id=str(message_id))
