"""File sends at the real Telegram boundary: albums, receipts, notices and the telegram_send plan.

The composed adapter with a real python-telegram-bot ``Bot`` against the stand-in (checks/e2e/wire.py).
A tool call runs ``tools._deliver`` for a turn the adapter admitted through its own processing hook, as the
gateway does. What a test asserts is what Telegram received and what the agent is told.
Regressions from the Codex review of slice 2 (docs/review-log.md).
"""

import asyncio
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from checks.e2e.wire import CHAT, sender_fixture  # noqa: F401  (the sender fixture)
from gateway.platforms.base import MessageEvent
from hermes_telegram import tools
from hermes_telegram.delivery import Receipt
from hermes_telegram.files import GONE, LOST_REPLY
from hermes_telegram.turn import Turn, TurnRefused, as_tool_send

pytestmark = pytest.mark.e2e


def _png(path: Path, side: int = 32) -> Path:
    out = io.BytesIO()
    Image.new("RGB", (side, side), (side % 255, 90, 40)).save(out, format="PNG")
    path.write_bytes(out.getvalue())
    return path


def _doc(path: Path, data: bytes = b"%PDF-1.4\n" + b"x" * 500) -> Path:
    path.write_bytes(data)
    return path


async def _turn(adapter, *, message_id: str = "9", thread_id: str | None = None) -> Turn:
    """A turn the adapter admitted: its processing hook saw the user's own message."""
    source = adapter.build_source(chat_id=CHAT, chat_type="dm", user_id=CHAT, thread_id=thread_id,
                                  message_id=message_id)
    await adapter.on_processing_start(MessageEvent(text="send it", source=source, message_id=message_id))
    return Turn(session_key="k", chat_id=CHAT, thread_id=thread_id, inbound_message_id=message_id, profile="")


async def _send(adapter, turn: Turn, paths: list[Path | str], *, as_files: bool = False, caption: str = ""):
    return await tools._deliver(adapter, turn, tools._resolve([str(p) for p in paths], turn.session_key),
                                as_files, caption)


def _texts(sender) -> list[str]:
    """The text messages the chat shows (a notice listing several files is a rich message)."""
    return [v.text for v in sender.shown(CHAT) if v.extra.get("kind") in ("text", "rich")]


def _plain(text: str) -> str:
    """MarkdownV2 as the user reads it, as far as escapes go."""
    import re
    return re.sub(r"\\(.)", r"\1", text)


# --- albums --------------------------------------------------------------------------------------------------


async def test_an_album_whose_reply_was_lost_is_not_sent_again_photo_by_photo(sender, tmp_path):
    photos = [(str(_png(tmp_path / f"p{i}.png", 30 + i)), None) for i in range(3)]
    sender.wire.fail("sendMediaGroup", 1, "lost")  # Telegram stored the album; its reply never came
    receipts = await sender.adapter.send_photos(CHAT, photos, None)
    assert len(sender.standin.calls_of("sendMediaGroup")) == 1 and not sender.standin.calls_of("sendPhoto")
    assert receipts == [Receipt(path, reason=LOST_REPLY) for path, _ in photos]
    assert any("may or may not be in the chat" in _plain(t) for t in _texts(sender))


async def test_a_file_whose_reply_was_lost_is_named_and_not_sent_again(sender, tmp_path):
    # Hermes counts an upload whose reply was lost as sent, with no message id and no notice (it may be in the
    # chat, hermes-agent#62079); the chat is told, as for an album.
    doc = _doc(tmp_path / "report.pdf")
    sender.wire.fail("sendDocument", 1, "lost")
    result = await sender.adapter.send_document(CHAT, str(doc))
    assert len(sender.standin.calls_of("sendDocument")) == 1
    assert not result.success and LOST_REPLY in result.error
    assert any(_plain(t) == f"report.pdf was not delivered: {LOST_REPLY}." for t in _texts(sender))


async def test_an_album_telegram_refused_is_sent_photo_by_photo(sender, tmp_path):
    photos = [(str(_png(tmp_path / f"p{i}.png", 30 + i)), None) for i in range(3)]
    sender.refuse("sendMediaGroup", "Bad Request: wrong file identifier", times=1)
    receipts = await sender.adapter.send_photos(CHAT, photos, None)
    sent = [c for c in sender.standin.calls_of("sendPhoto") if not c.faulted]
    assert len(sent) == 3
    assert [r.message_id for r in receipts] == [str(c.response["message_id"]) for c in sent]


# --- what telegram_send reports and sends ----------------------------------------------------------------------


async def test_each_photo_reports_its_own_message_id_and_an_oversize_one_its_reason(sender, tmp_path):
    big = tmp_path / "huge.png"
    with big.open("wb") as handle:
        handle.truncate(51 * 1024 * 1024)  # sparse: never read whole
    small = _png(tmp_path / "small.png")
    result = await _send(sender.adapter, await _turn(sender.adapter), [big, small])
    (photo,) = [c for c in sender.standin.calls_of("sendPhoto") if not c.faulted]
    assert result["sent"] == [{"file": str(small), "message_id": str(photo.response["message_id"])}]
    assert [d["file"] for d in result["not_delivered"]] == [str(big)]
    assert "51.0 MB" in result["not_delivered"][0]["reason"]


async def test_files_go_in_their_order_with_the_caption_on_the_first(sender, tmp_path):
    report, a, b, appendix = (_doc(tmp_path / "report.pdf"), _png(tmp_path / "a.png", 30),
                              _png(tmp_path / "b.png", 34), _doc(tmp_path / "appendix.pdf"))
    await _send(sender.adapter, await _turn(sender.adapter), [report, a, b, appendix], caption="Here")
    sent = [(c.method, c.params.get("caption", "")) for c in sender.standin.calls
            if c.method in ("sendDocument", "sendPhoto", "sendMediaGroup") and not c.faulted]
    assert sent == [("sendDocument", "Here"), ("sendMediaGroup", ""), ("sendDocument", "")]
    album = sender.standin.calls_of("sendMediaGroup")[0]
    assert [item.get("caption", "") for item in album.params["media"]] == ["", ""]


async def test_twenty_missing_photos_are_named_in_one_message(sender, tmp_path):
    missing = [tmp_path / f"gone-{i}.png" for i in range(20)]
    turn = await _turn(sender.adapter)
    result = await _send(sender.adapter, turn, missing)
    assert len(result["not_delivered"]) == 20 and result["sent"] == []
    (notice,) = _texts(sender)
    assert all(f"gone-{i}.png" in notice for i in range(20))
    assert sender.adapter.admitted_turn(turn).sent == 1  # the one notice


async def test_a_call_the_turn_cannot_hold_sends_nothing(sender, tmp_path):
    turn = await _turn(sender.adapter)
    sender.adapter.admitted_turn(turn).take(8)
    with pytest.raises(TurnRefused, match="at most 10"):
        await _send(sender.adapter, turn, [_doc(tmp_path / "a.pdf"), _doc(tmp_path / "b.pdf")])  # 2 + a notice
    assert not sender.standin.calls_of("sendDocument", "sendMessage")


async def test_a_file_gone_before_its_upload_is_named(sender, tmp_path):
    first, second = _doc(tmp_path / "first.pdf"), _doc(tmp_path / "second.pdf")
    sender.wire.before = lambda method: second.unlink(missing_ok=True) if method == "sendDocument" else None
    result = await _send(sender.adapter, await _turn(sender.adapter), [first, second], as_files=True)
    assert [d["file"] for d in result["not_delivered"]] == [str(second)]
    assert any(_plain(t) == f"second.pdf was not delivered: {GONE}." for t in _texts(sender))


async def test_an_audio_refusal_names_its_own_error_not_an_earlier_files(sender, tmp_path):
    doc, song = _doc(tmp_path / "doc.pdf"), _doc(tmp_path / "song.mp3", b"ID3" + b"a" * 300)
    sender.refuse("sendDocument", "Bad Request: wrong file type")
    sender.refuse("sendAudio", "Bad Request: audio is too long")
    await _send(sender.adapter, await _turn(sender.adapter), [doc, song])
    (notice,) = [_plain(t) for t in _texts(sender)]
    lines = {line.split(" was not delivered")[0].lstrip("- "): line for line in notice.splitlines() if "was not" in line}
    assert "wrong file type" in lines["doc.pdf"].lower() and "audio is too long" not in lines["doc.pdf"].lower()
    assert "audio is too long" in lines["song.mp3"].lower()


async def test_a_document_keeps_the_name_the_agent_gave_when_it_is_a_link(sender, tmp_path):
    target = _doc(tmp_path / "build-4821.pdf")
    link = tmp_path / "report.pdf"
    link.symlink_to(target)
    await _send(sender.adapter, await _turn(sender.adapter), [link], as_files=True)
    (doc,) = sender.standin.calls_of("sendDocument")
    assert doc.params["document"]["filename"] == "report.pdf"


async def test_a_notice_in_a_dm_topic_never_leaves_the_topic(sender, tmp_path):
    def to_topic(p: dict) -> bool:
        return "5" in (str(p.get("message_thread_id") or ""), str(p.get("direct_messages_topic_id") or ""))
    sender.refuse("sendMessage", "Bad Request: message thread not found", match=to_topic)  # the topic is gone
    turn = await _turn(sender.adapter, thread_id="5")
    # A tool's send (telegram_send runs _deliver under as_tool_send): Hermes' own delivery now moves a reply
    # whose private topic is gone to the chat root (hermes-agent#95517); a tool's notice must not.
    await as_tool_send(lambda: _send(sender.adapter, turn, [tmp_path / "missing.pdf"]))
    assert _texts(sender) == []  # no notice in the chat's root instead


# --- notices ---------------------------------------------------------------------------------------------------


async def test_a_notice_refused_by_a_flood_wait_arrives_after_it(sender):
    sender.adapter._record_send_flood_cooldown(CHAT, 1.0)
    await sender.adapter._not_delivered(CHAT, "/x/report.pdf", "it was refused", None)
    assert _texts(sender) == []
    for _ in range(50):  # the notice is sent on this loop: wait without blocking it
        if _texts(sender):
            break
        await asyncio.sleep(0.1)
    assert [_plain(t) for t in _texts(sender)] == ["report.pdf was not delivered: it was refused."]


async def test_a_placeholder_notice_is_found_by_the_path_hermes_hands_back(sender, tmp_path, monkeypatch):
    # The cache reached through a link: Hermes passes on the resolved path (gateway/platforms/base.py:1080).
    (tmp_path / "cache").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "cache")
    monkeypatch.setattr("gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "link")
    (notice, _), = sender.adapter.extract_media("MEDIA:/nonexistent/dir/report.pdf")[0]
    assert os.path.realpath(notice) != notice
    await sender.adapter.send_document(CHAT, os.path.realpath(notice))
    assert not sender.standin.calls_of("sendDocument")
    assert [_plain(t) for t in _texts(sender)] == [
        "report.pdf was not delivered: the file does not exist on the Hermes host."]


def test_a_missing_media_path_is_named_on_a_remote_backend_too(sender, monkeypatch):
    # With a remote sandbox Hermes tries a fetch first; a path it still cannot get is dropped all the same.
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    (path, _), = sender.adapter.extract_media("MEDIA:/workspace/report.pdf")[0]
    assert sender.adapter._placeholders()[os.path.realpath(path)] == (
        "report.pdf", "the file does not exist on the Hermes host")
