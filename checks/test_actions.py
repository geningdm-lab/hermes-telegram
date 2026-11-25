"""Polls, reactions and stickers: the tool's arguments, the sticker search and the stores
(hermes_telegram/actions.py, tools.py).
"""

import pytest

from hermes_telegram import tools
from hermes_telegram.actions import _JsonStore, poll_event_text, reaction_event_text, sticker_matches
from hermes_telegram.turn import TurnRefused

CATALOG = [
    {"sticker_id": "a", "emoji": "👍", "description": "a cat giving a thumbs up", "set": "cats"},
    {"sticker_id": "b", "emoji": "😂", "description": "a dog laughing", "set": "dogs"},
    {"sticker_id": "c", "emoji": "", "description": "", "set": "party_time", "set_title": "Party time"},
]


@pytest.mark.parametrize(("query", "expected"), [
    ("👍", ["a"]),  # the emoji itself
    ("laughing dog", ["b"]),  # words of the description
    ("party", ["c"]),  # the set's name and title
    ("submarine", []),
])
def test_a_sticker_is_found_by_its_emoji_words_or_set(query, expected):
    assert [s["sticker_id"] for s in sticker_matches(CATALOG, query)] == expected


@pytest.mark.parametrize(("args", "error"), [
    ({"question": "", "options": ["a", "b"]}, "question"),
    ({"question": "Q?", "options": ["only one"]}, "2 to 12"),
    ({"question": "Q?", "options": ["a", "b"], "quiz_answer": 5}, "index"),
    ({"question": "Q?", "options": ["a", "b"], "quiz_answer": 0, "multiple": True}, "one right answer"),
])
def test_poll_arguments_are_checked_before_anything_is_sent(args, error):
    with pytest.raises(TurnRefused, match=error):
        tools._parse_poll(args)


def test_events_read_as_the_design_documents_them():
    assert poll_event_text("Capital?", ["Paris", "Rome"]) == '[poll] "Capital?": "Paris", "Rome"'
    assert reaction_event_text("🔥", 42) == "[reaction] 🔥 on message 42"


def test_a_store_keeps_its_newest_entries_and_survives_a_restart(tmp_path):
    path = tmp_path / "store.json"
    store = _JsonStore(path, limit=2)
    for key in ("one", "two", "three"):
        store.put(key, {"k": key})
    assert list(_JsonStore(path, limit=2).data) == ["two", "three"]
