"""Business inbox slice 1 end to end (docs/business-inbox.md): B1, B3, B6, B7 and B10 through a real gateway.

A real gateway child with this plugin and ``business_inbox`` in the platform's ``extra``, a fake model, and the
Bot API stand-in of checks/e2e/standin.py, whose ``business_*`` methods are the owner's account connected to the
bot. What a scenario asserts is what the model received (nothing from those chats), what Telegram was asked to
do (nothing in them), and what the inbox file holds.
"""

import json
import sys
from typing import Any

import pytest

from checks.e2e.rig import C, PluginTelegramDriver, rig_fixtures
from hermes_telegram.inbox import STORE_NAME, Author, InboxStore
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX gateway harness")]

OWNER = int(PluginTelegramDriver.user_id)  # the owner also talks to the bot, as in the deployment
STRANGER = int(PluginTelegramDriver.other_user_id)  # an account whose owner is not configured
CONTACT, STRANGERS_CONTACT = 7001, 7002


class InboxDriver(PluginTelegramDriver):
    def gateway_config(self) -> dict[str, Any]:
        return C.merge(super().gateway_config(), {"platforms": {"telegram": {"extra": {
            "business_inbox": {"owners": {str(OWNER): "default"}}}}}})


rig, rig_stream = rig_fixtures(InboxDriver)


def test_the_owners_business_chats_are_kept_and_nobody_answers_them(rig):
    standin = rig.drv.standin
    store = InboxStore(rig.gw.hermes_home / STORE_NAME)
    standin.business_connect(OWNER, "bc-owner")
    first = standin.business_message("bc-owner", CONTACT, CONTACT, "are we still on for dinner?")
    first_id = first["business_message"]["message_id"]
    standin.business_message("bc-owner", CONTACT, OWNER, "yes, at 8")
    standin.business_message("bc-owner", CONTACT, CONTACT, "are we still on, at 9?", edit_of=first_id)
    standin.business_delete("bc-owner", CONTACT, [first_id])
    standin.business_connect(STRANGER, "bc-stranger")
    standin.business_message("bc-stranger", STRANGERS_CONTACT, STRANGERS_CONTACT, "stranger's words")

    wait_until(lambda: any(r.deleted for r in store.records()), "the deletion kept", timeout=C.TURN_TIMEOUT,
               on_timeout=rig.ctx)
    C.barrier(rig, "bi-barrier")  # a turn any of them started would have been handled before this one

    kept = [(r.chat, r.author, [v.text for v in r.versions], r.deleted is not None) for r in store.records()]
    assert kept == [(CONTACT, Author.CONTACT, ["are we still on for dinner?", "are we still on, at 9?"], True),
                    (CONTACT, Author.OWNER, ["yes, at 8"], False)]
    assert store.connection("bc-stranger") is None  # B6: nothing of an owner not configured
    sent = json.dumps(rig.llm.requests, ensure_ascii=False)
    assert "dinner" not in sent and "yes, at 8" not in sent and "stranger's words" not in sent
    # B7. Updates of different chats run concurrently (harvested/update_admission.py, PerChatUpdateProcessor) and
    # a connection update has no chat, so a message can come first; looking its connection up is all it may do.
    business_chats = {str(CONTACT), str(STRANGERS_CONTACT)}
    assert {c.method for c in standin.calls if c.params.get("business_connection_id")
            or str(c.params.get("chat_id")) in business_chats} <= {"getBusinessConnection"}
