# Compatibility and adoption

The initial package implements the contract in [SPEC.md](SPEC.md) independently.
Its validation boundary is a plain Django host and installed distribution.

Adopting it in an existing consumer is a separate change:

1. Replace consumer compiler imports and configuration subclasses through the
   public package contracts. Keep host configuration and product integrations in
   their existing owners.
2. Preserve historical Python import paths used by immutable migration files as
   narrow aliases. Check existing migration origin metadata without rewriting it.
3. Regenerate runtime artifacts through the new compiler and run the consumer's
   full model, schema, migration, typing and serving checks.
4. Delete the superseded consumer compiler and stale guidance once every caller
   has moved. A remaining duplicate implementation means adoption is incomplete.

The package does not migrate a live stack, change operator jobs, acquire sources,
replace marketplace policy or publish releases. Those remain explicit consumer
and release operations. Supporting another Django series requires running the
native lifecycle, migration and optional typing matrix for that series first.

## Pending host integration

The broader host contract remains staged. Settings contribution through
`configure()` and django-yamlconf, URL and ASGI composition, artifact phases,
the shared startup lock/reloader protocol and a revised tombstone module
location require their own implementation and verification. Existing receipt
and lifecycle behavior remains the supported contract until those changes land.
