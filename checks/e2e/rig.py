"""Hermes' own end-to-end rig for its Telegram adapter, pointed at this plugin.

The rig and its scenarios come from the Hermes source at the pin (hermes-agent@c419cf7
tests/e2e/core/platforms/): a real ``hermes gateway run`` child on a throwaway home, Hermes' Bot API
stand-in behind python-telegram-bot's ``extra.base_url``, and a fake model. The child runs Hermes from that
source tree (the rig puts it on the child's PYTHONPATH), so the tree must be exactly the pin.

This module changes two things: the child's home has this plugin installed and enabled, and every start of
a child must log this plugin's adapter line, so a run on the bundled adapter cannot pass.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERMES_SRC = ROOT / ".cache" / "hermes-agent"
PLUGIN_DIR = ROOT / "hermes_telegram"
PIN = (ROOT / "HERMES_PIN").read_text().strip()
ADAPTER_LINE = "Telegram adapter HermesTelegramAdapter from "


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(HERMES_SRC), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _require_pinned_source() -> None:
    if not (HERMES_SRC / ".git").is_dir():
        raise RuntimeError(f"Hermes source missing at {HERMES_SRC}: run scripts/hermes-src.sh")
    head = _git("rev-parse", "HEAD")
    if head != PIN:
        raise RuntimeError(f"{HERMES_SRC} is at {head}, not the pin {PIN}: run scripts/hermes-src.sh")
    dirty = _git("status", "--porcelain")
    if dirty:
        raise RuntimeError(f"{HERMES_SRC} differs from the pin:\n{dirty}")
    # Ignored by git, yet read by the child: hermes_cli/main.py:715 loads the source root's .env as a
    # development fallback, which could bring real credentials or a webhook URL into a test gateway.
    if (HERMES_SRC / ".env").exists():
        raise RuntimeError(f"{HERMES_SRC / '.env'} exists; the test gateway would load it. Remove it.")


_require_pinned_source()
# Appended, so installed packages win in this process for everything but Hermes' own ``tests`` package.
sys.path.append(str(HERMES_SRC))

from tests.e2e.core.platforms import _contract as C  # noqa: E402
from tests.e2e.core.platforms import _suite  # noqa: E402
from tests.e2e.core.platforms._drv_telegram import TelegramDriver  # noqa: E402
from tests.e2e.core.platforms._helpers import GatewayUnderTest  # noqa: E402

from checks.e2e.standin import MediaTelegramStandin  # noqa: E402  (imports Hermes' stand-in)

__all__ = ["C", "PluginGatewayUnderTest", "PluginTelegramDriver", "rig_fixtures"]


class PluginTelegramDriver(TelegramDriver):
    """Hermes' Telegram driver, with this plugin enabled and a stand-in that knows rich messages.

    Hermes' own stand-in answers ``sendRichMessage`` with 404, which the adapter takes as "no rich messages
    here" and remembers for the process; the extended one answers it as the Bot API documents.
    ``connected`` waits for a poll newer than the latest start: Hermes' driver accepts any earlier
    ``getUpdates``, so on a restart it would report a connection the new process has not made.
    """

    _polls_before_start = 0

    def __init__(self) -> None:
        self.standin = MediaTelegramStandin()

    def gateway_config(self) -> dict[str, Any]:
        return C.merge(super().gateway_config(), {"plugins": {"enabled": ["platforms/telegram"]}})

    def expect_fresh_poll(self) -> None:
        self._polls_before_start = len(self.standin.calls_of("getUpdates"))

    def connected(self) -> bool:
        return len(self.standin.calls_of("getUpdates")) > self._polls_before_start


class PluginGatewayUnderTest(GatewayUnderTest):
    """Hermes' gateway child, with this plugin installed in its home and its adapter checked per start."""

    def __init__(self, root: Path, **kwargs: Any) -> None:
        super().__init__(root, **kwargs)
        target = self.hermes_home / "plugins" / "platforms" / "telegram"
        shutil.copytree(PLUGIN_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))

    @property
    def info_log(self) -> Path:
        """Hermes' own INFO-level gateway log; ``log_path`` is the child's console, WARNING and up."""
        return self.hermes_home / "logs" / "gateway.log"

    def start(self, timeout: float = 60.0) -> "PluginGatewayUnderTest":
        # The log is appended across restarts, and Hermes rotates it at 5 MiB (hermes_logging.py:399): only
        # what this start adds counts, read from the start of a file that replaced the earlier one.
        before = self.info_log.stat() if self.info_log.exists() else None
        driver = getattr(self._ready, "__self__", None)
        if isinstance(driver, PluginTelegramDriver):
            driver.expect_fresh_poll()  # so start() returns only once this process has polled
        super().start(timeout)
        expected = ADAPTER_LINE + str((self.hermes_home / "plugins" / "platforms" / "telegram").resolve())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.info_log.exists():
                now = self.info_log.stat()
                same_file = before is not None and (now.st_ino, now.st_dev) == (before.st_ino, before.st_dev)
                with self.info_log.open("rb") as log:
                    log.seek(before.st_size if same_file else 0)
                    if expected in log.read().decode("utf-8", "replace"):
                        return self
            time.sleep(0.2)
        raise AssertionError(f"this start of the gateway did not build this plugin's adapter\n{self.tail()}")


def rig_fixtures(driver_cls: type = PluginTelegramDriver) -> tuple[Any, Any]:
    """Hermes' module-scoped ``(rig, rig_stream)`` fixtures, building this plugin's gateway."""
    _suite.GatewayUnderTest = PluginGatewayUnderTest  # _suite builds its gateway through this name
    return _suite.rig_fixtures(driver_cls)
