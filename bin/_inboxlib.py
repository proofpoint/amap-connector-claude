"""_inboxlib.py — the one private module the bin/ scripts share.

Loaded BY PATH from `bin/inbox-delivery` — the script resolves this file
relative to its own `__file__` (no package, no install step, no `sys.path`
edits from the environment). That keeps the house rule: every tool runs
straight off PATH from a checkout, and `pip install .` remains optional. See
pyproject.toml.

Today it holds exactly one thing: the CONSUMER CLAIM.

Exactly ONE process may consume a given notice directory. If two consumers
watch one directory they race and the loser's notices are SILENTLY LOST —
strictly worse than delivering twice, and invisible. The claim closes that.
It was born in `inbox-channel`, the one-way `claude/channel` push this
package shipped until 2026-09-15, when a stray channel and the delivery
daemon could both watch one spool; the channel is gone, and the claim stays
because the hazard does — any second consumer, a hand-run tool included.

Design notes:

  - The claim file lives where the CALLER says. The daemon is handed two
    explicit paths on a private mount; this module never derives a path and
    never reads the environment — the caller owns that decision.
  - Acquisition publishes a COMPLETE file atomically (write a temp, then
    `os.link` it into place — see `_try_create`), so two processes starting
    together cannot both win, and neither reads the other's claim half-written.
  - Staleness errs toward LIVE. Wrongly judging a claim stale starts a second
    consumer and reintroduces silent loss; wrongly judging it live refuses to
    start, which is loud and trivially fixable. Only a definitively-gone
    holder releases the claim.
  - This module RAISES; it never logs, never exits, never writes stdout. What
    a refusal means is the caller's policy — the daemon exits nonzero on any
    failure — so `ClaimUnwritable` is raised separately from `ClaimHeld`, and
    a caller may treat the two differently.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

__all__ = [
    "ClaimError", "ClaimHeld", "ClaimUnwritable", "ClaimHandle",
    "acquire_consumer_claim", "release_consumer_claim",
]


class ClaimError(Exception):
    """Base: the claim could not be taken. `str(e)` is a complete, operator-
    facing sentence naming the file and what to do."""


class ClaimHeld(ClaimError):
    """Another consumer holds it (or the file is unreadable / flapping — cases
    that also mean "do not start a second consumer"). Attributes name the
    holder where known, so a caller can put them in its own message."""

    def __init__(self, message: str, *, path: str, holder: Optional[str] = None,
                 holder_pid: Optional[int] = None):
        super().__init__(message)
        self.path = path
        self.holder = holder
        self.holder_pid = holder_pid


class ClaimUnwritable(ClaimError):
    """No claim can be written at that path at all (read-only parent, missing
    directory). Distinct from ClaimHeld because one caller treats it as a
    warning and the other as fatal."""

    def __init__(self, message: str, *, path: str, cause: OSError):
        super().__init__(message)
        self.path = path
        self.cause = cause


class ClaimHandle:
    """What a successful acquisition returns. `release()` is idempotent and
    best-effort; a hard kill leaves the file, which the next start reclaims
    via the staleness check."""

    def __init__(self, path: str):
        self.path: Optional[str] = path

    def release(self) -> None:
        path, self.path = self.path, None
        if path is None:
            return
        try:
            os.unlink(path)
        except OSError:
            pass


def _proc_start(pid: int) -> Optional[str]:
    """Linux: field 22 of /proc/<pid>/stat, the process start time in clock
    ticks. Distinguishes a live holder from an unrelated process that recycled
    its pid — which matters a lot in containers, where pids restart from 1 on
    every boot and collisions are ordinary rather than exotic. Returns None
    where unavailable (non-Linux, or no /proc); callers then fall back to a
    pid-only check, which errs toward LIVE."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            data = f.read()
        return data[data.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _holder_is_live(claim: dict) -> bool:
    """True unless the holder is DEFINITIVELY gone (see the erring-toward-live
    note in the module docstring)."""
    pid = claim.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True  # malformed -> assume live, refuse, let a human clear it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # nothing with that pid: definitively gone
    except PermissionError:
        return True   # exists, owned by someone else
    except OSError:
        return True
    recorded = claim.get("proc_start")
    current = _proc_start(pid)
    if isinstance(recorded, str) and current is not None and recorded != current:
        return False  # pid recycled: the original holder is gone
    return True


def _claim_payload(notice_dir: str, consumer_name: str) -> str:
    return json.dumps({
        "consumer": consumer_name,
        "pid": os.getpid(),
        "proc_start": _proc_start(os.getpid()),
        "notice_dir": os.path.abspath(notice_dir),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def _try_create(path: str, notice_dir: str, consumer_name: str) -> bool:
    """Publish a COMPLETE claim file, atomically, or report that one exists.

    Write-then-`os.link` rather than `O_CREAT|O_EXCL`-then-write: the latter
    creates the entry before its content, so a process racing us sees the file
    exist, reads it EMPTY, and reports "claim unreadable" instead of "another
    consumer holds it" (observed). `os.link` publishes an already-complete file
    and fails with FileExistsError if the name is taken, so the claim is never
    visible in a partial state.

    Falls back to the plain exclusive-create path where hard links are
    unavailable (some mounts); the race window returns there, which is why the
    reader retries before declaring a claim unreadable. Raises OSError when the
    location cannot be written at all — the caller turns that into
    ClaimUnwritable."""
    payload = _claim_payload(notice_dir, consumer_name)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass  # no hard-link support here; fall through
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    return True


def _read_claim(path: str) -> Optional[dict]:
    """Read a claim, tolerating the brief partial-file window that the
    no-hard-link fallback above can still produce. Returns the parsed dict, or
    None if it is genuinely unreadable after retries (or gone)."""
    for attempt in range(5):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            if raw.strip():
                claim = json.loads(raw)
                if isinstance(claim, dict):
                    return claim
        except FileNotFoundError:
            return None  # holder released it while we looked
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        if attempt < 4:
            time.sleep(0.05)
    return None


def acquire_consumer_claim(
    claim_path: str,
    notice_dir: str,
    consumer_name: str,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> ClaimHandle:
    """Claim exclusive consumption of `notice_dir` by publishing `claim_path`.

    Returns a ClaimHandle on success. Raises:
      ClaimUnwritable — nothing can be written at `claim_path` (the caller
                        decides whether that is a warning or fatal);
      ClaimHeld       — another consumer holds it, or the file is unreadable,
                        or it is flapping. Never start a second consumer.

    `log`, if given, receives the one informational line this function has
    to say (a stale claim being reclaimed). Nothing else is ever printed.
    Must be called BEFORE any watcher starts.
    """
    try:
        if _try_create(claim_path, notice_dir, consumer_name):
            return ClaimHandle(claim_path)
    except OSError as e:
        raise ClaimUnwritable(
            f"cannot write a consumer claim at {claim_path} ({e})",
            path=claim_path, cause=e)

    # Someone holds it. Read, and only proceed if they are definitively gone.
    claim = _read_claim(claim_path)
    if claim is None and not os.path.exists(claim_path):
        # Released while we looked. One clean retry, then give up loudly rather
        # than looping: a claim flapping this fast is a deployment problem.
        try:
            if _try_create(claim_path, notice_dir, consumer_name):
                return ClaimHandle(claim_path)
        except OSError:
            pass
        raise ClaimHeld(
            f"the consumer claim at {claim_path} is being taken and released "
            "repeatedly — another consumer is restarting in a loop. Not "
            "starting a competing watcher.", path=claim_path)
    if claim is None:
        raise ClaimHeld(
            f"a consumer claim exists at {claim_path} but could not be parsed after "
            f"retries. Refusing to start a second consumer of {notice_dir} — "
            "two watchers race and silently lose notices. If no other consumer "
            "is running, delete that file.", path=claim_path)

    holder = claim.get("consumer")
    holder_pid = claim.get("pid") if isinstance(claim.get("pid"), int) else None
    if _holder_is_live(claim):
        raise ClaimHeld(
            f"{holder or 'another consumer'} (pid {claim.get('pid')}) already "
            f"consumes {notice_dir}; claim at {claim_path}. Refusing to start a "
            "second consumer — two watchers race and silently lose notices. Run "
            "exactly one consumer of a notice directory. If that "
            "process is gone, delete the claim file.",
            path=claim_path, holder=str(holder) if holder else None,
            holder_pid=holder_pid)

    if log is not None:
        log(f"reclaiming stale consumer claim at {claim_path} "
            f"(holder pid {claim.get('pid')} no longer exists, or its pid was "
            "recycled by an unrelated process)")
    try:
        os.unlink(claim_path)
    except OSError as e:
        raise ClaimHeld(
            f"could not remove the stale consumer claim at {claim_path}: {e}",
            path=claim_path)
    try:
        if not _try_create(claim_path, notice_dir, consumer_name):
            raise ClaimHeld(
                f"lost a race for the consumer claim at {claim_path} — another "
                "consumer started at the same moment. Not starting.",
                path=claim_path)
    except OSError as e:
        raise ClaimHeld(
            f"could not take the consumer claim at {claim_path}: {e}",
            path=claim_path)
    return ClaimHandle(claim_path)


def release_consumer_claim(handle: Optional[ClaimHandle]) -> None:
    """Convenience for callers holding a handle in a global; safe on None."""
    if handle is not None:
        handle.release()
