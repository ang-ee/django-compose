# django-compose

This is an independent Django composition library. Consumers depend on this
package; this package never imports a consumer framework, host, or product.
`docs/SPEC.md` owns the contract. Read it before changing a public seam.

Find the owner first. Django owns application identity and population, model
construction, fields, managers, migration state, execution, routing, and history.
This package owns composition, deterministic emission, native history retention
and verified database preparation. Do not add parallel registries or parsers.

Keep entrypoints thin, names consistent, and policy on the owning object.
Preserve native extension points and fail explicitly on competing declarations.
Delete obsolete paths rather than leave competing implementations. Changes must
agree with their docs and tests. Do not copy implementation files from another
repository; reconstruct behavior through the public native owners.

Use jj for version-control writes. Do not rewrite published history. Each agent
owns a separate workspace and a disjoint file set; never revert another lane.
Read files before and after editing. Keep scratch and evidence outside source.
Run `make check` and `make dist-check` before delivery. Report checks not run.
Never run or modify a consumer's live stack to validate this independent library.

Public docs contain durable contracts and contributor guidance. Private plans,
lane reports, and review receipts belong in the adjacent work-state workspace.
Publication requires explicit authorization; building and local commits do not
publish the package.
