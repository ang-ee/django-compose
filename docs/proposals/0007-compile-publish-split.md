# 0007: compile before publishing generated models

Status: accepted.

The build compiler owns deterministic source emission; `BuildPlan` owns output
publication and its lock. Previously `BuildPlan.emit()` mixed declaration
discovery and rendering with receipt writes, file publication, generated-module
imports and registry validation. That prevented callers from inspecting the
compiled sources without entering the publication protocol.

`build.compile()` now returns a frozen `Build` containing the native model
composition, literal declarations, generated source bytes and model labels. It
reuses `ModelComposition.discover`, `model_declaration` and
`rendering.render_models`; Django still owns app population and model
registration. `BuildPlan` supplies retained history and migration-initializer
facts from disk, then keeps the receipt, `GeneratedTree`, lock, generated imports
and validation steps.

User-facing change: none. Build and check output and public entrypoint
signatures remain the same. The inline discovery, generated-config template and
source-rendering block in `BuildPlan.emit()` is removed.
