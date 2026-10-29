# Shop example

Compose reusable Django apps into a runnable project.

`catalog` declares an abstract `Item`; `reviews` adds a rating and method.
Both are installable example distributions in `compose.example`. The project
selects only `reviews`; its Requires-Dist activates catalog, whose native
requires_apps selects Django auth/contenttypes. No Angee installation is needed.

Follow the [quick start](../../docs/quickstart.md) from the checkout root. Install
both local app distributions before composing. Then, from this directory:

```sh
uv run --project ../.. --no-sync python verify.py
uv run --project ../.. --no-sync python manage.py verify_context
uv run --project ../.. --no-sync python manage.py verify_context --database reporting
```

[settings.py](settings.py) composes app selection, additive defaults, a declared
`CATALOG_` environment overlay and [post-parse command contexts](contexts.py).
The context command checks default and selected native database options; the
reviews callback reads the same derived selected closure exposed after configure.
[Catalog's native config](catalog/apps.py) calls `get_user_model()` in ready,
reads a models-phase artifact, and opts into URL and ASGI contributions.
[Integrations](catalog/integrations.py) render the same deterministic schema
from the models-phase view and ready-phase native registry. The ready hook derives
its filename from the populated model label; serving discovers that path from
the receipt, with no exact-path configuration. [verify.py](verify.py)
compares those files with serving bytes under a receipt lease, exercises
`/catalog/items/`, the `/status` ASGI mount and lifespan startup/shutdown.
The schema is plain SDL-like text; it needs no GraphQL dependency.

`manage.py runserver` uses the managed native Django reload loop. Changes to model
state still require reviewed native migrations. Generated output and SQLite data
are disposable example state; preserve authored migration history in real projects.
