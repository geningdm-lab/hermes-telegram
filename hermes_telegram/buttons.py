"""Action buttons the agent offers, and their taps coming back to it (docs/design.md, Events; slice 4,
row 11).

A button with a value carries ``tb:<token>`` as its callback data (at most 64 bytes, inlinekeyboardbutton.md).
The token's label and value are kept in a store under the profile's Hermes home, bounded, so a tap on an
old message still resolves after a restart; a token the store no longer knows gets an "expired" notice.

A tap becomes an ordinary inbound message of the same chat, from the user who tapped, built as the harvested
adapter builds a text message and passed to ``handle_message``, so it is authorized and routed exactly like a
text from that user (a tap by the routed ``family`` user reaches the ``family`` profile). Its text is
``[button] "<label>" value="<value>" on message <id>``. A set marked single-use takes its first tap only:
the second answers "Already answered" and sends nothing to the agent.
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


def event_text(label: str, value: str, message_id: Any) -> str:
    return f'[button] "{label}" value="{value}" on message {message_id}'


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
        except Exception:
            logger.warning("[Telegram] Could not save the button store at %s", self.path, exc_info=True)

    def new_set(self, chat_id: str, buttons: list[tuple[str, str]], *, single_use: bool) -> list[str]:
        """Tokens for ``(label, value)`` buttons of one message in ``chat_id``."""
        self._load()
        set_id = secrets.token_urlsafe(_TOKEN_BYTES)
        self._sets[set_id] = {"chat": str(chat_id), "single_use": single_use, "answered": None}
        tokens = []
        for label, value in buttons:
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            self._tokens[token] = {"set": set_id, "label": label, "value": value}
            tokens.append(token)
        self._save()
        return tokens

    def claim(self, token: str, chat_id: str) -> tuple[Optional[dict[str, Any]], bool]:
        """``(button, first)`` for a tap in ``chat_id``: ``(None, False)`` for a token not known here,
        ``(button, False)`` for a single-use set already answered. Claims synchronously: no await between
        the check and the mark, so two taps cannot both be first."""
        self._load()
        button = self._tokens.get(token)
        if button is None:
            return None, False
        group = self._sets.get(button["set"]) or {}
        if group.get("chat") != str(chat_id):
            return None, False
        if group.get("single_use"):
            if group.get("answered") is not None:
                return button, False
            group["answered"] = button["label"]
            self._save()
        return button, True


class ButtonsMixin:
    """Value buttons' taps; listed before the harvested mixins."""

    def _button_store(self) -> ButtonStore:
        store = self.__dict__.get("_hermes_telegram_buttons")
        if store is None:
            store = ButtonStore(Path(self._update_receipt_dir) / "telegram_buttons.json")
            self.__dict__["_hermes_telegram_buttons"] = store
        return store

    def button_markup(self, chat_id: str, buttons: list[dict[str, Any]], *, single_use: bool) -> dict[str, Any]:
        """The Bot API ``InlineKeyboardMarkup`` for the agent's buttons, one per row, with fresh tokens."""
        valued = [(b["label"], b["value"]) for b in buttons if "value" in b]
        tokens = iter(self._button_store().new_set(chat_id, valued, single_use=single_use)) if valued else iter(())
        rows = []
        for b in buttons:
            button: dict[str, Any] = {"text": b["label"]}
            if "url" in b:
                button["url"] = b["url"]
            elif "copy" in b:
                button["copy_text"] = {"text": b["copy"]}
            else:
                button["callback_data"] = PREFIX + next(tokens)
            if b.get("style"):
                button["style"] = b["style"]
            rows.append([button])
        return {"inline_keyboard": rows}

    async def send_with_buttons(self, chat_id: str, text: str, markup: dict[str, Any],
                                metadata: Optional[dict[str, Any]]) -> Any:
        """One message with its buttons: a rich message, or MarkdownV2 when Telegram refuses the rich one."""
        from gateway.platforms.base import SendResult

        from . import rich
        from .harvested.adapter_module import normalize_telegram_chat_id

        if not self._bot:
            return SendResult(success=False, error="Not connected")
        prepared = rich.prepare_markdown(text)
        if len(prepared) > rich.MAX_CHARS:
            return SendResult(success=False, error="the text is too long for one message with buttons")
        routing = self._compute_single_send_routing(chat_id, None, metadata, self._metadata_thread_id(metadata))
        if routing is None:
            return SendResult(success=False, error=self._dm_topic_missing_anchor_error())
        reply_to_id, thread_kwargs = routing
        base = {"chat_id": normalize_telegram_chat_id(chat_id), "reply_markup": markup,
                **{k: v for k, v in thread_kwargs.items() if v is not None}}
        if reply_to_id is not None:
            base["reply_parameters"] = {"message_id": reply_to_id}
        await self._before_message(chat_id, self._metadata_thread_id(metadata), text)  # the preview's fence
        if self._bot_supports_rich() and not getattr(self, "_rich_send_disabled", False):
            try:
                sent = await self._bot.do_api_request("sendRichMessage",
                                                       api_kwargs={**base, "rich_message": {"markdown": prepared}})
                message_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
                self._reshow_soon(chat_id, self._metadata_thread_id(metadata))
                self._remember_bot_messages(chat_id, [SendResult(success=True, message_id=str(message_id))])
                return SendResult(success=True, message_id=str(message_id) if message_id is not None else None)
            except Exception as exc:
                if not self._rich_rejected(exc, "sendRichMessage with buttons", "MarkdownV2"):
                    return self._rich_transient_result(exc, "sendRichMessage with buttons")
        try:
            msg = await self._bot.send_message(text=self.format_message(text), parse_mode="MarkdownV2",
                                               api_kwargs={k: v for k, v in base.items() if k != "chat_id"},
                                               chat_id=base["chat_id"])
        except Exception as exc:
            from .harvested.adapter_module import _redact_telegram_error_text
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        self._reshow_soon(chat_id, self._metadata_thread_id(metadata))
        self._remember_bot_messages(chat_id, [msg])
        return SendResult(success=True, message_id=str(msg.message_id))

    async def _handle_callback_query(self, update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        if query is None or not str(getattr(query, "data", "") or "").startswith(PREFIX):
            return await super()._handle_callback_query(update, context)
        self._accept_update()
        cb = self._callback_ctx(query)
        if not await self._callback_authorized(query, cb, "You cannot use these buttons."):
            return
        message = query.message
        button, first = self._button_store().claim(query.data[len(PREFIX):], str(cb["chat_id"]))
        if button is None:
            await query.answer(text=EXPIRED)
            return
        if not first:
            await query.answer(text=ALREADY_ANSWERED)
            return
        await query.answer()
        await self.handle_message(self._tap_event(query, message, button))

    def _tap_event(self, query: Any, message: Any, button: dict[str, Any]) -> Any:
        """The tap as an inbound text from the user who tapped, in the chat and topic of the buttons."""
        from gateway.platforms.base import MessageEvent, MessageType

        chat = message.chat
        user = query.from_user
        telegram_chat_type = self._chat_type_str(chat)
        chat_type = ("group" if telegram_chat_type in {"group", "supergroup"}
                     else "channel" if telegram_chat_type == "channel" else "dm")
        # Each tap is its own inbound: an id of its own (never a Telegram message id), while the answer still
        # replies to the message with the buttons. Reusing that message's id made a second tap look like the
        # first one again, and Hermes dropped it.
        tap_id = f"cb{query.id}"
        source = self.build_source(
            chat_id=str(chat.id), chat_name=chat.title or getattr(chat, "full_name", None), chat_type=chat_type,
            user_id=str(user.id), user_name=user.full_name, thread_id=self._effective_message_thread_id(message),
            message_id=tap_id, is_bot=bool(getattr(user, "is_bot", False)))
        return MessageEvent(text=event_text(button["label"], button["value"], message.message_id),
                            message_type=MessageType.TEXT, source=source, raw_message=message,
                            message_id=tap_id, reply_anchor_override=str(message.message_id),
                            user_id=str(user.id), user_name=user.full_name)

