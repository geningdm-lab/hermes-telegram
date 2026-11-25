"""Installed at $HERMES_HOME/plugins/platforms/telegram/ and enabled, the plugin replaces the bundled one.

Runs Hermes' real plugin discovery in a child interpreter on a throwaway home. The control case, no
install, gets the bundled adapter: it shows the check can tell the two apart. An install that is not
enabled still shadows the bundled plugin, so Telegram has no adapter at all: deployment must install and
enable together (docs/install.md).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes_telegram"

# Bundled and user platform plugins both import as hermes_plugins.platforms__telegram, so the probe tells
# them apart by where the adapter's module file lives.
PROBE = """
import json, os, sys
from hermes_cli.plugins import discover_plugins
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry
discover_plugins(force=True)
adapter = platform_registry.create_adapter("telegram", PlatformConfig(enabled=True, token="1:x"))
if adapter is None:
    print(json.dumps(None))
else:
    path = sys.modules[type(adapter).__module__].__file__
    origin = "user" if path.startswith(os.environ["HERMES_HOME"]) else "bundled"
    print(json.dumps([origin, type(adapter).__name__]))
"""


def _install(hermes_home: Path, *, installed: bool, enabled: bool) -> None:
    hermes_home.mkdir(parents=True)
    if installed:
        target = hermes_home / "plugins" / "platforms" / "telegram"
        shutil.copytree(PLUGIN_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    config = "plugins:\n  enabled:\n    - platforms/telegram\n" if enabled else ""
    (hermes_home / "config.yaml").write_text(config, encoding="utf-8")


def _probe(home: Path) -> list[str] | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "TELEGRAM_"))}
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"))
    out = subprocess.run([sys.executable, "-I", "-c", PROBE], env=env, cwd=home, capture_output=True,
                         text=True, timeout=120, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(("installed", "enabled", "expected"), [
    (True, True, ["user", "HermesTelegramAdapter"]),
    (False, False, ["bundled", "TelegramAdapter"]),
    (True, False, None),
], ids=["installed-enabled", "not-installed", "installed-not-enabled"])
def test_install_decides_which_adapter_telegram_gets(tmp_path: Path, installed: bool, enabled: bool,
                                                     expected: list[str] | None):
    home = tmp_path / "home"
    _install(home / ".hermes", installed=installed, enabled=enabled)
    assert _probe(home) == expected
