# Shop example

Compose reusable Django apps into a runnable project.

`catalog` declares an abstract `Item`; `reviews` adds a rating and method.
Both are installable example distributions in `compose.example`. The project
selects only `reviews`; its Requires-Dist activates catalog, whose
`requires_apps` selects Django auth/contenttypes. No other framework is needed.

Follow the [quick start](../../docs/quickstart.md) from the checkout root. Install
both local app distributions before composing. Then, from this directory:

```sh
uv run --project ../.. --no-sync python verify.py
```

[settings.py](settings.py) composes app selection, additive defaults and a
declared `CATALOG_` environment overlay; the reviews callback reads the same
derived selected closure exposed after configure. [Catalog's native config](catalog/apps.py)
calls `get_user_model()` in ready and reads a models-phase artifact. The project
[URLconf](urls.py) includes the app's URLconf with Django's native `include()`.
[Integrations](catalog/integrations.py) render the same deterministic schema
from the models-phase view and ready-phase native registry. The ready hook derives
its filename from the populated model label; serving discovers that path from
the receipt, with no exact-path configuration. [verify.py](verify.py)
compares those files with serving bytes under a receipt lease and exercises
`/catalog/items/`. The schema is plain SDL-like text; it needs no GraphQL dependency.

`manage.py runserver` uses the managed native Django reload loop. Changes to model
state still require reviewed native migrations. Generated output and SQLite data
are disposable example state; preserve authored migration history in real projects.
