"""Harvested code is the installed Hermes' Telegram adapter, apart from the rewrites the harvest records.

Catches: a hand edit inside harvested/, a harvest bug that drops or alters a statement, and a test
environment whose Hermes is not the commit harvested/PIN names.
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest

import plugins.platforms.telegram.adapter as upstream_adapter
from hermes_telegram.adapter import HermesTelegramAdapter

HARVESTED = Path(__file__).resolve().parents[1] / "hermes_telegram" / "harvested"
UPSTREAM_DIR = Path(upstream_adapter.__file__).parent
SIBLINGS = sorted(p.stem for p in UPSTREAM_DIR.glob("*.py") if p.stem not in {"__init__", "adapter"})


class _Normalize(ast.NodeTransformer):
    """Undo the harvest's recorded rewrites on upstream code, so equal means harvested verbatim."""

    def __init__(self, owner: str = "") -> None:
        self.owner = owner

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:
        prefix = "plugins.platforms.telegram."
        if node.level == 0 and node.module and node.module.startswith(prefix):
            node.module, node.level = node.module[len(prefix):], 1
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id == "TelegramAdapter" and self.owner:
            node.id = self.owner
        return node


def _dump(tree: ast.AST) -> str:
    return ast.dump(tree, include_attributes=False)


def _member_tree(obj: object) -> ast.AST:
    """A method's AST, parsed at its class indentation (dedent breaks on column-0 string lines)."""
    target = obj.fget if isinstance(obj, property) else getattr(obj, "__func__", obj)
    return ast.parse("if True:\n" + inspect.getsource(target)).body[0].body[0]


def test_installed_hermes_is_the_harvested_commit():
    # A drifted venv would make every other check here compare against the wrong upstream.
    pin = (HARVESTED / "PIN").read_text().strip()
    origin = importlib.metadata.distribution("hermes-agent").read_text("direct_url.json") or ""
    assert pin in origin


@pytest.mark.parametrize("name", sorted(vars(upstream_adapter.TelegramAdapter)))
def test_class_member_is_harvested_verbatim(name: str):
    theirs = vars(upstream_adapter.TelegramAdapter)[name]
    is_code = inspect.isfunction(theirs) or isinstance(theirs, (property, staticmethod, classmethod))
    machinery = {"_abc_impl", "__annotate_func__", "__annotate__", "__annotations_cache__"}
    if name.startswith("__") and name.endswith("__") and not is_code or name in machinery:
        return  # class machinery Python sets per class, not harvested code
    owner = next(k for k in HermesTelegramAdapter.__mro__ if name in vars(k))
    ours = vars(owner)[name]
    assert owner.__module__.startswith("hermes_telegram.harvested."), f"{name} is defined by {owner}"
    if is_code:
        expected = _Normalize(owner.__name__).visit(_member_tree(theirs))
        assert _dump(_member_tree(ours)) == _dump(expected)
    else:
        assert ours == theirs


@pytest.mark.parametrize("module", SIBLINGS)
def test_sibling_module_is_harvested_verbatim(module: str):
    theirs = _Normalize().visit(ast.parse((UPSTREAM_DIR / f"{module}.py").read_text()))
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
