"""Hermes' Bot API stand-in, extended with the Bot API 10.x methods this plugin calls.

Hermes' stand-in (hermes-agent@28ef6e4 tests/fakes/platforms/telegram_standin.py) answers only what the
bundled adapter calls and returns 404 for the rest; the adapter reads a 404 from ``sendRichMessage`` as
"no rich messages here" and stops trying for the process. The methods added here check what the Bot API
documents and nothing it does not: they hold no copy of this plugin's own rules.

- ``sendRichMessage`` (core.telegram.org/bots/api/methods/sendrichmessage.md): ``rich_message`` is an
  InputRichMessage with exactly one of ``markdown``, ``html`` or ``blocks``
  (types/inputrichmessage.md); its text is at most 32768 characters and a table at most 20 columns
  (rich-messages.md, "Rich Message Limits"). The block count is Telegram's own and not modelled: a test
  that needs a refusal injects it with ``fail``.
- ``editMessageText`` with ``rich_message`` instead of ``text`` (methods/editmessagetext.md).
- ``sendRichMessageDraft`` (methods/sendrichmessagedraft.md): private chats only, a non-zero ``draft_id``,
  an InputRichMessage; the latest frame per draft is kept in ``drafts``. ``stop_generation`` is the user
  pressing Stop on a draft: the ``stopped_message_generation`` update (types/messagegenerationstopped.md).
- Telegram Business (core.telegram.org/bots/api, Update, BusinessConnection, BusinessMessagesDeleted,
  getBusinessConnection): ``business_connect``, ``business_message`` and ``business_delete`` are the owner
  connecting the bot and messages in the owner's private chats; ``getBusinessConnection`` answers a connection
  made here and refuses any other.
"""

import hashlib
import json
import re
import time
import zlib
from typing import Any

from aiohttp import web

from tests.fakes.platforms._standin import Visible, decode_value
from tests.fakes.platforms.telegram_standin import BotApiError, TelegramStandin

RICH_MAX_CHARS = 32768
RICH_MAX_COLUMNS = 20


def _check_rich(p: dict[str, Any]) -> str:
    rich = p.get("rich_message")
    if not isinstance(rich, dict):
        raise BotApiError("Bad Request: rich_message must be an InputRichMessage object")
    given = [k for k in ("markdown", "html", "blocks") if rich.get(k)]
    if len(given) != 1:
        raise BotApiError("Bad Request: exactly one of markdown, html or blocks must be specified")
    if given != ["markdown"]:
        return str(rich[given[0]])
    text = str(rich["markdown"])
    if len(text) > RICH_MAX_CHARS:
        raise BotApiError("Bad Request: rich message is too long")
    fence = ""  # the opening run of the fenced code block the line is in
    lines = text.splitlines()
    for i, line in enumerate(lines):
        # CommonMark: a fence closes with the same character, at least as long, and nothing after it.
        run = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if run and not fence:
            fence = run.group(1)
        elif run and run.group(1)[0] == fence[0] and len(run.group(1)) >= len(fence) and not run.group(2).strip():
            fence = ""
        # A table is a header row followed by its separator row; anything else is not one.
        elif (not fence and i > 0 and "|" in lines[i - 1]
              and re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$", line)):
            if len(line.strip().strip("|").split("|")) > RICH_MAX_COLUMNS:
                raise BotApiError("Bad Request: too many table columns")
    return text


class RichTelegramStandin(TelegramStandin):
    """``TelegramStandin`` that also answers ``sendRichMessage`` and rich ``editMessageText``."""

    def _m_sendRichMessage(self, p: dict[str, Any]) -> dict[str, Any]:
        text = _check_rich(p)
        if "reply_to_message_id" in p:
            # sendRichMessage takes reply_parameters; a reply_to_message_id would be silently ignored.
            raise BotApiError("Bad Request: unexpected parameter reply_to_message_id")
        msg = self._bot_message(p, _kind="rich")
        mid = str(msg["message_id"])
        with self._lock:
            self._visible[(str(p["chat_id"]), mid)] = Visible(mid, text, extra={"kind": "rich"})
        msg["rich_message"] = {"blocks": []}  # a RichMessage, as types/richmessage.md shapes it
        return msg

    def _m_sendRichMessageDraft(self, p: dict[str, Any]) -> bool:
        if int(p.get("chat_id") or 0) <= 0:
            raise BotApiError("Bad Request: drafts can be sent only to private chats")
        if not int(p.get("draft_id") or 0):
            raise BotApiError("Bad Request: draft_id must be non-zero")
        text = _check_rich(p)
        with self._lock:
            self.drafts[(str(p["chat_id"]), int(p["draft_id"]))] = text
        return True

    def buttons(self, chat_id: Any) -> list[dict[str, Any]]:
        """Hermes' ``buttons``, and the inline buttons of rich messages too (sendrichmessage.md, reply_markup)."""
        out = super().buttons(chat_id)
        for call in self.calls_of("sendRichMessage"):
            if str(call.params.get("chat_id")) != str(chat_id) or call.faulted:
                continue
            markup = call.params.get("reply_markup") or {}
            mid = (call.response or {}).get("message_id")
            for row in (markup.get("inline_keyboard") or []) if isinstance(markup, dict) else []:
                for btn in row:
                    out.append({**btn, "message_id": mid})
        return out

    def stop_generation(self, chat_id: Any, draft_id: int, thread_id: Any = None) -> dict[str, Any]:
        """The user presses Stop on the draft ``draft_id``."""
        stopped: dict[str, Any] = {"chat": self._chat(chat_id), "draft_id": int(draft_id)}
        if thread_id:
            stopped["message_thread_id"] = int(thread_id)
        update = {"update_id": next(self._update_ids), "stopped_message_generation": stopped}
        self._push(update)
        return update

    def _m_editMessageText(self, p: dict[str, Any]) -> dict[str, Any]:
        if "rich_message" not in p:
            return super()._m_editMessageText(p)
        text = _check_rich(p)
        chat_id, mid = str(p["chat_id"]), str(p["message_id"])
        with self._lock:
            vis = self._visible.get((chat_id, mid))
            if vis is not None:
                if vis.text == text:
                    raise BotApiError("Bad Request: message is not modified")
                vis.text = text
                vis.edits += 1
                vis.extra["kind"] = "rich"
        return {"message_id": int(mid), "date": 0, "chat": self._chat(chat_id), "from": self._bot_user(),
                "rich_message": {"blocks": []}}


# --- media (slice 2) ------------------------------------------------------------------------------------
# sendPhoto/sendVideo/sendAudio/sendVoice/sendAnimation/sendDocument and sendMediaGroup, checked against
# api.md "Sending files" (multipart: 10 MB for photos, 50 MB for other files) and methods/sendmediagroup.md
# (2-10 items; documents and audio only with their own kind). An uploaded part is recorded by the base
# stand-in as {"filename", "size"}.

PHOTO_UPLOAD_MAX = 10 * 1024 * 1024
FILE_UPLOAD_MAX = 50 * 1024 * 1024
CAPTION_MAX = 1024


def _upload(p: dict[str, Any], field: str, limit: int) -> dict[str, Any]:
    part = p.get(field)
    if not isinstance(part, dict) or "size" not in part:
        raise BotApiError(f"Bad Request: there is no {field} in the request")
    if part["size"] > limit:
        raise BotApiError("Request Entity Too Large", code=413)
    if len(str(p.get("caption") or "")) > CAPTION_MAX:
        raise BotApiError("Bad Request: message caption is too long")
    return part


class MediaTelegramStandin(RichTelegramStandin):
    """``RichTelegramStandin`` that also answers the media methods the adapter uses.

    An uploaded file is recorded as ``{"filename", "size", "sha256"}``: Hermes' stand-in keeps only the
    size, which cannot tell the file's own bytes from other bytes of the same length."""

    async def _params(self, request: web.Request) -> dict[str, Any]:
        if not (request.content_type or "").startswith("multipart/"):
            return await super()._params(request)
        params: dict[str, Any] = dict(request.query)
        reader = await request.multipart()
        async for part in reader:
            if part.filename:
                data = await part.read()
                params[part.name] = {"filename": part.filename, "size": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()}
            else:
                params[part.name] = decode_value(await part.text())
        return params

    def _media(self, p: dict[str, Any], kind: str, field: str, limit: int) -> dict[str, Any]:
        part = _upload(p, field, limit)
        # The returned object has the required fields of its Bot API type (types/photosize.md, video.md,
        # audio.md, voice.md, animation.md, document.md); PTB refuses a reply without them.
        base = {"file_id": f"out-{kind}", "file_unique_id": f"o{kind}", "file_size": part["size"]}
        shape = {
            "photo": [{**base, "width": 1, "height": 1}],
            "video": {**base, "width": 1, "height": 1, "duration": 1},
            "animation": {**base, "width": 1, "height": 1, "duration": 1},
            "audio": {**base, "duration": 1, "file_name": part.get("filename")},
            "voice": {**base, "duration": 1},
            "document": {**base, "file_name": part.get("filename")},
        }[field]
        return self._bot_message(p, caption=p.get("caption", ""), _kind=kind, **{field: shape})

    def _m_sendPhoto(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._media(p, "photo", "photo", PHOTO_UPLOAD_MAX)

    def _m_sendDocument(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._media(p, "document", "document", FILE_UPLOAD_MAX)

    def _m_sendVideo(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._media(p, "video", "video", FILE_UPLOAD_MAX)

    def _m_sendAudio(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._media(p, "audio", "audio", FILE_UPLOAD_MAX)

    def _m_sendVoice(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._media(p, "voice", "voice", FILE_UPLOAD_MAX)

    def _m_sendAnimation(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._media(p, "animation", "animation", FILE_UPLOAD_MAX)

    # Polls, reactions, stickers (slice 4): sendpoll.md, types/poll.md, pollanswer.md, messagereactionupdated.md,
    # sendsticker.md, getstickerset.md.

    def _m_sendPoll(self, p: dict[str, Any]) -> dict[str, Any]:
        question = str(p.get("question") or "")
        options = p.get("options")
        if not 1 <= len(question) <= 300:
            raise BotApiError("Bad Request: poll question must be 1-300 characters")
        if not isinstance(options, list) or not 1 <= len(options) <= 12:
            raise BotApiError("Bad Request: poll must have 1-12 options")
        texts = [o.get("text") if isinstance(o, dict) else str(o) for o in options]
        quiz = p.get("type") == "quiz"
        if quiz and not p.get("correct_option_ids"):
            raise BotApiError("Bad Request: correct_option_ids is required for quiz polls")
        self._poll_ids = getattr(self, "_poll_ids", 0) + 1
        poll = {"id": f"poll-{self._poll_ids}", "question": question, "total_voter_count": 0, "is_closed": False,
                "is_anonymous": p.get("is_anonymous", True) not in (False, "false", "False"),
                "type": "quiz" if quiz else "regular",
                "allows_multiple_answers": bool(p.get("allows_multiple_answers")),
                "allows_revoting": True, "members_only": False,  # required fields of types/poll.md
                # persistent_id is required since Bot API 9.6 (PTB 22.8 refuses a poll without it)
                "options": [{"text": t, "voter_count": 0, "persistent_id": f"opt-{i}"} for i, t in enumerate(texts)]}
        if quiz:
            poll["correct_option_ids"] = p["correct_option_ids"]
        return self._bot_message(p, _kind="poll", poll=poll)

    def poll_answer(self, user_id: int, poll_id: str, option_ids: list[int]) -> dict[str, Any]:
        update = {"update_id": next(self._update_ids),
                  "poll_answer": {"poll_id": poll_id, "user": self._user(user_id), "option_ids": option_ids,
                                  "option_persistent_ids": [f"opt-{i}" for i in option_ids]}}
        self._push(update)
        return update

    def react(self, user_id: int, chat_id: Any, message_id: int, emoji: str) -> dict[str, Any]:
        """The user sets a reaction on a message."""
        update = {"update_id": next(self._update_ids), "message_reaction": {
            "chat": self._chat(chat_id), "message_id": int(message_id), "user": self._user(user_id),
            "date": 0, "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": emoji}]}}
        self._push(update)
        return update

    sticker_sets: dict[str, list[dict[str, Any]]] = {}

    def _sticker(self, file_id: str, emoji: str, set_name: str = "") -> dict[str, Any]:
        return {"file_id": file_id, "file_unique_id": f"u-{file_id}", "type": "regular", "width": 512,
                "height": 512, "is_animated": False, "is_video": False, "emoji": emoji,
                **({"set_name": set_name} if set_name else {})}

    def _m_getStickerSet(self, p: dict[str, Any]) -> dict[str, Any]:
        name = str(p.get("name") or "")
        if name not in self.sticker_sets:
            raise BotApiError("Bad Request: STICKERSET_INVALID")
        return {"name": name, "title": f"{name} title", "sticker_type": "regular",
                "stickers": [self._sticker(s["file_id"], s["emoji"], name) for s in self.sticker_sets[name]]}

    def _m_sendSticker(self, p: dict[str, Any]) -> dict[str, Any]:
        sticker = p.get("sticker")
        if not isinstance(sticker, str) or not sticker:
            raise BotApiError("Bad Request: sticker must be a file_id")
        return self._bot_message(p, _kind="sticker", sticker=self._sticker(sticker, ""))

    # Widgets (slice 5): sendwidget.md, answerwidgetquery.md, editmessagewidgetdata.md, types/widgetquery.md, and
    # the limits as bots/widgets states them (private chats only in production; 32768 bytes, 4096 after zlib 9).

    def _m_sendWidget(self, p: dict[str, Any]) -> dict[str, Any]:
        if int(p.get("chat_id") or 0) <= 0:
            raise BotApiError("Bad Request: widgets can be sent only to private chats")
        html = str(p.get("html") or "")
        if not html or "\x00" in html:
            raise BotApiError("Bad Request: INPUT_MEDIA_WIDGET_HTML_INVALID")
        raw = html.encode("utf-8")
        if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
            raise BotApiError("Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE")
        for key in ("chat_data", "public_data"):
            self._check_widget_data(p, key)
        widget = {"html": html, **{k: p[k] for k in ("height", "width", "chat_data", "public_data") if k in p}}
        if "height" in p and not 50 <= int(p["height"]) <= 600:
            raise BotApiError("Bad Request: widget height must be 50-600")
        msg = self._bot_message(p, _kind="widget", caption=p.get("caption", ""), widget=widget)
        with self._lock:
            self._visible[(str(p["chat_id"]), str(msg["message_id"]))].extra["chat_data"] = p.get("chat_data", "")
        return msg

    @staticmethod
    def _check_widget_data(p: dict[str, Any], key: str) -> None:
        """bots/widgets, Limits: a state is UTF-8 text without NUL, at most 1024 bytes."""
        value = str(p.get(key) or "")
        if "\x00" in value:
            raise BotApiError(f"Bad Request: {key} must not contain NUL")
        if len(value.encode("utf-8")) > 1024:
            raise BotApiError(f"Bad Request: {key} is too long")

    def _m_answerWidgetQuery(self, p: dict[str, Any]) -> bool:
        if len(str(p.get("data") or "").encode("utf-8")) > 1024:
            raise BotApiError("Bad Request: data is too long")
        return True

    def _m_editMessageWidgetData(self, p: dict[str, Any]) -> bool:
        with self._lock:
            vis = self._visible.get((str(p["chat_id"]), str(p["message_id"])))
            if vis is None or vis.extra.get("kind") != "widget":
                raise BotApiError("Bad Request: message is not a widget")
            self._check_widget_data(p, "chat_data")
            if vis.extra.get("chat_data", "") == p.get("chat_data", ""):
                raise BotApiError("Bad Request: message is not modified")  # as Telegram says (bots/widgets)
            vis.extra["chat_data"] = p.get("chat_data")
        return True

    def widget_query(self, user_id: int, chat_id: Any, message_id: int, data: str) -> dict[str, Any]:
        """The widget in ``message_id`` calls TelegramWidget.request(data)."""
        self._widget_queries = getattr(self, "_widget_queries", 0) + 1
        update = {"update_id": next(self._update_ids), "widget_query": {
            "id": f"wq-{self._widget_queries}", "from": self._user(user_id), "chat": self._chat(chat_id),
            "message_id": int(message_id), "chat_instance": "ci-1", "type": {"type": "data"}, "data": data}}
        self._push(update)
        return update

    # Slice 6: pinchatmessage.md, unpinchatmessage.md, and replies with a quote (types/replyparameters.md: the
    # quote must be an exact part of the message replied to) and an effect (private chats only).

    def _m_pinChatMessage(self, p: dict[str, Any]) -> bool:
        self._known_message(p)
        self.pinned.add((str(p["chat_id"]), str(p["message_id"])))
        return True

    def _m_unpinChatMessage(self, p: dict[str, Any]) -> bool:
        self.pinned.discard((str(p["chat_id"]), str(p.get("message_id"))))
        return True

    @property
    def pinned(self) -> set:
        return self.__dict__.setdefault("_pinned", set())

    def _known_message(self, p: dict[str, Any]) -> None:
        key = (str(p["chat_id"]), str(p["message_id"]))
        with self._lock:
            known = key in self._visible or key in self.__dict__.get("_inbound_texts", {})
        if not known:
            raise BotApiError("Bad Request: message to pin not found")

    def _inbound(self, chat: dict[str, Any], user_id: int, **fields: Any) -> dict[str, Any]:
        update = super()._inbound(chat, user_id, **fields)
        message = update["message"]
        self.__dict__.setdefault("_inbound_texts", {})[(str(chat["id"]), str(message["message_id"]))] = (
            message.get("text") or message.get("caption") or "")
        return update

    def _check_reply(self, p: dict[str, Any]) -> None:
        reply = p.get("reply_parameters") or {}
        if isinstance(reply, str):
            reply = json.loads(reply)
        if p.get("message_effect_id") and int(p.get("chat_id") or 0) <= 0:
            raise BotApiError("Bad Request: message effects are available only in private chats")
        quote = reply.get("quote")
        if not quote:
            return
        key = (str(p["chat_id"]), str(reply.get("message_id")))
        with self._lock:
            vis = self._visible.get(key)
            text = vis.text if vis is not None else self.__dict__.get("_inbound_texts", {}).get(key)
        if text is None or quote not in text:
            raise BotApiError("Bad Request: QUOTE_TEXT_INVALID")

    def _m_sendMessage(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_reply(p)
        return self._with_effect(p, super()._m_sendMessage(p))

    def _m_sendRichMessage(self, p: dict[str, Any]) -> dict[str, Any]:
        self._check_reply(p)
        return self._with_effect(p, super()._m_sendRichMessage(p))

    @staticmethod
    def _with_effect(p: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        """types/message.md: the sent Message carries the effect it was sent with (``effect_id``)."""
        if p.get("message_effect_id"):
            msg["effect_id"] = str(p["message_effect_id"])
        return msg

    # --- Telegram Business ---------------------------------------------------------------------------------

    def business_connect(self, owner_id: int, connection_id: str, *, enabled: bool = True) -> dict[str, Any]:
        """The owner connects the bot to their account (or ends the connection): a ``business_connection``."""
        connection = {"id": connection_id, "user": self._user(owner_id, "Owner"), "user_chat_id": int(owner_id),
                      "date": int(time.time()), "is_enabled": enabled, "rights": {}}
        self.__dict__.setdefault("_business_connections", {})[connection_id] = connection
        update = {"update_id": next(self._update_ids), "business_connection": connection}
        self._push(update)
        return update

    def business_message(self, connection_id: str, contact_id: int, sender_id: int, text: str, *,
                         edit_of: int | None = None) -> dict[str, Any]:
        """A message in the owner's private chat with ``contact_id``, written by ``sender_id`` (the owner or the
        contact); ``edit_of`` makes it the new version of that message."""
        message = {"message_id": edit_of or next(self._inbound_ids), "date": int(time.time()),
                   "business_connection_id": connection_id, "text": text,
                   "chat": {"id": int(contact_id), "type": "private", "first_name": "Contact"},
                   "from": self._user(sender_id, "Owner" if sender_id != contact_id else "Contact")}
        if edit_of:
            message["edit_date"] = int(time.time())
        update = {"update_id": next(self._update_ids),
                  "edited_business_message" if edit_of else "business_message": message}
        self._push(update)
        return update

    def business_delete(self, connection_id: str, contact_id: int, message_ids: list[int]) -> dict[str, Any]:
        update = {"update_id": next(self._update_ids), "deleted_business_messages": {
            "business_connection_id": connection_id, "message_ids": [int(m) for m in message_ids],
            "chat": {"id": int(contact_id), "type": "private", "first_name": "Contact"}}}
        self._push(update)
        return update

    def _m_getBusinessConnection(self, p: dict[str, Any]) -> dict[str, Any]:
        connection = self.__dict__.get("_business_connections", {}).get(str(p.get("business_connection_id")))
        if connection is None:
            raise BotApiError("Bad Request: business connection not found")
        return connection

    def _m_sendMediaGroup(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        media = p.get("media")
        if not isinstance(media, list) or not 2 <= len(media) <= 10:
            raise BotApiError("Bad Request: media group must contain 2-10 items")
        kinds = {item.get("type") for item in media}
        if len(kinds) > 1 and kinds & {"document", "audio"}:
            raise BotApiError("Bad Request: documents and audio can be grouped only with their own kind")
        out = []
        for item in media:
            ref = str(item.get("media", ""))
            if ref.startswith("attach://"):
                _upload({"part": p.get(ref[len("attach://"):]), "caption": item.get("caption")}, "part",
                        PHOTO_UPLOAD_MAX if item.get("type") == "photo" else FILE_UPLOAD_MAX)
            out.append(self._bot_message(p, caption=item.get("caption", ""), _kind=f"album-{item.get('type')}"))
        return out
