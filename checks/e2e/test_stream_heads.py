"""A rich answer streaming past the overflow limit (hermes_telegram/answers.py): how Hermes' stream consumer
gets it cut, and how the heads it seals are sent.

The consumer calls the adapter's ``truncate_message`` with the rich limit and its own length units
(``stream_consumer._split_first_send``), keeps the last part as its live text, and sends the others; these
tests call it the same way, on the composed adapter with a real PTB ``Bot`` against the stand-in.
Regressions from the Codex review of slice 3 (docs/review-log.md).
"""

import pytest

from checks.e2e.wire import CHAT, sender_fixture  # noqa: F401  (the sender fixture)
from gateway.platforms.base import utf16_len
from hermes_telegram import answers

pytestmark = pytest.mark.e2e

LIMIT = 32768 - 100  # the consumer's _safe_limit with the rich overflow limit


def _cut(adapter, text: str) -> list[str]:
    return adapter.truncate_message(text, LIMIT, len_fn=utf16_len)


def _prose(words: int) -> str:
    return " ".join(f"w{i:05d}" for i in range(words))


def test_the_live_tail_stays_as_the_agent_wrote_it(sender):
    # The consumer appends the next tokens to the last part: "$x" then "^2$" must still be a formula.
    text = "## Results\n\n" + "\n\n".join(_prose(400) for _ in range(12)) + "\n\nthe equation $x"
    parts = _cut(sender.adapter, text)
    assert len(parts) >= 2 and parts[-1].endswith("the equation $x")
    assert "".join(parts) == text  # nothing changed, nothing lost


async def test_a_sealed_head_without_structure_of_its_own_goes_as_one_rich_message(sender):
    # ~32,000 characters of prose first: the head alone looks like plain text, but it is part of a rich answer.
    text = _prose(4600) + "\n\n## Then a heading\n\nmore"
    head, *_ = _cut(sender.adapter, text)
    assert "##" not in head
    result = await sender.adapter.send(CHAT, head, metadata={"expect_edits": True})
    assert result.success
    assert len(sender.standin.calls_of("sendRichMessage")) == 1 and not sender.standin.calls_of("sendMessage")


def test_the_cut_counts_in_the_consumers_units(sender):
    # 17,011 characters are 34,011 UTF-16 units: over the limit for the consumer, so it must get parts.
    text = "## Emoji\n\n" + "😀" * 17000
    assert utf16_len(text) > LIMIT
    parts = _cut(sender.adapter, text)
    assert len(parts) >= 2 and all(sender.adapter._is_sealed_head(p) for p in parts[:-1])  # cut as rich parts


async def test_a_head_record_is_used_once(sender):
    # A head sent on the last tick goes without expect_edits; a later message with the same text that the
    # stream will edit must keep its expect_edits.
    head, *_ = _cut(sender.adapter, _prose(4600) + "\n\n## Then\n\nmore")
    await sender.adapter.send(CHAT, head)
    await sender.adapter.send(CHAT, head, metadata={"expect_edits": True})
    assert len(sender.standin.calls_of("sendRichMessage")) == 1  # the second went the editable way


async def test_a_head_record_lapses(sender, monkeypatch):
    monkeypatch.setattr(answers, "_SEALED_TTL", -1.0)
    head, *_ = _cut(sender.adapter, _prose(4600) + "\n\n## Then\n\nmore")
    await sender.adapter.send(CHAT, head, metadata={"expect_edits": True})
    assert not sender.standin.calls_of("sendRichMessage")
