# 0003: explicit adoption of a retained initial baseline

Status: accepted.

First-authoring baseline satisfaction deliberately does not exempt existing
native initials. Projects retaining an initial authored from their final schema
need an explicit way to associate that history with an already opted-in reusable
transition without rewriting history or claiming that its operations executed.

Extend the existing `TransitionBinding` owner with one-origin adoption against
a named retained native initial. Invoking the action attests that this anchor is
the baseline covered by the transition owner's existing satisfaction assertion.
Native active-history identity, initial/root scope, unique-leaf ancestry and two
false guard evaluations constrain the action; they do not prove historical data
completion. Whole-app model cleanliness is outside this action.

Reuse the frozen declaration/source snapshot, native MigrationLoader and graph,
shared guard and native-empty selection, existing renderer/seals and exclusive
publication rollback. Seal the named anchor and its exact source hash for
idempotent provenance. A thin management command dispatches to that owner. No
new registry, source scanner, database inference or recorder policy is introduced.

The change is additive. Ordinary false-guard authoring, first-authoring
satisfaction and reset retain their contracts. Only an explicitly selected
pending origin is adopted; existing origins are never silently converted and
unrelated pending sources are not imported. Existing source bytes and native
history remain immutable; no existing migration needs a format migration. The
ordinary binding path shares guard, native-empty and publication owners with
adoption, replacing the former inline-only implementations.

The current API lives in the [specification](../SPEC.md#native-history-and-transitions)
and the [migration guide](../migrations.md#adopt-an-explicit-retained-initial-baseline).
