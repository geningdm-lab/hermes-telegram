"""A widget's HTML against Telegram's limits and its sandbox (hermes_telegram/widgets.py).

Sizes are counted as bots/widgets says the server counts them: UTF-8 bytes, and zlib at level 9.
"""

import hashlib

import pytest

from hermes_telegram.widgets import check_state, check_widget

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
    # Review slice 5, P2: a relative image shows a placeholder; a worker throws.
    ('<img src="chart.png">', "loads nothing from outside"),
    ('<img src="data:image/svg+xml;base64,PHN2Zy8+">', "loads nothing from outside"),  # data: SVG is out
    ('<script>const w = new Worker("plot.js")</script>', "no network"),
    ('<button onclick="fetch(\'/x\')">Go</button>', "no network"),
    ("<style>@import 'theme.css';</style>", "loads nothing from outside"),
    ("<video></video>", "no <video>"),
])
def test_a_widget_telegram_or_its_sandbox_would_refuse_is_named_with_the_reason(html, why):
    assert why in (check_widget(html) or "")


def test_data_urls_and_inline_svg_are_allowed():
    assert check_widget('<img src="data:image/png;base64,iVBORw0KGgo=">') is None


@pytest.mark.parametrize("html", ids=lambda v: v[:24], argvalues=[
    # Review slice 5, P2: text that names an API runs nothing, and a link opens on a tap.
    "<p>How a WebSocket differs from fetch()</p>",
    '<a href="https://core.telegram.org/bots/widgets">Read more</a>',
    '<svg><defs><linearGradient id="g"/></defs><rect fill="url(#g)"/><use href="#g"/></svg>',
    '<script>const html = "<img src=x.png>"; const note = "uses fetch() elsewhere";</script>',
])
def test_a_widget_that_only_mentions_the_network_or_links_out_passes(html):
    assert check_widget(html) is None


@pytest.mark.parametrize(("state", "why"), [
    ("step 2", None),
    ("", None),  # an intentional clear
    ("a\x00b", "NUL"),  # Review slice 5, P3: Telegram refuses it in a send and an edit
    ("\ud800", "UTF-8"),  # a lone surrogate is a refusal, not a crash
    ("x" * 1025, "at most 1024"),
    (None, "must be a string"),
])
def test_a_state_is_held_to_telegrams_limits(state, why):
    assert (check_state(state) or None) == why or why in (check_state(state) or "")
