"""tests/test_version_refusal.py — the fail-closed version rule on message docs.

Draft §12.1: a consumer reads contract_version before anything else and
compares majors as non-negative decimal integers; "an absent major, a lower
major, or a higher major is a version refusal: fail closed, as a distinct,
logged outcome". The reader used to refuse only a higher major. Stdlib only.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
_ro = SourceFileLoader("_ro_for_version", str(HERE / "test_readonly_inbound.py")).load_module()
_McpProc = _ro._McpProc

NID = "1-1"
_DROP = object()


class VersionRefusal(unittest.TestCase):
    def setUp(self):
        self.messages = Path(tempfile.mkdtemp(prefix="ver-")) / "messages"
        self.messages.mkdir()

    def write(self, cv):
        doc = {"notice_id": NID, "from": "alice@example.org", "to": "agent@example.org",
               "subject": "hello", "body_text": "BODY"}
        if cv is not _DROP:
            doc["contract_version"] = cv
        (self.messages / f"notice-{NID}.json").write_text(json.dumps(doc))

    def call(self, tool, args):
        env = {k: v for k, v in os.environ.items() if k != "MAILBOX_ROOT_DIR"}
        env["INBOX_MESSAGE_DIR"] = str(self.messages)
        proc = _McpProc("inbox-mcp-vol", env)
        try:
            proc.initialize()
            r = proc.request("tools/call", {"name": tool, "arguments": args})
            time.sleep(0.2)                       # let the stderr reader catch up
            return r["result"], "\n".join(proc._err_lines)
        finally:
            proc.close()

    def refused(self, cv, fragment):
        self.write(cv)
        result, err = self.call("read_message", {"id": NID})
        self.assertTrue(result.get("isError"), result)
        text = result["content"][0]["text"]
        self.assertIn("version refusal", text)
        self.assertIn(fragment, text)
        self.assertNotIn("BODY", text)
        self.assertIn("version refusal", err, "§12.1: the refusal is LOGGED")

    def test_an_absent_version_is_refused(self):
        self.refused(_DROP, "no contract_version")

    def test_a_lower_major_is_refused(self):
        self.refused("1", "older major")

    def test_a_higher_major_is_refused(self):
        self.refused("3", "newer major")

    def test_a_non_numeric_version_is_refused(self):
        self.refused("two", "not a major version")

    def test_a_non_string_version_is_refused(self):
        self.refused(2, "not a major version")

    def test_the_current_major_reads(self):
        self.write("2")
        result, err = self.call("read_message", {"id": NID})
        self.assertFalse(result.get("isError"), result)
        self.assertIn("BODY", result["content"][0]["text"])
        self.assertNotIn("version refusal", err)

    def test_the_refusal_is_distinct_from_a_malformed_document(self):
        (self.messages / f"notice-{NID}.json").write_text("{not json")
        result, _ = self.call("read_message", {"id": NID})
        self.assertTrue(result.get("isError"))
        self.assertNotIn("version refusal", result["content"][0]["text"])

    def test_list_messages_shows_the_refusal_and_not_the_message(self):
        self.write("3")
        result, _ = self.call("list_messages", {})
        text = result["content"][0]["text"]
        self.assertIn("version refusal", text)
        self.assertNotIn("hello", text)


if __name__ == "__main__":
    unittest.main()
