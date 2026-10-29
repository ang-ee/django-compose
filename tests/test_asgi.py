"""Native ASGI events exercise routing and resource lifetime."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["asgi", "urls"])
def test_post_setup_import_guard(module):
    script = f"""
from django.conf import settings
settings.configure(INSTALLED_APPS=[])
from django.core.exceptions import AppRegistryNotReady
try:
    __import__("django_compose.{module}")
except AppRegistryNotReady:
    pass
else:
    raise AssertionError("post-setup seam imported before setup")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_asgi_composition():
    script = """
import asyncio, sys
from types import ModuleType
from contextlib import asynccontextmanager
from django.conf import settings
settings.configure(INSTALLED_APPS=[])
import django
django.setup()
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith((
            "django_compose.build", "django_compose.declarations", "channels", "uvicorn"
        )):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from django.apps import apps, AppConfig
from django_compose.asgi import AsgiContribution, _Application
from asgiref.testing import ApplicationCommunicator
from django.core.exceptions import ImproperlyConfigured
seen = []
async def mounted(scope, receive, send):
    seen.append(scope)
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})
@asynccontextmanager
async def first():
    seen.append("enter first")
    try: yield
    finally: seen.append("exit first")
@asynccontextmanager
async def second():
    seen.append("enter second")
    try: yield
    finally: seen.append("exit second")
class Config(AppConfig):
    name = "one"
    path = "/tmp"
    def compose_asgi(self): return self.value
one = Config("one", ModuleType("one"))
two = Config("two", ModuleType("two"))
one.value = AsgiContribution({"/api": mounted, "/api/deep": mounted}, (first,))
two.value = AsgiContribution(lifespans=(second,))
apps.get_app_configs = lambda: [one, two]
app = _Application()
async def main():
    scope = {"type": "http", "path": "/base/api/deep/item", "root_path": "/base", "query_string": b"a=1"}
    comm = ApplicationCommunicator(app, scope)
    assert (await comm.receive_output())["status"] == 204
    await comm.receive_output()
    await comm.wait()
    assert seen.pop() == dict(scope, root_path="/base/api/deep")
    async def fallback(scope, receive, send):
        await send({"type": "http.response.start", "status": 299, "headers": []})
    app.http = fallback
    comm = ApplicationCommunicator(app, dict(scope, path="/base/apix"))
    assert (await comm.receive_output())["status"] == 299
    await comm.wait()
    comm = ApplicationCommunicator(app, {"type": "lifespan"})
    await comm.send_input({"type": "lifespan.startup"})
    assert (await comm.receive_output())["type"] == "lifespan.startup.complete"
    await comm.send_input({"type": "lifespan.shutdown"})
    assert (await comm.receive_output())["type"] == "lifespan.shutdown.complete"
    await comm.wait()
    assert seen == ["enter first", "enter second", "exit second", "exit first"]
    seen.clear()
    @asynccontextmanager
    async def broken():
        raise RuntimeError("broken startup")
        yield
    app.lifespans = [first, broken]
    comm = ApplicationCommunicator(app, {"type": "lifespan"})
    await comm.send_input({"type": "lifespan.startup"})
    assert (await comm.receive_output())["type"] == "lifespan.startup.failed"
    await comm.wait()
    assert seen == ["enter first", "exit first"]
    @asynccontextmanager
    async def broken_exit():
        yield
        raise RuntimeError("broken shutdown")
    app.lifespans = [broken_exit]
    comm = ApplicationCommunicator(app, {"type": "lifespan"})
    await comm.send_input({"type": "lifespan.startup"})
    await comm.receive_output()
    await comm.send_input({"type": "lifespan.shutdown"})
    assert (await comm.receive_output())["type"] == "lifespan.shutdown.failed"
    await comm.wait()
    seen.clear()
    app.lifespans = [first]
    comm = ApplicationCommunicator(app, {"type": "lifespan"})
    await comm.send_input({"type": "lifespan.startup"})
    await comm.receive_output()
    comm.stop()
    await asyncio.sleep(0)
    assert seen == ["enter first", "exit first"]
    comm = ApplicationCommunicator(app, {"type": "websocket"})
    try: await comm.wait()
    except ValueError: pass
    else: raise AssertionError("unsupported scope accepted")
asyncio.run(main())
two.value = AsgiContribution({"/api": mounted})
try: _Application()
except ImproperlyConfigured as error: assert "one" in str(error) and "two" in str(error)
else: raise AssertionError("duplicate mount accepted")
two.value = AsgiContribution(protocols={"http": mounted})
try: _Application()
except ImproperlyConfigured as error: assert "Django HTTP fallback" in str(error) and "two" in str(error)
else: raise AssertionError("reserved protocol accepted")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
