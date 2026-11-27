"""The Business inbox's read contract, slice 2 (docs/business-inbox.md, Read contract; row B8).

The commands run against the real SQLite file, filled through the store as the gateway fills it. The last checks
run the real ``hermes`` executable on a throwaway home with this plugin installed and enabled, which proves that
``hermes telegram`` registers from the plugin on the pinned Hermes and reads the profile's own settings.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hermes_telegram.inbox import (
    STORE_NAME, Attachment, ChatId, ConnectionId, ConnectionSeen, InboxSettings, InboxStore, Limits, MessageId,
    MessageSeen, MessagesDeleted, OwnerId,
)
from hermes_telegram.inbox_cli import ContractError, answer

OWNER = OwnerId(111)
ANN, BOB = 4242, 4343
CID = ConnectionId("bc-1")
NOW = 1_800_000_000  # 2027-01-15T08:00:00Z
SETTINGS = InboxSettings(OWNER, Limits(72, 1000))
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes_telegram"


def _seen(message: int, chat: int = ANN, *, sender: int | None = None, text: str | None = "hi", date: int = NOW,
          edited: int = 0, reply_to: int | None = None, attachments: tuple[Attachment, ...] = ()) -> MessageSeen:
    name = {ANN: "Ann Lee", BOB: "Bob"}[chat]
    return MessageSeen(connection=CID, chat=ChatId(chat), chat_name=name, message=MessageId(message),
                       sender=sender or chat, sender_name="Owner" if sender == OWNER else name, via_bot=False,
                       date=date, edited=edited, text=text,
                       reply_to=MessageId(reply_to) if reply_to else None, forwarded=False, offline=False,
                       attachments=attachments, media_group=None)


@pytest.fixture
def store(tmp_path) -> InboxStore:
    store = InboxStore(tmp_path / STORE_NAME)
    store.save_connection(ConnectionSeen(CID, OWNER, True, {"can_reply": False, "can_read_messages": True}), None,
                          NOW)
    return store


def _ask(store: InboxStore, command: str, settings: InboxSettings | None = SETTINGS, now: int = NOW,
         **options: Any) -> dict[str, Any]:
    args = argparse.Namespace(**{"since": None, "limit": None, "chat": None, "before": None, **options})
    return answer(command, args, store, settings, None if settings else "not configured", now)


def _keep(store: InboxStore, seen: MessageSeen, limits: Limits = SETTINGS.limits, now: int = NOW) -> None:
    assert store.keep_message(seen, OWNER, limits, None, now) is not None


def test_status_names_the_connection_counts_and_limits_and_no_content(store):
    # B8. Bugs: message text or names leaking into status; the limits in effect misreported.
    _keep(store, _seen(5, text="private words", date=NOW - 600))
    _keep(store, _seen(9, BOB))

    assert _ask(store, "status") == {
        "contract_version": 1, "intake": "on",
        "limits": {"retention_hours": 72, "max_messages_per_chat": 1000, "source": "config"},
        "connections": [{"connection": "bc-1", "owner": 111, "enabled": True,
                         "rights": {"can_reply": False, "can_read_messages": True}}],
        "records": 2, "chats": 2, "oldest_record_age_secs": 600,
    }


def test_a_record_reads_with_its_versions_its_deletion_its_reply_and_a_link(store):
    # B8's record shape. Bugs: the first version's text shown as current; a deletion lost; the reply's ref
    # pointing into another chat.
    _keep(store, _seen(5, sender=OWNER, text="dinner at 8?"))
    _keep(store, _seen(6, text="at 9", reply_to=5, attachments=(Attachment("photo", size=10, width=4, height=3),)))
    _keep(store, _seen(6, text="at 9:30", edited=NOW + 60, attachments=(Attachment("photo", size=10, width=4,
                                                                                    height=3),)), now=NOW + 60)
    store.mark_deleted(MessagesDeleted(CID, ChatId(ANN), (MessageId(6),)), OWNER, SETTINGS.limits, None, NOW + 120)

    assert _ask(store, "window", chat="111.4242", now=NOW + 120)["records"][1] == {
        "ref": "111.4242.6", "chat": "111.4242", "author": "contact", "name": "Ann Lee",
        "date": "2027-01-15T08:00:00Z", "text": "at 9:30", "reply_to": "111.4242.5", "forwarded": False,
        "offline": False, "media": [{"kind": "photo", "size": 10, "width": 4, "height": 3}],
        "versions": [{"edited": None, "text": "at 9"}, {"edited": "2027-01-15T08:01:00Z", "text": "at 9:30"}],
        "deleted": "2027-01-15T08:02:00Z", "link": {"kind": "chat", "url": "tg://user?id=4242"},
    }


def test_a_window_gives_the_newest_records_before_a_ref_oldest_first(store):
    # B8. Bugs: the oldest records returned instead of the newest; "before" inclusive; more always false.
    for n in range(1, 8):
        _keep(store, _seen(n, date=NOW + n))
    _keep(store, _seen(50, BOB))

    first = _ask(store, "window", chat="111.4242", limit="3", now=NOW + 10)
    older = _ask(store, "window", chat="111.4242", limit="3", before=first["records"][0]["ref"], now=NOW + 10)
    rest = _ask(store, "window", chat="111.4242", limit="3", before=older["records"][0]["ref"], now=NOW + 10)

    assert [r["ref"] for r in first["records"]] == ["111.4242.5", "111.4242.6", "111.4242.7"] and first["more"]
    assert [r["ref"] for r in older["records"]] == ["111.4242.2", "111.4242.3", "111.4242.4"] and older["more"]
    assert [r["ref"] for r in rest["records"]] == ["111.4242.1"] and not rest["more"]
    assert _ask(store, "chats", now=NOW + 10)["chats"] == [
        {"chat": "111.4242", "name": "Ann Lee", "last_date": "2027-01-15T08:00:07Z", "records": 7},
        {"chat": "111.4343", "name": "Bob", "last_date": "2027-01-15T08:00:00Z", "records": 1}]


def test_changes_follow_a_cursor_and_a_purge_past_it_is_reported_as_a_gap(store):
    # B8. Bugs: a cursor that skips or repeats changes; changes removed by a purge passing silently; a reader
    # told of the same gap forever.
    limits = Limits(72, 2)
    small = InboxSettings(OWNER, limits)
    _keep(store, _seen(1), limits)
    _keep(store, _seen(1, text="edited", edited=NOW + 1), limits, NOW + 1)
    page = _ask(store, "changes", small, limit="1")
    rest = _ask(store, "changes", small, since=page["next_cursor"])
    assert [(c["cursor"], c["kind"]) for c in page["changes"]] == [("1", "insert")] and page["more"]
    assert [(c["cursor"], c["kind"], c["record"]["text"]) for c in rest["changes"]] == [("2", "edit", "edited")]
    assert (rest["next_cursor"], rest["more"], rest["gap"]) == ("2", False, False)

    _keep(store, _seen(2, date=NOW + 2), limits, NOW + 2)
    _keep(store, _seen(3, date=NOW + 3), limits, NOW + 3)  # the cap of 2 removes message 1 and its changes
    behind = _ask(store, "changes", small, since="1")
    assert [(c["cursor"], c["record"]["ref"]) for c in behind["changes"]] == [("3", "111.4242.2"),
                                                                             ("4", "111.4242.3")]
    assert behind["gap"] is True
    caught_up = _ask(store, "changes", small, since=behind["next_cursor"])
    assert (caught_up["changes"], caught_up["gap"]) == ([], False)


def test_every_command_purges_what_has_expired_before_it_answers(store):
    # Storage: "a purge runs before every CLI command answers". Bug: a reader shown a record past 72 hours.
    _keep(store, _seen(1, date=NOW))
    later = NOW + 72 * 3600 + 1

    assert _ask(store, "window", chat="111.4242", now=later)["records"] == []
    _keep(store, _seen(2, date=later), now=later)
    assert _ask(store, "purge", now=later + 72 * 3600 + 1) == {"contract_version": 1, "removed": 1}


def test_with_intake_off_the_file_expires_by_the_limits_it_was_written_under(store):
    _keep(store, _seen(1, date=NOW), Limits(1, 5))
    out = _ask(store, "status", None, NOW + 3601)
    assert out["intake"] == "off" and out["intake_error"] == "not configured"
    assert out["limits"] == {"retention_hours": 1, "max_messages_per_chat": 5, "source": "recorded"}
    assert out["records"] == 0
    gone = _ask(store, "changes", None, NOW + 3601)  # every change went with its record
    assert (gone["changes"], gone["gap"], gone["next_cursor"]) == ([], True, "1")
    assert _ask(store, "changes", None, NOW + 3601, since=gone["next_cursor"])["gap"] is False


@pytest.mark.parametrize("command, options, code", [
    ("changes", {"since": "-1"}, "bad_cursor"),
    ("changes", {"limit": "0"}, "bad_limit"),
    ("window", {"chat": "4242"}, "bad_ref"),
    ("window", {"chat": "111.4242", "before": "111.4343.1"}, "bad_ref"),  # a record of another chat
    ("window", {"chat": "111.4242", "before": "111.4242.99"}, "unknown_ref"),
])
def test_arguments_outside_the_contract_are_refused_with_a_code(store, command, options, code):
    with pytest.raises(ContractError) as refused:
        _ask(store, command, **options)
    assert refused.value.code == code


# --- the real CLI ----------------------------------------------------------------------------------------


def _home(tmp_path: Path) -> Path:
    """A Hermes home with this plugin installed and enabled and an inbox for owner 111 (docs/install.md)."""
    home = tmp_path / "home" / ".hermes"
    shutil.copytree(PLUGIN_DIR, home / "plugins" / "platforms" / "telegram",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - platforms/telegram\nplatforms:\n  telegram:\n"
                                      "    extra:\n      business_inbox:\n        owners:\n          '111': default\n",
                                      encoding="utf-8")
    return home


def _hermes(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "TELEGRAM_"))}
    env.update(HOME=str(home.parent), HERMES_HOME=str(home))
    hermes = Path(sys.executable).with_name("hermes")
    return subprocess.run([str(hermes), *args], env=env, cwd=home.parent, capture_output=True, text=True,
                          timeout=120)


def test_hermes_telegram_inbox_runs_from_the_plugin_and_reads_the_profiles_settings(tmp_path):
    # Slice 2's proof on the pin: Hermes imports a deferred platform for a CLI command only when the command is
    # named as the platform. Bugs: the command missing; the CLI reading another home or not the profile's config.
    home = _home(tmp_path)
    store = InboxStore(home / STORE_NAME)
    store.save_connection(ConnectionSeen(CID, OWNER, True, {}), None, int(time.time()))
    store.keep_message(_seen(5, text="kept for the reader", date=int(time.time())), OWNER, SETTINGS.limits, None,
                       int(time.time()))

    status = _hermes(home, "telegram", "inbox", "status")
    window = _hermes(home, "telegram", "inbox", "window", "--chat", "111.4242")
    refused = _hermes(home, "telegram", "inbox", "changes", "--since", "soon")

    assert status.returncode == 0, status.stderr
    out = json.loads(status.stdout)
    assert (out["contract_version"], out["intake"], out["limits"]["source"], out["records"]) == (1, "on", "config", 1)
    assert [r["text"] for r in json.loads(window.stdout)["records"]] == ["kept for the reader"]
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["error"]["code"] == "bad_cursor"
