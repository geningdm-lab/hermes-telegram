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
        "than 10 as consecutive albums. With as_files=true every file arrives as a downloadable document with "
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
                   ) -> dict[str, Any]:
    """Send the plan on the gateway loop; nothing is sent when the turn's cap cannot hold all of it."""
    from gateway.platforms.base import should_send_media_as_audio

    admitted: AdmittedTurn = adapter.admitted_turn(turn)
    deliverable = [(i, path, real) for i, (path, real, _) in enumerate(resolved) if real]
    steps = _plan(deliverable, as_files,
                  lambda ext: should_send_media_as_audio(adapter.platform, ext, is_voice=ext in _VOICE_EXTS))
    text, buttons, single_use = message
    # One more: the notice of what does not arrive, when files go. The text is one message (send_single).
    admitted.take(sum(step.messages for step in steps) + (1 if resolved else 0) + (1 if text else 0))
    metadata = admitted.metadata
    out: dict[str, Any] = {}
    if text:
        out["message"] = await _send_text(adapter, turn, admitted, text, buttons, single_use)
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
                     single_use: bool) -> dict[str, Any]:
    """The call's message, with its buttons bound to it once it is shown; its slot is already reserved."""
    from .buttons import StoreError

    try:
        markup, set_id = (adapter.button_markup(turn.chat_id, buttons, single_use=single_use, profile=turn.profile)
                          if buttons else (None, None))
    except StoreError:
        admitted.give_back(1)
        return {"sent": False, "reason": "its choices could not be recorded, so it was not sent"}
    sent = await adapter.send_single(turn.chat_id, text, markup, admitted.metadata)
    if not getattr(sent, "success", False):
        reason = str(getattr(sent, "error", None) or "not delivered")
        if _ambiguous(sent):
            return {"sent": False, "reason": f"{reason}. {MAYBE_SHOWN}"}
        admitted.give_back(1)
        return {"sent": False, "reason": reason}
    out: dict[str, Any] = {"sent": True, "message_id": sent.message_id}
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
        runner, adapter = live_adapter()
        await on_gateway_loop(runner, lambda: _admit(adapter, turn))
        resolved = await asyncio.to_thread(_resolve, paths, turn.session_key)
        result = await on_gateway_loop(runner, lambda: as_tool_send(
            lambda: _deliver(adapter, turn, resolved, as_files, caption, message)))
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
    if target != str(turn.inbound_message_id):
        # Telegram takes only the chat and the message: a message of another topic would get it.
        mine = adapter._bot_message(turn.chat_id, target)
        if mine is None or (mine.get("thread") or None) != (turn.thread_id or None):
            raise TurnRefused("message_id must be the user's current message or one of your messages in this "
                              "conversation.")
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
                                         ("telegram_sticker", STICKER_SCHEMA, telegram_sticker, "🎴")):
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, check_fn=check,
                          is_async=True, description=schema["description"], emoji=emoji)
