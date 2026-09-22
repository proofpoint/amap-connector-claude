# Contributing

Thanks for looking at this. This connector has a few constraints that are
unusual enough that a contribution can be good work and still be out of scope,
so please read this before opening a PR — the point is to save you the effort,
not to fence you out.

## Zero dependencies is a hard constraint, not a preference

This code runs inside an agent sandbox with no install step. The tools execute
straight off `bin/` from a checkout. **A change that adds a third-party import
cannot be merged, regardless of how much better the library is.**

CI enforces this by having no install step — its *absence* is the check. That
means a PR adding a dependency fails with an import error that names the
symptom rather than the rule, which is why the rule is written here.

If you need something a dependency would give you, open an issue first and
describe the problem rather than the library. Sometimes the answer is a dozen
lines of stdlib; sometimes the answer is that the feature belongs on the
trusted side of the seam, which is a different codebase entirely.

## Stdlib-only extends to the tests

The suite is `unittest`, not pytest, deliberately: a bare checkout has no
pytest, and the tests have to run in the same environment as the tools. pytest
can collect the suite if you happen to have it, but nothing here may depend on
it. Please don't convert the suite, and please don't add a fixture library.

```sh
python3 -m unittest discover -s tests -q
```

## A green run with skips is not a green run

An isolated checkout reports:

```
Ran 103 tests ... OK (skipped=8)
```

**Those eight skips are not incidental.** Seven are schema-conformance proofs
that validate the documents this suite writes against the specification's own
validator, loaded from a sibling `amap-spec` checkout. The eighth is the gate
that turns their absence into a failure when you ask it to. Without that
checkout they skip, and the run says nothing about schema conformance while
still printing `OK`.

So: **check for `s` in the output before believing the schemas were pinned.**
CI pins the count at exactly 8 and fails if it moves, so a new skip cannot hide
inside the same green tick. If you add a legitimately-skipped test you will need
to change that number deliberately — that is the point, not an obstacle.

## Mutation testing is the review standard

Every guard in this repo is expected to have been proved the same way: **break
it in the source, watch the NAMED test go red, restore it.** Not "the suite went
red" — the specific test that claims to cover that guard.

A PR that adds a guard without that evidence is incomplete. Say in the PR which
test you broke it against and what the failure looked like. This is not
ceremony: the repo has two recorded cases of tests that passed against
behaviour that did not exist, and one guard that could never pass and therefore
never failed.

## Three of the four tools have no `.py` suffix

```
bin/inbox-mcp-vol      bin/inbox-submit      bin/inbox-delivery      bin/_inboxlib.py
```

They are shebang'd executables. Any linter, formatter or type checker driven by
file extension will silently skip three of them and report success. If you add
tooling, name all four explicitly and check that its output mentions all four.

## stdout is protocol

The MCP servers speak JSON-RPC on stdout. A stray `print()` is wire corruption,
not a log line — including on error paths, including during startup failure.
Diagnostics go to stderr. This holds under every error condition.

## The reasoning behind a guard may not be in the repository

The design record — the architecture, the decisions and their measurements, and
the designs that were rejected — is held privately and is not distributed with
this source. The code cites rulings by number, and those numbers index that
record.

This means you may find a guard whose justification you cannot read. That is a
known and accepted state, not an oversight. If a guard looks wrong or
unnecessary, please open an issue and ask rather than removing it — the answer
usually exists, it just isn't in the tree.

## Practical

- Keep changes single-purpose. This is a security-boundary component, and a
  diff that mixes a fix with a refactor is much harder to review as one.
- Match the surrounding style rather than a general convention.
- If you change behaviour the docs describe, change the docs in the same PR.
- Security issues do **not** go in a PR or a public issue — see
  [SECURITY.md](SECURITY.md).
