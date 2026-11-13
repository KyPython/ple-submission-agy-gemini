# Design: Frontier (agy-gemini) Stage 1 Readiness and Boundary Hardening

Spec ID: FRNT · Status: draft (AI-prepared by Antigravity; awaiting Ky review)
Author: Antigravity (drafted at Ky's direction during Kiro credit exhaustion; not yet approved by Ky).

## Overview

This design hardens `agy-gemini` across PLE's two-container trust boundary while closing the multimodal `.pdf` inspection gap, replacing prompt-only deliverable checks with a deterministic `$0` post-run verification gate, and bringing repository packaging into Kaggle Stage 1 / `ple-bench` `kit` compliance.

Key architectural constraints carried from `ple-bench/kit/README.md` and `ple-bench/docs/protocol.md`:
- The **serve container** runs `serve.py` (`uid 1000`, read-only root, `/opt/harness` bundle, `1 GB` `/tmp`, `1 GB` `$HOME`, egress strictly via `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` to the declared model endpoint). It holds `GEMINI_API_KEY` / `MODEL_API_KEY` and `Relay.token`.
- The **task container** runs offline with the engineering workspace and optional task-provided Streamable HTTP MCP servers (`session.mcp_servers`).
- Communication between `serve.py` and the task container occurs exclusively over `taskbridge.Session` WebSocket frames (`MAX_FRAME = 16 MiB`, max `8` concurrent in-flight requests, max `20` malformed frames per task, `10 s` cancel grace window).

## Architecture / components

```
Serve Container (uid 1000, RO root, egress proxy -> model API only)
  ├── serve.py
  │   ├── configure(): validates session.mcp_servers, filters/bounds MCP tools,
  │   │                writes settings.json (allowNonWorkspaceAccess=False),
  │   │                task-agent.md (tools: []), mcp_config.json
  │   ├── solve(): runs agy subprocess with explicit policy (no blanket bypass),
  │   │            tracks Relay write targets / pre-post workspace baseline,
  │   │            invokes Deliverable Verification Gate + bounded --conversation repair
  │   └── taskbridge.Relay (127.0.0.1:<port>/<token>):
  │       ├── validates HTTP framing, size caps, mcp_allows() scheme/host/path
  │       └── emits structured audit logs (never leaking token/API keys)
  └── task_mcp.py (stdio MCP child process with minimal sanitized env)
      ├── validate_tool_args() + redact_secrets() + audit_log()
      ├── shell() -> relay("exec") [headless task container, no static cmd denylist]
      ├── read_file() -> IMAGE_MAGIC | PDF in-container render | CAD/binary metadata | text
      └── write_file() / edit_file() -> relay("write_file") / relay("read_file")

Task Container (no internet, engineering CLIs + optional task MCP servers)
  ├── /app / workdir (deliverables created & modified here)
  ├── PDF rasterizers (pdftoppm / mutool / gs / Python PDF lib, discovered at runtime)
  └── Task-specific MCP servers (proxied via Relay._mcp -> session.http)
```

### Component 1: Serve-Container Containment & Policy Enforcement (`serve.py`, `task_mcp.py`, `taskbridge.py`)
1. **Serve-Container Config Lock-Down (`serve.py`):**
   - `AGENT` retains `tools: []` so `agy`'s built-in local serve-container tools remain disabled (`FRNT-1.1`).
   - `SETTINGS` sets `"allowNonWorkspaceAccess": False` (and preserves `"enableTelemetry": False`, `"showFeedbackSurvey": False`, `"showTips": False`, `"notifications": False`) (`FRNT-1.2`).
   - Replace blanket `"--dangerously-skip-permissions"` in `solve()` `argv` with `agy` 1.4.10's supported explicit non-interactive policy flags or per-tool approval configuration scoped strictly to configured MCP bridge/task tools (`FRNT-1.2`).
   - Add an explicit pre-launch policy validation step (and post-startup `stream-json` error detection) so an unsupported CLI flag, ignored policy schema, or attempted serve-container local execution outside scratch `home` fails closed with `session.log` diagnostics and raises `RuntimeError` (`FRNT-1.3`).
   - Because `mcp_{bridge}_shell` executes inside the isolated offline task container (`exec`), `task_mcp.shell` continues passing arbitrary engineering commands (`python3`, `unzip`, CAD/BIM/FEA CLIs) without static command blacklists or interactive prompts (`FRNT-1.4`).
2. **Sanitized Child Environment (`serve.py` -> `task_mcp.py`):**
   - `mcp_config.json` passes only `TASK_RELAY_URL`, `TASK_WORKDIR`, and minimal runtime vars (e.g., `PATH`, `LANG`, `PYTHONUTF8`) into `task_mcp.py`, avoiding passing `GEMINI_API_KEY` / `MODEL_API_KEY` directly in `mcp_config.json` `"env"`.
   - `task_mcp.py` implements `redact_secrets(text)` using a fixed set of sensitive environment variable names (`GEMINI_API_KEY`, `MODEL_API_KEY`, `GOOGLE_GEMINI_BASE_URL`, `MODEL_BASE_URL`, `TASK_RELAY_URL`) plus any hex relay token pattern extracted from `RELAY`, scrubbing matches before returning MCP text or writing audit logs (`FRNT-2.4`).
3. **Dispatch-Boundary Validation & Audit Logging (`task_mcp.py`, `taskbridge.py`):**
   - `task_mcp.py` validates every `tools/call` before touching `HANDLERS` (`FRNT-2.1`): `arguments` must be a `dict`; unknown parameter keys are rejected; required keys must be present; `path` and `command` must be non-empty `str` without NUL bytes or unpaired UTF-16 surrogates; `offset`, `limit`, `timeout_sec` must be integers within bounds (no `bool` disguised as `int`); `replace_all` must be `bool`. Invalid inputs return `{"content": text(...), "isError": True}` without hitting `relay()`.
   - `task_mcp.py` validates relay payload limits before posting (`MAX_WRITE_BYTES` / `MAX_BODY`, `1..MAX_TIMEOUT_SEC`, `MAX_TEXT`, `MAX_IMAGE`) (`FRNT-2.2`).
   - `taskbridge.mcp_allows(url, servers)` strengthens URL parsing with `urllib.parse.urlsplit(url)`: requires `scheme in ("http", "https")`, non-empty `netloc`, and exact server URL prefix/subpath match while retaining existing rejections for `.`/`..`, `%2f`, `%5c`, `\`, and `;` (`FRNT-2.3`).
   - Both `task_mcp.py` (stderr JSON-line audit records forwarded via `run_process` pump or structured wrapper) and `Relay._mcp` emit structured per-call audit entries `{event, target, status, duration_ms, bytes}` without logging file bodies or credentials (`FRNT-2.5`).
4. **Task MCP Server & Tool Scoping (`serve.py`):**
   - `configure()` validates every item in `session.mcp_servers`: requires non-empty `name` matching `^[A-Za-z0-9_-]{1,64}$` and `url` accepted by `taskbridge.mcp_allows(url, [server])`, skipping malformed descriptors with `await session.log(...)` (`FRNT-3.1`).
   - Tool policy filtering (`AGY_MCP_TOOL_ALLOWLIST` / `AGY_MCP_TOOL_DENYLIST` or built-in policy parser in `serve.py`) filters external server tool names prior to writing `mcp_config.json` (`FRNT-3.2`).
   - Core `{bridge}` tools (`TASK_TOOLS`: `shell`, `read_file`, `write_file`, `edit_file`) remain `"eager": True`, while external `session.mcp_servers` tools use deferred/non-eager registration (`"eager": False` if supported by `agy` 1.4.10, or capped by `AGY_MAX_EAGER_MCP_TOOLS` with remaining tools discovered/invoked safely per verified `agy` schema) (`FRNT-3.3`).
   - `Relay._mcp` and `configure()` maintain per-server allowed tool sets (when tool policy is active) and reject disallowed tool invocations deterministically while logging `{registered, filtered, deferred}` counts per server via `session.log` (`FRNT-3.4`).

### Component 2: Multimodal PDF & Binary Drawing Inspection (`task_mcp.py`, `serve.py`)
1. **PDF Magic Detection & Tool Schema (`task_mcp.py`):**
   - Add an optional `pages` string parameter (e.g., `"1-5"` or `"2,4"`) to `read_file`'s `inputSchema` in `TOOLS`, with `MAX_PDF_PAGES = 5` and `MAX_PDF_PAYLOAD = 10 << 20` (10 MiB aggregate base64/text cap safely under `MAX_FRAME = 16 MiB`).
   - Update `read_file(path, offset=1, limit=2000, pages=None)`:
     - Keep `IMAGE_MAGIC` check (`PNG`, `JPEG`, `GIF`, `WEBP` with `[8:12] == b"WEBP"`) and `MAX_IMAGE = 5 << 20` unchanged (`FRNT-4.5`).
     - Before reading the entire file via `read_bytes(path)` (which fails above `10 MB` in `read_result`), probe file header cleanly or detect `.pdf` / `%PDF-`:
       - If `path` ends with `.pdf` (case-insensitive) OR a leading-byte probe starts with `%PDF-` (after optional whitespace/BOM), route to `_read_pdf(path, pages)` (`FRNT-4.1`, `FRNT-4.3`).
       - Note: probing `%PDF-` via `read_result` (when `<= 10 MB`) or via a lightweight `head -c 16` / Python probe inside `relay("exec", ...)` when `read_bytes` reports `truncated` ensures PDFs `> 10 MB` are still inspected in-container without failing `read_bytes` (`FRNT-4.3`)!
2. **Deterministic In-Container PDF Rendering (`_read_pdf`):**
   - `_read_pdf(path, pages)` executes a single deterministic Python helper script inside the task container via `relay("exec", command=..., timeout_sec=120)` using a temporary workspace under `/tmp` that is cleaned up in a `finally` block (`FRNT-4.1`, `FRNT-4.2`, `FRNT-4.3`):
     1. Determines total page count and validates `pages` (defaulting to `1..min(total_pages, MAX_PDF_PAGES)`; rejecting invalid syntax or out-of-range pages with a clear error message) (`FRNT-4.2`, `FRNT-4.4`).
     2. Discovers an available rasterizer in deterministic priority order: `pdftoppm` -> `mutool` -> `gs` -> Python `fitz` (PyMuPDF) -> `pypdfium2` (`FRNT-4.4`). If none is present, exits cleanly with a structured JSON status `{"ok": false, "reason": "no_renderer", ...}` so `task_mcp.py` returns an actionable message (`FRNT-4.4`).
     3. Renders each selected page deterministically to PNG at `150 DPI` (stepping down to `100 DPI`, `72 DPI`, or JPEG quality `85` if any single page exceeds `MAX_IMAGE` `5 MiB` or total encoded payload exceeds `MAX_PDF_PAYLOAD`) (`FRNT-4.3`).
     4. Returns base64-encoded images and metadata (`total_pages`, `rendered_pages`, `dpi`, `mime`, `byte_sizes`) via stdout JSON (never exceeding `MAX_FRAME`) or writes temporary rendered PNGs for chunked retrieval if stdout approaches protocol `100 KB` `exec` output truncation (`limits.max_output_bytes`)!
        - **Critical protocol constraint from `ple-bench/docs/protocol.md`:** `exec` truncates `output` past `100 KB` (`limits.max_output_bytes`)! Therefore, rendered PNGs MUST NOT be dumped as multi-megabyte base64 in `exec` stdout! Instead, the in-container helper renders PNG files to a temp directory inside the task container, prints ONLY compact JSON metadata (`paths`, `total_pages`, `dpi`, `sizes`) to `exec` stdout (< 4 KB), and `task_mcp.py` fetches each rendered PNG via `read_bytes(temp_png_path)` (which supports up to `10 MB` raw bytes per file, well above `MAX_IMAGE = 5 MiB`), then deletes the temp directory via `exec`! That respects EVERY protocol limit (`100 KB` `exec` stdout, `10 MB` `read_file`, `5 MiB` `MAX_IMAGE`, `16 MiB` `MAX_FRAME`) (`FRNT-4.1`, `FRNT-4.3`).
3. **Binary CAD/Drawing Metadata Summary (`FRNT-4.6`):**
   - For non-PDF binary files (`b"\x00" in data[:8192]`), inspect magic prefixes (`AC10` for AutoCAD DWG, `PK\x03\x04` for ZIP/DOCX/XLSX/KMZ, `\xd0\xcf\x11\xe0` for OLE2/RVT/etc.) and return deterministic metadata: `"{path} is a binary {detected_type} file of {len(data)} bytes; inspect or extract with shell commands (e.g. python3, unzip, or CAD/BIM CLI tools)."` (`FRNT-4.6`).
   - NUL-free ASCII `.dxf`, `.ifc`, `.json`, `.csv`, `.py`, `.txt` files continue through the existing UTF-8 numbered text path (`FRNT-4.5`).

### Component 3: Programmatic Deliverable Verification Gate (`serve.py`, `deliverable_gate.py`)
1. **Candidate Deliverable Discovery (Task-Agnostic, `FRNT-5.2`):**
   - Maintain a `Relay` / `solve()` record of all paths written or modified via `write_file` (or captured from tool events), plus scan `session.workdir` (excluding hidden/cache/temp folders like `.git`, `__pycache__`, `/tmp`) via a lightweight pre-run & post-run `find` snapshot in the task container AND parse explicit container file paths mentioned in `session.instruction` and `session.final_message` (strictly filtering to paths present inside the task container workspace or explicitly created during the session; never reading `ple-bench/eval/` or graders).
2. **Deterministic `$0` Verification Script (`FRNT-5.1`, `FRNT-5.2`):**
   - After `agy` exits with an answer (and no `resume_wait` transport/rate-limit retry is pending), run a deterministic single-shot Python verification snippet via `session.exec(...)` inside the task container (`timeout_sec=60`):
     - Checks each candidate path for:
       1. Existence as a regular file (`os.path.isfile(p)`) and `size > 0` (`non_empty`).
       2. `.zip` integrity (`zipfile.ZipFile(p).testzip() is None`).
       3. `.json` syntax (`json.loads`).
       4. `.csv` syntax (`csv.reader` parses without `csv.Error` and has `>= 1` non-empty row).
       5. `.xml` / `.svg` well-formedness (`xml.etree.ElementTree.parse`).
       6. `.py` syntax (`ast.parse`).
       7. `.pdf` header/trailer (`%PDF-` prefix and `%%EOF` in trailing 1024 bytes).
   - No model API call is made during verification (`$0` cost).
3. **Bounded Repair Loop & Exit Semantics (`FRNT-5.3`, `FRNT-5.4`):**
   - Constants: `MAX_REPAIR_ATTEMPTS = 2`, `REPAIR_RESERVE_SEC = 120` (so repair runs only when `session.time_left() is None or session.time_left() > STOP_BEFORE_SEC + REPAIR_RESERVE_SEC`).
   - If verification finds failing deliverables and `conversation` is available, `repair_count < MAX_REPAIR_ATTEMPTS`, budget exceeds reserve, and `not session.cancelled.is_set()`:
     - Construct a deterministic repair prompt listing each failing file, failed check, size, and error message, and invoke `agy --conversation <id>` (consuming from bounded repair budget).
     - Re-run verification once after the repair turn.
   - Emit a structured JSON/line verification summary to `await session.log(...)` and return `session.final_message` (or annotated status if critical deliverables remained invalid after budget exhaustion without overwriting valid partial work or falsely claiming verification passed) (`FRNT-5.4`).

### Component 4: Stage 1 Open-Source Packaging & `$0` Dev Flow (`LICENSE`, `submission.yaml`, `install.sh`, `requirements.txt`)
- Add root `/Users/ky/physical-engineering/agy-gemini/LICENSE` containing verbatim Apache License Version 2.0 text (`FRNT-6.1`).
- Keep `submission.yaml` pointing to `google-cloud/gemini-4-argon`, `install: bash {src}/install.sh`, `serve: /opt/harness/venv/bin/python {src}/serve.py`, `env: { AGY_EFFORT: "high" }` (`FRNT-6.2`).
- Keep runtime Python dependencies minimal in `requirements.txt` (`websockets==15.0.1`; standard library for `deliverable_gate.py`, `task_mcp.py`, `serve.py`) installed into `/opt/harness/venv` via `uv` (`FRNT-6.3`), preserving LF endings, zero secrets, and zero references to `artifacts/` or `eval/` (`FRNT-6.4`, `FRNT-6.5`).

## Data and interfaces

### `task_mcp.py` `read_file` Tool Schema Extension
```json
{
  "name": "read_file",
  "inputSchema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "offset": {"type": "integer", "description": "first line to return, from 1 (text files)"},
      "limit": {"type": "integer", "description": "number of lines (default 2000, text files)"},
      "pages": {"type": "string", "description": "1-based page range for .pdf files, e.g. '1-5' or '2,4' (max 5 pages per call)"}
    },
    "required": ["path"]
  }
}
```

### Structured Audit Log Entry Format (`task_mcp.py` & `Relay._mcp`)
```json
{
  "audit": "mcp_dispatch",
  "component": "task_mcp|relay_mcp",
  "tool": "shell|read_file|write_file|edit_file|<server>:<method>",
  "target": "/normalized/container/path|server_name",
  "status": "ok|error|rejected",
  "duration_ms": 42,
  "detail": "redacted summary <= 200 chars"
}
```

### Deliverable Verification Result Schema (`deliverable_gate`)
```json
{
  "event": "deliverable_verification",
  "checked": [
    {"path": "/app/out/report.json", "size": 1420, "checks": ["exists", "non_empty", "json"], "ok": true, "error": null}
  ],
  "passed": 1,
  "failed": 0,
  "repair_triggered": false
}
```

## Error handling and failure modes

| Failure mode | Handling behavior | Criteria |
|---|---|---|
| `agy` 1.4.10 rejects or lacks granular policy controls or attempts serve-container local write | Fail closed: log error to `session.log`, raise `RuntimeError`, do not silently revert to blanket bypass | FRNT-1.3 |
| Malformed/unknown tool name, wrong arg types, NUL/unpaired surrogate in `task_mcp.py` | Return MCP `isError: true` text message; never send invalid frame to evaluator WebSocket | FRNT-2.1, FRNT-2.2 |
| Disallowed URL scheme (`file://`, etc.), bad host, or path traversal in `mcp_allows` | Return `False` -> HTTP `400`/`404` or `TaskError`; blocked before `session.http` | FRNT-2.3 |
| Secret substring (`GEMINI_API_KEY`, `MODEL_API_KEY`, `Relay.token`) in error text | `redact_secrets()` replaces sensitive substrings with `[REDACTED]` before MCP reply or log | FRNT-2.4 |
| Invalid `session.mcp_servers` entry or `tools/list` failure in `configure()` | Log warning via `session.log`, skip offending server, continue session cleanly | FRNT-3.1 |
| Missing PDF renderer (`pdftoppm`/`mutool`/`gs`/`fitz`) or encrypted/corrupt PDF | Return actionable MCP `text` block describing missing tool/error and `shell` alternatives | FRNT-4.4 |
| Rendered PDF page > `MAX_IMAGE` (5 MiB) or combined payload > `MAX_PDF_PAYLOAD` | Deterministically lower DPI (`150 -> 100 -> 72`) or encode JPEG; if still over cap, emit bounded error/slice notice | FRNT-4.3 |
| Deliverable check fails with low time left (`<= STOP_BEFORE_SEC + REPAIR_RESERVE_SEC`) or `session.cancelled` | Skip `--conversation` repair, log structured summary, return cleanly within `10 s` cancel window | FRNT-5.4 |

## Open design choices requiring verification during implementation

Implementers MUST verify these empirical runtime questions inside the built `linux/amd64` container (`ple-bench/kit/builder/Dockerfile` + `agy` 1.4.10 binary) before finalizing implementation code:

1. **`agy` 1.4.10 CLI Permission & Settings Schema (`FRNT-1.2`, `FRNT-1.3`):**
   - Run `/opt/harness/bin/agy --help` and inspect `agy` 1.4.10 configuration docs/behavior for:
     - Exact CLI flags replacing `--dangerously-skip-permissions` (e.g., whether `agy` 1.4.10 offers `--permission-mode`, `--allowed-tools`, or `settings.json` `toolPermission` / `permissions` allowlists for MCP tools only).
     - Whether setting `SETTINGS["allowNonWorkspaceAccess"] = False` prevents `agy` 1.4.10 from reading `$HOME/.gemini/config/` or launching stdio `task_mcp.py`, or whether `"allowNonWorkspaceAccess": False` works cleanly because `HOME` is set to `home` (`home / "workspace"` vs `home / ".gemini"`).
     - Whether `agy` 1.4.10 respects `tools: []` in `task-agent.md` when `--dangerously-skip-permissions` is absent vs present, and whether interactive MCP approval prompts occur if `--dangerously-skip-permissions` is removed while `"toolPermission": "always-proceed"` is kept.
2. **`agy` 1.4.10 MCP Deferred / Non-Eager Tool Loading (`FRNT-3.3`):**
   - Empirically verify in `agy` 1.4.10 how `mcp_config.json` handles `"eager": false` (or omitting `"tools"` / `"eager"`) on `serverUrl` entries: does `agy` expose a deferred tool-lookup mechanism to Gemini, load schemas lazily, or ignore `"eager": false` and still inject all 264 schemas? If `agy` ignores `"eager": false`, implementers must use an explicit server/tool filter or cap in `configure()` and document the verified mechanism.
3. **PDF Rasterizer Availability in PLE Task Containers & Serve Bundle Budget (`FRNT-4.1`, `FRNT-4.4`, `FRNT-6.3`):**
   - Empirically verify which CLI/Python PDF rasterizers (`pdftoppm` from `poppler-utils`, `mutool`, `gs`, `pypdfium2`, `fitz`/PyMuPDF) are pre-installed across representative PLE task images (`debian:bookworm` / engineering containers).
   - Measure the packed `/opt/harness` bundle size from `kit build` on baseline `222df50` before deciding whether any serve-side fallback library is even desirable vs keeping PDF rendering strictly inside the task container (`exec`).
4. **Deliverable Candidate Discovery Noise (`FRNT-5.2`):**
   - Verify whether a pre-run and post-run `find` scan in `session.workdir` captures non-deliverable intermediate scratch files (e.g. `.tmp`, `.pyc`, cache files) and define deterministic ignore globs plus explicit file-extension checks so harmless scratch artifacts do not trigger unnecessary repair loops.

## Testing strategy (criterion ID -> test file)

All tests execute **100% offline at `$0` cost** using Python `unittest`/`pytest`, mocked `taskbridge.Session`/`Relay`, local loopback servers, and `scripts/mock_proxy.py` — zero Vertex AI or commercial LLM calls.

| Criterion | Test |
| --- | --- |
| FRNT-1.1 | `tests/test_permissions_boundary.py` (`test_agent_disables_built_in_tools`) |
| FRNT-1.2 | `tests/test_permissions_boundary.py` (`test_settings_disallow_non_workspace_and_policy_flags`) |
| FRNT-1.3 | `tests/test_permissions_boundary.py` (`test_unsupported_policy_fails_closed`) |
| FRNT-1.4 | `tests/test_permissions_boundary.py` (`test_headless_engineering_shell_commands_allowed`) |
| FRNT-1.5 | `tests/test_permissions_boundary.py` (`test_authorized_mcp_end_to_end_and_escape_rejection`) |
| FRNT-2.1 | `tests/test_permissions_boundary.py` (`test_task_mcp_argument_schema_validation`) |
| FRNT-2.2 | `tests/test_permissions_boundary.py` (`test_relay_bounds_timeouts_and_payload_limits`) |
| FRNT-2.3 | `tests/test_permissions_boundary.py` (`test_mcp_allows_scheme_host_and_traversal_checks`) |
| FRNT-2.4 | `tests/test_permissions_boundary.py` (`test_secret_isolation_and_error_redaction`) |
| FRNT-2.5 | `tests/test_permissions_boundary.py` (`test_structured_audit_log_emitted_without_secrets`) |
| FRNT-3.1 | `tests/test_permissions_boundary.py` (`test_configure_validates_mcp_server_descriptors`) |
| FRNT-3.2 | `tests/test_permissions_boundary.py` (`test_configure_filters_mcp_tools_by_policy`) |
| FRNT-3.3 | `tests/test_permissions_boundary.py` (`test_configure_bounds_eager_external_mcp_tools`) |
| FRNT-3.4 | `tests/test_permissions_boundary.py` (`test_disallowed_mcp_tool_rejected_and_counts_logged`) |
| FRNT-4.1 | `tests/test_pdf_inspection.py` (`test_read_file_renders_pdf_to_native_images`) |
| FRNT-4.2 | `tests/test_pdf_inspection.py` (`test_pdf_page_cap_and_explicit_page_range`) |
| FRNT-4.3 | `tests/test_pdf_inspection.py` (`test_pdf_downscaling_and_over_10mb_pdf_support`) |
| FRNT-4.4 | `tests/test_pdf_inspection.py` (`test_pdf_missing_renderer_corrupt_and_bad_range_fallback`) |
| FRNT-4.5 | `tests/test_pdf_inspection.py` (`test_raster_image_magic_and_text_cad_no_regression`) |
| FRNT-4.6 | `tests/test_pdf_inspection.py` (`test_binary_cad_metadata_summary`) |
| FRNT-5.1 | `tests/test_deliverable_gate.py` (`test_post_run_gate_executes_offline_in_solve`) |
| FRNT-5.2 | `tests/test_deliverable_gate.py` (`test_candidate_discovery_and_format_validators`) |
| FRNT-5.3 | `tests/test_deliverable_gate.py` (`test_failed_gate_triggers_bounded_conversation_repair`) |
| FRNT-5.4 | `tests/test_deliverable_gate.py` (`test_gate_pass_exhaustion_low_budget_and_cancel_exit`) |
| FRNT-6.1 | `tests/test_stage1_packaging.py` (`test_root_apache2_license_present`) |
| FRNT-6.2 | `tests/test_stage1_packaging.py` (`test_submission_yaml_stage2_and_kit_validate`) |
| FRNT-6.3 | `tests/test_stage1_packaging.py` (`test_uv_venv_isolation_and_bundle_size_guard`) |
| FRNT-6.4 | `tests/test_stage1_packaging.py` (`test_lf_line_endings_no_keys_no_lfs_no_excluded_dirs`) |
| FRNT-6.5 | `tests/test_stage1_packaging.py` (`test_no_artifact_or_grader_reads_and_offline_mock_suite`) |
