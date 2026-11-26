"""Readers of the evidence a row checks: the record of Bot API calls (``recorder``), read as what the chat
shows, and the agent's log for the chat (Hermes' ``state.db`` in the acceptance home).

Telegram's types are read as the Bot API documents them (core.telegram.org/bots/api, read 2026-11-26):
a Message's ``rich_message`` is a RichMessage, whose ``blocks`` are RichBlocks with a ``type`` ("heading",
"paragraph", "table" with ``cells``, "mathematical_expression" with ``expression``, ...), and a RichText is
a string, an array of RichTexts, or an object with a ``type`` and its ``text``.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

from hermes_telegram.messages import EFFECTS
from hermes_telegram.widgets import check_widget

from .home import HOME
from .recorder import Call, Update

DRAFTS = {"sendRichMessageDraft", "sendMessageDraft"}
# Fields of rich blocks and texts that are not text a reader sees.
_NOT_TEXT = {"type", "url", "align", "valign", "size", "colspan", "rowspan", "language", "custom_emoji_id",
             "name", "anchor", "user", "unix_time", "date_time_format", "width", "height", "file_id"}
# Arrays of blocks or rows, read one per line; any other array is RichText, read run together.
_LINES = {"blocks", "items", "cells", "rows", "photos", "documents"}
_EFFECT_NAMES = {v: k for k, v in EFFECTS.items()}


def plain(rich: Any) -> str:
    """The text of a RichText or RichBlock tree, without formatting; a table's cells apart by tabs."""
    if rich is None or isinstance(rich, (bool, int, float)):
        return ""
    if isinstance(rich, str):
        return rich
    if isinstance(rich, list):
        return "".join(plain(x) for x in rich)
    if rich.get("type") == "mathematical_expression":
        return str(rich.get("expression", ""))
    if rich.get("type") == "table":
        return "\n".join("\t".join(plain(c.get("text")) for c in row) for row in rich.get("cells") or ())
    if "label" in rich and isinstance(rich.get("blocks"), list):  # a list item: its bullet or number, then it
        return f"{rich['label']} " + "\n".join(p for p in (plain(b) for b in rich["blocks"]) if p)
    parts = []
    for key, value in rich.items():
        if key in _NOT_TEXT:
            continue
        if key in _LINES and isinstance(value, list):
            parts.append("\n".join(p for p in (plain(v) for v in value) if p))
        elif isinstance(value, (str, list, dict)):
            parts.append(plain(value))
    return "\n".join(p for p in parts if p)


def nodes(message: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every block and rich text of a message's rich content, nested ones included, in order."""
    out: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "type" in value and "label" not in value:  # a list item's type is its numbering style
                out.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(((message or {}).get("rich_message") or {}).get("blocks"))
    return out


def of_type(message: Optional[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [n for n in nodes(message) if n.get("type") == kind]


def table_cells(table: dict[str, Any]) -> list[list[str]]:
    return [[plain(c.get("text")).strip() for c in row] for row in table.get("cells") or ()]


def shown(message: Optional[dict[str, Any]]) -> str:
    """What the owner reads of a message as Telegram stored it."""
    if not message:
        return ""
    rich = message.get("rich_message")
    if rich:
        return "\n".join(p for p in (plain(b) for b in rich.get("blocks") or ()) if p)
    return str(message.get("text") or message.get("caption") or "")


def messages(calls: list[Call]) -> list[tuple[Call, dict[str, Any]]]:
    """The messages the bot put in the chat by these calls (sends, not edits or drafts), oldest first."""
    out: list[tuple[Call, dict[str, Any]]] = []
    for call in calls:
        if not call.ok or not call.method.startswith("send") or call.method in DRAFTS:
            continue
        for message in call.result if isinstance(call.result, list) else [call.result]:
            if isinstance(message, dict) and "message_id" in message:
                out.append((call, message))
    return out


def deleted(calls: list[Call]) -> set[int]:
    ids: set[int] = set()
    for call in calls:
        if call.ok and call.method == "deleteMessage":
            ids.add(int(call.params.get("message_id", 0)))
        elif call.ok and call.method == "deleteMessages":
            ids.update(int(i) for i in call.params.get("message_ids") or ())
    return ids


@dataclass
class Frame:
    """One frame of the preview (a draft) as the bot sent it."""

    at: float
    text: str
    can_stop: bool


def frames(calls: list[Call]) -> list[Frame]:
    out = []
    for call in calls:
        if call.method in DRAFTS and call.ok:
            rich = call.params.get("rich_message")
            text = rich.get("markdown", "") if isinstance(rich, dict) else str(call.params.get("text", ""))
            out.append(Frame(call.at, text, call.params.get("can_stop") in (True, "true", "True")))
    return out


def uploads(call: Call) -> list[dict[str, Any]]:
    """The files a call uploaded, in the order it lists them (an album's ``media`` order)."""
    files = {k: v for k, v in call.params.items() if isinstance(v, dict) and "upload" in v}
    media = call.params.get("media")
    if isinstance(media, list):
        ordered = []
        for item in media:
            ref = str(item.get("media", "")) if isinstance(item, dict) else ""
            if ref.startswith("attach://") and ref[9:] in files:
                ordered.append({**files[ref[9:]], "type": item.get("type")})
        return ordered
    return list(files.values())


# --- what the record says, for the owner to compare with iOS -------------------------------------------------

def _clock(at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(at))


def _media_kind(message: dict[str, Any]) -> str:
    return next((k for k in ("photo", "video", "audio", "voice", "animation", "sticker", "document", "poll")
                 if k in message), "")


def _buttons(markup: Any) -> list[str]:
    rows = (markup or {}).get("inline_keyboard") if isinstance(markup, dict) else None
    return [str(b.get("text")) for row in rows or () for b in row if isinstance(b, dict)]


def _message_lines(message: dict[str, Any]) -> list[str]:
    lines = []
    text = shown(message)
    if text:
        body = text.splitlines()
        lines += [f"| {line}" for line in body[:14]] + ([f"| ... {len(body) - 14} more lines"] if len(body) > 14 else [])
    kinds = [n["type"] for n in nodes(message)]
    if kinds:
        counted = {k: kinds.count(k) for k in dict.fromkeys(kinds)}
        lines.append("rich blocks: " + ", ".join(f"{k} x{n}" if n > 1 else k for k, n in counted.items()))
    for table in of_type(message, "table"):
        cells = table_cells(table)
        lines.append(f"table {len(cells[0]) if cells else 0} columns x {len(cells)} rows:")
        lines += ["  " + " | ".join(row) for row in cells]
    for maths in of_type(message, "mathematical_expression"):
        lines.append(f"formula: {maths.get('expression')}")
    kind = _media_kind(message)
    if kind:
        detail = message.get(kind)
        name = detail.get("file_name") if isinstance(detail, dict) else None
        lines.append(f"arrived as: {kind}" + (f", file name {name}" if name else ""))
    if message.get("effect_id"):
        lines.append(f"effect: {message['effect_id']} {_EFFECT_NAMES.get(str(message['effect_id']), '(not one of ours)')}")
    quote = (message.get("quote") or {}).get("text")
    reply = (message.get("reply_to_message") or {}).get("message_id")
    if reply:
        lines.append(f"replies to message {reply}" + (f", quoting \"{quote}\"" if quote else ""))
    labels = _buttons(message.get("reply_markup"))
    if labels:
        lines.append("buttons: " + ", ".join(labels))
    if message.get("media_group_id"):
        lines.append(f"album {message['media_group_id']}")
    return lines


def _call_lines(call: Call) -> list[str]:
    p = call.params
    lines = []
    for item in uploads(call):
        lines.append(f"upload {item['upload']} ({item['size']} bytes, sha256 {item['sha256'][:12]})"
                     + (f" as {item['type']}" if item.get("type") else ""))
    if call.method == "sendWidget":
        html = str(p.get("html", ""))
        verdict = check_widget(html)
        lines.append(f"widget \"{p.get('caption', '')}\": {len(html.encode())} bytes of HTML, "
                     + ("no network use found" if verdict is None else f"REFUSED by the check: {verdict}"))
    elif call.method == "sendPoll":
        options = [o.get("text") if isinstance(o, dict) else o for o in p.get("options") or ()]
        lines.append(f"poll \"{p.get('question')}\": " + ", ".join(map(str, options)))
    elif call.method == "setMessageReaction":
        emoji = [r.get("emoji") for r in p.get("reaction") or () if isinstance(r, dict)]
        lines.append(f"reaction {' '.join(map(str, emoji)) or '(cleared)'} on message {p.get('message_id')}")
    elif call.method in ("pinChatMessage", "unpinChatMessage", "deleteMessage"):
        lines.append(f"message {p.get('message_id')}")
    elif call.method == "answerCallbackQuery":
        lines.append(f"tap answered: \"{p.get('text', '')}\"")
    elif call.method.startswith("edit") and isinstance(call.result, dict):
        lines.append(f"message {call.result.get('message_id')} now reads:")
    return lines


def _update_line(update: Update) -> str:
    body = update.body
    match update.kind:
        case "message":
            what = body.get("text") or ("a sticker " + str((body.get("sticker") or {}).get("emoji", ""))
                                        if body.get("sticker") else _media_kind(body) or "a message")
            return f"owner sent: {what}"
        case "callback_query":
            return f"owner tapped a button (data {body.get('data')!r}) on message {(body.get('message') or {}).get('message_id')}"
        case "poll_answer":
            return f"owner voted option {body.get('option_ids')}"
        case "message_reaction":
            emoji = [r.get("emoji") for r in body.get("new_reaction") or () if isinstance(r, dict)]
            return f"owner reacted {' '.join(map(str, emoji)) or '(cleared)'} on message {body.get('message_id')}"
        case "widget_query":
            return f"owner's widget asked: {body.get('data')!r} (widget message {body.get('message_id')})"
        case "stopped_message_generation":
            return "owner tapped Stop on the preview"
    return f"Telegram delivered: {update.kind}"


def timeline(calls: list[Call], updates: list[Update]) -> list[str]:
    """What happened in the chat, both ways, in time order; the preview's frames summed up per run."""
    events: list[tuple[float, list[str]]] = [(u.at, [f"{_clock(u.at)}  {_update_line(u)}"]) for u in updates]
    run: list[Frame] = []

    def flush() -> None:
        if run:
            statuses = list(dict.fromkeys(f.text.split("</tg-thinking>")[0].replace("<tg-thinking>", "")
                                          for f in run if "<tg-thinking>" in f.text))
            events.append((run[0].at, [f"{_clock(run[0].at)}  preview: {len(run)} frames until {_clock(run[-1].at)}"
                                       f", Stop button {'on' if any(f.can_stop for f in run) else 'OFF'}",
                                       "      statuses: " + " > ".join(statuses or ["(none)"]),
                                       f"      longest frame: {max(len(f.text) for f in run)} characters"]))
            run.clear()

    for call in calls:
        if call.method in DRAFTS:
            run.append(Frame(call.at, *_frame_fields(call)))
            continue
        flush()
        head = f"{_clock(call.at)}  bot {call.method}" + ("" if call.ok else f"  REFUSED by Telegram: {call.error}")
        body = _call_lines(call)
        sent = messages([call])
        if sent:
            for _, message in sent:
                body += [f"message {message.get('message_id')}"] + ["  " + line for line in _message_lines(message)]
        elif call.ok and isinstance(call.result, dict) and "message_id" in call.result:
            body += ["  " + line for line in _message_lines(call.result)]
        events.append((call.at, [head] + ["      " + line for line in body]))
    flush()
    return [line for _, block in sorted(events, key=lambda e: e[0]) for line in block]


def _frame_fields(call: Call) -> tuple[str, bool]:
    frame = frames([call])
    return (frame[0].text, frame[0].can_stop) if frame else ("", False)


# --- the agent's log ------------------------------------------------------------------------------------------

def agent_log(owner_id: int, since: float) -> list[dict[str, Any]]:
    """Every message Hermes recorded in the owner's private chat since ``since`` (Unix time), oldest first, as
    ``{"role", "content", "tool_name", "tool_calls", "at"}``."""
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
    """What reached the agent from the chat since ``since``: user messages, optionally only those holding
    ``prefix`` (``[button]``, ``[widget]``, ``[poll]``, ``[reaction]``)."""
    texts = [m["content"] for m in agent_log(owner_id, since) if m["role"] == "user"]
    return [t for t in texts if prefix is None or prefix in t]
