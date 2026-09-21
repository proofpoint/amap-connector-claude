"""tests/test_readonly_inbound.py — the §7 operational check, in-repo.

agent-mailbox-protocol v2.2.0 §7 names the prescribed conformance check for
the read-only-inbound obligation as OPERATIONAL, not a fixture: run the
connector against an inbound tree it cannot write (a read-only mount, not
merely cleared permission bits — a same-uid agent can `chmod` past a mode
change) and confirm it starts, relays every pending notice, resolves bodies,
and submits outbound, without requiring any inbound write to succeed.

The defect this guards against was real: two independent reference
connectors (this package's since-retired `inbox-channel`, and a sibling skill
CLI built against the same seam) carried the same bug unnoticed — `mkdir` +
`os.replace` INSIDE `inbound/`, catching only `FileNotFoundError`, which does
not catch `OSError` from a read-only mount. The watcher that replaced the
channel, `bin/inbox-delivery`, never writes under an inbound tree by design
and is exercised against a read-only spool in `tests/test_inbox_delivery.py`;
the two tools here are the read and submit halves.

`chmod 0o555` on the fixture dirs stands in for a `:ro` MOUNT. It is a
strictly weaker constraint (the owning uid could `chmod` back — which is
exactly why AMP v2.2.0 says read-only inbound integrity requires a mount,
not mode bits, to be unforgeable) but it exercises the IDENTICAL failure
path every connector actually hits: every write, rename, or mkdir attempt
under the frozen directory raises `OSError`.

Self-contained, stdlib-only, unittest-compatible (also pytest-collectible).
Does not import sandy's harness (outside this unit's writable paths, and a
different deployment's scaffolding) — a small local JSON-RPC stdio helper is
enough for the request/notify/initialize shapes these binaries speak.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).absolute()
BIN_DIR = _HERE.parent.parent / "bin"  # <repo>/bin

_BASE_ENV = {"PATH": os.environ.get("PATH", "")}


def _bin(name: str) -> Path:
    p = BIN_DIR / name
    if not p.is_file():
        raise RuntimeError(f"test_readonly_inbound: expected connector binary at {p}")
    return p


class _McpProc:
    """One connector binary, run as a child process and driven as a
    newline-delimited JSON-RPC 2.0 stdio server. Trimmed local equivalent of
    sandy/tests/harness.py's McpProcess — request/notify/wait_notification/
    initialize only, nothing router-specific."""

    def __init__(self, bin_name: str, env: dict, extra_args=None, cwd=None):
        # `cwd` matters when a test exercises RELATIVE path handling: if the
        # code under test fails to resolve a path, the child writes wherever
        # it is standing. Default None keeps every existing caller unchanged;
        # a test that can produce a relative path passes a temp dir so a
        # regression cannot deposit files in the working tree.
        self.bin_name = bin_name
        cmd = [sys.executable, str(_bin(bin_name))] + (extra_args or [])
        self.proc = subprocess.Popen(
            cmd, env=env, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._err_lines: list = []
        self._next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._err_lines.append(line.rstrip("\n"))

    def stderr_text(self) -> str:
        return "\n".join(self._err_lines[-60:])

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict = None, timeout: float = 5) -> dict:
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        misses = []
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    obj = self._q.get(timeout=remaining)
                except queue.Empty:
                    break
                if obj.get("id") == req_id and "method" not in obj:
                    return obj
                misses.append(obj)
        finally:
            for m in misses:
                self._q.put(m)
        raise TimeoutError(
            f"{self.bin_name}: no response to {method!r} (id={req_id}) within "
            f"{timeout}s — stderr tail:\n{self.stderr_text()}"
        )

    def notify(self, method: str, params: dict = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def wait_notification(self, method: str, timeout: float = 8) -> dict:
        deadline = time.time() + timeout
        misses = []
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    obj = self._q.get(timeout=remaining)
                except queue.Empty:
                    break
                if obj.get("method") == method and "id" not in obj:
                    return obj
                misses.append(obj)
        finally:
            for m in misses:
                self._q.put(m)
        raise TimeoutError(
            f"{self.bin_name}: no notification {method!r} within {timeout}s — "
            f"stderr tail:\n{self.stderr_text()}"
        )

    def initialize(self, timeout: float = 5) -> dict:
        resp = self.request("initialize", {"protocolVersion": "2024-11-05"}, timeout=timeout)
        self.notify("notifications/initialized")
        return resp

    def close(self, timeout: float = 5) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


class ReadOnlyInboundFixture(unittest.TestCase):
    """Builds a tmp `notices/` + `messages/` tree with one deliver-notice +
    its spooled body, then freezes both dirs read-only (0o555, restored on
    cleanup) — the fixture every test class below shares."""

    NOTICE_ID = "ro-test-0001"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amp-ro-inbound-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.notices_dir = self.tmp / "notices"
        self.messages_dir = self.tmp / "messages"
        self.notices_dir.mkdir()
        self.messages_dir.mkdir()

        notice = {
            "contract_version": "2",
            "notice_id": self.NOTICE_ID,
            "ts": "2026-08-14T00:00:00Z",
            "kind": "deliver",
            "message": {
                "id": "provider-msg-1",
                "from": "Alice <alice@example.org>",
                "subject": "hello from a read-only inbound",
                "preview": "hi there",
                "mailbox": "inbox",
            },
        }
        (self.notices_dir / f"notice-{self.NOTICE_ID}.json").write_text(json.dumps(notice))

        message = {
            "contract_version": "2",
            "notice_id": self.NOTICE_ID,
            "body_text": "the full spooled body text",
            "from": "Alice <alice@example.org>",
            "subject": "hello from a read-only inbound",
        }
        (self.messages_dir / f"notice-{self.NOTICE_ID}.json").write_text(json.dumps(message))

        # Freeze AFTER writing fixture content — chmod restore is registered
        # per-dir so cleanup (rmtree, registered above and therefore run
        # LAST, addCleanup being LIFO) always finds writable directories.
        self._freeze(self.notices_dir)
        self._freeze(self.messages_dir)

    def _freeze(self, d: Path) -> None:
        original = os.stat(d).st_mode
        os.chmod(d, 0o555)
        self.addCleanup(os.chmod, d, original)

    def _listing(self, d: Path) -> list:
        return sorted(p.name for p in d.iterdir())


class InboxMcpVolReadOnlyInboundTest(ReadOnlyInboundFixture):
    """inbox-mcp-vol never writes anywhere (obligation 2 is vacuous for it),
    but this proves list/read still resolve a body against a frozen spool
    dir end to end."""

    def test_list_and_read_resolve_body_with_every_write_impossible(self):
        env = {**_BASE_ENV, "INBOX_MESSAGE_DIR": str(self.messages_dir)}
        proc = _McpProc("inbox-mcp-vol", env)
        try:
            proc.initialize()

            resp = proc.request("tools/call", {"name": "list_messages", "arguments": {"n": 10}})
            self.assertNotIn("isError", resp["result"])
            self.assertIn(self.NOTICE_ID, resp["result"]["content"][0]["text"])

            resp = proc.request(
                "tools/call", {"name": "read_message", "arguments": {"id": self.NOTICE_ID}})
            self.assertNotIn("isError", resp["result"])
            self.assertIn("the full spooled body text", resp["result"]["content"][0]["text"])
        finally:
            proc.close()


class InboxSubmitReadOnlyInboundTest(ReadOnlyInboundFixture):
    """inbox-submit only ever touches the outbound drop-box, never inbound —
    this closes the §7 quartet (start/relay/resolve/SUBMIT) by proving a
    submit still succeeds while inbound sits frozen alongside it."""

    def test_submit_writes_request_while_inbound_is_readonly(self):
        dropbox = self.tmp / "dropbox"
        dropbox.mkdir()
        env = {**_BASE_ENV, "OUTBOX_DIR": str(dropbox)}

        result = subprocess.run(
            [sys.executable, str(_bin("inbox-submit")), "submit",
             "--to", "operator@example.org", "--subject", "hi", "--body", "hello"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((dropbox / "req-00000000.json").is_file(),
                         result.stdout + result.stderr)
        # inbound is untouched by a submit -- still frozen, still just the one notice
        self.assertEqual(self._listing(self.notices_dir), [f"notice-{self.NOTICE_ID}.json"])


if __name__ == "__main__":
    unittest.main()
