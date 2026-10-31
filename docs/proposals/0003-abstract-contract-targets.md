# 0003: abstract contract extension targets

Status: accepted.

Implemented through the existing `extends` declaration; the specification owns
the current contract.

`ModelComposition` already owns discovery, additive extension edges and emission.
Its concrete-only target lookup required a separate donor for every generated
identity, even when selected models shared one native abstract contract. Django
owns the abstract ancestry, class MRO, descriptors, managers and migration state.

An app-owned abstract class explicitly declaring `concrete=False`, or directly
inheriting `Composable`, is addressable through its selected AppConfig's native
`models_module` exports. It generates no concrete identity of its own. Existing
`extends="label.Contract"` donors expand over selected source ancestry once.
Inherited markers and foreign reexports do not create declarations. A known
selected contract can have no participants; an unknown or unselected target fails.
Existing concrete-target matching remains unchanged.

Generated models retain donor/source/parent base order and native MRO. A donor
already contributed to a concrete ancestor is inherited there. Identical native
abstract fields repeated by an MTI source reuse concrete-parent descriptors so
fields and M2M through tables remain stored once. Different-origin field and
constraint collisions continue to fail. Static donor fields project to all
selected targets without choosing one target as the donor's identity.

Callers can replace repeated concrete donors with one abstract-contract donor;
they need no runtime registry, generated-class mutation or new class keyword.
Discovery removes concrete-only target validation, and typing removes its
last-target overwrite. Existing imports and concrete declarations remain valid.
An abstract donor's same-named manager must not discard a source factory or its
queryset: competing managers fail. Combining arbitrary manager/queryset policies
is a separate public design and is not introduced by this change.
