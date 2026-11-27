"""``hermes telegram inbox``: the Business inbox's read contract (docs/business-inbox.md, Read contract; slice 2).

Each command prints one JSON object with ``contract_version: 1`` and reads the base of the profile it runs in
(``HERMES_HOME``, ``hermes -p``); none takes a profile or an owner. With intake on it reads the configured owner's
chats only. The file's schema stays private to the plugin: readers depend on this output only. Every command runs
in one transaction that first purges what has expired, by the settings in effect, or by the limits the file was
last written under when intake is off, so what it prints is what the purge left.

Refs are opaque strings; a reader passes them back as given and never builds them. Any argument outside the
contract prints an ``error`` object on stdout and exits 2, before the file is touched; a file this version cannot
use exits 1.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from typing import Any, NoReturn, Optional

from . import inbox
from .inbox import (
    STORE_NAME, ChatId, InboxConfigError, InboxSettings, InboxStore, InboxStoreError, InboxView, Limits, MessageId,
    OwnerId, Record, inbox_settings,
)

CONTRACT_VERSION = 1
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
COMMANDS = ("status", "chats", "changes", "window", "purge")
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
        # ``edited`` is null for the message as first sent; a record first seen through an edit (its original
        # came while the bot was not connected) starts with that edit's time.
        "versions": [{"edited": iso(v.edited) if v.edited else None, "text": v.text} for v in record.versions],
        "deleted": iso(record.deleted),
        "link": link(record.chat),
    }


# --- arguments: parsed here, so every refusal is the contract's ------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ContractError("bad_arguments", message)


def _command_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="hermes telegram inbox", allow_abbrev=False, add_help=False)
    ops = parser.add_subparsers(dest="command", parser_class=_Parser)
    for name in ("status", "chats", "purge"):
        ops.add_parser(name, allow_abbrev=False, add_help=False)
    changes_parser = ops.add_parser("changes", allow_abbrev=False, add_help=False)
    changes_parser.add_argument("--since")
    changes_parser.add_argument("--limit")
    window_parser = ops.add_parser("window", allow_abbrev=False, add_help=False)
    window_parser.add_argument("--chat")
    window_parser.add_argument("--before")
    window_parser.add_argument("--limit")
    return parser


def parse(argv: list[str]) -> argparse.Namespace:
    if not argv or argv[0] not in COMMANDS:
        raise ContractError("bad_command", "use one of: " + ", ".join(COMMANDS))
    args = _command_parser().parse_args(argv)
    args.limit = _limit(getattr(args, "limit", None))
    if args.command == "changes":
        since = args.since
        if since is not None and not _CURSOR.fullmatch(since):
            raise ContractError("bad_cursor", "--since takes a cursor as `changes` printed it in next_cursor")
        args.since = int(since or 0)
    if args.command == "window":
        if args.chat is None:
            raise ContractError("bad_ref", "window needs --chat, a chat ref as `chats` prints it")
        args.chat = _parse_chat(args.chat)
        args.before = _parse_record(args.before) if args.before is not None else None
        if args.before is not None and args.before[:2] != args.chat:
            raise ContractError("bad_ref", "--before names a record of another chat")
    return args


def _limit(value: Optional[str]) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if not re.fullmatch(r"[0-9]{1,6}", value) or not 1 <= int(value) <= MAX_LIMIT:
        raise ContractError("bad_limit", f"--limit must be a whole number from 1 to {MAX_LIMIT}")
    return int(value)


def _parse_chat(ref: str) -> tuple[OwnerId, ChatId]:
    match = _CHAT_REF.fullmatch(ref)
    if match is None:
        raise ContractError("bad_ref", "--chat takes a chat ref as `chats` prints it")
    return OwnerId(int(match[1])), ChatId(int(match[2]))


def _parse_record(ref: str) -> tuple[OwnerId, ChatId, MessageId]:
    match = _RECORD_REF.fullmatch(ref)
    if match is None:
        raise ContractError("bad_ref", "--before takes a record ref as `window` or `changes` prints it")
    return OwnerId(int(match[1])), ChatId(int(match[2])), MessageId(int(match[3]))


# --- the commands, inside one session ---------------------------------------------------------------------


def _limits_in_effect(settings: Optional[InboxSettings], recorded: Optional[Limits]) -> tuple[Limits, str]:
    if settings is not None:
        return settings.limits, "config"
    if recorded is not None:
        return recorded, "recorded"
    return inbox.DEFAULT_LIMITS, "default"


def _own(owner: Optional[OwnerId], chat_owner: OwnerId) -> None:
    """With intake on, a ref of another owner's chat names nothing this profile may read."""
    if owner is not None and chat_owner != owner:
        raise ContractError("unknown_ref", "the ref names a chat of an owner this profile does not keep")


def respond(args: argparse.Namespace, view: InboxView, settings: Optional[InboxSettings],
            intake_error: Optional[str], now: int) -> dict[str, Any]:
    owner = settings.owner if settings is not None else None
    if args.command == "status":
        state = view.status(owner)
        limits, source = _limits_in_effect(settings, state.limits)
        body: dict[str, Any] = {
            "intake": "on" if settings is not None else "off",
            **({"intake_error": intake_error} if intake_error else {}),
            "limits": {"retention_hours": limits.retention_hours, "max_messages_per_chat": limits.max_per_chat,
                       "source": source},
            "connections": [{"connection": cid, "owner": o, "enabled": enabled, "rights": rights}
                            for cid, o, enabled, rights in state.connections],
            "records": state.records, "chats": state.chats,
            "oldest_record_age_secs": now - state.oldest if state.oldest is not None else None,
        }
    elif args.command == "chats":
        body = {"chats": [{"chat": chat_ref(o, c), "name": name, "last_date": iso(last), "records": count}
                          for o, c, name, last, count in view.chats(owner)]}
    elif args.command == "changes":
        page = view.changes_since(args.since, args.limit, owner)
        body = {"changes": [{"cursor": str(seq), "kind": kind.value, "at": iso(at), "record": record_json(record)}
                            for seq, kind, at, record in page.items],
                "next_cursor": str(page.next_cursor), "more": page.more, "gap": page.gap}
    elif args.command == "window":
        chat_owner, chat = args.chat
        _own(owner, chat_owner)
        position = None
        if args.before is not None:
            anchor = view.record(*args.before)
            if anchor is None:
                raise ContractError("unknown_ref", "--before names a record the inbox does not hold (it may have "
                                                   "expired)")
            position = (anchor.date, anchor.message)
        records, more = view.window(chat_owner, chat, position, args.limit)
        body = {"chat": chat_ref(chat_owner, chat), "records": [record_json(r) for r in records], "more": more}
    else:  # purge: the session's purge is the answer
        body = {"removed": view.removed}
    return {"contract_version": CONTRACT_VERSION, **body}


def answer(argv: list[str], store: InboxStore, settings: Optional[InboxSettings], intake_error: Optional[str],
           now: int) -> dict[str, Any]:
    """One command's JSON object: the arguments checked first, then one session that purges and reads."""
    args = parse(argv)
    with store.session(settings.limits if settings is not None else None, now) as view:
        return respond(args, view, settings, intake_error, now)


# --- the CLI ----------------------------------------------------------------------------------------------


def setup(parser: argparse.ArgumentParser) -> None:
    """``hermes telegram``: this plugin's commands; ``inbox`` is the Business inbox's read contract. Its own
    arguments are parsed by ``parse``, so an argument outside the contract is refused as JSON."""
    commands = parser.add_subparsers(dest="telegram_command", metavar="{inbox}")
    inbox_parser = commands.add_parser("inbox", help="Read the Business inbox (JSON, contract_version 1)",
                                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                       epilog="commands: status | chats | changes [--since CURSOR] [--limit N] | "
                                              "window --chat REF [--before REF] [--limit N] | purge")
    inbox_parser.add_argument("argv", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)


def _settings() -> tuple[Optional[InboxSettings], Optional[str]]:
    """The profile's settings as the gateway reads them, or why intake is off."""
    import os

    from gateway.config import Platform, load_gateway_config
    from hermes_cli.profiles import get_active_profile_name

    try:
        platform = load_gateway_config().platforms.get(Platform.TELEGRAM)
        if platform is None or not platform.enabled:
            # The gateway serves no disabled platform (gateway/config.py, get_connected_platforms).
            return None, "Telegram is not enabled in this profile"
        return inbox_settings(platform.extra or {}, os.environ, get_active_profile_name()), None
    except InboxConfigError as exc:
        return None, str(exc)
    except Exception as exc:  # an unreadable config: read as intake off, so the purge uses the file's own limits
        return None, f"the profile's configuration could not be read ({type(exc).__name__})"


def run(args: argparse.Namespace) -> int:
    """The handler Hermes calls for ``hermes telegram ...``; its return is the exit code."""
    from hermes_constants import get_hermes_home

    if getattr(args, "telegram_command", None) != "inbox":
        print("usage: hermes telegram inbox {status,chats,changes,window,purge}", file=sys.stderr)
        return 2
    store = InboxStore(get_hermes_home() / STORE_NAME)
    try:
        settings, intake_error = _settings()
        out, code = answer(list(args.argv or ()), store, settings, intake_error, int(time.time())), 0
    except ContractError as exc:
        out, code = _error(exc.code, str(exc)), 2
    except (InboxStoreError, sqlite3.Error, OSError, ValueError) as exc:
        # Named by kind only: an SQLite or decoding message may quote stored content.
        message = str(exc) if isinstance(exc, InboxStoreError) else f"the inbox file could not be used ({type(exc).__name__})"
        out, code = _error("store", message), 1
    print(json.dumps(out, ensure_ascii=False))
    return code


def _error(code: str, message: str) -> dict[str, Any]:
    return {"contract_version": CONTRACT_VERSION, "error": {"code": code, "message": message}}
