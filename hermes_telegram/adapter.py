"""The Telegram adapter this plugin registers in place of the bundled one.

It is composed from the bundled adapter's methods, harvested into one mixin per concern
(``harvested/adapter_*.py``), on the same bases the bundled ``TelegramAdapter`` had. Behaviour that this
plugin changes goes into its own mixins, listed before the harvested ones so they override them:

- ``RichAnswersMixin`` (slice 1): answers as Telegram rich messages, long ones cut into several.
"""

from gateway.platforms.base import BasePlatformAdapter

from .answers import RichAnswersMixin
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
    # This plugin's behaviour.
    RichAnswersMixin,
    # The bundled adapter's, harvested.
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
    """Telegram bot adapter: the bundled adapter's transport and routing, with this plugin's answers."""
