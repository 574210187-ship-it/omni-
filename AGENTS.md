# Omni Phase-1 agent contract

## Hard execution boundary

This public repository is a **local-only** media processor. It has no remote
connector, credential, Bitable row selector, downloader, uploader, dispatcher,
queue, lock, or remote SHA-256 readback implementation.

- Never access Feishu, MCP, browser business pages, credentials, or any remote
  record while following this repository's Phase-1 instructions.
- A local PASS, a generated contract JSON, a historical document, or a previous
  conversation never authorizes a remote action.
- If a repository document conflicts with the current explicit task or
  `WORKBUDDY_TEST.md`, follow the current explicit task and stop at the local
  boundary.
- Do not claim remote automation capability from a successful local run.

## Local quality gates

Use only this v0.6 source tree. Do not select or reconstruct an earlier release
as a fallback.

Before encoding, require a verified local input video, reliable multi-frame
visible text, and verified sentence endpoints. A failure is a
`cut_conflict.json` HOLD, not an approximate cut. Never relax the `(3, 8]`
duration contract.

## Result language

Report a local PASS only when the local artifacts and local verification pass.
Report HOLD when visual evidence is insufficient. Neither result implies that
any remote system was read, changed, or is ready for automation.
