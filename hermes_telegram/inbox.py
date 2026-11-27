"""The Business inbox: the owner's private chats, kept through Telegram Business (docs/business-inbox.md; slice 1).

The bot connected to the owner's account receives ``business_connection``, ``business_message``,
``edited_business_message`` and ``deleted_business_messages`` (core.telegram.org/bots/api, Update). A handler in
the first group takes every one of them and stops it there, so no other handler, and no agent, sees it. What it
keeps goes into one SQLite file in the adapter's Hermes home, for at most ``retention_hours`` by the message's
Telegram date and at most ``max_messages_per_chat`` records per chat.

- Nothing on this path calls a Bot API method but ``getBusinessConnection``: no send, edit, read mark or deletion.
- Updates of a connection whose owner is not configured store nothing; such a connection is remembered in memory.
- The author is set from ids: ``owner`` when the sender is the connection's owner (or a bot sent it on the
  owner's behalf, ``sender_business_bot``), ``contact`` otherwise.
- Media is kept as metadata. Contacts, locations, polls and the like keep their kind only.
- Purged rows do not linger in the file: ``secure_delete`` is on and the journal is not WAL.
- Logs name update kinds and outcomes, never text, names or ids.
"""

import asyncio
import json
import logging
import os
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

STORE_NAME = "telegram_business_inbox.sqlite3"
SCHEMA_VERSION = "1"
DEFAULT_RETENTION_HOURS = 72
DEFAULT_MAX_PER_CHAT = 1000
ENV_RETENTION_HOURS = "HERMES_TELEGRAM_INBOX_RETENTION_HOURS"
ENV_MAX_PER_CHAT = "HERMES_TELEGRAM_INBOX_MAX_PER_CHAT"
MAX_VERSIONS = 50  # per record: the first version and the newest edits; a burst of edits cannot grow it further
PURGE_EVERY_SECS = 600.0
BUSINESS_GROUP = -20  # before every other handler group; -10 is the Stop button's TypeHandler (preview.py)
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


# --- settings ---------------------------------------------------------------------------------------------


class InboxConfigError(ValueError):
    """``business_inbox`` in the platform's ``extra`` cannot be used; the inbox stays off."""


@dataclass(frozen=True)
class InboxSettings:
    owners: frozenset[OwnerId]
    retention_hours: int
    max_per_chat: int

    @property
    def retention_secs(self) -> int:
        return self.retention_hours * 3600


def _positive_int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise InboxConfigError(f"{what} must be a positive integer")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value < 1:
        raise InboxConfigError(f"{what} must be a positive integer")
    return value


def inbox_settings(extra: Mapping[str, Any], environ: Mapping[str, str], profile: str) -> Optional[InboxSettings]:
    """The inbox's settings from the platform's ``extra`` and the environment, or None when it is not configured.

    ``owners`` maps each owner's Telegram user id to the profile whose home keeps their chats. This version keeps
    them only in the adapter's own profile, ``profile``; any other target is an error.
    """
    raw = extra.get("business_inbox")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise InboxConfigError("business_inbox must be a mapping")
    owners_raw = raw.get("owners")
    if not isinstance(owners_raw, Mapping) or not owners_raw:
        raise InboxConfigError("business_inbox.owners must map at least one Telegram user id to a profile")
    owners: set[OwnerId] = set()
    for key, target in owners_raw.items():
        owners.add(OwnerId(_positive_int(key, "an owner id in business_inbox.owners")))
        if target != profile:
            raise InboxConfigError(f"business_inbox.owners maps an owner to profile {target!r}; this version "
                                   f"keeps the inbox only in the adapter's own profile {profile!r}")
    retention = environ.get(ENV_RETENTION_HOURS) or raw.get("retention_hours", DEFAULT_RETENTION_HOURS)
    max_per_chat = environ.get(ENV_MAX_PER_CHAT) or raw.get("max_messages_per_chat", DEFAULT_MAX_PER_CHAT)
    return InboxSettings(owners=frozenset(owners),
                         retention_hours=_positive_int(retention, "retention_hours"),
                         max_per_chat=_positive_int(max_per_chat, "max_messages_per_chat"))


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


def parse_business_update(update: Any) -> BusinessEvent | Skipped:
    """The boundary: one PTB business update as the inbox's own event, or why it is skipped."""
    connection = getattr(update, "business_connection", None)
    if connection is not None:
        owner = _opt_int(getattr(getattr(connection, "user", None), "id", None))
        cid = _opt_str(getattr(connection, "id", None))
        if owner is None or cid is None:
            return Skipped("malformed")
        rights = getattr(connection, "rights", None)
        granted = {k: bool(getattr(rights, k, False)) for k in ("can_reply", "can_read_messages")}
        return ConnectionSeen(ConnectionId(cid), OwnerId(owner), bool(getattr(connection, "is_enabled", False)),
                              granted)
    deleted = getattr(update, "deleted_business_messages", None)
    if deleted is not None:
        cid = _opt_str(getattr(deleted, "business_connection_id", None))
        chat = _opt_int(getattr(getattr(deleted, "chat", None), "id", None))
        if cid is None or chat is None:
            return Skipped("malformed")
        ids = tuple(MessageId(i) for i in (getattr(deleted, "message_ids", None) or ()) if _opt_int(i) is not None)
        return MessagesDeleted(ConnectionId(cid), ChatId(chat), ids)
    message = getattr(update, "business_message", None) or getattr(update, "edited_business_message", None)
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
    edited = _unix(getattr(message, "edit_date", None)) if getattr(update, "business_message", None) is None else None
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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, enabled INTEGER NOT NULL, rights TEXT NOT NULL,
    updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chats (
    owner INTEGER NOT NULL, chat INTEGER NOT NULL, name TEXT NOT NULL, PRIMARY KEY (owner, chat));
CREATE TABLE IF NOT EXISTS records (
    owner INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, date INTEGER NOT NULL,
    author TEXT NOT NULL, name TEXT NOT NULL, reply_to INTEGER, forwarded INTEGER NOT NULL,
    offline INTEGER NOT NULL, media TEXT NOT NULL, media_group TEXT, deleted INTEGER,
    PRIMARY KEY (owner, chat, message));
CREATE INDEX IF NOT EXISTS records_by_date ON records (date);
CREATE TABLE IF NOT EXISTS versions (
    owner INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, edited INTEGER NOT NULL, text TEXT,
    PRIMARY KEY (owner, chat, message, edited));
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, owner INTEGER NOT NULL, chat INTEGER NOT NULL,
    message INTEGER NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS changes_by_record ON changes (owner, chat, message);
"""


def _media_json(attachments: Iterable[Attachment]) -> str:
    return json.dumps([{k: v for k, v in vars(a).items() if v is not None} for a in attachments],
                      ensure_ascii=False, sort_keys=True)


class InboxStore:
    """One SQLite file, opened per operation (the gateway's worker threads and the CLI may use it at once).

    The gateway is the only writer of records; a purge from elsewhere removes only what has expired by the same
    rule, so running it twice, or next to the gateway, ends in the same state.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

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
            db.execute("PRAGMA journal_mode = DELETE")
            db.executescript(_SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if row is None:
                db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise InboxStoreError(f"inbox file has schema {row[0]!r}, this version reads {SCHEMA_VERSION!r}")
        except BaseException:
            db.close()
            raise
        return db

    # --- connections -------------------------------------------------------------------------------------

    def save_connection(self, seen: ConnectionSeen, now: int) -> None:
        db = self._open(create=True)
        try:
            db.execute("INSERT INTO connections (id, owner, enabled, rights, updated) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT (id) DO UPDATE SET owner = excluded.owner, enabled = excluded.enabled, "
                       "rights = excluded.rights, updated = excluded.updated",
                       (seen.connection, seen.owner, int(seen.enabled), json.dumps(seen.rights, sort_keys=True),
                        now))
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

    def keep_message(self, seen: MessageSeen, owner: OwnerId, settings: InboxSettings, now: int) -> Optional[Change]:
        """Keep one message or one edit of it; the change it made, or None when it changed nothing."""
        if seen.date < now - settings.retention_secs:
            return None
        key = (owner, seen.chat, seen.message)
        db = self._open(create=True)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO chats (owner, chat, name) VALUES (?, ?, ?) "
                       "ON CONFLICT (owner, chat) DO UPDATE SET name = excluded.name", (owner, seen.chat,
                                                                                        seen.chat_name))
            fields = (seen.date, author_of(seen, owner).value, seen.sender_name, seen.reply_to, int(seen.forwarded),
                      int(seen.offline), _media_json(seen.attachments), seen.media_group)
            inserted = db.execute(
                "INSERT OR IGNORE INTO records (owner, chat, message, date, author, name, reply_to, forwarded, "
                "offline, media, media_group, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (*key, *fields)).rowcount == 1
            added = db.execute("INSERT OR IGNORE INTO versions (owner, chat, message, edited, text) "
                               "VALUES (?, ?, ?, ?, ?)", (*key, seen.edited, seen.text)).rowcount == 1
            change = Change.INSERT if inserted else Change.EDIT if added else None
            if change is Change.EDIT and seen.edited:
                # An edit may change what the message carries; the original date and author stay.
                db.execute("UPDATE records SET media = ?, media_group = ? WHERE owner = ? AND chat = ? AND "
                           "message = ?", (fields[6], fields[7], *key))
            if change is not None:
                db.execute("INSERT INTO changes (kind, owner, chat, message, at) VALUES (?, ?, ?, ?, ?)",
                           (change.value, *key, now))
                self._trim_versions(db, key)
                self._remove(db, self._over_cap(db, settings, chat=(owner, seen.chat)))
            self._remove(db, self._expired(db, settings, now))
            db.execute("COMMIT")
            return change
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def mark_deleted(self, deleted: MessagesDeleted, owner: OwnerId, now: int) -> int:
        """Mark the messages deleted, keeping their text until they expire; how many were marked. A message the
        inbox never had is ignored."""
        db = self._open(create=False)
        if db is None:
            return 0
        try:
            db.execute("BEGIN IMMEDIATE")
            marked = 0
            for message in deleted.messages:
                key = (owner, deleted.chat, message)
                if db.execute("UPDATE records SET deleted = ? WHERE owner = ? AND chat = ? AND message = ? "
                              "AND deleted IS NULL", (now, *key)).rowcount:
                    db.execute("INSERT INTO changes (kind, owner, chat, message, at) VALUES (?, ?, ?, ?, ?)",
                               (Change.DELETE.value, *key, now))
                    marked += 1
            db.execute("COMMIT")
            return marked
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def purge(self, settings: InboxSettings, now: int) -> int:
        """Remove every record past the age or the per-chat limit; how many were removed."""
        db = self._open(create=False)
        if db is None:
            return 0
        try:
            db.execute("BEGIN IMMEDIATE")
            gone = self._expired(db, settings, now) + self._over_cap(db, settings)
            self._remove(db, gone)
            db.execute("COMMIT")
            return len(set(gone))
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _expired(db: sqlite3.Connection, settings: InboxSettings, now: int) -> list[tuple[int, int, int]]:
        return db.execute("SELECT owner, chat, message FROM records WHERE date < ?",
                          (now - settings.retention_secs,)).fetchall()

    @staticmethod
    def _over_cap(db: sqlite3.Connection, settings: InboxSettings,
                  chat: Optional[tuple[int, int]] = None) -> list[tuple[int, int, int]]:
        where, args = ("WHERE owner = ? AND chat = ?", chat) if chat else ("", ())
        return db.execute(
            "SELECT owner, chat, message FROM (SELECT owner, chat, message, ROW_NUMBER() OVER "
            f"(PARTITION BY owner, chat ORDER BY message DESC) AS n FROM records {where}) WHERE n > ?",
            (*args, settings.max_per_chat)).fetchall()

    @staticmethod
    def _trim_versions(db: sqlite3.Connection, key: tuple[int, int, int]) -> None:
        db.execute("DELETE FROM versions WHERE owner = ? AND chat = ? AND message = ? AND edited IN ("
                   "SELECT edited FROM versions WHERE owner = ? AND chat = ? AND message = ? "
                   "ORDER BY edited DESC LIMIT -1 OFFSET ?) AND edited != (SELECT MIN(edited) FROM versions "
                   "WHERE owner = ? AND chat = ? AND message = ?)", (*key, *key, MAX_VERSIONS - 1, *key))

    @staticmethod
    def _remove(db: sqlite3.Connection, keys: list[tuple[int, int, int]]) -> None:
        for key in dict.fromkeys(keys):
            where = "owner = ? AND chat = ? AND message = ?"
            top = db.execute(f"SELECT MAX(seq) FROM changes WHERE {where}", key).fetchone()[0]
            if top is not None:
                # Readers following the change sequence learn that changes up to here are gone.
                db.execute("INSERT INTO meta (key, value) VALUES ('removed_through', ?) ON CONFLICT (key) DO "
                           "UPDATE SET value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                           (str(top),))
            for table in ("changes", "versions", "records"):
                db.execute(f"DELETE FROM {table} WHERE {where}", key)
        if keys:
            db.execute("DELETE FROM chats WHERE NOT EXISTS (SELECT 1 FROM records WHERE records.owner = "
                       "chats.owner AND records.chat = chats.chat)")

    # --- reading -----------------------------------------------------------------------------------------

    def records(self, owner: Optional[OwnerId] = None) -> list[Record]:
        """Every record, oldest first per chat."""
        db = self._open(create=False)
        if db is None:
            return []
        try:
            where, args = ("WHERE owner = ?", (owner,)) if owner is not None else ("", ())
            rows = db.execute(f"SELECT owner, chat, message, date, author, name, reply_to, forwarded, offline, "
                              f"media, media_group, deleted FROM records {where} ORDER BY owner, chat, message",
                              args).fetchall()
            versions: dict[tuple[int, int, int], list[Version]] = {}
            for o, c, m, edited, text in db.execute("SELECT owner, chat, message, edited, text FROM versions "
                                                    "ORDER BY edited"):
                versions.setdefault((o, c, m), []).append(Version(edited, text))
        finally:
            db.close()
        return [Record(owner=OwnerId(o), chat=ChatId(c), message=MessageId(m), date=d, author=Author(a), name=n,
                       reply_to=MessageId(r) if r is not None else None, forwarded=bool(f), offline=bool(off),
                       media=tuple(Attachment(**x) for x in json.loads(media)), media_group=g, deleted=dl,
                       versions=tuple(versions.get((o, c, m), ())))
                for o, c, m, d, a, n, r, f, off, media, g, dl in rows]

    def changes(self) -> list[tuple[int, Change, OwnerId, ChatId, MessageId]]:
        db = self._open(create=False)
        if db is None:
            return []
        try:
            rows = db.execute("SELECT seq, kind, owner, chat, message FROM changes ORDER BY seq").fetchall()
        finally:
            db.close()
        return [(s, Change(k), OwnerId(o), ChatId(c), MessageId(m)) for s, k, o, c, m in rows]

    def removed_through(self) -> int:
        db = self._open(create=False)
        if db is None:
            return 0
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'removed_through'").fetchone()
        finally:
            db.close()
        return int(row[0]) if row else 0


# --- the adapter ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Inbox:
    settings: InboxSettings
    store: InboxStore


_UNSET = object()


class BusinessInboxMixin:
    """Business updates into the inbox; listed first among this plugin's mixins."""

    def _inbox(self) -> Optional[Inbox]:
        """The configured inbox, resolved once per adapter; None when it is off."""
        state = self.__dict__.get("_hermes_inbox", _UNSET)
        if state is _UNSET:
            from hermes_cli.profiles import get_active_profile_name

            state = None
            try:
                settings = inbox_settings(self.config.extra or {}, os.environ, get_active_profile_name())
            except InboxConfigError as exc:
                logger.error("[Telegram] Business inbox is off: %s", exc)
                settings = None
            if settings is not None:
                state = Inbox(settings, InboxStore(Path(self._update_receipt_dir) / STORE_NAME))
            self.__dict__["_hermes_inbox"] = state
        return state

    @property
    def _inbox_outcomes(self) -> Counter[tuple[str, str]]:
        return self.__dict__.setdefault("_hermes_inbox_outcomes", Counter())

    def _register_handlers(self, app: Any) -> None:
        super()._register_handlers(app)
        from telegram import Update
        from telegram.ext import TypeHandler

        app.add_handler(TypeHandler(Update, self._on_business_update), group=BUSINESS_GROUP)
        if self._inbox() is not None:
            self._start_inbox_purge()

    async def _on_business_update(self, update: Any, context: Any) -> None:
        """Every business update ends here: kept or dropped, it reaches no later handler."""
        from telegram.ext import ApplicationHandlerStop

        kind = business_kind(update)
        if kind is None:
            return
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
        if inbox is None:
            return "off"
        event = parse_business_update(update)
        if isinstance(event, Skipped):
            return event.reason
        now = int(_clock())
        if isinstance(event, ConnectionSeen):
            if event.owner not in inbox.settings.owners:
                self._ignored_connections.add(event.connection)
                return "ignored"
            self._ignored_connections.discard(event.connection)
            await asyncio.to_thread(inbox.store.save_connection, event, now)
            return "connection"
        known = await self._connection_owner(inbox, event.connection, now)
        if known is None:
            return "ignored" if event.connection in self._ignored_connections else "unknown_connection"
        owner, enabled = known
        if not enabled:
            return "disabled"
        if isinstance(event, MessagesDeleted):
            marked = await asyncio.to_thread(inbox.store.mark_deleted, event, owner, now)
            return "deleted" if marked else "unchanged"
        change = await asyncio.to_thread(inbox.store.keep_message, event, owner, inbox.settings, now)
        return change.value if change is not None else "unchanged"

    @property
    def _ignored_connections(self) -> set[ConnectionId]:
        """Connections of owners not configured, in memory only: the file holds nothing of theirs."""
        return self.__dict__.setdefault("_hermes_inbox_ignored", set())

    async def _connection_owner(self, inbox: Inbox, connection: ConnectionId,
                                now: int) -> Optional[tuple[OwnerId, bool]]:
        """The connection's owner and whether it is enabled; asks Telegram once for a connection the inbox
        has not seen (an update missed while the gateway was down)."""
        if connection in self._ignored_connections:
            return None
        known = await asyncio.to_thread(inbox.store.connection, connection)
        if known is not None:
            return known
        try:
            reply = await self._bot.get_business_connection(business_connection_id=connection)
        except Exception as exc:
            logger.warning("[Telegram] Business inbox could not look up a connection (%s)", type(exc).__name__)
            return None
        seen = parse_business_update(_ConnectionUpdate(reply))
        if not isinstance(seen, ConnectionSeen) or seen.connection != connection:
            return None
        if seen.owner not in inbox.settings.owners:
            self._ignored_connections.add(connection)
            return None
        await asyncio.to_thread(inbox.store.save_connection, seen, now)
        return seen.owner, seen.enabled

    def _start_inbox_purge(self) -> None:
        task = self.__dict__.get("_hermes_inbox_purge")
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.__dict__["_hermes_inbox_purge"] = loop.create_task(self._purge_inbox_periodically(),
                                                                name="telegram-business-inbox-purge")

    async def _purge_inbox_periodically(self) -> None:
        """Expired records leave the file within ``PURGE_EVERY_SECS`` while the gateway runs, also when no
        business update arrives to trigger a purge."""
        while True:
            await asyncio.sleep(PURGE_EVERY_SECS)
            inbox = self._inbox()
            if inbox is None:
                return
            try:
                await asyncio.to_thread(inbox.store.purge, inbox.settings, int(_clock()))
            except Exception as exc:
                logger.warning("[Telegram] Business inbox purge failed (%s)", type(exc).__name__)


@dataclass(frozen=True)
class _ConnectionUpdate:
    """A ``getBusinessConnection`` reply read as the update that would have carried it."""

    business_connection: Any
