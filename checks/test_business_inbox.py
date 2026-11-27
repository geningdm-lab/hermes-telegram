"""The Business inbox, slice 1 (docs/business-inbox.md): intake and storage, rows B1 and B3 to B10.

Store checks run the real SQLite file in a temporary directory. Adapter checks build Telegram's JSON into
python-telegram-bot's own objects and dispatch them through the plugin's real Application
(``harvested/update_admission.TelegramApplication``) with every handler the adapter registers, plus a spy in the
last group; the Bot API client is the recording one of checks/fake_bot.py.
"""

import asyncio
import itertools
import json
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
    STORE_NAME, Attachment, Author, Change, ChatId, ConnectionId, InboxConfigError, InboxSettings, InboxStore,
    MessageId, MessageSeen, MessagesDeleted, OwnerId, Record, Version, attachments_of, inbox_settings,
)

OWNER = OwnerId(int(OWNER_ID))
CONTACT = 4242
OTHER_CONTACT = 4343
CID = ConnectionId("bc-1")
NOW = 1_800_000_000
HOUR = 3600
SETTINGS = InboxSettings(owners=frozenset({OWNER}), retention_hours=72, max_per_chat=1000)


def _seen(message: int, *, sender: int = CONTACT, text: str | None = "hello", date: int = NOW, edited: int = 0,
          chat: int = CONTACT) -> MessageSeen:
    return MessageSeen(connection=CID, chat=ChatId(chat), chat_name="Ann Lee", message=MessageId(message),
                       sender=sender, sender_name="Ann Lee" if sender != OWNER else "Owner", via_bot=False,
                       date=date, edited=edited, text=text, reply_to=None, forwarded=False, offline=False,
                       attachments=(), media_group=None)


# --- the store -------------------------------------------------------------------------------------------


def test_a_message_its_edit_and_its_deletion_are_kept_and_repeats_change_nothing(tmp_path):
    # B3 and B9. Bugs: a repeated update adds a version or a change; a deletion drops the text; a deletion of
    # a message the inbox never had leaves a tombstone.
    store = InboxStore(tmp_path / STORE_NAME)
    first, edit = _seen(5, text="dinner at 8?"), _seen(5, text="dinner at 9?", edited=NOW + 10)
    deletion = MessagesDeleted(CID, ChatId(CONTACT), (MessageId(5), MessageId(77)))  # 77 was never kept

    assert store.keep_message(first, OWNER, SETTINGS, NOW) == Change.INSERT
    assert store.keep_message(first, OWNER, SETTINGS, NOW) is None
    assert store.keep_message(edit, OWNER, SETTINGS, NOW + 10) == Change.EDIT
    assert store.keep_message(edit, OWNER, SETTINGS, NOW + 10) is None
    assert store.mark_deleted(deletion, OWNER, NOW + 20) == 1
    assert store.mark_deleted(deletion, OWNER, NOW + 20) == 0

    assert store.records() == [Record(
        owner=OWNER, chat=ChatId(CONTACT), message=MessageId(5), date=NOW, author=Author.CONTACT, name="Ann Lee",
        reply_to=None, forwarded=False, offline=False, media=(), media_group=None, deleted=NOW + 20,
        versions=(Version(0, "dinner at 8?"), Version(NOW + 10, "dinner at 9?")))]
    assert [kind for _, kind, *_ in store.changes()] == [Change.INSERT, Change.EDIT, Change.DELETE]


def test_records_older_than_the_retention_are_not_kept_and_are_purged(tmp_path):
    # B5, age. Counted from the message's Telegram date. Bugs: the age compared the wrong way, or counted
    # from arrival.
    store = InboxStore(tmp_path / STORE_NAME)
    limit = 72 * HOUR
    assert store.keep_message(_seen(1, date=NOW - limit - 1), OWNER, SETTINGS, NOW) is None
    assert store.keep_message(_seen(2, date=NOW - limit + 60), OWNER, SETTINGS, NOW) == Change.INSERT

    assert store.purge(SETTINGS, NOW + 60) == 0  # exactly 72 hours old stays
    assert store.purge(SETTINGS, NOW + 61) == 1
    assert store.records() == []


def test_a_chat_keeps_its_newest_records_up_to_the_cap_and_readers_learn_what_went(tmp_path):
    # B5, count: per chat, by message, the newest kept. Bugs: the cap counted over every chat, or the oldest
    # kept; the change sequence silently losing a removed record's changes.
    store = InboxStore(tmp_path / STORE_NAME)
    settings = InboxSettings(owners=frozenset({OWNER}), retention_hours=72, max_per_chat=3)
    for message in (10, 11, 12):
        store.keep_message(_seen(message), OWNER, settings, NOW)
    store.keep_message(_seen(50, chat=OTHER_CONTACT), OWNER, settings, NOW)
    store.keep_message(_seen(13), OWNER, settings, NOW)

    assert [(r.chat, r.message) for r in store.records()] == [
        (CONTACT, 11), (CONTACT, 12), (CONTACT, 13), (OTHER_CONTACT, 50)]
    assert store.removed_through() == 1  # the insert of message 10 was the first change


def test_purged_text_does_not_remain_in_the_file(tmp_path):
    # Storage: secure_delete, no WAL, owner-only. Bug: SQLite leaves a deleted row's bytes in a free page.
    path = tmp_path / STORE_NAME
    store = InboxStore(path)
    store.keep_message(_seen(1, text="zebra-plan-7731"), OWNER, SETTINGS, NOW)
    store.keep_message(_seen(2, text="kept", date=NOW + HOUR), OWNER, SETTINGS, NOW + HOUR)
    assert b"zebra-plan-7731" in path.read_bytes()

    store.purge(SETTINGS, NOW + 72 * HOUR + 1)

    assert [r.message for r in store.records()] == [2]
    assert b"zebra-plan-7731" not in path.read_bytes()
    assert not path.with_name(STORE_NAME + "-wal").exists()
    assert path.stat().st_mode & 0o777 == 0o600


# --- settings --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("extra, environ, expected", [
    ({}, {}, None),
    ({"business_inbox": {"owners": {111: "default"}}}, {},
     InboxSettings(frozenset({OwnerId(111)}), retention_hours=72, max_per_chat=1000)),
    ({"business_inbox": {"owners": {"111": "default"}, "retention_hours": 24, "max_messages_per_chat": 10}},
     {"HERMES_TELEGRAM_INBOX_RETENTION_HOURS": "6"},
     InboxSettings(frozenset({OwnerId(111)}), retention_hours=6, max_per_chat=10)),
    ({"business_inbox": {"owners": {111: "default"}}}, {"HERMES_TELEGRAM_INBOX_MAX_PER_CHAT": "200"},
     InboxSettings(frozenset({OwnerId(111)}), retention_hours=72, max_per_chat=200)),
])
def test_settings_default_to_72_hours_and_1000_per_chat_and_the_environment_overrides(extra, environ, expected):
    # B5's limits. Bugs: a default other than the owner's 72 h and 1000; config winning over the environment.
    assert inbox_settings(extra, environ, "default") == expected


@pytest.mark.parametrize("raw, environ", [
    ({}, {}),  # no owners
    ({"owners": {111: "viki"}}, {}),  # another profile's home: not in this version
    ({"owners": {"me": "default"}}, {}),
    ({"owners": {111: "default"}, "retention_hours": 0}, {}),
    ({"owners": {111: "default"}}, {"HERMES_TELEGRAM_INBOX_MAX_PER_CHAT": "lots"}),
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


# --- the adapter, through the plugin's Application -------------------------------------------------------

_update_ids = itertools.count(1)


def _update(kind: str, payload: dict[str, Any], update_id: int | None = None) -> dict[str, Any]:
    return {"update_id": update_id or next(_update_ids), kind: payload}


def _connection_json(owner: int = int(OWNER), enabled: bool = True) -> dict[str, Any]:
    return {"id": CID, "user": {"id": owner, "is_bot": False, "first_name": "Owner"}, "user_chat_id": owner,
            "date": int(time.time()), "is_enabled": enabled, "rights": {"can_reply": False}}


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
    def __init__(self, tmp_path, inbox_config: dict[str, Any] | None) -> None:
        extra = {"business_inbox": inbox_config} if inbox_config is not None else {}
        self.adapter = build_adapter(tmp_path, **extra)
        builder = Application.builder().token("1:x").request(GetMeOnly()).get_updates_request(GetMeOnly())
        builder.application_class(TelegramApplication, {"adapter": self.adapter})
        self.app = builder.concurrent_updates(build_update_processor(self.adapter)).build()
        self.adapter._register_handlers(self.app)
        self.later: list[int] = []  # update ids a handler after every other group saw

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
        task = self.adapter.__dict__.get("_hermes_inbox_purge")
        if task is not None:
            task.cancel()


@pytest.fixture
def rig_factory(tmp_path):
    made: list[Rig] = []

    def make(inbox_config: dict[str, Any] | None = CONFIGURED) -> Rig:
        made.append(Rig(tmp_path, inbox_config))
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


async def test_a_connection_the_inbox_missed_is_looked_up_once(rig_factory):
    # A message whose connection update came while the gateway was down. Bugs: the message dropped; Telegram
    # asked again for every message.
    rig = rig_factory()
    rig.adapter.bot_stub.business_connections[CID] = BusinessConnection.de_json(_connection_json(), None)

    await rig.send(_update("business_message", _message_json(5, text="are you there?")),
                   _update("business_message", _message_json(6, text="hello?")))

    assert [name for name, _ in rig.adapter.bot_stub.calls] == ["getBusinessConnection"]
    assert [r.message for r in rig.store.records()] == [5, 6]


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
