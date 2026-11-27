"""The Business inbox, slice 1 (docs/business-inbox.md): intake and storage, rows B1 and B3 to B10.

Store checks run the real SQLite file in a temporary directory. Adapter checks build Telegram's JSON into
python-telegram-bot's own objects and dispatch them through the plugin's real Application
(``harvested/update_admission.TelegramApplication``) with every handler the adapter registers, plus a spy in the
last group; the Bot API client is the recording one of checks/fake_bot.py.
"""

import asyncio
import hashlib
import itertools
import json
import logging
import sqlite3
import time
from typing import Any

import pytest
from telegram import BusinessConnection, Message, Update
from telegram.ext import Application, TypeHandler
from telegram.request import BaseRequest, RequestData

from checks.fake_bot import OWNER as OWNER_ID
from checks.fake_bot import build_adapter
from hermes_telegram import inbox
from hermes_telegram.harvested.update_admission import TelegramApplication, build_update_processor
from hermes_telegram.inbox import (
    BUSINESS_GROUP, STORE_NAME, Attachment, Author, Change, ChatId, ConnectionId, ConnectionSeen, InboxConfigError,
    InboxSettings, InboxStore, InboxStoreError, Limits, MessageId, MessageSeen, MessagesDeleted, OwnerId, Record,
    Skipped, Version, attachments_of, inbox_settings, parse_business_update,
)

OWNER = OwnerId(int(OWNER_ID))
CONTACT = 4242
OTHER_CONTACT = 4343
CID = ConnectionId("bc-1")
NOW = 1_800_000_000
HOUR = 3600
LIMITS = Limits(retention_hours=72, max_per_chat=1000)
BOT = "1"


def _seen(message: int, *, sender: int = CONTACT, text: str | None = "hello", date: int = NOW, edited: int = 0,
          chat: int = CONTACT, name: str = "Ann Lee", attachments: tuple[Attachment, ...] = ()) -> MessageSeen:
    return MessageSeen(connection=CID, chat=ChatId(chat), chat_name=name, message=MessageId(message),
                       sender=sender, sender_name=name if sender != OWNER else "Owner", via_bot=False,
                       date=date, edited=edited, text=text, reply_to=None, forwarded=False, offline=False,
                       attachments=attachments, media_group=None)


def _store(tmp_path, *, enabled: bool = True) -> InboxStore:
    """A store whose connection ``CID`` belongs to the owner."""
    store = InboxStore(tmp_path / STORE_NAME)
    store.save_connection(ConnectionSeen(CID, OWNER, enabled, {}), None, NOW)
    return store


def _keep(store: InboxStore, seen: MessageSeen, *, limits: Limits = LIMITS, now: int = NOW,
          update: int | None = None) -> Change | None:
    return store.keep_message(seen, OWNER, limits, (BOT, update) if update is not None else None, now)


# --- the store -------------------------------------------------------------------------------------------


def test_a_message_its_edit_and_its_deletion_are_kept_and_repeats_change_nothing(tmp_path):
    # B3 and B9. Bugs: a repeated update adds a version or a change; a deletion drops the text; a deletion of
    # a message the inbox never had leaves a tombstone.
    store = _store(tmp_path)
    first, edit = _seen(5, text="dinner at 8?"), _seen(5, text="dinner at 9?", edited=NOW + 10)
    deletion = MessagesDeleted(CID, ChatId(CONTACT), (MessageId(5), MessageId(77)))  # 77 was never kept

    assert _keep(store, first) == Change.INSERT
    assert _keep(store, first) is None
    assert _keep(store, edit, now=NOW + 10) == Change.EDIT
    assert _keep(store, edit, now=NOW + 10) is None
    assert store.mark_deleted(deletion, OWNER, LIMITS, None, NOW + 20) == 1
    assert store.mark_deleted(deletion, OWNER, LIMITS, None, NOW + 20) == 0

    assert store.records() == [Record(
        owner=OWNER, chat=ChatId(CONTACT), message=MessageId(5), date=NOW, author=Author.CONTACT, name="Ann Lee",
        reply_to=None, forwarded=False, offline=False, media=(), media_group=None, deleted=NOW + 20,
        versions=(Version(0, "dinner at 8?"), Version(NOW + 10, "dinner at 9?")))]
    assert [kind for _, kind, *_ in store.changes()] == [Change.INSERT, Change.EDIT, Change.DELETE]


def test_an_update_applied_once_is_not_applied_again_after_a_restart(tmp_path):
    # B9 across restarts (review, slice 1): the harvested admission does not receipt business updates. Bugs: a
    # redelivered connection update restoring an old state; a replayed message renaming the chat back.
    path = tmp_path / STORE_NAME
    store = _store(tmp_path)
    assert _keep(store, _seen(5, name="Ann"), update=10) == Change.INSERT
    assert store.save_connection(ConnectionSeen(CID, OWNER, False, {}), (BOT, 11), NOW) is True
    assert store.save_connection(ConnectionSeen(CID, OWNER, True, {}), (BOT, 12), NOW) is True  # newer state
    restarted = InboxStore(path)

    assert restarted.save_connection(ConnectionSeen(CID, OWNER, False, {}), (BOT, 11), NOW) is False
    assert restarted.connection(CID) == (OWNER, True)
    assert _keep(restarted, _seen(5, name="Ann (old)"), update=10) is None
    assert sqlite3.connect(path).execute("SELECT name FROM chats").fetchall() == [("Ann",)]


def test_edits_in_the_same_second_are_both_kept_and_a_late_older_edit_does_not_set_the_media(tmp_path):
    # B3 (review, slice 1). Telegram's edit_date is in seconds. Bugs: the second edit of a second dropped; an
    # older edit arriving late putting its attachment back on the record.
    store = _store(tmp_path)
    photo, chart = (Attachment("photo", width=10, height=10),), (Attachment("document", name="chart.pdf"),)
    _keep(store, _seen(5, text="v0"))
    assert _keep(store, _seen(5, text="v2", edited=NOW + 9, attachments=chart)) == Change.EDIT
    assert _keep(store, _seen(5, text="v1", edited=NOW + 5, attachments=photo)) == Change.EDIT
    assert store.records()[0].media == chart  # the late, older edit is history only
    assert _keep(store, _seen(5, text="v3", edited=NOW + 9, attachments=chart)) == Change.EDIT

    [record] = store.records()
    assert [v.text for v in record.versions] == ["v0", "v1", "v2", "v3"]
    assert record.media == chart


def test_a_records_versions_and_changes_stay_bounded_under_many_edits(tmp_path):
    # Storage (review, slice 1). Bug: changes grow by one per edit while versions are trimmed.
    store = _store(tmp_path)
    _keep(store, _seen(5, text="first"))
    for n in range(1, 80):
        _keep(store, _seen(5, text=f"edit {n}", edited=NOW + n))

    [record] = store.records()
    assert len(record.versions) == inbox.MAX_VERSIONS
    assert record.versions[0].text == "first" and record.versions[-1].text == "edit 79"
    assert len(store.changes()) == inbox.MAX_VERSIONS + 1
    assert store.removed_through() == 29  # 80 changes made, the newest 51 kept


def test_records_older_than_the_retention_are_not_kept_and_are_purged(tmp_path):
    # B5, age. Counted from the message's Telegram date. Bugs: the age compared the wrong way, or counted
    # from arrival.
    store = _store(tmp_path)
    limit = 72 * HOUR
    assert _keep(store, _seen(1, date=NOW - limit - 1)) is None
    assert _keep(store, _seen(2, date=NOW - limit + 60)) == Change.INSERT

    assert store.purge(LIMITS, NOW + 60) == 0  # exactly 72 hours old stays
    assert store.purge(LIMITS, NOW + 61) == 1
    assert store.records() == []


def test_a_chat_keeps_its_newest_records_by_telegram_date_up_to_the_cap(tmp_path):
    # B5, count: per chat, the newest by date kept (review, slice 1: not by message id). Bugs: the cap over
    # every chat; the oldest kept; a message the cap removes at once reported as kept, readers told of a gap.
    store = _store(tmp_path)
    limits = Limits(retention_hours=72, max_per_chat=3)
    for message, date in ((10, NOW + 1), (11, NOW + 2), (9, NOW + 3)):  # 9 is the newest by date
        _keep(store, _seen(message, date=date), limits=limits, now=NOW + 3)
    _keep(store, _seen(50, chat=OTHER_CONTACT), limits=limits, now=NOW + 3)
    assert _keep(store, _seen(8, date=NOW), limits=limits, now=NOW + 3) is None  # older than all three
    assert store.removed_through() == 0
    assert _keep(store, _seen(12, date=NOW + 4), limits=limits, now=NOW + 4) == Change.INSERT

    assert [(r.chat, r.message) for r in store.records()] == [
        (CONTACT, 11), (CONTACT, 9), (CONTACT, 12), (OTHER_CONTACT, 50)]
    assert store.removed_through() == 1  # the insert of message 10 was the first change


def test_a_connection_that_is_not_enabled_keeps_nothing(tmp_path):
    # The connection is checked inside the write (review, slice 1). Bug: a message admitted while the connection
    # was enabled, written after it was switched off.
    store = _store(tmp_path, enabled=False)
    assert _keep(store, _seen(5)) is None
    assert store.records() == []


def test_purged_text_does_not_remain_in_the_file(tmp_path):
    # Storage: secure_delete, no WAL, owner-only. Bug: SQLite leaves a deleted row's bytes in a free page.
    path = tmp_path / STORE_NAME
    store = _store(tmp_path)
    _keep(store, _seen(1, text="zebra-plan-7731"))
    _keep(store, _seen(2, text="kept", date=NOW + HOUR), now=NOW + HOUR)
    assert b"zebra-plan-7731" in path.read_bytes()

    store.purge(LIMITS, NOW + 72 * HOUR + 1)

    assert [r.message for r in store.records()] == [2]
    assert b"zebra-plan-7731" not in path.read_bytes()
    assert not path.with_name(STORE_NAME + "-wal").exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_file_of_another_schema_is_refused_unchanged(tmp_path):
    # Review, slice 1. Bug: the current schema's tables and journal mode written into a file before refusing it.
    path = tmp_path / STORE_NAME
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES ('schema', '9');")
    db.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(InboxStoreError):
        InboxStore(path).records()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


# --- settings --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("extra, environ, expected", [
    ({}, {}, None),
    ({"business_inbox": {"owners": {111: "default"}}}, {}, InboxSettings(OwnerId(111), Limits(72, 1000))),
    ({"business_inbox": {"owners": {"111": "default"}, "retention_hours": 24, "max_messages_per_chat": 10}},
     {"HERMES_TELEGRAM_INBOX_RETENTION_HOURS": "6"}, InboxSettings(OwnerId(111), Limits(6, 10))),
    ({"business_inbox": {"owners": {111: "default"}}}, {"HERMES_TELEGRAM_INBOX_MAX_PER_CHAT": "200"},
     InboxSettings(OwnerId(111), Limits(72, 200))),
])
def test_settings_default_to_72_hours_and_1000_per_chat_and_the_environment_overrides(extra, environ, expected):
    # B5's limits. Bugs: a default other than the owner's 72 h and 1000; config winning over the environment.
    assert inbox_settings(extra, environ, "default") == expected


@pytest.mark.parametrize("raw, environ", [
    ({}, {}),  # no owner
    ({"owners": {111: "default", 222: "default"}}, {}),  # one owner in this version (decision 5)
    ({"owners": {111: "viki"}}, {}),  # another profile's home: not in this version
    ({"owners": {"me": "default"}}, {}),
    ({"owners": {111: "default"}, "retention_hours": 0}, {}),
    ({"owners": {111: "default"}, "retention_hours": 10**20}, {}),  # SQLite cannot bind it
    ({"owners": {111: "default"}}, {"HERMES_TELEGRAM_INBOX_MAX_PER_CHAT": "lots"}),
    ({"owners": {111: "default"}}, {"HERMES_TELEGRAM_INBOX_RETENTION_HOURS": "²"}),  # a digit int() refuses
])
def test_unusable_settings_are_refused(raw, environ):
    with pytest.raises(InboxConfigError):
        inbox_settings({"business_inbox": raw}, environ, "default")


# --- what an update says ---------------------------------------------------------------------------------


def _message_json(message_id: int, sender: int = CONTACT, **fields: Any) -> dict[str, Any]:
    return {"message_id": message_id, "date": int(time.time()), "business_connection_id": CID,
            "chat": {"id": CONTACT, "type": "private", "first_name": "Ann", "last_name": "Lee"},
            "from": {"id": sender, "is_bot": False, "first_name": "Ann" if sender == CONTACT else "Owner"},
            **fields}


def _connection_json(owner: int = int(OWNER), enabled: bool = True, **fields: Any) -> dict[str, Any]:
    return {"id": CID, "user": {"id": owner, "is_bot": False, "first_name": "Owner"}, "user_chat_id": owner,
            "date": int(time.time()), "is_enabled": enabled, "rights": {"can_reply": False}, **fields}


@pytest.mark.parametrize("fields, expected", [
    ({"photo": [{"file_id": "s", "file_unique_id": "s", "width": 90, "height": 60, "file_size": 900},
                {"file_id": "l", "file_unique_id": "l", "width": 1280, "height": 853, "file_size": 98000}]},
     [Attachment("photo", size=98000, width=1280, height=853)]),
    ({"voice": {"file_id": "v", "file_unique_id": "v", "duration": 7, "mime_type": "audio/ogg", "file_size": 5000}},
     [Attachment("voice", mime="audio/ogg", size=5000, duration=7)]),
    ({"document": {"file_id": "d", "file_unique_id": "d", "file_name": "plan.pdf", "mime_type": "application/pdf",
                   "file_size": 1234}},
     [Attachment("document", mime="application/pdf", size=1234, name="plan.pdf")]),
    ({"contact": {"phone_number": "+10000000000", "first_name": "Bob"}}, [Attachment("contact")]),
    ({"location": {"latitude": 1.5, "longitude": 2.5}}, [Attachment("location")]),
])
def test_media_is_described_by_its_metadata_and_other_content_by_its_kind(fields, expected):
    # B4. Bugs: a small photo size kept instead of the largest; a phone number or a place kept as metadata.
    message = Message.de_json(_message_json(9, **fields), None)
    assert list(attachments_of(message)) == expected


def test_a_connection_keeps_every_right_and_a_deletion_outside_a_private_chat_is_skipped():
    # Review, slice 1. Bugs: rights other than can_reply lost, so status cannot name them; a group deletion
    # resolving its connection.
    connection = Update.de_json({"update_id": 1, "business_connection": _connection_json(
        rights={"can_reply": False, "can_delete_all_messages": True})}, None)
    deletion = Update.de_json({"update_id": 2, "deleted_business_messages": {
        "business_connection_id": CID, "message_ids": [5], "chat": {"id": -100, "type": "supergroup", "title": "G"}}},
        None)

    rights = parse_business_update(connection).rights
    assert rights["can_delete_all_messages"] is True and rights["can_reply"] is False and len(rights) == 14
    assert parse_business_update(deletion) == Skipped("not_private")


def test_debug_records_of_business_payloads_are_dropped(caplog):
    # Logs (review, slice 1): python-telegram-bot logs every getUpdates result at DEBUG before any handler runs.
    payload = [{"update_id": 3, "business_message": {"business_connection_id": "bc-1", "text": "a secret"}}]
    with caplog.at_level(logging.DEBUG, logger="telegram"):
        logging.getLogger("telegram.ext.ExtBot").debug("Call to Bot API endpoint getUpdates finished with %s", payload)
        logging.getLogger("telegram.ext.ExtBot").debug("Call to Bot API endpoint getMe finished")
    assert "a secret" not in caplog.text and "getMe" in caplog.text


# --- the adapter, through the plugin's Application -------------------------------------------------------

_update_ids = itertools.count(1)


def _update(kind: str, payload: dict[str, Any], update_id: int | None = None) -> dict[str, Any]:
    return {"update_id": update_id or next(_update_ids), kind: payload}


CONFIGURED = {"owners": {OWNER_ID: "default"}}


class GetMeOnly(BaseRequest):
    """The Application's own HTTP layer, answering getMe for ``initialize`` and refusing every other method: the
    adapter's calls go through the recording client of checks/fake_bot.py instead."""

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 1.0

    async def do_request(self, url: str, method: str, request_data: RequestData | None = None,
                         **timeouts: Any) -> tuple[int, bytes]:
        if url.endswith("/getMe"):
            return 200, json.dumps({"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "bot",
                                                           "username": "bot"}}).encode()
        return 404, b'{"ok": false, "error_code": 404, "description": "Not Found"}'


class Rig:
    def __init__(self, tmp_path, inbox_config: dict[str, Any] | None, plugin_handler: bool = False) -> None:
        extra = {"business_inbox": inbox_config} if inbox_config is not None else {}
        self.adapter = build_adapter(tmp_path, **extra)
        builder = Application.builder().token("1:x").request(GetMeOnly()).get_updates_request(GetMeOnly())
        builder.application_class(TelegramApplication, {"adapter": self.adapter})
        self.app = builder.concurrent_updates(build_update_processor(self.adapter)).build()
        self.adapter._app = self.app
        self.later: list[int] = []  # update ids a handler after every other group saw
        self.plugin: list[int] = []  # update ids a native plugin's catch-all in the business group saw

        async def plugin_catch_all(update: Update, context: Any) -> None:
            self.plugin.append(update.update_id)
        if plugin_handler:  # native plugins are wired before the adapter's own handlers (adapter_transport.py)
            self.app.add_handler(TypeHandler(Update, plugin_catch_all), group=BUSINESS_GROUP)
        self.adapter._register_handlers(self.app)

        async def spy(update: Update, context: Any) -> None:
            self.later.append(update.update_id)
        self.app.add_handler(TypeHandler(Update, spy), group=1000)
        self.store = InboxStore(tmp_path / STORE_NAME)
        self.path = tmp_path / STORE_NAME

    async def send(self, *updates: dict[str, Any]) -> None:
        await self.app.initialize()
        for raw in updates:
            await self.app.process_update(Update.de_json(raw, self.app.bot))
        await asyncio.sleep(0.05)

    def close(self) -> None:
        current = self.adapter.__dict__.get("_hermes_inbox_purge")
        if current is not None:
            current[0].cancel()


@pytest.fixture
def rig_factory(tmp_path):
    made: list[Rig] = []

    def make(inbox_config: dict[str, Any] | None = CONFIGURED, **kw: Any) -> Rig:
        made.append(Rig(tmp_path, inbox_config, **kw))
        return made[-1]
    yield make
    for rig in made:
        rig.close()


async def _until(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def _dm(text: str) -> dict[str, Any]:
    return _update("message", {"message_id": 1, "date": int(time.time()), "text": text,
                               "chat": {"id": int(OWNER), "type": "private", "first_name": "Owner"},
                               "from": {"id": int(OWNER), "is_bot": False, "first_name": "Owner"}})


async def test_business_messages_are_kept_and_reach_no_other_handler_and_no_agent(rig_factory):
    # B1, B2 (offline), B4, B7, B9, B10. Bugs: the handler not stopping the update, so a later group (or the
    # agent) sees it; the author taken from the chat instead of the sender; a lookup or a getFile on this path.
    rig = rig_factory()
    from_contact = _update("business_message", _message_json(5, text="dinner tonight?"))
    photo = _update("business_message", _message_json(6, caption="the place", photo=[
        {"file_id": "p", "file_unique_id": "p", "width": 800, "height": 600, "file_size": 4000}]))
    from_owner = _update("business_message", _message_json(7, sender=int(OWNER), text="yes, at 8"))
    edited = _update("edited_business_message", _message_json(7, sender=int(OWNER), text="yes, at 9",
                                                               edit_date=int(time.time()) + 5))
    gone = _update("deleted_business_messages", {"business_connection_id": CID, "message_ids": [5],
                                                 "chat": {"id": CONTACT, "type": "private", "first_name": "Ann"}})
    business = [_update("business_connection", _connection_json()), from_contact, photo, from_owner,
                {**from_contact, "update_id": next(_update_ids)}, edited, gone]

    await rig.send(*business, from_contact)  # the same update id twice as well
    assert rig.adapter.bot_stub.calls == []
    await rig.send(dm := _dm("hi bot"))
    await _until(lambda: rig.adapter.received)  # Hermes batches a text for a moment before handing it on

    assert rig.later == [dm["update_id"]]
    assert [event.text for event in rig.adapter.received] == ["hi bot"]
    kept = {r.message: (r.author, [v.text for v in r.versions], r.media, r.deleted is not None)
            for r in rig.store.records()}
    assert kept == {
        5: (Author.CONTACT, ["dinner tonight?"], (), True),
        6: (Author.CONTACT, ["the place"], (Attachment("photo", size=4000, width=800, height=600),), False),
        7: (Author.OWNER, ["yes, at 8", "yes, at 9"], (), False),
    }


async def test_a_native_plugins_catch_all_in_the_business_group_does_not_get_business_updates(rig_factory):
    # Review, slice 1: PTB runs the first matching handler of a group, and native plugins are wired first. Bug:
    # the plugin's handler takes the update, the inbox's never runs, and later groups see it.
    rig = rig_factory(plugin_handler=True)
    await rig.send(_update("business_connection", _connection_json()),
                   _update("business_message", _message_json(5, text="hi")), dm := _dm("hi bot"))

    assert rig.plugin == [dm["update_id"]]
    assert rig.later == [dm["update_id"]]
    assert [r.message for r in rig.store.records()] == [5]


async def test_a_connection_the_inbox_missed_is_looked_up_once(rig_factory):
    # A message whose connection update came while the gateway was down. Bugs: the message dropped; Telegram
    # asked again for every message.
    rig = rig_factory()
    rig.adapter.bot_stub.business_connections[CID] = BusinessConnection.de_json(_connection_json(), None)

    await rig.send(_update("business_message", _message_json(5, text="are you there?")),
                   _update("business_message", _message_json(6, text="hello?")))

    assert [name for name, _ in rig.adapter.bot_stub.calls] == ["getBusinessConnection"]
    assert [r.message for r in rig.store.records()] == [5, 6]


async def test_a_disconnect_during_a_lookup_is_not_undone_by_the_lookups_answer(rig_factory):
    # Review, slice 1: updates of different chats run concurrently. Bugs: the lookup's older "enabled" saved
    # over the disconnect, so later messages are kept; two chats that miss the connection both asking Telegram.
    rig = rig_factory()
    bot = rig.adapter.bot_stub
    bot.business_connections[CID] = BusinessConnection.de_json(_connection_json(), None)
    bot.hold["getBusinessConnection"] = asyncio.Event()
    await rig.app.initialize()

    def process(raw: dict[str, Any]) -> asyncio.Task:
        return asyncio.create_task(rig.app.process_update(Update.de_json(raw, rig.app.bot)))
    first = process(_update("business_message", _message_json(5, text="one")))
    await _until(lambda: bot.calls)
    other_chat = process(_update("business_message", {**_message_json(6, text="two"),
                                                      "chat": {"id": OTHER_CONTACT, "type": "private"}}))
    disconnect = process(_update("business_connection", _connection_json(enabled=False)))
    await asyncio.sleep(0.05)
    bot.hold["getBusinessConnection"].set()
    await asyncio.gather(first, other_chat, disconnect)
    await rig.send(_update("business_message", _message_json(7, text="after the disconnect")))

    assert [name for name, _ in bot.calls] == ["getBusinessConnection"]
    assert rig.store.connection(CID) == (OWNER, False)
    assert 7 not in [r.message for r in rig.store.records()]


async def test_an_owner_not_configured_stores_nothing(rig_factory):
    # B6. Bugs: the connection or its messages written to the file; the connection asked for on every message.
    rig = rig_factory()
    stranger = int(OWNER) + 1
    rig.adapter.bot_stub.business_connections[CID] = BusinessConnection.de_json(_connection_json(stranger), None)

    await rig.send(_update("business_message", _message_json(5, sender=CONTACT, text="hi")),
                   _update("business_message", _message_json(6, sender=stranger, text="hey")),
                   _update("business_connection", _connection_json(stranger)))

    assert not rig.path.exists()
    assert [name for name, _ in rig.adapter.bot_stub.calls] == ["getBusinessConnection"]
    assert rig.later == []


async def test_a_kept_connection_of_an_owner_no_longer_configured_keeps_nothing_new(rig_factory):
    # B6 after the owner map changed (review, slice 1). Bug: the stored connection row trusted without checking
    # the owner against the current settings.
    stranger = OwnerId(int(OWNER) + 1)
    rig = rig_factory()
    rig.store.save_connection(ConnectionSeen(CID, stranger, True, {}), None, NOW)

    await rig.send(_update("business_message", _message_json(5, text="hi")))

    assert rig.store.records() == []


async def test_without_an_inbox_business_updates_are_still_stopped_and_nothing_is_kept(rig_factory):
    # A profile with no business_inbox, or a refused one. Bug: business updates reach later handlers when the
    # inbox is off.
    rig = rig_factory(None)
    await rig.send(_update("business_connection", _connection_json()),
                   _update("business_message", _message_json(5, text="hi")))
    assert rig.later == []
    assert not rig.path.exists()
    assert rig.adapter.bot_stub.calls == []


async def test_expired_records_leave_the_file_while_the_gateway_runs_without_new_updates(rig_factory, monkeypatch):
    # B5 when chats go quiet: the periodic purge. Bug: the purge runs only when a business update arrives.
    monkeypatch.setattr(inbox, "PURGE_EVERY_SECS", 0.02)
    rig = rig_factory({**CONFIGURED, "retention_hours": 1})
    now = time.time()
    await rig.send(_update("business_connection", _connection_json()),
                   _update("business_message", {**_message_json(5, text="soon gone"), "date": int(now) - HOUR + 1}))
    assert [r.message for r in rig.store.records()] == [5]

    monkeypatch.setattr(inbox, "_clock", lambda: now + 2)
    await _until(lambda: not rig.store.records())
    assert rig.store.records() == []


async def test_a_file_keeps_expiring_after_intake_is_switched_off(tmp_path, monkeypatch):
    # Decision 2 (review, slice 1). Bug: with business_inbox removed, nothing ever purges the existing file.
    monkeypatch.setattr(inbox, "PURGE_EVERY_SECS", 0.02)
    store = _store(tmp_path)
    now = time.time()
    _keep(store, _seen(5, text="written before", date=int(now) - HOUR + 1), limits=Limits(1, 1000), now=int(now))
    rig = Rig(tmp_path, None)
    try:
        monkeypatch.setattr(inbox, "_clock", lambda: now + 2)
        await _until(lambda: not store.records())
        assert store.records() == []
    finally:
        rig.close()


async def test_the_purge_ends_with_the_adapters_application(rig_factory, monkeypatch):
    # Review, slice 1. Bug: a replaced or disconnected adapter's purge running on with its old limits.
    monkeypatch.setattr(inbox, "PURGE_EVERY_SECS", 0.02)
    rig = rig_factory()
    task, _app = rig.adapter.__dict__["_hermes_inbox_purge"]
    rig.adapter._app = None  # disconnect() drops the Application
    await _until(task.done)
    assert task.done() and not task.cancelled()
