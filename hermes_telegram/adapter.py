"""The Telegram adapter this plugin registers in place of the bundled one.

It is composed from the bundled adapter's methods, harvested into one mixin per concern
(``harvested/adapter_*.py``), on the same bases the bundled ``TelegramAdapter`` had. Behaviour that this
plugin changes goes into classes listed before the harvested mixins, so it overrides them.

Slice 0 adds no behaviour of its own: the composed class behaves as the bundled adapter does.
"""

from gateway.platforms.base import BasePlatformAdapter

from .harvested.adapter_access import AccessMixin
from .harvested.adapter_core import CoreMixin
from .harvested.adapter_events import EventsMixin
from .harvested.adapter_groups import GroupsMixin
from .harvested.adapter_inbound import InboundMixin
from .harvested.adapter_outbound_legacy import OutboundLegacyMixin
from .harvested.adapter_pacing import PacingMixin
from .harvested.adapter_prompts import PromptsMixin
from .harvested.adapter_topics import TopicsMixin
from .harvested.adapter_transport import TransportMixin
from .harvested.telegram_held_inbound import TelegramHeldInboundMixin
from .harvested.telegram_topic_routing import TelegramTopicRoutingMixin


class HermesTelegramAdapter(
    CoreMixin,
    AccessMixin,
    TopicsMixin,
    TransportMixin,
    EventsMixin,
    PromptsMixin,
    PacingMixin,
    GroupsMixin,
    InboundMixin,
    OutboundLegacyMixin,
    # The bundled TelegramAdapter's own bases, in its order.
    TelegramHeldInboundMixin,
    TelegramTopicRoutingMixin,
    BasePlatformAdapter,
):
    """Telegram bot adapter: the bundled adapter's behaviour, as the base this plugin builds on."""
