"""The agent's action buttons: the token store and the tool's arguments (hermes_telegram/buttons.py, tools.py).

The store is the real one on a temporary file; what a test asserts is what a later tap would get.
"""

import pytest

from hermes_telegram import tools
from hermes_telegram.buttons import ButtonStore
from hermes_telegram.turn import TurnRefused


@pytest.fixture
def store(tmp_path) -> ButtonStore:
    return ButtonStore(tmp_path / "buttons.json")


def test_a_single_use_set_takes_its_first_tap_only(store):
    # Row 11: one tap reaches the agent; a second tap of the same set does not.
    paris, rome, _ = store.new_set("111", [("Paris", "paris"), ("Rome", "rome"), ("Oslo", "oslo")],
                                   single_use=True)
    button, first = store.claim(paris, "111")
    assert (button["label"], button["value"], first) == ("Paris", "paris", True)
    assert store.claim(paris, "111")[1] is False  # the same button again
    assert store.claim(rome, "111")[1] is False  # another button of the set


def test_a_set_that_is_not_single_use_takes_every_tap(store):
    again, = store.new_set("111", [("Again", "again")], single_use=False)
    assert store.claim(again, "111")[1] is True
    assert store.claim(again, "111")[1] is True


def test_taps_resolve_after_a_restart_and_an_answered_set_stays_answered(tmp_path):
    path = tmp_path / "buttons.json"
    paris, rome = ButtonStore(path).new_set("111", [("Paris", "paris"), ("Rome", "rome")], single_use=True)
    assert ButtonStore(path).claim(paris, "111")[1] is True
    assert ButtonStore(path).claim(rome, "111")[1] is False


@pytest.mark.parametrize(("token", "chat"), [("unknown-token", "111"), (None, "222")])
def test_a_token_from_nowhere_or_another_chat_is_not_known(store, token, chat):
    known, = store.new_set("111", [("Paris", "paris")], single_use=True)
    assert store.claim(token or known, chat) == (None, False)


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
