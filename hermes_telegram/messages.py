"""The agent's own messages after they are sent, and what a tool message can carry (docs/design.md, Tools;
slice 6: edits, pins, quote replies, link previews, effects).

- ``telegram_message`` changes only the bot's own messages in the turn's conversation: the chat and topic the
  bot-message index (slice 4) recorded when each was sent. Telegram's methods take a chat and a message, not
  a topic, so that check is the plugin's.
- ``edit`` replaces the text of one of the bot's text messages as ``send_single`` would send it: rich when an
  answer would be, else MarkdownV2, one message long. Its buttons stay unless new ones are given (an edit
  without a keyboard would remove it). Telegram's "message is not modified" is a change made. Files, polls,
  stickers, widgets and Hermes' own prompts are not edited.
- ``delete`` removes the message; ``pin`` pins it (silently unless asked), ``unpin`` unpins it.
- A tool message may reply to the user's current message or to one of the bot's messages in the same
  conversation, quoting part of it (``ReplyParameters.quote``, which Telegram refuses unless it is an exact
  part of that message); may set link preview options, which Telegram applies to an ordinary message and a
  rich message does not take; and may carry a message effect, in private chats.
- Effects: the Bot API has no method that lists them. ``EFFECTS`` names the ids Telegram's clients offer for
  six emoji; they are client evidence, to be confirmed in live acceptance. Any other id is passed on, and a
  refusal comes back to the agent.
"""

from typing import Any, Optional

EFFECTS = {
    "🔥": "5104841245755180586",
    "👍": "5107584321108051014",
    "👎": "5104858069142078462",
    "❤️": "5159385139981059251",
    "🎉": "5046509860389126442",
    "💩": "5046589136895476101",
}
_PREVIEW_KEYS = {"disabled": "is_disabled", "url": "url", "small": "prefer_small_media",
                 "large": "prefer_large_media", "above": "show_above_text"}


def effect_id(effect: str) -> Optional[str]:
    """The message effect id of ``effect`` (one of ``EFFECTS``' emoji, or an id), or ``None``."""
    effect = effect.strip()
    named = EFFECTS.get(effect) or EFFECTS.get(effect + "️") or EFFECTS.get(effect.replace("️", ""))
    if named:
        return named
    return effect if effect.isdigit() else None


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

        mine = self._bot_message(chat_id, message_id) or {}
        keyboard = markup if markup is not None else mine.get("markup")
        try:
            as_rich, body = self._single_body(text, bool(keyboard))
        except ValueError as too_long:
            return SendResult(success=False, error=str(too_long))
        target: dict[str, Any] = {"chat_id": normalize_telegram_chat_id(chat_id), "message_id": int(message_id)}
        extra: dict[str, Any] = {"reply_markup": keyboard} if keyboard else {}
        try:
            if as_rich:
                await self._bot.do_api_request("editMessageText", api_kwargs={
                    **target, **extra, "rich_message": {"markdown": body}})
            else:
                preview = preview_options(None, getattr(self, "_disable_link_previews", False))
                await self._bot.edit_message_text(text=body, parse_mode="MarkdownV2", **target, api_kwargs={
                    **extra, **({"link_preview_options": preview} if preview else {})})
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                return SendResult(success=False, error=_redact_telegram_error_text(exc))
        if markup is not None:
            self._remember_bot_messages(chat_id, [message_id], mine.get("thread"), kind="text", markup=markup)
        return SendResult(success=True, message_id=str(message_id))

    async def delete_own(self, chat_id: str, message_id: str) -> Any:
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        try:
            await self._bot.delete_message(chat_id=normalize_telegram_chat_id(chat_id), message_id=int(message_id))
        except Exception as exc:
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        self._forget_bot_message(chat_id, message_id)
        return SendResult(success=True, message_id=str(message_id))

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
