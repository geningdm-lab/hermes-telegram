"""The rewrites scripts/harvest.py records, applied to upstream code, so equal means harvested verbatim."""

import ast

# The logger rewrite of scripts/harvest.py for adapter.py's code (ADAPTER_LOGGER), as an expression.
ADAPTER_LOGGER_ARG = ast.parse('__name__.rpartition(".harvested.")[0] + ".adapter"').body[0].value


class Normalize(ast.NodeTransformer):
    """Apply the harvest's recorded rewrites to upstream code, so equal means harvested verbatim."""

    def __init__(self, owner: str = "", logger_arg: ast.expr = ADAPTER_LOGGER_ARG) -> None:
        self.owner = owner
        self.logger_arg = logger_arg

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom | list[ast.ImportFrom]:
        package = "plugins.platforms.telegram"
        if node.level or not node.module or not (node.module == package or node.module.startswith(package + ".")):
            return node
        if node.module == package:  # from . import x; adapter.py's module-level code is adapter_module
            names = [ast.alias("adapter_module", a.asname or "adapter") if a.name == "adapter" else a
                     for a in node.names]
            return ast.ImportFrom(module=None, names=names, level=1)
        if node.module == package + ".adapter":  # its names from adapter_module, its class composed
            plain = [a for a in node.names if a.name != "TelegramAdapter"]
            out = [ast.ImportFrom(module="adapter_module", names=plain, level=1)] if plain else []
            return out + [ast.ImportFrom(module="adapter", level=2, names=[
                ast.alias("HermesTelegramAdapter", a.asname or "TelegramAdapter")])
                for a in node.names if a.name == "TelegramAdapter"]
        node.module, node.level = node.module[len(package) + 1:], 1
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
