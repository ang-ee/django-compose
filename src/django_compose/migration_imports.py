"""Read retained Python dependencies without importing migration or helper code."""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.conf import settings

from django_compose.sources import SourceResolver


@dataclass(frozen=True)
class HistoricalImport:
    """One import edge, classified from its resolved file rather than package name."""

    path: Path
    module: str
    distributions: tuple[str, ...] = ()
    kind: str = "unresolved"
    detail: str = ""


def _distribution_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class MigrationImportReport:
    """Audit selected native migration sources and local helper bodies offline.

    Distribution source files are leaves: installed distributions own their
    dependencies. Project and enabled-app helpers are recursively inspected,
    including imports inside functions which a RunPython operation may call.
    """

    def __init__(self) -> None:
        self.resolver = SourceResolver(apps.get_app_configs())
        self.items: list[HistoricalImport] = []
        self._visited: set[Path] = set()
        snapshot = getattr(settings, "COMPOSE_SNAPSHOT", None)
        self._transition_packages = {
            record["module"].rpartition(".")[0] for record in getattr(snapshot, "transition_records", ())
        }

    def _record(self, path: Path, module: str) -> None:
        source = self.resolver.resolve(module)
        self.items.append(HistoricalImport(path, module, source.distributions, source.kind))
        if (
            source.path is not None
            and source.path.is_file()
            and (
                source.kind in {"app", "project"}
                or any(
                    module == package or module.startswith(package + ".")
                    for package in self._transition_packages
                )
            )
        ):
            self._scan(source.path, module)
        # Importing a child also executes its parent packages. Inspect local
        # package initializers even when the child itself belongs to a dist.
        for index in range(1, len(module.split("."))):
            parent = ".".join(module.split(".")[:index])
            parent_source = self.resolver.resolve(parent)
            self.items.append(HistoricalImport(path, parent, parent_source.distributions, parent_source.kind))
            if (
                parent_source.path is not None
                and parent_source.path.is_file()
                and (parent_source.kind in {"app", "project"} or parent in self._transition_packages)
            ):
                self._scan(parent_source.path, parent)

    def _unsupported(self, path: Path, node: ast.AST, detail: str) -> None:
        self.items.append(HistoricalImport(path, ast.unparse(node), kind="unsupported", detail=detail))

    def _scan(self, path: Path, module: str) -> None:
        path = path.resolve()
        if path in self._visited:
            return
        self._visited.add(path)
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as error:
            self.items.append(HistoricalImport(path, module, kind="unresolved", detail=str(error)))
            return
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        aliases = {"__import__": "__import__"}
        # Copied transitions deliberately retain their original relative package.
        for statement in tree.body:
            if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__package__" for target in statement.targets
            ):
                if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                    package = statement.value.value
                    self._transition_packages.add(package)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    try:
                        base = importlib.util.resolve_name("." * node.level + base, package)
                    except (ImportError, ValueError) as error:
                        self._unsupported(path, node, str(error))
                        continue
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._record(path, alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    try:
                        base = importlib.util.resolve_name("." * node.level + base, package)
                    except ImportError, ValueError:
                        continue
                if base:
                    self._record(path, base)
                # `from package import helper` can import a submodule, while a
                # symbol imported from a module has no separate file provider.
                for alias in node.names:
                    child = f"{base}.{alias.name}"
                    if alias.name != "*" and self.resolver.resolve(child).kind != "unresolved":
                        self._record(path, child)
            elif isinstance(node, ast.Call):
                target = self._call_target(node.func, aliases)
                if target not in {
                    "__import__",
                    "builtins.__import__",
                    "importlib.import_module",
                    "django.utils.module_loading.import_string",
                }:
                    continue
                argument = (
                    node.args[0]
                    if node.args
                    else next(
                        (
                            keyword.value
                            for keyword in node.keywords
                            if keyword.arg in {"name", "dotted_path"}
                        ),
                        None,
                    )
                )
                if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                    self._unsupported(path, node, "non-literal dynamic import")
                    continue
                imported = argument.value
                if target.endswith(".import_string"):
                    imported, _, attribute = imported.rpartition(".")
                    if not imported or not attribute:
                        self._unsupported(path, node, "import_string requires a module and attribute")
                        continue
                if target in {"__import__", "builtins.__import__"}:
                    level = (
                        node.args[4]
                        if len(node.args) > 4
                        else next(
                            (keyword.value for keyword in node.keywords if keyword.arg == "level"),
                            ast.Constant(value=0),
                        )
                    )
                    if not isinstance(level, ast.Constant) or type(level.value) is not int or level.value < 0:
                        self._unsupported(path, node, "__import__ needs a literal nonnegative level")
                        continue
                    if level.value:
                        try:
                            imported = importlib.util.resolve_name("." * level.value + imported, package)
                        except (ImportError, ValueError) as error:
                            self._unsupported(path, node, str(error))
                            continue
                if imported.startswith("."):
                    relative_package = (
                        node.args[1]
                        if len(node.args) > 1
                        else next(
                            (keyword.value for keyword in node.keywords if keyword.arg == "package"), None
                        )
                    )
                    if isinstance(relative_package, ast.Name) and relative_package.id == "__package__":
                        anchor = package
                    elif isinstance(relative_package, ast.Constant) and isinstance(
                        relative_package.value, str
                    ):
                        anchor = relative_package.value
                    else:
                        self._unsupported(path, node, "relative dynamic import needs a literal package")
                        continue
                    try:
                        imported = importlib.util.resolve_name(imported, anchor)
                    except (ImportError, ValueError) as error:
                        self._unsupported(path, node, str(error))
                        continue
                self._record(path, imported)
                if target in {"__import__", "builtins.__import__"}:
                    fromlist = (
                        node.args[3]
                        if len(node.args) > 3
                        else next(
                            (keyword.value for keyword in node.keywords if keyword.arg == "fromlist"),
                            ast.Tuple(elts=[]),
                        )
                    )
                    if not isinstance(fromlist, (ast.Tuple, ast.List)) or not all(
                        isinstance(item, ast.Constant) and isinstance(item.value, str)
                        for item in fromlist.elts
                    ):
                        self._unsupported(path, node, "__import__ needs a literal fromlist")
                        continue
                    for item in fromlist.elts:
                        child = imported + "." + str(ast.literal_eval(item))
                        if self.resolver.resolve(child).kind != "unresolved":
                            self._record(path, child)

    @staticmethod
    def _call_target(node: ast.expr, aliases: dict[str, str]) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return MigrationImportReport._call_target(node.value, aliases) + "." + node.attr
        return ""

    def collect(self) -> tuple[HistoricalImport, ...]:
        """Enumerate selected modules from native settings, never loader objects."""
        mappings = settings.MIGRATION_MODULES
        for config in sorted(apps.get_app_configs(), key=lambda config: config.label):
            module = mappings.get(config.label, config.name + ".migrations")
            if module is None:
                continue
            source = self.resolver.resolve(module)
            if source.path is None:
                # A plain unmigrated app need not provide the native default.
                if config.label in mappings:
                    self.items.append(HistoricalImport(Path(config.path), module, kind="unresolved"))
                continue
            directories = source.search_locations
            if not directories:
                if config.label not in mappings:
                    continue  # Django treats a non-package default as unmigrated.
                self._scan(source.path, module)
                self.items.append(
                    HistoricalImport(
                        source.path, module, kind="unresolved", detail="migration module must be a package"
                    )
                )
                continue
            for directory in directories:
                for path in sorted(directory.glob("*.py")):
                    migration_module = module if path.name == "__init__.py" else module + "." + path.stem
                    if path.name == "__init__.py" or not path.stem.startswith(("_", "~")):
                        self._record(path, migration_module)
                        self._scan(path, migration_module)
        return tuple(sorted(set(self.items), key=lambda item: (str(item.path), item.module, item.kind)))

    def removal_refusals(self, removing: Iterable[str]) -> tuple[str, ...]:
        """Refuse enabled providers, retained imports and unprovable dependencies."""
        selected = {_distribution_key(name) for name in removing}
        refusals = []
        for config in apps.get_app_configs():
            source = self.resolver.resolve(config.name)
            providers = set(source.distributions)
            config_source = self.resolver.resolve(type(config).__module__)
            providers.update(config_source.distributions)
            for name in sorted(providers):
                if _distribution_key(name) in selected:
                    refusals.append(f"{name} provides enabled app {config.name}")
        for item in self.items:
            if item.kind in {"unsupported", "unresolved"}:
                refusals.append(f"{item.path}: {item.kind} {item.module}: {item.detail}")
            for name in item.distributions:
                if _distribution_key(name) in selected:
                    refusals.append(f"{item.path}: retained import {item.module} requires {name}")
        return tuple(sorted(set(refusals)))


def historical_imports() -> tuple[HistoricalImport, ...]:
    """Report selected native history imports without executing imported code."""
    return MigrationImportReport().collect()
