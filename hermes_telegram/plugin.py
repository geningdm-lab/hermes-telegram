"""Plugin registration: the bundled Telegram registration with this plugin's adapter.

``build_adapter`` and ``register`` follow ``_build_adapter`` and ``register`` of hermes-agent@c419cf7
``plugins/platforms/telegram/adapter.py`` (MIT, Nous Research), which the harvest drops because they name
the upstream class. Every other registration argument is the bundled one; checks/test_registration.py
holds this to the bundled registration.

Hermes defers only bundled platform plugins: an enabled user plugin registers in every process that
discovers plugins, the CLI included (hermes_cli/plugins_discovery.py:312). So this module imports nothing
that loads python-telegram-bot or the adapter. The hooks that live in the harvested adapter module are
registered as thin wrappers that import it on first call, which is when the bundled plugin's deferred
loader would import it too.
"""

import importlib
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.i18n import t

from .harvested import gateway_env
from .hint import PLATFORM_HINT
from .harvested.dashboard_card import DASHBOARD_CARD
from .harvested.telegram_ids import parse_send_target

VERSION = "0.1.0"
logger = logging.getLogger(__name__)

_ADAPTER_MODULE = __package__ + ".harvested.adapter_module"


def _adapter_module() -> Any:
    return importlib.import_module(_ADAPTER_MODULE)


def _harvested_hook(name: str, *, is_async: bool = False) -> Callable[..., Any]:
    """A wrapper for the harvested adapter module's ``name`` that imports the module on first call.

    ``__harvested__`` names the target, so checks/test_registration.py compares it with upstream's hook.
    """
    if is_async:
        async def hook(*args: Any, **kwargs: Any) -> Any:
            return await getattr(_adapter_module(), name)(*args, **kwargs)
    else:
        def hook(*args: Any, **kwargs: Any) -> Any:
            return getattr(_adapter_module(), name)(*args, **kwargs)
    hook.__name__ = hook.__qualname__ = name
    hook.__harvested__ = name  # type: ignore[attr-defined]
    return hook


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
        adapter._notifications_mode = _adapter_module()._resolve_notifications_mode()
    except Exception:  # Same fallback as upstream: an unreadable mode must not stop the platform.
        logger.debug("Telegram notifications mode unreadable; using 'important'", exc_info=True)
        adapter._notifications_mode = "important"
    # The line that says which adapter serves Telegram: docs/install.md tells operators to look for it.
    logger.info("hermes-telegram %s: Telegram adapter %s from %s", VERSION, type(adapter).__name__,
                Path(__file__).resolve().parent)
    return adapter


def registration_kwargs() -> dict[str, Any]:
    """The arguments this plugin passes to ``ctx.register_platform``."""
    return dict(
        name="telegram", label="Telegram", adapter_factory=build_adapter,
        check_fn=_harvested_hook("telegram_deps_present"),
        **DASHBOARD_CARD,
        ensure_deps_fn=ensure_telegram_requirements, is_connected=_harvested_hook("_is_connected"),
        required_env=["TELEGRAM_BOT_TOKEN"], install_hint=t("platform.telegram.install_hint"),
        setup_fn=_harvested_hook("interactive_setup"), apply_yaml_config_fn=_harvested_hook("_apply_yaml_config"),
        cron_deliver_env_var="TELEGRAM_HOME_CHANNEL",
        standalone_sender_fn=_harvested_hook("_standalone_send", is_async=True),
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
