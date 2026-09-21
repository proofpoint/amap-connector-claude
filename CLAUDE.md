# CLAUDE.md — amap-connector-claude

## What this repo is

The **connector** half of the Agent Mailbox Protocol seam — "Body 1" in the
family's role taxonomy — implemented for Claude Code. *Body names a ROLE
relative to the trust boundary, not a repository*: several connectors exist for
different host agents, and the role is what they share.

The connector is the **untrusted** side. It runs inside the agent's sandbox. It
holds no mail credentials, makes no network calls, and has no send capability.
Everything here is stdlib-only, zero-dependency, single-file and shebang'd —
the four tools run straight off `bin/` from a checkout. `pyproject.toml` is
optional; `pip install .` copies the same scripts verbatim via `script-files`
(no wrapper, no importable package).

```
bin/inbox-mcp-vol    MCP read     — read_message / read_attachment over a spool volume
bin/inbox-submit     MCP + CLI    — writes an INERT drop-box request; a host-side
                                    relay applies policy and does the actual sending
bin/inbox-delivery   daemon       — the in-sandbox last hop into a live session over UDS
bin/_inboxlib.py     private      — the consumer claim the daemon takes on both spools
```

`.mcp.json.example` registers the **first three** as MCP servers. `inbox-delivery`
is **not** an MCP server and is not in that file: it is a long-lived daemon the
host adapter's relay chain launches and supervises inside the container.

## What this repo is NOT

- **Not a runtime.** It never decides policy, never holds a credential, never
  sends. A runtime (`amap-router-local`, a provider-pointed mail runtime, …) is
  the trusted half.
- **Not a deployment.** Installing this into sandboxes, writing the relay chain,
  rendering fleet policy — that is the *host adapter*. Pointing a runtime at a
  provider is a *provider deployment*. Neither is a body.
- **Not dependent on the workbench it was extracted from.** See "Frozen
  duplication" below. Never `import` the workbench from here.

## Publication and secrets

Proofpoint-owned; released as open source. Keep shipped text free of absolute
host paths, personal identifiers, and any internal fleet or infrastructure
identifier — use repo-relative paths and the reserved `example.com` /
`example.org` domains in tests. No credential ever belongs in this repo or in
a sandbox: outbound is an inert drop-box request that a host-side relay picks
up.

## Tests

```sh
python3 -m unittest discover -s tests -q
```

Stdlib only — there is **no pytest dependency**. The suite is `unittest.TestCase`
throughout (pytest can collect it if you have it, but nothing here needs it, and
a bare checkout has no pytest). Verified at HEAD: **103 tests, `OK (skipped=8)`,
~40 s** in a checkout with no sibling `amap-spec` (the eight skips are the schema
proofs plus their gate — see below; do not set `AMAP_REQUIRE_SPEC` unless a spec
checkout is present, or those skips become a failure). The suite is slow for its
size on purpose — `test_inbox_delivery.py` starts `bin/inbox-delivery` as a real
subprocess against temp trees, a fake session-source script and a fake UDS
receiver, and asserts only on what lands on disk or on the wire. Nothing there
imports the daemon.

`tests/test_peer_notice_schema.py` validates the documents the suite writes
against **the spec's own** `fixtures/validate.py`, loaded by path from a sibling
`amap-spec` checkout found by walking up from `tests/`. If that
checkout is absent the test **skips, loudly** — *a skipped validation is not a
passed one*; check for `s` in the output before believing the schema is pinned.

Real injection into a real session is `NOT-FOR-PUBLICATION/spikes/uds_inject_spike.py`, run by hand.
It is not a unit test and must not become one.

## Conventions a newcomer would otherwise violate

### Mutation testing — a test that cannot fail is worse than none

Every guard gets proved: **break it in the source, watch the NAMED test go red,
restore it.** Not "the suite went red" — the specific test that claims to cover
it. The design record counts them ("24 guards mutation-proved", "six mutants
red") because the count is the claim.

This repo earned the rule the hard way, twice, both recorded in its own text:

- **The receipt suite could not fail.** `tests/test_inbox_delivery.py`'s own
  header: the first version scripted receipts on the *inbound* connection — a
  channel the real receiver never writes on — and **"sixteen tests passed
  against a protocol that does not exist. Never again."** The fake receiver now
  behaves the way the real one was *measured* to behave (silent under `accept`;
  under `refuse` the inbound connection stalls open and the refusal arrives
  ~1 ms later at the reply socket the sender bound).
- **A vacuous cleanup test.** Recorded in `NOT-FOR-PUBLICATION/RULINGS.md` (ruling 15, adapter
  round 5): it made a directory unwritable and asserted no temp
  file was left — but an unwritable directory never lets one be created, so the
  cleanup path was never reached and the mutation stayed **GREEN**.

Verified while writing this file: disabling the "sender is the router" guard in
`_admit` turns
`tests/test_inbox_delivery.py::PeerLaneTest::test_a_notice_from_the_router_itself_is_refused`
red, and green again on restore.

### The inverse: a check that CANNOT PASS — the worked example

A guard that is always closed produces no red test either, and is worse,
because it looks like correct fail-closed behaviour.

`_admit` used to read the recipient off the **notice**:

```python
frm, to = msg.get("from"), msg.get("to")   # msg = notice["message"]
...
if to != me:
    return self._refuse(notice_id, f"message.to is not self ({me})")
```

But the router builds a notice's `message` object with `id` / `from` /
`subject` / `preview` / `mailbox` only. `to` is **not** there and never was. So
`msg.get("to")` was always `None`, `None != me` was always true, and **every
peer notice was refused, for the whole life of the lane** — silently, fail-
closed. Worse, the sender got a DSN whose generic prose blamed an allowlist
that ruling 16 had already deleted, so the symptom pointed away from the cause.

`to` lives on the **message spool doc** (`inbound-message.schema.json`), which
`_admit` already opens one line later for `body_text`. The check itself was
right and stays; only the document it reads was wrong.

**Say "the router does not write it there" — never "the schema forbids it".**
The schemas for runtime-authored documents (`deliver-notice`, `peer-notice`,
`inbound-message`) are **OPEN**: `additionalProperties` is absent at every
level, which is §7's v3.0.0 tolerance rule, and
`fixtures/valid/notice-unknown-member.json` pins it. They were closed at
v2.3.0 and were deliberately opened after a strict v2.0.0 connector silently
dropped whole notices when `provenance` was added. Only `submit-request`
(agent-authored) and `binding-record` stay closed. So a runtime that *wrongly*
emitted `message.to` on a notice would pass the conformance gate **clean**.
The schema makes this class of mistake **invisible, not impossible.**

**Three lessons, all durable:**

1. **Know which document carries which field**, and write it down — because
   the wire contract will not tell you. One placement writes two documents.
   The notice is the *arrival signal* — id, from, subject, preview, mailbox,
   and the profile's optional members. The message spool doc is the *content*
   — `to`, `date`, `body_text`, attachment descriptors with `content_ref`.
   They are not two views of one record.
2. **Pin the SOURCE of a field, not just its value.** A test asserting that
   `to` has the right value passes against a document that carries `to` in the
   wrong place. The assertion that would have caught this names which document
   the value came out of.
3. **The suite could not catch it, and you should see why.** The tests mint
   their own notices (`peer_notice_docs`), and that fixture put `to` on the
   notice. A fixture that models the producer *as you imagine it* tests your
   imagination — and an open envelope will not correct you. When you write a
   fixture for a document another repo produces, check it against that repo's
   writer: here, the runtime's `deliver.py:_message_and_notice`.

### Two lanes, different treatment — and the difference is the design

| tree | what the daemon does | outcome file |
|---|---|---|
| **mail** (`inbox`) | a **content-free doorbell**: "you have mail; use the inbox read tool". No field of the notice enters the injected string. Bodies never cross this lane. | none |
| **peer** (delegation) | notice + body injected as a teammate's request | one per outcome |

Both trees are **read-only mounts**. The daemon never creates, moves, renames
or deletes anything under them — there is no `processed/` anywhere.
At-most-once rests on one record: the private delivered ledger under
`AMAP_DELIVERY_STATE_DIR`, keyed `(tree, notice_id)` because notice ids are only
unique per tree. It is written **before** the delivered outcome, survives
restarts, and is never derived from outcome files.

### Ruling 16 — this daemon holds no allowlist

Peer-lane authorisation is the **router's**, evidenced by the read-only mount:
a notice is in a tree only the host can write, so the router put it there.
`AMAP_DELIVERY_PEERS` (formerly `AMP_DELIVERY_ALLOWED_SENDERS`) names the
*informational* `peers.json`, and the daemon reads **exactly one field, `self`**
— the doorbell's from-name, and the peer lane's `to == self` integrity check.
It is not an allowlist and nothing else in the file is consulted.

Two measurements retired the old one: the file lived on the read-write mount so
the agent could rewrite it (it could never bind the agent), and the router never
consulted it (it could never bind the router). What it *did* do was make every
graph edit a re-provision plus a daemon restart. **Do not reintroduce a second
allowlist here.** The two integrity checks that remain are not second opinions
on policy: `to == self` (a mismatch is a router fault, and injecting another
agent's task is wrong whoever authorised it) and "the sender is not the router"
(the router never tasks; its notices arrive on the mail lane as status).

### The daemon owns the envelope

Claude Code parses the sender's identity and class claim out of the **content
string** with an anchored regex. So a body is never sent as the whole content —
every injected string is wrapped:

```
<cross-session-message from-name="<addr-spec>">\n<body>\n</cross-session-message>
```

which makes an envelope-shaped body just text, since the regex admits one
envelope. **`from-mode` is NEVER set**: claiming `bypass` would assert a class
this process does not have, and would turn a missing receiver setting into a
silent success instead of the `held` outcome that says the sandbox is
misconfigured.

### Silence is delivery, and is never retried

Under `accept` the receiver emits no receipt on any channel. So silence within
`AMAP_DELIVERY_RECEIPT_WINDOW_SECONDS` (**the one optional env var**, default 30)
is `delivered`, recorded with a `detail` that says exactly that it is an
inference. **Never retry on silence** — a retry under `accept` delivers the task
again; that was the bug ruling 14 fixed. Negative receipts are the observable
ones, and they arrive **out of band**: the receiver connects *outward* to a
reply socket this daemon binds before sending and advertises as
`from: "uds:<path>"`, one per in-flight message, with a basename the receiver
accepts. The per-message socket is the correlation — receipts carry no
`orig_msg_id`.

A `held` receipt **does not** mean a human will decide, and the wire cannot tell
you which kind of hold it is: the receiver has eight causes and six of them are
setting- or policy-caused, yet all return the same generic approval-parity
string. So `_held_detail()` quotes the receiver verbatim and says the cause is
not knowable from here. **Never assert a cause you cannot observe.**

### Outcome files are write-only, and the router unlinks them

One file per outcome under `AMAP_DELIVERY_OUTCOME_DIR`, named
`<tree>-<notice_id>.json`, with **exactly** the keys `outcome`, `ts`, `tree`,
`notice_id`, and optionally `detail` — no others. Written `.{name}.tmp` then
`os.rename`; the leading dot and `.tmp` suffix matter, because the router reads
only names matching `^peer-[a-f0-9]{32}\.json$`. Never `os.link` (the router
requires `nlink == 1`). **The router unlinks each file after reading it:** never
assume a previous outcome is still there, never read outcomes back, and on a
later transition write the same name again.

### Config fails loud, never guesses

Each tool resolves its directory from a per-tool env var, else
`$MAILBOX_ROOT_DIR/<subdir>`, else **one line to stderr naming the missing var
and a nonzero exit**, checked once at startup. Never a `__file__`-relative
guess, never a cwd fallback. This is the one behaviour change from the in-tree
originals, whose `<repo>/.amp/...` default silently pointed at an empty spool
once extracted. The daemon's eight variables are all **required, no
defaults**; `AMAP_DELIVERY_SELF` is a ninth that is optional because a fleet
with no peer lane has no address to name, and malformed-but-present still
dies. The daemon reads no policy file — `AMAP_DELIVERY_PEERS` and the
`peers.json` reader both went when `self` became a variable.

### Exactly one consumer per notice directory

`bin/_inboxlib.py` holds the consumer claim and nothing else. Two consumers
watching one directory race and the loser's notices are **silently lost** —
strictly worse than delivering twice, and invisible. The module raises and never
logs, exits or writes stdout: what a refusal means is the caller's policy, and
the daemon's is to exit nonzero on *any* claim failure (for a daemon there is
no "continue without the interlock"). Staleness errs toward LIVE — only a
definitively-gone holder releases a claim.

### stdout is protocol, stderr is speech

All three MCP servers keep stdout JSON-RPC-pure under **every** error condition,
including the config fail-loud path. The daemon writes everything to stderr and
never uses stdout. A stray `print()` is a wire corruption, not a log line.

### Never dereference a grant you were not asked to

`inbox-mcp-vol` is the reference **consumer** for AMAP §5's tolerance obligation:
`content_ref`'s presence is a grant, never a fetch requirement. `read_message` /
`list_messages` never resolve one. `read_attachment` resolves only a path it
recomputes itself from the resolved spool key plus the requested 0-based index,
then containment/symlink/nlink/sha256-checks it — an attacker-controlled
`filename` is never a path component, and a forged or cross-message
`content_ref` is inert. An unresolved grant is a clean `isError`, never a crash.

### Never import the workbench — and there is no longer a twin to sync with

`inbox-submit` carries about 40 lines that **originated** in the workbench's
JMAP core: the submit-request builder, the monotonic `req_id` derivation, and
the atomic `.tmp` + `os.replace` write. They were duplicated rather than
imported because a standalone connector cannot depend on the repo it was
extracted from — a rule that stands, and stands harder now this repo is
published and the workbench is not. Do not "fix" it by adding an import.

**There is no second copy.** `lib/inboxlab_core.py`, `mail-cli` and
`bin/inbox-mcp` were deleted when the lab moved off Fastmail JMAP
(2026-09-17); the workbench kept its git history and nothing else. So
**frozen** no longer means "keep it in step with a twin" — it means don't grow
the surface. The `agent_id` omission is this code's behaviour, not
a divergence from a living copy.

### Git

**Commit only when asked.** The co-author trailer is the running model. This is
an independent git repo *symlinked* into a workbench — never `git add` it from
there, and never commit sibling repos from here. Concurrent agents may share
this worktree: judge a sha, not the tree.

## Where the "why" lives

This repo's design record is large and is the point — it holds decisions and
their measurements, not just outcomes. Read the relevant one before changing
behaviour:

| file | what it records |
|---|---|
| `NOT-FOR-PUBLICATION/delivery_design.md` | the architecture of the delivery daemon and the peer lane; §10 is the daemon's contract, §10.z rulings 15 and 16 in full |
| `NOT-FOR-PUBLICATION/RULINGS.md` | **every decision with its rationale and where it is implemented**: rulings 1–16 (14 = the receipt model; 15 = the wrapper does host-variable translation; 16 = the router authorises), the counterparts' corrections, the router's pushbacks, the six sandy asks. The round-trip correspondence that produced them was folded in and retired 2026-09-15 |
| `NOT-FOR-PUBLICATION/uds_findings.md` | the measured UDS wire: framing, the anchored envelope regex (§5.1), out-of-band receipts (§8) |
| `NOT-FOR-PUBLICATION/alternatives_considered.md`, `channel_roadmap.md` | rejected designs and what is deliberately not built |

Rulings are cited by number throughout the source. When source and a design
record disagree, that is a finding — report it, do not pick one silently.

## Non-obvious things the code does not say

- **`inbox-delivery` is the only Claude-Code-specific tool here** (it speaks the
  session's UDS messaging socket). `inbox-mcp-vol` and `inbox-submit` are
  generic and any MCP-capable runtime could use them as-is. A `common/` package is noted in `README.md` as *not started* — one
  consumer does not justify it.
- **Attachment handling is the relay's, and is NOT gated on a human here.** On
  the peer lane, attachment-bearing submits measured as `accepted` with no human
  step — 9 independent observations, 2026-09-21. **Mail-lane behaviour is
  untested** (this fleet declares no mail lane). No DLP/AV screener exists in
  this path; do not describe one. The connector only writes the request.
- **Write ordering is the commit sentinel on the submit side too**: every
  sidecar is fully written before `req-<id>.json` is, because the JSON's presence
  is the only publish signal. A crash mid-attachment never half-publishes.

## History — `inbox-channel` (retired 2026-09-15)

`bin/inbox-channel` was this package's first §4 delivery mechanism: a one-way
`claude/channel` MCP server that watched the mail spool and pushed a
`notifications/claude/channel` event per `deliver` notice. It needed the
`--dangerously-load-development-channels` launch flag, could carry no
content, and had no way to reach a peer tree. `bin/inbox-delivery` replaced
it (2026-09-03) with injection over the session's own socket: content-free
doorbells for mail, the peer's request for delegation. The two could never
run together — the channel started its watcher before the stdio loop, so
merely registering it made a second consumer of the spool, and the loser's
notices were silently lost; the consumer claim in `_inboxlib.py` (born in the
channel) is what made that loud. Channel mode was retired from the fleet on
2026-09-10 (`NOT-FOR-PUBLICATION/RULINGS.md` ruling 5) and the binary, its tests and the
conformance harness's channel tests were deleted on 2026-09-15 (inbox-lab
janitorial #11). Git history before that date has the file.
