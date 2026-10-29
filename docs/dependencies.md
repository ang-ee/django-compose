# Dependency ownership

`pyproject.toml` owns supported constraints; `uv.lock` records verified versions.
Update this table and the manifest together when changing dependencies.

| Dependency | Owns | Package seam |
| --- | --- | --- |
| Python >=3.14 | Imports, dataclasses, graphlib, paths and typing | Native standard library types |
| Django >=6.1.2,<6.2 | Apps, models, ORM, migrations, commands | Composition, emission and transition origin binding |
| django-stubs + mypy | Django static analysis and type checker internals | Optional composition context and semantic projections |
| Hatchling >=1.27 | Wheel and sdist formats | Library build backend |
| pytest / Ruff / build / Twine | Tests, code quality and distribution tooling | Contributor and CI checks |

Django is the only base dependency. Model imports, integration discovery and
migration authoring stay outside the pre-setup snapshot and serving owners.
Typing dependencies are imported only by the explicitly selected mypy plugin.

The typing extra pins verified django-stubs revision
`423ed850e5f2d7ea9e67f3e0f9e8bee4be215988` (including its `ext` subpackage) and mypy
revision `ac3de6e4c34b04728e768e07d4842f4309ff6c45`. These preserve native manager,
field and semantic-analysis hooks required by the plugin. Direct references need
Git/network access or cached sources. Public index publication requires replacing
these with verified published versions; local archives remain directly installable.

Primary extension contracts:
[Django applications](https://docs.djangoproject.com/en/6.1/ref/applications/) and
[Django migrations](https://docs.djangoproject.com/en/6.1/topics/migrations/).
