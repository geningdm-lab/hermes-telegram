"""Action buttons the agent offers, and their taps coming back to it (docs/design.md, Events; slice 4,
row 11).

A button with a value carries ``tb:<token>`` as its callback data (at most 64 bytes, inlinekeyboardbutton.md).
The token's label and value are kept in a store under the profile's Hermes home, bounded, so a tap on an
old message still resolves after a restart; a token the store no longer knows gets an "expired" notice.
A set is bound to the message and topic that show it and to the profile that sent it: Telegram does not
promise that a query's message holds a button with that data (callbackquery.md), so a token presented with
another message, topic or profile is not this set's tap.

A tap is accepted in a private chat only (groups are out of scope), from a user the adapter admits as it
would admit their text (``_actor_admitted``), before anything changes. It becomes an ordinary inbound message
of the same chat and topic, from the user who tapped, passed to ``handle_message``, so it is routed exactly
like a text from that user (a tap by the routed ``family`` user reaches the ``family`` profile). Its text is
``[button] "<label>" value="<value>" on message <id>``, the quoted fields JSON-escaped. A set marked
single-use takes its first tap only: the second answers "Already answered" and sends nothing to the agent.
Telegram's acknowledgement of the tap is best effort; the hand-off to the agent is not: a tap whose hand-off
fails is released, so the next tap of the set is first again.
"""

import json
import logging
import secrets
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PREFIX = "tb:"
_TOKEN_BYTES = 9  # 12 url-safe characters
_MEMORY = 4000  # tokens kept; the oldest are forgotten first
ALREADY_ANSWERED = "Already answered"
EXPIRED = "This choice has expired"
NOT_HERE = "These buttons work in the private chat with the bot only"


def quoted(text: Any) -> str:
    """A field of an event's text: a JSON string, so a quote or a line break in it cannot change the form."""
    return json.dumps(str(text), ensure_ascii=False)


def event_text(label: str, value: str, message_id: Any) -> str:
    return f"[button] {quoted(label)} value={quoted(value)} on message {message_id}"


class StoreError(Exception):
    """The store could not be saved: nothing that depends on it may be published or handed on."""


class ButtonStore:
    """Tokens of value buttons and the sets they belong to, persisted as one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._tokens: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sets: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.path.read_text())
            self._tokens = OrderedDict(data.get("tokens") or {})
            self._sets = OrderedDict(data.get("sets") or {})
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("[Telegram] Ignoring an unreadable button store at %s", self.path, exc_info=True)

    def _save(self) -> None:
        from utils import atomic_json_write

        while len(self._tokens) > _MEMORY:
            _, entry = self._tokens.popitem(last=False)
            if not any(t["set"] == entry["set"] for t in self._tokens.values()):
                self._sets.pop(entry["set"], None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(self.path, {"tokens": self._tokens, "sets": self._sets})
        except Exception as exc:
            raise StoreError(f"the button store at {self.path} could not be saved: {exc}") from exc

    def new_set(self, chat_id: str, buttons: list[tuple[str, str]], *, single_use: bool,
                profile: str = "") -> tuple[str, list[str]]:
        """``(set id, tokens)`` for ``(label, value)`` buttons of one message in ``chat_id``; ``StoreError``
        when it could not be saved (the buttons must then not be sent)."""
        self._load()
        set_id = secrets.token_urlsafe(_TOKEN_BYTES)
        self._sets[set_id] = {"chat": str(chat_id), "single_use": single_use, "answered": None,
                              "message": None, "thread": None, "profile": profile or ""}
        tokens = []
        for label, value in buttons:
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            self._tokens[token] = {"set": set_id, "label": label, "value": value}
            tokens.append(token)
        try:
            self._save()
        except StoreError:
            for token in tokens:
                self._tokens.pop(token, None)
            self._sets.pop(set_id, None)
            raise
        return set_id, tokens

    def bind(self, set_id: str, message_id: Any, thread_id: Any) -> None:
        """The set belongs to the message that shows it, in its topic."""
        self._load()
        group = self._sets.get(set_id)
        if group is not None:
            group["message"], group["thread"] = str(message_id), str(thread_id) if thread_id else None
            self._save()

    def retire(self, chat_id: str, message_id: Any) -> None:
        """The sets bound to this message no longer take taps: an edit replaced its keyboard."""
        self._load()
        changed = False
        for group in self._sets.values():
            if group.get("chat") == str(chat_id) and group.get("message") == str(message_id):
                group["message"], changed = None, True
        if changed:
            self._save()

    def claim(self, token: str, chat_id: str, message_id: Any, thread_id: Any,
              profile: str = "") -> tuple[Optional[dict[str, Any]], bool]:
        """``(button, first)`` for a tap: ``(None, False)`` for a token not of this chat, message, topic and
        profile, ``(button, False)`` for a single-use set already answered. Claims synchronously: no await
        between the check and the mark, so two taps cannot both be first. ``StoreError`` when the claim could
        not be saved; it is then not taken."""
        self._load()
        button = self._tokens.get(token)
        if button is None:
            return None, False
        group = self._sets.get(button["set"]) or {}
        if (group.get("chat") != str(chat_id) or group.get("message") != str(message_id)
                or (group.get("thread") or None) != (str(thread_id) if thread_id else None)
                or (group.get("profile") or "") != (profile or "")):
            return None, False
        if group.get("single_use"):
            if group.get("answered") is not None:
                return button, False
            group["answered"] = button["label"]
            try:
                self._save()
            except StoreError:
                group["answered"] = None
                raise
        return button, True

    def release(self, token: str) -> None:
        """Undo a claim whose tap never reached the agent: the set's next tap is first again."""
        button = self._tokens.get(token)
        group = self._sets.get(button["set"]) if button else None
        if group is not None and group.get("answered") is not None:
            group["answered"] = None
            try:
                self._save()
            except StoreError:
                logger.warning("[Telegram] a released button claim could not be saved", exc_info=True)


class ButtonsMixin:
    """Value buttons and single tool messages; listed before the harvested mixins."""

    def _button_store(self) -> ButtonStore:
        store = self.__dict__.get("_hermes_telegram_buttons")
        if store is None:
            store = ButtonStore(Path(self._update_receipt_dir) / "telegram_buttons.json")
            self.__dict__["_hermes_telegram_buttons"] = store
        return store

    def button_markup(self, chat_id: str, buttons: list[dict[str, Any]], *, single_use: bool,
                      profile: str = "") -> tuple[dict[str, Any], Optional[str]]:
        """``(InlineKeyboardMarkup, set id)`` for the agent's buttons, one per row, with fresh tokens;
        ``StoreError`` when the tokens could not be saved."""
        valued = [(b["label"], b["value"]) for b in buttons if "value" in b]
        set_id, tokens = (self._button_store().new_set(chat_id, valued, single_use=single_use, profile=profile)
                          if valued else (None, []))
        pending = iter(tokens)
        rows = []
        for b in buttons:
            button: dict[str, Any] = {"text": b["label"]}
            if "url" in b:
                button["url"] = b["url"]
            elif "copy" in b:
                button["copy_text"] = {"text": b["copy"]}
            else:
                button["callback_data"] = PREFIX + next(pending)
            if b.get("style"):
                button["style"] = b["style"]
            rows.append([button])
        return {"inline_keyboard": rows}, set_id

    def _single_body(self, text: str, keyboard: bool) -> tuple[bool, str]:
        """``(rich, body)`` of a tool's text as one message: the prepared Markdown of a rich message when the
        answer path would send it rich (the same checks, Telegram Desktop's guards included), else MarkdownV2.
        ``ValueError`` when it does not fit one message."""
        from gateway.platforms.base import utf16_len

        from . import rich

        prepared = rich.prepare_markdown(text)
        if (self._rich_eligible_any_length(text, part=keyboard) and len(prepared) <= rich.MAX_CHARS
                and rich.estimate_blocks(prepared) <= rich.MAX_BLOCKS):
            return True, prepared
        formatted = self.format_message(text)
        if utf16_len(formatted) > self.MAX_MESSAGE_LENGTH:
            raise ValueError("the text is too long for one message: put it in your answer")
        return False, formatted

    async def send_single(self, chat_id: str, text: str, markup: Optional[dict[str, Any]],
                          metadata: Optional[dict[str, Any]], *, reply_to: Optional[str] = None,
                          quote: Optional[str] = None, link_preview: Optional[dict[str, Any]] = None,
                          effect: Optional[str] = None) -> Any:
        """A tool's message as one message, never several (``_single_body``); with its buttons, the profile's
        notification policy, and no fallback out of the turn's topic. ``reply_to`` replaces the turn's reply
        anchor, with ``quote`` an exact part of that message. ``link_preview`` (LinkPreviewOptions, else the
        profile's ``disable_link_previews``) applies to a MarkdownV2 message: a rich one does not take it.
        ``effect`` is a message effect id, for private chats."""
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id
        from .messages import preview_options

        if not self._bot:
            return SendResult(success=False, error="Not connected")
        try:
            as_rich, body = self._single_body(text, bool(markup))
        except ValueError as too_long:
            return SendResult(success=False, error=str(too_long))
        if effect and int(normalize_telegram_chat_id(chat_id)) <= 0:
            return SendResult(success=False, error="message effects work only in private chats")
        thread_id = self._metadata_thread_id(metadata)
        routing = self._compute_single_send_routing(chat_id, None, metadata, thread_id)
        if routing is None:
            return SendResult(success=False, error=self._dm_topic_missing_anchor_error())
        reply_to_id, thread_kwargs = routing
        base: dict[str, Any] = {"chat_id": normalize_telegram_chat_id(chat_id),
                                **{k: v for k, v in thread_kwargs.items() if v is not None},
                                **self._notification_kwargs(metadata)}
        if markup:
            base["reply_markup"] = markup
        if reply_to is not None:
            reply_to_id = int(reply_to)
        if reply_to_id is not None:
            base["reply_parameters"] = {"message_id": reply_to_id, **({"quote": quote} if quote else {})}
        if effect:
            base["message_effect_id"] = effect
        preview = preview_options(link_preview, getattr(self, "_disable_link_previews", False))
        await self._before_message(chat_id, thread_id, text)  # the preview's fence
        try:
            if as_rich:
                sent = await self._bot.do_api_request("sendRichMessage",
                                                       api_kwargs={**base, "rich_message": {"markdown": body}})
                message_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
            else:
                if preview:
                    base["link_preview_options"] = preview
                msg = await self._bot.send_message(chat_id=base.pop("chat_id"), text=body, parse_mode="MarkdownV2",
                                                   api_kwargs=base)
                message_id = msg.message_id
        except Exception as exc:
            return self._send_failed(exc, _redact_telegram_error_text(exc))
        result = SendResult(success=True, message_id=str(message_id))
        if as_rich and link_preview:
            result.raw_response = {"preview_ignored": True}
        self._remember_bot_messages(chat_id, [result], thread_id, markup=markup)
        self._reshow_soon(chat_id, thread_id)
        return result

    async def _handle_callback_query(self, update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        if query is None or not str(getattr(query, "data", "") or "").startswith(PREFIX):
            return await super()._handle_callback_query(update, context)
        self._accept_update()
        cb = self._callback_ctx(query)
        message = query.message
        if str(cb.get("chat_type") or "") != "private" or message is None:
            await self._answer_quietly(query, NOT_HERE)
            return
        thread_id = self._effective_message_thread_id(message)
        if not self._actor_admitted(query.from_user, str(cb["chat_id"]), thread_id):
            await self._answer_quietly(query, "You cannot use these buttons.")
            return
        event = self._action_event(str(cb["chat_id"]), thread_id, query.from_user, "", f"cb{query.id}",
                                   str(message.message_id))
        token = query.data[len(PREFIX):]
        try:
            button, first = self._button_store().claim(token, str(cb["chat_id"]), message.message_id, thread_id,
                                                       event.source.profile or "")
        except StoreError:
            logger.error("[%s] a button tap could not be recorded", self.name, exc_info=True)
            await self._answer_quietly(query, "Please try again")
            return
        if button is None:
            await self._answer_quietly(query, EXPIRED)
            return
        if not first:
            await self._answer_quietly(query, ALREADY_ANSWERED)
            return
        await self._answer_quietly(query, None)
        event.text = event_text(button["label"], button["value"], message.message_id)
        try:
            await self.handle_message(event)
        except Exception:
            logger.error("[%s] a button tap did not reach the agent; released", self.name, exc_info=True)
            self._button_store().release(token)

    @staticmethod
    async def _answer_quietly(query: Any, text: Optional[str]) -> None:
        """Telegram's acknowledgement of a tap: best effort, it never decides what the agent gets."""
        try:
            await query.answer(text=text) if text else await query.answer()
        except Exception:
            logger.debug("[Telegram] a callback query could not be answered", exc_info=True)
