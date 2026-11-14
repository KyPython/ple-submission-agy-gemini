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

"""Offline $0 tests for FRNT-1, FRNT-2, and FRNT-3 security & MCP boundaries."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

os.environ.setdefault("TASK_RELAY_URL", "http://127.0.0.1:9999/deadbeef")
os.environ.setdefault("TASK_WORKDIR", "/app")
os.environ.setdefault("MODEL_NAME", "gemini-4-argon")

import serve  # noqa: E402
import task_mcp  # noqa: E402
import taskbridge  # noqa: E402


class FakeSession:
  """Minimal in-memory mock of taskbridge.Session for offline $0 tests."""

  def __init__(
      self,
      instruction: str = "Solve the task.",
      workdir: str = "/app",
      mcp_servers: list[dict] | None = None,
      time_left_sec: float | None = 3600.0,
  ) -> None:
    self.instruction = instruction
    self.workdir = workdir
    self.mcp_servers = mcp_servers or []
    self._time_left = time_left_sec
    self.final_message = ""
    self.cancelled = asyncio.Event()
    self.logs: list[str] = []
    self.usage_calls: list[tuple] = []
    self.files: dict[str, bytes] = {}
    self.exec_calls: list[tuple[str, int]] = []
    self.http_calls: list[tuple[str, str, bytes]] = []
    self.mcp_tool_catalog: dict[str, list[str]] = {}
    self.exec_handler = None

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
    self.exec_calls.append((command, timeout_sec))
    if self.exec_handler is not None:
      return await self.exec_handler(command, timeout_sec)
    return {"ok": True, "exit_code": 0, "output": "", "truncated": False, "timed_out": False}

  async def read_result(self, path: str) -> dict:
    if path not in self.files:
      return {"ok": False, "error": f"missing: {path}"}
    raw = self.files[path]
    if len(raw) > 10_000_000:
      return {"ok": True, "truncated": True, "size": len(raw)}
    return {
        "ok": True,
        "truncated": False,
        "size": len(raw),
        "content_b64": base64.b64encode(raw).decode(),
    }

  async def write_file(self, path: str, data: bytes | str, append: bool = False) -> int:
    raw = data.encode() if isinstance(data, str) else bytes(data)
    prev = self.files.get(path, b"") if append else b""
    self.files[path] = prev + raw
    return len(self.files[path])

  async def http(
      self,
      url: str,
      method: str = "POST",
      headers: dict | None = None,
      body: bytes = b"",
      timeout_sec: int = 120,
  ) -> dict:
    self.http_calls.append((method, url, body))
    if not taskbridge.mcp_allows(url, self.mcp_servers):
      raise taskbridge.TaskError(f"http: {url} disallowed")
    msg = json.loads(body.decode("utf-8"))
    rpc_method = msg.get("method")
    rid = msg.get("id")
    if rpc_method == "initialize":
      res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
    elif rpc_method == "tools/list":
      matched = next(
          (s["name"] for s in self.mcp_servers if url.startswith(s["url"].rstrip("/"))),
          "",
      )
      names = self.mcp_tool_catalog.get(matched, ["ping_tool"])
      res = {
          "tools": [
              {"name": n, "inputSchema": {"type": "object", "properties": {}}}
              for n in names
          ]
      }
    elif rpc_method == "tools/call":
      tool_name = (msg.get("params") or {}).get("name")
      res = {"content": [{"type": "text", "text": f"called:{tool_name}"}], "isError": False}
    else:
      res = {}
    if rid is None:
      return {"status": 202, "headers": {}, "body": b"", "truncated": False}
    payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}).encode()
    return {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": payload,
        "truncated": False,
    }


async def _relay_post(relay: taskbridge.Relay, subpath: str, data: bytes, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
  status, _, payload = await relay._route(
      "POST",
      f"/{relay.token}/{subpath.lstrip('/')}",
      "",
      {"Content-Type": "application/json", **(headers or {})},
      data,
  )
  return status, payload


class PermissionBoundaryTests(unittest.TestCase):
  """Tests FRNT-1, FRNT-2, and FRNT-3 requirements offline."""

  def test_agent_disables_built_in_tools(self) -> None:
    """FRNT-1.1: task-agent.md generated by configure() enforces tools: []."""
    async def run() -> str:
      session = FakeSession()
      with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        async with taskbridge.Relay(session) as relay:
          await serve.configure(session, relay, home)
        return (home / ".gemini" / "config" / "agents" / "task-agent.md").read_text()

    agent_md = asyncio.run(run())
    self.assertIn("\ntools: []\n", agent_md)

  def test_settings_disallow_non_workspace_and_policy_flags(self) -> None:
    """FRNT-1.2: Non-workspace serve access disabled and blanket bypass removed."""
    self.assertIs(serve.SETTINGS.get("allowNonWorkspaceAccess"), False)
    self.assertEqual(serve.SETTINGS.get("toolPermission"), "strict")
    self.assertNotEqual(serve.SETTINGS.get("artifactReviewPolicy"), "always-proceed")
    argv = serve.build_agy_argv(["-p", "hi"], conversation=None)
    self.assertNotIn("--dangerously-skip-permissions", argv)
    self.assertIn("--sandbox", argv)

  def test_unsupported_policy_fails_closed(self) -> None:
    """FRNT-1.3: Permissive/broken policy or serve-local execution fails closed cleanly."""
    with self.assertRaises(RuntimeError):
      serve.validate_serve_policy(
          {**serve.SETTINGS, "allowNonWorkspaceAccess": True},
          serve.AGENT,
          [serve.AGY, "--sandbox", "--agent", "task-agent"],
      )
    with self.assertRaises(RuntimeError):
      serve.validate_serve_policy(
          {**serve.SETTINGS, "toolPermission": "always-proceed"},
          serve.AGENT,
          [serve.AGY, "--sandbox", "--agent", "task-agent"],
      )
    with self.assertRaises(RuntimeError):
      serve.validate_serve_policy(
          serve.SETTINGS,
          "---\nname: task-agent\n---\nPrompt",
          [serve.AGY, "--sandbox", "--agent", "task-agent"],
      )
    with self.assertRaises(RuntimeError):
      serve.validate_serve_policy(
          serve.SETTINGS,
          serve.AGENT,
          [serve.AGY, "--dangerously-skip-permissions", "--agent", "task-agent"],
      )

    # Runtime fail-closed on unsupported policy error or serve-local tool execution
    async def run_fail_closed() -> None:
      session = FakeSession()
      with tempfile.TemporaryDirectory() as tmp:
        fake_agy = pathlib.Path(tmp) / "fake_agy.sh"
        fake_agy.write_text(
            "#!/bin/sh\n"
            'echo \'{"event":"init","conversation_id":"c1"}\'\n'
            'echo \'{"event":"step","step_update":{"step_index":0,"step_type":"tool","tool_name":"run_command","state":"DONE"}}\'\n'
            'echo "unknown flag: --sandbox" >&2\n'
            "exit 1\n"
        )
        fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)
        with (
            mock.patch.object(serve, "AGY", str(fake_agy)),
            mock.patch.dict(os.environ, {"HOME": tmp, "MODEL_NAME": "gemini-4-argon"}),
        ):
          with self.assertRaises(RuntimeError):
            await serve.solve(session)
      self.assertTrue(
          any("policy" in log.lower() or "local" in log.lower() or "sandbox" in log.lower() for log in session.logs),
          f"expected structured policy error in session.logs, got {session.logs}",
      )

      # Even rc=0 headless auto-denial / mode warning must fail closed
      rc0_session = FakeSession()
      with tempfile.TemporaryDirectory() as tmp2:
        fake_rc0 = pathlib.Path(tmp2) / "fake_rc0.sh"
        fake_rc0.write_text(
            "#!/bin/sh\n"
            'echo "the tool required approval that headless mode cannot prompt for, so they were auto-denied." >&2\n'
            'echo \'{"event":"init","conversation_id":"c-rc0"}\'\n'
            'echo \'{"event":"result","result":{"status":"OK","response":"done"}}\'\n'
            "exit 0\n"
        )
        fake_rc0.chmod(fake_rc0.stat().st_mode | stat.S_IEXEC)
        with (
            mock.patch.object(serve, "AGY", str(fake_rc0)),
            mock.patch.dict(os.environ, {"HOME": tmp2, "MODEL_NAME": "gemini-4-argon"}),
        ):
          with self.assertRaises(RuntimeError):
            await serve.solve(rc0_session)

    asyncio.run(run_fail_closed())

  def test_headless_engineering_shell_commands_allowed(self) -> None:
    """FRNT-1.4: Arbitrary task-container engineering commands run non-interactively."""
    captured: list[tuple[str, dict]] = []

    def fake_relay(route: str, **fields):
      captured.append((route, fields))
      return {"ok": True, "exit_code": 0, "output": "Archive OK\n", "truncated": False, "timed_out": False}

    with mock.patch.object(task_mcp, "relay", side_effect=fake_relay):
      for cmd in (
          "python3 -c 'print(42)'",
          "unzip -t /app/out/model.zip",
          "freecadcmd --console /app/check.py",
      ):
        res = task_mcp.shell(cmd, timeout_sec=180)
        self.assertIn("exit_code=0", res[0]["text"])
    self.assertEqual([c[1]["command"] for c in captured], [
        "python3 -c 'print(42)'",
        "unzip -t /app/out/model.zip",
        "freecadcmd --console /app/check.py",
    ])

  def test_authorized_mcp_end_to_end_and_escape_rejection(self) -> None:
    """FRNT-1.5: End-to-end mocked serve.solve() configures hardened policy & runs task_mcp ops."""
    async def run_e2e() -> str:
      session = FakeSession(instruction="Write /app/out/ok.txt via task_mcp")
      with tempfile.TemporaryDirectory() as tmp:
        args_seen = pathlib.Path(tmp) / "argv.json"
        cfg_seen = pathlib.Path(tmp) / "mcp_cfg.json"
        fake_agy = pathlib.Path(tmp) / "fake_agy.py"
        fake_agy.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            f"pathlib.Path({str(args_seen)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "home = pathlib.Path(os.environ['HOME'])\n"
            "cfg_text = (home / '.gemini' / 'config' / 'mcp_config.json').read_text()\n"
            f"pathlib.Path({str(cfg_seen)!r}).write_text(cfg_text)\n"
            "print(json.dumps({'event': 'init', 'conversation_id': 'conv-e2e'}))\n"
            "print(json.dumps({'event': 'step', 'step_update': {'step_index': 0, 'step_type': 'tool', 'tool_name': 'mcp_task_write_file', 'state': 'DONE'}}))\n"
            "print(json.dumps({'event': 'step', 'step_update': {'step_index': 1, 'step_type': 'agent_response', 'text_delta': 'ALL_GOOD', 'state': 'DONE'}}))\n"
            "print(json.dumps({'event': 'result', 'result': {'status': 'OK', 'response': 'ALL_GOOD', 'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_tokens': 0}}}))\n"
        )
        fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)
        with (
            mock.patch.object(serve, "AGY", str(fake_agy)),
            mock.patch.dict(
                os.environ,
                {
                    "HOME": tmp,
                    "MODEL_NAME": "gemini-4-argon",
                    "GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:18080",
                    "GEMINI_API_KEY": "test-dummy-token",
                },
            ),
        ):
          answer = await serve.solve(session)
        recorded_argv = json.loads(args_seen.read_text())
        self.assertNotIn("--dangerously-skip-permissions", recorded_argv)
        self.assertIn("--sandbox", recorded_argv)
        self.assertEqual(answer, "ALL_GOOD")

      # Also exercise task_mcp <-> Relay._route <-> FakeSession dispatch end-to-end
      async with taskbridge.Relay(session) as relay:
        loop = asyncio.get_running_loop()

        def relay_bridge(route: str, **fields):
          fut = asyncio.run_coroutine_threadsafe(
              _relay_post(relay, route, json.dumps(fields).encode()),
              loop,
          )
          status, payload = fut.result(timeout=5)
          self.assertEqual(status, 200)
          return json.loads(payload.decode())

        def run_mcp() -> list[dict]:
          out: list[dict] = []
          with (
              mock.patch.object(task_mcp, "relay", side_effect=relay_bridge),
              mock.patch.object(task_mcp, "send", side_effect=out.append),
          ):
            task_mcp.call(1, {"name": "write_file", "arguments": {"path": "out/ok.txt", "content": "ready\n"}})
            task_mcp.call(2, {"name": "shell", "arguments": {"command": "ls -la /app/out"}})
            task_mcp.call(3, {"name": "read_file", "arguments": {"path": "/app/out/ok.txt"}})
            task_mcp.call(4, {"name": "read_file", "arguments": {"path": "/app/out/ok.txt\x00bad"}})
          return out

        responses = await asyncio.to_thread(run_mcp)
        self.assertFalse(responses[0]["result"]["isError"])
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertEqual(session.files.get("/app/out/ok.txt"), b"ready\n")
        self.assertTrue(session.exec_calls)
        return answer

    asyncio.run(run_e2e())

  def test_task_mcp_argument_schema_validation(self) -> None:
    """FRNT-2.1: Invalid tool names/args return MCP isError=True without relay calls."""
    relay_calls: list[str] = []
    sent: list[dict] = []

    def boom_relay(route: str, **fields):
      relay_calls.append(route)
      raise AssertionError(f"relay should not be called on invalid input: {route}")

    bad_requests = [
        ("unknown_tool", {}),
        ("shell", None),
        ("shell", "not-a-dict"),
        ("shell", {}),
        ("shell", {"command": ""}),
        ("shell", {"command": "ls", "extra_key": 1}),
        ("shell", {"command": "ls\x00bad"}),
        ("shell", {"command": "ls", "timeout_sec": True}),
        ("shell", {"command": "ls", "timeout_sec": 0}),
        ("shell", {"command": "ls", "timeout_sec": "10"}),
        ("read_file", {}),
        ("read_file", {"path": ""}),
        ("read_file", {"path": "/app/a", "offset": True}),
        ("read_file", {"path": "/app/a", "offset": 0}),
        ("read_file", {"path": "/app/a", "limit": -5}),
        ("write_file", {"path": "/app/a\ud800", "content": "x"}),
        ("write_file", {"path": "/app/a", "content": 123}),
        ("edit_file", {"path": "/app/a", "old_text": "a", "new_text": "b", "replace_all": "yes"}),
    ]

    with (
        mock.patch.object(task_mcp, "relay", side_effect=boom_relay),
        mock.patch.object(task_mcp, "send", side_effect=sent.append),
    ):
      for idx, (tool_name, args) in enumerate(bad_requests, 1):
        task_mcp.call(idx, {"name": tool_name, "arguments": args})

    self.assertEqual(relay_calls, [])
    self.assertEqual(len(sent), len(bad_requests))
    for msg in sent:
      self.assertTrue(msg["result"]["isError"], f"expected isError=True: {msg}")

  def test_relay_bounds_timeouts_and_payload_limits(self) -> None:
    """FRNT-2.2: Buffer limits and timeout bounds reject oversized payloads locally."""
    sent: list[dict] = []
    relay_calls: list[str] = []

    def tracking_relay(route: str, **fields):
      relay_calls.append(route)
      return {"ok": True}

    with (
        mock.patch.object(task_mcp, "relay", side_effect=tracking_relay),
        mock.patch.object(task_mcp, "send", side_effect=sent.append),
    ):
      task_mcp.call(
          1,
          {
              "name": "write_file",
              "arguments": {"path": "/app/big.txt", "content": "x" * (taskbridge.MAX_WRITE_BYTES + 1)},
          },
      )
      task_mcp.call(
          2,
          {
              "name": "shell",
              "arguments": {"command": "echo " + ("a" * taskbridge.MAX_FRAME)},
          },
      )
    self.assertEqual(relay_calls, [])
    self.assertEqual(len(sent), 2)
    self.assertTrue(all(m["result"]["isError"] for m in sent))

    # Relay HTTP body over MAX_HTTP_BYTES rejected before session.http
    async def run_relay_limits() -> None:
      session = FakeSession(mcp_servers=[{"name": "apps", "url": "http://127.0.0.1:8000/mcp/"}])
      async with taskbridge.Relay(session) as relay:
        status, _ = await _relay_post(
            relay,
            "mcp/apps/",
            b"{" + b" " * taskbridge.MAX_HTTP_BYTES + b"}",
        )
        self.assertEqual(status, 413)
        self.assertEqual(session.http_calls, [])

    asyncio.run(run_relay_limits())
    self.assertEqual(taskbridge.positive_int(999999, 120, taskbridge.MAX_TIMEOUT_SEC), taskbridge.MAX_TIMEOUT_SEC)
    self.assertEqual(taskbridge._seconds(999999), taskbridge.MAX_TIMEOUT_SEC)
    self.assertEqual(taskbridge._seconds(0), 1)

  def test_mcp_allows_scheme_host_and_traversal_checks(self) -> None:
    """FRNT-2.3: mcp_allows strictly validates http/https scheme, host, and path safety."""
    servers = [{"name": "apps", "url": "http://127.0.0.1:8000/mcp/"}]
    self.assertTrue(taskbridge.mcp_allows("http://127.0.0.1:8000/mcp/", servers))
    self.assertTrue(taskbridge.mcp_allows("http://127.0.0.1:8000/mcp/tools?v=1", servers))
    for bad in (
        "file:///etc/passwd",
        "ftp://127.0.0.1:8000/mcp/",
        "javascript:alert(1)",
        "gopher://127.0.0.1:8000/mcp/",
        "http:///mcp/",
        "http://127.0.0.1:8001/mcp/",
        "http://evil.example:8000/mcp/",
        "http://127.0.0.1:8000@evil.example/mcp/",
        "https://127.0.0.1:8000/mcp/",
        "http://127.0.0.1:8000/mcp/../secret",
        "http://127.0.0.1:8000/mcp/./secret",
        "http://127.0.0.1:8000/mcp/%2f..%2fsecret",
        "http://127.0.0.1:8000/mcp/%5csecret",
        "http://127.0.0.1:8000/mcp\\secret",
        "http://127.0.0.1:8000/mcp/;jsessionid=1",
    ):
      self.assertFalse(taskbridge.mcp_allows(bad, servers), f"expected False for {bad!r}")

    for bad_self in (
        "file:///etc/passwd",
        "ftp://127.0.0.1:8000/mcp/",
        "javascript:alert(1)",
        "gopher://127.0.0.1:8000/mcp/",
        "http:///mcp/",
    ):
      self.assertFalse(
          taskbridge.mcp_allows(bad_self, [{"name": "x", "url": bad_self}]),
          f"expected False for self-declared {bad_self!r}",
      )

    self.assertFalse(
        taskbridge.mcp_allows(
            "http://127.0.0.1:8000@evil.example/mcp/",
            [{"name": "root", "url": "http://127.0.0.1:8000"}],
        )
    )
    self.assertFalse(
        taskbridge.mcp_allows(
            "http://127.0.0.1:8000.evil.example/mcp/",
            [{"name": "root", "url": "http://127.0.0.1:8000"}],
        )
    )
    query_server = [{"name": "q", "url": "http://127.0.0.1:8000/mcp?token=abc"}]
    self.assertTrue(taskbridge.mcp_allows("http://127.0.0.1:8000/mcp?token=abc", query_server))
    self.assertFalse(taskbridge.mcp_allows("http://127.0.0.1:8000/mcp?token=abc&x=1", query_server))
    self.assertFalse(
        taskbridge.mcp_allows(
            "http://127.0.0.1:8000/mcp?token=abc#frag", query_server
        )
    )
    frag_server = [
        {"name": "f", "url": "https://api.example.com/mcp?session=1#frag"}
    ]
    self.assertTrue(
        taskbridge.mcp_allows(
            "https://api.example.com/mcp?session=1#frag", frag_server
        )
    )
    self.assertFalse(
        taskbridge.mcp_allows(
            "https://api.example.com:443/mcp?session=1#frag", frag_server
        )
    )

  def test_secret_isolation_and_error_redaction(self) -> None:
    """FRNT-2.4: mcp_config.json strips keys and redact_secrets scrubs tokens/keys."""
    secret_key = "TEST_" + "GEMINI_" + "SECRET_9911"
    relay_tok = "abcdef0123456789abcdef0123456789"
    relay_url = f"http://127.0.0.1:54321/{relay_tok}"
    with mock.patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": secret_key,
            "MODEL_API_KEY": "MODEL_" + "SECRET_8822",
            "GOOGLE_GEMINI_BASE_URL": "https://private.example/v1beta",
            "TASK_RELAY_URL": relay_url,
        },
    ):
      scrubbed = taskbridge.redact_secrets(
          f"failed calling {relay_url}/exec with key {secret_key} and MODEL_SECRET_8822"
      )
    self.assertNotIn(secret_key, scrubbed)
    self.assertNotIn(relay_tok, scrubbed)
    self.assertNotIn("MODEL_SECRET_8822", scrubbed)
    self.assertIn("[REDACTED]", scrubbed)

    async def check_mcp_env() -> dict:
      session = FakeSession()
      with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        with mock.patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": secret_key,
                "MODEL_API_KEY": "MODEL_" + "SECRET_8822",
                "MODEL_NAME": "gemini-4-argon",
            },
        ):
          async with taskbridge.Relay(session) as relay:
            await serve.configure(session, relay, home)
        return json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

    cfg = asyncio.run(check_mcp_env())
    bridge_env = cfg["mcpServers"]["task"]["env"]
    self.assertNotIn("GEMINI_API_KEY", bridge_env)
    self.assertNotIn("MODEL_API_KEY", bridge_env)
    self.assertNotIn("GOOGLE_GEMINI_BASE_URL", bridge_env)

    # _scrub_process_env pops sensitive keys from child env while keeping redaction active
    with mock.patch.dict(os.environ, {"GEMINI_API_KEY": secret_key}, clear=False):
      task_mcp._scrub_process_env()
      self.assertNotIn("GEMINI_API_KEY", os.environ)
      err_out: list[dict] = []
      with (
          mock.patch.object(task_mcp, "RELAY", relay_url),
          mock.patch.object(task_mcp, "send", side_effect=err_out.append),
          mock.patch.object(task_mcp, "relay", side_effect=RuntimeError(f"boom GET /{relay_tok}/exec key={secret_key}")),
      ):
        task_mcp.call(1, {"name": "read_file", "arguments": {"path": "/app/x.txt"}})
      scrub_txt = err_out[0]["result"]["content"][0]["text"]
      self.assertNotIn(secret_key, scrub_txt)
      self.assertNotIn(relay_tok, scrub_txt)
      # Allow whitespace-only old_text in edit_file
      cleaned = task_mcp.validate_tool_args("edit_file", {"path": "/app/x.txt", "old_text": "    \n", "new_text": ""})
      self.assertEqual(cleaned["old_text"], "    \n")
      self.assertNotIn("GEMINI_API_KEY", os.environ)
      self.assertNotIn(secret_key, taskbridge.redact_secrets(f"err {secret_key}", extra_secrets=task_mcp._extra_secrets()))

  def test_structured_audit_log_emitted_without_secrets(self) -> None:
    """FRNT-2.5: task_mcp and Relay._mcp emit structured audit records omitting secrets."""
    secret_key = "AUDIT_" + "SECRET_7733"
    sent: list[dict] = []
    stderr_buf = io.StringIO()
    with (
        mock.patch.dict(os.environ, {"GEMINI_API_KEY": secret_key}),
        mock.patch.object(
            task_mcp,
            "relay",
            return_value={"ok": True, "exit_code": 0, "output": f"body={secret_key}", "truncated": False},
        ),
        mock.patch.object(task_mcp, "send", side_effect=sent.append),
        mock.patch.object(sys, "stderr", stderr_buf),
    ):
      task_mcp.call(1, {"name": "shell", "arguments": {"command": f"echo {secret_key}"}})

    records = [json.loads(line) for line in stderr_buf.getvalue().splitlines() if line.strip()]
    self.assertTrue(any(r.get("audit") == "mcp_dispatch" and r.get("component") == "task_mcp" for r in records))
    self.assertNotIn(secret_key, stderr_buf.getvalue())

    async def check_relay_audit() -> list[str]:
      session = FakeSession(mcp_servers=[{"name": "apps", "url": "http://127.0.0.1:8000/mcp/"}])
      async with taskbridge.Relay(session) as relay:
        req_body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        status, _ = await _relay_post(relay, "mcp/apps/", req_body)
        self.assertEqual(status, 200)
        await asyncio.sleep(0)
      return session.logs

    logs = asyncio.run(check_relay_audit())
    self.assertTrue(any("mcp_dispatch" in entry and "relay_mcp" in entry for entry in logs), logs)

  def test_configure_validates_mcp_server_descriptors(self) -> None:
    """FRNT-3.1: Invalid session.mcp_servers entries skipped with logged warning."""
    session = FakeSession(
        mcp_servers=[
            {"name": "", "url": "http://127.0.0.1:8000/mcp/"},
            {"name": "../bad", "url": "http://127.0.0.1:8000/mcp/"},
            {"name": "bad_scheme", "url": "file:///etc/passwd"},
            {"name": "apps", "url": "http://127.0.0.1:8000/mcp/"},
            {"name": "apps", "url": "http://127.0.0.1:8000/mcp/"},
        ]
    )

    async def run() -> dict:
      with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        async with taskbridge.Relay(session) as relay:
          await serve.configure(session, relay, home)
        return json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

    cfg = asyncio.run(run())
    self.assertIn("task", cfg["mcpServers"])
    self.assertIn("apps", cfg["mcpServers"])
    self.assertNotIn("../bad", cfg["mcpServers"])
    self.assertNotIn("bad_scheme", cfg["mcpServers"])
    self.assertGreaterEqual(len(session.logs), 3)

  def test_configure_filters_mcp_tools_by_policy(self) -> None:
    """FRNT-3.2: Tool allowlist/denylist filters external MCP tool names before mcp_config.json."""
    session = FakeSession(mcp_servers=[{"name": "cad", "url": "http://127.0.0.1:8000/mcp/"}])
    session.mcp_tool_catalog["cad"] = ["inspect_part", "delete_database", "measure_area"]

    async def run() -> dict:
      with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        with mock.patch.dict(
            os.environ,
            {
                "AGY_MCP_TOOL_ALLOWLIST": "inspect_*,measure_*,delete_*",
                "AGY_MCP_TOOL_DENYLIST": "delete_*",
            },
        ):
          async with taskbridge.Relay(session) as relay:
            await serve.configure(session, relay, home)
        return json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

    cfg = asyncio.run(run())
    tools = cfg["mcpServers"]["cad"]["tools"]
    self.assertEqual(set(tools), {"inspect_part", "measure_area"})
    self.assertNotIn("delete_database", tools)

  def test_configure_bounds_eager_external_mcp_tools(self) -> None:
    """FRNT-3.3: Core TASK_TOOLS remain eager; 264 external tools do not inject 264 eager schemas."""
    session = FakeSession(mcp_servers=[{"name": "apps", "url": "http://127.0.0.1:8000/mcp/"}])
    session.mcp_tool_catalog["apps"] = [f"tool_{i}" for i in range(264)]

    async def run() -> dict:
      with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        async with taskbridge.Relay(session) as relay:
          await serve.configure(session, relay, home)
        return json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

    cfg = asyncio.run(run())
    task_tools = cfg["mcpServers"]["task"]["tools"]
    self.assertEqual(list(task_tools), list(serve.TASK_TOOLS))
    self.assertTrue(all(v == {"eager": True} for v in task_tools.values()))

    ext_tools = cfg["mcpServers"]["apps"]["tools"]
    eager_count = sum(1 for v in ext_tools.values() if v.get("eager") is True)
    self.assertLessEqual(eager_count, serve.MAX_EAGER_EXTERNAL_MCP_TOOLS)
    self.assertGreater(len(ext_tools) - eager_count, 200)

  def test_disallowed_mcp_tool_rejected_and_counts_logged(self) -> None:
    """FRNT-3.4: Relay._mcp rejects filtered tools/call and configure() logs counts."""
    session = FakeSession(mcp_servers=[{"name": "cad", "url": "http://127.0.0.1:8000/mcp/"}])
    session.mcp_tool_catalog["cad"] = ["inspect_part", "delete_database"]

    async def run() -> None:
      with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        with mock.patch.dict(os.environ, {"AGY_MCP_TOOL_DENYLIST": "delete_*"}):
          async with taskbridge.Relay(session) as relay:
            await serve.configure(session, relay, home)
            denied_req = json.dumps({
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "delete_database", "arguments": {}},
            }).encode()
            status, body = await _relay_post(relay, "mcp/cad/", denied_req)
            self.assertEqual(status, 403)
            self.assertIn(b"denied", body.lower())
            self.assertEqual(
                [c for c in session.http_calls if b"delete_database" in c[2]],
                [],
            )
            unk_status, _ = await _relay_post(relay, "mcp/nonexistent/", denied_req)
            self.assertEqual(unk_status, 404)

    asyncio.run(run())
    self.assertTrue(
        any("registered" in log and "filtered" in log and "deferred" in log for log in session.logs),
        session.logs,
    )

    # SSE tools/list filtering AND skipped/failed MCP server fail-closed checks
    sse_body = (
        "event: message\r\n"
        + "data: "
        + json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "inspect_part", "description": "ok"},
                    {"name": "delete_database", "description": "blocked"},
                ]
            },
        })
        + "\r\n\r\n"
    ).encode("utf-8")
    filtered_sse = taskbridge._filter_tools_list_body(sse_body, {"inspect_part"}).decode("utf-8")
    self.assertIn("inspect_part", filtered_sse)
    self.assertNotIn("delete_database", filtered_sse)

    # Skipped server (e.g. failed tools/list or disallowed URL) must fail closed in Relay._mcp
    async def run_skipped_server() -> None:
      s2 = FakeSession(mcp_servers=[
          {"name": "broken_srv", "url": "http://127.0.0.1:8000/broken"},
          {"name": "bad_url", "url": "file:///etc/passwd"},
      ])
      with (
          tempfile.TemporaryDirectory() as tmp2,
          mock.patch.object(serve, "mcp_tools", side_effect=RuntimeError("offline")),
      ):
        async with taskbridge.Relay(s2) as r2:
          await serve.configure(s2, r2, pathlib.Path(tmp2))
          call_req = json.dumps({
              "jsonrpc": "2.0",
              "id": 1,
              "method": "tools/call",
              "params": {"name": "any_tool", "arguments": {}},
          }).encode()
          st_broken, _ = await _relay_post(r2, "mcp/broken_srv/", call_req)
          st_bad, _ = await _relay_post(r2, "mcp/bad_url/", call_req)
          self.assertEqual(st_broken, 403)
          self.assertEqual(st_bad, 403)

    asyncio.run(run_skipped_server())


if __name__ == "__main__":
  unittest.main()
