# 0004: keep web serving and command dispatch outside the compiler

Status: accepted.

django-compose owns composition, deterministic emission, verified output,
native history retention and database preparation. Django owns URL resolution
(`include()`, `ROOT_URLCONF`), the ASGI/WSGI handlers and management command
dispatch (`ManagementUtility`). The original contract stated that the compiler
knows no web or protocol vocabulary; `django_compose.urls`,
`django_compose.asgi` and `COMPOSE_COMMAND_CONTEXT` contradicted it. They had
no dependency on the snapshot, receipt or generated output.

Remove the three seams from this distribution:

- `django_compose.urls` with the `compose_urls`, `compose_url_prefix` and
  `compose_url_namespace` AppConfig attributes and system check `django_compose.E001`
  ([0001](0001-native-url-collisions.md) records the check's semantics).
- `django_compose.asgi` with `AppConfig.compose_asgi()` and `AsgiContribution`.
- `COMPOSE_COMMAND_CONTEXT` and the launcher's command-execution wrapper. The
  launcher dispatches through Django's `ManagementUtility`; `execute()` gains a
  keyword-only `utility_class` so a project supplies its own subclass. That is
  the only native extension point retained.

Caller migration: route app URLconfs with native `include()` in the project
URLconf; serve with Django's ASGI or WSGI application or a project-owned ASGI
router; wrap command execution through `utility_class` from the project's own
`manage.py`. The seams continue as reusable code in the Angee framework's serving
addon (`angee.server.urls`, `angee.server.asgi`, `angee.server.management` in
`angee-django`), which depends on Django, not on the compiler. No django-compose
API replaces them.

Compatibility: this is a prerelease API cutoff recorded in the changelog. The
snapshot no longer lists `COMPOSE_COMMAND_CONTEXT` as a non-input setting; a
project that still sets it fails at `configure` because unknown `COMPOSE_*`
values must be literal snapshot inputs.
