# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Task-agnostic, $0 post-run deliverable verification and repair helpers.

Discovers created or modified container files plus explicit output paths from
the session, validates file existence, non-zero byte size, and structural
syntax inside the task container via `session.exec`, and builds bounded repair
prompts without making any paid LLM calls.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any, Iterable

MAX_REPAIR_ATTEMPTS = 2
REPAIR_RESERVE_SEC = 120

_IGNORED_DIRS = frozenset({
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".cache",
    ".gemini",
})
_IGNORED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".tmp",
    ".temp",
    ".swp",
    ".swo",
    ".log",
    ".lock",
    ".pid",
    ".part",
    ".bak",
    "~",
)
_IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_SYSTEM_PREFIXES = (
    "/bin/",
    "/sbin/",
    "/usr/",
    "/etc/",
    "/lib/",
    "/lib64/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/opt/",
    "/var/",
    "/tmp/",
    "/root/",
    "/home/",
)
_DELIVERABLE_EXTS = frozenset({
    ".zip",
    ".json",
    ".csv",
    ".xml",
    ".svg",
    ".py",
    ".pdf",
    ".md",
    ".txt",
    ".html",
    ".xlsx",
    ".docx",
    ".dxf",
    ".step",
    ".stp",
    ".ifc",
})
_URL_RE = re.compile(r"https?://\S+", re.I)
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.:@/-])(/[A-Za-z0-9._/+~-]+)")
_REL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./:-])([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.[A-Za-z0-9]{1,8})"
)
_OUTPUT_CUE_RE = re.compile(
    r"\b(?:save|write|wrote|written|output|outputs|deliverable|deliverables"
    r"|create|created|export|exported|generate|generated|produce|produced"
    r"|store|place|target|destination)\b",
    re.I,
)

_CONTAINER_SNAPSHOT_SCRIPT = """
import json, os, posixpath, sys
root = sys.argv[1] or "/"
ignored_dirs = set(json.loads(sys.argv[2]))
ignored_names = set(json.loads(sys.argv[3]))
ignored_suffixes = tuple(json.loads(sys.argv[4]))
max_entries = int(sys.argv[5])
out = {}
if os.path.isdir(root):
  for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [
        d for d in sorted(dirnames)
        if d not in ignored_dirs and not d.startswith(".")
    ]
    for name in sorted(filenames):
      if name in ignored_names or name.startswith(".#") or name.endswith(ignored_suffixes):
        continue
      full = posixpath.normpath(os.path.join(dirpath, name))
      try:
        st = os.stat(full)
      except OSError:
        continue
      out[full] = [st.st_size, st.st_mtime_ns]
      if len(out) >= max_entries:
        break
    if len(out) >= max_entries:
      break
print(json.dumps(out))
""".strip()

_CONTAINER_VERIFY_SCRIPT = """
import ast, csv, json, os, sys, xml.etree.ElementTree as ET, zipfile, zlib

paths = json.loads(sys.argv[1])
results = []

def check_one(path):
  checks = ["exists"]
  if not os.path.isfile(path):
    return {"path": path, "size": None, "checks": checks, "ok": False, "error": "file does not exist or is not a regular file"}
  try:
    size = os.path.getsize(path)
  except OSError as exc:
    return {"path": path, "size": None, "checks": checks, "ok": False, "error": f"stat failed: {exc}"}
  checks.append("non_empty")
  if size <= 0:
    return {"path": path, "size": size, "checks": checks, "ok": False, "error": "file is 0 bytes"}

  ext = os.path.splitext(path)[1].lower()
  try:
    if ext == ".zip":
      checks.append("zip")
      with zipfile.ZipFile(path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
          return {"path": path, "size": size, "checks": checks, "ok": False, "error": f"corrupt zip member: {bad}"}
    elif ext == ".json":
      checks.append("json")
      with open(path, "r", encoding="utf-8") as f:
        json.load(f)
    elif ext == ".csv":
      checks.append("csv")
      with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
      if not any(any(cell.strip() for cell in row) for row in rows):
        return {"path": path, "size": size, "checks": checks, "ok": False, "error": "CSV has no non-empty rows"}
    elif ext in (".xml", ".svg"):
      checks.append("svg" if ext == ".svg" else "xml")
      ET.parse(path)
    elif ext == ".py":
      checks.append("py")
      with open(path, "r", encoding="utf-8") as f:
        ast.parse(f.read(), filename=path)
    elif ext == ".pdf":
      checks.append("pdf")
      with open(path, "rb") as f:
        head = f.read(5)
        f.seek(max(0, size - 1024))
        tail = f.read(1024)
      if not head.startswith(b"%PDF-"):
        return {"path": path, "size": size, "checks": checks, "ok": False, "error": "missing %PDF- header"}
      if b"%%EOF" not in tail:
        return {"path": path, "size": size, "checks": checks, "ok": False, "error": "missing %%EOF marker in trailing 1024 bytes"}
  except (OSError, UnicodeDecodeError, ValueError, SyntaxError, csv.Error, ET.ParseError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
    return {"path": path, "size": size, "checks": checks, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

  return {"path": path, "size": size, "checks": checks, "ok": True, "error": None}

for p in paths:
  results.append(check_one(p))

print(json.dumps({"checked": results}))
""".strip()


def _is_ignored_path(path: str) -> bool:
  """Returns True if `path` matches scratch, cache, or hidden-artifact noise."""
  norm = posixpath.normpath(path)
  parts = norm.split("/")
  base = parts[-1] if parts else ""
  if not base or base in _IGNORED_NAMES or base.startswith(".#"):
    return True
  if any(part in _IGNORED_DIRS or (part.startswith(".") and part not in (".", "..")) for part in parts[:-1]):
    return True
  if base.startswith("tmp_") or base.startswith("_tmp_"):
    return True
  return base.lower().endswith(_IGNORED_SUFFIXES)


def _is_candidate_scope(path: str, workdir: str) -> bool:
  """Returns True if `path` lies in `workdir` or non-system container output space."""
  norm_path = posixpath.normpath(path)
  norm_work = posixpath.normpath(workdir or "/")
  if _is_ignored_path(norm_path):
    return False
  if norm_work != "/" and norm_path.startswith(norm_work + "/"):
    return True
  return not norm_path.startswith(_SYSTEM_PREFIXES) and norm_path.startswith("/")


async def snapshot_workdir(
    session: Any, workdir: str, *, max_entries: int = 2000
) -> dict[str, tuple[int, int]]:
  """Collects `{path: (size, mtime_ns)}` inside `workdir` via `$0` `session.exec`."""
  if getattr(session, "cancelled", None) and session.cancelled.is_set():
    return {}
  cmd = (
      "if command -v python3 >/dev/null 2>&1; then "
      f"python3 -c {shlex.quote(_CONTAINER_SNAPSHOT_SCRIPT)} "
      f"{shlex.quote(workdir or '/')} "
      f"{shlex.quote(json.dumps(sorted(_IGNORED_DIRS)))} "
      f"{shlex.quote(json.dumps(sorted(_IGNORED_NAMES)))} "
      f"{shlex.quote(json.dumps(list(_IGNORED_SUFFIXES)))} "
      f"{max_entries}; "
      "else echo '{}'; fi"
  )
  try:
    res = await session.exec(cmd, timeout_sec=30)
  except Exception:  # pylint: disable=broad-exception-caught
    return {}
  if not res.get("ok") or res.get("exit_code") != 0:
    return {}
  try:
    raw = json.loads((res.get("output") or "").strip() or "{}")
  except ValueError:
    return {}
  if not isinstance(raw, dict):
    return {}
  out: dict[str, tuple[int, int]] = {}
  for path, meta in raw.items():
    if (
        isinstance(path, str)
        and isinstance(meta, list)
        and len(meta) == 2
        and all(isinstance(v, int) for v in meta)
    ):
      out[path] = (meta[0], meta[1])
  return out


def diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
  """Returns sorted paths created or modified between `before` and `after`."""
  return sorted(
      path for path, meta in after.items() if before.get(path) != meta
  )


def _clean_token(token: str) -> str:
  """Strips trailing prose punctuation from a path token."""
  return token.rstrip(".,;:!?)'\"`]")


def extract_explicit_paths(
    text: str, workdir: str, *, require_output_cue: bool = False
) -> list[str]:
  """Extracts probable container deliverable paths referenced in prose."""
  if not text:
    return []
  scrubbed = _URL_RE.sub(" ", text)
  norm_work = posixpath.normpath(workdir or "/")
  found: list[str] = []
  seen: set[str] = set()

  def consider(raw: str, match_start: int) -> None:
    cand = _clean_token(raw)
    if not cand:
      return
    norm = (
        posixpath.normpath(cand)
        if cand.startswith("/")
        else posixpath.normpath(posixpath.join(norm_work, cand))
    )
    _, ext = posixpath.splitext(norm)
    if not ext or ext.lower() not in _DELIVERABLE_EXTS:
      return
    if not _is_candidate_scope(norm, norm_work):
      return
    if require_output_cue:
      para_start = scrubbed.rfind("\n\n", 0, match_start)
      window_start = max(para_start + 2 if para_start != -1 else 0, match_start - 200)
      window = scrubbed[window_start:match_start]
      if not _OUTPUT_CUE_RE.search(window):
        return
    if norm not in seen:
      seen.add(norm)
      found.append(norm)

  for match in _ABS_PATH_RE.finditer(scrubbed):
    consider(match.group(1), match.start())
  if norm_work != "/":
    for match in _REL_PATH_RE.finditer(scrubbed):
      consider(match.group(1), match.start())
  return found


def discover_candidates(
    workdir: str,
    instruction: str,
    final_message: str,
    written_paths: Iterable[str],
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[str]:
  """Collects deduplicated task-agnostic candidate deliverable paths."""
  norm_work = posixpath.normpath(workdir or "/")
  ordered: list[str] = []
  seen: set[str] = set()

  def add(path: str) -> None:
    norm = posixpath.normpath(path)
    if not _is_candidate_scope(norm, norm_work):
      return
    if norm not in seen:
      seen.add(norm)
      ordered.append(norm)

  for p in diff_snapshots(before, after):
    add(p)
  for p in written_paths:
    if isinstance(p, str):
      add(p)
  for p in extract_explicit_paths(final_message, norm_work, require_output_cue=False):
    add(p)
  for p in extract_explicit_paths(instruction, norm_work, require_output_cue=True):
    add(p)
  return ordered


def build_verify_command(candidates: list[str]) -> str:
  """Builds a `$0` in-container script checking existence and syntax for candidates."""
  fallback = (
      "python3_missing=1; "
      "echo '{\"checked\": []}'"
  )
  # POSIX fallback for containers lacking python3 (e.g. bare ubuntu:24.04)
  sh_checks = []
  for path in candidates:
    qp = shlex.quote(path)
    ext = posixpath.splitext(path)[1].lower()
    sh_checks.append(
        f"p={qp}; "
        'if [ ! -f "$p" ]; then err="file does not exist or is not a regular file"; sz=null; ok=false; '
        'else sz=$(wc -c < "$p" | tr -d " "); '
        'if [ "$sz" -le 0 ] 2>/dev/null; then err="file is 0 bytes"; ok=false; '
        'else err=null; ok=true; fi; fi; '
        + (
            'if [ "$ok" = "true" ] && command -v unzip >/dev/null 2>&1; then '
            'unzip -tqq "$p" >/dev/null 2>&1 || { err="corrupt zip"; ok=false; }; fi; '
            if ext == ".zip"
            else ""
        )
    )
  if sh_checks:
    fallback = (
        "items=''; "
        + " ".join(
            c
            + 'ep=$(printf \'%s\' "$p" | sed \'s/"/\\\\"/g\'); '
            + 'ee=$(if [ "$err" = "null" ]; then echo null; else echo "\\"$err\\""; fi); '
            + 'item="{\\"path\\":\\"$ep\\",\\"size\\":$sz,\\"checks\\":[\\"exists\\",\\"non_empty\\"],\\"ok\\":$ok,\\"error\\":$ee}"; '
            + 'if [ -z "$items" ]; then items="$item"; else items="$items,$item"; fi;'
            for c in sh_checks
        )
        + ' echo "{\\"checked\\":[$items]}"'
    )
  return (
      "if command -v python3 >/dev/null 2>&1; then "
      f"python3 -c {shlex.quote(_CONTAINER_VERIFY_SCRIPT)} "
      f"{shlex.quote(json.dumps(candidates))}; "
      f"else {fallback}; fi"
  )


async def verify_deliverables(
    session: Any, candidates: list[str]
) -> dict[str, Any]:
  """Runs deterministic `$0` checks inside `session` for `candidates`."""
  if not candidates:
    return {
        "event": "deliverable_verification",
        "checked": [],
        "passed": 0,
        "failed": 0,
        "repair_triggered": False,
    }
  res = await session.exec(build_verify_command(candidates), timeout_sec=60)
  checked: list[dict[str, Any]] = []
  if res.get("ok") and res.get("exit_code") == 0:
    try:
      parsed = json.loads((res.get("output") or "").strip())
      if isinstance(parsed, dict) and isinstance(parsed.get("checked"), list):
        checked = parsed["checked"]
    except ValueError:
      checked = []
  if not checked:
    detail = (res.get("error") or res.get("output") or f"exit_code={res.get('exit_code')}")[:200]
    checked = [{
        "path": p,
        "size": None,
        "checks": ["exists"],
        "ok": False,
        "error": f"verification runner failed: {detail}",
    } for p in candidates]
  passed = sum(1 for item in checked if item.get("ok"))
  failed = len(checked) - passed
  return {
      "event": "deliverable_verification",
      "checked": checked,
      "passed": passed,
      "failed": failed,
      "repair_triggered": False,
  }


def build_repair_prompt(report: dict[str, Any]) -> str:
  """Formats a deterministic repair prompt listing failing deliverables and errors."""
  failures = [item for item in report.get("checked") or [] if not item.get("ok")]
  lines = [
      "Post-run deliverable verification failed on the following container file(s):"
  ]
  for item in failures:
    checks = ", ".join(item.get("checks") or [])
    size = item.get("size")
    err = item.get("error") or "validation failed"
    lines.append(
        f"- {item.get('path')} (size={size}, checks=[{checks}]): {err}"
    )
  lines.append(
      "Fix every failing deliverable inside the task container, verify file"
      " non-emptiness and format syntax with your container tools, and then"
      " provide your final response."
  )
  return "\n".join(lines)
