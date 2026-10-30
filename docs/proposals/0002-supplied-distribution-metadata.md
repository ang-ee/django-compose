# 0002: index supplied native distribution metadata

Status: accepted.

Tooling may prepare package metadata through a build backend before installing
the package or making its Django app importable. It needs the same component
cardinality and collision checks as installed discovery.

Expose `components.index_components(distributions, *, group)` as the single
validator and constructor of the existing `InstalledComponents` result.
`installed_components(group)` selects Python's native installed-distribution
winners and delegates. Supplied inputs must have unique canonical distribution
names; there is no installation precedence to resolve duplicate source inputs.

Native `Distribution` and `EntryPoint` objects retain metadata ownership.
Metadata preparation, source retrieval and installation stay with the caller;
app activation still checks native AppConfig identity. No source catalogue,
metadata parser, alternate index type or dependency is introduced.

The API is additive. Existing installed-discovery callers need no migration;
the previous inline index construction moves to the public owner.

The public contract lives in [the API reference](../SPEC.md#package-metadata)
and the [component metadata guide](../components.md#inspect-package-metadata-without-importing-apps).
