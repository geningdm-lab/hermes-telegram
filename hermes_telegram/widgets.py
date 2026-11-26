"""Widgets: the agent's visual answers, and the requests they send back (docs/design.md, Tools, Events;
slice 5, rows 12 to 14).

A widget (core.telegram.org/bots/widgets) is one self-contained HTML document with inline CSS and JavaScript,
drawn inside a message and run on the user's device in a QuickJS sandbox with no network at all. Its code
never changes after it is sent; its state (``chat_data``, 1024 bytes) is the bot's to write. In production,
widgets work only in the private chat with the bot.

- ``check_widget`` holds the HTML to Telegram's limits, counted as the server counts them (at most 32768
  bytes of UTF-8 and 4096 bytes after zlib at level 9; no NUL), and to the widget profile: a network API
  called by its code throws, an image that is not a ``data:`` PNG, JPEG, GIF or WebP shows a placeholder, and
  an external script, stylesheet or frame never loads, so a widget relying on one is refused before it is
  sent. Text that merely names an API, and links (which open on a tap), are fine. ``check_state`` holds a
  state to the limits for state (NUL-free UTF-8, 1024 bytes), for a send and an edit alike.
- Each widget is kept with its topic and profile in ``telegram_widgets.json``, which outlives a restart. A
  query is taken for a widget of this bot only, and reaches the agent in that topic; a state change is made
  only from a turn in the same conversation.
- Interaction inside the widget stays on the device and reaches nobody (row 13). The widget's one way out
  is ``TelegramWidget.request(data)``, 1-64 bytes, arriving as a ``widget_query`` update. Telegram wants
  every query answered within 15 s, so the adapter answers first (without data), sending again within that
  time when Telegram certainly did not take the answer. Then, in the private chat and from a user the
  adapter admits as it would admit their text, the request reaches the agent as
  ``[widget] data="<data>" on widget <message id>`` (row 14). Every query reaches it once: the same query
  seen again is dropped, a second query with the same data is a second action.
"""

import asyncio
import logging
import re
import time
import zlib
from collections import OrderedDict
from typing import Any, Optional

from .buttons import StoreError, quoted

logger = logging.getLogger(__name__)

HTML_MAX = 32768  # bytes of UTF-8 (sendwidget.md)
COMPRESSED_MAX = 4096  # bytes after zlib at level 9
DATA_MAX = 1024  # chat_data, public_data, answer data
ANSWER_WITHIN = 14.0  # seconds after a query arrives; Telegram allows 15
_WIDGET_MEMORY = 2000
_QUERY_MEMORY = 512

# Called by the widget's code, these throw NetworkDisabledError (bots/widgets, "What widgets support").
_NETWORK_CALL = re.compile(
    r"\bfetch\s*\(|\bnew\s+(?:XMLHttpRequest|WebSocket|EventSource|RTCPeerConnection|Worker|SharedWorker|"
    r"BroadcastChannel)\b|\b(?:sendBeacon|importScripts)\s*\(")
# String literals and comments of the widget's code: naming an API there calls nothing.
_JS_TEXT = re.compile(r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`",
                      re.DOTALL)
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<([a-zA-Z][\w:-]*)\b((?:\"[^\"]*\"|'[^']*'|[^'\">])*)>", re.DOTALL)
_ATTR = re.compile(r"([^\s=/>\"']+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?")
_CSS_URL = re.compile(r"url\(\s*([\"']?)(.*?)\1\s*\)|@import\b", re.IGNORECASE | re.DOTALL)
_DATA_IMAGE = re.compile(r"\s*data:image/(?:png|jpeg|jpg|gif|webp)[;,]", re.IGNORECASE)
_NOT_LOADED = {"link", "iframe", "frame", "object", "embed", "audio", "video", "source", "track", "portal"}
_RESOURCE = {"src", "srcset", "poster", "background", "action", "formaction", "href", "xlink:href"}


def _attributes(raw: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(2) or "").strip("\"'") for m in _ATTR.finditer(raw)}


def _outside(value: str) -> bool:
    """A reference the widget would have to load: anything but a fragment or a data: image."""
    value = value.strip()
    return bool(value) and not value.startswith("#") and not _DATA_IMAGE.match(value)


def _not_loaded(where: str) -> str:
    return (f"the widget loads nothing from outside ({where[:40]}): inline the CSS and script, and give images "
            "as data: PNG, JPEG, GIF or WebP URLs or inline SVG")


def check_widget(html: str) -> Optional[str]:
    """Why Telegram or its sandbox would refuse ``html``, or ``None`` when it is a valid widget."""
    if not html.strip():
        return "the widget's HTML is empty"
    if "\x00" in html:
        return "the HTML contains a NUL character, which Telegram refuses"
    try:
        raw = html.encode("utf-8")
    except UnicodeEncodeError:
        return "the HTML is not valid UTF-8 text"
    if len(raw) > HTML_MAX:
        return f"the HTML is {len(raw)} bytes; a widget has at most {HTML_MAX}"
    packed = len(zlib.compress(raw, 9))
    if packed > COMPRESSED_MAX:
        return (f"the HTML is {packed} bytes compressed; a widget has at most {COMPRESSED_MAX}: draw with SVG and "
                "CSS rather than embedded images, and shorten the code")
    code = [body for attrs, body in _SCRIPT.findall(html)]
    markup = _SCRIPT.sub(lambda m: f"<script{m.group(1)}></script>", html)  # not the tags its code writes
    styles = _STYLE.findall(markup)
    for name, raw_attrs in _TAG.findall(_STYLE.sub("<style></style>", markup)):
        tag, attrs = name.lower(), _attributes(raw_attrs)
        if tag == "script" and "src" in attrs:
            return _not_loaded(f"<script src={attrs['src']}>")
        if tag in _NOT_LOADED:
            return f"a widget has no <{tag}> (it is not in Telegram's widget profile)"
        code.extend(v for k, v in attrs.items() if k.startswith("on"))
        if "style" in attrs:
            styles.append(attrs["style"])
        for key in _RESOURCE & attrs.keys():
            if tag == "a" and key in ("href", "xlink:href"):
                continue  # a link opens on a tap, through openLink: nothing is loaded into the widget
            if _outside(attrs[key]):
                return _not_loaded(f"<{tag} {key}={attrs[key]}>")
    for css in styles:
        for found in _CSS_URL.finditer(css):
            if found.group(0).lower().startswith("@import") or _outside(found.group(2) or ""):
                return _not_loaded(found.group(0))
    for body in code:
        found = _NETWORK_CALL.search(_JS_TEXT.sub(" ", body))
        if found:
            return f"the widget has no network ({found.group(0).rstrip('(').strip()} throws in its sandbox)"
    return None


def check_state(state: Any) -> Optional[str]:
    """Why Telegram would refuse ``state`` as a widget's chat state, or ``None``."""
    if not isinstance(state, str):
        return "state must be a string"
    if "\x00" in state:
        return "state must not contain a NUL character"
    try:
        size = len(state.encode("utf-8"))
    except UnicodeEncodeError:
        return "state must be valid UTF-8 text"
    if size > DATA_MAX:
        return f"state is {size} bytes; a widget's state has at most {DATA_MAX}"
    return None


def widget_event_text(data: str, message_id: Any) -> str:
    return f"[widget] data={quoted(data)} on widget {message_id}"


def _wait_secs(exc: Exception) -> float:
    """The flood wait Telegram asked for, else a short pause before sending again."""
    retry_after = getattr(exc, "_retry_after", None) or getattr(exc, "retry_after", None)
    if retry_after is None:
        return 0.5
    return float(retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else retry_after)


class WidgetsMixin:
    """Widgets and their queries; listed before the harvested mixins."""

    def _widget(self, chat_id: Any, message_id: Any) -> Optional[dict[str, Any]]:
        """``{"thread": ..., "profile": ...}`` when ``message_id`` is one of the bot's widgets in ``chat_id``."""
        return self._store("widgets", _WIDGET_MEMORY).data.get(f"{chat_id}:{message_id}")

    async def send_widget(self, chat_id: str, html: str, metadata: Optional[dict[str, Any]], *,
                          caption: str = "", height: Optional[int] = None, width: Optional[int] = None,
                          state: str = "", profile: str = "") -> Any:
        """``height`` 0 or ``None`` is Telegram's automatic height."""
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        if int(normalize_telegram_chat_id(chat_id)) <= 0:
            return SendResult(success=False, error="widgets work only in the private chat with the bot")
        thread_id = self._metadata_thread_id(metadata)
        routing = self._compute_single_send_routing(chat_id, None, metadata, thread_id)
        if routing is None:
            return SendResult(success=False, error=self._dm_topic_missing_anchor_error())
        reply_to_id, thread_kwargs = routing
        payload: dict[str, Any] = {"chat_id": normalize_telegram_chat_id(chat_id), "html": html,
                                   "allow_queries": True, **{k: v for k, v in thread_kwargs.items() if v is not None},
                                   **self._notification_kwargs(metadata)}
        for key, value in (("caption", caption), ("height", height), ("width", width), ("chat_data", state)):
            if value:
                payload[key] = value
        if reply_to_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_id}
        await self._before_message(chat_id, thread_id)  # the preview's fence
        try:
            sent = await self._bot.do_api_request("sendWidget", api_kwargs=payload)
        except Exception as exc:
            return self._send_failed(exc, _redact_telegram_error_text(exc))
        message_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
        self._reshow_soon(chat_id, thread_id)
        result = SendResult(success=True, message_id=str(message_id) if message_id is not None else None)
        self._remember_bot_messages(chat_id, [result], thread_id)
        try:
            self._store("widgets", _WIDGET_MEMORY).put(f"{chat_id}:{message_id}", {
                "thread": str(thread_id) if thread_id else None, "profile": profile or ""})
        except StoreError:
            logger.error("[%s] a sent widget could not be recorded: its requests will not reach the agent",
                         self.name, exc_info=True)
            result.raw_response = {"unrecorded": True}
        return result

    async def set_widget_state(self, chat_id: str, message_id: str, state: str) -> Any:
        """The widget's chat state (editMessageWidgetData ``chat_data``; "" removes it); the widget redraws
        from it. A state it already has is no change, and no failure (bots/widgets, Changing the state)."""
        from gateway.platforms.base import SendResult

        from .harvested.adapter_module import _redact_telegram_error_text, normalize_telegram_chat_id

        try:
            await self._bot.do_api_request("editMessageWidgetData", api_kwargs={
                "chat_id": normalize_telegram_chat_id(chat_id), "message_id": int(message_id), "chat_data": state})
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return SendResult(success=True, message_id=str(message_id))
            return SendResult(success=False, error=_redact_telegram_error_text(exc))
        return SendResult(success=True, message_id=str(message_id))

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        from telegram import Update
        from telegram.ext import TypeHandler

        # PTB 22.8 does not model widget_query: it arrives in Update.api_kwargs.
        app.add_handler(TypeHandler(Update, self._on_widget_query), group=-7)

    async def _answer_widget_query(self, query_id: str, arrived: float) -> None:
        """Answer within Telegram's 15 s, sending again only while Telegram certainly did not take it."""
        while True:
            try:
                await self._bot.do_api_request("answerWidgetQuery", api_kwargs={"widget_query_id": query_id})
                return
            except Exception as exc:
                wait = _wait_secs(exc)
                if not self._certainly_unsent(exc) or time.monotonic() + wait > arrived + ANSWER_WITHIN:
                    logger.warning("[%s] widget query %s not answered", self.name, query_id, exc_info=True)
                    return
                await asyncio.sleep(wait)

    async def _on_widget_query(self, update: Any, context: Any) -> None:
        query = (getattr(update, "api_kwargs", None) or {}).get("widget_query")
        if not isinstance(query, dict) or not query.get("id"):
            return
        arrived = time.monotonic()
        self._accept_update()
        seen = self.__dict__.setdefault("_hermes_telegram_widget_queries", OrderedDict())
        if str(query["id"]) in seen:
            return  # the same query again: it was answered and passed on
        seen[str(query["id"])] = True
        while len(seen) > _QUERY_MEMORY:
            seen.popitem(last=False)
        await self._answer_widget_query(str(query["id"]), arrived)
        sender, chat, message_id = query.get("from") or {}, query.get("chat") or {}, query.get("message_id")
        data = str(query.get("data") or "")
        if not sender.get("id") or not chat.get("id") or message_id is None or not data:
            return  # an inline message's widget: out of scope (production has widgets in private chats only)
        chat_id = str(chat["id"])
        widget = self._widget(chat_id, message_id)
        if widget is None or str(chat.get("type") or "") != "private":
            return  # not one of this bot's widgets, or not in the private chat
        from telegram import User

        actor = User.de_json(sender, self._bot)
        if not self._actor_admitted(actor, chat_id, widget.get("thread")):
            return
        event = self._action_event(chat_id, widget.get("thread"), actor, widget_event_text(data, message_id),
                                   f"wq{update.update_id}", str(message_id))
        if (event.source.profile or "") != (widget.get("profile") or ""):
            return  # a query routed to another profile than the widget's
        await self.handle_message(event)
