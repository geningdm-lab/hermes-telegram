"""A widget's HTML against Telegram's limits and its sandbox (hermes_telegram/widgets.py).

Sizes are counted as bots/widgets says the server counts them: UTF-8 bytes, and zlib at level 9.
"""

import hashlib

import pytest

from hermes_telegram.widgets import check_widget

# ~10 KB of hex digests: within 32 KB, but about 5 KB after zlib at level 9.
NOISE = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(160))

DIAGRAM = ('<svg viewBox="0 0 120 40"><rect width="50" height="30" rx="4"/><text x="60" y="20">A to B</text></svg>'
           '<script>document.querySelector("svg").onclick = () => TelegramWidget.request("next")</script>')


def test_a_self_contained_widget_passes():
    assert check_widget(DIAGRAM) is None


@pytest.mark.parametrize(("html", "why"), ids=lambda v: str(v)[:24], argvalues=[
    ("", "empty"),
    ("<p>a\x00b</p>", "NUL"),
    ("<p>" + "x" * 33000 + "</p>", "33007 bytes"),
    ("<p>" + NOISE + "</p>", "compressed"),
    ('<script>fetch("https://example.com")</script>', "no network"),
    ("<script>new WebSocket('wss://x')</script>", "no network"),
    ('<script src="https://cdn.example.com/chart.js"></script>', "loads nothing from outside"),
    ('<img src="https://example.com/a.png">', "loads nothing from outside"),
    ('<div style="background: url(//example.com/a.png)"></div>', "loads nothing from outside"),
])
def test_a_widget_telegram_or_its_sandbox_would_refuse_is_named_with_the_reason(html, why):
    assert why in (check_widget(html) or "")


def test_data_urls_and_inline_svg_are_allowed():
    assert check_widget('<img src="data:image/png;base64,iVBORw0KGgo=">') is None
