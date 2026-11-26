"""Slice 5 end to end: widgets as visual answers, and their requests back to the agent (rows 12 and 14).

A real gateway child with this plugin, a fake model scripted per inbound, and the Bot API stand-in of
checks/e2e/standin.py, which holds a widget to Telegram's limits and whose ``widget_query`` is a widget
calling ``TelegramWidget.request``. How a client draws the widget, and that its own interaction stays on the
device (row 13), are live acceptance: the sandbox runs on the user's device.
"""

import sys
import time

import pytest

from checks.e2e.rig import C, rig_fixtures, tool_call
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

rig, rig_stream = rig_fixtures()


def _diagram(request: str) -> str:
    return ('<svg viewBox="0 0 160 40"><rect width="60" height="30" rx="4"/><text x="70" y="20">Plan</text></svg>'
            f'<script>document.querySelector("svg").onclick = () => TelegramWidget.request("{request}")</script>')


def _texts_sent(rig, token: str) -> list[str]:
    return [rig.director._text(m) for r in rig.director.requests.get(token, []) for m in r.get("messages", [])[-1:]
            if m.get("role") == "user"]


def test_a_diagram_goes_as_a_widget_and_its_request_reaches_the_agent_which_updates_it(rig):
    # Row 12: a diagram drawn as a widget. Row 14: an action that needs the agent reaches it with its widget.
    offer, act = "wg-offer", "wg-act"
    html = _diagram(f"next [in:{act}]")
    rig.director.script(offer, tool_call(rig, "wgs", "telegram_widget", {"html": html, "caption": "The plan"}),
                        C.answer("A-wg-offer", "drawn"))
    inbound = rig.drv.dm(f"draw the plan [in:{offer}]")
    C.wait_reply(rig, inbound.chat_id, "A-wg-offer")
    (sent,) = [c for c in rig.drv.standin.calls_of("sendWidget") if not c.faulted]
    assert sent.params["html"] == html and sent.params.get("allow_queries") is True
    widget_id = str(sent.response["message_id"])

    rig.director.script(act, tool_call(rig, "wgm", "telegram_message",
                                       {"op": "widget_state", "message_id": widget_id, "state": "step 2"}),
                        C.answer("A-wg-act", "moved on"))
    pushed = time.monotonic()
    query = rig.drv.standin.widget_query(int(rig.drv.user_id), inbound.chat_id, int(widget_id), f"next [in:{act}]")
    (answered,) = wait_until(lambda: _answers(rig, [query]), "the widget's request answered", timeout=15,
                             on_timeout=rig.ctx)
    assert answered.at - pushed < 5.0  # Telegram allows 15 s; the agent's turn takes longer
    C.wait_reply(rig, inbound.chat_id, "A-wg-act", "the agent's answer to the widget")
    assert _texts_sent(rig, act)[0] == f'[widget] data="next [in:{act}]" on widget {widget_id}'
    (state,) = [c for c in rig.drv.standin.calls_of("editMessageWidgetData") if not c.faulted]
    assert (str(state.params["message_id"]), state.params["chat_data"]) == (widget_id, "step 2")


def _answers(rig, queries: list[dict]) -> list:
    """The successful answerWidgetQuery calls for these queries: one each, or nothing yet."""
    ids = [q["widget_query"]["id"] for q in queries]
    calls = [c for c in rig.drv.standin.calls_of("answerWidgetQuery")
             if not c.faulted and str(c.params.get("widget_query_id")) in ids]
    return calls if sorted(str(c.params["widget_query_id"]) for c in calls) == sorted(ids) else []


def test_two_presses_with_the_same_data_reach_the_agent_twice_each_answered_once(rig):
    # Review slice 5, P1: pressing "next" twice is two moves, not a background refresh to drop.
    offer, act = "wg-offer2", "wg-twice"
    rig.director.script(offer, tool_call(rig, "wgs2", "telegram_widget", {"html": _diagram(f"next [in:{act}]")}),
                        C.answer("A-wg-offer2", "drawn"))
    rig.director.script(act, C.answer("A-wg-twice-1", "step 2"), C.answer("A-wg-twice-2", "step 3"))
    inbound = rig.drv.dm(f"draw it [in:{offer}]")
    C.wait_reply(rig, inbound.chat_id, "A-wg-offer2")
    widget_id = int([c for c in rig.drv.standin.calls_of("sendWidget") if not c.faulted][-1].response["message_id"])
    queries = [rig.drv.standin.widget_query(int(rig.drv.user_id), inbound.chat_id, widget_id, f"next [in:{act}]")
               for _ in range(2)]
    C.wait_reply(rig, inbound.chat_id, "A-wg-twice-2", "the agent's answer to the second press")
    assert C.turns(rig, act) == 2
    assert len(_answers(rig, queries)) == 2  # every query answered, none twice


def test_a_widget_that_needs_the_network_is_refused_before_it_is_sent(rig):
    token = "wg-fetch"
    before = len(rig.drv.standin.calls_of("sendWidget"))
    html = '<script>fetch("https://example.com/data").then(r => r.json())</script>'
    rig.director.script(token, tool_call(rig, "wgf", "telegram_widget", {"html": html}), C.answer("A-wg-fetch", "ok"))
    inbound = rig.drv.dm(f"chart it live [in:{token}]")
    C.wait_reply(rig, inbound.chat_id, "A-wg-fetch")
    assert len(rig.drv.standin.calls_of("sendWidget")) == before
    tool_results = [rig.director._text(m) for r in rig.director.requests[token] for m in r.get("messages", [])
                    if m.get("role") == "tool"]
    assert any("no network" in t for t in tool_results), tool_results


def test_the_gateway_asks_telegram_for_widget_requests(rig):
    # Telegram delivers only the update kinds a bot asks for; PTB 22.8's list predates widgets.
    polls = [c for c in rig.drv.standin.calls_of("getUpdates") if c.params.get("allowed_updates")]
    assert polls and "widget_query" in polls[-1].params["allowed_updates"]
