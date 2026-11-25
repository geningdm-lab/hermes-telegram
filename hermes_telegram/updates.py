"""The update kinds this plugin asks Telegram for (docs/design.md, Turn lifecycle; slice 3).

The harvested polling and webhook start ask for ``Update.ALL_TYPES``, python-telegram-bot 22.8's list,
which predates Bot API 10.3. Telegram delivers only the kinds a bot asks for (getUpdates and setWebhook,
``allowed_updates``), so the Stop button's ``stopped_message_generation`` would never arrive.

The two methods below are the harvested ones with that one argument changed; checks/test_harvest.py holds
each equal to the harvested member apart from ``Update.ALL_TYPES`` -> ``allowed_updates()``, so an upstream
change to either method fails the check instead of being lost here.
"""

from .harvested import adapter_module
from .harvested.adapter_transport import (
    _POLLING_GENERATION_CONTEXT,
    _PollingLifecycleAbort,
    _UPDATER_START_TIMEOUT,
    _await_with_thread_deadline,
    _get_scoped_secret,
    _shutdown_abandoned_app,
    asyncio,
    env_int,
    logger,
    os,
)

# Bot API kinds python-telegram-bot 22.8 does not list, that this plugin handles. Slice 5 adds widget_query.
EXTRA_UPDATE_KINDS = ("stopped_message_generation",)


def allowed_updates() -> list[str]:
    """PTB's list, read when polling starts (the SDK may be installed lazily), plus ``EXTRA_UPDATE_KINDS``."""
    return [*(str(kind) for kind in adapter_module.Update.ALL_TYPES), *EXTRA_UPDATE_KINDS]


class UpdateKindsMixin:
    """Listed before the harvested mixins; the two overrides are in checks/test_harvest.py's table."""

    async def _start_polling_once(
        self, app, *, drop_pending_updates: bool, error_callback, abandon_app_on_timeout: bool = False,
        schedule_verifier: bool = True) -> tuple[int, asyncio.Event]:
        """Start one generation and verify real getUpdates progress. Returns this generation's
        ``(generation, progress_event)`` so readiness-gating callers bind to exactly it."""
        if self._teardown_started:
            raise _PollingLifecycleAbort("Telegram polling teardown started")
        generation, progress = self._begin_polling_generation()
        if not self._polling_progress_accepting:
            raise _PollingLifecycleAbort("Telegram polling teardown started")

        def _generation_error_callback(error: Exception) -> None:
            if self._teardown_started or generation != self._polling_generation or error_callback is None:
                return
            callback_context_token = _POLLING_GENERATION_CONTEXT.set(None)
            try:
                error_callback(error)
            finally:
                _POLLING_GENERATION_CONTEXT.reset(callback_context_token)

        context_token = _POLLING_GENERATION_CONTEXT.set(generation)
        try:
            # asyncio.wait_for can wait forever on httpcore/AnyIO shielded scopes; use the wall-deadline
            # helper and abandon the partial updater (caller rebuilds).
            await _await_with_thread_deadline(
                app.updater.start_polling(
                    allowed_updates=allowed_updates(), drop_pending_updates=drop_pending_updates, error_callback=_generation_error_callback),
                timeout=_UPDATER_START_TIMEOUT,
                on_abandon=((lambda app=app: _shutdown_abandoned_app(app)) if abandon_app_on_timeout else None))
        finally:
            _POLLING_GENERATION_CONTEXT.reset(context_token)
        if self._teardown_started:
            self._fence_polling()
            raise _PollingLifecycleAbort("Telegram polling teardown started")
        if schedule_verifier:
            self._schedule_polling_progress_verifier(generation, progress)
        return generation, progress

    async def _start_webhook_mode(self, webhook_url: str, *, is_reconnect: bool) -> None:
        """Start PTB's webhook server (Telegram pushes updates; lets cloud platforms auto-wake suspended
        machines). SECURITY: TELEGRAM_WEBHOOK_SECRET is REQUIRED — without it the endpoint accepts forged
        updates (GHSA-3vpc-7q5r-276h); refuse to start rather than run fail-open."""
        webhook_port = env_int("TELEGRAM_WEBHOOK_PORT", 8443)
        # Default "" → tornado listens on IPv4 + IPv6; "0.0.0.0" is unreachable on IPv6-only networks.
        webhook_host = (os.getenv("TELEGRAM_WEBHOOK_HOST", "").strip() or str((self.config.extra or {}).get("webhook_host") or "").strip())
        webhook_secret = (_get_scoped_secret("TELEGRAM_WEBHOOK_SECRET") or "").strip()
        if not webhook_secret:
            raise RuntimeError(
                "TELEGRAM_WEBHOOK_SECRET is required when TELEGRAM_WEBHOOK_URL is set. Without it, the "
                "webhook endpoint accepts forged updates from anyone who can reach it — see "
                "https://github.com/NousResearch/hermes-agent/security/advisories/GHSA-3vpc-7q5r-276h.\n\n"
                "Generate a secret and set it in your .env:\n  export TELEGRAM_WEBHOOK_SECRET=\"$(openssl rand -hex 32)\"\n\n"
                "Then register it with Telegram when setting the webhook via setWebhook's secret_token parameter.")
        from urllib.parse import urlparse
        webhook_path = urlparse(webhook_url).path or "/telegram"
        await self._app.updater.start_webhook(
            listen=webhook_host, port=webhook_port, url_path=webhook_path, webhook_url=webhook_url,
            secret_token=webhook_secret, allowed_updates=allowed_updates(),
            drop_pending_updates=self._cold_boot_drop_pending(is_reconnect=is_reconnect),
       )
        self._webhook_mode = True
        self._polling_progress_accepting = False
        self._send_path_degraded = False
        logger.info(
            "[%s] Webhook server listening on %s:%d%s", self.name, webhook_host or "* (all interfaces, IPv4+IPv6)",
            webhook_port, webhook_path)
