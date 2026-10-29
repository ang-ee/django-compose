# Packaging a composed host

Install `django-compose[hatch]` in the host's build environment. The optional
Hatch hook consumes the same raw settings module as the CLI:

```toml
[build-system]
requires = ["hatchling", "django-compose[hatch]"]
build-backend = "hatchling.build"

[tool.hatch.build.hooks.django-compose]
settings = "project.settings"

[tool.hatch.build.targets.wheel]
packages = ["project", "catalog", "reviews"]
```

Until a release is published, use a direct path or URL to the built distribution
in the build requirements, or build with a prepared environment and
`--no-build-isolation`. All selected apps and their dependencies must be installed
in the build environment before composition can import their native declarations.

Editable builds explicitly compile. Standard wheels and sdists validate sealed
output and native history without repairing it. Build first, author and review
required migrations, then package. Native Hatch `packages`, `include`, and
`force-include` own authored source selection; include the raw settings module
and local source apps. The hook adds generated files recorded by the manifest
and durable runtime migration history, excludes unrelated files and refuses
symlinked sources or competing output paths. A runtime directory is not a
self-contained deployment without its source app packages and dependencies.

For this library itself:

```sh
uv build
uv run --no-sync python scripts/check_distribution.py
```

The second command creates a separate temporary environment, installs the wheel
without typing extras and executes a complete native host workflow. It does not
publish an artifact. The included CI and release workflows upload checked
archives as workflow artifacts; they contain no PyPI publishing step.
