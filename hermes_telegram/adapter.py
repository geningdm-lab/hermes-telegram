"""The Telegram adapter this plugin registers in place of the bundled one.

It is composed from the bundled adapter's methods, harvested into one mixin per concern
(``harvested/adapter_*.py``), on the harvested copies of the bases the bundled ``TelegramAdapter`` has. Behaviour that this
plugin changes goes into its own mixins, listed before the harvested ones so they override them:

- ``RichAnswersMixin`` (slice 1): answers as Telegram rich messages, long ones cut into several.
- ``FilesMixin`` (slice 2): files as their kind or as documents; a file that does not arrive is named
  with the reason.
- ``HumanTurnsMixin`` (slice 2): the human Telegram events being processed, which the agent's tools act for.
- ``UpdateKindsMixin`` (slice 3): asks Telegram for the Bot API update kinds PTB 22.8 does not list.
- ``PreviewMixin`` (slice 3): the preview of a turn in a private chat: status, partial answer, Stop.
- ``ButtonsMixin`` (slice 4): the agent's action buttons, whose taps come back to it as messages.
- ``ActionsMixin`` (slice 4): polls, reactions and stickers, and the answers and reactions that come back.
- ``WidgetsMixin`` (slice 5): widgets as visual answers, and their requests back to the agent.
- ``MessagesMixin`` (slice 6): the agent's own messages edited, deleted, pinned and unpinned.
- ``BusinessInboxMixin`` (docs/business-inbox.md, slice 1): business updates kept in the inbox, never seen by
  any other handler.
"""

from gateway.platforms.base import BasePlatformAdapter

from .actions import ActionsMixin
from .answers import RichAnswersMixin
from .buttons import ButtonsMixin
from .files import FilesMixin
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
from .harvested.telegram_flood_cooldown import TelegramFloodCooldownMixin
from .harvested.telegram_held_inbound import TelegramHeldInboundMixin
from .harvested.telegram_ingress import TelegramContactMixin
from .harvested.telegram_media_flood import TelegramMediaFloodMixin
from .harvested.telegram_polling_probe import TelegramPendingProbeMixin
from .harvested.telegram_replacement import TelegramReplacementMixin
from .harvested.telegram_send_path_recovery import TelegramSendPathRecoveryMixin
from .harvested.telegram_topic_routing import TelegramTopicRoutingMixin
from .harvested.telegram_vault_code import TelegramVaultCodeMixin
from .inbox import BusinessInboxMixin
from .messages import MessagesMixin
from .turn import HumanTurnsMixin
from .preview import PreviewMixin
from .updates import UpdateKindsMixin
from .widgets import WidgetsMixin


class HermesTelegramAdapter(
    # This plugin's behaviour.
    BusinessInboxMixin,
    MessagesMixin,
    ButtonsMixin,
    WidgetsMixin,
    ActionsMixin,
    PreviewMixin,
    RichAnswersMixin,
    FilesMixin,
    HumanTurnsMixin,
    UpdateKindsMixin,
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
    # The bundled TelegramAdapter's own bases, in its order (scripts/harvest.py BASES).
    TelegramContactMixin,
    TelegramHeldInboundMixin,
    TelegramReplacementMixin,
    TelegramTopicRoutingMixin,
    TelegramPendingProbeMixin,
    TelegramMediaFloodMixin,
    TelegramSendPathRecoveryMixin,
    TelegramVaultCodeMixin,
    TelegramFloodCooldownMixin,
    BasePlatformAdapter,
):
    """Telegram bot adapter: the bundled adapter's transport and routing, with this plugin's answers."""
