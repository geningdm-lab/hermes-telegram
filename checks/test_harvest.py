"""Harvested code is the installed Hermes' Telegram adapter, apart from the rewrites the harvest records.

Catches: a hand edit inside harvested/, a harvest bug that drops or alters a statement, a composition that
loses a base class or adds a member, and a test environment whose Hermes is not the pinned commit.
"""

import ast
import importlib
import inspect
import re
import tomllib
from pathlib import Path

import pytest

import plugins.platforms.telegram.adapter as upstream_adapter
from gateway.platforms.base import BasePlatformAdapter
from hermes_telegram.adapter import HermesTelegramAdapter
from checks.harvest_rewrites import Normalize as _Normalize
from hermes_telegram.harvested.sdk_names import REBOUND

ROOT = Path(__file__).resolve().parents[1]
HARVESTED = ROOT / "hermes_telegram" / "harvested"
UPSTREAM_DIR = Path(upstream_adapter.__file__).parent
SIBLINGS = sorted(p.stem for p in UPSTREAM_DIR.glob("*.py") if p.stem not in {"__init__", "adapter"})
PIN = (ROOT / "HERMES_PIN").read_text().strip()
MACHINERY = {"_abc_impl", "__annotate_func__", "__annotate__", "__annotations_cache__"}
# Members this plugin overrides on purpose, with the class that does it. Any other member of the composed
# adapter must be the harvested one.
INTENDED_OVERRIDES: dict[str, str] = {
    # Slice 1, docs/design.md "How slice 1 does it".
    "__init__": "RichAnswersMixin",
    "_needs_rich_rendering": "RichAnswersMixin",
    "_rich_message_payload": "RichAnswersMixin",
    "_content_fits_rich_limits": "RichAnswersMixin",
    "format_message": "RichAnswersMixin",
    "_rich_transient_result": "RichAnswersMixin",
    "_send_text_locked": "RichAnswersMixin",
    "_resume_partial_send": "RichAnswersMixin",
    "_send_chunk_markdown_or_plain": "RichAnswersMixin",  # slice 2 review: fallback chunks, timedelta waits
    "_send_retry_is_final": "RichAnswersMixin",  # a part that may have been shown is not sent again
    # Slice 2, hermes_telegram/files.py.
    "_send_local_file": "FilesMixin",
    "send_document": "FilesMixin",
    "send_image_file": "FilesMixin",
    "send_multiple_images": "FilesMixin",
    "send_video": "FilesMixin",
    "send_voice": "FilesMixin",
    "_send_media": "FilesMixin",
    # Slice 3, hermes_telegram/preview.py (the hooks chain on to HumanTurnsMixin's).
    "on_processing_start": "PreviewMixin",
    "on_processing_complete": "PreviewMixin",
    "send": "ActionsMixin",  # slice 4 remembers the bot's messages, then slice 3's preview
    "send_draft": "PreviewMixin",
    "_send_prompt": "PreviewMixin",
    "_send_with_dm_topic_reply_anchor_retry": "ActionsMixin",
    # The Business inbox's handler (docs/business-inbox.md), then slice 5's, slice 4's and slice 3's.
    "_register_handlers": "BusinessInboxMixin",
    # Slice 4, hermes_telegram/buttons.py and actions.py.
    "_handle_callback_query": "ButtonsMixin",
    "_handle_sticker": "ActionsMixin",
    "_set_reaction": "ActionsMixin",  # the agent's reaction stays
    "_send_control_message": "ActionsMixin",  # prompts are bot messages too
    "_with_partial_send": "ActionsMixin",  # every delivered id of a partial send
    "_try_send_rich": "ActionsMixin",  # a text that went rich is remembered as rich (Hermes indexes all texts)
    "_try_edit_rich": "ActionsMixin",  # and one a streamed answer's final edit made rich
    # Slice 4 review, hermes_telegram/turn.py: a tool's send keeps the turn's topic.
    "_is_thread_not_found_error": "HumanTurnsMixin",
    # Slice 3, hermes_telegram/updates.py: copies with one argument changed (test below).
    "_start_polling_once": "UpdateKindsMixin",
    "_start_webhook_mode": "UpdateKindsMixin",
}


def _dump(tree: ast.AST) -> str:
    return ast.dump(tree, include_attributes=False)


def _member_tree(obj: object) -> ast.AST:
    """A method's AST, parsed at its class indentation (dedent breaks on column-0 string lines)."""
    target = obj.fget if isinstance(obj, property) else getattr(obj, "__func__", obj)
    source = inspect.getsource(target)
    if not source[:1].isspace():  # a module-level function bound in the class body: _x = staticmethod(f)
        return ast.parse(source).body[0]
    return ast.parse("if True:\n" + source).body[0].body[0]


def _is_code(value: object) -> bool:
    return inspect.isfunction(value) or isinstance(value, (property, staticmethod, classmethod))


def _harvested_members() -> dict[str, type]:
    """Member name -> the harvested mixin that defines it."""
    out: dict[str, type] = {}
    for klass in HermesTelegramAdapter.__mro__:
        if klass.__module__.startswith("hermes_telegram.harvested.adapter_"):
            for name, value in vars(klass).items():
                if not (name.startswith("__") and name.endswith("__") and not _is_code(value)):
                    assert name not in out, f"{name} defined by two mixins"
                    out[name] = klass
    return out


def test_pin_is_one_commit_everywhere():
    # A drifted venv or a stale generated tree would make every other check compare the wrong upstream.
    assert (HARVESTED / "PIN").read_text().strip() == PIN
    origin = importlib.metadata.distribution("hermes-agent").read_text("direct_url.json") or ""
    assert PIN in origin
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    (hermes,) = [d for d in dev if d.startswith("hermes-agent")]  # its extras are the environment's, not the pin
    assert hermes.endswith(f" @ git+https://github.com/NousResearch/hermes-agent.git@{PIN}"), hermes


def _upstream_bases() -> list[type]:
    return list(upstream_adapter.TelegramAdapter.__bases__)


def test_composition_keeps_the_bundled_bases_in_order():
    # Losing TelegramTopicRoutingMixin passes every member check yet drops forum-topic handling.
    ours = [k for k in HermesTelegramAdapter.__mro__ if k.__name__ in {b.__name__ for b in _upstream_bases()}]
    assert [k.__name__ for k in ours] == [b.__name__ for b in _upstream_bases()]
    for klass in ours[:-1]:  # every base but BasePlatformAdapter is the harvested copy, not the bundled one
        assert klass.__module__.startswith("hermes_telegram.harvested."), klass
    assert ours[-1] is BasePlatformAdapter
    assert HermesTelegramAdapter.__mro__[-len(BasePlatformAdapter.__mro__):] == BasePlatformAdapter.__mro__


def test_harvested_members_are_exactly_upstreams():
    upstream = {name for name, value in vars(upstream_adapter.TelegramAdapter).items()
                if not (name.startswith("__") and name.endswith("__") and not _is_code(value))} - MACHINERY
    assert set(_harvested_members()) - MACHINERY == upstream


@pytest.mark.parametrize("name", sorted(n for n, v in vars(upstream_adapter.TelegramAdapter).items()
                                        if not (n.startswith("__") and n.endswith("__") and not _is_code(v))
                                        and n not in MACHINERY))
def test_class_member_is_harvested_verbatim(name: str):
    theirs = vars(upstream_adapter.TelegramAdapter)[name]
    owner = _harvested_members()[name]
    ours = vars(owner)[name]
    if _is_code(theirs):
        expected = _Normalize(owner.__name__).visit(_member_tree(theirs))
        assert _dump(_member_tree(ours)) == _dump(expected)
        theirs_fn, ours_fn = (getattr(v, "__func__", v) for v in (theirs, ours))
        if inspect.isfunction(theirs_fn) and "." not in theirs_fn.__qualname__:  # bound from a sibling module
            sibling = theirs_fn.__module__.rpartition(".")[2]
            assert ours_fn.__module__ == f"hermes_telegram.harvested.{sibling}"
    else:
        assert ours == theirs


@pytest.mark.parametrize("name", sorted(n for n, v in vars(upstream_adapter.TelegramAdapter).items()
                                        if not (n.startswith("__") and n.endswith("__") and not _is_code(v))
                                        and n not in MACHINERY))
def test_the_composed_adapter_runs_the_harvested_member_or_an_intended_override(name: str):
    # A class earlier in the MRO can shadow a harvested member; only the overrides listed are allowed.
    effective = next(k for k in HermesTelegramAdapter.__mro__ if name in vars(k))
    if name in INTENDED_OVERRIDES:
        assert effective.__name__ == INTENDED_OVERRIDES[name]
    else:
        assert effective is _harvested_members()[name]


def _base_members() -> list[str]:
    """Members the upstream class takes from its bases in sibling modules, not defined on the class itself."""
    own = set(vars(upstream_adapter.TelegramAdapter))
    return sorted({n for base in _upstream_bases()[:-1] for n, v in vars(base).items()
                   if n not in own and not (n.startswith("__") and n.endswith("__") and not _is_code(v))
                   and n not in MACHINERY})


@pytest.mark.parametrize("name", _base_members())
def test_the_composed_adapter_runs_the_harvested_base_member_or_an_intended_override(name: str):
    # Upstream moved send paths (_send_media, _send_local_file, flood windows) into its base mixins; an override
    # of one of those must be listed as well.
    upstream_owner = next(k for k in upstream_adapter.TelegramAdapter.__mro__ if name in vars(k))
    effective = next(k for k in HermesTelegramAdapter.__mro__ if name in vars(k))
    if name in INTENDED_OVERRIDES:
        assert effective.__name__ == INTENDED_OVERRIDES[name]
    else:
        assert effective.__name__ == upstream_owner.__name__
        assert effective.__module__.startswith("hermes_telegram.harvested.")


@pytest.mark.parametrize("module", SIBLINGS)
def test_sibling_module_is_harvested_verbatim(module: str):
    logger_arg = ast.Constant(f"plugins.platforms.telegram.{module}")
    theirs = _Normalize(logger_arg=logger_arg).visit(ast.parse((UPSTREAM_DIR / f"{module}.py").read_text()))
    ours = ast.parse((HARVESTED / f"{module}.py").read_text())
    assert _dump(ours) == _dump(theirs)


def test_adapter_module_level_code_is_harvested_verbatim():
    tree = ast.parse((UPSTREAM_DIR / "adapter.py").read_text())
    dropped = {"TelegramAdapter", "_build_adapter", "register"}
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in dropped:
            continue
        body.append(node)
    expected = _Normalize().visit(ast.Module(body=body, type_ignores=[]))
    ours = ast.parse((HARVESTED / "adapter_module.py").read_text())
    assert _dump(ours) == _dump(expected)


def test_rebound_sdk_names_are_the_ones_upstream_rebinds():
    source = inspect.getsource(upstream_adapter.check_telegram_requirements)
    declared = [n for line in re.findall(r"^\s*global (.+)$", source, re.M) for n in re.split(r",\s*", line)]
    assert list(REBOUND) == declared


@pytest.mark.parametrize("hook", ["_is_connected", "interactive_setup", "_apply_yaml_config"])
def test_registration_hook_is_harvested_verbatim(hook: str):
    from hermes_telegram.harvested import registration

    expected = _Normalize().visit(ast.parse(inspect.getsource(getattr(upstream_adapter, hook))))
    assert _dump(ast.parse(inspect.getsource(getattr(registration, hook)))) == _dump(expected)


@pytest.mark.parametrize("module", [*SIBLINGS, "adapter"])
def test_module_loggers_keep_their_upstream_names(module: str):
    # Operators filter logs by these names. Hermes loads adapter.py as hermes_plugins.platforms__telegram.
    # adapter, which in this process is hermes_telegram.adapter; siblings keep their absolute names.
    import importlib as _importlib

    ours = _importlib.import_module(f"hermes_telegram.harvested.{'adapter_module' if module == 'adapter' else module}")
    theirs = _importlib.import_module(f"plugins.platforms.telegram.{module}")
    if not hasattr(theirs, "logger"):
        assert not hasattr(ours, "logger")
    elif module == "adapter":
        assert ours.logger.name == "hermes_telegram.adapter"
    else:
        assert ours.logger.name == theirs.logger.name


class _WidenUpdates(ast.NodeTransformer):
    """The one change hermes_telegram/updates.py makes: ``Update.ALL_TYPES`` -> ``allowed_updates()``."""

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if isinstance(node.value, ast.Name) and node.value.id == "Update" and node.attr == "ALL_TYPES":
            return ast.Call(func=ast.Name("allowed_updates", ast.Load()), args=[], keywords=[])
        return self.generic_visit(node)


@pytest.mark.parametrize("name", ["_start_polling_once", "_start_webhook_mode"])
def test_the_update_kinds_overrides_differ_from_the_harvested_methods_only_in_the_update_list(name: str):
    # An upstream change to either method changes the harvested copy, and this fails until updates.py follows.
    from hermes_telegram.updates import UpdateKindsMixin

    harvested = _WidenUpdates().visit(_member_tree(vars(_harvested_members()[name])[name]))
    assert _dump(_member_tree(vars(UpdateKindsMixin)[name])) == _dump(ast.fix_missing_locations(harvested))
