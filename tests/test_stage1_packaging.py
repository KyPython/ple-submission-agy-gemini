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

"""Offline Stage 1 packaging, licensing, and boundary compliance checks (FRNT-6)."""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLE_BENCH = ROOT.parent / "ple-bench"
KEY_RE = re.compile(
    rb"(?<![A-Za-z0-9])(?:(?P<prefix>sk-(?:ant-|proj-)?|AIza|gh[pousr]_|hf_|xox[baprs]-)"
    rb"(?P<body>[A-Za-z0-9_-]{20,4000})|-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----)"
)
MAX_SOURCE_BYTES = 1 << 30
MAX_BUNDLE_BYTES = 2 << 30
TARGET_BUNDLE_BYTES = 512 << 20
FORBIDDEN_PATH_TOKENS = (
    "artifacts/",
    "/artifacts",
    "ple-bench/eval",
    "eval/run.py",
    "harbor_grader",
)


class Stage1PackagingTests(unittest.TestCase):
  """Verifies FRNT-6.1..6.5 compliance offline at $0 cost."""

  def test_root_apache2_license_present(self) -> None:
    """FRNT-6.1: Root LICENSE exists with verbatim Apache-2.0 terms."""
    license_path = ROOT / "LICENSE"
    self.assertTrue(license_path.is_file(), f"missing root {license_path}")
    text = license_path.read_text(encoding="utf-8")
    self.assertIn("Apache License", text)
    self.assertIn("Version 2.0, January 2004", text)
    self.assertIn("http://www.apache.org/licenses/", text)
    self.assertGreater(len(text), 10_000)

  def test_submission_yaml_stage2_and_kit_validate(self) -> None:
    """FRNT-6.2: submission.yaml matches Stage 2 config and passes kit validate."""
    sub_text = (ROOT / "submission.yaml").read_text(encoding="utf-8")
    self.assertRegex(sub_text, r"(?m)^name:\s*agy-gemini\s*$")
    self.assertRegex(
        sub_text, r"(?m)^model_api:\s*google-cloud/gemini-4-argon\s*$"
    )
    self.assertRegex(
        sub_text, r"(?m)^install:\s*bash \{src\}/install\.sh\s*$"
    )
    self.assertRegex(
        sub_text, r"(?m)^serve:\s*/opt/harness/venv/bin/python \{src\}/serve\.py\s*$"
    )
    self.assertRegex(sub_text, r'(?m)^\s+AGY_EFFORT:\s*"high"\s*$')

    if PLE_BENCH.is_dir():
      proc = subprocess.run(
          ["uv", "--directory", str(PLE_BENCH), "run", "python", "-m", "kit", "validate", str(ROOT)],
          text=True,
          capture_output=True,
          timeout=30,
          check=False,
      )
      self.assertEqual(
          proc.returncode,
          0,
          f"kit validate failed: stdout={proc.stdout!r} stderr={proc.stderr!r}",
      )
      self.assertIn("google-cloud/gemini-4-argon", proc.stdout)
      self.assertIn("egress none", proc.stdout)

  def test_uv_venv_isolation_and_bundle_size_guard(self) -> None:
    """FRNT-6.3: Dependencies isolated via uv in /opt/harness/venv within size budget."""
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    non_empty = [line.strip() for line in reqs if line.strip() and not line.strip().startswith("#")]
    self.assertEqual(non_empty, ["websockets==15.0.1"])

    install_text = (ROOT / "install.sh").read_text(encoding="utf-8")
    self.assertIn("uv venv --python 3.12 /opt/harness/venv", install_text)
    self.assertIn("uv pip install --python /opt/harness/venv/bin/python", install_text)
    self.assertIn("requirements.txt", install_text)

    allowed_non_stdlib = {"websockets", "taskbridge", "deliverable_gate"}
    for py_path in sorted(ROOT.glob("*.py")):
      tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
      for node in ast.walk(tree):
        if isinstance(node, ast.Import):
          roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
          roots = [node.module.split(".")[0]]
        else:
          continue
        for mod in roots:
          if mod not in sys.stdlib_module_names and mod not in allowed_non_stdlib:
            self.fail(f"{py_path.name} imports undeclared non-stdlib module {mod!r}")

    source_bytes = sum(
        p.stat().st_size
        for p in ROOT.rglob("*")
        if p.is_file() and ".git" not in p.parts and ".superpowers" not in p.parts
    )
    self.assertLess(source_bytes, MAX_SOURCE_BYTES)

    cached_bundle = pathlib.Path.home() / ".cache" / "plebench" / "agy-gemini" / "bundle.tar.gz"
    if cached_bundle.is_file():
      bundle_size = cached_bundle.stat().st_size
      self.assertLess(bundle_size, MAX_BUNDLE_BYTES)
      self.assertLessEqual(bundle_size, TARGET_BUNDLE_BYTES)

  def test_lf_line_endings_no_keys_no_lfs_no_excluded_dirs(self) -> None:
    """FRNT-6.4: LF scripts, zero keys, zero Git LFS pointers, no code in artifacts*."""
    scripts = [ROOT / "install.sh", *ROOT.glob("*.sh")]
    for script in scripts:
      raw = script.read_bytes()
      self.assertNotIn(b"\r\n", raw, f"CRLF line endings found in {script}")

    for path in sorted(ROOT.rglob("*")):
      if not path.is_file() or ".git" in path.parts or ".superpowers" in path.parts:
        continue
      rel = path.relative_to(ROOT).as_posix()
      if rel.startswith("artifacts") and path.suffix in {".py", ".sh"}:
        self.fail(f"harness code stored inside bundle-excluded path {rel}")
      raw = path.read_bytes()
      self.assertFalse(
          raw.startswith(b"version https://git-lfs.github.com/spec/v1"),
          f"Git LFS pointer detected in {rel}",
      )
      for match in KEY_RE.finditer(raw):
        body = match.group("body")
        is_pem = body is None
        is_gen = is_pem or (
            bool(re.search(rb"[A-Z]", body))
            and bool(re.search(rb"[a-z]", body))
            and bool(re.search(rb"[0-9]", body))
        )
        if is_gen:
          self.fail(f"suspected key material matched in {rel}")

  def test_no_artifact_or_grader_reads_and_offline_mock_suite(self) -> None:
    """FRNT-6.5: Runtime harness files never reference artifacts/, eval/, or graders."""
    for py_path in sorted(ROOT.glob("*.py")):
      source = py_path.read_text(encoding="utf-8")
      tree = ast.parse(source, filename=str(py_path))
      literals = [
          node.value
          for node in ast.walk(tree)
          if isinstance(node, ast.Constant) and isinstance(node.value, str)
      ]
      for text in literals:
        low = text.lower()
        for token in FORBIDDEN_PATH_TOKENS:
          self.assertNotIn(
              token,
              low,
              f"forbidden grader/artifact reference {token!r} found in {py_path.name}",
          )


if __name__ == "__main__":
  unittest.main()
