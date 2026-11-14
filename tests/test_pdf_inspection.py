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

"""Offline $0 tests for FRNT-4 multimodal PDF and binary drawing inspection."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

os.environ.setdefault("TASK_RELAY_URL", "http://127.0.0.1:9999/deadbeef")
os.environ.setdefault("TASK_WORKDIR", "/app")

import task_mcp  # noqa: E402
import taskbridge  # noqa: E402

SAMPLE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class PdfInspectionTests(unittest.TestCase):
  """Tests FRNT-4.1..4.6 offline without container downloads or model calls."""

  def test_read_file_renders_pdf_to_native_images(self) -> None:
    """FRNT-4.1: %PDF- files render via relay exec and return MCP image + text metadata."""
    read_tool = next(t for t in task_mcp.TOOLS if t["name"] == "read_file")
    self.assertIn("pages", read_tool["inputSchema"]["properties"])

    files = {
        "/app/plan.pdf": b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n",
        "/tmp/pdfview-test/page_1.png": SAMPLE_PNG,
        "/tmp/pdfview-test/page_2.png": SAMPLE_PNG,
    }
    exec_cmds: list[str] = []

    def fake_relay(route: str, **fields):
      if route == "read_file":
        p = fields["path"]
        raw = files.get(p)
        if raw is None:
          return {"ok": False, "error": f"missing {p}"}
        return {"ok": True, "truncated": False, "size": len(raw), "content_b64": base64.b64encode(raw).decode()}
      if route == "exec":
        cmd = fields["command"]
        exec_cmds.append(cmd)
        if "rm -rf" in cmd:
          return {"ok": True, "exit_code": 0, "output": ""}
        return {
            "ok": True,
            "exit_code": 0,
            "output": json.dumps({
                "ok": True,
                "tmpdir": "/tmp/pdfview-test",
                "total_pages": 7,
                "rendered_pages": [1, 2],
                "dpi": 150,
                "renderer": "pdftoppm",
                "files": [
                    {"page": 1, "path": "/tmp/pdfview-test/page_1.png", "mime": "image/png", "bytes": len(SAMPLE_PNG)},
                    {"page": 2, "path": "/tmp/pdfview-test/page_2.png", "mime": "image/png", "bytes": len(SAMPLE_PNG)},
                ],
            }),
        }
      raise AssertionError(f"unexpected route: {route}")

    with mock.patch.object(task_mcp, "relay", side_effect=fake_relay):
      blocks = task_mcp.read_file("/app/plan.pdf", pages="1-2")

    image_blocks = [b for b in blocks if b.get("type") == "image"]
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    self.assertEqual(len(image_blocks), 2)
    self.assertEqual(image_blocks[0]["mimeType"], "image/png")
    self.assertEqual(base64.b64decode(image_blocks[0]["data"]), SAMPLE_PNG)
    self.assertEqual(len(text_blocks), 1)
    meta = text_blocks[0]["text"]
    self.assertIn("/app/plan.pdf", meta)
    self.assertIn("7", meta)
    self.assertIn("150", meta)
    self.assertIn("pages", meta.lower())
    self.assertTrue(any("rm -rf" in c and "/tmp/pdfview-test" in c for c in exec_cmds))

  def test_pdf_page_cap_and_explicit_page_range(self) -> None:
    """FRNT-4.2: Default page cap (5) and explicit page range selection/rejection work."""
    self.assertEqual(task_mcp.MAX_PDF_PAGES, 5)
    self.assertEqual(task_mcp.parse_pages_spec(None, total_pages=12), [1, 2, 3, 4, 5])
    self.assertEqual(task_mcp.parse_pages_spec(None, total_pages=3), [1, 2, 3])
    self.assertEqual(task_mcp.parse_pages_spec("2,4-5", total_pages=10), [2, 4, 5])
    for bad in ("0", "6-3", "1-6", "abc", "1,2,3,4,5,6", "9-10"):
      with self.assertRaises(ValueError, msg=f"expected ValueError for {bad!r}"):
        task_mcp.parse_pages_spec(bad, total_pages=8)

  def test_pdf_downscaling_and_over_10mb_pdf_support(self) -> None:
    """FRNT-4.3: Large >10 MB PDFs inspect via exec cleanly and container script downscales."""
    # 1) Over-10MB PDF route: truncated=True triggers in-container header probe + _read_pdf
    big_pdf_files = {
        "/tmp/pdfview-big/page_1.png": SAMPLE_PNG,
    }

    def fake_big_relay(route: str, **fields):
      if route == "read_file":
        p = fields["path"]
        if p == "/app/giant.pdf":
          return {"ok": True, "truncated": True, "size": 15_000_000}
        raw = big_pdf_files[p]
        return {"ok": True, "truncated": False, "size": len(raw), "content_b64": base64.b64encode(raw).decode()}
      if route == "exec":
        cmd = fields["command"]
        if "rm -rf" in cmd:
          return {"ok": True, "exit_code": 0, "output": ""}
        if "head" in cmd or "read(16)" in cmd:
          return {"ok": True, "exit_code": 0, "output": json.dumps({"is_pdf": True})}
        return {
            "ok": True,
            "exit_code": 0,
            "output": json.dumps({
                "ok": True,
                "tmpdir": "/tmp/pdfview-big",
                "total_pages": 1,
                "rendered_pages": [1],
                "dpi": 100,
                "renderer": "pdftoppm",
                "files": [{"page": 1, "path": "/tmp/pdfview-big/page_1.png", "mime": "image/png", "bytes": len(SAMPLE_PNG)}],
            }),
        }
      raise AssertionError(route)

    with mock.patch.object(task_mcp, "relay", side_effect=fake_big_relay):
      blocks = task_mcp.read_file("/app/giant.pdf")
    self.assertEqual(len([b for b in blocks if b["type"] == "image"]), 1)

    # 2) Execute the real in-container render script with a stub pdftoppm that writes oversized file at 150 DPI then compact PNG at 100 DPI
    with tempfile.TemporaryDirectory() as tmp:
      tmp_path = pathlib.Path(tmp)
      bin_dir = tmp_path / "bin"
      bin_dir.mkdir()
      pdf_path = tmp_path / "big_sheet.pdf"
      pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Pages/Count 1>>endobj\n%%EOF\n")
      fake_pdftoppm = bin_dir / "pdftoppm"
      fake_pdftoppm.write_text(
          "#!/usr/bin/env python3\n"
          "import pathlib, sys\n"
          "args = sys.argv[1:]\n"
          "dpi = 150\n"
          "if '-r' in args:\n"
          "  dpi = int(args[args.index('-r') + 1])\n"
          "prefix = pathlib.Path(args[-1])\n"
          "out = prefix.parent / f'{prefix.name}-1.png'\n"
          f"small = {SAMPLE_PNG!r}\n"
          "out.write_bytes((b'\\x89PNG' + b'X' * (5_500_000)) if dpi >= 150 else small)\n"
      )
      fake_pdftoppm.chmod(fake_pdftoppm.stat().st_mode | stat.S_IEXEC)
      cmd = task_mcp._build_pdf_render_command(str(pdf_path), "1")
      res = subprocess.run(
          ["bash", "-c", cmd],
          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": tmp},
          text=True,
          capture_output=True,
          timeout=15,
          check=False,
      )
      self.assertEqual(res.returncode, 0, res.stderr)
      meta = json.loads(res.stdout)
      self.assertTrue(meta["ok"], meta)
      self.assertLessEqual(meta["dpi"], 100)
      self.assertLessEqual(meta["files"][0]["bytes"], task_mcp.MAX_IMAGE)
      # Clean up temp dir created by test subprocess
      subprocess.run(["rm", "-rf", meta["tmpdir"]], check=False)

  def test_pdf_missing_renderer_corrupt_and_bad_range_fallback(self) -> None:
    """FRNT-4.4: Missing rasterizer, corrupt PDF, and bad range return actionable text without crashing."""
    for reason_payload in (
        {"ok": False, "reason": "no_renderer", "detail": "none of pdftoppm, mutool, gs, fitz, pypdfium2 found"},
        {"ok": False, "reason": "corrupt_or_encrypted", "detail": "Invalid PDF xref / encrypted"},
        {"ok": False, "reason": "bad_pages", "detail": "page 9 exceeds total_pages=3", "total_pages": 3},
    ):
      def fake_err_relay(route: str, payload=reason_payload, **fields):
        if route == "read_file":
          return {"ok": True, "truncated": False, "size": 32, "content_b64": base64.b64encode(b"%PDF-1.7\n%%EOF\n").decode()}
        if route == "exec":
          return {"ok": True, "exit_code": 0, "output": json.dumps(payload)}
        raise AssertionError(route)

      with mock.patch.object(task_mcp, "relay", side_effect=fake_err_relay):
        blocks = task_mcp.read_file("/app/bad.pdf")
      self.assertEqual(len(blocks), 1)
      self.assertEqual(blocks[0]["type"], "text")
      self.assertIn("shell", blocks[0]["text"].lower())

    # Stderr warnings before valid JSON must still render + clean up tmpdir
    noisy_cmds: list[str] = []

    def fake_noisy_relay(route: str, **fields):
      if route == "read_file":
        raw = b"%PDF-1.7\n%%EOF\n" if fields["path"] == "/app/noisy.pdf" else SAMPLE_PNG
        return {"ok": True, "truncated": False, "size": len(raw), "content_b64": base64.b64encode(raw).decode()}
      if route == "exec":
        cmd = fields["command"]
        noisy_cmds.append(cmd)
        if "rm -rf" in cmd:
          return {"ok": True, "exit_code": 0, "output": ""}
        payload = {
            "ok": True,
            "tmpdir": "/tmp/pdfview-noisy",
            "total_pages": 1,
            "rendered_pages": [1],
            "dpi": 150,
            "renderer": "fitz",
            "files": [{"page": 1, "path": "/tmp/pdfview-noisy/page_1.png", "mime": "image/png", "bytes": len(SAMPLE_PNG)}],
        }
        return {"ok": True, "exit_code": 0, "output": "MuPDF warning: minor xref issue\n" + json.dumps(payload)}
      raise AssertionError(route)

    with mock.patch.object(task_mcp, "relay", side_effect=fake_noisy_relay):
      noisy_blocks = task_mcp.read_file("/app/noisy.pdf")
    self.assertEqual(len([b for b in noisy_blocks if b["type"] == "image"]), 1)
    self.assertTrue(any("rm -rf" in c and "/tmp/pdfview-noisy" in c for c in noisy_cmds))

  def test_raster_image_magic_and_text_cad_no_regression(self) -> None:
    """FRNT-4.5: PNG/WEBP magic images, oversize image rejection, and UTF-8 ASCII CAD text unchanged."""
    webp = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16
    ascii_dxf = "0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n".encode("utf-8")
    too_big_png = b"\x89PNG\r\n\x1a\n" + (b"0" * (task_mcp.MAX_IMAGE + 10))
    fake_store = {
        "/app/img.webp": webp,
        "/app/plan.dxf": ascii_dxf,
        "/app/huge.png": too_big_png,
    }

    def fake_relay(route: str, **fields):
      self.assertEqual(route, "read_file")
      raw = fake_store[fields["path"]]
      return {"ok": True, "truncated": False, "size": len(raw), "content_b64": base64.b64encode(raw).decode()}

    with mock.patch.object(task_mcp, "relay", side_effect=fake_relay):
      img_blocks = task_mcp.read_file("/app/img.webp")
      self.assertEqual(img_blocks[0]["type"], "image")
      self.assertEqual(img_blocks[0]["mimeType"], "image/webp")

      big_blocks = task_mcp.read_file("/app/huge.png")
      self.assertEqual(big_blocks[0]["type"], "text")
      self.assertIn("too large to view", big_blocks[0]["text"])

      dxf_blocks = task_mcp.read_file("/app/plan.dxf", offset=2, limit=3)
      self.assertEqual(dxf_blocks[0]["type"], "text")
      self.assertIn("     2\tSECTION", dxf_blocks[0]["text"])
      self.assertIn("[lines 2-4 of 8]", dxf_blocks[0]["text"])

  def test_binary_cad_metadata_summary(self) -> None:
    """FRNT-4.6: Non-PDF NUL-containing binaries (.dwg/.zip/.rvt) summarize format + size + shell hints."""
    samples = {
        "/app/site.dwg": (b"AC1032\x00\x00" + b"\x00" * 120, "AutoCAD DWG"),
        "/app/model.zip": (b"PK\x03\x04\x00\x00" + b"\x00" * 64, "ZIP"),
        "/app/building.rvt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00" + b"\x00" * 64, "OLE2"),
    }

    def fake_relay(route: str, **fields):
      raw = samples[fields["path"]][0]
      return {"ok": True, "truncated": False, "size": len(raw), "content_b64": base64.b64encode(raw).decode()}

    with mock.patch.object(task_mcp, "relay", side_effect=fake_relay):
      for path, (raw, label) in samples.items():
        blocks = task_mcp.read_file(path)
        self.assertEqual(len(blocks), 1)
        txt = blocks[0]["text"]
        self.assertIn(label, txt)
        self.assertIn(str(len(raw)), txt)
        self.assertIn("shell", txt.lower())

  def test_pdf_failed_json_parse_still_cleans_up_tmpdir(self) -> None:
    """FRNT-4.5: Cleanup removes /tmp/pdfview-* even when renderer crashes non-JSON."""
    probe_cmd = task_mcp._build_header_probe_command("/tmp/a b.pdf")
    self.assertIn("head -c 5", probe_cmd)
    self.assertIn("read(16)", probe_cmd)

    exec_cmds: list[str] = []

    def fake_relay(route: str, **fields):
      if route == "read_file":
        return {
            "ok": True,
            "truncated": False,
            "size": 16,
            "content_b64": base64.b64encode(b"%PDF-1.4\n%%EOF\n").decode(),
        }
      if route == "exec":
        cmd = fields["command"]
        exec_cmds.append(cmd)
        if cmd.startswith("rm -rf"):
          return {"ok": True, "exit_code": 0, "output": ""}
        return {
            "ok": True,
            "exit_code": 1,
            "output": "fatal: crashed working in /tmp/pdfview-bad\nnot json",
        }
      raise AssertionError(route)

    with mock.patch.object(task_mcp, "relay", side_effect=fake_relay):
      blocks = task_mcp.read_file("/app/broken.pdf")

    self.assertEqual(len(blocks), 1)
    self.assertIn("corrupt_or_encrypted", blocks[0]["text"])
    self.assertTrue(
        any("rm -rf" in c and "/tmp/pdfview-bad" in c for c in exec_cmds)
    )


if __name__ == "__main__":
  unittest.main()
