# Shop example

`catalog` declares an abstract `Item`; `reviews` adds a rating and a method.
The selected apps produce one concrete `catalog.Item` with one database table.
The project uses local SQLite and the library's base dependencies.

Follow the repository's [quick start](../../docs/quickstart.md), beginning at the
checkout root. [settings.py](settings.py) contains the current explicit settings
integration, and [manage.py](manage.py) selects composition mode before Django
loads settings. Generated classes go to `runtime/`; native migration files live
under `runtime/catalog/migrations/`.
