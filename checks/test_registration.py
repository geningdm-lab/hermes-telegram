"""The plugin registers the platform exactly as the bundled Telegram plugin does, with its own adapter.

Catches: a registration argument dropped or changed while copying ``register``, which would change
authorization, cron delivery, setup or display for the replaced platform.
"""

import ast
import dataclasses
import functools
import inspect
import textwrap
from typing import Any

import plugins.platforms.telegram.adapter as upstream_adapter
from hermes_telegram import register
from hermes_telegram.adapter import HermesTelegramAdapter


class _Ctx:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def register_platform(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _registered(register_fn) -> dict[str, Any]:
    ctx = _Ctx()
    register_fn(ctx)
    return ctx.kwargs


def _code(fn: Any) -> str:
    return ast.dump(ast.parse(textwrap.dedent(inspect.getsource(fn))))


def _same(ours: Any, theirs: Any) -> bool:
    """Equal values; functions compare by name and code, since harvested copies are new objects."""
    if inspect.isfunction(ours) and inspect.isfunction(theirs):
        return ours.__qualname__ == theirs.__qualname__ and _code(ours) == _code(theirs)
    if isinstance(ours, functools.partial) and isinstance(theirs, functools.partial):
        return _same(ours.func, theirs.func) and _same(ours.args, theirs.args) and _same(
            ours.keywords, theirs.keywords)
    if dataclasses.is_dataclass(ours) and not isinstance(ours, type):
        return type(ours).__qualname__ == type(theirs).__qualname__ and all(
            _same(getattr(ours, f.name), getattr(theirs, f.name)) for f in dataclasses.fields(ours))
    if isinstance(ours, dict) and isinstance(theirs, dict):
        return ours.keys() == theirs.keys() and all(_same(ours[k], theirs[k]) for k in ours)
    if isinstance(ours, (list, tuple)) and isinstance(theirs, (list, tuple)):
        return type(ours) is type(theirs) and len(ours) == len(theirs) and all(
            _same(a, b) for a, b in zip(ours, theirs))
    return ours == theirs


def test_registration_matches_bundled_except_the_adapter_factory():
    ours, theirs = _registered(register), _registered(upstream_adapter.register)
    assert ours.keys() == theirs.keys()
    differing = sorted(k for k in ours if k != "adapter_factory" and not _same(ours[k], theirs[k]))
    assert differing == []


def test_adapter_factory_builds_this_plugins_adapter_with_the_notification_mode(monkeypatch):
    from gateway.config import PlatformConfig

    monkeypatch.setenv("HERMES_TELEGRAM_NOTIFICATIONS", "all")
    adapter = _registered(register)["adapter_factory"](PlatformConfig(enabled=True, token="1:x"))
    assert type(adapter) is HermesTelegramAdapter
    assert adapter._notifications_mode == "all"
