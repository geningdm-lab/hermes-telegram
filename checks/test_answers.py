"""The ordinary (MarkdownV2) path of the composed adapter, which needs no Telegram.

The rich sender's failure handling is tested at the real Telegram boundary in checks/e2e/test_sender.py.
"""

import pytest

from gateway.config import PlatformConfig
from hermes_telegram.adapter import HermesTelegramAdapter


@pytest.fixture
def adapter() -> HermesTelegramAdapter:
    return HermesTelegramAdapter(PlatformConfig(enabled=True, token="1:x"))


def test_the_ordinary_path_shows_an_escaped_dollar_as_a_dollar(adapter):
    # The hint asks the agent to write \$; MarkdownV2 would otherwise show the backslash.
    assert adapter.format_message(r"It costs \$5.") == r"It costs $5\."
