"""Hermes' own messaging-adapter contract, Telegram leg, run against this plugin.

The scenarios are hermes-agent@c419cf7 tests/e2e/core/platforms/test_telegram_contract.py's, unchanged,
on the rig of checks/e2e/rig.py. Scenarios Hermes marks red on its own main (``KNOWN``) keep that mark.
Needs the Hermes source at the pin (its tests are not installed): scripts/hermes-src.sh.
"""

import sys
from pathlib import Path

import pytest

from checks.e2e.rig import rig_fixtures
from tests.e2e.core.platforms import _suite
from tests.e2e.core.platforms.test_telegram_contract import KNOWN, SKIP

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, rig_stream = rig_fixtures()


@pytest.mark.parametrize("scenario", _suite.scenario_params(SKIP))
def test_contract(scenario: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    _suite.run_scenario(scenario, request, tmp_path, KNOWN)
