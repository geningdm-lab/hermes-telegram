"""The guards every telegram tool passes through (hermes_telegram/turn.py, docs/design.md "Tools").

Session context is set the way the gateway sets it (gateway.session_context.set_session_vars).
"""

from collections.abc import Iterator

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_telegram import tools
from hermes_telegram.turn import SendBudget, Turn, TurnRefused


@pytest.fixture
def session() -> Iterator[None]:
    tokens = set_session_vars(platform="telegram", chat_id="111", chat_type="dm", user_id="42",
                              session_key="agent:main:telegram:dm:111", message_id="9", profile="default")
    yield
    clear_session_vars(tokens)


def test_a_telegram_turn_gives_its_own_chat_and_topic(session):
    turn = Turn.current()
    assert (turn.chat_id, turn.thread_id, turn.inbound_message_id) == ("111", None, "9")


@pytest.mark.parametrize("override", [
    {"platform": "discord"},  # another surface
    {"platform": ""},  # no session: CLI or a background job
    {"chat_id": ""},  # no chat to act in
])
def test_tools_refuse_outside_a_telegram_chat_turn(override: dict[str, str]):
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


def test_a_turn_sends_at_most_ten_extra_messages_and_another_turn_starts_fresh(session):
    budget = SendBudget()
    turn = Turn.current()
    budget.take(turn, 7)
    with pytest.raises(TurnRefused, match="at most 10"):
        budget.take(turn, 4)  # refused whole: nothing of it is reserved
    budget.take(turn, 3)
    next_turn = Turn(turn.session_key, turn.chat_id, None, "10", turn.profile)
    budget.take(next_turn, 10)


@pytest.mark.parametrize(("paths", "as_files", "expected"), [
    ([f"/p/{i}.png" for i in range(12)], False, 2),  # albums of 10 and 2
    (["/p/a.png", "/p/b.mp4", "/p/c.pdf"], False, 3),
    (["/p/a.png", "/p/b.png"], True, 2),  # each file its own document
])
def test_messages_needed_counts_albums_as_one_message(paths: list[str], as_files: bool, expected: int):
    assert tools._messages_needed(paths, as_files) == expected


@pytest.mark.parametrize("args", [{}, {"files": []}, {"files": [""]}, {"files": "a.png"},
                                  {"files": [f"/f{i}" for i in range(21)]}, {"files": ["/a"], "caption": 5}])
def test_send_arguments_are_checked_before_anything_is_sent(args: dict):
    with pytest.raises(TurnRefused):
        tools._parse_send(args)
