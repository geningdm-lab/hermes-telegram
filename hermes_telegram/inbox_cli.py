"""``hermes telegram inbox``: the Business inbox's read contract (docs/business-inbox.md, Read contract; slice 2).

Each command prints one JSON object with ``contract_version: 1`` and reads the base of the profile it runs in
(``HERMES_HOME``, ``hermes -p``); none takes a profile or an owner. The file's schema stays private to the plugin:
readers depend on this output only. Every command first purges what has expired, by the settings in effect, or by
the limits the file was last written under when intake is off.

Refs are opaque strings; a reader passes them back as given and never builds them. A bad argument prints an
``error`` object and exits 2; a file this version cannot use exits 1.
"""

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any, Optional

from . import inbox
from .inbox import (
    STORE_NAME, ChatId, InboxConfigError, InboxSettings, InboxStore, InboxStoreError, Limits, MessageId, OwnerId,
    Record, inbox_settings,
)

CONTRACT_VERSION = 1
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
_CHAT_REF = re.compile(r"([0-9]{1,16})\.(-?[0-9]{1,16})")
_RECORD_REF = re.compile(r"([0-9]{1,16})\.(-?[0-9]{1,16})\.([0-9]{1,16})")
_CURSOR = re.compile(r"[0-9]{1,18}")


class ContractError(ValueError):
    """An argument the contract does not accept; ``code`` names it for readers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- the output's shapes ----------------------------------------------------------------------------------


def chat_ref(owner: int, chat: int) -> str:
    return f"{owner}.{chat}"


def record_ref(owner: int, chat: int, message: int) -> str:
    return f"{owner}.{chat}.{message}"


def iso(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def link(chat: int) -> dict[str, Any]:
    """A link for the record. No form is documented for a message in a private chat (api/links, Message links),
    so this version links the other person, with the Bot API's mention link."""
    return {"kind": "chat", "url": f"tg://user?id={chat}"}


def record_json(record: Record) -> dict[str, Any]:
    latest = record.versions[-1].text if record.versions else None
    return {
        "ref": record_ref(record.owner, record.chat, record.message),
        "chat": chat_ref(record.owner, record.chat),
        "author": record.author.value,
        "name": record.name,
        "date": iso(record.date),
        "text": latest,
        "reply_to": record_ref(record.owner, record.chat, record.reply_to) if record.reply_to is not None else None,
        "forwarded": record.forwarded,
        "offline": record.offline,
        "media": [{k: v for k, v in vars(a).items() if v is not None} for a in record.media],
        "versions": [{"edited": iso(v.edited) if v.edited else None, "text": v.text} for v in record.versions],
        "deleted": iso(record.deleted),
        "link": link(record.chat),
    }


# --- the commands -----------------------------------------------------------------------------------------


def _limit(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    if not re.fullmatch(r"[0-9]{1,6}", value) or not 1 <= int(value) <= MAX_LIMIT:
        raise ContractError("bad_limit", f"--limit must be a whole number from 1 to {MAX_LIMIT}")
    return int(value)


def _parse_chat(ref: str) -> tuple[OwnerId, ChatId]:
    match = _CHAT_REF.fullmatch(ref or "")
    if match is None:
        raise ContractError("bad_ref", "--chat takes a chat ref as `chats` prints it")
    return OwnerId(int(match[1])), ChatId(int(match[2]))


def _parse_record(ref: str) -> tuple[OwnerId, ChatId, MessageId]:
    match = _RECORD_REF.fullmatch(ref or "")
    if match is None:
        raise ContractError("bad_ref", "--before takes a record ref as `window` or `changes` prints it")
    return OwnerId(int(match[1])), ChatId(int(match[2])), MessageId(int(match[3]))


def status(store: InboxStore, settings: Optional[InboxSettings], intake_error: Optional[str],
           now: int) -> dict[str, Any]:
    state = store.status()
    limits, source = _limits_in_effect(settings, state.limits)
    return {
        "intake": "on" if settings is not None else "off",
        **({"intake_error": intake_error} if intake_error else {}),
        "limits": {"retention_hours": limits.retention_hours, "max_messages_per_chat": limits.max_per_chat,
                   "source": source},
        "connections": [{"connection": cid, "owner": owner, "enabled": enabled, "rights": rights}
                        for cid, owner, enabled, rights in state.connections],
        "records": state.records,
        "chats": state.chats,
        "oldest_record_age_secs": now - state.oldest if state.oldest is not None else None,
    }


def chats(store: InboxStore) -> dict[str, Any]:
    return {"chats": [{"chat": chat_ref(owner, chat), "name": name, "last_date": iso(last), "records": count}
                      for owner, chat, name, last, count in store.chats()]}


def changes(store: InboxStore, since: Optional[str], limit: Optional[str]) -> dict[str, Any]:
    if since is not None and not _CURSOR.fullmatch(since):
        raise ContractError("bad_cursor", "--since takes a cursor as `changes` printed it in next_cursor")
    cursor = int(since or 0)
    items, more, removed_through = store.changes_since(cursor, _limit(limit, DEFAULT_LIMIT))
    # Past the cursor, changes up to removed_through may be gone: the reader has missed them.
    last = items[-1][0] if items else max(cursor, removed_through)
    return {"changes": [{"cursor": str(seq), "kind": kind.value, "at": iso(at), "record": record_json(record)}
                        for seq, kind, at, record in items],
            "next_cursor": str(last), "more": more, "gap": cursor < removed_through}


def window(store: InboxStore, chat: str, before: Optional[str], limit: Optional[str]) -> dict[str, Any]:
    owner, chat_id = _parse_chat(chat)
    position = None
    if before is not None:
        b_owner, b_chat, b_message = _parse_record(before)
        if (b_owner, b_chat) != (owner, chat_id):
            raise ContractError("bad_ref", "--before names a record of another chat")
        anchor = store.record(b_owner, b_chat, b_message)
        if anchor is None:
            raise ContractError("unknown_ref", "--before names a record the inbox does not hold (it may have expired)")
        position = (anchor.date, anchor.message)
    records, more = store.window(owner, chat_id, position, _limit(limit, DEFAULT_LIMIT))
    return {"chat": chat_ref(owner, chat_id), "records": [record_json(r) for r in records], "more": more}


def _limits_in_effect(settings: Optional[InboxSettings], recorded: Optional[Limits]) -> tuple[Limits, str]:
    if settings is not None:
        return settings.limits, "config"
    if recorded is not None:
        return recorded, "recorded"
    return inbox.DEFAULT_LIMITS, "default"


# --- the CLI ----------------------------------------------------------------------------------------------


def setup(parser: argparse.ArgumentParser) -> None:
    """``hermes telegram``: this plugin's commands; ``inbox`` is the Business inbox's read contract."""
    commands = parser.add_subparsers(dest="telegram_command", metavar="{inbox}")
    inbox_parser = commands.add_parser("inbox", help="Read the Business inbox (JSON, contract_version 1)",
                                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ops = inbox_parser.add_subparsers(dest="inbox_command", metavar="{status,chats,changes,window,purge}")
    ops.add_parser("status", help="Connections, counts and the limits in effect; no content")
    ops.add_parser("chats", help="Each chat with its ref, name, last message date and record count")
    changes_parser = ops.add_parser("changes", help="Inserts, edits and deletions after a cursor, in order")
    changes_parser.add_argument("--since", help="the next_cursor of the previous call; omitted: from the start")
    changes_parser.add_argument("--limit", help=f"at most this many (default {DEFAULT_LIMIT}, at most {MAX_LIMIT})")
    window_parser = ops.add_parser("window", help="Records of one chat, oldest first")
    window_parser.add_argument("--chat", required=True, help="a chat ref as `chats` prints it")
    window_parser.add_argument("--before", help="only records before this record ref")
    window_parser.add_argument("--limit", help=f"the newest this many (default {DEFAULT_LIMIT}, at most {MAX_LIMIT})")
    ops.add_parser("purge", help="Remove what has expired now; prints the count")


def _settings() -> tuple[Optional[InboxSettings], Optional[str]]:
    """The profile's settings as the gateway reads them, or why intake is off."""
    import os

    from gateway.config import Platform, load_gateway_config
    from hermes_cli.profiles import get_active_profile_name

    try:
        platform = load_gateway_config().platforms.get(Platform.TELEGRAM)
        return inbox_settings((platform.extra if platform else None) or {}, os.environ,
                              get_active_profile_name()), None
    except InboxConfigError as exc:
        return None, str(exc)
    except Exception as exc:  # an unreadable config: read as intake off, so the purge uses the file's own limits
        return None, f"the profile's configuration could not be read ({type(exc).__name__})"


def answer(command: str, args: argparse.Namespace, store: InboxStore, settings: Optional[InboxSettings],
           intake_error: Optional[str], now: int) -> dict[str, Any]:
    """One command's JSON object, after the purge every command runs first."""
    removed = store.purge(settings.limits if settings is not None else None, now)
    if command == "status":
        body = status(store, settings, intake_error, now)
    elif command == "chats":
        body = chats(store)
    elif command == "changes":
        body = changes(store, args.since, args.limit)
    elif command == "window":
        body = window(store, args.chat, args.before, args.limit)
    elif command == "purge":
        body = {"removed": removed}
    else:
        raise ContractError("bad_command", "use one of: status, chats, changes, window, purge")
    return {"contract_version": CONTRACT_VERSION, **body}


def run(args: argparse.Namespace) -> int:
    """The handler Hermes calls for ``hermes telegram ...``; its return is the exit code."""
    from hermes_constants import get_hermes_home

    if getattr(args, "telegram_command", None) != "inbox":
        print("usage: hermes telegram inbox {status,chats,changes,window,purge}", file=sys.stderr)
        return 2
    command = getattr(args, "inbox_command", None) or ""
    store = InboxStore(get_hermes_home() / STORE_NAME)
    try:
        settings, intake_error = _settings()
        out, code = answer(command, args, store, settings, intake_error, int(time.time())), 0
    except ContractError as exc:
        out, code = {"contract_version": CONTRACT_VERSION, "error": {"code": exc.code, "message": str(exc)}}, 2
    except InboxStoreError as exc:
        out, code = {"contract_version": CONTRACT_VERSION, "error": {"code": "store", "message": str(exc)}}, 1
    print(json.dumps(out, ensure_ascii=False))
    return code
