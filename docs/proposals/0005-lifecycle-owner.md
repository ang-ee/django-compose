# Proposal 0005: Lifecycle and output ownership

The process lifecycle owns retained startup verification, writer wait, active
build cleanup, pending artifacts, and resolved integrations. One output value
binds the generated package directory to its import name. Previously these
facts lived in serving module globals or were reread independently from mutable
settings; check mode verified the same receipt during configuration and again
during native population.

The public serving functions remain thin entrypoints. Verification uses the
existing literal receipt reader and native filesystem lock, and composition
continues through Django's app population and migration APIs. Configure now
verifies check mode once and refuses an importable output module whose package
directory disagrees with `COMPOSE_OUTPUT_DIR`. Later composition reads frozen
`COMPOSE_*` values from `COMPOSE_SNAPSHOT`.

Projects should set `COMPOSE_OUTPUT_DIR` and `COMPOSE_OUTPUT_MODULE` to the same
package, then recompose if their prior output was stale. No receipt migration is
needed. The serving lifecycle globals, repeated check verification, and
`DJANGO_COMPOSE_WAIT` environment handoff are removed.
