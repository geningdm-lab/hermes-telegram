"""Hermes' Bot API stand-in, extended with the Bot API 10.x methods this plugin calls.

Hermes' stand-in (hermes-agent@c419cf7 tests/fakes/platforms/telegram_standin.py) answers only what the
bundled adapter calls and returns 404 for the rest; the adapter reads a 404 from ``sendRichMessage`` as
"no rich messages here" and stops trying for the process. The methods added here check what the Bot API
documents and nothing it does not: they hold no copy of this plugin's own rules.

- ``sendRichMessage`` (core.telegram.org/bots/api/methods/sendrichmessage.md): ``rich_message`` is an
  InputRichMessage with exactly one of ``markdown``, ``html`` or ``blocks``
  (types/inputrichmessage.md); its text is at most 32768 characters and a table at most 20 columns
  (rich-messages.md, "Rich Message Limits"). The block count is Telegram's own and not modelled: a test
  that needs a refusal injects it with ``fail``.
- ``editMessageText`` with ``rich_message`` instead of ``text`` (methods/editmessagetext.md).
"""

import re
from typing import Any

from tests.fakes.platforms._standin import Visible
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
    in_fence = False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
        # A table is a header row followed by its separator row; anything else is not one.
        elif (not in_fence and i > 0 and "|" in lines[i - 1]
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
    """``RichTelegramStandin`` that also answers the media methods the adapter uses."""

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
