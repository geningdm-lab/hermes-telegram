"""The rich answer sender's failure handling (hermes_telegram/answers.py), on the real composed adapter.

Telegram is a scripted bot object: its methods answer or raise as the Bot API would, and record what was
sent. It decides nothing else, so each test checks the adapter's behaviour, not the double's.
Regressions from the Codex review of slice 1 (docs/review-log.md).
"""

import re
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from telegram.error import BadRequest, EndPointNotFound, NetworkError, RetryAfter

from gateway.config import PlatformConfig
from hermes_telegram.adapter import HermesTelegramAdapter

CHAT = "111"


class ScriptedBot:
    """``rich(payload)`` decides each sendRichMessage: return nothing to accept it, or raise."""

    def __init__(self, rich: Callable[[dict[str, Any]], None] = lambda _p: None) -> None:
        self.rich_decide = rich
        self.rich_sent: list[str] = []
        self.rich_calls = 0
        self.plain_sent: list[str] = []
        self._ids = iter(range(1000, 100000))

    async def do_api_request(self, method: str, api_kwargs: dict[str, Any] | None = None, **_: Any) -> Any:
        assert method == "sendRichMessage", method
        self.rich_calls += 1
        self.rich_decide(api_kwargs or {})
        self.rich_sent.append(api_kwargs["rich_message"]["markdown"])
        return {"message_id": next(self._ids)}

    async def send_message(self, text: str, **_: Any) -> Any:
        self.plain_sent.append(text)
        return SimpleNamespace(message_id=next(self._ids))

    async def send_chat_action(self, **_: Any) -> bool:
        return True


@pytest.fixture
def adapter() -> HermesTelegramAdapter:
    adapter = HermesTelegramAdapter(PlatformConfig(enabled=True, token="1:x"))
    adapter._telegram_chat_outbound_slot_secs = 0  # no 1 s pacing between parts in tests
    return adapter


def _words(text: str) -> list[str]:
    return re.findall(r"w\d{5}", text)


def _answer(sections: int, words: int = 300) -> str:
    return "\n\n".join(f"## Part {i}\n\n" + " ".join(f"w{i * 1000 + j:05d}" for j in range(words))
                       for i in range(sections)) + "\n"


async def test_a_refused_one_part_answer_is_resent_smaller(adapter):
    # Telegram counts blocks its own way: an answer that fits one part may still be refused as too big.
    body = _answer(3)

    def refuse_whole(payload: dict[str, Any]) -> None:
        markdown = payload["rich_message"]["markdown"]
        if "w00000" in markdown and "w02299" in markdown:
            raise BadRequest("Bad Request: too many blocks")
    adapter._bot = ScriptedBot(refuse_whole)
    result = await adapter.send(CHAT, body)
    assert result.success
    assert len(adapter._bot.rich_sent) >= 2
    assert [w for m in adapter._bot.rich_sent for w in _words(m)] == _words(body)
    assert adapter._bot.plain_sent == []


async def test_once_rich_messages_are_gone_the_rest_goes_as_markdownv2_without_more_rich_calls(adapter):
    def gone(_payload: dict[str, Any]) -> None:
        raise EndPointNotFound("Not Found: method not found")
    adapter._bot = ScriptedBot(gone)
    body = _answer(40)  # several parts
    result = await adapter.send(CHAT, body)
    assert result.success
    assert adapter._bot.rich_calls == 1
    assert [w for t in adapter._bot.plain_sent for w in _words(t)] == _words(body)


async def test_a_flood_refusal_mid_answer_arms_the_chat_cooldown_and_keeps_the_tail(adapter):
    calls = {"n": 0}

    def flood_on_second(_payload: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RetryAfter(30)
    adapter._bot = ScriptedBot(flood_on_second)
    result = await adapter.send(CHAT, _answer(40))
    assert not result.success
    assert result.raw_response["partial_overflow"] and result.raw_response["undelivered_rich_items"]
    assert adapter._send_flood_cooldown_remaining(CHAT) is not None
    before = adapter._bot.rich_calls
    again = await adapter.send(CHAT, "## another\n\nanswer\n")
    assert not again.success and adapter._bot.rich_calls == before  # refused locally, nothing sent


@pytest.mark.parametrize(("error", "resumable"), [
    (NetworkError("httpx.ConnectError: [Errno 61] Connection refused"), True),  # never reached Telegram
    (NetworkError("httpx.ReadError: connection closed"), False),  # may have been shown
])
async def test_only_a_certainly_unsent_part_is_kept_for_resuming(adapter, error: Exception, resumable: bool):
    calls = {"n": 0}

    def fail_second(_payload: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise error
    adapter._bot = ScriptedBot(fail_second)
    result = await adapter.send(CHAT, _answer(40))
    assert result.raw_response["partial_overflow"]
    assert bool(result.raw_response.get("undelivered_rich_items")) is resumable


async def test_markdownv2_fallback_chunks_escape_their_part_counters(adapter):
    def always_refuse(_payload: dict[str, Any]) -> None:
        raise BadRequest("Bad Request: can't parse rich message")
    adapter._bot = ScriptedBot(always_refuse)
    result = await adapter.send(CHAT, _answer(2, words=1500))
    assert result.success
    counters = [t for t in adapter._bot.plain_sent if re.search(r"\d+/\d+\\?\)\s*$", t)]  # either form
    assert counters and all(re.search(r"\\\(\d+/\d+\\\)\s*$", t) for t in counters)


def test_the_ordinary_path_shows_an_escaped_dollar_as_a_dollar(adapter):
    # The hint asks the agent to write \$; MarkdownV2 would otherwise show the backslash.
    assert adapter.format_message(r"It costs \$5.") == r"It costs $5\."
