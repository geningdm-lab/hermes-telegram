"""The acceptance kit (acceptance/): its proxy, readers and playbook decide what a live run checks, so a wrong
one makes a good run fail or a bad one pass. Checked here offline: the proxy with python-telegram-bot's own
requests in front of a fake Telegram, the readers on Bot API types as core.telegram.org documents them."""

import argparse
import hashlib
import re
from pathlib import Path

from aiohttp import web
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from acceptance import rows
from acceptance.evidence import messages, of_type, shown, table_cells, timeline, uploads
from acceptance.recorder import Recorder

TOKEN = "123456:acceptance-test-token-value"
_CHAT = {"id": 42, "type": "private", "first_name": "Owner"}


async def _fake_telegram(received: list[tuple[str, bytes]]) -> tuple[web.AppRunner, str]:
    updates = [[{"update_id": 9, "message": {"message_id": 5, "date": 0, "chat": _CHAT, "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
                                             "text": "r2"}}]]

    async def api(request: web.Request) -> web.Response:
        method = request.match_info["method"]
        received.append((method, await request.read()))
        base = {"message_id": 7, "date": 0, "chat": _CHAT}
        if method == "getUpdates":
            return web.json_response({"ok": True, "result": updates.pop(0) if updates else []})
        result = {
            "getMe": {"id": 1, "is_bot": True, "first_name": "Bot", "username": "bot"},
            "sendDocument": {**base, "document": {"file_id": "f", "file_unique_id": "u", "file_name": "sample.zip"}},
            "sendMessage": {**base, "text": "Pick", "reply_markup": {"inline_keyboard": [[{"text": "Rome",
                                                                                           "callback_data": "rome"}]]}},
        }[method]
        return web.json_response({"ok": True, "result": result})

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def test_the_proxy_passes_calls_through_unchanged_and_records_them_without_the_token(tmp_path: Path) -> None:
    received: list[tuple[str, bytes]] = []
    upstream, upstream_url = await _fake_telegram(received)
    log = tmp_path / "record.jsonl"
    recorder = Recorder(log, TOKEN, upstream=upstream_url)
    api = await recorder.start()
    # Bytes a sloppy multipart reader changes: CRLFs and a boundary-like line inside the file.
    data = b"\r\n--x\r\nnot a part\r\n\r\n" + bytes(range(256)) * 40 + b"\r\n"
    try:
        async with Bot(TOKEN, base_url=f"{api}/bot", base_file_url=f"{api}/file/bot") as bot:
            sent = await bot.send_document(42, document=data, filename="sample.zip")
            await bot.send_message(42, "Pick", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Rome", callback_data="rome")]]))
            await bot.get_updates(timeout=0)
            await bot.get_updates(timeout=0)
    finally:
        await recorder.stop()
        await upstream.cleanup()

    assert sent.document.file_name == "sample.zip"  # Telegram's answer reached the bot
    upload = next(body for method, body in received if method == "sendDocument")
    assert data in upload  # forwarded as python-telegram-bot sent it
    (document,) = [c for c in recorder.calls if c.method == "sendDocument"]
    assert uploads(document) == [{"upload": "sample.zip", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
    (choice,) = [c for c in recorder.calls if c.method == "sendMessage"]
    assert choice.params["reply_markup"] == {"inline_keyboard": [[{"text": "Rome", "callback_data": "rome"}]]}
    assert str(choice.params["chat_id"]) == "42" and choice.params["text"] == "Pick"
    assert [u.kind for u in recorder.updates] == ["message"] and recorder.updates[0].body["text"] == "r2"
    assert not [c for c in recorder.calls if c.method == "getUpdates"]  # empty polls are not calls
    assert TOKEN not in log.read_text() and TOKEN.split(":")[1] not in log.read_text()


def _rich(*blocks: dict) -> dict:
    return {"message_id": 3, "date": 0, "chat": _CHAT, "rich_message": {"blocks": list(blocks)}}


def test_the_readers_see_a_rich_message_as_the_owner_reads_it() -> None:
    from acceptance.recorder import Call

    table = {"type": "table", "cells": [[{"text": {"type": "bold", "text": c}, "align": "left", "valign": "top"}
                                         for c in r] for r in rows.TABLE_ROWS]}
    message = _rich({"type": "heading", "size": 1, "text": "Heading one"},
                    {"type": "paragraph", "text": ["Plain, ", {"type": "bold", "text": "bold"}, " and a ",
                                                   {"type": "url", "text": "link", "url": "https://x.example"}]},
                    {"type": "mathematical_expression", "expression": rows.FORMULA}, table)
    text = shown(message)
    assert "Heading one" in text and "Plain, bold and a link" in text and rows.FORMULA in text
    assert "x.example" not in text and not rows._LITERAL.search(text)
    assert rows._LITERAL.search("Plain, **bold**")
    (found,) = of_type(message, "table")
    assert table_cells(found) == rows.TABLE_ROWS
    assert [n["expression"] for n in of_type(message, "mathematical_expression")] == [rows.FORMULA]

    album = Call(0.0, "sendMediaGroup", {"media": [{"type": "photo", "media": "attach://b"},
                                                   {"type": "photo", "media": "attach://a"}],
                                         "a": {"upload": "a.png", "size": 1, "sha256": "aa"},
                                         "b": {"upload": "b.png", "size": 1, "sha256": "bb"}}, True,
                 result=[{"message_id": 1, "photo": [], "media_group_id": "g"},
                         {"message_id": 2, "photo": [], "media_group_id": "g"}])
    assert [u["upload"] for u in uploads(album)] == ["b.png", "a.png"]  # the album's order, not the form's
    assert [m["message_id"] for _, m in messages([album])] == [1, 2]
    assert any("table 4 columns x 7 rows:" in line for line in timeline([Call(0.0, "sendRichMessage", {}, True,
                                                                               result=message)], []))


def test_every_code_a_row_asks_for_is_in_the_playbook_and_the_playbook_fits_soul_md() -> None:
    source = Path(rows.__file__).read_text()
    asked = {code.split()[0] for code in re.findall(r'run\.send\("([^"]+)"', source)}
    asked |= {code for code in re.findall(r'\(\("(r\w+)", ', source)}  # row 4's loop over its codes
    asked |= set(re.findall(r'\("(r1l?)", "(?:short|long)"\)', source))
    assert asked and asked <= set(rows.PLAYBOOK), asked - set(rows.PLAYBOOK)
    text = rows.playbook({n: Path(f"/samples/{n}") for n in _SAMPLE_NAMES})
    assert len(text) < 20_000  # Hermes truncates a SOUL.md beyond CONTEXT_FILE_MAX_CHARS (agent/prompt_builder.py)


_SAMPLE_NAMES = ["document", "photo", "video", "audio", "voice", "big", "empty", "stop",
                 *(f"photo-{n:02d}" for n in range(1, 13))]


def test_the_report_names_each_failed_check_and_the_owners_no() -> None:
    from acceptance.__main__ import _report
    from acceptance.rows import Result

    ok = rows.Result("2", "A table", checks=[("cells equal", True, "")], observed=[("looks right?", True)])
    bad = Result("11", "A choice", checks=[("one incoming choice", False, "['a', 'b']")], observed=[("notice?", False)])
    report = _report([ok, bad], 0.0, argparse.Namespace(bot="vtadevbot", provider="anthropic", model="m"), "rec.jsonl")
    assert "| 2 | A table | pass |" in report and "| 11 | A choice | FAIL |" in report
    assert "- FAIL: one incoming choice (['a', 'b'])" in report and "- owner NO: notice?" in report
