"""The Business inbox: the owner's private chats, kept through Telegram Business (docs/business-inbox.md; slice 1).

The bot connected to the owner's account receives ``business_connection``, ``business_message``,
``edited_business_message`` and ``deleted_business_messages`` (core.telegram.org/bots/api, Update). A handler that
matches only those, first in the first group, takes every one of them and stops it there, so no other handler,
and no agent, sees it. What it keeps goes into one SQLite file in the adapter's Hermes home, for at most
``retention_hours`` by the message's Telegram date and at most ``max_messages_per_chat`` records per chat.

- Nothing on this path calls a Bot API method but ``getBusinessConnection``: no send, edit, read mark or deletion.
- Updates of a connection whose owner is not configured store nothing; such a connection is remembered in memory.
- The author is set from ids: ``owner`` when the sender is the connection's owner (or a bot sent it on the
  owner's behalf, ``sender_business_bot``), ``contact`` otherwise.
- Media is kept as metadata. Contacts, locations, polls and the like keep their kind only.
- Each update is applied once, also across restarts: its id is part of the write's transaction.
- Purged rows do not linger in the file: ``secure_delete`` is on and the journal is not WAL. A file that exists
  keeps expiring while the gateway runs, also when intake is switched off.
- Logs name update kinds and outcomes, never text, names or ids; python-telegram-bot's own DEBUG records of a
  business update are dropped.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, NewType, Optional

logger = logging.getLogger(__name__)

OwnerId = NewType("OwnerId", int)
ChatId = NewType("ChatId", int)
MessageId = NewType("MessageId", int)
ConnectionId = NewType("ConnectionId", str)
UpdateKey = tuple[str, int]  # (the bot's id, the update's id): update ids count per bot

STORE_NAME = "telegram_business_inbox.sqlite3"
SCHEMA_VERSION = "1"
DEFAULT_RETENTION_HOURS = 72
DEFAULT_MAX_PER_CHAT = 1000
MAX_RETENTION_HOURS = 87_600  # ten years: a bound on what SQLite and the purge are asked to handle, not a policy
MAX_PER_CHAT = 1_000_000
ENV_RETENTION_HOURS = "HERMES_TELEGRAM_INBOX_RETENTION_HOURS"
ENV_MAX_PER_CHAT = "HERMES_TELEGRAM_INBOX_MAX_PER_CHAT"
MAX_VERSIONS = 50  # per record: the first version and the newest edits; a burst of edits cannot grow it further
PURGE_EVERY_SECS = 600.0
BUSINESS_GROUP = -20  # before every other group of this plugin; -10 is the Stop button's TypeHandler (preview.py)
_BUSINESS_FIELDS = ("business_connection", "business_message", "edited_business_message",
                    "deleted_business_messages")


def _clock() -> float:
    """Unix time now; the checks move it."""
    return time.time()


class Author(StrEnum):
    OWNER = "owner"
    CONTACT = "contact"


class Change(StrEnum):
    INSERT = "insert"
    EDIT = "edit"
    DELETE = "delete"


# --- python-telegram-bot's DEBUG records ------------------------------------------------------------------


class _DropBusinessPayloads(logging.Filter):
    """python-telegram-bot logs every getUpdates result and webhook body at DEBUG, before any handler runs
    (Hermes drops them while a vault code is pending, ``harvested/telegram_vault_code.py``), and the reply of
    getBusinessConnection names the owner. Drop the records that carry a business update or connection, so
    ``logging.level: DEBUG`` keeps the owner's chats out of the logs. Filters run only for enabled records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "business_connection" not in record.getMessage()


for _name in ("telegram.Bot", "telegram.ext.ExtBot", "telegram.ext.Updater", "telegram.ext.Application"):
    _logger = logging.getLogger(_name)
    if not any(isinstance(f, _DropBusinessPayloads) for f in _logger.filters):
        _logger.addFilter(_DropBusinessPayloads())


# --- settings ---------------------------------------------------------------------------------------------


class InboxConfigError(ValueError):
    """``business_inbox`` in the platform's ``extra`` cannot be used; intake stays off."""


@dataclass(frozen=True)
class Limits:
    retention_hours: int
    max_per_chat: int

    @property
    def retention_secs(self) -> int:
        return self.retention_hours * 3600


DEFAULT_LIMITS = Limits(DEFAULT_RETENTION_HOURS, DEFAULT_MAX_PER_CHAT)


@dataclass(frozen=True)
class InboxSettings:
    owner: OwnerId
    limits: Limits


_DIGITS = re.compile(r"[0-9]{1,16}")


def _bounded_int(value: Any, what: str, maximum: int) -> int:
    if isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise InboxConfigError(f"{what} must be a whole number from 1 to {maximum}")
    return value


def inbox_settings(extra: Mapping[str, Any], environ: Mapping[str, str], profile: str) -> Optional[InboxSettings]:
    """The inbox's settings from the platform's ``extra`` and the environment, or None when it is not configured.

    ``owners`` maps the owner's Telegram user id to the profile whose home keeps their chats. This version takes
    exactly one owner, kept in the adapter's own profile, ``profile``.
    """
    raw = extra.get("business_inbox")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise InboxConfigError("business_inbox must be a mapping")
    owners = raw.get("owners")
    if not isinstance(owners, Mapping) or len(owners) != 1:
        raise InboxConfigError("business_inbox.owners must map exactly one Telegram user id to a profile")
    [(key, target)] = owners.items()
    owner = OwnerId(_bounded_int(key, "the owner id in business_inbox.owners", 2**53))
    if target != profile:
        raise InboxConfigError(f"business_inbox.owners maps the owner to profile {target!r}; this version keeps "
                               f"the inbox only in the adapter's own profile {profile!r}")
    retention = environ.get(ENV_RETENTION_HOURS) or raw.get("retention_hours", DEFAULT_RETENTION_HOURS)
    max_per_chat = environ.get(ENV_MAX_PER_CHAT) or raw.get("max_messages_per_chat", DEFAULT_MAX_PER_CHAT)
    return InboxSettings(owner, Limits(_bounded_int(retention, "retention_hours", MAX_RETENTION_HOURS),
                                       _bounded_int(max_per_chat, "max_messages_per_chat", MAX_PER_CHAT)))


# --- what an update says ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    kind: str
    mime: Optional[str] = None
    size: Optional[int] = None
    name: Optional[str] = None
    duration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True)
class ConnectionSeen:
    connection: ConnectionId
    owner: OwnerId
    enabled: bool
    rights: dict[str, bool]


@dataclass(frozen=True)
class MessageSeen:
    connection: ConnectionId
    chat: ChatId
    chat_name: str
    message: MessageId
    sender: Optional[int]
    sender_name: str
    via_bot: bool
    date: int
    edited: int  # the edit's Unix time; 0 for the message as first sent
    text: Optional[str]
    reply_to: Optional[MessageId]
    forwarded: bool
    offline: bool
    attachments: tuple[Attachment, ...]
    media_group: Optional[str]


@dataclass(frozen=True)
class MessagesDeleted:
    connection: ConnectionId
    chat: ChatId
    messages: tuple[MessageId, ...]


@dataclass(frozen=True)
class Skipped:
    reason: str


BusinessEvent = ConnectionSeen | MessageSeen | MessagesDeleted


def business_kind(update: Any) -> Optional[str]:
    """The business field an update carries, or None for any other update."""
    for field in _BUSINESS_FIELDS:
        if getattr(update, field, None) is not None:
            return field
    return None


def _unix(value: Any) -> Optional[int]:
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _seconds(value: Any) -> Optional[int]:
    """A duration as whole seconds: PTB 22.8 gives an int, or a timedelta under ``PTB_TIMEDELTA``."""
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _opt_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _file(kind: str, item: Any) -> Attachment:
    side = _opt_int(getattr(item, "length", None))  # a video note is square
    return Attachment(kind=kind, mime=_opt_str(getattr(item, "mime_type", None)),
                      size=_opt_int(getattr(item, "file_size", None)),
                      name=_opt_str(getattr(item, "file_name", None)),
                      duration=_seconds(getattr(item, "duration", None)),
                      width=_opt_int(getattr(item, "width", None)) or side,
                      height=_opt_int(getattr(item, "height", None)) or side)


# Content kept as its kind only: what it holds (a phone number, a place, a question) is not metadata of a file.
_KIND_ONLY = ("contact", "location", "venue", "poll", "dice", "game", "story", "invoice", "paid_media",
              "giveaway", "checklist")


def attachments_of(message: Any) -> tuple[Attachment, ...]:
    """Metadata of what a message carries besides its text. Nothing is downloaded."""
    found: list[Attachment] = []
    photos = getattr(message, "photo", None) or ()
    if photos:
        largest = max(photos, key=lambda p: (_opt_int(getattr(p, "width", None)) or 0)
                      * (_opt_int(getattr(p, "height", None)) or 0))
        found.append(_file("photo", largest))
    animation = getattr(message, "animation", None)
    if animation is not None:
        found.append(_file("animation", animation))
    for kind in ("video", "audio", "voice", "video_note", "sticker"):
        item = getattr(message, kind, None)
        if item is not None:
            found.append(_file(kind, item))
    document = getattr(message, "document", None)
    if document is not None and animation is None:  # PTB sets both for an animation
        found.append(_file("document", document))
    for kind in _KIND_ONLY:
        if getattr(message, kind, None) is not None:
            found.append(Attachment(kind=kind))
    return tuple(found)


def _name(entity: Any) -> str:
    full = getattr(entity, "full_name", None)
    if isinstance(full, str) and full:
        return full
    parts = [getattr(entity, k, None) for k in ("first_name", "last_name")]
    return " ".join(p for p in parts if isinstance(p, str) and p) or str(getattr(entity, "title", "") or "")


def _rights(rights: Any) -> dict[str, bool]:
    """Every right BusinessBotRights grants (bots/api, BusinessBotRights), so ``status`` can name each one on."""
    names = getattr(type(rights), "__slots__", ()) if rights is not None else ()
    return {n: bool(getattr(rights, n, False)) for n in names if n.startswith("can_")}


def parse_business_update(update: Any) -> BusinessEvent | Skipped:
    """The boundary: one PTB business update as the inbox's own event, or why it is skipped."""
    connection = getattr(update, "business_connection", None)
    if connection is not None:
        owner = _opt_int(getattr(getattr(connection, "user", None), "id", None))
        cid = _opt_str(getattr(connection, "id", None))
        if owner is None or cid is None:
            return Skipped("malformed")
        return ConnectionSeen(ConnectionId(cid), OwnerId(owner), bool(getattr(connection, "is_enabled", False)),
                              _rights(getattr(connection, "rights", None)))
    deleted = getattr(update, "deleted_business_messages", None)
    if deleted is not None:
        chat = getattr(deleted, "chat", None)
        if getattr(chat, "type", None) != "private":
            return Skipped("not_private")
        cid = _opt_str(getattr(deleted, "business_connection_id", None))
        chat_id = _opt_int(getattr(chat, "id", None))
        if cid is None or chat_id is None:
            return Skipped("malformed")
        ids = tuple(MessageId(i) for i in (getattr(deleted, "message_ids", None) or ()) if _opt_int(i) is not None)
        return MessagesDeleted(ConnectionId(cid), ChatId(chat_id), ids)
    first = getattr(update, "business_message", None)
    message = first or getattr(update, "edited_business_message", None)
    if message is None:
        return Skipped("not_business")
    chat = getattr(message, "chat", None)
    if getattr(chat, "type", None) != "private":
        return Skipped("not_private")
    cid = _opt_str(getattr(message, "business_connection_id", None))
    chat_id = _opt_int(getattr(chat, "id", None))
    message_id = _opt_int(getattr(message, "message_id", None))
    date = _unix(getattr(message, "date", None))
    if cid is None or chat_id is None or message_id is None or date is None:
        return Skipped("malformed")
    sender = getattr(message, "from_user", None)
    via_bot = getattr(message, "sender_business_bot", None) is not None
    if sender is None and not via_bot:
        return Skipped("no_sender")
    edited = _unix(getattr(message, "edit_date", None)) if first is None else None
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    reply = getattr(message, "reply_to_message", None)
    return MessageSeen(
        connection=ConnectionId(cid), chat=ChatId(chat_id), chat_name=_name(chat), message=MessageId(message_id),
        sender=_opt_int(getattr(sender, "id", None)), sender_name=_name(sender) if sender is not None else "",
        via_bot=via_bot, date=date, edited=edited or 0, text=text if isinstance(text, str) else None,
        reply_to=MessageId(reply.message_id) if _opt_int(getattr(reply, "message_id", None)) is not None else None,
        forwarded=getattr(message, "forward_origin", None) is not None,
        offline=bool(getattr(message, "is_from_offline", False)),
        attachments=attachments_of(message), media_group=_opt_str(getattr(message, "media_group_id", None)))


def author_of(event: MessageSeen, owner: OwnerId) -> Author:
    return Author.OWNER if event.via_bot or event.sender == owner else Author.CONTACT


# --- the store --------------------------------------------------------------------------------------------


class InboxStoreError(RuntimeError):
    """The inbox file cannot be used as this version expects; nothing is written to it."""


@dataclass(frozen=True)
class Version:
    edited: int
    text: Optional[str]


@dataclass(frozen=True)
class Record:
    owner: OwnerId
    chat: ChatId
    message: MessageId
    date: int
    author: Author
    name: str
    reply_to: Optional[MessageId]
    forwarded: bool
    offline: bool
    media: tuple[Attachment, ...]
    media_group: Optional[str]
    deleted: Optional[int]
    versions: tuple[Version, ...]


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS updates (bot TEXT NOT NULL, id INTEGER NOT NULL, at INTEGER NOT NULL, "
    "PRIMARY KEY (bot, id))",
    "CREATE INDEX IF NOT EXISTS updates_by_time ON updates (at)",
    "CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, "
    "enabled INTEGER NOT NULL, rights TEXT NOT NULL, updated INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS chats (owner INTEGER NOT NULL, chat INTEGER NOT NULL, name TEXT NOT NULL, "
    "PRIMARY KEY (owner, chat))",
    "CREATE TABLE IF NOT EXISTS records (owner INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, "
    "date INTEGER NOT NULL, author TEXT NOT NULL, name TEXT NOT NULL, reply_to INTEGER, "
    "forwarded INTEGER NOT NULL, offline INTEGER NOT NULL, media TEXT NOT NULL, media_group TEXT, deleted INTEGER, "
    "PRIMARY KEY (owner, chat, message))",
    "CREATE INDEX IF NOT EXISTS records_by_date ON records (date)",
    # One version per distinct content at one edit time: edits within the same second stay apart.
    "CREATE TABLE IF NOT EXISTS versions (owner INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, "
    "edited INTEGER NOT NULL, digest TEXT NOT NULL, text TEXT, media TEXT NOT NULL, "
    "PRIMARY KEY (owner, chat, message, edited, digest))",
    "CREATE TABLE IF NOT EXISTS changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
    "owner INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, at INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS changes_by_record ON changes (owner, chat, message)",
)
_RECORD = "owner = ? AND chat = ? AND message = ?"
Key = tuple[int, int, int]


def _media_json(attachments: Iterable[Attachment]) -> str:
    return json.dumps([{k: v for k, v in vars(a).items() if v is not None} for a in attachments],
                      ensure_ascii=False, sort_keys=True)


class _Write:
    """One write transaction (``BEGIN IMMEDIATE``): committed when the block ends, rolled back on any error."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, kind: Any, *_: Any) -> None:
        self.db.execute("COMMIT" if kind is None else "ROLLBACK")


class InboxStore:
    """One SQLite file, opened per operation (the gateway's worker threads and the CLI may use it at once).

    The gateway is the only writer of records; a purge from elsewhere removes only what has expired by the same
    rule, so running it twice, or next to the gateway, ends in the same state.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def _open(self, create: bool) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            if not create:
                return None
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        if self.path.stat().st_mode & 0o077:
            os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            db.execute("PRAGMA secure_delete = ON")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone():
                row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
                if row is None or row[0] != SCHEMA_VERSION:
                    raise InboxStoreError(f"the inbox file has schema {row[0] if row else None!r}; this version "
                                          f"reads {SCHEMA_VERSION!r}")
            elif db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                raise InboxStoreError("the inbox file holds tables of something else")
            else:  # a new file: nothing in it is touched before this point
                db.execute("PRAGMA journal_mode = DELETE")
                with _Write(db):
                    for statement in _SCHEMA:
                        db.execute(statement)
                    db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
        except BaseException:
            db.close()
            raise
        return db

    # --- connections -------------------------------------------------------------------------------------

    def save_connection(self, seen: ConnectionSeen, update: Optional[UpdateKey], now: int) -> bool:
        """Keep a connection's owner, state and rights; False when the update was applied before."""
        db = self._open(create=True)
        try:
            with _Write(db):
                if not self._first_time(db, update, now):
                    return False
                db.execute("INSERT INTO connections (id, owner, enabled, rights, updated) VALUES (?, ?, ?, ?, ?) "
                           "ON CONFLICT (id) DO UPDATE SET owner = excluded.owner, enabled = excluded.enabled, "
                           "rights = excluded.rights, updated = excluded.updated",
                           (seen.connection, seen.owner, int(seen.enabled), json.dumps(seen.rights, sort_keys=True),
                            now))
                return True
        finally:
            db.close()

    def connection(self, connection: ConnectionId) -> Optional[tuple[OwnerId, bool]]:
        db = self._open(create=False)
        if db is None:
            return None
        try:
            row = db.execute("SELECT owner, enabled FROM connections WHERE id = ?", (connection,)).fetchone()
        finally:
            db.close()
        return (OwnerId(row[0]), bool(row[1])) if row else None

    # --- records -----------------------------------------------------------------------------------------

    def keep_message(self, seen: MessageSeen, owner: OwnerId, limits: Limits, update: Optional[UpdateKey],
                     now: int) -> Optional[Change]:
        """Keep one message or one edit of it; the change it made, or None when it changed nothing: an update
        applied before, a connection not enabled for ``owner``, a message past the limits, or content kept
        already."""
        key: Key = (owner, seen.chat, seen.message)
        db = self._open(create=True)
        try:
            with _Write(db):
                if not self._first_time(db, update, now) or not self._enabled(db, seen.connection, owner):
                    return None
                self._record_limits(db, limits)
                self._remove(db, self._expired(db, limits, now))
                if seen.date < now - limits.retention_secs:
                    return None
                new = db.execute(f"SELECT 1 FROM records WHERE {_RECORD}", key).fetchone() is None
                if new and self._would_be_evicted(db, seen, owner, limits):
                    return None
                media = _media_json(seen.attachments)
                if new:
                    db.execute("INSERT INTO records (owner, chat, message, date, author, name, reply_to, forwarded, "
                               "offline, media, media_group, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                               (*key, seen.date, author_of(seen, owner).value, seen.sender_name, seen.reply_to,
                                int(seen.forwarded), int(seen.offline), media, seen.media_group))
                newest = db.execute(f"SELECT MAX(edited) FROM versions WHERE {_RECORD}", key).fetchone()[0]
                digest = hashlib.sha256(json.dumps([seen.text, media]).encode()).hexdigest()
                added = db.execute("INSERT OR IGNORE INTO versions (owner, chat, message, edited, digest, text, media) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (*key, seen.edited, digest, seen.text, media)).rowcount == 1
                if not new and not added:
                    return None
                change = Change.INSERT if new else Change.EDIT
                if change is Change.EDIT and (newest is None or seen.edited >= newest):
                    # The newest edit says what the message carries now; an older one arriving late is history.
                    db.execute(f"UPDATE records SET media = ?, media_group = ? WHERE {_RECORD}",
                               (media, seen.media_group, *key))
                db.execute("INSERT INTO chats (owner, chat, name) VALUES (?, ?, ?) ON CONFLICT (owner, chat) "
                           "DO UPDATE SET name = excluded.name", (owner, seen.chat, seen.chat_name))
                self._change(db, change, key, now)
                self._trim_versions(db, key)
                self._remove(db, self._over_cap(db, limits, chat=(owner, seen.chat)))
                return change
        finally:
            db.close()

    def mark_deleted(self, deleted: MessagesDeleted, owner: OwnerId, limits: Limits, update: Optional[UpdateKey],
                     now: int) -> int:
        """Mark the messages deleted, keeping their text until they expire; how many were marked. A message the
        inbox never had is ignored. Telegram does not say who deleted them."""
        db = self._open(create=False)
        if db is None:
            return 0
        try:
            with _Write(db):
                if not self._first_time(db, update, now) or not self._enabled(db, deleted.connection, owner):
                    return 0
                self._remove(db, self._expired(db, limits, now))
                marked = 0
                for message in deleted.messages:
                    key: Key = (owner, deleted.chat, message)
                    if db.execute(f"UPDATE records SET deleted = ? WHERE {_RECORD} AND deleted IS NULL",
                                  (now, *key)).rowcount:
                        self._change(db, Change.DELETE, key, now)
                        marked += 1
                return marked
        finally:
            db.close()

    def purge(self, limits: Optional[Limits], now: int) -> int:
        """Remove every record past the age or the per-chat limit; how many were removed. Without ``limits``
        (intake switched off) it applies the limits the file was last written under, else the defaults."""
        db = self._open(create=False)
        if db is None:
            return 0
        try:
            with _Write(db):
                limits = limits or self._recorded_limits(db)
                gone = self._expired(db, limits, now) + self._over_cap(db, limits)
                self._remove(db, gone)
                db.execute("DELETE FROM updates WHERE at < ?", (now - limits.retention_secs,))
                return len(set(gone))
        finally:
            db.close()

    # --- inside a transaction ----------------------------------------------------------------------------

    @staticmethod
    def _first_time(db: sqlite3.Connection, update: Optional[UpdateKey], now: int) -> bool:
        """Claim the update in this transaction; False when it was applied before (a redelivery, a restart)."""
        if update is None:
            return True
        return db.execute("INSERT OR IGNORE INTO updates (bot, id, at) VALUES (?, ?, ?)",
                          (update[0], update[1], now)).rowcount == 1

    @staticmethod
    def _enabled(db: sqlite3.Connection, connection: ConnectionId, owner: OwnerId) -> bool:
        row = db.execute("SELECT owner, enabled FROM connections WHERE id = ?", (connection,)).fetchone()
        return row is not None and row[0] == owner and bool(row[1])

    @staticmethod
    def _record_limits(db: sqlite3.Connection, limits: Limits) -> None:
        for name, value in (("retention_hours", limits.retention_hours), ("max_per_chat", limits.max_per_chat)):
            db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                       "value = excluded.value", (name, str(value)))

    @staticmethod
    def _recorded_limits(db: sqlite3.Connection) -> Limits:
        values = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('retention_hours', 'max_per_chat')"))
        try:
            return Limits(int(values["retention_hours"]), int(values["max_per_chat"]))
        except (KeyError, ValueError):
            return DEFAULT_LIMITS

    @staticmethod
    def _expired(db: sqlite3.Connection, limits: Limits, now: int) -> list[Key]:
        return db.execute("SELECT owner, chat, message FROM records WHERE date < ?",
                          (now - limits.retention_secs,)).fetchall()

    @staticmethod
    def _over_cap(db: sqlite3.Connection, limits: Limits, chat: Optional[tuple[int, int]] = None) -> list[Key]:
        where, args = ("WHERE owner = ? AND chat = ?", chat) if chat else ("", ())
        return db.execute(
            "SELECT owner, chat, message FROM (SELECT owner, chat, message, ROW_NUMBER() OVER "
            f"(PARTITION BY owner, chat ORDER BY date DESC, message DESC) AS n FROM records {where}) WHERE n > ?",
            (*args, limits.max_per_chat)).fetchall()

    @staticmethod
    def _would_be_evicted(db: sqlite3.Connection, seen: MessageSeen, owner: OwnerId, limits: Limits) -> bool:
        """A new record older than every one a full chat keeps would be removed at once: it is not kept."""
        oldest_kept = db.execute("SELECT date, message FROM records WHERE owner = ? AND chat = ? "
                                 "ORDER BY date DESC, message DESC LIMIT 1 OFFSET ?",
                                 (owner, seen.chat, limits.max_per_chat - 1)).fetchone()
        return oldest_kept is not None and (seen.date, seen.message) < tuple(oldest_kept)

    def _change(self, db: sqlite3.Connection, kind: Change, key: Key, now: int) -> None:
        """Append a change; a record's change history is bounded as its versions are."""
        db.execute("INSERT INTO changes (kind, owner, chat, message, at) VALUES (?, ?, ?, ?, ?)",
                   (kind.value, *key, now))
        old = [seq for (seq,) in db.execute(f"SELECT seq FROM changes WHERE {_RECORD} ORDER BY seq DESC "
                                            "LIMIT -1 OFFSET ?", (*key, MAX_VERSIONS + 1))]
        if old:
            self._removed(db, max(old))
            db.executemany("DELETE FROM changes WHERE seq = ?", [(seq,) for seq in old])

    @staticmethod
    def _trim_versions(db: sqlite3.Connection, key: Key) -> None:
        db.execute(f"DELETE FROM versions WHERE rowid IN (SELECT rowid FROM versions WHERE {_RECORD} "
                   "ORDER BY edited DESC, rowid DESC LIMIT -1 OFFSET ?) AND rowid != (SELECT rowid FROM versions "
                   f"WHERE {_RECORD} ORDER BY edited, rowid LIMIT 1)", (*key, MAX_VERSIONS - 1, *key))

    @staticmethod
    def _removed(db: sqlite3.Connection, seq: int) -> None:
        """Readers following the change sequence learn that changes up to ``seq`` may be gone."""
        db.execute("INSERT INTO meta (key, value) VALUES ('removed_through', ?) ON CONFLICT (key) DO UPDATE SET "
                   "value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))", (str(seq),))

    def _remove(self, db: sqlite3.Connection, keys: list[Key]) -> None:
        for key in dict.fromkeys(tuple(k) for k in keys):
            top = db.execute(f"SELECT MAX(seq) FROM changes WHERE {_RECORD}", key).fetchone()[0]
            if top is not None:
                self._removed(db, top)
            for table in ("changes", "versions", "records"):
                db.execute(f"DELETE FROM {table} WHERE {_RECORD}", key)
        if keys:
            db.execute("DELETE FROM chats WHERE NOT EXISTS (SELECT 1 FROM records WHERE records.owner = "
                       "chats.owner AND records.chat = chats.chat)")

    # --- reading: each call reads one snapshot ----------------------------------------------------------

    def _snapshot(self) -> Optional[sqlite3.Connection]:
        """A connection inside a read transaction, or None when there is no file; the caller closes it."""
        db = self._open(create=False)
        if db is not None:
            db.execute("BEGIN")
        return db

    @staticmethod
    def _build(db: sqlite3.Connection, rows: list[tuple]) -> list[Record]:
        records = []
        for o, c, m, d, a, n, r, f, off, media, g, dl in rows:
            versions = tuple(Version(e, x) for e, x in db.execute(
                f"SELECT edited, text FROM versions WHERE {_RECORD} ORDER BY edited, rowid", (o, c, m)))
            records.append(Record(owner=OwnerId(o), chat=ChatId(c), message=MessageId(m), date=d, author=Author(a),
                                  name=n, reply_to=MessageId(r) if r is not None else None, forwarded=bool(f),
                                  offline=bool(off), media=tuple(Attachment(**x) for x in json.loads(media)),
                                  media_group=g, deleted=dl, versions=versions))
        return records

    _COLUMNS = ("SELECT owner, chat, message, date, author, name, reply_to, forwarded, offline, media, media_group, "
                "deleted FROM records")

    def records(self) -> list[Record]:
        """Every record, per chat in Telegram's order (date, then message id)."""
        db = self._snapshot()
        if db is None:
            return []
        try:
            return self._build(db, db.execute(f"{self._COLUMNS} ORDER BY owner, chat, date, message").fetchall())
        finally:
            db.close()

    def record(self, owner: OwnerId, chat: ChatId, message: MessageId) -> Optional[Record]:
        db = self._snapshot()
        if db is None:
            return None
        try:
            found = self._build(db, db.execute(f"{self._COLUMNS} WHERE {_RECORD}", (owner, chat, message)).fetchall())
        finally:
            db.close()
        return found[0] if found else None

    def window(self, owner: OwnerId, chat: ChatId, before: Optional[tuple[int, int]],
               limit: int) -> tuple[list[Record], bool]:
        """The newest ``limit`` records of one chat before ``(date, message)``, oldest first, and whether older
        ones remain."""
        db = self._snapshot()
        if db is None:
            return [], False
        try:
            where, args = "owner = ? AND chat = ?", [owner, chat]
            if before is not None:
                where += " AND (date, message) < (?, ?)"
                args += list(before)
            rows = db.execute(f"{self._COLUMNS} WHERE {where} ORDER BY date DESC, message DESC LIMIT ?",
                              (*args, limit + 1)).fetchall()
            return self._build(db, list(reversed(rows[:limit]))), len(rows) > limit
        finally:
            db.close()

    def changes_since(self, since: int, limit: int) -> tuple[list[tuple[int, Change, int, Record]], bool, int]:
        """Changes after ``since`` in order, each with its record as it is now; whether more remain; and the
        highest change that may be gone (``removed_through``)."""
        db = self._snapshot()
        if db is None:
            return [], False, 0
        try:
            rows = db.execute("SELECT seq, kind, at, owner, chat, message FROM changes WHERE seq > ? ORDER BY seq "
                              "LIMIT ?", (since, limit + 1)).fetchall()
            items = []
            for seq, kind, at, o, c, m in rows[:limit]:
                [record] = self._build(db, db.execute(f"{self._COLUMNS} WHERE {_RECORD}", (o, c, m)).fetchall())
                items.append((seq, Change(kind), at, record))
            removed = db.execute("SELECT value FROM meta WHERE key = 'removed_through'").fetchone()
            return items, len(rows) > limit, int(removed[0]) if removed else 0
        finally:
            db.close()

    def chats(self) -> list[tuple[OwnerId, ChatId, str, int, int]]:
        """Each chat: owner, chat, the other person's name, the last record's date and the record count."""
        db = self._snapshot()
        if db is None:
            return []
        try:
            rows = db.execute("SELECT c.owner, c.chat, c.name, MAX(r.date), COUNT(r.message) FROM chats c JOIN "
                              "records r ON r.owner = c.owner AND r.chat = c.chat GROUP BY c.owner, c.chat "
                              "ORDER BY MAX(r.date) DESC, c.chat").fetchall()
        finally:
            db.close()
        return [(OwnerId(o), ChatId(c), n, d, k) for o, c, n, d, k in rows]

    def status(self) -> "StoreStatus":
        db = self._snapshot()
        if db is None:
            return StoreStatus(connections=(), records=0, chats=0, oldest=None, limits=None)
        try:
            connections = tuple((ConnectionId(i), OwnerId(o), bool(e), json.loads(r)) for i, o, e, r in db.execute(
                "SELECT id, owner, enabled, rights FROM connections ORDER BY updated, id"))
            records, oldest = db.execute("SELECT COUNT(*), MIN(date) FROM records").fetchone()
            chats = db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT owner, chat FROM records)").fetchone()[0]
            recorded = db.execute("SELECT COUNT(*) FROM meta WHERE key IN ('retention_hours', 'max_per_chat')")
            limits = self._recorded_limits(db) if recorded.fetchone()[0] == 2 else None
        finally:
            db.close()
        return StoreStatus(connections=connections, records=records, chats=chats, oldest=oldest, limits=limits)

    def changes(self) -> list[tuple[int, Change, OwnerId, ChatId, MessageId]]:
        db = self._snapshot()
        if db is None:
            return []
        try:
            rows = db.execute("SELECT seq, kind, owner, chat, message FROM changes ORDER BY seq").fetchall()
        finally:
            db.close()
        return [(s, Change(k), OwnerId(o), ChatId(c), MessageId(m)) for s, k, o, c, m in rows]

    def removed_through(self) -> int:
        return self.changes_since(2**62, 1)[2]

    def rights(self, connection: ConnectionId) -> dict[str, bool]:
        return next((r for i, _o, _e, r in self.status().connections if i == connection), {})


@dataclass(frozen=True)
class StoreStatus:
    connections: tuple[tuple[ConnectionId, OwnerId, bool, dict[str, bool]], ...]
    records: int
    chats: int
    oldest: Optional[int]
    limits: Optional[Limits]  # the limits the file was last written under


# --- the adapter ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Inbox:
    settings: Optional[InboxSettings]  # None: intake is off; an existing file still expires
    store: InboxStore


def _business_handler(callback: Any) -> Any:
    """A PTB handler that matches business updates only, so a group it shares stays open to every other update."""
    from telegram.ext import BaseHandler

    class BusinessUpdateHandler(BaseHandler):
        def check_update(self, update: object) -> bool:
            return business_kind(update) is not None

    return BusinessUpdateHandler(callback)


class BusinessInboxMixin:
    """Business updates into the inbox; listed first among this plugin's mixins."""

    def _inbox(self) -> Inbox:
        """The inbox of this adapter, its settings resolved once; ``settings`` None when intake is off."""
        state = self.__dict__.get("_hermes_inbox")
        if state is None:
            from hermes_cli.profiles import get_active_profile_name

            try:
                settings = inbox_settings(self.config.extra or {}, os.environ, get_active_profile_name())
            except InboxConfigError as exc:
                logger.error("[Telegram] Business inbox intake is off: %s", exc)
                settings = None
            state = Inbox(settings, InboxStore(Path(self._update_receipt_dir) / STORE_NAME))
            self.__dict__["_hermes_inbox"] = state
        return state

    @property
    def _inbox_outcomes(self) -> Counter[tuple[str, str]]:
        return self.__dict__.setdefault("_hermes_inbox_outcomes", Counter())

    @property
    def _ignored_connections(self) -> set[ConnectionId]:
        """Connections of owners not configured, in memory only: the file holds nothing of theirs."""
        return self.__dict__.setdefault("_hermes_inbox_ignored", set())

    def _connection_lock(self, connection: ConnectionId) -> asyncio.Lock:
        """One at a time per connection: a lookup and a connection update cannot interleave, and two chats that
        miss the same connection ask Telegram once."""
        return self.__dict__.setdefault("_hermes_inbox_locks", {}).setdefault(connection, asyncio.Lock())

    # --- handlers ----------------------------------------------------------------------------------------

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        handler = _business_handler(self._on_business_update)
        self.__dict__["_hermes_inbox_handler"] = handler
        app.add_handler(handler, group=BUSINESS_GROUP)
        self._keep_business_handler_first(app)
        inbox = self._inbox()
        if inbox.settings is not None or inbox.store.exists():
            self._start_inbox_purge(app)

    def _wire_plugin_handlers(self, native: Any = None) -> None:
        super()._wire_plugin_handlers(native)
        self._keep_business_handler_first(native)  # a plugin wired later lands in the group, after it

    def _keep_business_handler_first(self, app: Any) -> None:
        """PTB runs only the first handler of a group that matches: the business handler stays first in its own."""
        handler = self.__dict__.get("_hermes_inbox_handler")
        group = (getattr(app, "handlers", None) or {}).get(BUSINESS_GROUP)
        if handler is not None and isinstance(group, list) and handler in group and group[0] is not handler:
            group.remove(handler)
            group.insert(0, handler)

    async def _on_business_update(self, update: Any, context: Any) -> None:
        """Every business update ends here: kept or dropped, it reaches no later handler."""
        from telegram.ext import ApplicationHandlerStop

        kind = business_kind(update) or "unknown"
        try:
            outcome = await self._keep_business_update(update)
        except InboxStoreError as exc:
            logger.error("[Telegram] Business inbox cannot use its file: %s", exc)
            outcome = "store_error"
        except Exception as exc:
            logger.error("[Telegram] Business inbox did not keep a %s update (%s)", kind, type(exc).__name__)
            outcome = "error"
        self._inbox_outcomes[(kind, outcome)] += 1
        logger.debug("[Telegram] Business %s: %s", kind, outcome)
        raise ApplicationHandlerStop

    async def _keep_business_update(self, update: Any) -> str:
        inbox = self._inbox()
        settings = inbox.settings
        if settings is None:
            return "off"
        event = parse_business_update(update)
        if isinstance(event, Skipped):
            return event.reason
        now = int(_clock())
        update_id = _opt_int(getattr(update, "update_id", None))
        key: Optional[UpdateKey] = (self._bot_identity(), update_id) if update_id is not None else None
        if isinstance(event, ConnectionSeen):
            async with self._connection_lock(event.connection):
                if event.owner != settings.owner:
                    self._ignored_connections.add(event.connection)
                    return "ignored"
                self._ignored_connections.discard(event.connection)
                saved = await asyncio.to_thread(inbox.store.save_connection, event, key, now)
            return "connection" if saved else "unchanged"
        owner = await self._connection_owner(inbox, settings, event.connection, now)
        if owner is None:
            return "ignored" if event.connection in self._ignored_connections else "unknown_connection"
        if isinstance(event, MessagesDeleted):
            marked = await asyncio.to_thread(inbox.store.mark_deleted, event, owner, settings.limits, key, now)
            return "deleted" if marked else "unchanged"
        change = await asyncio.to_thread(inbox.store.keep_message, event, owner, settings.limits, key, now)
        return change.value if change is not None else "unchanged"

    async def _connection_owner(self, inbox: Inbox, settings: InboxSettings, connection: ConnectionId,
                                now: int) -> Optional[OwnerId]:
        """The configured owner of the connection, or None. A connection the inbox has not seen is asked of
        Telegram once: after an update missed while the gateway was down, or because updates of different chats
        run concurrently and a connection update has no chat, so a message can come first. Whether it is
        enabled is checked again inside the write."""
        async with self._connection_lock(connection):
            if connection in self._ignored_connections:
                return None
            known = await asyncio.to_thread(inbox.store.connection, connection)
            if known is not None:
                owner, _enabled = known
                return owner if owner == settings.owner else None
            try:
                reply = await self._bot.get_business_connection(business_connection_id=connection)
            except Exception as exc:
                logger.warning("[Telegram] Business inbox could not look up a connection (%s)", type(exc).__name__)
                return None
            seen = parse_business_update(_ConnectionUpdate(reply))
            if not isinstance(seen, ConnectionSeen) or seen.connection != connection:
                return None
            if seen.owner != settings.owner:
                self._ignored_connections.add(connection)
                return None
            await asyncio.to_thread(inbox.store.save_connection, seen, None, now)
            return seen.owner

    # --- expiry while the gateway runs -------------------------------------------------------------------

    def _start_inbox_purge(self, app: Any) -> None:
        current = self.__dict__.get("_hermes_inbox_purge")
        if current is not None:
            task, for_app = current
            if for_app is app and not task.done():
                return
            task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._purge_inbox_periodically(app), name="telegram-business-inbox-purge")
        self.__dict__["_hermes_inbox_purge"] = (task, app)

    async def _purge_inbox_periodically(self, app: Any) -> None:
        """Expired records leave the file within ``PURGE_EVERY_SECS`` while this adapter's Application runs,
        also when no business update arrives and when intake is off; the task ends with the Application."""
        while True:
            await asyncio.sleep(PURGE_EVERY_SECS)
            if getattr(self, "_app", None) is not app:
                return
            inbox = self._inbox()
            limits = inbox.settings.limits if inbox.settings is not None else None
            try:
                await asyncio.to_thread(inbox.store.purge, limits, int(_clock()))
            except Exception as exc:
                logger.warning("[Telegram] Business inbox purge failed (%s)", type(exc).__name__)


@dataclass(frozen=True)
class _ConnectionUpdate:
    """A ``getBusinessConnection`` reply read as the update that would have carried it."""

    business_connection: Any
