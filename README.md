# django-compose

Compile reusable native Django applications into an importable runtime package.
Django owns application lifecycle, model construction, and migrations. This
library adds explicit composition and deterministic generation; serving imports
prebuilt output.

The [specification](docs/SPEC.md) defines the supported contracts. The package
requires Python 3.14 and Django 6.1. It is independent of any consumer framework.
