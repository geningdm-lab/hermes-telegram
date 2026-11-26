"""The guards every telegram tool passes through (hermes_telegram/turn.py, docs/design.md "Tools").

Session context is set the way the gateway sets it (gateway.session_context.set_session_vars); events are
built with the composed adapter's own ``build_source`` and passed to its processing hooks, as the gateway's
``_process_message_background`` does.
"""

from collections.abc import Iterator

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, ProcessingOutcome
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_telegram import tools
from hermes_telegram.delivery import album_chunks
from hermes_telegram.adapter import HermesTelegramAdapter
from hermes_telegram.turn import Turn, TurnRefused


@pytest.fixture
def session() -> Iterator[None]:
    tokens = set_session_vars(platform="telegram", chat_id="111", chat_type="dm", user_id="42",
                              session_key="agent:main:telegram:dm:111", message_id="9", profile="default")
    yield
    clear_session_vars(tokens)


@pytest.fixture
def adapter() -> HermesTelegramAdapter:
    return HermesTelegramAdapter(PlatformConfig(enabled=True, token="1:x"))


def _event(adapter: HermesTelegramAdapter, *, message_id: str | None = "9", internal: bool = False) -> MessageEvent:
    source = adapter.build_source(chat_id="111", chat_type="dm", user_id="42", message_id=message_id)
    return MessageEvent(text="hi", source=source, message_id=message_id, internal=internal)


def test_a_telegram_turn_gives_its_own_chat_and_topic(session):
    turn = Turn.current()
    assert (turn.chat_id, turn.thread_id, turn.inbound_message_id) == ("111", None, "9")


@pytest.mark.parametrize("override", [
    {"platform": "discord"},  # another surface
    {"platform": ""},  # no session: CLI or a background job
    {"chat_id": ""},  # no chat to act in
    {"message_id": ""},  # a synthetic turn (loop or goal wakeup): its source carries no message id
])
def test_tools_refuse_outside_a_turn_a_telegram_message_started(override: dict[str, str]):
    values = {"platform": "telegram", "chat_id": "111", "session_key": "k", "message_id": "9", **override}
    tokens = set_session_vars(**values)
    try:
        with pytest.raises(TurnRefused):
            Turn.current()
    finally:
        clear_session_vars(tokens)


def test_tools_refuse_in_a_cron_run(session):
    tokens = set_session_vars(platform="telegram", chat_id="111", session_key="k", message_id="9",
                              cron_session="job-1")
    try:
        with pytest.raises(TurnRefused, match="cron"):
            Turn.current()
    finally:
        clear_session_vars(tokens)


async def test_a_tool_acts_only_while_the_users_own_event_is_processed(adapter, session):
    turn = Turn.current()
    with pytest.raises(TurnRefused, match="user's own"):
        adapter.admitted_turn(turn)  # nothing is being processed
    await adapter.on_processing_start(_event(adapter, internal=True))  # an internal event of that chat
    with pytest.raises(TurnRefused, match="user's own"):
        adapter.admitted_turn(turn)
    event = _event(adapter)
    await adapter.on_processing_start(event)
    assert adapter.admitted_turn(turn).chat_id == "111"
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    with pytest.raises(TurnRefused, match="user's own"):
        adapter.admitted_turn(turn)  # a tool still running after the turn ended


async def test_a_turn_sends_at_most_ten_extra_messages_and_the_next_turn_starts_fresh(adapter, session):
    await adapter.on_processing_start(_event(adapter))
    admitted = adapter.admitted_turn(Turn.current())
    admitted.take(7)
    with pytest.raises(TurnRefused, match="at most 10"):
        admitted.take(4)  # refused whole: nothing of it is reserved
    admitted.take(3)
    assert admitted.try_take() is False
    await adapter.on_processing_complete(_event(adapter), ProcessingOutcome.SUCCESS)
    await adapter.on_processing_start(_event(adapter, message_id="10"))
    tokens = set_session_vars(platform="telegram", chat_id="111", session_key="k", message_id="10")
    try:
        adapter.admitted_turn(Turn.current()).take(10)
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize(("paths", "as_files", "expected"), [
    ([f"/p/{i}.png" for i in range(12)], False, 2),  # albums of 10 and 2
    ([f"/p/{i}.png" for i in range(11)], False, 2),  # 9 and 2: an album holds at least two
    (["/p/a.png", "/p/b.mp4", "/p/c.pdf"], False, 3),
    (["/p/a.png", "/p/b.pdf", "/p/c.png"], False, 3),  # photos apart do not share an album
    (["/p/a.png", "/p/b.png"], True, 2),  # each file its own document
])
def test_messages_needed_counts_each_album_as_one_message(paths: list[str], as_files: bool, expected: int):
    assert tools._messages_needed(paths, as_files) == expected


@pytest.mark.parametrize("args", [{}, {"files": []}, {"files": [""]}, {"files": "a.png"},
                                  {"files": [f"/f{i}" for i in range(21)]}, {"files": ["/a"], "caption": 5}])
def test_send_arguments_are_checked_before_anything_is_sent(args: dict):
    with pytest.raises(TurnRefused):
        tools._parse_send(args)


@pytest.mark.parametrize(("count", "sizes"), [(1, [1]), (2, [2]), (10, [10]), (11, [9, 2]), (12, [10, 2]),
                                              (21, [10, 9, 2])])
def test_photos_are_cut_into_albums_telegram_accepts(count: int, sizes: list[int]):
    # sendMediaGroup takes 2-10 items: a remainder of one is rebalanced; a lone photo stays a photo.
    assert [len(chunk) for chunk in album_chunks(list(range(count)))] == sizes
