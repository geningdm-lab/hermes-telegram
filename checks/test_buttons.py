"""The agent's action buttons: the token store and the tool's arguments (hermes_telegram/buttons.py, tools.py).

The store is the real one on a temporary file; what a test asserts is what a later tap would get.
"""

import pytest

from hermes_telegram import tools
from hermes_telegram.actions import poll_event_text
from hermes_telegram.buttons import ButtonStore, StoreError, event_text
from hermes_telegram.turn import TurnRefused


@pytest.fixture
def store(tmp_path) -> ButtonStore:
    return ButtonStore(tmp_path / "buttons.json")


def _set(store: ButtonStore, buttons: list[tuple[str, str]], *, single_use: bool = True) -> list[str]:
    """Tokens of a set shown on message 5 of chat 111, as the tool binds it once sent."""
    set_id, tokens = store.new_set("111", buttons, single_use=single_use)
    store.bind(set_id, 5, None)
    return tokens


def _claim(store: ButtonStore, token: str) -> tuple:
    return store.claim(token, "111", 5, None)


def test_a_single_use_set_takes_its_first_tap_only(store):
    # Row 11: one tap reaches the agent; a second tap of the same set does not.
    paris, rome, _ = _set(store, [("Paris", "paris"), ("Rome", "rome"), ("Oslo", "oslo")])
    button, first = _claim(store, paris)
    assert (button["label"], button["value"], first) == ("Paris", "paris", True)
    assert _claim(store, paris)[1] is False  # the same button again
    assert _claim(store, rome)[1] is False  # another button of the set


def test_a_set_that_is_not_single_use_takes_every_tap(store):
    again, = _set(store, [("Again", "again")], single_use=False)
    assert _claim(store, again)[1] is True
    assert _claim(store, again)[1] is True


def test_taps_resolve_after_a_restart_and_an_answered_set_stays_answered(tmp_path):
    path = tmp_path / "buttons.json"
    paris, rome = _set(ButtonStore(path), [("Paris", "paris"), ("Rome", "rome")])
    assert _claim(ButtonStore(path), paris)[1] is True
    assert _claim(ButtonStore(path), rome)[1] is False


def test_a_released_claim_lets_the_next_tap_be_first(store):
    paris, rome = _set(store, [("Paris", "paris"), ("Rome", "rome")])
    _claim(store, paris)
    store.release(paris)
    assert _claim(store, rome)[1] is True


def test_a_store_that_cannot_save_publishes_and_takes_nothing(store, monkeypatch):
    # Review slice 4, P2: a choice recorded only in memory would reach the agent again after a restart.
    import utils

    paris, rome = _set(store, [("Paris", "paris"), ("Rome", "rome")])

    def full_disk(*_args, **_kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(utils, "atomic_json_write", full_disk)
    with pytest.raises(StoreError):
        store.new_set("111", [("Yes", "yes")], single_use=True)
    with pytest.raises(StoreError):
        _claim(store, paris)
    monkeypatch.undo()
    assert len(store._tokens) == 2  # the set that was refused left no tokens behind
    assert _claim(store, rome)[1] is True  # the claim that could not be saved was not taken


@pytest.mark.parametrize(("build", "one", "two"), [
    (lambda labels: event_text(labels[0], "v", 5), ['Paris" value="x'], ["Paris"]),
    (lambda labels: poll_event_text("Capital?", labels), ['Paris", "Rome'], ["Paris", "Rome"]),
])
def test_a_quote_inside_a_field_cannot_read_as_another_field(build, one, two):
    # Review slice 4, P2: one option holding a quote must not read as two options.
    assert build(one) != build(two)
    assert build(["Paris"]).count('"Paris"') == 1  # the plain form the hint teaches is unchanged


@pytest.mark.parametrize(("buttons", "error"), [
    ([{"label": "A", "value": "a", "url": "https://x"}], "one of value, url or copy"),
    ([{"label": "Site", "url": "javascript:alert(1)"}], "must start with"),
    ([{"value": "a"}], "needs a label"),
    ([{"label": str(i)} for i in range(13)], "at most 12"),
])
def test_buttons_are_checked_before_anything_is_sent(buttons, error):
    with pytest.raises(TurnRefused, match=error):
        tools._parse_message({"text": "Pick one", "buttons": buttons})


def test_buttons_need_a_text_to_go_under():
    with pytest.raises(TurnRefused, match="need a text"):
        tools._parse_message({"buttons": [{"label": "A"}]})


def test_a_button_without_a_target_is_a_choice_of_its_label():
    assert tools._parse_message({"text": "Pick", "buttons": [{"label": "Paris"}]}) == (
        "Pick", [{"label": "Paris", "value": "Paris"}], True)
