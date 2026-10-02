"""tests/test_peers_tool.py — inbox-submit's `peers` tool, driven over MCP stdio
against a real roster.json in a temp directory.

The roster is the runtime's document (roster.schema.json): a DIRECTORY, not an
allowlist. These tests pin the four properties that make the tool safe to hand
an agent: ambiguity is reported rather than resolved by guessing; an absent
roster is an error rather than an empty fleet; freshness is reported rather
than judged; and the caller's own address is flagged only when the deployment
says what it is. Stdlib only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
_ro = SourceFileLoader("_ro_for_peers", str(HERE / "test_readonly_inbound.py")).load_module()
_McpProc = _ro._McpProc

DOMAIN = "example.com"


def member(local, state="admitted"):
    return {"address": f"{local}@{DOMAIN}", "state": state}


class PeersTool(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="peers-"))
        self.roster_dir = self.tmp / "roster"
        self.roster_dir.mkdir()
        self.outbox = self.tmp / "outbox"
        self.outbox.mkdir()

    def write_roster(self, members, **over):
        doc = {
            "contract_version": "2",
            "router": f"amap.router@{DOMAIN}",
            "written_at": datetime.now(timezone.utc).isoformat(),
            "interval_s": 5.0,
            "members": members,
        }
        for k, v in over.items():
            if v is _DROP:
                doc.pop(k, None)
            else:
                doc[k] = v
        (self.roster_dir / "roster.json").write_text(json.dumps(doc), encoding="utf-8")

    def env(self, **over):
        e = {k: v for k, v in os.environ.items()
             if k not in ("AMAP_ROSTER_DIR", "AMAP_SELF", "MAILBOX_ROOT_DIR")}
        e["OUTBOX_DIR"] = str(self.outbox)
        e["AMAP_ROSTER_DIR"] = str(self.roster_dir)
        for k, v in over.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def call(self, query=None, **env_over):
        proc = _McpProc("inbox-submit", self.env(**env_over), extra_args=["mcp"])
        try:
            proc.initialize()
            args = {} if query is None else {"query": query}
            resp = proc.request("tools/call", {"name": "peers", "arguments": args})
        finally:
            proc.close()
        result = resp["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return None, text
        return json.loads(text), None

    def addresses(self, out):
        return sorted(m["address"] for m in out["members"])

    # --- matching and ambiguity -------------------------------------------------

    def test_empty_query_lists_every_member(self):
        self.write_roster([member("alpha-1a2b"), member("beta-3c4d"), member("gamma-5e6f")])
        out, err = self.call("")
        self.assertIsNone(err)
        self.assertEqual(len(out["members"]), 3)
        self.assertFalse(out["ambiguous"])

    def test_a_shared_prefix_is_ambiguous_and_returns_every_candidate(self):
        """The case that made prose guidance unsafe: two members whose local
        parts share the queried prefix. Both come back, flagged, unranked."""
        self.write_roster([member("amap-spec-68e2e676"), member("amap-spec-old-11112222"),
                           member("beta-3c4d")])
        out, err = self.call("amap-spec")
        self.assertIsNone(err)
        self.assertTrue(out["ambiguous"])
        self.assertEqual(self.addresses(out), [f"amap-spec-68e2e676@{DOMAIN}",
                                               f"amap-spec-old-11112222@{DOMAIN}"])

    def test_an_exact_local_part_wins_over_a_longer_prefix_match(self):
        self.write_roster([member("beta"), member("beta-old")])
        out, _ = self.call("BETA")
        self.assertFalse(out["ambiguous"])
        self.assertEqual(self.addresses(out), [f"beta@{DOMAIN}"])

    def test_a_full_address_matches_exactly(self):
        self.write_roster([member("beta"), member("beta-old")])
        out, _ = self.call(f"beta-old@{DOMAIN}")
        self.assertEqual(self.addresses(out), [f"beta-old@{DOMAIN}"])

    def test_prefix_is_tried_before_substring(self):
        self.write_roster([member("ops-beta"), member("beta-7")])
        out, _ = self.call("beta")
        self.assertFalse(out["ambiguous"])
        self.assertEqual(self.addresses(out), [f"beta-7@{DOMAIN}"])

    def test_no_match_is_an_empty_result_not_an_error(self):
        self.write_roster([member("alpha")])
        out, err = self.call("zeta")
        self.assertIsNone(err)
        self.assertEqual(out["members"], [])
        self.assertFalse(out["ambiguous"])

    # --- absent is unknown, never empty -----------------------------------------

    def test_an_unset_roster_variable_is_an_error_naming_it(self):
        out, err = self.call("", AMAP_ROSTER_DIR=None)
        self.assertIsNone(out)
        self.assertIn("AMAP_ROSTER_DIR", err)

    def test_a_missing_roster_file_is_an_error_never_an_empty_fleet(self):
        out, err = self.call("")                       # directory exists, file does not
        self.assertIsNone(out, "an absent roster must not read as an empty fleet")
        self.assertIn("UNKNOWN", err)

    # --- version first ----------------------------------------------------------

    def test_an_absent_contract_version_is_refused(self):
        self.write_roster([member("alpha")], contract_version=_DROP)
        out, err = self.call("")
        self.assertIsNone(out)
        self.assertIn("contract_version", err)

    def test_another_major_is_refused(self):
        self.write_roster([member("alpha")], contract_version="3")
        out, err = self.call("")
        self.assertIsNone(out)
        self.assertIn("contract_version", err)

    # --- reported, not judged ---------------------------------------------------

    def test_an_absent_interval_is_null_never_a_default(self):
        self.write_roster([member("alpha")], interval_s=_DROP)
        out, _ = self.call("")
        self.assertIsNone(out["interval_s"])

    def test_age_is_reported_and_there_is_no_staleness_verdict(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        self.write_roster([member("alpha")], written_at=old, interval_s=5.0)
        out, _ = self.call("")
        self.assertGreater(out["age_s"], 7000)
        self.assertNotIn("stale", out, "the deployment applies its own staleness rule")

    def test_an_unknown_state_is_passed_through_untouched(self):
        self.write_roster([member("alpha", state="quarantined-for-review")])
        out, _ = self.call("alpha")
        self.assertEqual(out["members"][0]["state"], "quarantined-for-review")

    def test_a_member_without_a_valid_address_is_skipped_and_counted(self):
        self.write_roster([member("alpha"), {"state": "admitted"}, {"address": "Bob <b@x.y>"}])
        out, _ = self.call("")
        self.assertEqual(self.addresses(out), [f"alpha@{DOMAIN}"])
        self.assertEqual(out["skipped_entries"], 2)

    # --- self comes from the deployment, never from the roster -------------------

    def test_self_is_flagged_only_when_the_deployment_names_it(self):
        self.write_roster([member("me-1a2b"), member("you-3c4d")])
        out, _ = self.call("", AMAP_SELF=f"me-1a2b@{DOMAIN}")
        flagged = [m["address"] for m in out["members"] if m.get("self")]
        self.assertEqual(flagged, [f"me-1a2b@{DOMAIN}"])

    def test_without_amap_self_nothing_is_flagged(self):
        self.write_roster([member("me-1a2b"), member("you-3c4d")])
        out, _ = self.call("")
        self.assertFalse(any("self" in m for m in out["members"]))

    def test_a_malformed_amap_self_is_an_error_naming_it(self):
        self.write_roster([member("me-1a2b")])
        out, err = self.call("", AMAP_SELF="Me <me@x.y>")
        self.assertIsNone(out)
        self.assertIn("AMAP_SELF", err)


class PeersServerContract(unittest.TestCase):
    """What surrounds the tool: its description, and the server staying usable
    and protocol-clean when the roster is not configured at all."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="peers-srv-"))
        (self.tmp / "outbox").mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("AMAP_ROSTER_DIR", "AMAP_SELF", "MAILBOX_ROOT_DIR")}
        self.env["OUTBOX_DIR"] = str(self.tmp / "outbox")

    def test_the_description_says_directory_not_allowlist(self):
        proc = _McpProc("inbox-submit", self.env, extra_args=["mcp"])
        try:
            proc.initialize()
            tools = proc.request("tools/list")["result"]["tools"]
        finally:
            proc.close()
        peers = next(t for t in tools if t["name"] == "peers")
        self.assertIn("NOT AN ALLOWLIST", peers["description"])
        self.assertIn("never pick", peers["description"])

    def test_submit_still_works_with_no_roster_configured(self):
        proc = _McpProc("inbox-submit", self.env, extra_args=["mcp"])
        try:
            proc.initialize()
            resp = proc.request("tools/call", {"name": "submit", "arguments": {
                "to": [f"peer@{DOMAIN}"], "body_text": "hi"}})
        finally:
            proc.close()
        self.assertFalse(resp["result"].get("isError"), resp)

    def test_stdout_stays_json_rpc_on_the_error_path(self):
        """The helper drops non-JSON stdout lines silently, so purity is checked
        on the raw stream instead."""
        reqs = "\n".join(json.dumps(r) for r in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "peers", "arguments": {"query": "x"}}},
        ]) + "\n"
        p = subprocess.run([sys.executable, str(REPO / "bin" / "inbox-submit"), "mcp"],
                           input=reqs, env=self.env, capture_output=True, text=True, timeout=15)
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        for l in lines:
            json.loads(l)


class _Drop:
    pass


_DROP = _Drop()


if __name__ == "__main__":
    unittest.main()
