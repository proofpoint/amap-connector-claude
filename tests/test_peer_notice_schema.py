"""tests/test_peer_notice_schema.py — the documents our tests write validate
against the spec's own schema, using the spec's own validator.

The daemon validates against the schema when it exists. It exists as of 2026-09-03 — drafted in the working tree of
`agent-mailbox-protocol/` (a sibling checkout), not yet on `main`. So this
test loads that repo's `fixtures/validate.py` BY PATH and runs its
`check_document` over the exact notice documents `test_inbox_delivery.py`
writes into the peer and mail trees. One shape under test, not a copy.

If the sibling checkout or the schema is absent the test SKIPS, loudly: a
skipped validation is not a passed one, so the skip message names every
place that was looked and every variable that could have said otherwise.
Set AMAP_REQUIRE_SPEC=1 to turn that skip into a hard failure — the gate
for a run that is only meaningful if the schema really was checked.

DISCOVERY IS DUAL-NAMED for the amap rename (inbox-lab #21): the spec repo
`agent-mailbox-protocol` becomes `amap-spec`, and a name-keyed walk-up is
exactly the kind of lookup that goes quietly green-by-skip the moment the
name it hardcodes stops existing. New name first, old name still accepted,
and $AMAP_SPEC_REPO / $AMP_SPEC_REPO override the search entirely.

Stdlib only, like everything here.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

# New name first in both. The env vars beat the walk-up; within each, the
# post-rename spelling wins so a transitional tree carrying both resolves
# forward, never back.
SPEC_ENV_VARS = ("AMAP_SPEC_REPO", "AMP_SPEC_REPO")
SPEC_DIR_NAMES = ("amap-spec", "agent-mailbox-protocol")
REQUIRE_ENV_VAR = "AMAP_REQUIRE_SPEC"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _carries_spec(cand: Path) -> bool:
    """A candidate counts only if it really holds both files we use. The
    DIRECTORY NAME IS NEVER ENOUGH — that is what makes the dual-name search
    safe to widen: a stale empty `agent-mailbox-protocol/` left behind by the
    rename cannot shadow a real `amap-spec/`."""
    return (cand / "schemas" / "peer-notice.schema.json").is_file() \
        and (cand / "fixtures" / "validate.py").is_file()


def _resolve_spec(start: Path = HERE, env=None):
    """(spec_dir | None, reason_if_none). `start` and `env` are injectable so
    the resolution order itself is testable without a real sibling checkout."""
    env = os.environ if env is None else env
    for var in SPEC_ENV_VARS:
        raw = env.get(var)
        if raw:
            cand = Path(raw).expanduser()
            if _carries_spec(cand):
                return cand, None
            # An explicit pointer that does not hold the spec is an ERROR, not
            # an invitation to go looking elsewhere: falling through to the
            # walk-up here would validate against a checkout the operator did
            # not name, which is the silent-wrong-source failure this whole
            # file exists to prevent.
            return None, (
                f"${var} is set to {raw!r}, but that directory does not carry both "
                "schemas/peer-notice.schema.json and fixtures/validate.py; refusing "
                "to fall back to a checkout you did not name")
    for parent in Path(start).resolve().parents:
        for name in SPEC_DIR_NAMES:
            if _carries_spec(parent / name):
                return parent / name, None
    return None, (
        "no spec checkout found: walked up from {start} looking for {names}, each "
        "required to carry schemas/peer-notice.schema.json and fixtures/validate.py; "
        "neither ${vars} was set. NOTHING WAS VALIDATED — this run's green says "
        "nothing about the schema.".format(
            start=start, names=" or ".join(SPEC_DIR_NAMES),
            vars=" nor $".join(SPEC_ENV_VARS)))


SPEC, SKIP_REASON = _resolve_spec()
REQUIRE_SPEC = os.environ.get(REQUIRE_ENV_VAR, "").strip() not in ("", "0")
fixtures = _load(HERE / "test_inbox_delivery.py", "_delivery_fixtures")


@unittest.skipIf(SPEC is None, SKIP_REASON)
class PeerNoticeSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = _load(SPEC / "fixtures" / "validate.py", "_amp_validate")

    def _check(self, name, doc):
        return self.gate.check_document(name, doc)

    def test_the_peer_notice_our_tests_write_is_a_valid_peer_notice(self):
        notice, _ = fixtures.peer_notice_docs()
        self.assertEqual(self._check("peer-test.json", notice), [])

    def test_a_reply_notice_validates_too(self):
        notice, _ = fixtures.peer_notice_docs(frm=fixtures.CAROL, in_reply_to=fixtures.OTHER_ID)
        self.assertEqual(self._check("peer-reply.json", notice), [])

    def test_the_body_spool_document_is_a_valid_inbound_message(self):
        _, message = fixtures.peer_notice_docs()
        self.assertEqual(self._check("message-test.json", message), [])

    def test_the_mail_notice_our_tests_write_is_a_valid_deliver_notice(self):
        self.assertEqual(self._check("notice-test.json", fixtures.mail_notice_doc()), [])

    def test_a_wrong_kind_fails_the_peer_schema(self):
        """The second lock behind the write-permission boundary: the schema
        itself pins kind to `peer`, so a `deliver` document under peer/ is
        invalid before the daemon ever looks at it."""
        notice, _ = fixtures.peer_notice_docs(kind="deliver")
        self.assertNotEqual(self._check("peer-wrongkind.json", notice), [])

    def test_a_display_name_from_fails(self):
        """`from` is pinned to a bare addr-spec — the one `from` in the protocol
        a consumer may treat as runtime-asserted, which is why the daemon can
        refuse a notice that claims the router's own local part (ruling 16:
        there is no allowlist; presence in the tree is the authorisation)."""
        notice, _ = fixtures.peer_notice_docs(frm='Alice <alice@example.com>')
        self.assertNotEqual(self._check("peer-displayname.json", notice), [])

    def test_the_validator_is_the_specs_own(self):
        """If someone vendors a copy this stops meaning anything."""
        self.assertTrue(str(Path(self.gate.__file__).resolve()).startswith(str(SPEC)))


@unittest.skipUnless(REQUIRE_SPEC, f"${REQUIRE_ENV_VAR} is not set")
class SpecCheckoutRequiredTest(unittest.TestCase):
    """The release gate. Skipping is the honest default for a developer who
    has no spec checkout, but a release run must not be allowed to report OK
    on the strength of seven skips. With $AMAP_REQUIRE_SPEC=1 the absence
    becomes one unmistakable red naming exactly what was looked for — and
    the presence becomes one PASS, so a gated run with the spec found reads
    as all-green rather than as a skip whose reason has to be read to learn
    it was the good case."""

    def test_the_spec_checkout_is_present_as_required(self):
        self.assertIsNotNone(SPEC, SKIP_REASON)


class SpecDiscoveryTest(unittest.TestCase):
    """Covers `_resolve_spec` itself, and ALWAYS RUNS — including on a machine
    with no spec checkout, which is the machine where the rest of this file is
    dark. The discovery is the part that failed silently before: a lookup
    keyed on a name that stopped existing returned None and the suite went
    green. These assertions are what make the widened search a claim rather
    than a hope."""

    def _spec_tree(self, root: Path, name: str, *, complete: bool = True) -> Path:
        cand = root / name
        (cand / "schemas").mkdir(parents=True)
        (cand / "fixtures").mkdir(parents=True)
        (cand / "schemas" / "peer-notice.schema.json").write_text("{}")
        if complete:
            # Never executed: these tests assert on RESOLUTION ONLY. Loading a
            # stub validator and asserting against it would be the "sixteen
            # tests passed against a protocol that does not exist" mistake in
            # a new costume.
            (cand / "fixtures" / "validate.py").write_text("# stub, never loaded\n")
        return cand

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="amap-spec-discovery-")).resolve()
        self.start = self.tmp / "workspace" / "connector" / "tests"
        self.start.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_new_name_is_found_by_the_walk_up(self):
        want = self._spec_tree(self.tmp / "workspace", "amap-spec")
        self.assertEqual(_resolve_spec(self.start, env={})[0], want)

    def test_the_old_name_is_still_found(self):
        want = self._spec_tree(self.tmp / "workspace", "agent-mailbox-protocol")
        self.assertEqual(_resolve_spec(self.start, env={})[0], want)

    def test_the_new_name_wins_when_both_exist(self):
        want = self._spec_tree(self.tmp / "workspace", "amap-spec")
        self._spec_tree(self.tmp / "workspace", "agent-mailbox-protocol")
        self.assertEqual(_resolve_spec(self.start, env={})[0], want)

    def test_a_directory_with_the_right_name_but_no_files_is_not_accepted(self):
        """The guard that lets the search be widened at all: an empty husk left
        by the rename must not shadow the real checkout one level further up."""
        self._spec_tree(self.tmp / "workspace" / "connector", "amap-spec",
                        complete=False)
        want = self._spec_tree(self.tmp / "workspace", "amap-spec")
        self.assertEqual(_resolve_spec(self.start, env={})[0], want)

    def test_the_env_var_beats_the_walk_up(self):
        self._spec_tree(self.tmp / "workspace", "amap-spec")
        want = self._spec_tree(self.tmp, "elsewhere")
        got, reason = _resolve_spec(self.start, env={"AMAP_SPEC_REPO": str(want)})
        self.assertEqual(got, want)
        self.assertIsNone(reason)

    def test_the_new_env_var_beats_the_old_one(self):
        new = self._spec_tree(self.tmp, "new-pointer")
        old = self._spec_tree(self.tmp, "old-pointer")
        got, _ = _resolve_spec(self.start, env={"AMAP_SPEC_REPO": str(new),
                                                "AMP_SPEC_REPO": str(old)})
        self.assertEqual(got, new)

    def test_the_legacy_env_var_still_works_alone(self):
        want = self._spec_tree(self.tmp, "legacy-pointer")
        got, _ = _resolve_spec(self.start, env={"AMP_SPEC_REPO": str(want)})
        self.assertEqual(got, want)

    def test_an_env_var_pointing_at_no_spec_does_not_fall_back(self):
        """Silently validating against a checkout the operator did not name is
        the failure this file exists to prevent."""
        self._spec_tree(self.tmp / "workspace", "amap-spec")
        got, reason = _resolve_spec(self.start,
                                    env={"AMAP_SPEC_REPO": str(self.tmp / "nothing-here")})
        self.assertIsNone(got)
        self.assertIn("AMAP_SPEC_REPO", reason)

    def test_the_not_found_reason_names_both_names_and_both_vars(self):
        """The skip message is the whole diagnostic — a reader must be able to
        tell 'no checkout' from 'passed' without reading this file."""
        got, reason = _resolve_spec(self.start, env={})
        self.assertIsNone(got)
        for token in ("amap-spec", "agent-mailbox-protocol",
                      "AMAP_SPEC_REPO", "AMP_SPEC_REPO", "NOTHING WAS VALIDATED"):
            self.assertIn(token, reason)


if __name__ == "__main__":
    unittest.main()
