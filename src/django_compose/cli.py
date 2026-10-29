"""Small command adapter for the compiler and guarded generated-tree owner."""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path

_WORKER = """import sys
sys.dont_write_bytecode = True
from django_compose.build import compile_settings
try:
    compile_settings(sys.argv[1], output=sys.argv[2] or None, check=sys.argv[3] == "check",
                     check_history=sys.argv[4] == "history")
except Exception as error:
    print(f"django-compose: {error}", file=sys.stderr)
    sys.exit(1)
"""


def run_compiler(
    settings: str,
    output: str | None = None,
    *,
    check: bool = False,
    check_history: bool = False,
    cwd: str | None = None,
) -> int:
    """Run one compiler operation in one fresh native Django interpreter."""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            _WORKER,
            settings,
            output or "",
            "check" if check else "build",
            "history" if check_history else "",
        ],
        cwd=cwd,
        check=False,
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Dispatch build, check and cleanup without repairing output implicitly."""
    parser = argparse.ArgumentParser(prog="django-compose")
    parser.add_argument("command", choices=("build", "check", "clean"))
    parser.add_argument("--settings", required=True, help="raw importable authored Django settings module")
    parser.add_argument("--output", help="generated runtime package directory")
    arguments = parser.parse_args(argv)
    if arguments.command != "clean":
        return run_compiler(arguments.settings, arguments.output, check=arguments.command == "check")
    try:
        from django_compose.artifacts import GeneratedTree

        raw = importlib.import_module(arguments.settings)
        output = Path(arguments.output or getattr(raw, "COMPOSE_RUNTIME_DIR", "runtime")).expanduser()
        if not output.is_absolute():
            filename = getattr(raw, "__file__", None)
            if not filename:
                raise ValueError("The authored settings module must have a source location")
            output = Path(filename).resolve().parent / output
        GeneratedTree(output, {}, clean_root=output).clean()
    except Exception as error:
        print(f"django-compose: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
