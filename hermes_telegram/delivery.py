"""What a file send produced, in a module light enough for plugin registration (no SDK import).

``Receipt`` is one file's outcome; ``album_chunks`` cuts photos into legal albums; ``collecting_notices``
gathers the not-delivered notices of a tool call so it sends them as one message.
"""

import contextlib
import contextvars
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Optional

ALBUM_MAX = 10  # sendMediaGroup: 2-10 items
# raw_response key of a partly delivered send: the ids of every message that did arrive, in order.
DELIVERED_IDS = "hermes_telegram_delivered_ids"

# While set, notices are collected here instead of sent: a tool call sends them as one message.
NOTICES: contextvars.ContextVar[Optional[list[str]]] = contextvars.ContextVar("hermes_telegram_notices",
                                                                             default=None)


@dataclass(frozen=True)
class Receipt:
    """What became of one file: its message id when it arrived, otherwise why not."""

    file: str
    message_id: Optional[str] = None
    reason: str = ""

    @property
    def sent(self) -> bool:
        return not self.reason


def album_chunks(items: list[Any]) -> list[list[Any]]:
    """``items`` in order as albums of at most 10, none of one item when there are several: 11 is 9 and 2,
    12 is 10 and 2. A lone item stays alone, to go as a photo."""
    sizes = [ALBUM_MAX] * (len(items) // ALBUM_MAX) + ([len(items) % ALBUM_MAX] if len(items) % ALBUM_MAX else [])
    if len(sizes) > 1 and sizes[-1] == 1:
        sizes[-2:] = [ALBUM_MAX - 1, 2]
    chunks, start = [], 0
    for size in sizes:
        chunks.append(items[start:start + size])
        start += size
    return chunks


@contextlib.contextmanager
def collecting_notices() -> Iterator[list[str]]:
    """Collect the notices of the sends in this block instead of sending each."""
    collected: list[str] = []
    token = NOTICES.set(collected)
    try:
        yield collected
    finally:
        NOTICES.reset(token)


def combined_notice(texts: list[str]) -> str:
    if len(texts) == 1:
        return texts[0]
    return "Some files did not arrive:\n\n" + "\n".join(f"- {text}" for text in texts)
