# Architecture rationale

Feature Factory AI is an independently packaged deterministic control plane.
Pure transitions decide ordering, retries and acceptance. Runtime adapters own
processes, clocks, filesystem effects and installed provider protocols. Durable
receipts and explicit ownership make recovery reviewable.

These are architectural choices, not comparative reliability or token-saving
claims. Runtime evidence lives in [REFACTOR-VERIFICATION.md](REFACTOR-VERIFICATION.md);
operational findings live in [OPERATIONS-LESSONS.md](OPERATIONS-LESSONS.md).

Historical source comparisons are archived outside the distributable documentation.
No third-party runtime source or assets were copied. Future source adoption must
retain the upstream licence and provenance. Provider integrations are explicit
adapters, not dependencies on another orchestration application.
