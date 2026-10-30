"""Settings-free app inspection and the native management entrypoint."""

from __future__ import annotations

import argparse
import json
import sys

from django.core.exceptions import ImproperlyConfigured


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "inspect-apps":
        from django_compose.addons import inspect_apps

        parser = argparse.ArgumentParser(prog="django-compose inspect-apps")
        parser.add_argument("--group", help="Installed addon entry-point group")
        parser.add_argument("roots", nargs="*", help="Native app or AppConfig entries")
        options = parser.parse_args(arguments[1:])
        try:
            result = inspect_apps(options.roots, group=options.group)
        except ImproperlyConfigured as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0

    from django_compose.management import execute

    execute(["django-compose", *arguments])
    return 0
