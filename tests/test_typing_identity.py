"""Independent plugins read selected identities through native TypeInfo metadata."""

from test_typing import check, host

MODELS = """
from django.db import models
from django_compose.models import Composable
class Root(Composable):
    class Meta:
        abstract = True
class First(Root, concrete=True):
    title = models.CharField(max_length=20)
    class Meta:
        abstract = True
class Policy(Composable, extends="modelapp.Root"):
    code = models.IntegerField(default=0)
    class Meta:
        abstract = True
class Unselected(Composable):
    class Meta:
        abstract = True
"""

SECOND = """
class Second(Root, concrete=True):
    class Meta:
        abstract = True
"""

PROBE = """
from mypy.plugin import Plugin
from mypy.types import Instance, LiteralType, get_proper_type
class Probe(Plugin):
    def get_function_hook(self, fullname):
        return self.identity if fullname == "probe.identity" else None
    def identity(self, ctx):
        subject = get_proper_type(ctx.arg_types[0][0])
        assert isinstance(subject, Instance)
        value = subject.type.metadata.get("django_compose", {}).get("runtime_model", "unresolved")
        return LiteralType(value, ctx.api.named_generic_type("builtins.str", []))
def plugin(version):
    return Probe
"""

CONSUMER = """
from typing import Literal, assert_type
from modelapp.models import First, Root, Policy, Unselected
from probe import identity

def consume(first: First, root: Root, policy: Policy, unselected: Unselected) -> None:
    assert_type(identity(first), Literal["typed_runtime.modelapp.models.First"])
    assert_type(identity(root), Literal["unresolved"])
    assert_type(identity(unselected), Literal["unresolved"])
    assert_type(identity(policy), Literal["typed_runtime.modelapp.models.First"])
"""


def test_selected_identity_channel_is_independent_and_invalidates_ambiguity(tmp_path):
    config, env = host(tmp_path)
    models = tmp_path / "modelapp/models.py"
    models.write_text(MODELS)
    (tmp_path / "probe.py").write_text("def identity(subject: object) -> str:\n    return ''\n")
    (tmp_path / "probe_plugin.py").write_text(PROBE)
    config.write_text(
        config.read_text().replace(
            "plugins = django_compose.typing", "plugins = django_compose.typing, probe_plugin"
        )
    )
    consumer = tmp_path / "consumer.py"
    consumer.write_text(CONSUMER)
    for _ in range(2):
        result = check(config, env, consumer)
        assert result.returncode == 0, result.stdout + result.stderr
    models.write_text(MODELS + SECOND)
    ambiguous = check(config, env, consumer)
    assert ambiguous.returncode == 1, ambiguous.stdout + ambiguous.stderr
    assert "Literal['unresolved']" in ambiguous.stdout
    assert ambiguous.stdout.count("[assert-type]") == 1, ambiguous.stdout + ambiguous.stderr
    consumer.write_text(
        CONSUMER.replace(
            'identity(policy), Literal["typed_runtime.modelapp.models.First"]',
            'identity(policy), Literal["unresolved"]',
        )
    )
    result = check(config, env, consumer)
    assert result.returncode == 0, result.stdout + result.stderr
    models.write_text(MODELS)
    stale = check(config, env, consumer)
    assert stale.returncode == 1, stale.stdout + stale.stderr
    assert stale.stdout.count("[assert-type]") == 1, stale.stdout + stale.stderr
    consumer.write_text(CONSUMER)
    restored = check(config, env, consumer)
    assert restored.returncode == 0, restored.stdout + restored.stderr
