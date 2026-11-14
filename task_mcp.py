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
import inspect
import json
import os
import posixpath
import re
import shlex
import sys
import threading
import time
from typing import Any
import urllib.parse
import urllib.request

# taskbridge.py sits beside this file, a folder python -I leaves off sys.path.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import taskbridge  # pylint: disable=g-import-not-at-top,g-bad-import-order

RELAY = os.environ.get("TASK_RELAY_URL", "")
WORKDIR = os.environ.get("TASK_WORKDIR", "/")
MAX_TEXT = 40_000
MAX_IMAGE = 5 << 20
MAX_PDF_PAGES = 5
MAX_PDF_TOTAL_BYTES = 10 << 20
PDF_DPI_STEPS = ((150, "png"), (100, "png"), (72, "png"), (72, "jpeg"))
IMAGE_MAGIC = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}
_BINARY_MAGIC = (
    (b"AC10", "AutoCAD DWG"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"PK\x05\x06", "ZIP archive"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE2 compound document"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"\x1f\x8b\x08", "gzip compressed"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"\x7fELF", "ELF executable"),
    (b"glTF", "glTF binary"),
)
# the relay is local: skip any HTTP proxy
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# write_file and edit_file take turns, so concurrent edits cannot undo each
# other
_files = threading.Lock()
_err_lock = threading.Lock()
_CAPTURED_SECRETS: list[str] = []
_PDF_TMPDIR_RE = re.compile(r"/tmp/pdfview-[A-Za-z0-9._-]+")


def _extra_secrets() -> tuple[str, ...]:
  """Returns relay URLs, bare relay tokens, and scrubbed process secrets."""
  vals: list[str] = []
  for url in (RELAY, os.environ.get("TASK_RELAY_URL", "")):
    if not url:
      continue
    vals.append(url)
    path_token = urllib.parse.urlsplit(url).path.strip("/").split("/")[0]
    if len(path_token) >= 4:
      vals.append(path_token)
  vals.extend(_CAPTURED_SECRETS)
  return tuple(dict.fromkeys(v for v in vals if v))


def _enforce_frame_limit(label: str, body: bytes) -> None:
  """Raises ValueError if a command payload approaches taskbridge.MAX_FRAME."""
  if len(body) > taskbridge.MAX_FRAME - 1024:
    raise ValueError(
        f"{label}: payload {len(body)} bytes approaches MAX_FRAME"
        f" ({taskbridge.MAX_FRAME}); write a script with write_file instead"
    )


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
            " numbers, png/jpeg/gif/webp images come back as images, and PDF"
            " files are rendered to page images (up to 5 pages per call; pass"
            " pages like '1-3' or '2,4-5' for multi-page PDFs). Use offset and"
            " limit to page through long text files."
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
                "pages": {
                    "type": "string",
                    "description": (
                        "1-based PDF page selection (e.g. '1-3' or '2,4-5';"
                        f" default 1-{MAX_PDF_PAGES}, max {MAX_PDF_PAGES})"
                    ),
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
  if route == "exec":
    _enforce_frame_limit("exec", body)
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
  _enforce_frame_limit("shell command", probe)
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


def parse_pages_spec(
    pages: str | None,
    total_pages: int | None = None,
    max_pages: int = MAX_PDF_PAGES,
) -> list[int]:
  """Parses a 1-based PDF page spec (`'1-3'` or `'2,4-5'`) into page numbers."""
  if total_pages is not None and total_pages < 1:
    raise ValueError("PDF has no pages")
  if pages is None:
    end = max_pages if total_pages is None else min(total_pages, max_pages)
    return list(range(1, end + 1))
  spec = pages.strip()
  if not spec:
    raise ValueError("pages spec must not be empty")
  selected: list[int] = []
  seen: set[int] = set()
  for part in spec.split(","):
    token = part.strip()
    if not token:
      raise ValueError(f"invalid empty token in pages spec {pages!r}")
    if "-" in token:
      pieces = token.split("-")
      if (
          len(pieces) != 2
          or not (pieces[0].isascii() and pieces[0].isdigit())
          or not (pieces[1].isascii() and pieces[1].isdigit())
      ):
        raise ValueError(f"invalid page range {token!r}")
      start, end = int(pieces[0]), int(pieces[1])
      if start < 1 or start > end:
        raise ValueError(f"invalid page range {token!r}")
      if end - start + 1 > max_pages:
        raise ValueError(f"requested range exceeds {max_pages}-page cap")
      span = range(start, end + 1)
    else:
      if not (token.isascii() and token.isdigit()):
        raise ValueError(f"invalid page number {token!r}")
      num = int(token)
      if num < 1:
        raise ValueError(f"page numbers must be >= 1, got {num}")
      span = (num,)
    for num in span:
      if total_pages is not None and num > total_pages:
        raise ValueError(f"page {num} exceeds total_pages={total_pages}")
      if num not in seen:
        seen.add(num)
        selected.append(num)
      if len(selected) > max_pages:
        raise ValueError(
            f"requested pages exceed max {max_pages} pages per read_file call"
        )
  return selected


_CONTAINER_PDF_SCRIPT = """
from __future__ import annotations
import glob, importlib.util, json, os, re, shutil, subprocess, sys, tempfile

path, spec = sys.argv[1], sys.argv[2]
MAX_PDF_PAGES = int(sys.argv[3])
max_img = int(sys.argv[4])
max_total = int(sys.argv[5])
dpi_steps = json.loads(sys.argv[6])

__PARSE_PAGES_SOURCE__

def fail(reason, detail, **extra):
  payload = {"ok": False, "reason": reason, "detail": detail, **extra}
  print(json.dumps(payload))

try:
  with open(path, "rb") as f:
    sig = f.read(5)
except OSError as exc:
  fail("read_error", str(exc))
  sys.exit(0)

if sig != b"%PDF-":
  fail("not_pdf", "missing %PDF- signature")
  sys.exit(0)

tools = {
    "pdftoppm": shutil.which("pdftoppm"),
    "mutool": shutil.which("mutool"),
    "gs": shutil.which("gs"),
    "pdfinfo": shutil.which("pdfinfo"),
    "fitz": importlib.util.find_spec("fitz"),
    "pypdfium2": importlib.util.find_spec("pypdfium2"),
}
if not any(tools[k] for k in ("pdftoppm", "mutool", "gs", "fitz", "pypdfium2")):
  fail("no_renderer", "none of pdftoppm, mutool, gs, fitz, pypdfium2 found")
  sys.exit(0)

def count_pages():
  last_err = ""
  if tools["pdfinfo"]:
    r = subprocess.run([tools["pdfinfo"], path], capture_output=True, text=True)
    if r.returncode == 0:
      m = re.search(r"^Pages:\\s+(\\d+)", r.stdout, re.M)
      if m:
        return int(m.group(1)), ""
    last_err = (r.stderr or r.stdout or "").strip()
    if any(k in last_err.lower() for k in ("password", "encrypt", "syntax", "damaged")):
      return 0, last_err
  if tools["fitz"]:
    try:
      import fitz
      doc = fitz.open(path)
      try:
        if doc.needs_pass:
          return 0, "PDF is encrypted and requires a password"
        return len(doc), ""
      finally:
        doc.close()
    except Exception as exc:
      last_err = str(exc)
  if tools["pypdfium2"]:
    try:
      import pypdfium2
      pdf = pypdfium2.PdfDocument(path)
      try:
        return len(pdf), ""
      finally:
        pdf.close()
    except Exception as exc:
      last_err = str(exc)
  if tools["mutool"]:
    r = subprocess.run([tools["mutool"], "info", path], capture_output=True, text=True)
    if r.returncode == 0:
      m = re.search(r"Pages:\\s+(\\d+)", r.stdout)
      if m:
        return int(m.group(1)), ""
    last_err = (r.stderr or r.stdout or "").strip()
  counts, page_objs, saw_encrypt = [], 0, False
  overlap = b""
  with open(path, "rb") as f:
    while True:
      chunk = f.read(1 << 20)
      if not chunk:
        break
      buf = overlap + chunk
      if b"/Encrypt" in buf:
        saw_encrypt = True
      for m in re.finditer(rb"/Type\\s*/Pages\\b[^>]{0,512}?/Count\\s+(\\d+)", buf):
        counts.append(int(m.group(1)))
      page_objs += len(re.findall(rb"/Type\\s*/Page\\b(?!s)", chunk))
      overlap = buf[-1024:]
  if saw_encrypt and not counts and not page_objs:
    return 0, "PDF is encrypted"
  if counts:
    return max(counts), ""
  if page_objs:
    return page_objs, ""
  return 0, last_err or "unable to determine PDF page count"

total_pages, err = count_pages()
if total_pages < 1:
  fail("corrupt_or_encrypted", err or "invalid or encrypted PDF")
  sys.exit(0)

try:
  target_pages = parse_pages_spec(spec or None, total_pages=total_pages, max_pages=MAX_PDF_PAGES)
except ValueError as exc:
  fail("bad_pages", str(exc), total_pages=total_pages)
  sys.exit(0)

def render_with(renderer, dpi, fmt, tmpdir):
  ext = "png" if fmt == "png" else "jpg"
  mime = "image/png" if fmt == "png" else "image/jpeg"
  out_files = []
  for p in target_pages:
    out_path = os.path.join(tmpdir, f"page_{p}.{ext}")
    if renderer == "pdftoppm":
      prefix = os.path.join(tmpdir, f"raw_{p}")
      flag = "-png" if fmt == "png" else "-jpeg"
      r = subprocess.run(
          [tools["pdftoppm"], flag, "-r", str(dpi), "-f", str(p), "-l", str(p), path, prefix],
          capture_output=True, text=True,
      )
      if r.returncode != 0:
        return None, (r.stderr or r.stdout or "pdftoppm failed").strip()
      matches = sorted(glob.glob(f"{prefix}-*.png") + glob.glob(f"{prefix}-*.jpg"))
      if not matches:
        return None, f"pdftoppm produced no output for page {p}"
      shutil.move(matches[0], out_path)
    elif renderer == "mutool":
      r = subprocess.run(
          [tools["mutool"], "draw", "-q", "-F", fmt, "-r", str(dpi), "-o", out_path, path, str(p)],
          capture_output=True, text=True,
      )
      if r.returncode != 0 or not os.path.exists(out_path):
        return None, (r.stderr or r.stdout or "mutool failed").strip()
    elif renderer == "fitz":
      import fitz
      doc = fitz.open(path)
      try:
        if doc.needs_pass:
          return None, "PDF is encrypted"
        doc[p - 1].get_pixmap(dpi=dpi).save(out_path)
      finally:
        doc.close()
    elif renderer == "pypdfium2":
      import pypdfium2
      pdf = pypdfium2.PdfDocument(path)
      try:
        pil = pdf[p - 1].render(scale=dpi / 72).to_pil()
        pil.save(out_path, format="PNG" if fmt == "png" else "JPEG")
      finally:
        pdf.close()
    elif renderer == "gs":
      dev = "png16m" if fmt == "png" else "jpeg"
      r = subprocess.run(
          [tools["gs"], "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", f"-sDEVICE={dev}", f"-r{dpi}", f"-dFirstPage={p}", f"-dLastPage={p}", f"-sOutputFile={out_path}", path],
          capture_output=True, text=True,
      )
      if r.returncode != 0 or not os.path.exists(out_path):
        return None, (r.stderr or r.stdout or "gs failed").strip()
    sz = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if sz <= 0:
      return None, f"{renderer} produced empty output for page {p}"
    out_files.append({"page": p, "path": out_path, "mime": mime, "bytes": sz})
  return out_files, ""

order = [k for k in ("pdftoppm", "mutool", "fitz", "pypdfium2", "gs") if tools[k]]
tmpdir = tempfile.mkdtemp(prefix="pdfview-", dir="/tmp" if os.path.isdir("/tmp") else None)
last_err = ""
rendered_too_large = None
for dpi, fmt in dpi_steps:
  for name in os.listdir(tmpdir):
    try:
      os.unlink(os.path.join(tmpdir, name))
    except OSError:
      pass
  for renderer in order:
    if renderer in ("fitz", "pypdfium2") and fmt == "jpeg" and tools["pdftoppm"]:
      continue
    try:
      files, err = render_with(renderer, dpi, fmt, tmpdir)
    except Exception as exc:
      files, err = None, str(exc)
    if not files:
      last_err = err or f"{renderer} failed"
      continue
    max_sz = max(item["bytes"] for item in files)
    tot_sz = sum(item["bytes"] for item in files)
    if max_sz <= max_img and tot_sz <= max_total:
      print(json.dumps({
          "ok": True,
          "tmpdir": tmpdir,
          "total_pages": total_pages,
          "rendered_pages": target_pages,
          "dpi": dpi,
          "renderer": renderer,
          "files": files,
      }))
      sys.exit(0)
    rendered_too_large = (dpi, fmt, max_sz, tot_sz)
    break

shutil.rmtree(tmpdir, ignore_errors=True)
if rendered_too_large:
  dpi, fmt, max_sz, tot_sz = rendered_too_large
  fail("too_large", f"rendered {fmt} at {dpi} DPI exceeds byte limits (max_page={max_sz}, total={tot_sz})")
else:
  fail("corrupt_or_encrypted", last_err or "all renderers failed on PDF")
""".strip()


def _build_header_probe_command(path: str) -> str:
  """Builds a lightweight container command checking the file header for %PDF-."""
  script = (
      "import json, sys; "
      "d = open(sys.argv[1], 'rb').read(16); "
      "print(json.dumps({'is_pdf': d.startswith(b'%PDF-')}))"
  )
  qp = shlex.quote(path)
  fallback = (
      f"if [ \"$(head -c 5 {qp} 2>/dev/null | LC_ALL=C tr -d '\\000')\" ="
      " '%PDF-' ]; then echo '{\"is_pdf\":true}'; else echo"
      " '{\"is_pdf\":false}'; fi"
  )
  return (
      "if command -v python3 >/dev/null 2>&1; then "
      f"python3 -c {shlex.quote(script)} {qp}; "
      f"else {fallback}; fi"
  )


def _build_pdf_render_command(path: str, pages: str | None = None) -> str:
  """Builds an in-container python3 command that renders PDF pages into a temp dir."""
  script = _CONTAINER_PDF_SCRIPT.replace(
      "__PARSE_PAGES_SOURCE__", inspect.getsource(parse_pages_spec)
  )
  steps_json = json.dumps([list(step) for step in PDF_DPI_STEPS])
  args = " ".join(
      shlex.quote(str(x))
      for x in (
          path,
          pages or "",
          MAX_PDF_PAGES,
          MAX_IMAGE,
          MAX_PDF_TOTAL_BYTES,
          steps_json,
      )
  )
  missing = (
      '{"ok":false,"reason":"no_renderer",'
      '"detail":"python3 and PDF rasterizers not found"}'
  )
  return (
      f"command -v python3 >/dev/null 2>&1 || {{ echo {shlex.quote(missing)};"
      f" exit 0; }}; python3 -c {shlex.quote(script)} {args}"
  )


def _describe_binary(path: str, data: bytes) -> str:
  """Returns an informative metadata summary + shell hint for non-PDF binaries."""
  for magic, label in _BINARY_MAGIC:
    if data.startswith(magic):
      detail = label
      if magic == b"AC10" and len(data) >= 6:
        ver = data[:6].decode("ascii", "ignore")
        if ver.isprintable():
          detail = f"{label} ({ver})"
      return (
          f"{path} is a {detail} binary file of {len(data)} bytes; inspect or"
          " convert it with shell commands"
      )
  return (
      f"{path} is a binary file of {len(data)} bytes; inspect or convert it"
      " with shell commands"
  )


def _format_pdf_error(path: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
  """Formats an actionable fallback message when PDF rasterization cannot complete."""
  reason = meta.get("reason", "unknown")
  detail = str(meta.get("detail") or "PDF inspection failed").strip()
  total = meta.get("total_pages")
  if reason == "bad_pages":
    bounds = f"1-{total}" if total else "1+"
    return text(
        f"cannot render {path}: {detail}. Choose valid 1-based pages within"
        f" {bounds} (max {MAX_PDF_PAGES} pages per call), or inspect with shell"
        " tools (pdftotext, pdfinfo, mutool, pypdf)."
    )
  if reason == "no_renderer":
    return text(
        f"{path} is a PDF, but no rasterizer is installed ({detail}); inspect or"
        " extract text/images using shell commands (pdftotext, pdfinfo, pypdf,"
        " python3)."
    )
  return text(
      f"cannot render PDF {path} ({reason}: {detail}); inspect or decrypt/"
      "convert it using shell commands (qpdf, pdfinfo, pdftotext, mutool,"
      " pypdf)."
  )


def _read_pdf(path: str, pages: str | None = None) -> list[dict[str, Any]]:
  """Renders up to `MAX_PDF_PAGES` pages of a container PDF into MCP image blocks."""
  if pages is not None:
    try:
      parse_pages_spec(pages, total_pages=None, max_pages=MAX_PDF_PAGES)
    except ValueError as exc:
      return _format_pdf_error(
          path, {"reason": "bad_pages", "detail": str(exc)}
      )

  tmpdir: str | None = None
  try:
    r = relay(
        "exec", command=_build_pdf_render_command(path, pages), timeout_sec=120
    )
    out = (r.get("output") or "").strip()
    m_tmp = _PDF_TMPDIR_RE.search(out)
    if m_tmp:
      tmpdir = m_tmp.group(0)
    if not r.get("ok", True):
      err = str(r.get("error") or "exec failed")
      if not tmpdir:
        m_err = _PDF_TMPDIR_RE.search(err)
        if m_err:
          tmpdir = m_err.group(0)
      return _format_pdf_error(path, {"reason": "exec_error", "detail": err})
    meta = taskbridge.parse_last_json_dict(out)
    if meta is None:
      return _format_pdf_error(
          path,
          {
              "reason": "corrupt_or_encrypted",
              "detail": out or f"exit_code={r.get('exit_code')}",
          },
      )
    if isinstance(meta.get("tmpdir"), str):
      tmpdir = meta["tmpdir"]
    if not meta.get("ok"):
      return _format_pdf_error(path, meta)

    files = meta.get("files") or []
    blocks: list[dict[str, Any]] = []
    total_img_bytes = 0
    for item in files:
      img_bytes = read_bytes(item["path"])
      if len(img_bytes) > MAX_IMAGE:
        return _format_pdf_error(
            path,
            {
                "reason": "too_large",
                "detail": f"page {item.get('page')} is {len(img_bytes)} bytes",
            },
        )
      total_img_bytes += len(img_bytes)
      if total_img_bytes > MAX_PDF_TOTAL_BYTES:
        return _format_pdf_error(
            path,
            {
                "reason": "too_large",
                "detail": "combined page images exceed MCP frame cap",
            },
        )
      blocks.append({
          "type": "image",
          "data": base64.b64encode(img_bytes).decode(),
          "mimeType": item.get("mime", "image/png"),
      })
    rendered = meta.get("rendered_pages") or [f.get("page") for f in files]
    rendered_str = ",".join(str(p) for p in rendered)
    blocks.append({
        "type": "text",
        "text": (
            f"{path} (PDF: rendered pages [{rendered_str}] of"
            f" {meta.get('total_pages')} total at {meta.get('dpi')} DPI via"
            f" {meta.get('renderer')}; pass pages='...' to inspect other"
            f" pages, max {MAX_PDF_PAGES} per call)"
        ),
    })
    return blocks
  finally:
    if isinstance(tmpdir, str) and tmpdir.startswith("/tmp/pdfview-"):
      try:
        relay("exec", command=f"rm -rf {shlex.quote(tmpdir)}", timeout_sec=30)
      except Exception:  # pylint: disable=broad-exception-caught
        pass


def read_file(
    path: str, offset: int = 1, limit: int = 2000, pages: str | None = None
) -> list[dict[str, Any]]:
  """Reads a text, raster image, or PDF file from the task container.

  Args:
    path: Path to the file inside the task container.
    offset: 1-based starting line number for text files.
    limit: Maximum number of lines to return for text files.
    pages: Optional 1-based page range string (`'1-3'`, `'2,4-5'`) for PDFs.

  Returns:
    An MCP content list containing numbered text lines, base64 images, or
    informative metadata for non-text binary files.
  """
  path = resolve(path)
  r = relay("read_file", path=path)
  if not r.get("ok"):
    raise ValueError(r.get("error") or f"could not read {path}")
  if r.get("truncated"):
    probe = relay(
        "exec", command=_build_header_probe_command(path), timeout_sec=15
    )
    probe_meta = taskbridge.parse_last_json_dict(probe.get("output") or "")
    is_pdf = bool(probe_meta and probe_meta.get("is_pdf"))
    if is_pdf:
      return _read_pdf(path, pages)
    raise ValueError(
        f"{path} is {r.get('size')} bytes, over the 10 MB a read returns; work"
        " on it with shell"
    )
  data = base64.b64decode(r["content_b64"])
  if data.startswith(b"%PDF-"):
    return _read_pdf(path, pages)
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
    return text(_describe_binary(path, data))
  if pages is not None:
    return text(f"{path} is a text file; pages only applies to PDF files")
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


def _validate_str(
    key: str,
    val: Any,
    *,
    allow_empty: bool = False,
    allow_whitespace: bool = False,
) -> str:
  """Validates string fields for UTF-8 safety, NUL, and emptiness."""
  if not isinstance(val, str):
    raise ValueError(f"{key} must be a string")
  if "\x00" in val and not allow_empty:
    raise ValueError(f"{key} holds a NUL byte")
  try:
    val.encode("utf-8")
  except UnicodeEncodeError:
    raise ValueError(f"{key} contains unpaired UTF-16 surrogates") from None
  if not allow_empty and (not val or (not allow_whitespace and not val.strip())):
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
      cleaned[key] = _validate_str(
          key,
          val,
          allow_empty=allow_empty,
          allow_whitespace=(key == "old_text"),
      )
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
