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
from hermes_telegram.harvested.sdk_names import REBOUND

ROOT = Path(__file__).resolve().parents[1]
HARVESTED = ROOT / "hermes_telegram" / "harvested"
UPSTREAM_DIR = Path(upstream_adapter.__file__).parent
SIBLINGS = sorted(p.stem for p in UPSTREAM_DIR.glob("*.py") if p.stem not in {"__init__", "adapter"})
PIN = (ROOT / "HERMES_PIN").read_text().strip()
# The logger rewrite of scripts/harvest.py for adapter.py's code (ADAPTER_LOGGER), as an expression.
ADAPTER_LOGGER_ARG = ast.parse('__name__.rpartition(".harvested.")[0] + ".adapter"').body[0].value
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
    "send": "PreviewMixin",
    "send_draft": "PreviewMixin",
    "_send_prompt": "PreviewMixin",
    "_send_with_dm_topic_reply_anchor_retry": "PreviewMixin",
    "_register_handlers": "PreviewMixin",
    # Slice 3, hermes_telegram/updates.py: copies with one argument changed (test below).
    "_start_polling_once": "UpdateKindsMixin",
    "_start_webhook_mode": "UpdateKindsMixin",
}


class _Normalize(ast.NodeTransformer):
    """Apply the harvest's recorded rewrites to upstream code, so equal means harvested verbatim."""

    def __init__(self, owner: str = "", logger_arg: ast.expr = ADAPTER_LOGGER_ARG) -> None:
        self.owner = owner
        self.logger_arg = logger_arg

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:
        prefix = "plugins.platforms.telegram."
        if node.level == 0 and node.module and node.module.startswith(prefix):
            node.module, node.level = node.module[len(prefix):], 1
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id == "TelegramAdapter" and self.owner:
            node.id = self.owner
        return node

    def visit_Call(self, node: ast.Call) -> ast.Call:
        self.generic_visit(node)
        if (ast.unparse(node.func) == "logging.getLogger" and len(node.args) == 1
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "__name__"):
            node.args = [self.logger_arg]
        return node


def _dump(tree: ast.AST) -> str:
    return ast.dump(tree, include_attributes=False)


def _member_tree(obj: object) -> ast.AST:
    """A method's AST, parsed at its class indentation (dedent breaks on column-0 string lines)."""
    target = obj.fget if isinstance(obj, property) else getattr(obj, "__func__", obj)
    return ast.parse("if True:\n" + inspect.getsource(target)).body[0].body[0]


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
    assert [d for d in dev if d.startswith("hermes-agent")] == [
        f"hermes-agent[telegram] @ git+https://github.com/NousResearch/hermes-agent.git@{PIN}"]


def test_composition_keeps_the_bundled_bases_in_order():
    # Losing TelegramTopicRoutingMixin passes every member check yet drops forum-topic handling.
    tail = [k for k in HermesTelegramAdapter.__mro__ if k.__module__.startswith(("plugins.", "gateway."))
            or k in (upstream_adapter.TelegramHeldInboundMixin, upstream_adapter.TelegramTopicRoutingMixin)]
    names = [k.__name__ for k in HermesTelegramAdapter.__mro__]
    expected_tail = ["TelegramHeldInboundMixin", "TelegramTopicRoutingMixin", "BasePlatformAdapter", "ABC", "object"]
    assert names[-len(expected_tail):] == expected_tail
    assert tail == [BasePlatformAdapter]  # the two mixins are the harvested copies, not the bundled ones


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
        if ast.unparse(node) == "sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))":
            node = ast.Pass()  # the one statement the harvest replaces
        body.append(node)
    expected = _Normalize().visit(ast.Module(body=body, type_ignores=[]))
    ours = ast.parse((HARVESTED / "adapter_module.py").read_text())
    assert _dump(ours) == _dump(expected)


def test_rebound_sdk_names_are_the_ones_upstream_rebinds():
    source = inspect.getsource(upstream_adapter.check_telegram_requirements)
    declared = [n for line in re.findall(r"^\s*global (.+)$", source, re.M) for n in re.split(r",\s*", line)]
    assert list(REBOUND) == declared


@pytest.mark.parametrize("hook", ["_resolve_notifications_mode", "_is_connected", "_standalone_send",
                                  "interactive_setup", "_apply_yaml_config"])
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
