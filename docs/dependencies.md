# Dependency ownership

`pyproject.toml` owns supported constraints; `uv.lock` records verified versions.
Update this table and the manifest together when changing the dependency graph.

| Dependency | Owns | Package seam |
| --- | --- | --- |
| Python >=3.14 | Execution, import machinery, dataclasses, graphlib, paths and typing | Composition uses native standard library types |
| Django >=6.1.2,<6.2 | Apps, model construction, ORM, native migrations and command lifecycle | Selection, emission and reusable migration origin binding |
| django-yamlconf >=1.5,<2 | Setting merge operators and declaration provenance | Ordered app autoconfig and explicit environment overrides |
| hatch-angee >=0.2.6,<0.3 | Manifest parsing/discovery, app dependency ordering and requirement projection | Native AppConfig identity binding and optional manifest adapter |
| Hatchling >=1.27 | Wheel/sdist building and hook lifecycle | Optional hook over the one compiler; also a transitive dependency of hatch-angee |
| django-stubs + mypy | Django static analysis and type checker internals | Optional composition context and semantic projections |
| pytest / Ruff / build / Twine | Tests, code quality and distribution tooling | Contributor and CI checks |

Metadata consumers use `hatch_angee` directly; no facade duplicates those APIs.
Importing its metadata APIs loads neither Django nor Hatchling. Serving never
imports the build hook or typing plugin even when their dependencies are installed.

The typing extra currently requires the verified django-stubs revision
`423ed850e5f2d7ea9e67f3e0f9e8bee4be215988` (including its `ext` subpackage) and mypy
revision `ac3de6e4c34b04728e768e07d4842f4309ff6c45`. These preserve native manager,
field and semantic-analysis hooks required by the plugin. They are explicit
direct references, not silently substituted published releases. Installation of
that extra needs Git/network access or cached source archives. Public index
publication requires replacing those references with verified published versions;
the local wheel/sdist and CI artifacts remain installable directly.

Primary extension contracts:
[Django applications](https://docs.djangoproject.com/en/6.1/ref/applications/),
[Django migrations](https://docs.djangoproject.com/en/6.1/topics/migrations/),
[Hatch build hooks](https://hatch.pypa.io/latest/plugins/build-hook/reference/).
