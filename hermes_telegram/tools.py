"""The agent's ``telegram`` toolset (docs/design.md, Agent interface, Tools).

Slice 2 adds ``telegram_send`` for files; later slices add text, buttons, widgets, polls, reactions and
stickers to the same toolset. Every tool acts in the turn's own chat through ``turn.Turn``, which carries
the three guards of the design: no recipient argument, Telegram user turns only, a cap per turn.
"""

import json
import os
from typing import Any
from urllib.parse import quote

from .turn import SendBudget, Turn, TurnRefused, live_adapter, on_gateway_loop

TOOLSET = "telegram"
MAX_FILES = 20
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp"}

_budget = SendBudget()


def _available() -> bool:
    """Offer the toolset only to a Telegram chat turn, so other surfaces do not see tools they cannot use.

    Uncached (``no_cache_check_fn``): the answer depends on the turn building the tool list."""
    from hermes_cli.plugin_session import session_surface

    return session_surface() == "telegram"


SEND_SCHEMA: dict[str, Any] = {
    "name": "telegram_send",
    "description": (
        "Send files to the user in this Telegram chat now. Each file arrives as its kind: photo, video, "
        "audio, voice note (.ogg/.opus) or document; several photos arrive as one album, more than 10 as "
        "consecutive albums. With as_files=true every file arrives as a downloadable document with its name and "
        "bytes unchanged, never shown or played: use it when the user asks for a photo, video or recording "
        "\"as a file\". A file that cannot be delivered (missing, over Telegram's 50 MB limit for bots, refused "
        "by Telegram) is named to the user with the reason, and listed under not_delivered in the result. "
        "At most 10 extra messages per turn."),
    "parameters": {
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_FILES,
                      "description": "Absolute paths of the files to send, in order."},
            "as_files": {"type": "boolean",
                         "description": "Send every file as a document instead of as a photo, video or audio."},
            "caption": {"type": "string", "description": "Optional caption for the first file (1024 characters)."},
        },
        "required": ["files"],
    },
}


def _messages_needed(paths: list[str], as_files: bool) -> int:
    if as_files:
        return len(paths)
    photos = sum(os.path.splitext(p)[1].lower() in _IMAGE_EXTS for p in paths)
    return (len(paths) - photos) + -(-photos // 10)


def _parse_send(args: dict[str, Any]) -> tuple[list[str], bool, str]:
    files = args.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f.strip() for f in files):
        raise TurnRefused("files must be a non-empty list of absolute paths.")
    if len(files) > MAX_FILES:
        raise TurnRefused(f"At most {MAX_FILES} files per call.")
    caption = args.get("caption") or ""
    if not isinstance(caption, str):
        raise TurnRefused("caption must be a string.")
    return [f.strip() for f in files], bool(args.get("as_files", False)), caption[:1024]


async def _deliver(adapter: Any, turn: Turn, paths: list[str], as_files: bool, caption: str) -> dict[str, Any]:
    from gateway.platforms.base import should_send_media_as_audio

    sent: list[dict[str, Any]] = []
    not_delivered: list[dict[str, str]] = []
    deliverable: list[tuple[str, str]] = []  # (requested path, resolved path)
    for path in paths:
        resolved = adapter.validate_media_delivery_path(path, session_key=turn.session_key)
        if resolved:
            deliverable.append((path, resolved))
            continue
        reason = ("Hermes' delivery policy does not allow sending it" if os.path.isfile(os.path.expanduser(path))
                  else "the file does not exist on the Hermes host")
        result = await adapter._not_delivered(turn.chat_id, path, reason, turn.metadata)
        not_delivered.append({"file": path, "reason": result.error or reason})

    def record(path: str, result: Any) -> None:
        if result is not None and getattr(result, "success", False):
            sent.append({"file": path, "message_id": getattr(result, "message_id", None)})
        else:
            not_delivered.append({"file": path, "reason": getattr(result, "error", None) or "not delivered"})

    first_caption = caption or None
    if as_files:
        for path, resolved in deliverable:
            record(path, await adapter.send_document(turn.chat_id, resolved, caption=first_caption,
                                                     metadata=turn.metadata))
            first_caption = None
        return {"sent": sent, "not_delivered": not_delivered}
    photos = [(p, r) for p, r in deliverable if os.path.splitext(r)[1].lower() in _IMAGE_EXTS]
    others = [(p, r) for p, r in deliverable if (p, r) not in photos]
    if photos:
        images = [(f"file://{quote(r)}", first_caption if i == 0 and first_caption else "")
                  for i, (_, r) in enumerate(photos)]
        first_caption = None
        album = await adapter.send_multiple_images(turn.chat_id, images, metadata=turn.metadata)
        for path, _ in photos:
            record(path, album)
    for path, resolved in others:
        ext = os.path.splitext(resolved)[1].lower()
        if should_send_media_as_audio(adapter.platform, ext, is_voice=ext in {".ogg", ".opus"}):
            result = await adapter.send_voice(turn.chat_id, resolved, caption=first_caption, metadata=turn.metadata)
        elif ext in _VIDEO_EXTS:
            result = await adapter.send_video(turn.chat_id, resolved, caption=first_caption, metadata=turn.metadata)
        else:
            result = await adapter.send_document(turn.chat_id, resolved, caption=first_caption,
                                                 metadata=turn.metadata)
        first_caption = None
        record(path, result)
    return {"sent": sent, "not_delivered": not_delivered}


async def telegram_send(args: dict[str, Any], **_: Any) -> str:
    """Handler of ``telegram_send``; every refusal comes back to the agent as an error."""
    from tools.registry import tool_error

    try:
        turn = Turn.current()
        paths, as_files, caption = _parse_send(args)
        _budget.take(turn, _messages_needed(paths, as_files))
        runner, adapter = live_adapter()
        result = await on_gateway_loop(runner, lambda: _deliver(adapter, turn, paths, as_files, caption))
    except TurnRefused as refused:
        return tool_error(str(refused))
    return json.dumps(result)


def register_tools(ctx: Any) -> None:
    """Register the toolset's tools on the plugin context."""
    from tools.registry import no_cache_check_fn

    check = no_cache_check_fn(_available)
    ctx.register_tool(name="telegram_send", toolset=TOOLSET, schema=SEND_SCHEMA, handler=telegram_send,
                      check_fn=check, is_async=True, description=SEND_SCHEMA["description"], emoji="📎")
