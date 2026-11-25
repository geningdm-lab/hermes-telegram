"""The plugin registers the platform exactly as the bundled Telegram plugin does, with its own adapter.

Catches: a registration argument dropped or changed while copying ``register``, which would change
authorization, cron delivery, setup or display for the replaced platform; a registration that imports the
SDK in every process; and harvested modules left with stale SDK names after a lazy install.
"""

import ast
import dataclasses
import functools
import inspect
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import plugins.platforms.telegram.adapter as upstream_adapter
from hermes_telegram import register
from hermes_telegram.adapter import HermesTelegramAdapter
from hermes_telegram.harvested import adapter_module, adapter_transport

ROOT = Path(__file__).resolve().parents[1]
# Arguments this plugin sets on purpose to something else than the bundled registration.
OWN = {
    "adapter_factory": "builds HermesTelegramAdapter (checked below)",
    "ensure_deps_fn": "also copies rebound SDK names into the harvested modules (checked below)",
    "platform_hint": "describes rich answers instead of forbidding tables (slice 1, checked below)",
}


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
    if callable(ours) and hasattr(ours, "__harvested__"):
        ours = getattr(adapter_module, ours.__harvested__)  # a lazy hook stands for this harvested function
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


def test_registration_matches_bundled_except_this_plugins_own_arguments():
    ours, theirs = _registered(register), _registered(upstream_adapter.register)
    assert ours.keys() == theirs.keys()
    differing = sorted(k for k in ours if k not in OWN and not _same(ours[k], theirs[k]))
    assert differing == []


def test_adapter_factory_builds_this_plugins_adapter_with_the_notification_mode(monkeypatch):
    from gateway.config import PlatformConfig

    monkeypatch.setenv("HERMES_TELEGRAM_NOTIFICATIONS", "all")
    adapter = _registered(register)["adapter_factory"](PlatformConfig(enabled=True, token="1:x"))
    assert type(adapter) is HermesTelegramAdapter
    assert adapter._notifications_mode == "all"


def test_lazy_install_reaches_the_mixins_that_use_the_sdk(monkeypatch):
    # Hermes calls ensure_deps_fn when the SDK was missing at import: check_telegram_requirements() then
    # rebinds the SDK names in adapter_module only, and connect() reads TELEGRAM_AVAILABLE in a mixin.
    import pm
    import telegram

    for module in (adapter_module, adapter_transport):
        monkeypatch.setattr(module, "TELEGRAM_AVAILABLE", False)
        monkeypatch.setattr(module, "Update", Any)
    monkeypatch.setattr(pm, "ensure_import", lambda name: None)  # "installed" already
    assert _registered(register)["ensure_deps_fn"]() is True
    assert adapter_transport.TELEGRAM_AVAILABLE is True
    assert adapter_transport.Update is telegram.Update


def test_registration_imports_neither_the_sdk_nor_the_adapter(tmp_path: Path):
    # An enabled user plugin registers in every process that discovers plugins, the CLI included.
    probe = textwrap.dedent("""
        import json, sys
        import hermes_telegram
        class Ctx:
            def register_platform(self, **kwargs): pass
        hermes_telegram.register(Ctx())
        heavy = ("telegram", "hermes_telegram.adapter")
        print(json.dumps(sorted(m for m in sys.modules if m in heavy or (
            m.startswith("hermes_telegram.harvested.adapter_") and m != "hermes_telegram.harvested.adapter_hint"))))
    """)
    env = {**{k: v for k, v in os.environ.items()}, "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "h"),
           "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", probe], env=env, cwd=tmp_path, capture_output=True, text=True,
                         timeout=120, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_hint_allows_tables_and_keeps_the_bundled_media_instructions():
    # The bundled hint says "no tables", which would keep row 2's table from ever being written.
    hint = _registered(register)["platform_hint"]
    bundled = _registered(upstream_adapter.register)["platform_hint"]
    assert "no tables" in bundled and "no tables" not in hint
    media = bundled[bundled.index("You can send files natively"):]
    assert hint.endswith(media)
