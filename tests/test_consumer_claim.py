"""tests/test_consumer_claim.py — exactly one consumer per notice dir.

Two consumers watching one notice directory race, and the loser's notices are
SILENTLY LOST. Losing mail is strictly worse than delivering it twice, and
nothing surfaces it. `bin/_inboxlib.py`'s `acquire_consumer_claim()` closes
that, and these tests pin its contract against the module itself — loaded by
path, exactly as `bin/inbox-delivery` loads it.

The governing bias: staleness errs toward LIVE. Wrongly judging a claim stale
starts a second consumer and reintroduces the silent loss; wrongly judging it
live refuses to start, which is loud and trivially fixed by deleting one file.

What a refusal MEANS is the caller's policy, not this module's: the daemon
exits nonzero on any claim failure (`tests/test_inbox_delivery.py`
`test_held_claim_is_nonzero_naming_the_holder`). Here only the primitive's own
promises are checked: who wins, who is refused and told why, when a stale
claim is reclaimed, and that a racing reader never sees a half-written claim.

Self-contained, stdlib-only, unittest-compatible (also pytest-collectible).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "bin" / "_inboxlib.py"
CLAIM = ".amap-consumer.json"


def _load_lib():
    spec = importlib.util.spec_from_file_location("_inboxlib_under_test", LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _proc_start(pid: int):
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            data = f.read()
        return data[data.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _dead_pid() -> int:
    p = 99999
    while os.path.exists(f"/proc/{p}"):
        p += 1
    return p


# One contender in the race below. Loads the module by path like the daemon,
# tries the claim once, reports the outcome on stdout, and — if it won — holds
# the claim long enough for every loser to have been refused, then releases.
_CONTENDER = r"""
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("lib", sys.argv[1])
lib = importlib.util.module_from_spec(spec); spec.loader.exec_module(lib)
try:
    h = lib.acquire_consumer_claim(sys.argv[2], sys.argv[3], "contender")
except lib.ClaimHeld as e:
    print("HELD " + str(e)); sys.exit(2)
print("WIN"); sys.stdout.flush()
time.sleep(2.5)
h.release()
"""


class ConsumerClaimTest(unittest.TestCase):
    def setUp(self):
        self.lib = _load_lib()
        self.root = Path(tempfile.mkdtemp())
        self.notices = self.root / "notices"
        self.notices.mkdir()
        self.claim = self.root / CLAIM
        self.logged = []

    def _acquire(self):
        return self.lib.acquire_consumer_claim(
            str(self.claim), str(self.notices), "test-consumer", log=self.logged.append)

    def _write_claim(self, **over):
        payload = {"consumer": "injector-daemon", "pid": 1,
                   "proc_start": _proc_start(1),
                   "notice_dir": str(self.notices),
                   "started_at": "x"}
        payload.update(over)
        self.claim.write_text(json.dumps(payload))

    # --- the happy path ----------------------------------------------------

    def test_clean_start_claims_then_releases(self):
        handle = self._acquire()
        self.assertTrue(self.claim.is_file(), "the claim is published")
        self.assertEqual(json.loads(self.claim.read_text())["consumer"], "test-consumer")
        handle.release()
        self.assertFalse(self.claim.exists(), "claim must be released on clean exit")

    # --- refusals (fail closed) -------------------------------------------

    def test_live_holder_refuses(self):
        self._write_claim()  # pid 1 always exists
        with self.assertRaises(self.lib.ClaimHeld) as cm:
            self._acquire()
        self.assertIn("already consumes", str(cm.exception))
        self.assertEqual(cm.exception.holder, "injector-daemon",
                         "the refusal must name who holds it")

    def test_unparseable_claim_refuses(self):
        self.claim.write_text("not json{")
        with self.assertRaises(self.lib.ClaimHeld) as cm:
            self._acquire()
        self.assertIn(CLAIM, str(cm.exception), "the refusal must name the file to delete")

    def test_a_refusal_is_raised_never_printed(self):
        """The module RAISES; what to say, and where, is the caller's. The
        daemon's stdout carries nothing at all; a stdio server's must stay
        protocol-pure — neither could tolerate the primitive printing."""
        self.claim.write_text("not json{")
        import io, contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(self.lib.ClaimHeld):
                self._acquire()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    # --- reclaiming (only when definitively gone) --------------------------

    def test_dead_holder_is_reclaimed(self):
        self._write_claim(pid=_dead_pid(), proc_start="1")
        handle = self._acquire()
        self.assertTrue(any("reclaiming stale consumer claim" in m for m in self.logged))
        handle.release()

    def test_recycled_pid_is_reclaimed(self):
        """A live pid with a different start time is an unrelated process that
        recycled it — ordinary in containers, where pids restart from 1."""
        # Our own pid: live everywhere, and ours to signal. pid 1 is root's on
        # a macOS host, where kill(1, 0) is EPERM and the library (correctly)
        # reports it live before it ever compares start times.
        me = os.getpid()
        self._write_claim(pid=me, proc_start="999999999")
        if _proc_start(me) is None:
            # No /proc here (macOS host). The library then cannot see a start
            # time and errs toward LIVE by design, so stand in for the read:
            # our current start time is "1", not the "999999999" recorded.
            self.lib._proc_start = lambda pid: "1" if pid == me else None
        handle = self._acquire()
        self.assertTrue(any("reclaiming stale consumer claim" in m for m in self.logged))
        handle.release()

    # --- the unwritable case is the CALLER's decision ---------------------

    def test_unwritable_location_is_a_distinct_error(self):
        """Raised as `ClaimUnwritable`, never `ClaimHeld`, so a caller can
        tell "somebody else holds it" from "nothing can be written here"
        and apply its own policy (the daemon treats both as fatal)."""
        os.chmod(self.root, 0o555)
        try:
            with self.assertRaises(self.lib.ClaimUnwritable) as cm:
                self._acquire()
        finally:
            os.chmod(self.root, 0o755)
        self.assertIsInstance(cm.exception.cause, OSError)
        self.assertNotIsInstance(cm.exception, self.lib.ClaimHeld)

    # --- the race ----------------------------------------------------------

    def test_concurrent_starts_yield_exactly_one_winner(self):
        """Regression: an earlier O_EXCL-then-write created the entry before its
        content, so a racing process read it EMPTY and reported 'unreadable'
        rather than 'another consumer holds it'. Publication is now
        write-then-link, so the claim is never visible partially written."""
        procs = [subprocess.Popen(
            [sys.executable, "-c", _CONTENDER, str(LIB), str(self.claim), str(self.notices)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(8)]
        time.sleep(1.5)
        try:
            alive = [p for p in procs if p.poll() is None]
            dead = [p for p in procs if p.poll() is not None]
            outs = [p.stdout.read() for p in dead]

            self.assertEqual(len(alive), 1, "exactly one process may hold the claim")
            self.assertEqual(len(dead), 7)
            self.assertTrue(all(p.returncode == 2 for p in dead))
            self.assertTrue(all(o.startswith("HELD") and "already consumes" in o for o in outs),
                            f"losers must refuse with the holder's identity: {outs}")
            self.assertFalse(any("could not be parsed" in o for o in outs),
                             "no loser may see a partially-written claim")
            self.assertEqual([f for f in os.listdir(self.root) if ".tmp." in f], [],
                             "no temporary claim files may be left behind")
        finally:
            for p in procs:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.assertFalse(self.claim.exists(), "claim released once every consumer exited")


if __name__ == "__main__":
    unittest.main()
