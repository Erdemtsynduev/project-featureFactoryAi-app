`opencode-2.0.16.jsonl` is captured stdout, not a synthetic stream. Source:
`reports/refactor-live/opencode/1790538776755332600/workspace/.sdd-engine/smoke/987e75e21cd145a7a4e5db7f12c57698/stdout.log`.
Windows, OpenCode 2.0.16, 2026-09-27. Durable host receipt: exit 0 after about
40.3 seconds (120 second deadline). The final assistant message is fenced JSON;
there is no trailing `step_finish(reason=stop)`. Replay proves parser compatibility,
not authentication, provider availability, or unattended operation.
