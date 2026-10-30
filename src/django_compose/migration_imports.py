"""Read retained Python dependencies without importing migration or helper code."""

from __future__ import annotations

import ast
import importlib.util
import pkgutil
import symtable
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.machinery import SOURCE_SUFFIXES
from pathlib import Path

from django.apps import apps
from django.conf import settings
from packaging.utils import canonicalize_name

from django_compose.sources import ModuleSource, SourceResolver


@dataclass(frozen=True)
class HistoricalImport:
    """One import edge, classified from its resolved file rather than package name."""

    path: Path
    module: str
    distributions: tuple[str, ...] = ()
    kind: str = "unresolved"
    detail: str = ""


class _ImportCalls(ast.NodeVisitor):
    """Let Python's symbol tables own import bindings across lexical scopes.

    Retain every imported candidate for a binding: conditional imports and
    global/nonlocal rebindings cannot erase a possible native import callable.
    Unrelated local names never replace candidates from a different scope.
    """

    def __init__(self, source: str, path: Path, package: str) -> None:
        self.root = symtable.symtable(source, str(path), "exec")
        self.scope = self.root
        self.package = package
        self.parents: dict[symtable.SymbolTable, symtable.SymbolTable] = {}
        self.aliases: dict[symtable.SymbolTable, dict[str, set[str]]] = {}
        self.calls: list[tuple[ast.Call, symtable.SymbolTable, frozenset[str]]] = []
        self.masked: frozenset[str] = frozenset()

    def _owner(self, scope: symtable.SymbolTable, name: str) -> symtable.SymbolTable:
        try:
            symbol = scope.lookup(name)
        except KeyError:
            return self.root
        if symbol.is_global():
            return self.root
        if symbol.is_free() or symbol.is_nonlocal():
            parent = self.parents[scope]
            while parent is not self.root:
                if parent.get_type() != symtable.SymbolTableType.CLASS:
                    candidate = parent.lookup(name) if name in parent.get_identifiers() else None
                    if candidate is not None and candidate.is_local():
                        return parent
                parent = self.parents[parent]
            return self.root
        return scope

    def _bind(self, name: str, target: str) -> None:
        owner = self._owner(self.scope, name)
        self.aliases.setdefault(owner, {}).setdefault(name, set()).add(target)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self._bind(name, alias.name if alias.asname else name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            try:
                base = importlib.util.resolve_name("." * node.level + base, self.package)
            except ImportError, ValueError:
                return  # The report separately records this invalid import.
        for alias in node.names:
            self._bind(alias.asname or alias.name, f"{base}.{alias.name}")

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, self.scope, self.masked))
        self.generic_visit(node)

    def _nested(self, node: ast.stmt | ast.expr, name: str, body: Iterable[ast.AST]) -> None:
        # Type parameter tables wrap generic functions/classes in Python 3.14.
        parent = self.scope
        candidates = [
            child
            for child in parent.get_children()
            if child.get_name() == name and child.get_lineno() == node.lineno
        ]
        if len(candidates) != 1:
            raise ValueError(f"Cannot uniquely match Python lexical scope {name!r} at line {node.lineno}")
        child = candidates[0]
        self.parents[child] = parent
        while child.get_type() == symtable.SymbolTableType.TYPE_PARAMETERS:
            parent = child
            child = next(table for table in child.get_children() if table.get_name() == name)
            self.parents[child] = parent
        previous, self.scope = self.scope, child
        for statement in body:
            self.visit(statement)
        self.scope = previous

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
            if expression is not None:
                self.visit(expression)
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *([node.args.vararg] if node.args.vararg else []),
            *([node.args.kwarg] if node.args.kwarg else []),
        ):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        for parameter in node.type_params:
            self.visit(parameter)
        self._nested(node, node.name, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases, *node.keywords):
            self.visit(expression)
        self._nested(node, node.name, node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in (*node.args.defaults, *node.args.kw_defaults):
            if expression is not None:
                self.visit(expression)
        self._nested(node, "lambda", (node.body,))

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        # Python inlines list/set/dict comprehensions; their iteration variables
        # still shadow outer bindings. Generator expressions have a native scope.
        self.visit(node.generators[0].iter)
        expressions = [expression for generator in node.generators for expression in generator.ifs]
        expressions.extend(generator.iter for generator in node.generators[1:])
        if isinstance(node, ast.DictComp):
            expressions.extend((node.key, node.value))
        else:
            expressions.append(node.elt)
        if isinstance(node, ast.GeneratorExp):
            self._nested(node, "genexpr", expressions)
            return
        previous = self.masked
        self.masked |= frozenset(
            target.id
            for generator in node.generators
            for target in ast.walk(generator.target)
            if isinstance(target, ast.Name)
        )
        for expression in expressions:
            self.visit(expression)
        self.masked = previous

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension

    def targets(self, node: ast.expr, scope: symtable.SymbolTable, masked: frozenset[str]) -> set[str]:
        if isinstance(node, ast.Name):
            if node.id in masked:
                return set()
            owner = self._owner(scope, node.id)
            owners = {owner}
            if scope.get_type() == symtable.SymbolTableType.CLASS and owner is scope:
                # Class bodies use LOAD_NAME: an unbound local falls back to
                # globals, unlike a function's statically local variables.
                owners.add(self.root)
            targets = {
                target for table in owners for target in self.aliases.get(table, {}).get(node.id, set())
            }
            if node.id == "__import__" and self.root in owners:
                targets.add("__import__")
            return targets
        if isinstance(node, ast.Attribute):
            return {target + "." + node.attr for target in self.targets(node.value, scope, masked)}
        return set()


class MigrationImportReport:
    """Audit selected native migration sources and local helper bodies offline.

    Installed dependencies outside native migration packages and literal historical
    package anchors are leaves. Project, enabled-app and anchored helpers are
    recursively inspected, including imports inside RunPython callables.
    """

    def __init__(self) -> None:
        self.resolver = SourceResolver(apps.get_app_configs())
        self.items: list[HistoricalImport] = []
        self._visited: set[Path] = set()
        self._history_helper_packages: set[str] = set()

    def _edge(self, path: Path, source: ModuleSource) -> HistoricalImport:
        if source.path is not None and source.path.is_dir() and source.search_locations:
            # A namespace executes no initializer. Its distribution portions are
            # alternatives for availability, not independent import requirements.
            providers = (
                source.distributions
                if all(self.resolver.distribution_names(location) for location in source.search_locations)
                else ()
            )
            return HistoricalImport(path, source.module, providers, "namespace")
        return HistoricalImport(path, source.module, source.distributions, source.kind)

    def _record(self, path: Path, module: str) -> None:
        source = self.resolver.resolve(module)
        self.items.append(self._edge(path, source))
        if (
            source.path is not None
            and source.path.is_file()
            and (
                source.kind in {"app", "project"}
                or any(
                    module == package or module.startswith(package + ".")
                    for package in self._history_helper_packages
                )
            )
        ):
            self._scan(source.path, module)
        # Importing a child also executes its parent packages. Inspect local
        # package initializers even when the child itself belongs to a dist.
        for index in range(1, len(module.split("."))):
            parent = ".".join(module.split(".")[:index])
            parent_source = self.resolver.resolve(parent)
            self.items.append(self._edge(path, parent_source))
            if (
                parent_source.path is not None
                and parent_source.path.is_file()
                and (parent_source.kind in {"app", "project"} or parent in self._history_helper_packages)
            ):
                self._scan(parent_source.path, parent)

    def _unsupported(self, path: Path, node: ast.AST, detail: str) -> None:
        self.items.append(HistoricalImport(path, ast.unparse(node), kind="unsupported", detail=detail))

    def _scan(self, path: Path, module: str) -> None:
        path = path.resolve()
        if path in self._visited:
            return
        self._visited.add(path)
        if path.suffix not in SOURCE_SUFFIXES:
            self.items.append(
                HistoricalImport(
                    path,
                    module,
                    kind="unsupported",
                    detail="migration/helper has no inspectable Python source",
                )
            )
            return
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as error:
            self.items.append(HistoricalImport(path, module, kind="unresolved", detail=str(error)))
            return
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        # Retained history can explicitly preserve its original relative package.
        for statement in tree.body:
            if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__package__" for target in statement.targets
            ):
                if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                    package = statement.value.value
                    self._history_helper_packages.add(package)
        try:
            bindings = _ImportCalls(source, path, package)
            bindings.visit(tree)
        except SyntaxError as error:
            self.items.append(HistoricalImport(path, module, kind="unresolved", detail=str(error)))
            return
        except (ValueError, StopIteration) as error:
            self.items.append(HistoricalImport(path, module, kind="unsupported", detail=str(error)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._record(path, alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    try:
                        base = importlib.util.resolve_name("." * node.level + base, package)
                    except (ImportError, ValueError) as error:
                        self._unsupported(path, node, str(error))
                        continue
                if base:
                    self._record(path, base)
                # `from package import helper` can import a submodule, while a
                # symbol imported from a module has no separate file provider.
                for alias in node.names:
                    child = f"{base}.{alias.name}"
                    if alias.name != "*" and self.resolver.resolve(child).kind != "unresolved":
                        self._record(path, child)
        for node, scope, masked in bindings.calls:
            for target in bindings.targets(node.func, scope, masked):
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

    def collect(self) -> tuple[HistoricalImport, ...]:
        """Enumerate selected modules from native settings, never loader objects."""
        mappings = settings.MIGRATION_MODULES
        configs = sorted(apps.get_app_configs(), key=lambda config: config.label)
        for config in configs:
            module = mappings.get(config.label, config.name + ".migrations")
            if module is not None and self.resolver.resolve(module).search_locations:
                self._history_helper_packages.add(module)
        for config in configs:
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
            self._record(source.path, module)
            for _, name, is_package in sorted(
                pkgutil.iter_modules([str(path) for path in directories]), key=lambda item: item.name
            ):
                if not is_package and not name.startswith(("_", "~")):
                    migration_module = module + "." + name
                    self._record(source.path, migration_module)
                    migration_source = self.resolver.resolve(migration_module)
                    if migration_source.path is not None:
                        self._scan(migration_source.path, migration_module)
        return tuple(sorted(set(self.items), key=lambda item: (str(item.path), item.module, item.kind)))

    def removal_refusals(self, removing: Iterable[str]) -> tuple[str, ...]:
        """Refuse enabled providers, retained imports and unprovable dependencies."""
        selected = {canonicalize_name(name) for name in removing}
        refusals = []
        for config in apps.get_app_configs():
            source = self.resolver.resolve(config.name)
            providers = set(source.distributions)
            config_source = self.resolver.resolve(type(config).__module__)
            providers.update(config_source.distributions)
            for name in sorted(providers):
                if canonicalize_name(name) in selected:
                    refusals.append(f"{name} provides enabled app {config.name}")
        for item in self.items:
            if item.kind in {"unsupported", "unresolved"}:
                refusals.append(f"{item.path}: {item.kind} {item.module}: {item.detail}")
            if item.kind == "namespace":
                if item.distributions and all(
                    canonicalize_name(name) in selected for name in item.distributions
                ):
                    refusals.append(
                        f"{item.path}: retained namespace import {item.module} needs at least one of "
                        + ", ".join(item.distributions)
                    )
                continue
            for name in item.distributions:
                if canonicalize_name(name) in selected:
                    refusals.append(f"{item.path}: retained import {item.module} requires {name}")
        return tuple(sorted(set(refusals)))


def historical_imports() -> tuple[HistoricalImport, ...]:
    """Report selected native history imports without executing imported code."""
    return MigrationImportReport().collect()
