"""Slice 2 end to end: files arrive as their kind, photos as albums, "as a file" as documents, and a file
that cannot be delivered is named with the reason (rows 3, 4, 5, 15).

A real gateway child with this plugin, a fake model scripted per inbound, and the Bot API stand-in of
checks/e2e/standin.py, which checks the documented upload limits. What a scenario asserts is what Telegram
received; whether a client plays it is live acceptance.
"""

import hashlib
import io
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from checks.e2e.rig import C, rig_fixtures
from tests.fakes.fake_llm_provider import ToolCall
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, _rig_stream = rig_fixtures()

MEDIA_METHODS = ("sendPhoto", "sendDocument", "sendVideo", "sendAudio", "sendVoice", "sendMediaGroup",
                 "sendAnimation")


def _png(side: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (side, side), (side % 255, 40, 90)).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("media")
    files = {
        "doc": ("report.pdf", b"%PDF-1.4\n" + b"x" * 5000),
        "photo": ("photo.png", _png(64)),
        "video": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"v" * 4000),
        "audio": ("song.mp3", b"ID3" + b"a" * 3000),
        "voice": ("note.ogg", b"OggS" + b"o" * 2000),
        "rejected": ("refused.pdf", b"%PDF-1.4\n" + b"r" * 100),
    }
    out = {key: root / name for key, (name, _) in files.items()}
    for key, (_, data) in files.items():
        out[key].write_bytes(data)
    for i in range(12):  # distinct sizes, so their order can be read from the upload
        path = root / f"album-{i:02d}.png"
        path.write_bytes(_png(20 + 4 * i))
        out[f"album{i}"] = path
    big = root / "big.bin"
    with big.open("wb") as handle:
        handle.truncate(51 * 1024 * 1024)  # sparse: nothing that size is ever read
    out["big"] = big
    return out


class _Turn:
    """One scripted turn: its chat and the stand-in calls made since its inbound."""

    def __init__(self, rig: Any, tag: str, *responses: Any, body: str = "done") -> None:
        token, self.aid = f"f-{tag}", f"A-f-{tag}"
        self._rig = rig
        self._since = len(rig.drv.standin.calls)
        rig.director.script(token, *responses, C.answer(self.aid, body))
        inbound = rig.drv.dm(f"files please [in:{token}]")
        self.chat_id = inbound.chat_id
        C.wait_reply(rig, self.chat_id, self.aid)

    def calls(self, *methods: str) -> list[Any]:
        return [c for c in self._rig.drv.standin.calls[self._since:] if c.method in methods
                and str(c.params.get("chat_id")) == self.chat_id and not c.faulted]

    def wait(self, what: str, predicate: Any) -> None:
        wait_until(predicate, what, timeout=C.TURN_TIMEOUT)

    def notices(self) -> list[str]:
        """Not-delivered notices as the user reads them (the stand-in holds their MarkdownV2 source)."""
        texts = [C.norm(str(c.params.get("text", ""))) for c in self.calls("sendMessage")]
        return [t for t in texts if "was not delivered" in t]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _upload(call: Any, field: str) -> dict[str, Any]:
    return call.params[field]


def test_each_kind_of_file_arrives_as_that_kind(rig, media):
    # Row 3: document, photo, video, audio.
    lines = "\n".join(f"MEDIA:{media[k]}" for k in ("doc", "photo", "video", "audio"))
    turn = _Turn(rig, "kinds", body=f"Here they are:\n{lines}\n")
    turn.wait("four media sends", lambda: len(turn.calls(*MEDIA_METHODS)) >= 4)
    doc = turn.calls("sendDocument")
    assert [_upload(c, "document")["filename"] for c in doc] == ["report.pdf"]
    assert _upload(doc[0], "document")["sha256"] == _sha256(media["doc"])  # bytes unchanged
    assert len(turn.calls("sendPhoto")) == 1
    assert len(turn.calls("sendVideo")) == 1
    assert len(turn.calls("sendAudio")) == 1


def test_a_voice_note_arrives_as_a_voice_message(rig, media):
    # Row 3, voice: Hermes sends audio as a voice message when the answer carries [[audio_as_voice]]
    # (the directive applies to the whole answer, so it has a turn of its own here).
    turn = _Turn(rig, "voice", body=f"Listen:\n[[audio_as_voice]]\nMEDIA:{media['voice']}\n")
    turn.wait("the voice message", lambda: turn.calls("sendVoice"))
    assert len(turn.calls("sendVoice")) == 1 and not turn.calls("sendDocument", "sendAudio")


@pytest.mark.parametrize(("count", "albums"), [(3, [3]), (11, [9, 2]), (12, [10, 2])])
def test_photos_arrive_as_albums_of_up_to_ten_in_order(rig, media, count: int, albums: list[int]):
    # Row 4.
    lines = "\n".join(f"MEDIA:{media[f'album{i}']}" for i in range(count))
    turn = _Turn(rig, f"album{count}", body=f"Photos:\n{lines}\n")
    turn.wait(f"{len(albums)} album(s)", lambda: len(turn.calls("sendMediaGroup")) >= len(albums))
    groups = turn.calls("sendMediaGroup")
    assert [len(g.params["media"]) for g in groups] == albums
    sizes = [g.params[item["media"][len("attach://"):]]["size"] for g in groups for item in g.params["media"]]
    assert sizes == [media[f"album{i}"].stat().st_size for i in range(count)]
    assert not turn.calls("sendPhoto"), "a photo of an album went alone"


def test_media_asked_for_as_files_arrive_as_documents_with_name_and_bytes_unchanged(rig, media):
    # Row 5: the agent sends a photo, a video, audio and a voice note "as a file".
    # Hermes defers plugin tools behind its tool-search bridge (website/docs/user-guide/features/tool-search.md),
    # so the model reaches telegram_send through tool_call, as a real model would.
    keys = ("photo", "video", "audio", "voice")
    args = {"files": [str(media[k]) for k in keys], "as_files": True}
    offered = C._offered_tools(rig, "asfiles")
    assert "telegram_send" in offered or "tool_call" in offered, sorted(offered)
    call = (ToolCall("telegram_send", args) if "telegram_send" in offered
            else ToolCall("tool_call", {"calls": [{"name": "telegram_send", "arguments": args}]}))
    turn = _Turn(rig, "asfiles", call)
    turn.wait("four documents", lambda: len(turn.calls("sendDocument")) >= 4)
    docs = turn.calls("sendDocument")
    assert [_upload(c, "document")["filename"] for c in docs] == [media[k].name for k in keys]
    assert [_upload(c, "document")["sha256"] for c in docs] == [_sha256(media[k]) for k in keys]
    assert all(str(c.params.get("disable_content_type_detection")).lower() == "true" for c in docs)
    assert not turn.calls("sendPhoto", "sendVideo", "sendAudio", "sendVoice", "sendMediaGroup")


def test_a_file_over_the_upload_limit_is_named_with_its_size_and_not_uploaded(rig, media):
    # Row 15, first half: over Telegram's 50 MB limit for bots.
    turn = _Turn(rig, "big", body=f"The archive:\nMEDIA:{media['big']}\n")
    turn.wait("the not-delivered notice", lambda: turn.notices())
    assert turn.notices() == ["big.bin was not delivered: it is 51.0 MB, over Telegram's 50 MB upload limit for bots."]
    assert not turn.calls(*MEDIA_METHODS)


def test_a_file_telegram_refuses_is_named_with_telegrams_reason(rig, media):
    # Row 15, second half: the send fails.
    rig.drv.standin.fail("sendDocument", {"ok": False, "error_code": 400,
                                          "description": "Bad Request: wrong file type"}, status=400, times=1,
                         match=lambda p: (p.get("document") or {}).get("filename") == "refused.pdf")
    turn = _Turn(rig, "refused", body=f"The file:\nMEDIA:{media['rejected']}\n")
    turn.wait("the not-delivered notice", lambda: turn.notices())
    (notice,) = turn.notices()
    assert notice.startswith("refused.pdf was not delivered: Telegram refused the upload (")
    assert "wrong file type" in notice.lower()


def test_a_media_path_that_does_not_exist_is_named_instead_of_dropped(rig):
    # Hermes drops a MEDIA path it cannot find and only logs it: the chat saw nothing (PRD Q12).
    turn = _Turn(rig, "missing", body="Your report:\nMEDIA:/nonexistent/dir/report.pdf\n")
    turn.wait("the not-delivered notice", lambda: turn.notices())
    assert turn.notices() == ["report.pdf was not delivered: the file does not exist on the Hermes host."]
    assert not turn.calls(*MEDIA_METHODS)
