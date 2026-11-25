"""The rich answer sender's failure handling (hermes_telegram/answers.py) at the real Telegram boundary.

A real python-telegram-bot ``Bot`` talks HTTP to the Bot API stand-in (checks/e2e/standin.py) in this
process, so every failure is the one PTB raises: a Bot API refusal comes from the stand-in (``fail``), and
a connection failure from httpx itself, through the transport below. What a test asserts is what the chat
shows. No gateway child: these tests need Hermes' stand-in from the pinned source, hence the e2e mark.
Regressions from the Codex review of slice 1 (docs/review-log.md).
"""

import itertools
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import httpx
import pytest

import checks.e2e.rig  # noqa: F401  (puts the pinned Hermes source, and with it the stand-in, on sys.path)
from checks.e2e.standin import RichTelegramStandin
from gateway.config import PlatformConfig
from hermes_telegram.adapter import HermesTelegramAdapter

pytestmark = pytest.mark.e2e

CHAT = "111"  # a private chat
GROUP = "-100123"  # a forum supergroup
TOPIC = "77"


class Wire(httpx.AsyncBaseTransport):
    """httpx's own transport, with a connection failure on chosen calls of a Bot API method.

    ``refused``: the connection never opens (httpx.ConnectError). ``pool``: no pooled connection is free
    (httpx.PoolTimeout). ``lost``: the request reaches Telegram, then the connection drops before the reply
    (httpx.ReadError), so the message is on screen and the sender cannot know it."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self._calls: dict[str, int] = {}
        self._faults: dict[tuple[str, int], str] = {}

    def fail(self, method: str, call: int, fault: str) -> None:
        """Fail the ``call``-th request (from 1) of ``method`` with ``fault``."""
        self._faults[(method, call)] = fault

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self._calls[method] = self._calls.get(method, 0) + 1
        fault = self._faults.get((method, self._calls[method]))
        if fault == "refused":
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if fault == "pool":
            raise httpx.PoolTimeout("no free connection", request=request)
        response = await self._inner.handle_async_request(request)
        if fault == "lost":
            await response.aread()
            raise httpx.ReadError("connection closed", request=request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Sender:
    standin: RichTelegramStandin
    wire: Wire
    adapter: HermesTelegramAdapter

    def shown(self, chat: str) -> list[Any]:
        return self.standin.visible(chat)

    def words(self, chat: str) -> list[str]:
        return [w for v in self.shown(chat) for w in _words(v.text)]

    def refuse(self, method: str, description: str, *, status: int = 400, times: int = 10_000,
               match: Callable[[dict[str, Any]], bool] | None = None, **parameters: Any) -> None:
        body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
        if parameters:
            body["parameters"] = parameters
        self.standin.fail(method, body, status=status, times=times, match=match)


@pytest.fixture
async def sender() -> AsyncIterator[Sender]:
    from telegram import Bot
    from telegram.request import HTTPXRequest

    standin = RichTelegramStandin().start()
    wire = Wire()
    bot = Bot(standin.token, base_url=standin.api_base, request=HTTPXRequest(httpx_kwargs={"transport": wire}))
    await bot.initialize()
    adapter = HermesTelegramAdapter(PlatformConfig(enabled=True, token=standin.token))
    adapter._bot = bot
    adapter._telegram_chat_outbound_slot_secs = 0  # no 1 s pacing between parts in tests
    try:
        yield Sender(standin, wire, adapter)
    finally:
        await bot.shutdown()
        standin.stop()


def _words(text: str) -> list[str]:
    return re.findall(r"w\d{5}", text)


def _answer(sections: int, words: int = 300) -> str:
    """Headed sections of numbered words: rich, and cut into parts in order when long."""
    return "\n\n".join(f"## Part {i}\n\n" + " ".join(f"w{i * 1000 + j:05d}" for j in range(words))
                       for i in range(sections)) + "\n"


def _nth(n: int) -> Callable[[dict[str, Any]], bool]:
    """A fault match that takes the ``n``-th call of its method (from 1)."""
    seen = itertools.count(1)
    return lambda _p: next(seen) == n


def _mdv2_shown(text: str) -> str:
    """What a client shows for MarkdownV2 text, as far as escapes go."""
    return re.sub(r"\\(.)", r"\1", text)


LONG = _answer(40)  # three rich parts


@pytest.mark.parametrize("fault", ["refused", "pool", "lost"])
async def test_a_one_part_answer_is_shown_exactly_once_whatever_the_connection_failure(sender, fault):
    # refused and pool never reached Telegram, so the base retry sends the answer again; lost did, so
    # nothing may send it again (before the fix: pool was given up, lost was shown twice).
    body = _answer(1)
    sender.wire.fail("sendRichMessage", 1, fault)
    await sender.adapter._send_with_retry(CHAT, body, base_delay=0)
    assert sender.words(CHAT) == _words(body)


@pytest.mark.parametrize(("fault", "completes"), [("refused", True), ("pool", True), ("lost", False)])
async def test_a_failed_middle_part_is_resumed_only_when_it_certainly_was_not_shown(sender, fault, completes):
    sender.wire.fail("sendRichMessage", 2, fault)
    result = await sender.adapter._send_with_retry(CHAT, LONG, base_delay=0)
    shown = sender.words(CHAT)
    if completes:
        assert result.success and shown == _words(LONG)
    else:
        # The head as far as the lost part, once each, and nothing after it: a gap, never a duplicate.
        assert not result.success
        assert 0 < len(shown) < len(_words(LONG)) and shown == _words(LONG)[:len(shown)]


async def test_a_flood_wait_given_as_a_timedelta_is_waited_out_and_the_answer_completes(sender, monkeypatch):
    # PTB_TIMEDELTA makes RetryAfter.retry_after a timedelta; the flood cooldown counts seconds.
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    sender.refuse("sendRichMessage", "Too Many Requests: retry after 1", status=429, times=1, match=_nth(2),
                  retry_after=1)
    result = await sender.adapter._send_with_retry(CHAT, LONG, base_delay=0)
    assert result.success and sender.words(CHAT) == _words(LONG)


async def test_a_flood_refusal_mid_answer_holds_the_chat_without_more_requests(sender):
    sender.refuse("sendRichMessage", "Too Many Requests: retry after 30", status=429, times=1, match=_nth(2),
                  retry_after=30)
    first = await sender.adapter.send(CHAT, LONG)
    assert not first.success
    before = len(sender.standin.calls_of("sendRichMessage", "sendMessage"))
    again = await sender.adapter.send(CHAT, "## another\n\nanswer\n")
    assert not again.success
    assert len(sender.standin.calls_of("sendRichMessage", "sendMessage")) == before  # refused locally


async def test_a_one_part_answer_telegram_refuses_as_too_big_is_resent_as_smaller_rich_parts(sender):
    # Telegram counts blocks its own way: an answer within our estimate may still be refused as too big.
    body = _answer(3)
    sender.refuse("sendRichMessage", "Bad Request: too many blocks",
                  match=lambda p: "w00000" in p["rich_message"]["markdown"] and "w02299" in p["rich_message"]["markdown"])
    result = await sender.adapter.send(CHAT, body)
    assert result.success
    assert len(sender.shown(CHAT)) >= 2 and {v.extra["kind"] for v in sender.shown(CHAT)} == {"rich"}
    assert sender.words(CHAT) == _words(body)


async def test_once_rich_messages_are_gone_the_rest_goes_as_markdownv2_without_more_rich_calls(sender):
    sender.refuse("sendRichMessage", "Not Found", status=404)
    result = await sender.adapter.send(CHAT, LONG)
    assert result.success
    assert len(sender.standin.calls_of("sendRichMessage")) == 1
    assert {v.extra["kind"] for v in sender.shown(CHAT)} == {"text"}
    assert sender.words(CHAT) == _words(LONG)


async def test_markdownv2_fallback_chunks_are_valid_markdownv2_with_their_part_counters(sender):
    sender.refuse("sendRichMessage", "Bad Request: can't parse rich message")
    result = await sender.adapter.send(CHAT, _answer(2, words=1500))
    assert result.success
    sent = sender.standin.calls_of("sendMessage")
    assert sent and all(c.params.get("parse_mode") == "MarkdownV2" and not c.faulted for c in sent)
    assert any(re.search(r"\\\(\d+/\d+\\\)\s*$", c.params["text"]) for c in sent)


async def test_a_part_sent_as_markdownv2_after_all_shows_the_agents_own_text(sender):
    # Preparation escapes a stray $ and a bare <; the MarkdownV2 path must not show those backslashes.
    sender.refuse("sendRichMessage", "Bad Request: can't parse rich message")
    result = await sender.adapter.send(CHAT, "## Prices\n\nIf a < b, it costs $5.\n")
    assert result.success
    assert "If a < b, it costs $5." in _mdv2_shown(sender.shown(CHAT)[-1].text)


async def test_after_leaving_a_deleted_forum_topic_the_rest_of_the_answer_still_goes_as_rich(sender):
    sender.standin.chats[GROUP] = {"id": int(GROUP), "type": "supergroup", "title": "Team", "is_forum": True}
    in_topic = lambda p: str(p.get("message_thread_id") or "") == TOPIC  # noqa: E731
    sender.refuse("sendRichMessage", "Bad Request: message thread not found", match=in_topic)
    sender.refuse("sendMessage", "Bad Request: message thread not found", match=in_topic)
    result = await sender.adapter.send(GROUP, LONG, metadata={"thread_id": TOPIC})
    assert result.success
    assert sender.words(GROUP) == _words(LONG)
    # The first part could only go as MarkdownV2, which left the topic; the parts after it are rich.
    assert sender.shown(GROUP)[-1].extra["kind"] == "rich"


async def test_a_wide_table_shown_as_code_inside_a_longer_fence_is_not_refused(sender):
    # The stand-in's column limit is for tables; a ``` line inside a ```` fence does not close the fence.
    table = "| " + " | ".join(f"c{i}" for i in range(25)) + " |\n|" + "---|" * 25 + "\n"
    body = "## Example\n\n````markdown\n```\n" + table + "```\n````\n"
    result = await sender.adapter.send(CHAT, body)
    assert result.success and [v.extra["kind"] for v in sender.shown(CHAT)] == ["rich"]



# Regressions from the Codex review of slice 2 (docs/review-log.md).


async def test_a_markdownv2_fallback_chunk_whose_reply_was_lost_is_not_sent_again(sender):
    # The harvested chunk sender retries a dropped connection; inside a rich answer that shows it twice.
    sender.refuse("sendRichMessage", "Bad Request: can't parse rich message")
    sender.wire.fail("sendMessage", 1, "lost")
    result = await sender.adapter._send_with_retry(CHAT, _answer(2, words=1500), base_delay=0)
    assert not result.success
    assert len(sender.standin.calls_of("sendMessage")) == 1  # the lost chunk once, nothing after it


async def test_a_timedelta_flood_wait_on_a_fallback_chunk_is_waited_out(sender, monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    sender.refuse("sendRichMessage", "Bad Request: can't parse rich message")
    sender.refuse("sendMessage", "Too Many Requests: retry after 1", status=429, times=1, retry_after=1)
    body = _answer(2, words=1500)
    result = await sender.adapter._send_with_retry(CHAT, body, base_delay=0)
    assert result.success and sender.words(CHAT) == _words(body)
