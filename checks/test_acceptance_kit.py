"""The acceptance kit's readers (acceptance/live.py, rows.py): a wrong reader makes a good live run fail, or a
bad one pass, so they are checked here on Telegram's own types, offline."""

import pytest

telethon = pytest.importorskip("telethon")
types = telethon.tl.types


def _text(value: str):
    return types.TextPlain(text=value)


def test_a_native_table_reads_back_cell_by_cell():
    from acceptance import rows
    from acceptance.live import blocks

    table = types.PageBlockTable(title=_text(""), rows=[
        types.PageTableRow(cells=[types.PageTableCell(text=types.TextBold(text=_text(c))) for c in r])
        for r in rows.TABLE_ROWS])
    message = types.Message(id=1, peer_id=types.PeerUser(user_id=1), message="", rich_message=types.RichMessage(
        blocks=[types.PageBlockParagraph(text=_text("Moons")), table], photos=[], documents=[]))
    (found,) = [b for b in blocks(message) if isinstance(b, types.PageBlockTable)]
    assert [[rows._cell(c) for c in r.cells] for r in found.rows] == rows.TABLE_ROWS


def test_what_the_user_reads_of_a_rich_message_has_no_markup_left():
    from acceptance import rows
    from acceptance.live import shown_text

    message = types.Message(id=1, peer_id=types.PeerUser(user_id=1), message="", rich_message=types.RichMessage(
        blocks=[types.PageBlockHeading1(text=_text("Heading one")),
                types.PageBlockParagraph(text=types.TextConcat(texts=[_text("Plain, "), types.TextBold(text=_text("bold"))])),
                types.PageBlockMath(source=rows.FORMULA)], photos=[], documents=[]))
    shown = shown_text(message)
    assert "Heading one" in shown and "Plain, bold" in shown and rows.FORMULA in shown
    assert not rows._LITERAL.search("Plain, bold") and rows._LITERAL.search("Plain, **bold**")


def test_the_report_names_each_failed_check_and_the_owners_no():
    import argparse

    from acceptance.__main__ import _report
    from acceptance.rows import Result

    ok = Result("2", "A table", checks=[("cells equal", True, "")], observed=[("looks right?", True)])
    bad = Result("11", "A choice", checks=[("one incoming choice", False, "['a', 'b']")], observed=[("notice?", False)])
    report = _report([ok, bad], 0.0, argparse.Namespace(bot="vtadevbot", provider="anthropic", model="m"))
    assert "| 2 | A table | pass |" in report and "| 11 | A choice | FAIL |" in report
    assert "- FAIL: one incoming choice (['a', 'b'])" in report and "- owner NO: notice?" in report
