"""Plugin registration: the bundled Telegram registration with this plugin's adapter.

``build_adapter`` and ``register`` follow ``_build_adapter`` and ``register`` of hermes-agent@c419cf7
``plugins/platforms/telegram/adapter.py`` (MIT, Nous Research), which the harvest drops because they name
the upstream class. Every other registration argument is the bundled one; checks/test_registration.py
holds this to the bundled registration.
"""

from typing import Any

from gateway.config import PlatformConfig

from .adapter import HermesTelegramAdapter
from .harvested import gateway_env
from .harvested.adapter_hint import PLATFORM_HINT
from .harvested.adapter_module import (
    _apply_yaml_config,
    _is_connected,
    _resolve_notifications_mode,
    _standalone_send,
    check_telegram_requirements,
    interactive_setup,
    logger,
    parse_send_target,
    t,
    telegram_deps_present,
)
from .harvested.dashboard_card import DASHBOARD_CARD


def build_adapter(config: PlatformConfig) -> HermesTelegramAdapter:
    """Construct the adapter and apply the notification mode, as the bundled factory does."""
    adapter = HermesTelegramAdapter(config)
    try:
        adapter._notifications_mode = _resolve_notifications_mode()
    except Exception:  # Same fallback as upstream: an unreadable mode must not stop the platform.
        logger.debug("Telegram notifications mode unreadable; using 'important'", exc_info=True)
        adapter._notifications_mode = "important"
    return adapter


def registration_kwargs() -> dict[str, Any]:
    """The arguments this plugin passes to ``ctx.register_platform``."""
    return dict(
        name="telegram", label="Telegram", adapter_factory=build_adapter, check_fn=telegram_deps_present,
        **DASHBOARD_CARD,
        ensure_deps_fn=check_telegram_requirements, is_connected=_is_connected,
        required_env=["TELEGRAM_BOT_TOKEN"], install_hint=t("platform.telegram.install_hint"),
        setup_fn=interactive_setup, apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="TELEGRAM_HOME_CHANNEL", standalone_sender_fn=_standalone_send,
        max_message_length=4096, emoji="✈️", allow_update_command=True,
        platform_hint=PLATFORM_HINT,
        display_tier="high",
        display_overrides={"tool_progress": "off", "busy_ack_detail": False},
        target_ref_syntax_fn=parse_send_target,
        env_overrides=gateway_env.ENV_OVERRIDES,
        env_enable_credentials=gateway_env.ENV_ENABLE_CREDENTIALS, **gateway_env.AUTHZ,
    )


def register(ctx: Any) -> None:
    """Plugin entry point, called by the Hermes plugin system."""
    ctx.register_platform(**registration_kwargs())
