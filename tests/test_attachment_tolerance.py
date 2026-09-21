"""tests/test_attachment_tolerance.py — agent-mailbox-protocol v2.3.0 §5
consumer-tolerance proof for `inbox-mcp-vol`.

v2.3.0 removes the release-scoped byte embargo from §2/§5: a runtime MAY now
publish an attachment's bytes and emit `content_ref`/`sha256` for a
descriptor it asserts `disposition == "clean"`. The new consumer obligation
(§5) is the thing this file drives end to end against the real binary:

  - a connector MUST tolerate a descriptor carrying `content_ref` it never
    resolves — `read_message`/`list_messages` must never treat the field's
    presence as a fetch requirement, and must render fully regardless;
  - resolving one (`read_attachment`) is opt-in, and MUST derive the served
    path from the message's own resolved spool key + the requested index —
    NEVER from the descriptor's `filename` or from its own `content_ref`
    string parsed as a path — so a hostile/forged/unresolvable grant is
    inert, not a crash and not a leak.

Reuses `_McpProc` (and the bin-resolution helpers) from
`test_readonly_inbound.py` rather than re-implementing the JSON-RPC stdio
driver — same package, same test process, no reason to duplicate it.
Self-contained otherwise: builds its own tmp spool per test, stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parent))
from test_readonly_inbound import _BASE_ENV, _McpProc  # noqa: E402


def _write_message(messages_dir: Path, notice_id: str, attachments: list) -> Path:
    doc = {
        "contract_version": "2",
        "notice_id": notice_id,
        "from": "Alice <alice@example.org>",
        "to": "agent@example.org",
        "subject": "see attached",
        "body_text": "the full spooled body text",
        "attachments": attachments,
    }
    path = messages_dir / f"notice-{notice_id}.json"
    path.write_text(json.dumps(doc))
    return path


def _write_sidecar(notices_dir: Path, notice_id: str, index: int, data: bytes) -> None:
    """Publish bytes where the runtime publishes them: seam §2's
    `<tree>/notices/<notice_id>.attachments/<index>`, keyed by the BARE id.

    Takes the notices directory explicitly rather than deriving it from the
    messages directory. The code under test derives it; a fixture that derived
    it the same way would agree with the code by construction and could never
    catch the derivation being wrong."""
    sidecar_dir = notices_dir / f"{notice_id}.attachments"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / str(index)).write_bytes(data)


class AttachmentToleranceTest(unittest.TestCase):
    NOTICE_ID = "att-test-0001"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amp-att-tolerance-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.messages_dir = self.tmp / "inbox" / "messages"
        self.notices_dir = self.tmp / "inbox" / "notices"
        self.messages_dir.mkdir(parents=True)
        self.notices_dir.mkdir(parents=True)
        self.env = {**_BASE_ENV, "INBOX_MESSAGE_DIR": str(self.messages_dir)}

    def _proc(self) -> _McpProc:
        proc = _McpProc("inbox-mcp-vol", self.env)
        proc.initialize()
        return proc

    def _read_message(self, proc: _McpProc, notice_id: str) -> dict:
        resp = proc.request(
            "tools/call", {"name": "read_message", "arguments": {"id": notice_id}})
        return resp["result"]

    def _read_attachment(self, proc: _McpProc, notice_id: str, index: int) -> dict:
        resp = proc.request(
            "tools/call",
            {"name": "read_attachment",
             "arguments": {"message_id": notice_id, "index": index}})
        return resp["result"]

    # 1. clean descriptor + valid content_ref/sha256 + real sidecar on disk
    #    -> read_message renders "readable"; read_attachment returns the bytes.
    def test_clean_descriptor_with_real_sidecar_is_readable(self):
        data = b"hello attachment bytes"
        digest = hashlib.sha256(data).hexdigest()
        _write_message(self.messages_dir, self.NOTICE_ID, [{
            "filename": "notes.txt",
            "media_type": "text/plain",
            "size_bytes": len(data),
            "disposition": "clean",
            "content_ref": f"{self.NOTICE_ID}.attachments/0",
            "sha256": digest,
        }])
        _write_sidecar(self.notices_dir, self.NOTICE_ID, 0, data)

        proc = self._proc()
        try:
            msg = self._read_message(proc, self.NOTICE_ID)
            self.assertNotIn("isError", msg)
            text = msg["content"][0]["text"]
            self.assertIn("readable via read_attachment", text)

            att = self._read_attachment(proc, self.NOTICE_ID, 0)
            self.assertNotIn("isError", att)
            att_text = att["content"][0]["text"]
            self.assertIn(digest, att_text)
            self.assertIn("hello attachment bytes", att_text)  # text/* inlined
        finally:
            proc.close()

    # 2. clean descriptor with content_ref but NO sidecar published on disk
    #    -> read_message still renders fully (tolerance: content_ref is a
    #       grant, not a fetch requirement); read_attachment is a clean
    #       isError, never a crash.
    def test_clean_descriptor_without_sidecar_still_renders_and_refuses_fetch(self):
        digest = hashlib.sha256(b"never published").hexdigest()
        _write_message(self.messages_dir, self.NOTICE_ID, [{
            "filename": "ghost.txt",
            "media_type": "text/plain",
            "size_bytes": 16,
            "disposition": "clean",
            "content_ref": f"{self.NOTICE_ID}.attachments/0",
            "sha256": digest,
        }])
        # deliberately no sidecar file/dir written

        proc = self._proc()
        try:
            msg = self._read_message(proc, self.NOTICE_ID)
            self.assertNotIn("isError", msg)
            self.assertIn("the full spooled body text", msg["content"][0]["text"])

            att = self._read_attachment(proc, self.NOTICE_ID, 0)
            self.assertIn("isError", att)
            self.assertTrue(att["isError"])
        finally:
            proc.close()

    # 3. hostile filename ("../../x", an absolute path) on an otherwise valid
    #    clean grant -> read succeeds via the index-derived path; filename
    #    never becomes a path component (assert via the returned bytes/path,
    #    and that no file outside the spool tree was ever touched).
    def test_hostile_filename_is_inert_path_stays_index_derived(self):
        data = b"index derived bytes"
        digest = hashlib.sha256(data).hexdigest()
        for hostile_name in ("../../etc/passwd", "/etc/passwd"):
            with self.subTest(filename=hostile_name):
                notice_id = "att-hostile-" + str(abs(hash(hostile_name)) % 10000)
                _write_message(self.messages_dir, notice_id, [{
                    "filename": hostile_name,
                    "media_type": "text/plain",
                    "size_bytes": len(data),
                    "disposition": "clean",
                    "content_ref": f"{notice_id}.attachments/0",
                    "sha256": digest,
                }])
                _write_sidecar(self.notices_dir, notice_id, 0, data)

                proc = self._proc()
                try:
                    att = self._read_attachment(proc, notice_id, 0)
                    self.assertNotIn("isError", att)
                    att_text = att["content"][0]["text"]
                    # the served path is the recomputed index-derived one,
                    # under THIS notice's own sidecar dir -- never the
                    # attacker-controlled filename used as a path.
                    expected_path = str(
                        self.notices_dir / f"{notice_id}.attachments" / "0")
                    self.assertIn(f"path:       {expected_path}", att_text)
                    # the hostile string is only ever echoed as an inert
                    # display field (`filename:   ...`) -- it never appears
                    # as (part of) the resolved `path:` line above.
                    path_line = next(
                        line for line in att_text.splitlines()
                        if line.startswith("path:"))
                    self.assertNotIn("etc/passwd", path_line)
                finally:
                    proc.close()

    # 4. forged content_ref (wrong index / another notice's key) ->
    #    load_published drops the keys; read_attachment refuses.
    def test_forged_content_ref_is_dropped_and_refused(self):
        data = b"real bytes for notice A"
        digest = hashlib.sha256(data).hexdigest()
        other_id = "att-other-0002"
        # Notice A's descriptor claims notice B's sidecar ref.
        _write_message(self.messages_dir, self.NOTICE_ID, [{
            "filename": "steal.txt",
            "media_type": "text/plain",
            "size_bytes": len(data),
            "disposition": "clean",
            "content_ref": f"{other_id}.attachments/0",  # WRONG key: another notice
            "sha256": digest,
        }])
        _write_sidecar(self.notices_dir, other_id, 0, data)

        proc = self._proc()
        try:
            msg = self._read_message(proc, self.NOTICE_ID)
            self.assertNotIn("isError", msg)
            text = msg["content"][0]["text"]
            # dropped content_ref -> not rendered as readable
            self.assertNotIn("readable via read_attachment", text)
            self.assertIn("bytes not published by the runtime", text)

            att = self._read_attachment(proc, self.NOTICE_ID, 0)
            self.assertIn("isError", att)
            self.assertTrue(att["isError"])
        finally:
            proc.close()

        # Also cover a wrong-index-within-the-same-notice forgery.
        notice_id2 = "att-wrongidx-0003"
        _write_message(self.messages_dir, notice_id2, [
            {
                "filename": "a.txt", "media_type": "text/plain",
                "size_bytes": len(data), "disposition": "clean",
                # index 1's content_ref points at index 0's slot
                "content_ref": f"{notice_id2}.attachments/0",
                "sha256": digest,
            },
            {
                "filename": "b.txt", "media_type": "text/plain",
                "size_bytes": len(data), "disposition": "clean",
                "content_ref": f"{notice_id2}.attachments/0",  # should be /1
                "sha256": digest,
            },
        ])
        _write_sidecar(self.notices_dir, notice_id2, 0, data)

        proc2 = self._proc()
        try:
            att0 = self._read_attachment(proc2, notice_id2, 0)
            self.assertNotIn("isError", att0)  # index 0's own ref is correct
            att1 = self._read_attachment(proc2, notice_id2, 1)
            self.assertIn("isError", att1)  # index 1's ref pointed at index 0 -- refused
        finally:
            proc2.close()

    # 5. unscanned descriptor carrying content_ref/sha256 (a runtime
    #    violating the clean-gate) -> keys dropped, bytes withheld, no crash.
    def test_unscanned_descriptor_with_content_ref_is_clean_gated(self):
        data = b"should never be reachable"
        digest = hashlib.sha256(data).hexdigest()
        _write_message(self.messages_dir, self.NOTICE_ID, [{
            "filename": "sneaky.txt",
            "media_type": "text/plain",
            "size_bytes": len(data),
            "disposition": "unscanned",  # NOT clean -- content_ref must be ignored
            "content_ref": f"{self.NOTICE_ID}.attachments/0",
            "sha256": digest,
        }])
        _write_sidecar(self.notices_dir, self.NOTICE_ID, 0, data)

        proc = self._proc()
        try:
            msg = self._read_message(proc, self.NOTICE_ID)
            self.assertNotIn("isError", msg)
            text = msg["content"][0]["text"]
            self.assertIn("disposition=unscanned", text)
            self.assertNotIn("readable via read_attachment", text)

            att = self._read_attachment(proc, self.NOTICE_ID, 0)
            self.assertIn("isError", att)
            self.assertTrue(att["isError"])
            self.assertIn("bytes withheld", att["content"][0]["text"])
        finally:
            proc.close()


if __name__ == "__main__":
    unittest.main()


# --- seam §2 (v3.0.0): bytes live under notices/, keyed by the BARE id ------

SPEC_NOTICE_ID = "1721457600000000003-4242"      # the spec's own fixture id
SPEC_REF = f"{SPEC_NOTICE_ID}.attachments/0"     # notice-attachment-clean-ref.json


class SpecSidecarLocationTest(unittest.TestCase):
    """Step 1 of the coordinated fix: resolve seam §2's location FIRST, keyed
    by the bare notice id, keeping the legacy messages-side path as a
    temporary fallback so nothing disappears before the runtime moves.

    Two divergences are pinned here, because fixing only the first still
    misses:
      directory   messages/  ->  notices/
      key         notice-<id>  ->  <id>     (the sidecar dir carries no prefix
                                             even though the spool FILE does)

    The fixture models the RUNTIME, not this connector's imagination: id and
    content_ref are the spec's own valid/notice-attachment-clean-ref.json.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amap-att-spec-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lane = self.tmp / "inbox"
        self.messages_dir = self.lane / "messages"
        self.notices_dir = self.lane / "notices"
        self.messages_dir.mkdir(parents=True)
        self.notices_dir.mkdir(parents=True)
        self.env = {**_BASE_ENV, "INBOX_MESSAGE_DIR": str(self.messages_dir)}
        self.data = b"published where the seam says"
        self.digest = hashlib.sha256(self.data).hexdigest()

    def _publish(self, where: Path, key: str) -> None:
        d = where / f"{key}.attachments"
        d.mkdir(parents=True, exist_ok=True)
        (d / "0").write_bytes(self.data)

    def _descriptor(self, ref: str) -> list:
        return [{"filename": "a.txt", "media_type": "text/plain",
                 "size_bytes": len(self.data), "disposition": "clean",
                 "sha256": self.digest, "content_ref": ref}]

    def _read(self, notice_id: str, index: int = 0) -> dict:
        proc = _McpProc("inbox-mcp-vol", self.env)
        try:
            proc.initialize()
            return proc.request("tools/call", {
                "name": "read_attachment",
                "arguments": {"message_id": notice_id, "index": index}})["result"]
        finally:
            proc.close()

    def test_the_spec_location_and_bare_key_resolve(self):
        """THE FIX. Nothing is published messages-side at all."""
        _write_message(self.messages_dir, SPEC_NOTICE_ID, self._descriptor(SPEC_REF))
        self._publish(self.notices_dir, SPEC_NOTICE_ID)
        att = self._read(SPEC_NOTICE_ID)
        self.assertNotIn("isError", att)
        self.assertIn(self.digest, att["content"][0]["text"])

    def test_the_spec_form_ref_survives_load_published(self):
        """THE GATE, and why fixing read_attachment alone was not enough: a ref
        that fails the equality check has content_ref and sha256 STRIPPED, so
        the attachment renders as withheld and no path is ever computed."""
        _write_message(self.messages_dir, SPEC_NOTICE_ID, self._descriptor(SPEC_REF))
        self._publish(self.notices_dir, SPEC_NOTICE_ID)
        proc = _McpProc("inbox-mcp-vol", self.env)
        try:
            proc.initialize()
            msg = proc.request("tools/call", {
                "name": "read_message",
                "arguments": {"id": SPEC_NOTICE_ID}})["result"]
        finally:
            proc.close()
        self.assertIn("readable via read_attachment", msg["content"][0]["text"])

    def test_the_legacy_location_is_not_consulted(self):
        """Step 3. The messages-side path has no producer behind it any more,
        and a stale copy left there must not be served — which is the whole
        reason the fallback could not simply be left in place."""
        _write_message(self.messages_dir, SPEC_NOTICE_ID, self._descriptor(SPEC_REF))
        self._publish(self.notices_dir, SPEC_NOTICE_ID)
        legacy = self.messages_dir / f"notice-{SPEC_NOTICE_ID}.attachments"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "0").write_bytes(b"stale messages-side copy")
        att = self._read(SPEC_NOTICE_ID)
        self.assertIn(self.digest, att["content"][0]["text"])

    def test_a_foreign_or_traversal_ref_is_still_refused(self):
        """Widening the accepted spellings must not widen what is accepted."""
        for bad in (f"../{SPEC_NOTICE_ID}.attachments/0",
                    "someone-else.attachments/0",
                    f"{SPEC_NOTICE_ID}.attachments/1"):
            _write_message(self.messages_dir, SPEC_NOTICE_ID, self._descriptor(bad))
            self._publish(self.notices_dir, SPEC_NOTICE_ID)
            att = self._read(SPEC_NOTICE_ID)
            self.assertIn("isError", att, f"accepted a bad ref: {bad!r}")


class PerTreeConfinementTest(unittest.TestCase):
    """peer-origin §1d: bytes resolve only within the `notices/` of the tree
    the enclosing document was read from — a peer notice can never point into
    the mail sidecar directory, or vice versa.

    Here that is STRUCTURAL rather than checked. One server process is
    configured with one INBOX_MESSAGE_DIR, both resolution candidates are
    derived from it (`<tree>/notices/` primary, `<tree>/messages/` fallback),
    and the other tree's path is not constructible. "The tree the document was
    read from" and "the configured tree" are the same thing because the
    process only ever reads one. These tests pin that, so a later refactor
    that introduces a second root has to break them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="amap-att-tree-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.trees = {}
        for lane in ("inbox", "peer"):
            m = self.tmp / lane / "messages"; n = self.tmp / lane / "notices"
            m.mkdir(parents=True); n.mkdir(parents=True)
            self.trees[lane] = (m, n)
        self.data = b"per-tree bytes"
        self.digest = hashlib.sha256(self.data).hexdigest()

    def _publish(self, notices: Path, notice_id: str) -> None:
        d = notices / f"{notice_id}.attachments"; d.mkdir(parents=True, exist_ok=True)
        (d / "0").write_bytes(self.data)

    def _read(self, lane: str, notice_id: str) -> dict:
        env = {**_BASE_ENV, "INBOX_MESSAGE_DIR": str(self.trees[lane][0])}
        proc = _McpProc("inbox-mcp-vol", env)
        try:
            proc.initialize()
            return proc.request("tools/call", {
                "name": "read_attachment",
                "arguments": {"message_id": notice_id, "index": 0}})["result"]
        finally:
            proc.close()

    def _descriptor(self, notice_id: str) -> list:
        return [{"filename": "a.txt", "media_type": "text/plain",
                 "size_bytes": len(self.data), "disposition": "clean",
                 "sha256": self.digest,
                 "content_ref": f"{notice_id}.attachments/0"}]

    def test_the_peer_tree_resolves_its_own_notices(self):
        """The half the original step 1 instruction omitted: the delegation
        lane publishes attachments too, and a mail-only fix leaves them dark
        in exactly the window the sequence exists to prevent."""
        m, n = self.trees["peer"]
        _write_message(m, SPEC_NOTICE_ID, self._descriptor(SPEC_NOTICE_ID))
        self._publish(n, SPEC_NOTICE_ID)
        att = self._read("peer", SPEC_NOTICE_ID)
        self.assertNotIn("isError", att)
        self.assertIn(self.digest, att["content"][0]["text"])

    def test_a_peer_notice_never_reaches_the_mail_tree(self):
        """§1d's prohibition. The bytes exist — in the OTHER tree — and the
        descriptor is otherwise valid. Resolution must fail rather than find
        them."""
        pm, _ = self.trees["peer"]
        _, mn = self.trees["inbox"]
        _write_message(pm, SPEC_NOTICE_ID, self._descriptor(SPEC_NOTICE_ID))
        self._publish(mn, SPEC_NOTICE_ID)          # published in MAIL's notices
        att = self._read("peer", SPEC_NOTICE_ID)   # read as a PEER document
        self.assertIn("isError", att)

    def test_a_mail_notice_never_reaches_the_peer_tree(self):
        mm, _ = self.trees["inbox"]
        _, pn = self.trees["peer"]
        _write_message(mm, SPEC_NOTICE_ID, self._descriptor(SPEC_NOTICE_ID))
        self._publish(pn, SPEC_NOTICE_ID)
        att = self._read("inbox", SPEC_NOTICE_ID)
        self.assertIn("isError", att)
