"""The standalone launcher uses the same native management entrypoint."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from django_compose.management import execute

    execute(["django-compose", *(sys.argv[1:] if argv is None else argv)])
    return 0
