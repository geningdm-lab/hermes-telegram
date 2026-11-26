"""Synthetic samples the acceptance rows send, generated under ``STATE/samples`` (ffmpeg from mise).

The record identifies every upload by its SHA-256, so an album's order is read from what the bot uploaded.
The album photos are solid colours, so the owner can see that order on iOS too.
"""

import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

from .home import STATE

SAMPLES = STATE / "samples"
# Twelve hues far enough apart to tell after JPEG recompression; the first three are row 4's album of 3.
COLOURS = ["ff0000", "00a000", "0000ff", "ffff00", "ff00ff", "00ffff", "ff8000", "8000ff", "808080", "804000",
           "ffffff", "000000"]
COLOUR_NAMES = ["red", "green", "blue", "yellow", "magenta", "cyan", "orange", "purple", "grey", "brown", "white",
                "black"]
BIG_BYTES = 51 * 1024 * 1024  # over the Bot API's 50 MB upload limit (row 15)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def generate() -> dict[str, Path]:
    """Every sample, generated once."""
    SAMPLES.mkdir(parents=True, exist_ok=True)
    out = {
        "document": SAMPLES / "acceptance-document.zip",
        "photo": SAMPLES / "acceptance-photo.png",
        "video": SAMPLES / "acceptance-video.mp4",
        "audio": SAMPLES / "acceptance-audio.m4a",
        "voice": SAMPLES / "acceptance-voice.ogg",
        "big": SAMPLES / "acceptance-too-big.bin",
        "empty": SAMPLES / "acceptance-empty.txt",
        "stop": SAMPLES / "acceptance-after-stop.zip",
    }
    for key in ("document", "stop"):
        if not out[key].exists():
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("README.txt", f"hermes-telegram acceptance sample: {key}\n" * 40)
            out[key].write_bytes(buffer.getvalue())
    if not out["photo"].exists():
        _ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x480", "-frames:v", "1", str(out["photo"]))
    if not out["video"].exists():
        _ffmpeg("-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=25", "-pix_fmt", "yuv420p",
                str(out["video"]))
    if not out["audio"].exists():
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", str(out["audio"]))
    if not out["voice"].exists():
        _ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:duration=3", "-c:a", "libopus", str(out["voice"]))
    if not out["big"].exists():
        with open(out["big"], "wb") as big:
            big.truncate(BIG_BYTES)
    out["empty"].write_bytes(b"")
    for n, colour in enumerate(COLOURS, start=1):
        path = SAMPLES / f"acceptance-photo-{n:02d}.png"
        if not path.exists():
            _ffmpeg("-f", "lavfi", "-i", f"color=c=0x{colour}:s=320x240", "-frames:v", "1", str(path))
        out[f"photo-{n:02d}"] = path
    return out


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

