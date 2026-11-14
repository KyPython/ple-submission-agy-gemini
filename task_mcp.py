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

"""Stdio MCP server exposing task container tools via `taskbridge.Relay`.

`agy` runs in the serve container and uses these MCP tools (`shell`,
`read_file`, `write_file`, `edit_file`) to interact with the task container over
`TASK_RELAY_URL`.
"""

from __future__ import annotations

import base64
from concurrent import futures
import json
import os
import posixpath
import sys
import threading
import time
from typing import Any
import urllib.request

# taskbridge.py sits beside this file, a folder python -I leaves off sys.path.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import taskbridge  # pylint: disable=g-import-not-at-top,g-bad-import-order

RELAY = os.environ.get("TASK_RELAY_URL", "")
WORKDIR = os.environ.get("TASK_WORKDIR", "/")
MAX_TEXT = 40_000
MAX_IMAGE = 5 << 20
IMAGE_MAGIC = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}
# the relay is local: skip any HTTP proxy
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# write_file and edit_file take turns, so concurrent edits cannot undo each
# other
_files = threading.Lock()
_err_lock = threading.Lock()
_CAPTURED_SECRETS: list[str] = []


def _extra_secrets() -> tuple[str, ...]:
  """Returns relay and scrubbed process secret values for redaction."""
  vals = [RELAY, os.environ.get("TASK_RELAY_URL", ""), *_CAPTURED_SECRETS]
  return tuple(v for v in vals if v)


def _scrub_process_env() -> None:
  """Captures sensitive env values for redaction, then drops keys from os.environ."""
  for var in taskbridge.SENSITIVE_ENV_VARS:
    if var == "TASK_RELAY_URL":
      continue
    val = os.environ.pop(var, "")
    if val:
      _CAPTURED_SECRETS.append(val)


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
  """Builds a JSON Schema object definition for an MCP tool.

  Args:
    props: Mapping of property names to JSON Schema definitions.
    required: List of required property names.

  Returns:
    A JSON Schema dictionary of type `object`.
  """
  return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {
        "name": "shell",
        "description": (
            "Run a bash command in the task's Linux container (no internet)"
            " and return its exit code and output. Every call starts in"
            f" {WORKDIR}; a cd does not carry over. Use it to inspect files,"
            " run python3 and other installed tools, and check your"
            " deliverables."
        ),
        "inputSchema": _schema(
            {
                "command": {"type": "string"},
                "timeout_sec": {
                    "type": "integer",
                    "description": "default 600, at most 600",
                },
            },
            ["command"],
        ),
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the task container: text comes back with line"
            " numbers, png/jpeg/gif/webp images come back as images. Use offset"
            " and limit to page through long files."
        ),
        "inputSchema": _schema(
            {
                "path": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "description": "first line to return, from 1",
                },
                "limit": {
                    "type": "integer",
                    "description": "number of lines (default 2000)",
                },
            },
            ["path"],
        ),
    },
    {
        "name": "write_file",
        "description": (
            "Write a text file in the task container, replacing it if it exists"
            " and creating its parent directories."
        ),
        "inputSchema": _schema(
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
    },
    {
        "name": "edit_file",
        "description": (
            "Replace text in a file in the task container. old_text must appear"
            " exactly once, unless replace_all is true."
        ),
        "inputSchema": _schema(
            {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            ["path", "old_text", "new_text"],
        ),
    },
]


def relay(route: str, **fields: Any) -> dict[str, Any]:
  """Posts a JSON request to the local taskbridge relay.

  Args:
    route: Relay endpoint route (`exec`, `read_file`, or `write_file`).
    **fields: JSON payload fields to include in the request body.

  Returns:
    The parsed JSON response dictionary from the relay.
  """
  relay_url = RELAY or os.environ.get("TASK_RELAY_URL", "")
  if not relay_url:
    raise ValueError("TASK_RELAY_URL is not configured")
  body = json.dumps(fields).encode("utf-8")
  if len(body) > taskbridge.MAX_BODY:
    raise ValueError(
        f"{route}: request body is {len(body)} bytes, over MAX_BODY"
        f" ({taskbridge.MAX_BODY})"
    )
  if route == "exec" and len(body) > taskbridge.MAX_FRAME - 1024:
    raise ValueError(
        f"exec: payload {len(body)} bytes approaches MAX_FRAME"
        f" ({taskbridge.MAX_FRAME}); write a script with write_file instead"
    )
  req = urllib.request.Request(
      f"{relay_url}/{route}",
      data=body,
      method="POST",
      headers={"Content-Type": "application/json"},
  )
  with OPENER.open(req, timeout=900) as resp:
    return json.loads(resp.read())


def resolve(path: str) -> str:
  """Resolves `path` against `WORKDIR` and normalizes it.

  Args:
    path: Absolute or workdir-relative container path.

  Returns:
    The normalized absolute POSIX path in the container.
  """
  return posixpath.normpath(
      path if path.startswith("/") else posixpath.join(WORKDIR, path)
  )


def clip(content: str) -> str:
  """Truncates `content` in the middle if it exceeds `MAX_TEXT` characters.

  Args:
    content: Text string to clip.

  Returns:
    The original string if within `MAX_TEXT`, or a middle-truncated string.
  """
  if len(content) <= MAX_TEXT:
    return content
  return (
      content[: MAX_TEXT // 2]
      + f"\n[... {len(content) - MAX_TEXT} characters cut ...]\n"
      + content[-MAX_TEXT // 2 :]
  )


def read_bytes(path: str) -> bytes:
  """Reads the full contents of `path` from the task container via the relay.

  Args:
    path: Absolute path inside the task container.

  Returns:
    Raw file bytes.

  Raises:
    ValueError: If the file cannot be read or exceeds the 10 MB read limit.
  """
  r = relay("read_file", path=path)
  if not r.get("ok"):
    raise ValueError(r.get("error") or f"could not read {path}")
  if r.get("truncated"):
    raise ValueError(
        f"{path} is {r.get('size')} bytes, over the 10 MB a read returns; work"
        " on it with shell"
    )
  return base64.b64decode(r["content_b64"])


def write_bytes(path: str, data: bytes) -> int:
  """Writes `data` to `path` in the task container via the relay.

  Args:
    path: Absolute path inside the task container.
    data: Raw bytes to write.

  Returns:
    The number of bytes written.

  Raises:
    ValueError: If `data` exceeds `MAX_WRITE_BYTES` or the relay reports failure.
  """
  if len(data) > taskbridge.MAX_WRITE_BYTES:
    raise ValueError(
        f"{path}: {len(data)} bytes exceeds MAX_WRITE_BYTES"
        f" ({taskbridge.MAX_WRITE_BYTES})"
    )
  r = relay(
      "write_file",
      path=path,
      content_b64=base64.b64encode(data).decode(),
      append=False,
  )
  if not r.get("ok"):
    raise ValueError(r.get("error") or f"could not write {path}")
  return r.get("size", len(data))


def text(message: str) -> list[dict[str, Any]]:
  """Wraps `message` in an MCP text content block list.

  Args:
    message: Text content to return from a tool call.

  Returns:
    A single-element list containing the MCP text content block.
  """
  return [{"type": "text", "text": message}]


def shell(command: str, timeout_sec: int = 600) -> list[dict[str, Any]]:
  """Runs a bash command in the task container and formats the output for MCP.

  Args:
    command: Bash command string to execute.
    timeout_sec: Maximum execution time in seconds (up to `MAX_TIMEOUT_SEC`).

  Returns:
    An MCP content list with the command's exit code and clipped output.

  Raises:
    ValueError: If the command exceeds protocol frame limits or relay rejects.
  """
  bounded_timeout = min(max(1, timeout_sec), taskbridge.MAX_TIMEOUT_SEC)
  probe = json.dumps(
      {"command": command, "timeout_sec": bounded_timeout}
  ).encode("utf-8")
  if len(probe) > taskbridge.MAX_FRAME - 1024:
    raise ValueError(
        f"shell command payload ({len(probe)} bytes) exceeds MAX_FRAME limit"
    )
  r = relay(
      "exec",
      command=command,
      timeout_sec=bounded_timeout,
  )
  if not r.get("ok", True):
    raise ValueError(r.get("error"))
  notes = " (timed out)" if r.get("timed_out") else ""
  notes += " (output cut)" if r.get("truncated") else ""
  return text(
      f"exit_code={r.get('exit_code')}{notes}\n{clip(r.get('output') or '')}"
  )


def read_file(
    path: str, offset: int = 1, limit: int = 2000
) -> list[dict[str, Any]]:
  """Reads a text or image file from the task container.

  Args:
    path: Path to the file inside the task container.
    offset: 1-based starting line number for text files.
    limit: Maximum number of lines to return for text files.

  Returns:
    An MCP content list containing numbered text lines or base64 image data.
  """
  path = resolve(path)
  data = read_bytes(path)
  mime = next(
      (m for magic, m in IMAGE_MAGIC.items() if data.startswith(magic)), None
  )
  if mime and (mime != "image/webp" or data[8:12] == b"WEBP"):
    if len(data) > MAX_IMAGE:
      return text(
          f"{path} is a {len(data)}-byte image, too large to view; downscale it"
          " first"
      )
    return [
        {
            "type": "image",
            "data": base64.b64encode(data).decode(),
            "mimeType": mime,
        },
        {"type": "text", "text": f"{path} ({mime}, {len(data)} bytes)"},
    ]
  if b"\x00" in data[:8192]:
    return text(
        f"{path} is a binary file of {len(data)} bytes; inspect or convert it"
        " with shell commands"
    )
  lines = data.decode("utf-8", "replace").splitlines()
  start = taskbridge.positive_int(offset, 1)
  if not lines:
    return text(f"{path} is empty")
  if start > len(lines):
    return text(
        f"{path} has {len(lines)} line{'' if len(lines) == 1 else 's'}; offset"
        f" {start} is past the end"
    )
  shown = lines[start - 1 : start - 1 + taskbridge.positive_int(limit, 2000)]
  body = "\n".join(f"{n:6d}\t{line}" for n, line in enumerate(shown, start))
  more = (
      f"\n[lines {start}-{start + len(shown) - 1} of {len(lines)}]"
      if len(shown) < len(lines)
      else ""
  )
  return text(clip(body) + more)


def write_file(path: str, content: str) -> list[dict[str, Any]]:
  """Writes a UTF-8 text file to `path` in the task container.

  Args:
    path: Destination file path inside the task container.
    content: Text content to write.

  Returns:
    An MCP content list confirming the number of bytes written.
  """
  path = resolve(path)
  with _files:
    return text(f"wrote {write_bytes(path, content.encode())} bytes to {path}")


def edit_file(
    path: str, old_text: str, new_text: str, replace_all: bool = False
) -> list[dict[str, Any]]:
  """Replaces `old_text` with `new_text` in a file inside the task container.

  Args:
    path: Target file path inside the task container.
    old_text: Existing substring to search for.
    new_text: Replacement string.
    replace_all: Whether to replace all occurrences instead of requiring one.

  Returns:
    An MCP content list reporting the number of replacements made.

  Raises:
    ValueError: If `old_text` is not found, or appears multiple times when
      `replace_all` is `False`.
  """
  path = resolve(path)
  with _files:
    current = read_bytes(path).decode("utf-8")
    count = current.count(old_text) if old_text else 0
    if count == 0:
      raise ValueError(f"old_text was not found in {path}")
    if count > 1 and not replace_all:
      raise ValueError(
          f"old_text appears {count} times in {path}; give more context or set"
          " replace_all"
      )
    write_bytes(
        path,
        current.replace(old_text, new_text, -1 if replace_all else 1).encode(),
    )
  return text(f"replaced {count if replace_all else 1} occurrence(s) in {path}")


HANDLERS = {
    "shell": shell,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
}
_TOOL_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}
_out = threading.Lock()


def _validate_str(key: str, val: Any, *, allow_empty: bool = False) -> str:
  """Validates string fields for UTF-8 safety, optional non-emptiness, and NUL."""
  if not isinstance(val, str):
    raise ValueError(f"{key} must be a string")
  if "\x00" in val and not allow_empty:
    raise ValueError(f"{key} holds a NUL byte")
  try:
    val.encode("utf-8")
  except UnicodeEncodeError:
    raise ValueError(f"{key} contains unpaired UTF-16 surrogates") from None
  if not allow_empty and not val.strip():
    raise ValueError(f"{key} must be a non-empty string")
  return val


def _validate_pos_int(key: str, val: Any) -> int:
  """Validates positive integers while rejecting booleans and floats."""
  if isinstance(val, bool) or not isinstance(val, int) or val < 1:
    raise ValueError(f"{key} must be a positive integer")
  return val


def _validate_bool(key: str, val: Any) -> bool:
  """Validates boolean parameters strictly."""
  if not isinstance(val, bool):
    raise ValueError(f"{key} must be a boolean")
  return val


def validate_tool_args(name: Any, args: Any) -> dict[str, Any]:
  """Validates `name` against `HANDLERS` and `args` against its JSON Schema."""
  if not isinstance(name, str) or name not in HANDLERS:
    raise ValueError(f"unknown tool {name}")
  if not isinstance(args, dict):
    raise ValueError("arguments must be a JSON object")
  schema = _TOOL_SCHEMAS[name]
  props = schema.get("properties") or {}
  required = schema.get("required") or []
  unknown = set(args) - set(props)
  if unknown:
    raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}")
  missing = [k for k in required if k not in args]
  if missing:
    raise ValueError(f"missing required argument(s): {', '.join(missing)}")

  cleaned: dict[str, Any] = {}
  for key, val in args.items():
    spec_type = (props.get(key) or {}).get("type")
    if spec_type == "string":
      allow_empty = key in ("content", "new_text")
      cleaned[key] = _validate_str(key, val, allow_empty=allow_empty)
      if key == "old_text" and not cleaned[key]:
        raise ValueError("old_text must not be empty")
    elif spec_type == "integer":
      cleaned[key] = _validate_pos_int(key, val)
    elif spec_type == "boolean":
      cleaned[key] = _validate_bool(key, val)
    else:
      cleaned[key] = val
  return cleaned


def _audit_target(name: str, args: dict[str, Any]) -> str:
  """Returns a safe redacted audit target summary without full payloads."""
  if isinstance(args.get("path"), str):
    return resolve(args["path"])
  if isinstance(args.get("command"), str):
    t = args.get("timeout_sec", taskbridge.MAX_TIMEOUT_SEC)
    return f"timeout={t}s,cmd_bytes={len(args['command'].encode('utf-8', 'replace'))}"
  return str(name)


def _write_audit(
    tool: str, target: str, status: str, started: float, detail: str = ""
) -> None:
  """Writes one structured JSON-line audit record to stderr under a lock."""
  line = taskbridge.format_audit_record(
      "task_mcp",
      tool,
      target,
      status,
      round((time.monotonic() - started) * 1000),
      detail,
      extra_secrets=_extra_secrets(),
  )
  with _err_lock:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _scrub_content(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Redacts leaked secret substrings in MCP text blocks."""
  extras = _extra_secrets()
  out: list[dict[str, Any]] = []
  for block in blocks:
    if block.get("type") == "text" and isinstance(block.get("text"), str):
      out.append({
          **block,
          "text": taskbridge.redact_secrets(
              block["text"], extra_secrets=extras
          ),
      })
    else:
      out.append(block)
  return out


def send(message: dict[str, Any]) -> None:
  """Writes a JSON-RPC message line to stdout under a lock.

  Args:
    message: JSON-RPC response dictionary.
  """
  with _out:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def call(rid: Any, params: dict[str, Any]) -> None:
  """Executes a `tools/call` request and sends the JSON-RPC response.

  Args:
    rid: JSON-RPC request ID.
    params: Request `params` dictionary containing `name` and `arguments`.
  """
  started = time.monotonic()
  name = params.get("name")
  raw_args = params.get("arguments")
  if raw_args is None and "arguments" not in params:
    raw_args = {}
  status, detail, target = "ok", "", str(name or "?")
  try:
    args = validate_tool_args(name, raw_args)
    target = _audit_target(str(name), args)
  except ValueError as exc:
    status = "rejected"
    detail = taskbridge.redact_secrets(str(exc), extra_secrets=_extra_secrets())
    send({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {"content": text(f"error: {detail}"), "isError": True},
    })
    _write_audit(str(name or "?"), target, status, started, detail)
    return

  try:
    content = _scrub_content(HANDLERS[name](**args))
    send({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {"content": content, "isError": False},
    })
  except Exception as exc:  # pylint: disable=broad-exception-caught
    status = "error"
    raw_message = (
        str(exc)
        if isinstance(exc, ValueError)
        else f"{type(exc).__name__}: {exc}"
    )
    message = taskbridge.redact_secrets(
        raw_message, extra_secrets=_extra_secrets()
    )
    detail = message
    send({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {"content": text(f"error: {message}"), "isError": True},
    })
  finally:
    _write_audit(str(name), target, status, started, detail)


def error(rid: Any, code: int, message: str) -> None:
  """Sends a JSON-RPC error response to stdout.

  Args:
    rid: JSON-RPC request ID (or `None` if unknown).
    code: JSON-RPC numeric error code.
    message: Human-readable error message.
  """
  send(
      {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
  )


def handle(pool: futures.ThreadPoolExecutor, req: Any) -> None:
  """Dispatches a single JSON-RPC message read from stdin.

  Executes `tools/call` in `pool` so long-running commands do not block
  subsequent requests. Ignores notifications and returns an Invalid Request
  error for non-dict messages.

  Args:
    pool: Thread pool executor for running tool calls concurrently.
    req: Parsed JSON-RPC request message.
  """
  if not isinstance(req, dict):
    error(None, -32600, "invalid request: not a JSON object")
    return
  rid, method = req.get("id"), req.get("method")
  if rid is None:
    return
  params = req.get("params") if isinstance(req.get("params"), dict) else {}
  if method == "initialize":
    version = params.get("protocolVersion", "2025-06-18")
    send({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "task", "version": "1"},
        },
    })
  elif method == "tools/list":
    send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
  elif method == "tools/call":
    pool.submit(call, rid, params)
  elif method == "ping":
    send({"jsonrpc": "2.0", "id": rid, "result": {}})
  else:
    error(rid, -32601, f"unknown method {method}")


def main() -> None:
  """Scrubs parent secrets, reads JSON-RPC lines from stdin, and dispatches."""
  _scrub_process_env()
  pool = futures.ThreadPoolExecutor(4)
  for line in sys.stdin:
    if not line.strip():
      continue
    try:
      msg = json.loads(line)
    except ValueError:
      error(None, -32700, "parse error: not JSON")
      continue
    for req in msg if isinstance(msg, list) and msg else [msg]:
      handle(pool, req)


if __name__ == "__main__":
  main()
