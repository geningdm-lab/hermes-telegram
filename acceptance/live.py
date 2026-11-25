"""The owner's side of the acceptance chat, through the owner's own Telegram account (Telethon, MTProto layer
233, which knows rich messages, drafts and widgets), and the agent's log for the chat (Hermes' ``state.db``).

The session file is ``STATE/owner.session``: it is a login to the owner's account, kept outside the
repository; ``python -m acceptance logout`` ends it on Telegram and deletes it.
"""

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from telethon import TelegramClient, events
from telethon.tl import types

from .home import HOME, STATE

SESSION = STATE / "owner"


def client() -> TelegramClient:
    api_id, api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH come from 1Password: run under `op run --env-file "
                         "acceptance/op.env` (acceptance/README.md).")
    STATE.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(SESSION), int(api_id), api_hash)


def plain(rich: Any) -> str:
    """The text of a RichText or PageBlock tree, without formatting."""
    if rich is None:
        return ""
    if isinstance(rich, str):
        return rich
    if isinstance(rich, (list, tuple)):
        return "".join(plain(x) for x in rich)
    if isinstance(rich, types.TextConcat):
        return "".join(plain(t) for t in rich.texts)
    if isinstance(rich, types.PageBlockMath):
        return rich.source
    parts = []
    for name in ("text", "texts", "title", "items", "blocks", "rows", "cells", "caption", "summary"):
        value = getattr(rich, name, None)
        if value is not None and not isinstance(value, (bool, int)):
            parts.append(plain(value))
    return "\n".join(p for p in parts if p)


def blocks(message: Any) -> list[Any]:
    """A rich message's top-level blocks, nested blocks included, in order."""
    out: list[Any] = []

    def walk(block: Any) -> None:
        out.append(block)
        for name in ("blocks", "items"):
            for child in getattr(block, name, None) or ():
                if not isinstance(child, (str, bytes)):
                    walk(child)

    rich = getattr(message, "rich_message", None)
    for block in getattr(rich, "blocks", None) or ():
        walk(block)
    return out


def shown_text(message: Any) -> str:
    """What the user reads: a rich message's text, else the message text."""
    return plain(message.rich_message.blocks) if getattr(message, "rich_message", None) else (message.message or "")


@dataclass
class Frame:
    """One draft update the bot sent (the preview), as the owner's client receives it."""

    at: float
    text: str
    can_stop: bool
    random_id: int


@dataclass
class Chat:
    """The private chat of the owner's account with the bot."""

    tg: TelegramClient
    bot: Any
    frames: list[Frame] = field(default_factory=list)

    @classmethod
    async def open(cls, tg: TelegramClient, username: str) -> "Chat":
        chat = cls(tg, await tg.get_entity(username))

        @tg.on(events.Raw(types.UpdateUserTyping))
        async def on_typing(update: types.UpdateUserTyping) -> None:
            if update.user_id != chat.bot.id:
                return
            action = update.action
            if isinstance(action, types.SendMessageRichMessageDraftAction):
                chat.frames.append(Frame(time.time(), plain(action.rich_message.blocks), bool(action.can_stop),
                                         action.random_id))
            elif isinstance(action, types.SendMessageTextDraftAction):
                chat.frames.append(Frame(time.time(), action.text.text, bool(action.can_stop), action.random_id))

        return chat

    async def say(self, text: str, **kwargs: Any) -> Any:
        return await self.tg.send_message(self.bot, text, **kwargs)

    async def from_bot(self, after_id: int) -> list[Any]:
        """The bot's messages after ``after_id``, oldest first, as they are now (edits included)."""
        found = [m async for m in self.tg.iter_messages(self.bot, min_id=after_id, limit=200)]
        return sorted((m for m in found if not m.out), key=lambda m: m.id)

    async def wait(self, after_id: int, until: Any, *, timeout: float = 240.0, quiet: float = 0.0) -> list[Any]:
        """The bot's messages after ``after_id`` once ``until(messages)`` holds, then ``quiet`` more seconds
        with nothing new; ``TimeoutError`` otherwise."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = await self.from_bot(after_id)
            if until(messages):
                if quiet:
                    await asyncio.sleep(quiet)
                    messages = await self.from_bot(after_id)
                return messages
            await asyncio.sleep(1.5)
        raise TimeoutError(f"no answer that satisfies the row within {timeout:.0f} s")

    async def message(self, message_id: int) -> Any:
        return await self.tg.get_messages(self.bot, ids=message_id)


def answered(marker: str) -> Any:
    """``until`` for ``Chat.wait``: a bot message showing ``marker``."""
    return lambda messages: any(marker in shown_text(m) for m in messages)


def agent_log(owner_id: int, since: float) -> list[dict[str, Any]]:
    """The agent's log for the chat: every message Hermes recorded in the owner's private chat since
    ``since`` (Unix time), oldest first, as ``{"role", "content", "tool_name", "at"}``."""
    db = HOME / "state.db"
    if not db.exists():
        return []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT m.role, m.content, m.tool_name, m.tool_calls, m.timestamp FROM messages m "
            "JOIN sessions s ON s.id = m.session_id WHERE s.chat_id = ? AND m.timestamp >= ? ORDER BY m.id",
            (str(owner_id), since)).fetchall()
    return [{"role": r[0], "content": r[1] or "", "tool_name": r[2], "tool_calls": json.loads(r[3]) if r[3] else [],
             "at": r[4]} for r in rows]


def incoming(owner_id: int, since: float, prefix: Optional[str] = None) -> list[str]:
    """What reached the agent from the chat since ``since``: user messages, optionally only those starting
    with ``prefix`` (``[button]``, ``[widget]``, ``[poll]``, ``[reaction]``)."""
    texts = [m["content"] for m in agent_log(owner_id, since) if m["role"] == "user"]
    return [t for t in texts if prefix is None or prefix in t]
