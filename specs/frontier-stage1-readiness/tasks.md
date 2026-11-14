# Tasks: Frontier (agy-gemini) Stage 1 Readiness and Boundary Hardening

Spec ID: FRNT · Status: approved by Ky on 2026-11-13

## Phase 0: Pre-Implementation Empirical Container & Baseline Checks ($0 Offline)

- [x] 1. Build the baseline bundle (`uv run python -m kit build .`) offline inside `ple-bench` to measure the current `/opt/harness` packed and unpacked size against `MAX_BUNDLE_BYTES` (`2 GiB`) (FRNT-6.3) — *Done (`174.01 MiB` packed / `561.44 MiB` unpacked / `10,700` members)*
- [x] 2. Run `/opt/harness/bin/agy --help` and inspect `agy` 1.4.10 CLI/settings behavior inside the built `linux/amd64` serve container to determine exact non-interactive policy flags replacing `--dangerously-skip-permissions`, verify `"allowNonWorkspaceAccess": false` behavior with `$HOME/.gemini`, and confirm `tools: []` enforcement (FRNT-1.1, FRNT-1.2, FRNT-1.3) — *Done (findings recorded in `design.md`)*
- [x] 3. Test `agy` 1.4.10 `mcp_config.json` handling of `"eager": false` vs omitted `"tools"` / tool caps using a local dummy MCP server so prompt-token mitigation matches actual binary behavior (FRNT-3.3) — *Done (findings recorded in `design.md`)*
- [x] 4. Probe representative PLE task container images offline for installed PDF rasterizers (`pdftoppm`, `mutool`, `gs`, `fitz`, `pypdfium2`) to confirm runtime discovery order and fallback paths (FRNT-4.1, FRNT-4.4) — *Done for cached base images (`plebench-serve:d61dfd66aaacd0a6`, `ubuntu:24.04`, `plebench-builder:5f1332de50594db5`/`debian:bookworm`, `python:3.12-slim`: all none); specialized Harbor/HF task containers not yet cached locally (`~/.cache/plebench/` has no task images) and remain to be checked when task images are pulled*

## Phase 1: Stage 1 Licensing & Packaging Foundation

- [x] 5. Add the standard Apache License Version 2.0 `LICENSE` file at `/Users/ky/physical-engineering/agy-gemini/LICENSE` matching existing file headers (FRNT-6.1) — *Done (`LICENSE` added, verified by `tests/test_stage1_packaging.py`)*
- [x] 6. Verify `submission.yaml` Stage 2 settings (`google-cloud/gemini-4-argon`, `install.sh`, `serve.py`, quoted `env`), LF line endings on `install.sh`, clean `requirements.txt` isolation via `uv`, zero secrets/LFS/excluded `artifacts*` dirs, and no runtime reads of `artifacts/` or `eval/` (FRNT-6.2, FRNT-6.3, FRNT-6.4, FRNT-6.5) — *Done (verified by `tests/test_stage1_packaging.py` & `kit validate`)*

## Phase 2: Container-Scoped Permissions & MCP Boundary Containment

- [x] 7. Update `serve.py` `SETTINGS`, `AGENT`, and `solve()` CLI invocation to keep `tools: []` disabled, set `allowNonWorkspaceAccess` to `False` (or verified scratch-only policy), replace blanket `--dangerously-skip-permissions` with explicit non-interactive policy controls, and fail closed with `session.log` diagnostics if unsupported while preserving arbitrary task-container commands in `mcp_{bridge}_shell` (FRNT-1.1, FRNT-1.2, FRNT-1.3, FRNT-1.4) — *Done (`toolPermission: strict`, `allowNonWorkspaceAccess: False`, `enableTerminalSandbox: True`, `--sandbox`, `validate_serve_policy()` + runtime fail-closed checks)*
- [x] 8. Harden `taskbridge.py` (`mcp_allows`, `Relay._mcp`, `Relay._route`) to require `http`/`https` schemes and non-empty matching hosts alongside traversal checks, enforce `MAX_HTTP_BYTES`/`MAX_BODY`/`MAX_FRAME`/`MAX_TIMEOUT_SEC`, and emit structured audit logs without leaking `Relay.token` (FRNT-2.2, FRNT-2.3, FRNT-2.5) — *Done*
- [x] 9. Harden `task_mcp.py` dispatch boundary with `validate_tool_args()` schema/type checking returning `isError: true` on bad args, sanitized minimal `mcp_config.json` `"env"` in `serve.py`, `redact_secrets()` scrubbing API keys/URLs/tokens from responses and logs, and structured per-call audit logging (FRNT-2.1, FRNT-2.2, FRNT-2.4, FRNT-2.5) — *Done*
- [x] 10. Update `serve.py` `configure()` to validate `session.mcp_servers` descriptors, filter task MCP tools through configurable allowlist/denylist policy, keep core `TASK_TOOLS` eager while bounding/deferring external task MCP catalogs, reject filtered calls, and log registered/filtered/deferred counts (FRNT-3.1, FRNT-3.2, FRNT-3.3, FRNT-3.4) — *Done (verified in `tests/test_permissions_boundary.py`)*

## Phase 3: Multimodal PDF and Binary Drawing Inspection

- [x] 11. Extend `task_mcp.py` `TOOLS` schema (`read_file` optional `pages` parameter) and implement deterministic `%PDF-` detection (including PDFs exceeding the `10 MB` single-read limit) routing to in-container `_read_pdf()` via `relay("exec", ...)` + temp PNG retrieval via `read_bytes()` + temp cleanup (FRNT-4.1, FRNT-4.2, FRNT-4.3) — *Done*
- [x] 12. Implement `MAX_PDF_PAGES` (default `5`), explicit `pages` parsing, deterministic DPI/format downscaling to stay within `MAX_IMAGE` (`5 MiB`) and `MAX_FRAME` (`16 MiB`), and graceful actionable fallback messages when renderers are missing or PDFs are corrupt/encrypted/out-of-range (FRNT-4.2, FRNT-4.3, FRNT-4.4) — *Done*
- [x] 13. Preserve exact `IMAGE_MAGIC` (`PNG`/`JPEG`/`GIF`/`WEBP`) and NUL-free text/ASCII CAD behavior while enhancing non-PDF binary detection (`.dwg`, `.rvt`, `.zip`, etc.) to return deterministic format/size metadata and `shell` inspection hints (FRNT-4.5, FRNT-4.6) — *Done*

## Phase 4: Programmatic Deliverable Verification Gate

- [x] 14. Implement task-agnostic candidate deliverable discovery and deterministic `$0` format validators (existence, `size > 0`, `.zip` integrity, `.json`/`.csv`/`.xml`/`.svg`/`.py`/`.pdf` syntax checks) executed via `session.exec(...)` inside the task container (FRNT-5.1, FRNT-5.2) — *Done (`deliverable_gate.py`, `Relay.written_paths` in `taskbridge.py`)*
- [x] 15. Integrate the post-run verification gate and bounded `--conversation` repair loop (`MAX_REPAIR_ATTEMPTS`, `STOP_BEFORE_SEC + REPAIR_RESERVE_SEC`, `session.cancelled` guard, structured `session.log` summary) into `serve.py` `solve()` before exit (FRNT-5.1, FRNT-5.3, FRNT-5.4) — *Done (`_run_agy_turn`, `_run_verification_and_repair` in `serve.py`)*

## Phase 5: Offline $0 Test Suite & Stage 1 Readiness Verification

- [x] 16. Write and run offline `$0` boundary tests in `tests/test_permissions_boundary.py` covering positive authorized MCP flows and negative escape, invalid-arg, bad-URL, secret-redaction, policy-fail-closed, and MCP tool-scoping checks (FRNT-1.1, FRNT-1.2, FRNT-1.3, FRNT-1.4, FRNT-1.5, FRNT-2.1, FRNT-2.2, FRNT-2.3, FRNT-2.4, FRNT-2.5, FRNT-3.1, FRNT-3.2, FRNT-3.3, FRNT-3.4) — *Done (14/14 passing)*
- [x] 17. Write and run offline `$0` PDF and drawing inspection tests in `tests/test_pdf_inspection.py` covering multi-page rendering, >10 MB PDFs, DPI downscaling, missing renderer/corrupt PDF fallbacks, `IMAGE_MAGIC` regressions, and binary CAD metadata summaries (FRNT-4.1, FRNT-4.2, FRNT-4.3, FRNT-4.4, FRNT-4.5, FRNT-4.6) — *Done (6/6 passing)*
- [x] 18. Write and run offline `$0` verification gate tests in `tests/test_deliverable_gate.py` covering candidate discovery, format checks, bounded `--conversation` repair trigger, exhaustion/low-budget exit, and cancellation safety (FRNT-5.1, FRNT-5.2, FRNT-5.3, FRNT-5.4) — *Done (4/4 passing)*
- [x] 19. Write and run offline `$0` packaging tests in `tests/test_stage1_packaging.py` plus `uv run python -m kit validate .` and `kit check --no-smoke` with `../scripts/mock_proxy.py` (no paid API calls; see also `../scripts/check_submission.sh`) and verify final bundle size `< 2 GiB` (`<= 512 MiB` target) (FRNT-6.1, FRNT-6.2, FRNT-6.3, FRNT-6.4, FRNT-6.5)
