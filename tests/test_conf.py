"""Project settings compose through native app and yamlconf owners before setup."""

import importlib
import os
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_compose import conf


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DJANGO_COMPOSE_MODE", "write")
    created = []

    def app(body=""):
        name = f"confapp_{len(created)}"
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "models.py").write_text("raise AssertionError('premature model import')\n")
        (package / "autoconfig.py").write_text(body)
        created.append(name)
        importlib.invalidate_caches()
        return name

    namespace = {"COMPOSE_OUTPUT_DIR": tmp_path / "runtime", "INSTALLED_APPS": []}
    yield app, namespace
    for name in tuple(sys.modules):
        if name.split(".")[0] in created:
            sys.modules.pop(name)


def test_defaults_native_merges_and_provenance(project):
    app, ns = project
    ns.update(PROJECT_VALUE="authored", ITEMS=["project"], OPTIONS={"existing": True})
    ns["INSTALLED_APPS"] = [
        app("""SETTINGS = {
'PROJECT_VALUE': 'default', 'DEFAULT_VALUE': 42,
'ITEMS:append': ['first'], 'OPTIONS.child': 'first', 'NEW.child': 'new'}
"""),
        app("SETTINGS = {'ITEMS:append': ['second'], 'OPTIONS.other': 'second'}"),
    ]
    conf.configure(ns)
    assert ns["PROJECT_VALUE"] == "authored"
    assert ns["DEFAULT_VALUE"] == 42
    assert ns["ITEMS"] == ["project", "first", "second"]
    assert ns["OPTIONS"] == {"existing": True, "child": "first", "other": "second"}
    assert ns["NEW"] == {"child": "new"}
    info = ns["_YAMLCONF_ATTRIBUTES"]["ITEMS"]
    assert info["source"] == "confapp_1.autoconfig"
    assert info["history"][0][1] == "confapp_0.autoconfig"
    assert ns["COMPOSE_ROOTS"] == ("confapp_0", "confapp_1")
    assert ns["COMPOSE_SNAPSHOT"].settings["DEFAULT_AUTO_FIELD"]
    assert ns["INSTALLED_APPS"][-1] == "django_compose.apps.BuildComposeConfig"


def test_callable_reads_effective_environment_and_authored_values(project, monkeypatch):
    app, ns = project
    monkeypatch.setenv("TASK_LIMIT", "7")
    monkeypatch.setenv("UNDECLARED_SECRET", "hidden")
    ns.update(COMPOSE_ENV_PREFIXES=["TASK_"], DEPLOYMENT="project")
    ns["INSTALLED_APPS"] = [
        app("""SETTINGS = {'TASK_LIMIT': 1}
def settings(namespace):
    assert namespace['TASK_LIMIT'] == '7'
    assert namespace['DEPLOYMENT'] == 'project'
    assert 'UNDECLARED_SECRET' not in namespace
    return {'TASK_LIMIT': int(namespace['TASK_LIMIT']), 'DEPLOYMENT': 'ignored'}
""")
    ]
    conf.configure(ns)
    assert ns["TASK_LIMIT"] == 7
    assert ns["DEPLOYMENT"] == "project"
    assert ns["_YAMLCONF_ATTRIBUTES"]["TASK_LIMIT"]["history"][0] == ("7", "**ENVIRONMENT**")


def test_only_scalar_competing_defaults_fail_even_when_authored(project):
    app, ns = project
    ns["VALUE"] = "project"
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'VALUE': 'one'}"), app("SETTINGS = {'VALUE': 'two'}")]
    with pytest.raises(ImproperlyConfigured, match="VALUE.*confapp_0.*confapp_1"):
        conf.configure(ns)


def test_container_defaults_fill_gaps_and_callbacks_do_not_replace_another_app(project):
    app, ns = project
    ns["INSTALLED_APPS"] = [
        app("SETTINGS = {'ITEMS': ['one'], 'OPTIONS': {'one': 1}}"),
        app("""
SETTINGS = {'ITEMS': ['two'], 'OPTIONS': {'two': 2}}
def settings(namespace):
    return {'ITEMS': ['derived'], 'OPTIONS': {'derived': True}}
"""),
    ]
    conf.configure(ns)
    assert ns["ITEMS"] == ["one"]
    assert ns["OPTIONS"] == {"one": 1}


@pytest.mark.parametrize(
    "key",
    [
        "INSTALLED_APPS:append",
        "COMPOSE_OUTPUT_DIR.child",
        "COMPOSE_OUTPUT_OTHER",
        "COMPOSE_ROOTS:doc",
        "COMPOSE_SNAPSHOT",
        "COMPOSE_CLOSURE:append",
    ],
)
def test_protected_contributions(project, key):
    app, ns = project
    ns["INSTALLED_APPS"] = [app(f"SETTINGS = {{{key!r}: 'forbidden'}}")]
    with pytest.raises(ImproperlyConfigured, match="must not contribute"):
        conf.configure(ns)


@pytest.mark.parametrize(
    "body, message",
    [
        ("SETTINGS = []", "SETTINGS must be a mapping"),
        ("settings = 1", "settings must be callable"),
        ("def settings(namespace): return []", "must return a mapping"),
        ("SETTINGS = {1: 'bad'}", "invalid setting name"),
        ("SETTINGS = {'lower': 'bad'}", "invalid setting name"),
        ("SETTINGS = {'OPTIONS:append': []}", "not a dict"),
    ],
)
def test_invalid_contributions_fail(project, body, message):
    app, ns = project
    ns["OPTIONS"] = {"existing": 1}
    ns["INSTALLED_APPS"] = [app(body)]
    with pytest.raises(ImproperlyConfigured, match=message):
        conf.configure(ns)


@pytest.mark.parametrize("name", ["COMPOSE_ROOTS", "COMPOSE_SNAPSHOT", "COMPOSE_CLOSURE"])
def test_derived_settings_are_refused_on_entry(project, name):
    _, ns = project
    ns[name] = None
    with pytest.raises(ImproperlyConfigured, match="derived"):
        conf.configure(ns)


def test_structural_environment_refused_before_and_after_custom_declaration(project, monkeypatch):
    app, ns = project
    ns["COMPOSE_ENV_PREFIXES"] = ["CUSTOM_"]
    monkeypatch.setenv("CUSTOM_INPUT", "environment")
    ns["INSTALLED_APPS"] = [
        app("SETTINGS = {'CUSTOM_INPUT': 'default'}"),
        app("SETTINGS = {'COMPOSE_FINGERPRINT_SETTINGS:append': ['CUSTOM_INPUT']}"),
    ]
    with pytest.raises(ImproperlyConfigured, match="CUSTOM_INPUT.*environment"):
        conf.configure(ns)


def test_existing_dotted_environment_provenance_is_refused(project):
    _, ns = project
    ns["_YAMLCONF_ATTRIBUTES"] = {
        "MIGRATION_MODULES.label": {
            "source": "project",
            "history": [("old", "**ENVIRONMENT**")],
            "value": None,
            "evalue": None,
            "doc": [],
            "hide": False,
        }
    }
    with pytest.raises(ImproperlyConfigured, match="MIGRATION_MODULES.*environment"):
        conf.configure(ns)


def test_import_errors_are_not_treated_as_absent_autoconfig(project):
    app, ns = project
    ns["INSTALLED_APPS"] = [app("import missing_conf_dependency")]
    with pytest.raises(ModuleNotFoundError, match="missing_conf_dependency"):
        conf.configure(ns)


def test_native_subprocess_build_check_and_compiler_free_serve(tmp_path):
    package = tmp_path / "nativeapp"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text("""from django.db import models
from django_compose.models import Composable
class Item(Composable, concrete=True):
    name = models.CharField(max_length=20)
    class Meta:
        abstract = True
""")
    (package / "autoconfig.py").write_text("SETTINGS = {'TEST_VALUES:append': ['native']}\n")
    (tmp_path / "settings.py").write_text("""from pathlib import Path
from django_compose.conf import configure
SECRET_KEY = 'test'
INSTALLED_APPS = ['nativeapp']
COMPOSE_OUTPUT_DIR = Path(__file__).parent / 'runtime'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
configure(globals())
""")
    env = dict(
        os.environ, DJANGO_SETTINGS_MODULE="settings", PYTHONPATH=os.pathsep.join([str(tmp_path), *sys.path])
    )
    env.pop("DJANGO_COMPOSE_MODE", None)
    for args in [
        ("-m", "django_compose", "compose"),
        ("-m", "django_compose", "makemigrations", "--noinput"),
        ("-m", "django_compose", "compose", "--check"),
        (
            "-c",
            """
import importlib.abc
import sys
class BlockCompiler(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'django_compose.build', 'django_compose.model_composition'}:
            raise AssertionError('compiler imported while serving')
sys.meta_path.insert(0, BlockCompiler())
from django_compose.conf import configure
assert 'django.db.models' not in sys.modules
import django
django.setup()
from django.apps import apps
from django.conf import settings
assert apps.get_model('nativeapp', 'Item')._meta.abstract is False
assert settings.TEST_VALUES == ['native']
""",
        ),
    ]:
        result = subprocess.run(
            [sys.executable, "-B", *args], cwd=tmp_path, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_snapshot_once_after_contributions_and_projection_cleanup(project, monkeypatch):
    app, ns = project
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'VALUE': 'contributed'}")]
    monkeypatch.delenv("DJANGO_COMPOSE_MODE")
    original = conf.snapshot
    calls = []
    closed = []

    def freeze(*args, **kwargs):
        assert kwargs["namespace"]["VALUE"] == "contributed"
        calls.append(args)
        return original(*args, **kwargs)

    def verify(snap, output_module, *, retain_startup):
        assert retain_startup
        raise RuntimeError("startup failure")

    monkeypatch.setattr(conf, "snapshot", freeze)
    monkeypatch.setattr(conf.serving, "verify", verify)
    monkeypatch.setattr(conf.serving, "_close_startup_verification", lambda: closed.append(True))
    with pytest.raises(RuntimeError, match="startup failure"):
        conf.configure(ns)
    assert len(calls) == 1
    assert closed == [True]


def test_direct_structural_environment_input_is_refused(project, monkeypatch):
    app, ns = project
    ns["COMPOSE_ENV_PREFIXES"] = ["COMPOSE_"]
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'COMPOSE_INTEGRATIONS:append': []}")]
    monkeypatch.setenv("COMPOSE_INTEGRATIONS", "untrusted")
    with pytest.raises(ImproperlyConfigured, match="COMPOSE_INTEGRATIONS.*environment"):
        conf.configure(ns)


def test_mode_and_required_output_are_validated_before_imports(project, monkeypatch):
    _, ns = project
    monkeypatch.setenv("DJANGO_COMPOSE_MODE", "unknown")
    with pytest.raises(ImproperlyConfigured, match="DJANGO_COMPOSE_MODE"):
        conf.configure(ns)
    monkeypatch.setenv("DJANGO_COMPOSE_MODE", "write")
    del ns["COMPOSE_OUTPUT_DIR"]
    with pytest.raises(ImproperlyConfigured, match="COMPOSE_OUTPUT_DIR"):
        conf.configure(ns)


def test_authored_explicit_roots_survive_dependency_expansion(project, tmp_path):
    app, ns = project
    dependency = app()
    root = app()
    (tmp_path / root / "apps.py").write_text(f"""from django.apps import AppConfig
class Chosen(AppConfig):
    name = {root!r}
    requires_apps = ({dependency!r},)
""")
    selected = f"{root}.apps.Chosen"
    ns["INSTALLED_APPS"] = (selected,)
    conf.configure(ns)
    assert ns["COMPOSE_ROOTS"] == (selected,)
    assert ns["INSTALLED_APPS"][:2] == (dependency, selected)


def test_callback_snapshot_is_readonly_and_nested_values_are_detached(project):
    app, ns = project
    ns.update(OPTIONS={"items": ["project"]}, deployment_module=os)
    ns["INSTALLED_APPS"] = [
        app("""SETTINGS = {'CALLBACK_VALUE': 'before'}
def settings(namespace):
    try:
        namespace['COMPOSE_OUTPUT_DIR'] = '/redirected'
    except TypeError:
        pass
    else:
        raise AssertionError('writable callback namespace')
    namespace['OPTIONS']['items'].append('mutated')
    return {'CALLBACK_VALUE': 'after'}
""")
    ]
    original = ns["COMPOSE_OUTPUT_DIR"]
    conf.configure(ns)
    assert ns["COMPOSE_OUTPUT_DIR"] == original
    assert ns["OPTIONS"] == {"items": ["project"]}
    assert ns["CALLBACK_VALUE"] == "after"


def test_returned_callback_protected_keys_still_fail(project):
    app, ns = project
    ns["INSTALLED_APPS"] = [app("def settings(namespace): return {'COMPOSE_OUTPUT_DIR': '/other'}")]
    with pytest.raises(ImproperlyConfigured, match="COMPOSE_OUTPUT_DIR"):
        conf.configure(ns)


def test_project_reference_inputs_and_raw_authored_strings(project):
    app, ns = project
    ns.update(BASE_DIR="/project", TEMPLATE="literal {not_a_reference}", OPTIONS={"literal": "{untouched}"})
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'DATA_DIR': '{BASE_DIR}/data'}")]
    conf.configure(ns)
    assert ns["DATA_DIR"] == "/project/data"
    assert ns["TEMPLATE"] == "literal {not_a_reference}"
    assert ns["OPTIONS"] == {"literal": "{untouched}"}
    assert ns["_YAMLCONF_ATTRIBUTES"]["BASE_DIR"]["source"] == "project.settings"


@pytest.mark.parametrize(
    "template",
    [
        "{TASK_INPUT}",
        ["{TASK_INPUT}"],
        {"nested": ["{TASK_INPUT}"]},
        "{BRIDGE}",
        "{TASK_INPUT!r}",
    ],
)
def test_transitive_environment_references_in_structural_values(project, monkeypatch, template):
    app, ns = project
    ns["COMPOSE_ENV_PREFIXES"] = ["TASK_"]
    monkeypatch.setenv("TASK_INPUT", "environment")
    ns["INSTALLED_APPS"] = [
        app(f"""SETTINGS = {{
'TASK_INPUT': 'default', 'BRIDGE': '{{TASK_INPUT}}', 'CUSTOM_INPUT': {template!r}}}
"""),
        app("SETTINGS = {'COMPOSE_FINGERPRINT_SETTINGS:append': ['CUSTOM_INPUT']}"),
    ]
    with pytest.raises(ImproperlyConfigured, match="CUSTOM_INPUT.*environment.*TASK_INPUT"):
        conf.configure(ns)


def test_environment_in_directive_and_prior_native_provenance(project, monkeypatch):
    app, ns = project
    ns["COMPOSE_ENV_PREFIXES"] = ["TASK_"]
    monkeypatch.setenv("TASK_INPUT", "environment")
    ns["_YAMLCONF_ATTRIBUTES"] = {
        "BRIDGE": {
            "value": "{TASK_INPUT}",
            "evalue": "default",
            "source": "project",
            "history": [],
            "doc": [],
            "hide": False,
        }
    }
    ns["INSTALLED_APPS"] = [
        app("SETTINGS = {'TASK_INPUT': 'default', 'CUSTOM_INPUT:append': ['{BRIDGE}']}"),
        app("SETTINGS = {'COMPOSE_FINGERPRINT_SETTINGS:append': ['CUSTOM_INPUT']}"),
    ]
    with pytest.raises(ImproperlyConfigured, match="CUSTOM_INPUT.*environment"):
        conf.configure(ns)


def test_unrelated_nonstructural_environment_is_allowed(project, monkeypatch):
    app, ns = project
    ns.update(COMPOSE_ENV_PREFIXES=["TASK_"], COMPOSE_FINGERPRINT_SETTINGS=["CUSTOM_INPUT"])
    monkeypatch.setenv("TASK_INPUT", "environment")
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'TASK_INPUT': 'default', 'CUSTOM_INPUT': '{BASE_DIR}/data'}")]
    ns["BASE_DIR"] = "/project"
    conf.configure(ns)
    assert ns["TASK_INPUT"] == "environment"
    assert ns["CUSTOM_INPUT"] == "/project/data"


def test_seeded_project_dictionary_preserves_later_dotted_contribution(project):
    app, ns = project
    ns["PROJECT_OPTIONS"] = {"a": 1}
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'PROJECT_OPTIONS.b': 2}"), app("SETTINGS = {'OTHER': 'value'}")]
    conf.configure(ns)
    assert ns["PROJECT_OPTIONS"] == {"a": 1, "b": 2}


def test_reference_to_preexisting_environment_dictionary_leaf(project):
    app, ns = project
    ns.update(OPTIONS={"child": "environment"}, COMPOSE_FINGERPRINT_SETTINGS=["CUSTOM_INPUT"])
    ns["_YAMLCONF_ATTRIBUTES"] = {
        "OPTIONS.child": {
            "value": "environment",
            "evalue": "environment",
            "source": "**ENVIRONMENT**",
            "history": [],
            "doc": [],
            "hide": False,
        }
    }
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'CUSTOM_INPUT': '{OPTIONS[child]}'}")]
    with pytest.raises(ImproperlyConfigured, match="CUSTOM_INPUT.*environment.*OPTIONS.child"):
        conf.configure(ns)


def test_native_append_expands_templates_in_project_authored_list(project):
    app, ns = project
    ns.update(BASE_DIR="/project", ITEMS=[], TEMPLATE="literal {not_a_reference}")
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'ITEMS:append': ['{BASE_DIR}/data']}")]
    conf.configure(ns)
    assert ns["ITEMS"] == ["/project/data"]
    assert ns["TEMPLATE"] == "literal {not_a_reference}"


def test_preexisting_native_raw_marker_remains_authoritative(project):
    app, ns = project
    ns.update(BASE_DIR="/project", ITEMS=[])
    ns["_YAMLCONF_ATTRIBUTES"] = {
        "ITEMS:raw": {
            "value": True,
            "evalue": True,
            "source": "project",
            "history": [],
            "doc": [],
            "hide": False,
        }
    }
    ns["INSTALLED_APPS"] = [app("SETTINGS = {'ITEMS:append': ['{BASE_DIR}/data']}")]
    conf.configure(ns)
    assert ns["ITEMS"] == ["{BASE_DIR}/data"]


def test_native_logging_stream_and_opaque_values_survive_container_isolation(project):
    app, ns = project
    ns.update(
        LOGGING={
            "version": 1,
            "handlers": {"console": {"class": "logging.StreamHandler", "stream": sys.stderr}},
        },
        CALLBACK_OPTIONS={"nested": [{"project"}]},
    )
    stream = ns["LOGGING"]["handlers"]["console"]["stream"]
    ns["INSTALLED_APPS"] = [
        app("""import sys
SETTINGS = {'ADDON_VALUE': 1}
def settings(namespace):
    assert namespace['LOGGING']['handlers']['console']['stream'] is sys.stderr
    namespace['LOGGING']['handlers']['console']['class'] = 'changed'
    namespace['CALLBACK_OPTIONS']['nested'][0].add('changed')
    return {}
""")
    ]
    conf.configure(ns)
    assert ns["LOGGING"]["handlers"]["console"]["stream"] is stream
    assert ns["LOGGING"]["handlers"]["console"]["class"] == "logging.StreamHandler"
    assert ns["CALLBACK_OPTIONS"] == {"nested": [{"project"}]}
    assert ns["ADDON_VALUE"] == 1


@pytest.mark.parametrize("template", ["{TASK_INPUT}", ["{TASK_INPUT}"], {"nested": ["{TASK_INPUT}"]}])
@pytest.mark.parametrize("retained_history", [False, True])
def test_native_loader_handoff_preserves_environment_reference_evidence(project, template, retained_history):
    from types import ModuleType

    import django_yamlconf

    app, ns = project
    ns["INSTALLED_APPS"] = [app()]
    ns["COMPOSE_FINGERPRINT_SETTINGS"] = ["CUSTOM_INPUT"]
    settings = ModuleType("loader.settings")
    settings.__dict__.update(ns)
    attributes = {}
    django_yamlconf.set_attr_value(attributes, settings, "loader", "TASK_INPUT", "default")
    django_yamlconf.set_attr_value(attributes, settings, "**ENVIRONMENT**", "TASK_INPUT", "environment")
    django_yamlconf.set_attr_value(attributes, settings, "loader", "CUSTOM_INPUT", template)
    if retained_history:
        django_yamlconf.set_attr_value(attributes, settings, "later-loader", "CUSTOM_INPUT", "constant")
    conf.expand_attribute_refs(attributes)
    django_yamlconf.inject_attr(attributes, settings)
    evidence = (
        attributes["CUSTOM_INPUT"]["history"][0][0]
        if retained_history
        else attributes["CUSTOM_INPUT"]["value"]
    )
    assert evidence == template
    ns.update(vars(settings))
    assert ns["_YAMLCONF_ATTRIBUTES"] is attributes
    with pytest.raises(ImproperlyConfigured, match="CUSTOM_INPUT.*environment.*TASK_INPUT"):
        conf.configure(ns)


def test_native_loader_adapter_preserves_opaque_values_and_template_containers():
    import django_yamlconf

    attributes = {}
    django_yamlconf.add_attr_info(attributes, "BASE_DIR", "/project", "loader")
    django_yamlconf.add_attr_info(attributes, "CUSTOM_INPUT", {"nested": ["{BASE_DIR}/data"]}, "loader")
    django_yamlconf.add_attr_info(attributes, "LOGGING", {"stream": sys.stderr}, "loader")
    original = attributes["CUSTOM_INPUT"]["value"]
    conf.expand_attribute_refs(attributes)
    assert attributes["CUSTOM_INPUT"]["value"] is original
    assert original == {"nested": ["{BASE_DIR}/data"]}
    assert attributes["CUSTOM_INPUT"]["evalue"] == {"nested": ["/project/data"]}
    assert attributes["LOGGING"]["evalue"]["stream"] is sys.stderr


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("VALUE", "VALUE"),
        ("OPTIONS.child", "OPTIONS"),
        ("ITEMS:append", "ITEMS"),
        ("OPTIONS.child:default", "OPTIONS"),
        ("OPTIONS:append.child", "OPTIONS"),
    ],
)
def test_setting_name_projects_native_root(key, expected):
    assert conf.setting_name(key) == expected


def test_configure_retains_resolved_dependencies_without_reactivation(project, monkeypatch):
    from pathlib import Path

    app, ns = project
    dependency = app()
    consumer = app()
    (Path(ns["COMPOSE_OUTPUT_DIR"]).parent / consumer / "apps.py").write_text(
        "from django.apps import AppConfig\n"
        f"class Config(AppConfig):\n    name = {consumer!r}\n    requires_apps = ({dependency!r},)\n"
    )
    ns["INSTALLED_APPS"] = [consumer]
    native_enabled_apps = conf.enabled_apps
    calls = []

    def resolve(*args, **kwargs):
        calls.append(args)
        return native_enabled_apps(*args, **kwargs)

    monkeypatch.setattr(conf, "enabled_apps", resolve)
    conf.configure(ns)
    assert len(calls) == 1
    snap = ns["COMPOSE_SNAPSHOT"]
    assert tuple(entry.name for entry in snap.entries) == (dependency, consumer)
    assert snap.dependencies == {dependency: (), consumer: (dependency,)}

    def refuse_reactivation(*args, **kwargs):
        raise AssertionError("runtime reader reactivated app selection")

    monkeypatch.setattr(conf, "enabled_apps", refuse_reactivation)
    assert snap.dependencies[consumer] == (dependency,)
    with pytest.raises(TypeError):
        snap.dependencies[consumer] = ()


def test_callbacks_and_runtime_share_resolved_native_closure(project, monkeypatch):
    from pathlib import Path

    from django.apps import AppConfig

    app, ns = project
    foreign = app()
    consumer = app("""SEEN = []
def settings(namespace):
    closure = namespace["COMPOSE_CLOSURE"]
    assert "COMPOSE_SNAPSHOT" not in namespace
    SEEN.append(closure)
    return {"APP_IDENTITIES": tuple((entry.name, entry.label, entry.path) for entry in closure.app_entries)}
""")
    root = Path(ns["COMPOSE_OUTPUT_DIR"]).parent
    (root / foreign / "apps.py").write_text(
        "from django.apps import AppConfig\n"
        f"class ForeignConfig(AppConfig):\n    name = {foreign!r}\n    label = 'custom_foreign'\n"
    )
    (root / consumer / "apps.py").write_text(
        "from django.apps import AppConfig\n"
        f"class ConsumerConfig(AppConfig):\n    name = {consumer!r}\n    requires_apps = ({foreign!r},)\n"
    )
    ns["INSTALLED_APPS"] = [consumer]
    native_create = AppConfig.create
    activations = []

    def create(entry):
        activations.append(entry)
        return native_create(entry)

    monkeypatch.setattr(AppConfig, "create", create)
    conf.configure(ns)
    closure = ns["COMPOSE_CLOSURE"]
    assert importlib.import_module(f"{consumer}.autoconfig").SEEN == [closure]
    assert importlib.import_module(f"{consumer}.autoconfig").SEEN[0] is closure
    assert closure.entries == (foreign, consumer)
    assert closure.dependencies == {foreign: (), consumer: (foreign,)}
    assert closure.app_entries == ns["COMPOSE_SNAPSHOT"].entries
    assert ns["APP_IDENTITIES"][0] == (foreign, "custom_foreign", str(root / foreign))
    assert closure.app_entries[0].config == f"{foreign}.apps.ForeignConfig"
    assert activations == [consumer, foreign]
    with pytest.raises(TypeError):
        closure.dependencies[consumer] = ()
    assert "COMPOSE_CLOSURE" not in ns["COMPOSE_SNAPSHOT"].settings
