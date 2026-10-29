# Packaging a composed host

Compose explicitly in a prepared environment containing every selected source
app and its dependencies:

```sh
python manage.py compose
python manage.py makemigrations
python manage.py migrate
python manage.py compose --check
```

Review and track authored migration history before packaging. Use your build
backend's normal file selection to include the authored settings module, source
apps, generated model/config modules, receipt and native history. A generated
package still depends on its source apps and Django. The package provides no
build-backend adapter and no editable-build side effects.

Serving settings verify source fingerprints and every owned generated file.
Do not strip required source modules or change inputs after sealing output.
Deployment values stay late-bound in the authored settings module; only declared
fingerprint settings become freshness inputs. Do not fingerprint secrets.

For this library itself:

```sh
make check
make dist-check
```

`dist-check` builds wheel and sdist through Hatchling, validates metadata with
Twine, inspects archive contents and installs the base wheel into a separate
temporary environment. It exercises compose, native history, cold serving, ORM,
drift refusal and history-preserving cleanup. The typing extra is absent from
that environment. CI and release workflows upload checked archives as workflow
artifacts and contain no package-index publication step.
