"""Hermes' own messaging-adapter contract, Telegram leg, run against this plugin.

Hermes ships a contract every real adapter must pass (hermes-agent@c419cf7
tests/e2e/core/platforms/test_telegram_contract.py): a real ``hermes gateway run`` child on a throwaway
home, the adapter's python-telegram-bot talking to a local Bot API stand-in through ``extra.base_url``,
and a fake model. This module runs that rig and those scenarios unchanged, with two differences: the
child's home has this plugin installed and enabled, and the rig refuses to run unless the gateway loaded
it instead of the bundled adapter. Scenarios Hermes marks as red on its own main (``KNOWN``) keep that mark.

Needs the Hermes source at the pin (its tests are not installed): scripts/hermes-src.sh.
"""

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERMES_SRC = ROOT / ".cache" / "hermes-agent"
PLUGIN_DIR = ROOT / "hermes_telegram"

if not (HERMES_SRC / "tests" / "e2e" / "core" / "platforms" / "_suite.py").is_file():
    raise RuntimeError(f"Hermes source missing at {HERMES_SRC}: run scripts/hermes-src.sh")
# Appended, so installed packages win for everything but Hermes' own ``tests`` package.
sys.path.append(str(HERMES_SRC))

from tests.e2e.core.platforms import _contract as C  # noqa: E402
from tests.e2e.core.platforms import _suite  # noqa: E402
from tests.e2e.core.platforms._drv_telegram import TelegramDriver  # noqa: E402
from tests.e2e.core.platforms._helpers import GatewayUnderTest  # noqa: E402
from tests.e2e.core.platforms.test_telegram_contract import KNOWN, SKIP  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

# Only this plugin has modules under harvested/, so a gateway log line from one proves the running
# adapter is ours. (Discovery's "shadows the bundled copy" line is logged before the gateway log exists.)
OUR_LOGGER = "hermes_plugins.platforms__telegram.harvested."


class PluginTelegramDriver(TelegramDriver):
    """Hermes' Telegram driver, with this plugin enabled in the gateway's configuration."""

    def gateway_config(self) -> dict[str, Any]:
        return C.merge(super().gateway_config(), {"plugins": {"enabled": ["platforms/telegram"]}})


class PluginGatewayUnderTest(GatewayUnderTest):
    """Hermes' gateway child, with this plugin installed in its home before it starts."""

    def __init__(self, root: Path, **kwargs: Any) -> None:
        super().__init__(root, **kwargs)
        target = self.hermes_home / "plugins" / "platforms" / "telegram"
        shutil.copytree(PLUGIN_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))

    def start(self, timeout: float = 60.0) -> "PluginGatewayUnderTest":
        super().start(timeout)
        log = self.log_path.read_text(encoding="utf-8", errors="replace")
        if OUR_LOGGER not in log:
            raise AssertionError(f"the gateway did not load this plugin over the bundled one\n{self.tail()}")
        return self


# _suite builds its gateway through this module-level name.
_suite.GatewayUnderTest = PluginGatewayUnderTest
rig, rig_stream = _suite.rig_fixtures(PluginTelegramDriver)


@pytest.mark.parametrize("scenario", _suite.scenario_params(SKIP))
def test_contract(scenario: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    _suite.run_scenario(scenario, request, tmp_path, KNOWN)
