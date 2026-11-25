"""The agent's ``telegram`` toolset (docs/design.md, Agent interface, Tools).

Slice 2 adds ``telegram_send`` for files; later slices add text, buttons, widgets, polls, reactions and
stickers to the same toolset. Every tool acts in the turn's own chat through ``turn``, which carries the
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
from typing import Any, Callable

from .delivery import Receipt, album_chunks, collecting_notices, combined_notice
from .turn import AdmittedTurn, Turn, TurnRefused, live_adapter, on_gateway_loop

TOOLSET = "telegram"
MAX_FILES = 20
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp"}
_VOICE_EXTS = {".ogg", ".opus"}


def _available() -> bool:
    """Offer the toolset only to a Telegram chat turn, so other surfaces do not see tools they cannot use.

    Uncached (``no_cache_check_fn``): the answer depends on the turn building the tool list."""
    from hermes_cli.plugin_session import session_surface

    return session_surface() == "telegram"


SEND_SCHEMA: dict[str, Any] = {
    "name": "telegram_send",
    "description": (
        "Send files to the user in this Telegram chat now, in the order given. Each file arrives as its kind: "
        "photo, video, audio, voice note (.ogg/.opus) or document; consecutive photos arrive as one album, more "
        "than 10 as consecutive albums. With as_files=true every file arrives as a downloadable document with "
        "its name and bytes unchanged, never shown or played: use it when the user asks for a photo, video or "
        "recording \"as a file\". A file that cannot be delivered (missing, over Telegram's 50 MB limit for "
        "bots, refused by Telegram) is named to the user with the reason, and listed under not_delivered in "
        "the result. At most 10 extra messages per turn: an album counts as one."),
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
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f.strip() for f in files):
        raise TurnRefused("files must be a non-empty list of absolute paths.")
    if len(files) > MAX_FILES:
        raise TurnRefused(f"At most {MAX_FILES} files per call.")
    caption = args.get("caption") or ""
    if not isinstance(caption, str):
        raise TurnRefused("caption must be a string.")
    return [f.strip() for f in files], bool(args.get("as_files", False)), caption[:1024]


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
                   caption: str) -> dict[str, Any]:
    """Send the plan on the gateway loop; nothing is sent when the turn's cap cannot hold all of it."""
    from gateway.platforms.base import should_send_media_as_audio

    admitted: AdmittedTurn = adapter.admitted_turn(turn)
    deliverable = [(i, path, real) for i, (path, real, _) in enumerate(resolved) if real]
    steps = _plan(deliverable, as_files,
                  lambda ext: should_send_media_as_audio(adapter.platform, ext, is_voice=ext in _VOICE_EXTS))
    admitted.take(sum(step.messages for step in steps) + 1)  # one more: the notice of what does not arrive
    metadata = admitted.metadata
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
    return {"sent": [{"file": r.file, "message_id": r.message_id} for r in ordered if r.sent],
            "not_delivered": [{"file": r.file, "reason": r.reason} for r in ordered if not r.sent]}


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
        runner, adapter = live_adapter()
        await on_gateway_loop(runner, lambda: _admit(adapter, turn))
        resolved = await asyncio.to_thread(_resolve, paths, turn.session_key)
        result = await on_gateway_loop(runner, lambda: _deliver(adapter, turn, resolved, as_files, caption))
    except TurnRefused as refused:
        return tool_error(str(refused))
    return json.dumps(result)


def register_tools(ctx: Any) -> None:
    """Register the toolset's tools on the plugin context."""
    from tools.registry import no_cache_check_fn

    check = no_cache_check_fn(_available)
    ctx.register_tool(name="telegram_send", toolset=TOOLSET, schema=SEND_SCHEMA, handler=telegram_send,
                      check_fn=check, is_async=True, description=SEND_SCHEMA["description"], emoji="📎")
