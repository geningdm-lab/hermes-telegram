"""Slice 1 end to end: answers reach Telegram as rich messages, long ones cut in order (rows 1, 2, 16).

A real gateway child with this plugin, a fake model scripted per inbound, and the Bot API stand-in of
checks/e2e/standin.py. What a scenario asserts is what Telegram received: the method and the Markdown.
Whether a client draws it is live acceptance (docs/design.md, Acceptance), not this file.
"""

import re
import sys
from typing import Any

import pytest

from checks.e2e.rig import C, rig_fixtures

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, _rig_stream = rig_fixtures()


class _Answer:
    """One scripted answer: its chat, and the stand-in calls made since its inbound."""

    def __init__(self, rig: Any, tag: str, body: str) -> None:
        token, self.aid = f"r-{tag}", f"A-r-{tag}"
        self._rig = rig
        self._since = len(rig.drv.standin.calls)  # every scenario shares one private chat
        rig.director.script(token, C.answer(self.aid, body))
        inbound = rig.drv.dm(f"answer please [in:{token}]")
        self.chat_id = inbound.chat_id
        C.wait_reply(rig, self.chat_id, self.aid)

    def calls(self, method: str) -> list[Any]:
        """Successful calls of ``method`` in this answer's chat since its inbound."""
        return [c for c in self._rig.drv.standin.calls[self._since:] if c.method == method
                and str(c.params.get("chat_id")) == self.chat_id and not c.faulted]


def _markdown(call: Any) -> str:
    return str(call.params["rich_message"]["markdown"])


def _words(text: str) -> list[str]:
    return re.findall(r"w\d{5}", text)


def test_a_table_arrives_as_one_rich_message_with_every_cell(rig):
    # Row 2: a 4-column, 6-row table.
    rows = [f"| r{i}c1 | r{i}c2 | r{i}c3 | r{i}c4 |" for i in range(1, 7)]
    table = "\n".join(["| A | B | C | D |", "|---|---|---|---|", *rows])
    answer = _Answer(rig, "table", "Here it is:\n\n" + table + "\n")
    sent = answer.calls("sendRichMessage")
    assert len(sent) == 1, rig.ctx()
    assert table in _markdown(sent[0])  # the whole table, header and separator included, rows in order
    assert not answer.calls("sendMessage"), "a piece of the table answer went as a plain message"


def test_a_short_plain_answer_stays_one_ordinary_message(rig):
    # Row 16.
    answer = _Answer(rig, "plain", "Sure, that works for me.")
    assert len(answer.calls("sendMessage")) == 1, rig.ctx()
    assert not answer.calls("sendRichMessage")


def test_formulas_render_and_prices_stay_prices(rig):
    # Row 1 as amended: maths is LaTeX that Telegram renders; a literal dollar must not open a formula.
    answer = _Answer(rig, "math", "## Energy\n\n$$E = mc^2$$\n\nIt costs $5 or $10, and $x^2$ grows.\n")
    (sent,) = answer.calls("sendRichMessage")
    markdown = _markdown(sent)
    assert "$$E = mc^2$$" in markdown
    assert r"\$5" in markdown and r"\$10" in markdown
    assert "$x^2$" in markdown


def test_a_long_formatted_answer_arrives_whole_in_order_as_rich_messages(rig):
    # Row 1, longer than one Telegram message: here about 75,000 characters, over two rich messages' limit.
    sections = [f"## Part {i}\n\n" + " ".join(f"w{i * 1000 + j:05d}" for j in range(400)) + "\n\n- item\n- item\n"
                for i in range(1, 28)]
    body = "\n".join(sections)
    answer = _Answer(rig, "long", body)
    sent = answer.calls("sendRichMessage")
    assert len(sent) >= 3, rig.ctx()
    assert all(len(_markdown(c)) <= 32768 for c in sent)
    assert [w for c in sent for w in _words(_markdown(c))] == _words(body)
    assert not answer.calls("sendMessage"), "part of the long answer fell back to plain messages"


def test_a_part_telegram_refuses_is_sent_again_smaller_and_nothing_is_lost(rig):
    # Telegram counts blocks its own way: a part it refuses was not delivered, so it is cut and resent.
    sections = [f"## Section {i}\n\n" + " ".join(f"w{i * 1000 + j:05d}" for j in range(400)) + "\n"
                for i in range(30, 40)]  # about 28,000 characters: one part, refused whole
    body = "\n".join(sections)
    # Refused every time it is sent whole, so only a smaller retry can get it through.
    refused = rig.drv.standin.fail(
        "sendRichMessage", {"ok": False, "error_code": 400, "description": "Bad Request: too many blocks"},
        status=400, times=100, match=lambda p: "w30000" in str(p.get("rich_message"))
        and "w39399" in str(p.get("rich_message")))
    answer = _Answer(rig, "refused", body)
    assert refused.fired >= 1, "the injected refusal never fired"
    sent = answer.calls("sendRichMessage")
    assert len(sent) >= 2
    assert [w for c in sent for w in _words(_markdown(c))] == _words(body), rig.ctx()
    assert not answer.calls("sendMessage"), "the refused answer fell back to plain messages"
