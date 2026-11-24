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

"""Offline $0 tests for FRNT-5 programmatic deliverable verification gate."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

os.environ.setdefault("TASK_RELAY_URL", "http://127.0.0.1:9999/deadbeef")
os.environ.setdefault("TASK_WORKDIR", "/app")
os.environ.setdefault("MODEL_NAME", "gemini-4-argon")

import deliverable_gate  # noqa: E402
import serve  # noqa: E402
import taskbridge  # noqa: E402


class LocalContainerSession:
  """In-process mock taskbridge.Session executing bash commands inside a temp workdir."""

  def __init__(
      self,
      workdir: str,
      instruction: str = "Generate /app/out/report.json.",
      mcp_servers: list[dict] | None = None,
      time_left_sec: float | None = 3600.0,
  ) -> None:
    self.workdir = workdir
    self.instruction = instruction
    self.mcp_servers = mcp_servers or []
    self._time_left = time_left_sec
    self.final_message = ""
    self.cancelled = asyncio.Event()
    self.logs: list[str] = []
    self.usage_calls: list[tuple] = []
    self.exec_commands: list[str] = []
    self.http_calls: list[tuple[str, str, bytes]] = []

  def time_left(self) -> float | None:
    return self._time_left

  async def log(self, text: str) -> None:
    self.logs.append(text)

  async def usage(
      self,
      model: str,
      input_tokens: int = 0,
      output_tokens: int = 0,
      cache_read_tokens: int = 0,
  ) -> None:
    self.usage_calls.append((model, input_tokens, output_tokens, cache_read_tokens))

  async def exec(self, command: str, timeout_sec: int = 120) -> dict:
    self.exec_commands.append(command)
    proc = subprocess.run(
        ["bash", "-c", command],
        cwd=self.workdir,
        capture_output=True,
        text=True,
        timeout=min(timeout_sec, 30),
        check=False,
    )
    out = proc.stdout + proc.stderr
    return {
        "ok": True,
        "exit_code": proc.returncode,
        "output": out,
        "truncated": False,
        "timed_out": False,
    }

  async def read_result(self, path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
      return {"ok": False, "error": f"missing: {path}"}
    return {"ok": True, "size": p.stat().st_size, "content_b64": ""}

  async def write_file(self, path: str, data: bytes, append: bool = False) -> int:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    with open(p, mode) as f:
      f.write(data)
    return len(data)


class FakeRelay:
  """Minimal in-process Relay stub avoiding loopback sockets in sandboxed unit tests."""

  def __init__(self, session: LocalContainerSession) -> None:
    self.session = session
    self.token = "fake-token"
    self.url = "http://127.0.0.1:1/fake-token"
    self.written_paths: list[str] = []
    self.allowed_mcp_tools: dict[str, set[str]] = {}

  async def __aenter__(self) -> FakeRelay:
    return self

  async def __aexit__(self, *args) -> None:
    return None

  def mcp_url(self, name: str) -> str:
    return f"{self.url}/mcp/{name}"

  def set_mcp_policy(self, server_name: str, allowed_tools: set[str]) -> None:
    self.allowed_mcp_tools[server_name] = set(allowed_tools)

  def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
    return {**(base or {}), "TASK_RELAY_URL": self.url}


def _stream_lines(conversation: str, message: str) -> str:
  return "\n".join([
      json.dumps({"event": "init", "conversation_id": conversation}),
      json.dumps({
          "event": "step",
          "step_update": {
              "step_index": 0,
              "step_type": "agent_response",
              "text_delta": message,
              "state": "DONE",
          },
      }),
      json.dumps({
          "event": "result",
          "result": {
              "status": "OK",
              "response": message,
              "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0},
          },
      }),
  ]) + "\n"


class DeliverableGateTests(unittest.TestCase):
  """Offline $0 verification gate tests covering FRNT-5.1..5.4."""

  def test_post_run_gate_executes_offline_in_solve(self) -> None:
    """FRNT-5.1: solve() runs $0 in-container deliverable verification before exiting."""
    with tempfile.TemporaryDirectory() as workdir:
      out = pathlib.Path(workdir) / "report.json"
      out.write_text('{"ok": true, "value": 42}\n')
      session = LocalContainerSession(
          workdir=workdir,
          instruction=f"Write JSON to {out}.",
      )
      fake_relay = FakeRelay(session)
      calls: list[list[str]] = []

      async def fake_run_process(argv, sess, *, env, cwd, stdin, stop_before_sec, log_prefix):
        calls.append(list(argv))
        pathlib.Path(env["AGY_STREAM"]).write_text(
            _stream_lines("conv-1", f"Wrote {out}."),
            encoding="utf-8",
        )
        return taskbridge.Finished(code=0, stderr="")

      with (
          mock.patch.dict(os.environ, {"HOME": workdir}),
          mock.patch.object(taskbridge, "Relay", return_value=fake_relay),
          mock.patch.object(taskbridge, "run_process", side_effect=fake_run_process),
      ):
        result = asyncio.run(serve.solve(session))

    self.assertIn(str(out), result)
    self.assertEqual(len(calls), 1)
    self.assertEqual(len(session.usage_calls), 1)
    self.assertGreaterEqual(len(session.exec_commands), 2)
    verifications = [
        json.loads(line)
        for line in session.logs
        if '"deliverable_verification"' in line
    ]
    self.assertEqual(len(verifications), 1)
    summary = verifications[0]
    self.assertEqual(summary["event"], "deliverable_verification")
    self.assertFalse(summary["repair_triggered"])
    self.assertEqual(summary["failed"], 0)
    self.assertGreaterEqual(summary["passed"], 1)

  def test_candidate_discovery_and_format_validators(self) -> None:
    """FRNT-5.2: Task-agnostic discovery + zero-byte/zip/json/csv/xml/svg/py/pdf checks."""
    with tempfile.TemporaryDirectory() as workdir:
      root = pathlib.Path(workdir)
      session = LocalContainerSession(workdir=workdir)
      before = asyncio.run(deliverable_gate.snapshot_workdir(session, workdir))

      # Valid deliverables
      (root / "ok.json").write_text('{"a": 1}\n')
      (root / "ok.csv").write_text("col1,col2\n1,2\n")
      (root / "ok.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
      (root / "ok.py").write_text("x = 1 + 2\n")
      (root / "ok.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
      with zipfile.ZipFile(root / "ok.zip", "w") as zf:
        zf.writestr("hello.txt", "world")

      # Bad deliverables
      (root / "empty.txt").write_bytes(b"")
      (root / "bad.json").write_text("{not json")
      (root / "bad.csv").write_text("\n\n")
      (root / "bad.xml").write_text("<unclosed>")
      (root / "bad.py").write_text("def broken(:\n")
      (root / "bad.pdf").write_bytes(b"%PDF-1.4\ntruncated without marker")
      (root / "bad.zip").write_bytes(b"PK\x03\x04corruptzipdata")

      # Scratch noise that MUST be ignored
      cache_dir = root / "__pycache__"
      cache_dir.mkdir()
      (cache_dir / "mod.cpython-312.pyc").write_bytes(b"\x00\x00")
      (root / "scratch.tmp").write_bytes(b"")

      after = asyncio.run(deliverable_gate.snapshot_workdir(session, workdir))
      missing_target = str(root / "missing.json")
      candidates = deliverable_gate.discover_candidates(
          workdir=workdir,
          instruction=f"Save final results to {missing_target} and visit https://example.com/ignored.json.",
          final_message=f"Done. Wrote {root / 'ok.json'}.",
          written_paths=[str(root / "empty.txt")],
          before=before,
          after=after,
      )
      self.assertIn(missing_target, candidates)
      self.assertFalse(any("__pycache__" in p or p.endswith(".tmp") or "example.com" in p for p in candidates))

      report = asyncio.run(deliverable_gate.verify_deliverables(session, candidates))
      by_path = {entry["path"]: entry for entry in report["checked"]}

      for ok_name in ("ok.json", "ok.csv", "ok.svg", "ok.py", "ok.pdf", "ok.zip"):
        p = str(root / ok_name)
        self.assertTrue(by_path[p]["ok"], f"expected ok for {ok_name}: {by_path[p]}")

      for bad_name in ("empty.txt", "bad.json", "bad.csv", "bad.xml", "bad.py", "bad.pdf", "bad.zip"):
        p = str(root / bad_name)
        self.assertFalse(by_path[p]["ok"], f"expected failure for {bad_name}: {by_path[p]}")
        self.assertTrue(by_path[p]["error"])
      self.assertFalse(by_path[missing_target]["ok"])

  def test_failed_gate_triggers_bounded_conversation_repair(self) -> None:
    """FRNT-5.3: Failed deliverables trigger agy --conversation repair within MAX_REPAIR_ATTEMPTS."""
    with tempfile.TemporaryDirectory() as workdir:
      target = pathlib.Path(workdir) / "summary.json"
      session = LocalContainerSession(
          workdir=workdir,
          instruction=f"Write valid JSON to {target}.",
          time_left_sec=1800.0,
      )
      fake_relay = FakeRelay(session)
      invocations: list[list[str]] = []

      async def fake_run_process(argv, sess, *, env, cwd, stdin, stop_before_sec, log_prefix):
        invocations.append(list(argv))
        turn = len(invocations)
        if turn == 1:
          # First attempt leaves corrupt/empty output
          target.write_bytes(b"")
          fake_relay.written_paths.append(str(target))
          msg = f"Created {target}."
        else:
          # Repair turn fixes it
          target.write_text('{"repaired": true}\n')
          msg = f"Repaired {target}."
        pathlib.Path(env["AGY_STREAM"]).write_text(
            _stream_lines("conv-repair-1", msg),
            encoding="utf-8",
        )
        return taskbridge.Finished(code=0, stderr="")

      with (
          mock.patch.dict(os.environ, {"HOME": workdir}),
          mock.patch.object(taskbridge, "Relay", return_value=fake_relay),
          mock.patch.object(taskbridge, "run_process", side_effect=fake_run_process),
      ):
        result = asyncio.run(serve.solve(session))

    self.assertEqual(len(invocations), 2)
    self.assertIn("--conversation", invocations[1])
    self.assertIn("conv-repair-1", invocations[1])
    self.assertIn("Repaired", result)
    verifications = [
        json.loads(line)
        for line in session.logs
        if '"deliverable_verification"' in line
    ]
    self.assertEqual(len(verifications), 1)
    self.assertTrue(verifications[0]["repair_triggered"])
    self.assertEqual(verifications[0]["failed"], 0)
    self.assertGreaterEqual(verifications[0]["passed"], 1)

  def test_gate_pass_exhaustion_low_budget_and_cancel_exit(self) -> None:
    """FRNT-5.4: Exhaustion, low time budget, and cancellation skip extra repair safely."""
    self.assertEqual(deliverable_gate.MAX_REPAIR_ATTEMPTS, 2)
    self.assertEqual(deliverable_gate.REPAIR_RESERVE_SEC, 120)

    # 1) Repair exhaustion keeps partial work and logs failed > 0 without falsely claiming pass
    with tempfile.TemporaryDirectory() as workdir:
      bad = pathlib.Path(workdir) / "broken.json"
      good = pathlib.Path(workdir) / "partial_ok.txt"
      session = LocalContainerSession(
          workdir=workdir,
          instruction=f"Write {bad} and {good}.",
          time_left_sec=1800.0,
      )
      fake_relay = FakeRelay(session)
      calls = 0

      async def always_broken(argv, sess, *, env, cwd, stdin, stop_before_sec, log_prefix):
        nonlocal calls
        calls += 1
        good.write_text("useful partial output\n")
        bad.write_text("{still invalid json")
        pathlib.Path(env["AGY_STREAM"]).write_text(
            _stream_lines("conv-exhaust", "Partial answer preserved."),
            encoding="utf-8",
        )
        return taskbridge.Finished(code=0, stderr="")

      with (
          mock.patch.dict(os.environ, {"HOME": workdir}),
          mock.patch.object(taskbridge, "Relay", return_value=fake_relay),
          mock.patch.object(taskbridge, "run_process", side_effect=always_broken),
      ):
        res = asyncio.run(serve.solve(session))
      self.assertEqual(good.read_text(), "useful partial output\n")

    self.assertEqual(calls, 1 + deliverable_gate.MAX_REPAIR_ATTEMPTS)
    self.assertIn("Partial answer preserved.", res)
    summary = [json.loads(l) for l in session.logs if '"deliverable_verification"' in l][-1]
    self.assertTrue(summary["repair_triggered"])
    self.assertEqual(summary["failed"], 1)

    # 2) Low time budget skips repair even when deliverables fail
    with tempfile.TemporaryDirectory() as workdir:
      bad = pathlib.Path(workdir) / "broken.json"
      session = LocalContainerSession(
          workdir=workdir,
          instruction=f"Write {bad}.",
          time_left_sec=float(serve.STOP_BEFORE_SEC + deliverable_gate.REPAIR_RESERVE_SEC),
      )
      fake_relay = FakeRelay(session)
      low_calls = 0

      async def low_budget_run(argv, sess, *, env, cwd, stdin, stop_before_sec, log_prefix):
        nonlocal low_calls
        low_calls += 1
        bad.write_bytes(b"")
        pathlib.Path(env["AGY_STREAM"]).write_text(
            _stream_lines("conv-low", "Finished with low budget."),
            encoding="utf-8",
        )
        return taskbridge.Finished(code=0, stderr="")

      with (
          mock.patch.dict(os.environ, {"HOME": workdir}),
          mock.patch.object(taskbridge, "Relay", return_value=fake_relay),
          mock.patch.object(taskbridge, "run_process", side_effect=low_budget_run),
      ):
        asyncio.run(serve.solve(session))

    self.assertEqual(low_calls, 1)
    low_summary = [json.loads(l) for l in session.logs if '"deliverable_verification"' in l][-1]
    self.assertFalse(low_summary["repair_triggered"])
    self.assertEqual(low_summary["failed"], 1)

    # 3) Cancelled session exits cleanly without starting repair
    with tempfile.TemporaryDirectory() as workdir:
      session = LocalContainerSession(workdir=workdir, time_left_sec=1800.0)
      fake_relay = FakeRelay(session)
      cancel_calls = 0

      async def cancel_run(argv, sess, *, env, cwd, stdin, stop_before_sec, log_prefix):
        nonlocal cancel_calls
        cancel_calls += 1
        sess.cancelled.set()
        pathlib.Path(env["AGY_STREAM"]).write_text(
            _stream_lines("conv-cancel", "Cancelled mid-run."),
            encoding="utf-8",
        )
        return taskbridge.Finished(code=0, stderr="")

      with (
          mock.patch.dict(os.environ, {"HOME": workdir}),
          mock.patch.object(taskbridge, "Relay", return_value=fake_relay),
          mock.patch.object(taskbridge, "run_process", side_effect=cancel_run),
      ):
        asyncio.run(serve.solve(session))

    self.assertEqual(cancel_calls, 1)
    cancel_summary = [json.loads(l) for l in session.logs if '"deliverable_verification"' in l][-1]
    self.assertFalse(cancel_summary["repair_triggered"])

    # 4) Failed repair turn with partial text does NOT overwrite valid Turn-0 final_message
    with tempfile.TemporaryDirectory() as workdir:
      target = pathlib.Path(workdir) / "out.json"
      session = LocalContainerSession(
          workdir=workdir,
          instruction=f"Write {target}.",
          time_left_sec=1800.0,
      )
      fake_relay = FakeRelay(session)
      err_calls = 0

      async def good_then_crashed_repair(argv, sess, *, env, cwd, stdin, stop_before_sec, log_prefix):
        nonlocal err_calls
        err_calls += 1
        target.write_text("{bad json")
        if err_calls == 1:
          pathlib.Path(env["AGY_STREAM"]).write_text(
              _stream_lines("conv-preserve", "Complete Turn 0 Answer."),
              encoding="utf-8",
          )
        else:
          pathlib.Path(env["AGY_STREAM"]).write_text(
              "\n".join([
                  json.dumps({"event": "init", "conversation_id": "conv-preserve"}),
                  json.dumps({
                      "event": "step",
                      "step_update": {
                          "step_index": 0,
                          "step_type": "agent_response",
                          "text_delta": "Starting repair fragment...",
                          "state": "ACTIVE",
                      },
                  }),
                  json.dumps({"event": "result", "result": {"status": "ERROR", "error": "transient error"}}),
              ]) + "\n",
              encoding="utf-8",
          )
        return taskbridge.Finished(code=1, stderr="")

      with (
          mock.patch.dict(os.environ, {"HOME": workdir}),
          mock.patch.object(taskbridge, "Relay", return_value=fake_relay),
          mock.patch.object(taskbridge, "run_process", side_effect=good_then_crashed_repair),
      ):
        preserved = asyncio.run(serve.solve(session))
    self.assertEqual(preserved, "Complete Turn 0 Answer.")

    # 5) POSIX fallback (no python3 on PATH) handles backslash filenames + unreadable files cleanly
    import shutil
    with tempfile.TemporaryDirectory() as workdir:
      root = pathlib.Path(workdir)
      pbin = root / "pbin"
      pbin.mkdir()
      for tool in ("wc", "tr", "head", "tail", "unzip"):
        src = shutil.which(tool)
        if src:
          (pbin / tool).symlink_to(src)
      bs_file = root / "dir\\sub.txt"
      bs_file.write_text("ok\n")
      unreadable = root / "locked.txt"
      unreadable.write_text("secret\n")
      unreadable.chmod(0)
      try:
        cmd = deliverable_gate.build_verify_command([str(bs_file), str(unreadable)])
        res = subprocess.run(
            ["/bin/sh", "-c", cmd],
            env={"PATH": str(pbin), "HOME": workdir},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        parsed = json.loads(res.stdout)
        entries = {e["path"]: e for e in parsed["checked"]}
        self.assertTrue(entries[str(bs_file)]["ok"])
        if os.geteuid() != 0:
          self.assertFalse(entries[str(unreadable)]["ok"])
      finally:
        unreadable.chmod(0o600)


  def test_noisy_stderr_before_json_in_snapshot_and_verify(self) -> None:
    """FRNT-5: Leading stderr warnings before JSON output do not break snapshot or verify."""

    class NoisySession:

      def __init__(self) -> None:
        self.cancelled = None

      async def exec(self, command: str, timeout_sec: int = 30) -> dict:
        if "mtime" in command or "_IGNORED" in command or "os.walk" in command:
          return {
              "ok": True,
              "exit_code": 0,
              "output": 'Warning: locale not set\n{"out.txt": [5, 100]}',
          }
        return {
            "ok": True,
            "exit_code": 0,
            "output": (
                "Notice: archive comment\n"
                '{"checked": [{"path": "out.txt", "size": 5, "checks":'
                ' ["exists", "non_empty"], "ok": true, "error": null}]}'
            ),
        }

    session = NoisySession()
    snap = asyncio.run(deliverable_gate.snapshot_workdir(session, "/app"))
    self.assertEqual(snap, {"out.txt": (5, 100)})
    report = asyncio.run(
        deliverable_gate.verify_deliverables(session, ["out.txt"])
    )
    self.assertEqual(report["passed"], 1)
    self.assertEqual(report["failed"], 0)


if __name__ == "__main__":
  unittest.main()
