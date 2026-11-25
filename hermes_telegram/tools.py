"""The agent's ``telegram`` toolset (docs/design.md, Agent interface, Tools).

Slice 2 adds ``telegram_send`` for files and slice 3 ``telegram_status`` for the preview's status line; later
slices add text, buttons, widgets, polls, reactions and stickers to the same toolset. Every tool acts in the turn's own chat through ``turn``, which carries the
three guards of the design: no recipient argument, a turn started by the user's own Telegram message, a cap
per turn.

``telegram_send`` resolves each path as Hermes resolves a ``MEDIA:`` path (the host, Docker's mounts, the
active remote sandbox), plans the messages in the order given (consecutive photos as albums), reserves the
whole plan against the turn's cap before sending anything, and reports each file: its message id, or why
it did not arrive. Files that did not arrive are named in the chat in one message.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .delivery import Receipt, album_chunks, collecting_notices, combined_notice
from .turn import AdmittedTurn, Turn, TurnRefused, as_tool_send, live_adapter, on_gateway_loop

TOOLSET = "telegram"
MAX_FILES = 20
MAX_BUTTONS = 12
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp"}
_VOICE_EXTS = {".ogg", ".opus"}
MAYBE_SHOWN = "Telegram may have shown it anyway: do not send it again."


def _ambiguous(result: Any) -> bool:
    raw = getattr(result, "raw_response", None)
    return isinstance(raw, dict) and bool(raw.get("ambiguous_delivery"))


def _available() -> bool:
    """Offer the toolset only to a Telegram chat turn, so other surfaces do not see tools they cannot use.

    Uncached (``no_cache_check_fn``): the answer depends on the turn building the tool list."""
    from hermes_cli.plugin_session import session_surface

    return session_surface() == "telegram"


SEND_SCHEMA: dict[str, Any] = {
    "name": "telegram_send",
    "description": (
        "Send a message, buttons or files to the user in this Telegram chat now. With text, a message comes "
        "first (Markdown, as in your answers); with buttons, it carries them. A button with a value is a "
        "choice: when the user taps it you receive a new message "
        "[button] \"<label>\" value=\"<value>\" on message <id>; with single_use (the default for choices) "
        "only the first tap of the set reaches you, later taps show \"Already answered\". A button with url "
        "opens a link; with copy, copies text. Files follow, in the order given. Each file arrives as its kind: "
        "photo, video, audio, voice note (.ogg/.opus) or document; consecutive photos arrive as one album, more "
        "than 10 as consecutive albums. The text can reply to the user's current message or one of yours "
        "(reply_to), quoting an exact part of it (quote), set link_preview options, and carry a message effect "
        "(effect: one of 🔥 👍 👎 ❤️ 🎉 💩, or an effect id; private chats). "
        "With as_files=true every file arrives as a downloadable document with "
        "its name and bytes unchanged, never shown or played: use it when the user asks for a photo, video or "
        "recording \"as a file\". A file that cannot be delivered (missing, over Telegram's 50 MB limit for "
        "bots, refused by Telegram) is named to the user with the reason, and listed under not_delivered in "
        "the result. At most 10 extra messages per turn: an album counts as one."),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "A message to send first (Markdown), one message "
                                                       "long: a longer text belongs in your answer."},
            "buttons": {
                "type": "array", "maxItems": MAX_BUTTONS,
                "description": "Buttons under the text, one per row. Each has a label and one of value (a "
                               "choice that comes back to you), url or copy; style is optional.",
                "items": {"type": "object", "properties": {
                    "label": {"type": "string", "maxLength": 64},
                    "value": {"type": "string", "maxLength": 256},
                    "url": {"type": "string"},
                    "copy": {"type": "string", "maxLength": 256},
                    "style": {"type": "string", "enum": ["primary", "success", "danger"]},
                }, "required": ["label"]},
            },
            "single_use": {"type": "boolean",
                           "description": "Only the first tap of these choices reaches you (default true)."},
            "reply_to": {"type": "string", "description": "The text replies to this message: the user's current "
                                                          "message (the default) or one of yours here."},
            "quote": {"type": "string", "maxLength": 1024,
                      "description": "An exact part of the message replied to, shown as a quote."},
            "link_preview": {"type": "object", "description": "Link preview of the text (not for rich text).",
                             "properties": {"disabled": {"type": "boolean"}, "url": {"type": "string"},
                                            "small": {"type": "boolean"}, "large": {"type": "boolean"},
                                            "above": {"type": "boolean"}}},
            "effect": {"type": "string", "description": "A message effect: 🔥 👍 👎 ❤️ 🎉 💩, or an effect id."},
            "files": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_FILES,
                      "description": "Absolute paths of the files to send, in order."},
            "as_files": {"type": "boolean",
                         "description": "Send every file as a document instead of as a photo, video or audio."},
            "caption": {"type": "string", "description": "Optional caption for the first file (1024 characters)."},
        },
    },
}


@dataclass(frozen=True)
class _Step:
    """One send of the plan: ``kind`` is photos (one or more albums), document, video or voice."""

    kind: str
    files: tuple[tuple[int, str, str], ...]  # (position in the call, path as given, path resolved)

    @property
    def messages(self) -> int:
        return len(album_chunks(list(self.files))) if self.kind == "photos" else 1


def _plan(files: list[tuple[int, str, str]], as_files: bool, is_audio: Callable[[str], bool]) -> list[_Step]:
    """The sends for ``files`` in their order; only consecutive photos share an album."""
    steps: list[_Step] = []
    for item in files:
        ext = os.path.splitext(item[2])[1].lower()
        if as_files:
            kind = "document"
        elif ext in _IMAGE_EXTS:
            kind = "photos"
        elif ext in _VIDEO_EXTS:
            kind = "video"
        elif is_audio(ext):
            kind = "voice"
        else:
            kind = "document"
        if kind == "photos" and steps and steps[-1].kind == "photos":
            steps[-1] = _Step("photos", steps[-1].files + (item,))
        else:
            steps.append(_Step(kind, (item,)))
    return steps


def _messages_needed(paths: list[str], as_files: bool) -> int:
    """Messages a call sends when every file arrives (audio or not, a non-photo is one message)."""
    return sum(step.messages for step in _plan([(i, p, p) for i, p in enumerate(paths)], as_files, lambda _e: False))


def _parse_send(args: dict[str, Any]) -> tuple[list[str], bool, str]:
    files = args.get("files")
    if files is None and (args.get("text") or args.get("buttons")):
        files = []
    elif not isinstance(files, list) or not files or not all(isinstance(f, str) and f.strip() for f in files):
        raise TurnRefused("files must be a non-empty list of absolute paths (or send text).")
    if len(files) > MAX_FILES:
        raise TurnRefused(f"At most {MAX_FILES} files per call.")
    caption = args.get("caption") or ""
    if not isinstance(caption, str):
        raise TurnRefused("caption must be a string.")
    return [f.strip() for f in files], bool(args.get("as_files", False)), caption[:1024]


def _parse_message(args: dict[str, Any]) -> tuple[str, list[dict[str, str]], bool]:
    """The message part of a call: ``(text, buttons, single_use)``; no text means no message."""
    text = args.get("text") or ""
    buttons = args.get("buttons") or []
    if not isinstance(text, str):
        raise TurnRefused("text must be a string.")
    if not isinstance(buttons, list) or len(buttons) > MAX_BUTTONS:
        raise TurnRefused(f"buttons must be a list of at most {MAX_BUTTONS} buttons.")
    if buttons and not text.strip():
        raise TurnRefused("Buttons need a text to go under.")
    parsed = []
    for raw in buttons:
        if not isinstance(raw, dict) or not isinstance(raw.get("label"), str) or not raw["label"].strip():
            raise TurnRefused("Each button needs a label.")
        kinds = [k for k in ("value", "url", "copy") if raw.get(k) not in (None, "")]
        if len(kinds) > 1:
            raise TurnRefused(f"A button has one of value, url or copy, not {' and '.join(kinds)}.")
        button = {"label": raw["label"].strip()[:64]}
        kind = kinds[0] if kinds else "value"
        target = str(raw.get(kind) or button["label"])
        if kind == "url" and not target.startswith(("https://", "http://", "tg://")):
            raise TurnRefused("A button url must start with https://, http:// or tg://.")
        button[kind] = target[:256] if kind != "url" else target
        if raw.get("style") in ("primary", "success", "danger"):
            button["style"] = raw["style"]
        parsed.append(button)
    return text.strip(), parsed, bool(args.get("single_use", True))


def _parse_extras(args: dict[str, Any]) -> dict[str, Any]:
    """What the call's text carries besides its buttons: a reply and quote, link preview options, an effect."""
    from .messages import effect_id

    extras: dict[str, Any] = {}
    reply_to, quote = args.get("reply_to"), args.get("quote")
    if reply_to not in (None, ""):
        if not str(reply_to).isdigit():
            raise TurnRefused("reply_to must be a message id.")
        extras["reply_to"] = str(reply_to)
    if quote not in (None, ""):
        if not isinstance(quote, str) or len(quote) > 1024:
            raise TurnRefused("quote must be an exact part of the message replied to, at most 1024 characters.")
        extras["quote"] = quote
    preview = args.get("link_preview")
    if preview not in (None, {}):
        if not isinstance(preview, dict) or not set(preview) <= {"disabled", "url", "small", "large", "above"}:
            raise TurnRefused("link_preview takes disabled, url, small, large and above.")
        if preview.get("url") is not None and not str(preview["url"]).startswith(("https://", "http://")):
            raise TurnRefused("link_preview url must start with https:// or http://.")
        extras["link_preview"] = preview
    if args.get("effect") not in (None, ""):
        effect = effect_id(str(args["effect"]))
        if effect is None:
            raise TurnRefused("effect must be one of 🔥 👍 👎 ❤️ 🎉 💩, or an effect id.")
        extras["effect"] = effect
    if extras and not (args.get("text") or "").strip():
        raise TurnRefused("reply_to, quote, link_preview and effect apply to a text: give text.")
    return extras


def _quote(admitted: AdmittedTurn, turn: Turn, extras: dict[str, Any]) -> dict[str, Any]:
    """``reply_to`` and the ReplyParameters quote fields for a quote of the user's current message, the only
    message whose text and formatting the plugin holds; refused before anything is reserved."""
    from .messages import quote_parts

    current = str(turn.inbound_message_id)
    if extras.get("reply_to", current) != current:
        raise TurnRefused("A quote can be of the user's current message only: reply to your own message "
                          "without quote.")
    if not current.isdigit() or admitted.text is None:
        raise TurnRefused("This turn has no message text of the user's to quote (it started with a tap, a vote, "
                          "a reaction or a widget request): reply without quote.")
    parts = quote_parts(admitted.text, admitted.entities, extras["quote"])
    if parts is None:
        raise TurnRefused("quote must be an exact part of the user's message.")
    return {"reply_to": current, "quote": parts}


def _own_target(adapter: Any, turn: Turn, message_id: str, *, bots_only: bool = False) -> Optional[dict[str, Any]]:
    """The bot's record of ``message_id`` when it is the bot's, in this conversation; ``None`` for the user's
    current message (unless ``bots_only``); else ``TurnRefused``. Telegram's methods take only the chat and the
    message: a message of another topic would be acted on."""
    if not bots_only and message_id == str(turn.inbound_message_id):
        return None
    mine = adapter._bot_message(turn.chat_id, message_id)
    if (mine is None or (mine.get("thread") or None) != (turn.thread_id or None)
            or mine.get("bot") != adapter._bot_identity() or (mine.get("profile") or "") != (turn.profile or "")):
        raise TurnRefused("message_id must be " + ("one of your messages" if bots_only
                          else "the user's current message or one of your messages") + " in this conversation.")
    return mine


def _resolve(paths: list[str], session_key: str) -> list[tuple[str, str | None, str]]:
    """``(path, resolved path or None, reason)`` per path, by Hermes' own MEDIA resolution. Blocking (a
    remote sandbox fetch); run off any loop, in the tool's context, which carries the turn's profile."""
    from gateway.platforms.base import BasePlatformAdapter

    out: list[tuple[str, str | None, str]] = []
    for path in paths:
        dropped: list[dict[str, str]] = []
        kept = BasePlatformAdapter.filter_media_delivery_paths([(path, False)], session_key=session_key,
                                                               dropped=dropped)
        if kept:
            out.append((path, kept[0][0], ""))
            continue
        why = dropped[0]["reason"] if dropped else ""
        out.append((path, None, "Hermes' delivery policy does not allow sending it" if "policy" in why
                    else "the file does not exist on the Hermes host"))
    return out


async def _admit(adapter: Any, turn: Turn) -> None:
    adapter.admitted_turn(turn)


async def _deliver(adapter: Any, turn: Turn, resolved: list[tuple[str, str | None, str]], as_files: bool,
                   caption: str, message: tuple[str, list[dict[str, str]], bool] = ("", [], True),
                   extras: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Send the plan on the gateway loop; nothing is sent when the turn's cap cannot hold all of it."""
    from gateway.platforms.base import should_send_media_as_audio

    admitted: AdmittedTurn = adapter.admitted_turn(turn)
    extras = dict(extras or {})
    if "quote" in extras:
        extras.update(_quote(admitted, turn, extras))
    if "reply_to" in extras:
        _own_target(adapter, turn, extras["reply_to"])
    deliverable = [(i, path, real) for i, (path, real, _) in enumerate(resolved) if real]
    steps = _plan(deliverable, as_files,
                  lambda ext: should_send_media_as_audio(adapter.platform, ext, is_voice=ext in _VOICE_EXTS))
    text, buttons, single_use = message
    # One more: the notice of what does not arrive, when files go. The text is one message (send_single).
    admitted.take(sum(step.messages for step in steps) + (1 if resolved else 0) + (1 if text else 0))
    metadata = admitted.metadata
    out: dict[str, Any] = {}
    if text:
        out["message"] = await _send_text(adapter, turn, admitted, text, buttons, single_use, extras)
        if not out["message"]["sent"] and not resolved:
            return out
    if not resolved:
        return out
    receipts: dict[int, Receipt] = {}
    with collecting_notices() as notices:
        for i, (path, real, reason) in enumerate(resolved):
            if not real:
                receipts[i] = Receipt(path, reason=reason)
                notices.append(adapter._not_delivered_text(os.path.basename(path) or path, reason))
        first_caption: str | None = caption or None
        for step in steps:
            receipts.update(await _send_step(adapter, turn.chat_id, step, first_caption, metadata, admitted))
            first_caption = None
    if notices:
        await adapter.send_notice(turn.chat_id, combined_notice(notices), metadata,
                                  paths=[r.file for r in receipts.values() if not r.sent])
    else:
        admitted.give_back(1)
    ordered = [receipts[i] for i in sorted(receipts)]
    return {**out, "sent": [{"file": r.file, "message_id": r.message_id} for r in ordered if r.sent],
            "not_delivered": [{"file": r.file, "reason": r.reason} for r in ordered if not r.sent]}


async def _send_text(adapter: Any, turn: Turn, admitted: AdmittedTurn, text: str, buttons: list[dict[str, str]],
                     single_use: bool, extras: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The call's message, with its buttons bound to it once it is shown; its slot is already reserved."""
    from .buttons import StoreError

    try:
        markup, set_id = (adapter.button_markup(turn.chat_id, buttons, single_use=single_use, profile=turn.profile)
                          if buttons else (None, None))
    except StoreError:
        admitted.give_back(1)
        return {"sent": False, "reason": "its choices could not be recorded, so it was not sent"}
    sent = await adapter.send_single(turn.chat_id, text, markup, admitted.metadata, **(extras or {}))
    if not getattr(sent, "success", False):
        reason = str(getattr(sent, "error", None) or "not delivered")
        if _ambiguous(sent):
            return {"sent": False, "reason": f"{reason}. {MAYBE_SHOWN}"}
        admitted.give_back(1)
        return {"sent": False, "reason": reason}
    out: dict[str, Any] = {"sent": True, "message_id": sent.message_id}
    raw = sent.raw_response if isinstance(sent.raw_response, dict) else {}
    notes = (["The text went as a rich message, which takes no link preview options."] if raw.get("preview_ignored")
             else []) + (["Telegram delivered the text without the effect."] if raw.get("effect_missing") else [])
    if notes:
        out["note"] = " ".join(notes)
    if set_id is not None:
        try:
            adapter._button_store().bind(set_id, sent.message_id, turn.thread_id)
        except StoreError:
            out["note"] = "Its choices could not be recorded: taps on them will not reach you."
    return out


async def _send_step(adapter: Any, chat_id: str, step: _Step, caption: str | None, metadata: dict[str, Any],
                     admitted: AdmittedTurn) -> dict[int, Receipt]:
    if step.kind == "photos":
        photos = [(real, caption if n == 0 else None) for n, (_, _, real) in enumerate(step.files)]
        receipts = await adapter.send_photos(chat_id, photos, metadata, charge=admitted.try_take)
        return {i: Receipt(path, receipt.message_id, receipt.reason)
                for (i, path, _), receipt in zip(step.files, receipts)}
    (i, path, real), = step.files
    if step.kind == "video":
        result = await adapter.send_video(chat_id, real, caption=caption, metadata=metadata)
    elif step.kind == "voice":
        result = await adapter.send_voice(chat_id, real, caption=caption, metadata=metadata)
    else:
        # The name the agent gave: a resolved path may be a link's target or a fetched copy.
        result = await adapter.send_document(chat_id, real, caption=caption, file_name=os.path.basename(path),
                                             metadata=metadata)
    if getattr(result, "success", False):
        return {i: Receipt(path, message_id=getattr(result, "message_id", None))}
    return {i: Receipt(path, reason=str(getattr(result, "error", None) or "not delivered"))}


async def telegram_send(args: dict[str, Any], **_: Any) -> str:
    """Handler of ``telegram_send``; every refusal comes back to the agent as an error."""
    from tools.registry import tool_error

    try:
        turn = Turn.current()
        paths, as_files, caption = _parse_send(args)
        message = _parse_message(args)
        extras = _parse_extras(args)
        runner, adapter = live_adapter()
        await on_gateway_loop(runner, lambda: _admit(adapter, turn))
        resolved = await asyncio.to_thread(_resolve, paths, turn.session_key)
        result = await on_gateway_loop(runner, lambda: as_tool_send(
            lambda: _deliver(adapter, turn, resolved, as_files, caption, message, extras)))
    except TurnRefused as refused:
        return tool_error(str(refused))
    return json.dumps(result)


STATUS_SCHEMA: dict[str, Any] = {
    "name": "telegram_status",
    "description": (
        "Set the status line of the preview the user sees in this Telegram chat while you work; it reads "
        "\"Contemplating...\" until you set one, and shows the text you set last. Use a short phrase about what you "
        "are doing now, such as \"Reading the report...\". It sends no message and does not count towards the "
        "limit of extra messages."),
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 120,
                                "description": "The status line, at most 120 characters."}},
        "required": ["text"],
    },
}


async def _set_status(adapter: Any, turn: Turn, text: str) -> dict[str, Any]:
    adapter.admitted_turn(turn)
    shown = await adapter.set_preview_status(turn.chat_id, turn.thread_id, text)
    return {"status": text, "shown": shown,
            **({} if shown else {"note": "This chat has no preview now (group chats have none)."})}


async def telegram_status(args: dict[str, Any], **_: Any) -> str:
    """Handler of ``telegram_status``."""
    from tools.registry import tool_error

    try:
        turn = Turn.current()
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TurnRefused("text must be a non-empty string.")
        runner, adapter = live_adapter()
        result = await on_gateway_loop(runner, lambda: _set_status(adapter, turn, text.strip()[:120]))
    except TurnRefused as refused:
        return tool_error(str(refused))
    return json.dumps(result)


POLL_SCHEMA: dict[str, Any] = {
    "name": "telegram_poll",
    "description": (
        "Send a poll, or a quiz, in this Telegram chat. Each answer comes back to you as a new message "
        "[poll] \"<question>\": \"<option>\". Counts as one extra message."),
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string", "maxLength": 300},
        "options": {"type": "array", "items": {"type": "string", "maxLength": 100}, "minItems": 2, "maxItems": 12},
        "quiz_answer": {"type": "integer", "description": "For a quiz: the 0-based index of the right option."},
        "multiple": {"type": "boolean", "description": "Let the user choose several options (not for a quiz)."},
    }, "required": ["question", "options"]},
}

REACT_SCHEMA: dict[str, Any] = {
    "name": "telegram_react",
    "description": (
        "React with an emoji to a message in this Telegram chat: by default the user's message you are "
        "answering. Telegram accepts only its reaction emoji (such as 👍 ❤ 🔥 🎉 🤔 👀 😁 🙏). Sends no message."),
    "parameters": {"type": "object", "properties": {
        "emoji": {"type": "string"},
        "message_id": {"type": "string", "description": "A message of this chat; default: the user's current one."},
    }, "required": ["emoji"]},
}

STICKER_SCHEMA: dict[str, Any] = {
    "name": "telegram_sticker",
    "description": (
        "Stickers in this Telegram chat. op=find searches the sticker catalog (the stickers users sent to you, "
        "and the profile's sticker sets) by emoji or words and returns candidates with their sticker_id; "
        "op=send sends one of them. Sending counts as one extra message."),
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["find", "send"]},
        "query": {"type": "string", "description": "For find: an emoji or words, such as \"thumbs up\"."},
        "sticker_id": {"type": "string", "description": "For send: a sticker_id that find returned."},
    }, "required": ["op"]},
}


WIDGET_SCHEMA: dict[str, Any] = {
    "name": "telegram_widget",
    "description": (
        "Send a widget: a visual answer drawn inside the message, such as a diagram, a chart, a step-by-step "
        "explanation or a UI prototype. html is one self-contained HTML document: inline <style> and <script>, "
        "SVG or CSS for drawing, images only as data: URLs; at most 32 KB, and 4 KB after compression, so draw "
        "with SVG and CSS. It runs on the user's device with no network: fetch, XMLHttpRequest and external "
        "files do not work. Interaction inside it (tabs, steps, toggles) stays on the device. For an action "
        "that needs you, the script calls TelegramWidget.request(data) with up to 64 bytes, and you receive "
        "[widget] data=\"<data>\" on widget <id>; answer with a message, or with new widget state through "
        "telegram_message. Every request reaches you as a new message, so call request on the user's action, "
        "never on a timer. state is the widget's initial TelegramWidget.chatData (up to 1024 bytes). height "
        "is 50 to 600 pixels, or 0 (the default) for the content's own height. Private chats only. Counts as "
        "one extra message."),
    "parameters": {"type": "object", "properties": {
        "html": {"type": "string"},
        "caption": {"type": "string", "maxLength": 1024},
        "height": {"type": "integer", "minimum": 0, "maximum": 600},
        "state": {"type": "string", "maxLength": 1024},
    }, "required": ["html"]},
}

MESSAGE_SCHEMA: dict[str, Any] = {
    "name": "telegram_message",
    "description": (
        "Change one of your messages in this Telegram conversation. op=edit replaces a text message's text "
        "(Markdown, one message long; its buttons stay unless you give new ones). op=delete deletes it. "
        "op=pin pins it (silently unless notify), op=unpin unpins it. op=widget_state sets a widget's state "
        "(TelegramWidget.chatData, up to 1024 bytes); the widget redraws from it, and \"\" removes the state. "
        "None of these counts as an extra message."),
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["edit", "delete", "pin", "unpin", "widget_state"]},
        "message_id": {"type": "string"},
        "text": {"type": "string", "description": "For edit: the new text."},
        "buttons": SEND_SCHEMA["parameters"]["properties"]["buttons"],
        "single_use": {"type": "boolean", "description": "For edit with buttons (default true)."},
        "notify": {"type": "boolean", "description": "For pin: notify the user (default false)."},
        "state": {"type": "string", "maxLength": 1024, "description": "For widget_state: the new state."},
    }, "required": ["op", "message_id"]},
}


async def _send_widget(adapter: Any, turn: Turn, args: dict[str, Any]) -> dict[str, Any]:
    from .widgets import check_state, check_widget

    html = args.get("html")
    if not isinstance(html, str):
        raise TurnRefused("html must be a string.")
    why = check_widget(html) or check_state(args.get("state", ""))
    if why:
        raise TurnRefused(f"The widget was not sent: {why}.")
    height = args.get("height") or 0
    if not isinstance(height, int) or not (height == 0 or 50 <= height <= 600):
        raise TurnRefused("height must be 50 to 600 pixels, or 0 for the content's own height.")
    admitted = adapter.admitted_turn(turn)
    admitted.take(1)
    result = await adapter.send_widget(turn.chat_id, html, admitted.metadata, caption=str(args.get("caption") or ""),
                                       height=height or None, state=args.get("state", ""), profile=turn.profile)
    out = _sent_or_refused(admitted, result, "widget")
    if isinstance(result.raw_response, dict) and result.raw_response.get("unrecorded"):
        out["note"] = "The widget could not be recorded: its requests will not reach you."
    return out


async def _change_message(adapter: Any, turn: Turn, args: dict[str, Any]) -> dict[str, Any]:
    adapter.admitted_turn(turn)
    op, message_id = args.get("op"), str(args.get("message_id") or "")
    if op not in ("edit", "delete", "pin", "unpin", "widget_state") or not message_id.isdigit():
        raise TurnRefused("op must be edit, delete, pin, unpin or widget_state, with the message_id.")
    if op == "widget_state":
        return await _set_widget_state(adapter, turn, message_id, args)
    mine = _own_target(adapter, turn, message_id, bots_only=True)
    if op == "edit":
        return await _edit(adapter, turn, message_id, mine or {}, args)
    if op == "delete":
        result = await adapter.delete_own(turn.chat_id, message_id)
    else:
        result = await adapter.pin(turn.chat_id, message_id, pinned=op == "pin", notify=bool(args.get("notify")))
    if not result.success:
        raise TurnRefused(f"The message was not changed ({op}): {result.error}")
    return {op: True, "message_id": message_id}


async def _edit(adapter: Any, turn: Turn, message_id: str, mine: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    from .buttons import StoreError

    if mine.get("kind") != "text":  # a record without a kind (before slice 6) may be a prompt
        raise TurnRefused("Only a text message you sent can be edited: not a file, poll, sticker, widget or "
                          "prompt (for a widget, set its state with op=widget_state).")
    text, buttons, single_use = _parse_message({k: args[k] for k in ("text", "buttons", "single_use") if k in args})
    if not text:
        raise TurnRefused("edit needs the new text.")
    markup, set_id = None, None
    if "buttons" in args and not buttons:
        markup = {"inline_keyboard": []}  # buttons=[]: the keyboard goes
    elif buttons:
        try:
            markup, set_id = adapter.button_markup(turn.chat_id, buttons, single_use=single_use, profile=turn.profile)
        except StoreError:
            raise TurnRefused("The new buttons could not be recorded, so the message was not edited.") from None
    result = await adapter.edit_text(turn.chat_id, message_id, text, markup)
    uncertain = not result.success and _ambiguous(result)
    if not result.success and not uncertain:
        raise TurnRefused(f"The message was not edited: {result.error}")
    out: dict[str, Any] = {"edited": True, "message_id": message_id}
    if isinstance(result.raw_response, dict) and result.raw_response.get("note"):
        out["note"] = result.raw_response["note"]
    if markup is not None:
        try:
            store = adapter._button_store()
            store.retire(turn.chat_id, message_id)  # the old keyboard is gone, or may be
            if set_id is not None:
                store.bind(set_id, message_id, turn.thread_id)
            if uncertain:
                adapter._keep_keyboard(turn.chat_id, message_id, mine, markup)
        except StoreError:
            out["note"] = "Its new choices could not be recorded: taps on them will not reach you."
    if uncertain:
        raise TurnRefused(f"The edit may not have been applied ({result.error}): Telegram did not confirm it. "
                          "If you edit it again, give the buttons it should show.")
    return out


async def _set_widget_state(adapter: Any, turn: Turn, message_id: str, args: dict[str, Any]) -> dict[str, Any]:
    from .widgets import check_state

    if "state" not in args:
        raise TurnRefused("state is required: the widget's new state, or \"\" to remove it.")
    why = check_state(args["state"])
    if why:
        raise TurnRefused(f"The widget's state was not changed: {why}.")
    widget = adapter._widget(turn.chat_id, message_id)
    if (widget is None or (widget.get("thread") or None) != (turn.thread_id or None)
            or (widget.get("profile") or "") != (turn.profile or "")):
        raise TurnRefused("That is not one of your widgets in this conversation.")
    result = await adapter.set_widget_state(turn.chat_id, message_id, args["state"])
    if not result.success:
        raise TurnRefused(f"The widget's state was not changed: {result.error}")
    return {"changed": True, "message_id": message_id}


def _parse_poll(args: dict[str, Any]) -> tuple[str, list[str], Optional[int], bool]:
    question, options = args.get("question"), args.get("options")
    if not isinstance(question, str) or not question.strip():
        raise TurnRefused("question must be a non-empty string.")
    if (not isinstance(options, list) or not 2 <= len(options) <= 12
            or not all(isinstance(o, str) and o.strip() for o in options)):
        raise TurnRefused("options must be 2 to 12 non-empty strings.")
    quiz = args.get("quiz_answer")
    if quiz is not None and (not isinstance(quiz, int) or not 0 <= quiz < len(options)):
        raise TurnRefused("quiz_answer must be the index of one of the options.")
    multiple = bool(args.get("multiple", False))
    if quiz is not None and multiple:
        raise TurnRefused("A quiz has one right answer: multiple does not apply.")
    return question.strip()[:300], [o.strip()[:100] for o in options], quiz, multiple


async def _send_poll(adapter: Any, turn: Turn, poll: tuple[str, list[str], Optional[int], bool]) -> dict[str, Any]:
    admitted = adapter.admitted_turn(turn)
    admitted.take(1)
    question, options, quiz, multiple = poll
    result = await adapter.send_poll(turn.chat_id, question, options, admitted.metadata, quiz_answer=quiz,
                                     multiple=multiple, profile=turn.profile)
    return _sent_or_refused(admitted, result, "poll")


def _sent_or_refused(admitted: AdmittedTurn, result: Any, what: str) -> dict[str, Any]:
    """A one-message send's result: a certain refusal gives its slot back, an uncertain one keeps it."""
    if result.success:
        return {"sent": True, "message_id": result.message_id}
    if _ambiguous(result):
        raise TurnRefused(f"The {what} may not have been sent ({result.error}). {MAYBE_SHOWN}")
    admitted.give_back(1)
    raise TurnRefused(f"The {what} was not sent: {result.error}")


async def _react(adapter: Any, turn: Turn, emoji: str, message_id: Optional[str]) -> dict[str, Any]:
    adapter.admitted_turn(turn)
    target = str(message_id or turn.inbound_message_id)
    if not target.isdigit():
        raise TurnRefused("This turn has no message of its own to react to: give message_id.")
    _own_target(adapter, turn, target)
    result = await adapter.react(turn.chat_id, str(target), emoji)
    if not result.success:
        raise TurnRefused(f"The reaction was not set: {result.error}")
    return {"reacted": emoji, "message_id": str(target)}


async def _sticker(adapter: Any, turn: Turn, args: dict[str, Any]) -> dict[str, Any]:
    from .actions import sticker_matches

    admitted = adapter.admitted_turn(turn)
    if args.get("op") == "find":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise TurnRefused("find needs a query: an emoji or words.")
        found = sticker_matches(await adapter.sticker_catalog(turn.profile), query.strip())
        return {"candidates": [{k: s.get(k) for k in ("sticker_id", "emoji", "description", "set")} for s in found]}
    if args.get("op") != "send" or not isinstance(args.get("sticker_id"), str):
        raise TurnRefused("op must be find (with query) or send (with sticker_id).")
    admitted.take(1)
    result = await adapter.send_sticker(turn.chat_id, args["sticker_id"], admitted.metadata, turn.profile)
    return _sent_or_refused(admitted, result, "sticker")


def _handler(run: Callable[[Any, Turn, dict[str, Any]], Any]) -> Callable[..., Any]:
    """A tool handler: the turn's guards, then ``run`` on the gateway loop; refusals go back to the agent."""
    async def handle(args: dict[str, Any], **_: Any) -> str:
        from tools.registry import tool_error

        try:
            turn = Turn.current()
            runner, adapter = live_adapter()
            result = await on_gateway_loop(runner, lambda: as_tool_send(lambda: run(adapter, turn, args)))
        except TurnRefused as refused:
            return tool_error(str(refused))
        return json.dumps(result)
    return handle


telegram_poll = _handler(lambda adapter, turn, args: _send_poll(adapter, turn, _parse_poll(args)))
telegram_react = _handler(lambda adapter, turn, args: _react(
    adapter, turn, str(args.get("emoji") or "").strip() or _no_emoji(), args.get("message_id")))
telegram_sticker = _handler(_sticker)
telegram_widget = _handler(_send_widget)
telegram_message = _handler(_change_message)


def _no_emoji() -> str:
    raise TurnRefused("emoji must be a reaction emoji.")


def register_tools(ctx: Any) -> None:
    """Register the toolset's tools on the plugin context."""
    from tools.registry import no_cache_check_fn

    check = no_cache_check_fn(_available)
    ctx.register_tool(name="telegram_send", toolset=TOOLSET, schema=SEND_SCHEMA, handler=telegram_send,
                      check_fn=check, is_async=True, description=SEND_SCHEMA["description"], emoji="📎")
    ctx.register_tool(name="telegram_status", toolset=TOOLSET, schema=STATUS_SCHEMA, handler=telegram_status,
                      check_fn=check, is_async=True, description=STATUS_SCHEMA["description"], emoji="💭")
    for name, schema, handler, emoji in (("telegram_poll", POLL_SCHEMA, telegram_poll, "📊"),
                                         ("telegram_react", REACT_SCHEMA, telegram_react, "👍"),
                                         ("telegram_sticker", STICKER_SCHEMA, telegram_sticker, "🎴"),
                                         ("telegram_widget", WIDGET_SCHEMA, telegram_widget, "🧩"),
                                         ("telegram_message", MESSAGE_SCHEMA, telegram_message, "✏️")):
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, check_fn=check,
                          is_async=True, description=schema["description"], emoji=emoji)
