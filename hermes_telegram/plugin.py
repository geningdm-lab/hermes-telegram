"""Plugin registration: the bundled Telegram registration with this plugin's adapter.

``build_adapter`` and ``register`` follow ``_build_adapter`` and ``register`` of hermes-agent@c419cf7
``plugins/platforms/telegram/adapter.py`` (MIT, Nous Research), which the harvest drops because they name
the upstream class. checks/test_registration.py holds every argument to the bundled registration except
those in its ``OWN`` table.

Hermes defers only bundled platform plugins: an enabled user plugin registers in every process that
discovers plugins, the CLI included (hermes_cli/plugins_discovery.py:312), and its hooks run on every
config load (gateway/config_env.py:206). So nothing here imports python-telegram-bot or the adapter
until a gateway builds one. The hooks Hermes calls outside the gateway are the bundled functions,
harvested into ``harvested/registration.py`` with only the light imports they need.
"""

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from agent.i18n import t

from .harvested import gateway_env
from .hint import PLATFORM_HINT
from .harvested.dashboard_card import DASHBOARD_CARD
from .harvested.registration import (
    _apply_yaml_config,
    _is_connected,
    _resolve_notifications_mode,
    _standalone_send,
    interactive_setup,
)
from .harvested.telegram_ids import parse_send_target

VERSION = "0.1.0"
logger = logging.getLogger(__name__)

_ADAPTER_MODULE = __package__ + ".harvested.adapter_module"


def _adapter_module() -> Any:
    return importlib.import_module(_ADAPTER_MODULE)


def telegram_deps_present() -> bool:
    """PASSIVE ``check_fn``: is python-telegram-bot importable? Never installs, never imports the SDK.

    The bundled probe returns the adapter module's ``TELEGRAM_AVAILABLE``, which costs importing the adapter
    and the SDK; for a deferred bundled plugin that import has already happened when the probe runs. Here
    the adapter module's answer is used once it is loaded; before that, whether the package can be found.
    """
    module = sys.modules.get(_ADAPTER_MODULE)
    if module is not None:
        return bool(module.TELEGRAM_AVAILABLE)
    return importlib.util.find_spec("telegram") is not None


def propagate_sdk_names() -> None:
    """Copy the SDK names the adapter module rebound into the harvested modules that imported them.

    Upstream, ``check_telegram_requirements()`` lazily installs python-telegram-bot and rebinds the SDK
    names in the one module that uses them. The harvested mixin modules imported those names' values, so
    after a rebinding they would keep the stand-ins and ``connect()`` would refuse to start.
    """
    from .harvested.sdk_names import REBOUND

    module = sys.modules.get(_ADAPTER_MODULE)
    if module is None:
        return  # nothing harvested is imported yet, so nothing holds a stale value
    prefix = __package__ + ".harvested."
    for name, mod in list(sys.modules.items()):
        if mod is None or mod is module or not name.startswith(prefix):
            continue
        for sdk_name in REBOUND:
            if sdk_name in vars(mod):
                setattr(mod, sdk_name, getattr(module, sdk_name))


def ensure_telegram_requirements() -> bool:
    """The bundled dependency check (which may lazily install the SDK), then the rebound names copied."""
    ok = bool(_adapter_module().check_telegram_requirements())
    if ok:
        propagate_sdk_names()
    return ok


def build_adapter(config: Any) -> Any:
    """Construct the adapter and apply the notification mode, as the bundled factory does."""
    from .adapter import HermesTelegramAdapter

    propagate_sdk_names()
    adapter = HermesTelegramAdapter(config)
    try:
        adapter._notifications_mode = _resolve_notifications_mode()
    except Exception:  # Same fallback as upstream: an unreadable mode must not stop the platform.
        logger.debug("Telegram notifications mode unreadable; using 'important'", exc_info=True)
        adapter._notifications_mode = "important"
    # Which adapter serves Telegram; docs/install.md tells operators to look for this line.
    logger.info("hermes-telegram %s: Telegram adapter %s from %s", VERSION, type(adapter).__name__,
                Path(__file__).resolve().parent)
    return adapter


def registration_kwargs() -> dict[str, Any]:
    """The arguments this plugin passes to ``ctx.register_platform``."""
    return dict(
        name="telegram", label="Telegram", adapter_factory=build_adapter, check_fn=telegram_deps_present,
        **DASHBOARD_CARD,
        ensure_deps_fn=ensure_telegram_requirements, is_connected=_is_connected,
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
    """Plugin entry point, called by the Hermes plugin system: the platform, then the agent's tools."""
    from .tools import register_tools

    ctx.register_platform(**registration_kwargs())
    register_tools(ctx)
