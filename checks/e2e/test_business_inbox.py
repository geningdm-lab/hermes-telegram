"""Business inbox slice 1 end to end (docs/business-inbox.md): B1, B3, B4, B6, B7 and B10 through a real gateway.

A real gateway child with this plugin and ``business_inbox`` in the platform's ``extra``, a fake model, and the
Bot API stand-in of checks/e2e/standin.py, whose ``business_*`` methods are the owner's account connected to the
bot. What a scenario asserts is what the model received (nothing from those chats), what Telegram was asked to
do while they were handled (nothing but a connection lookup), and what the inbox file holds.
"""

import json
import sys
from typing import Any

import pytest

from checks.e2e.rig import C, PluginTelegramDriver, rig_fixtures
from hermes_telegram.inbox import STORE_NAME, Attachment, Author, InboxStore
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

OWNER = int(PluginTelegramDriver.user_id)  # the owner also talks to the bot, as in the deployment
STRANGER = int(PluginTelegramDriver.other_user_id)  # an account whose owner is not configured
CONTACT, STRANGERS_CONTACT = 7001, 7002
PHOTO = [{"file_id": "biz-photo", "file_unique_id": "biz-photo", "width": 640, "height": 480, "file_size": 2048}]


class InboxDriver(PluginTelegramDriver):
    def gateway_config(self) -> dict[str, Any]:
        return C.merge(super().gateway_config(), {"platforms": {"telegram": {"extra": {
            "business_inbox": {"owners": {str(OWNER): "default"}}}}}})


rig, rig_stream = rig_fixtures(InboxDriver)


def _handled(rig: Any, update_ids: list[int]) -> bool:
    """Each update's handling has ended: the harvested admission logs, once its claim is released, every update
    no core handler served (``harvested/update_admission.py``, ``_release_claim``), which is every business one."""
    log = rig.gw.info_log.read_text(errors="replace") if rig.gw.info_log.exists() else ""
    return all(f"Update {u} matched no core handler" in log for u in update_ids)


def test_the_owners_business_chats_are_kept_and_nobody_answers_them(rig):
    standin = rig.drv.standin
    store = InboxStore(rig.gw.hermes_home / STORE_NAME)
    # The command menu goes out in three scopes after connecting (_run_post_connect_housekeeping): let it finish,
    # so what the bot asks of Telegram from here on is what the business updates caused.
    wait_until(lambda: len(standin.calls_of("setMyCommands")) >= 3, "the command menu registered",
               timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)
    before = len(standin.calls)
    first = standin.business_message("bc-owner", CONTACT, CONTACT, "are we still on for dinner?")
    first_id = first["business_message"]["message_id"]
    updates = [
        standin.business_connect(OWNER, "bc-owner"), first,
        standin.business_message("bc-owner", CONTACT, OWNER, "yes, at 8"),
        standin.business_message("bc-owner", CONTACT, CONTACT, caption="the place", photo=PHOTO),
        standin.business_message("bc-owner", CONTACT, CONTACT, "are we still on, at 9?", edit_of=first_id),
        standin.business_delete("bc-owner", CONTACT, [first_id]),
        standin.business_connect(STRANGER, "bc-stranger"),
        standin.business_message("bc-stranger", STRANGERS_CONTACT, STRANGERS_CONTACT, "stranger's words"),
    ]
    wait_until(lambda: _handled(rig, [u["update_id"] for u in updates]), "every business update handled",
               timeout=C.TURN_TIMEOUT, on_timeout=rig.ctx)
    # B7, over everything the bot asked of Telegram meanwhile. Updates of different chats run concurrently and a
    # connection update has no chat, so a message can come before its connection: a lookup is all it may cause.
    assert {c.method for c in standin.calls[before:]} <= {"getUpdates", "getBusinessConnection"}

    kept = [(r.author, [v.text for v in r.versions], r.media, r.deleted is not None) for r in store.records()]
    assert sorted(kept, key=str) == sorted([
        (Author.CONTACT, ["are we still on for dinner?", "are we still on, at 9?"], (), True),
        (Author.OWNER, ["yes, at 8"], (), False),
        (Author.CONTACT, ["the place"], (Attachment("photo", size=2048, width=640, height=480),), False),
    ], key=str)
    assert store.connection("bc-stranger") is None  # B6: nothing of an owner not configured

    C.barrier(rig, "bi-barrier")  # the gateway idle and the owner's chat answered: no turn of theirs is pending
    sent = json.dumps(rig.llm.requests, ensure_ascii=False)
    assert "dinner" not in sent and "yes, at 8" not in sent and "the place" not in sent
    assert "stranger's words" not in sent
