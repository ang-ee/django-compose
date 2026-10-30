"""django-compose's durable provision lifecycle, independent of compiler generation."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _probe(source: str, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-c", source, *args],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(ROOT / "src"),
                        os.environ.get("PYTHONPATH"),
                    ),
                )
            ),
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


_SETUP = """
import sys
import django
from django.conf import settings
settings.configure(INSTALLED_APPS=[], SECRET_KEY='test', DATABASES={
    'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': sys.argv[1]},
    'other': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': sys.argv[2]},
})
django.setup()
from django.apps import apps
from django.db import connections
from django_compose.provision import ProvisionRecorder
"""


def test_provision_record_is_private_and_all_queries_use_selected_alias(tmp_path: Path) -> None:
    result = _probe(
        _SETUP
        + """
from django.db.migrations.state import ProjectState
recorder = ProvisionRecorder(connections['other'])
recorder.ensure_schema()
recorder.ensure_schema()
recorder.write('in_progress', 'a' * 64, 'compiler:1')
assert recorder.records.db == 'other'
assert recorder.read().state == 'in_progress'
assert not ProvisionRecorder(connections['default']).tables().__contains__('django_compose_provision')
assert recorder.Record._meta.apps is not apps
assert not any(label == 'django_compose_provision' for label, _name in ProjectState.from_apps(apps).models)
recorder.write('complete', 'a' * 64, 'compiler:1')
assert recorder.records.count() == 1
assert recorder.read().compiler == 'compiler:1'
print('private alias record verified')
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )
    assert result.stdout.strip() == "private alias record verified"


def test_crash_and_same_or_different_digest_retry_leave_durable_invalidation(tmp_path: Path) -> None:
    default = str(tmp_path / "default.sqlite3")
    other = str(tmp_path / "other.sqlite3")
    _probe(
        _SETUP
        + """
import os
recorder = ProvisionRecorder(connections['other'])
recorder.ensure_schema()
recorder.write('complete', 'old', 'compiler:1')
recorder.write('in_progress', 'same', 'compiler:1')
os._exit(0)
""",
        default,
        other,
    )
    result = _probe(
        _SETUP
        + """
recorder = ProvisionRecorder(connections['other'])
assert recorder.read().state == 'in_progress'
assert recorder.read().digest == 'same'
for digest in ('same', 'new'):
    recorder.write('in_progress', digest, 'compiler:2')
    assert recorder.read().state == 'in_progress'
    recorder.write('complete', digest, 'compiler:2')
    assert recorder.read().state == 'complete'
    assert recorder.read().digest == digest
print('durable retry verified')
""",
        default,
        other,
    )
    assert result.stdout.strip() == "durable retry verified"


def test_provision_record_refuses_outer_transaction(tmp_path: Path) -> None:
    result = _probe(
        _SETUP
        + """
from django.db import transaction
from django.core.management.base import CommandError
recorder = ProvisionRecorder(connections['other'])
recorder.ensure_schema()
with transaction.atomic(using='other'):
    try:
        recorder.write('in_progress', 'a', 'compiler:1')
    except CommandError:
        pass
    else:
        raise AssertionError('record invalidation would be rolled back')
assert recorder.read() is None
print('transaction refusal verified')
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )
    assert result.stdout.strip() == "transaction refusal verified"


def test_advisory_lock_is_unpooled_constant_and_physically_closed_on_hook_failure() -> None:
    result = _probe("""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django_compose.provision import advisory_lock, PROVISION_LOCK
physical = MagicMock()
cursor = physical.cursor.return_value.__enter__.return_value
connection = SimpleNamespace(
    vendor='postgresql', Database=SimpleNamespace(connect=MagicMock(return_value=physical)),
    get_connection_params=lambda: {'dbname':'test','pool':{'max_size':1}},
)
with patch.object(connection.Database, 'connect', return_value=physical) as connect:
    try:
        with advisory_lock(connection):
            raise RuntimeError('hook failed')
    except RuntimeError:
        pass
connect.assert_called_once_with(dbname='test')
assert physical.autocommit is True
assert cursor.execute.call_args_list[0].args == ('SELECT pg_advisory_lock(%s)', (PROVISION_LOCK,))
assert cursor.execute.call_args_list[1].args == ('SELECT pg_advisory_unlock(%s)', (PROVISION_LOCK,))
physical.close.assert_called_once()
print('unpooled failure cleanup verified')
""")
    assert result.stdout.strip() == "unpooled failure cleanup verified"


def test_hook_failure_keeps_in_progress_and_retry_completes_last(tmp_path: Path) -> None:
    result = _probe(
        _SETUP
        + """
from contextlib import contextmanager
from io import StringIO
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from django.apps import AppConfig
from django.core.management.base import BaseCommand, CommandError
from django_compose.provision import run
import django_compose.provision as provision
# The compiler lease is an explicit integration dependency. This tests base's
# ordering/durability around that boundary, not the external compiler lock.
events=[]
@contextmanager
def lease():
    events.append('lease-enter')
    try:
        yield SimpleNamespace(DIGEST='verified', COMPILER='compiler:1')
    finally:
        events.append('lease-exit')
@contextmanager
def lock(connection):
    events.append('lock-enter')
    try:
        yield
    finally:
        events.append('lock-exit')
provision.receipt_lease=lease
provision.verified_receipt=lambda:SimpleNamespace(DIGEST='verified', COMPILER='compiler:1')
module=ModuleType('native_hook')
module.__file__=sys.argv[1]
sys.modules[module.__name__]=module
apps.set_installed_apps(['native_hook'])
settings.MIGRATION_MODULES={'native_hook':None}
config=apps.get_app_config('native_hook')
recorder=ProvisionRecorder(connections['other'])
fail=[True]
def hook(context):
    assert context.database=='other'
    assert recorder.read().state=='in_progress'
    events.append('hook')
    if fail[0]:
        raise ValueError('hook failed')
config.compose_provision=hook
command=BaseCommand(stdout=StringIO(),stderr=StringIO())
options={'database':'other','check':False,'wait_db':0,'verbosity':0}
def step(command,args):
    assert recorder.read().state=='in_progress'
    events.append(args[0])
with (
    patch.object(provision,'advisory_lock',lock),
    patch.object(provision,'run_step',step),
    patch.object(provision,'migrate_and_analyze',lambda *args,**kwargs:events.append('migrate')),
):
    try:
        run(command,options)
    except CommandError as error:
        assert 'native_hook.compose_provision failed' in str(error)
    else:
        raise AssertionError('hook failure was ignored')
    assert recorder.read().state=='in_progress'
    assert events[:2]==['lease-enter','lock-enter']
    assert events[-2:]==['lock-exit','lease-exit']
    fail[0]=False
    events.clear()
    run(command,options)
    assert events==[
        'lease-enter','lock-enter','check_migrations','check_migrations','migrate','hook','check','lock-exit','lease-exit',
    ]
    assert recorder.read().state=='complete'
    assert recorder.read().digest=='verified'
print('failure retry lifecycle verified')
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )
    assert result.stdout.strip() == "failure retry lifecycle verified"


def test_readiness_reports_missing_in_progress_and_receipt_mismatch(tmp_path: Path) -> None:
    result = _probe(
        _SETUP
        + """
from types import ModuleType, SimpleNamespace
from django.core.management.base import CommandError
from django_compose.provision import require_provisioned
from contextlib import contextmanager
import django_compose.provision as provision
@contextmanager
def lease():
    yield SimpleNamespace(DIGEST='verified',COMPILER='compiler:1')
provision.receipt_lease=lease
recorder=ProvisionRecorder(connections['other'])
def refuses(expected):
    try:
        require_provisioned(using='other')
    except CommandError as error:
        assert expected in str(error),str(error)
    else:
        raise AssertionError('unready database accepted')
refuses('missing provision record')
assert not recorder.tables().__contains__('django_compose_provision')
recorder.ensure_schema()
recorder.write('in_progress','verified','compiler:1')
refuses('in_progress')
recorder.write('complete','stale','compiler:1')
refuses('digest differs')
recorder.write('complete','verified','compiler:0')
refuses('compiler version differs')
recorder.write('complete','verified','compiler:1')
require_provisioned(using='other')
assert not ProvisionRecorder(connections['default']).tables().__contains__('django_compose_provision')
print('readiness state verified')
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )
    assert result.stdout.strip() == "readiness state verified"


def test_provision_consumes_native_startup_receipt_and_lease(tmp_path: Path) -> None:
    """Actual native startup/commands feed base's selected-alias durable gate."""
    (tmp_path / "native_owner_settings.py").write_text(
        "from pathlib import Path\n"
        "from django_compose.conf import configure\n"
        "SECRET_KEY='base-owner-probe'\n"
        "INSTALLED_APPS=[]\n"
        "DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'\n"
        f"COMPOSE_OUTPUT_DIR=Path({str(tmp_path / 'native_output')!r})\n"
        "COMPOSE_OUTPUT_MODULE='native_output'\n"
        "DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3',"
        f"'NAME':{str(tmp_path / 'default.sqlite3')!r}}},"
        f"'other':{{'ENGINE':'django.db.backends.sqlite3','NAME':{str(tmp_path / 'other.sqlite3')!r}}}}}\n"
        "configure(globals())\n"
    )
    bootstrap = """
import os,sys
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='native_owner_settings'
os.environ.pop('DJANGO_COMPOSE_MODE',None)
"""
    _probe(
        bootstrap
        + """
from django_compose.management import execute
execute(['manage.py','compose'])
""",
        str(tmp_path),
    )
    result = _probe(
        bootstrap
        + """
from io import StringIO
from unittest.mock import patch
import django
django.setup()
from django.core.management.base import BaseCommand,CommandError
from django.db import connections
import django_compose.serving as serving
from django_compose.provision import ProvisionRecorder,require_provisioned,run
receipt=serving.verified_receipt()
try:
    require_provisioned(using='other')
except CommandError as error:
    assert 'missing provision record' in str(error)
else:
    raise AssertionError('unprovisioned native host was accepted')
command=BaseCommand(stdout=StringIO(),stderr=StringIO())
run(command,{'database':'other','check':False,'wait_db':0,'verbosity':0})
record=ProvisionRecorder(connections['other']).read()
assert record.state=='complete'
assert record.digest==receipt.DIGEST and record.compiler==receipt.COMPILER
assert not ProvisionRecorder(connections['default']).tables().__contains__('django_compose_provision')
require_provisioned(using='other')
print('native receipt selected-alias provision verified')
""",
        str(tmp_path),
    )
    assert result.stdout.strip() == "native receipt selected-alias provision verified"


def _environment(tmp_path: Path) -> dict[str, str]:
    source_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment.pop("DJANGO_COMPOSE_MODE", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(tmp_path), str(source_root / "src"), environment.get("PYTHONPATH", "")))
    )
    return environment


def _write_topology_host(root: Path) -> Path:
    """Write a native host recording Angee's real step and hook dispatch."""

    package = root / "probe"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (root / "manage.py").write_text(
        "import os\nfrom django_compose.management import execute\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'probe.settings')\nexecute()\n",
    )
    (package / "settings.py").write_text(
        "from pathlib import Path\nfrom django_compose.conf import configure\n"
        "SECRET_KEY='provision-process-probe'\nINSTALLED_APPS=['probe.apps.ProbeConfig']\n"
        f"COMPOSE_OUTPUT_DIR=Path({str(root / 'runtime')!r})\nCOMPOSE_OUTPUT_MODULE='runtime'\n"
        "DATABASES={'default': {'ENGINE':'django.db.backends.sqlite3',"
        f"'NAME':{str(root / 'probe.sqlite3')!r}}}}}\n"
        "configure(globals())\n",
    )
    (package / "apps.py").write_text("""import json
import os
from pathlib import Path
from django.apps import AppConfig

def record(step):
    with (Path(__file__).resolve().parent.parent / 'events.jsonl').open('a') as stream:
        stream.write(json.dumps({'step':step,'pid':os.getpid()})+'\\n')

class ProbeConfig(AppConfig):
    name='probe'
    def ready(self):
        from django_compose import provision
        original=provision.run_step
        def run_step(command,step):
            record(step)
            return original(command,step)
        provision.run_step=run_step
    def compose_provision(self,context):
        context.call('probe_step',database=context.database)
""")
    commands = package / "management/commands"
    commands.mkdir(parents=True)
    (commands.parent / "__init__.py").write_text("")
    (commands / "__init__.py").write_text("")
    (commands / "provision.py").write_text(
        "from django_compose.management.commands.provision import Command\n"
    )
    (commands / "probe_step.py").write_text("""from django.core.management.base import BaseCommand
from probe.apps import record
class Command(BaseCommand):
    requires_system_checks=[]
    def add_arguments(self,parser):
        parser.add_argument('--database')
    def handle(self,**options):
        record(['hook',options['database']])
""")
    return root / "manage.py"


def test_provision_runs_native_steps_and_addon_hooks_in_one_process(tmp_path: Path) -> None:
    """Native prebuilt checks/migrate and AppConfig hooks share selected command IO."""

    manage_py = _write_topology_host(tmp_path)
    environment = _environment(tmp_path)
    for arguments in (("compose",), ("provision", "--wait-db", "1")):
        result = subprocess.run(
            [sys.executable, str(manage_py), *arguments],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["step"] for event in events] == [
        ["check_migrations"],
        ["check_migrations", "--imports"],
        ["migrate", "--database", "default", "--noinput", "--skip-checks"],
        ["hook", "default"],
        ["check", "--database", "default"],
    ]
    assert len({event["pid"] for event in events}) == 1
    database = tmp_path / "probe.sqlite3"
    before = database.read_bytes()
    result = subprocess.run(
        [sys.executable, str(manage_py), "provision", "--check", "--wait-db", "0"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "provision: ready" in result.stdout
    assert database.read_bytes() == before
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == len(events)


def test_native_makemigrations_then_migrate_sees_new_file_in_same_process(tmp_path: Path) -> None:
    """Django reloads a migration file created earlier in the same process."""

    app = tmp_path / "native_app"
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "models.py").write_text(
        "from django.db import models\n\nclass Entry(models.Model):\n"
        "    value = models.CharField(max_length=20)\n",
        encoding="utf-8",
    )
    database = tmp_path / "native.sqlite3"
    script = tmp_path / "migrate_in_process.py"
    script.write_text(
        f"""from django.conf import settings
settings.configure(
    SECRET_KEY="probe",
    INSTALLED_APPS=["django.contrib.contenttypes", "native_app"],
    DATABASES={{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": {str(database)!r}}}}},
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
)
import django
django.setup()
from django.core.management import call_command
call_command("makemigrations", "native_app", verbosity=0, skip_checks=True)
call_command("migrate", verbosity=0, interactive=False, skip_checks=True)
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"native migration sequence failed:\n{result.stdout}\n{result.stderr}"
    assert (app / "migrations" / "0001_initial.py").is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "select name from sqlite_master where type = 'table' and name = 'native_app_entry'"
        ).fetchone() == ("native_app_entry",)


def test_upgrade_invalidates_before_table_rename_and_refuses_ambiguity(tmp_path: Path) -> None:
    _probe(
        _SETUP
        + """
from unittest.mock import patch
from django.core.management.base import CommandError
from django_compose.provision import TABLE, LEGACY_TABLE
recorder=ProvisionRecorder(connections['other'])
recorder.ensure_schema()
recorder.write('complete','current','compiler:1')
with recorder.connection.schema_editor() as editor:
    editor.alter_db_table(recorder.Record,TABLE,LEGACY_TABLE)
try:
    recorder.read()
except CommandError as error:
    assert '--upgrade-from base_provision' in str(error)
else:
    raise AssertionError('legacy readiness accepted')
original=recorder.connection.schema_editor
seen=[]
def editor(*args,**kwargs):
    with recorder.connection.cursor() as cursor:
        cursor.execute('SELECT state FROM base_provision')
        assert cursor.fetchone()[0]=='in_progress'
        seen.append('invalidated-before-rename')
    return original(*args,**kwargs)
with patch.object(recorder.connection,'schema_editor',editor):
    recorder.ensure_schema(LEGACY_TABLE)
assert seen==['invalidated-before-rename']
assert recorder.read().state=='in_progress'
assert recorder.read().digest=='current'
recorder.ensure_schema(LEGACY_TABLE)
assert recorder.records.count()==1
with recorder.connection.cursor() as cursor:
    cursor.execute('CREATE TABLE base_provision (id integer)')
for action in (recorder.read,lambda:recorder.ensure_schema(LEGACY_TABLE)):
    try:
        action()
    except CommandError as error:
        assert 'Both provision tables' in str(error)
    else:
        raise AssertionError('ambiguous tables accepted')
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )


def test_readiness_rejects_pending_and_inconsistent_history_without_writes(tmp_path: Path) -> None:
    _probe(
        _SETUP
        + """
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.core.management.base import CommandError
from django.db.migrations.exceptions import InconsistentMigrationHistory
import django_compose.provision as provision
@contextmanager
def lease():
    yield SimpleNamespace(DIGEST='verified', COMPILER='compiler:1')
recorder=ProvisionRecorder(connections['other'])
recorder.ensure_schema()
recorder.write('complete','verified','compiler:1')
executor=MagicMock()
executor.migration_plan.return_value=[('pending',False)]
with (
    patch.object(provision,'receipt_lease',lease),
    patch.object(provision,'MigrationExecutor',return_value=executor),
):
    for reason in ('unapplied migrations','inconsistent'):
        if reason=='inconsistent':
            executor.loader.check_consistent_history.side_effect=InconsistentMigrationHistory('inconsistent')
        try:
            provision.require_provisioned('other')
        except CommandError as error:
            assert reason in str(error)
        else:
            raise AssertionError('migration defect accepted')
assert recorder.read().state=='complete'
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )


def test_wait_and_native_command_check_policy() -> None:
    _probe("""
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.conf import settings
settings.configure()
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
import django_compose.provision as provision
command=BaseCommand(stdout=StringIO(),stderr=StringIO())
connection=MagicMock()
connection.ensure_connection.side_effect=[OperationalError('offline'),None]
with patch.object(provision,'connections',{'selected':connection}), patch.object(provision.time,'sleep'):
    provision.wait_for_database(command,2,using='selected')
assert connection.close.call_count==2
connection.ensure_connection.side_effect=OperationalError('offline')
with patch.object(provision,'connections',{'selected':connection}):
    try:
        provision.wait_for_database(command,0,using='selected')
    except CommandError as error:
        assert 'offline' in str(error) and 'selected' in str(error)
    else:
        raise AssertionError('offline database accepted')
for requires_checks, arguments, expected in (
    (True,['migrate'],{'skip_checks':False}),
    (True,['migrate','--skip-checks'],{'skip_checks':True}),
    (False,['check_migrations'],{}),
):
    native=SimpleNamespace(requires_system_checks=requires_checks)
    with (patch.object(provision,'get_commands',return_value={arguments[0]:'owner'}),
          patch.object(provision,'load_command_class',return_value=native),
          patch.object(provision,'call_command') as call):
        provision.run_step(command,arguments)
    assert call.call_args.kwargs=={'stdout':command.stdout,'stderr':command.stderr,**expected}
""")


def test_analyze_uses_native_plan_and_existing_managed_m2m_tables(tmp_path: Path) -> None:
    _probe(
        _SETUP
        + """
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from io import StringIO
from django.db import models
from django.core.management.base import BaseCommand
import django_compose.provision as provision
module=ModuleType('native_tables')
module.__file__=sys.argv[1]
sys.modules[module.__name__]=module
apps.set_installed_apps(['native_tables'])
class Entry(models.Model):
    related=models.ManyToManyField('self')
    class Meta:
        app_label='native_tables'
class External(models.Model):
    class Meta:
        app_label='native_tables'
        managed=False
plan=[(SimpleNamespace(app_label='native_tables'),False)]
existing=['native_tables_entry','native_tables_entry_related','native_tables_external']
assert provision.analyzed_tables(plan,existing)==[
    'native_tables_entry','native_tables_entry_related',
]
assert provision.analyzed_tables([(plan[0][0],True)],['native_tables_entry'])==[]
command=BaseCommand(stdout=StringIO(),stderr=StringIO())
with patch.object(provision,'run_step',side_effect=RuntimeError('failed')):
    try:
        provision.migrate_and_analyze(command,['migrate'])
    except RuntimeError:
        pass
# A failed call must not leave its per-call receiver behind.
assert not any(
    'migrate_and_analyze.<locals>.record' in repr(receiver)
    for receiver in provision.post_migrate.receivers
)
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )


def test_malformed_legacy_cutover_refuses_before_any_mutation(tmp_path: Path) -> None:
    _probe(
        _SETUP
        + """
from django.core.management.base import CommandError
from django_compose.provision import TABLE,LEGACY_TABLE
recorder=ProvisionRecorder(connections['other'])
with recorder.connection.cursor() as cursor:
    cursor.execute("CREATE TABLE base_provision (id integer, state text)")
    cursor.execute("INSERT INTO base_provision VALUES (1,'complete')")
try:
    recorder.ensure_schema(LEGACY_TABLE)
except CommandError as error:
    assert 'unexpected schema' in str(error)
else:
    raise AssertionError('malformed schema renamed')
assert TABLE not in recorder.tables()
with recorder.connection.cursor() as cursor:
    cursor.execute('SELECT * FROM base_provision')
    assert cursor.fetchall()==[(1,'complete')]
    cursor.execute('DROP TABLE base_provision')
recorder.ensure_schema()
recorder.write('complete','current','compiler:1')
with recorder.connection.schema_editor() as editor:
    editor.alter_db_table(recorder.Record,TABLE,LEGACY_TABLE)
with recorder.connection.cursor() as cursor:
    cursor.execute("UPDATE base_provision SET state='invalid'")
try:
    recorder.ensure_schema(LEGACY_TABLE)
except CommandError as error:
    assert 'invalid singleton state' in str(error)
else:
    raise AssertionError('invalid state renamed')
assert TABLE not in recorder.tables()
with recorder.connection.cursor() as cursor:
    cursor.execute('SELECT state FROM base_provision')
    assert cursor.fetchone()==('invalid',)
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )


def test_legacy_timestamp_is_validated_by_native_field_before_cutover(tmp_path: Path) -> None:
    _probe(
        _SETUP
        + """
from django.core.management.base import CommandError
from django_compose.provision import TABLE,LEGACY_TABLE
recorder=ProvisionRecorder(connections['other'])
with recorder.connection.cursor() as cursor:
    cursor.execute('CREATE TABLE base_provision ('
                   'id integer PRIMARY KEY, state varchar(16) NOT NULL, '
                   'digest varchar(64) NOT NULL, compiler varchar(255) NOT NULL, '
                   'updated TEXT NOT NULL)')
    cursor.execute('INSERT INTO base_provision VALUES (%s,%s,%s,%s,%s)',
                   [1,'complete','current','compiler:1','not-a-date'])
try:
    recorder.ensure_schema(LEGACY_TABLE)
except CommandError as error:
    assert 'invalid updated timestamp' in str(error)
else:
    raise AssertionError('malformed timestamp renamed')
assert TABLE not in recorder.tables()
with recorder.connection.cursor() as cursor:
    cursor.execute('SELECT state,updated FROM base_provision')
    assert cursor.fetchone()==('complete','not-a-date')
""",
        str(tmp_path / "default.sqlite3"),
        str(tmp_path / "other.sqlite3"),
    )
