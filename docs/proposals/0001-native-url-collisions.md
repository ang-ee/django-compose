# 0001: preserve native URL converter semantics

Status: accepted.

Django owns URL matching through native patterns, converters and ordered
resolution. A converter's `to_python()` may raise `ValueError` to reject a regex
match, allowing a later pattern to handle the request. Two converters can
therefore share a regex while accepting disjoint inputs.

The composition check `django_compose.E001` rejects identical native matching
chains, naming both contributing apps. Include boundaries, native matcher types,
compiled regex text and flags, endpoint behavior, capture names and converter
instances must agree. Reverse namespaces do not distinguish matches. Regex
equality alone does not prove equivalence when converters differ; differently
expressed chains and custom matchers retain Django's normal ordered resolution.
The check does not execute converters or try to solve general regex equivalence.

This removes the regex-only approximation without adding another router,
registry, setting or dependency. Identical native chains remain errors,
including duplicates reached through different reverse namespaces.
Valid custom-converter routes previously rejected need no caller migration.

The implemented contract belongs in [the API reference](../SPEC.md#serving-composition)
and the native check's docstring. Django's [converter documentation](https://docs.djangoproject.com/en/6.1/topics/http/urls/#registering-custom-path-converters)
owns matching behavior.
