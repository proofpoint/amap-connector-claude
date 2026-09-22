# claude-code — reference connector

Standalone, zero-dependency implementation of the Claude Code connector
described in the connector interface specification, speaking the
seam in `amap-spec`. Two MCP servers the agent talks to, one
daemon the host runs beside the session; no install step required.

```
bin/inbox-mcp-vol     §5 message read      — MCP read_message/read_attachment over a spool volume
bin/inbox-submit      §6 outbound submit   — MCP `submit`/`submit_result` tools
                      (`inbox-submit mcp`) + an unchanged CLI front-end
                      (`inbox-submit submit`/`submit-result`); writes a
                      drop-box request — a host-side relay (not part of this
                      connector) applies policy and actually sends
bin/inbox-delivery    §4 inbound delivery  — the last hop: injects a content-free
                      doorbell (mail) or a peer's request (delegation) into the
                      live session over its own messaging socket; NOT an MCP
                      server — the host runs it beside the session
bin/_inboxlib.py      private               — the consumer claim the daemon takes
```

All of them are stdlib-only, single-file, shebang'd, `chmod +x` executables —
run them straight from `bin/` via PATH or a bare-relative path. `pyproject.toml`
is optional: `pip install .` copies the same scripts verbatim into the
environment (via setuptools `script-files`, not a wrapped console-script —
there's nothing to wrap, they're already standalone).

## Config — the only thing you set

Each MCP server needs to know where the shared spool volume is. Resolution
order, identical for both:

1. A per-tool env var naming its exact dir — wins if set:
   - `INBOX_MESSAGE_DIR` (inbox-mcp-vol)
   - `OUTBOX_DIR` (inbox-submit)
2. Else `$MAILBOX_ROOT_DIR/messages` / `$MAILBOX_ROOT_DIR/dropbox` respectively.
3. Else the process **fails loud**: a one-line message to stderr naming the
   missing var, nonzero exit, checked once at startup — never a silent
   `__file__`-relative guess, never a silent cwd fallback, and never
   something that leaks onto stdout (both MCP servers keep stdout
   protocol-pure even on this failure).

Whichever value is used, `~` and `$VAR` in it are expanded by the tool. That
is not a default and not a guess — the value was supplied; this only spells it
the way a shell would.

**If you are registering with Claude Code, use `${HOME}`, not `~`.** Claude
Code expands `${VAR}` in both `command` and `env` before spawning the server,
and does *not* expand `~`: a `command` of `~/…/inbox-mcp-vol` is spawned
literally and fails ENOENT. `command` is resolved by the host before this code
runs, so the expansion above can never help there — it applies to `env` values
only. (Measured on a live sandbox, 2026-09-17, by the sandy adapter: the
`${HOME}` registration connected, the `~` twin did not.)

The tool-side expansion is therefore belt-and-braces under Claude Code, and
load-bearing anywhere else — an SDK-built registration, a hand-edited
`.mcp.json`, or another MCP host need not expand anything, and without it every
value has to be an absolute path baked in per deployment.

`inbox-mcp-vol` also reads **`INBOX_LANE`** — `mail` (the default) or `peer`.
It selects the `instructions` string the server hands the host at startup, and
nothing else. The binary is registered twice in a delegation deployment, once
per spool, and the two lanes have opposite policies: mail is untrusted content
to be reported rather than acted on, while a delegation that reached the
read-only peer tree was authorised by the runtime and is meant to be acted on.
An unrecognised value fails loud rather than falling back, because a typo that
silently served mail instructions on the peer lane is exactly the defect the
variable exists to prevent. What does *not* change between lanes: material the
sender quoted rather than wrote carries no authority, and attachment bytes are
unscanned on both.

`inbox-delivery` is configured differently and deliberately so: eight explicit
`AMAP_DELIVERY_*` variables, every one required, no root variable to derive
from — it owns two spools and two claims, and a missing variable must fail
loud. Its module docstring lists them; under sandy the adapter's `relay.sh`
derives them from sandy's own exports.

One more is optional: `AMAP_DELIVERY_SELF`, this agent's own address, used for
the mail doorbell's from-name and the peer lane's `to == self` integrity
check. Absent is a supported deployment — a fleet with no fleet domain has no
peer lane and no address to name — so the doorbell falls back to a constant
and the peer lane refuses, naming the variable in the outcome's `detail`.
Present but malformed still dies at startup: a misspelling is not an omission.
**The daemon reads no policy file at all**; `self` used to come from the
adapter's `peers.json` and that reader is gone.

This is the one behavior change from the in-tree (inbox-lab) originals,
which defaulted to `<repo>/.amp/...` relative to their own file location —
a default that only made sense symlinked inside inbox-lab, and silently
pointed at the wrong (or an empty) spool once extracted.

## Registering with Claude Code / sandy

Ship-and-`cp`: **[`.mcp.json.example`](.mcp.json.example)** is a ready registration
of both servers (`inbox`, `inbox-submit`), using
workspace-root-relative paths so it works verbatim once two conventions hold
(the runbooks standardize them):

1. a symlink `amap-connector-claude` → this repo, at the workspace root;
2. the shared `.amp` volume at the workspace root.

```sh
cd <workspace>
ln -s /path/to/amap-connector-claude amap-connector-claude   # convention (1)
cp amap-connector-claude/.mcp.json.example .mcp.json
sandy                                     # .mcp.json auto-discovered at cwd
```

Both `command` and `MAILBOX_ROOT_DIR` resolve against the launch cwd (= the workspace
root), so run `sandy` from there. `MAILBOX_ROOT_DIR` may be relative (`.amp`, the
common case) or absolute (a workspace binding a *different* agent identity/volume
points it at that agent's volume). Nothing in `.mcp.json` pushes: a new notice
reaches the live session through `bin/inbox-delivery`, which the host runs
beside the session (under sandy, from its read-only relay slot, installed by
`amap-adapter-sandy`).

`inbox-submit` is now an MCP server too (ccc-v0.2): `inbox-submit mcp`
(registered above alongside `inbox`) exposes exactly two
tools, `submit` and `submit_result` — `submit` writes the same inert
drop-box request the CLI does and returns `{req_id, status: "queued"}`; it
is a **GATED request, not a send** — the host-side relay applies the
recipient allowlist and may hold it (`queued_for_human`); nothing is "sent"
until `submit_result` reports outcome `accepted`. There is no read tool and
no direct-send path on this server; it makes no network calls itself.

The CLI front-end is unchanged and still there for host scripting /
host-side scripting — invoke it directly:
```sh
MAILBOX_ROOT_DIR=/path/to/your/.amp bin/inbox-submit submit --to a@example.com --subject hi --body 'hello'
MAILBOX_ROOT_DIR=/path/to/your/.amp bin/inbox-submit submit-result <req_id>
```

## Delivery — `bin/inbox-delivery`

The daemon is the connector's §4 half. It watches two runtime-owned trees —
the mail tree (`inbound/notices`) and the peer tree (`peer/notices`, the AMAP
3.1.0 peer-origin profile) — and injects into exactly one live Claude Code
session over the session's own Unix-socket messaging: a **content-free
doorbell** for mail (the agent then reads with `read_message`), the **peer's
request itself** for delegation, framed as untrusted content with the
runtime-asserted sender. It takes a consumer claim on both spools before
watching and exits nonzero if either is held; keeps its delivered ledger
where no router can write; writes a per-notice outcome file
(`outbound/ext/claude-code/outcomes/`) the router reads as a
claim, never as proof; refuses when it cannot identify a single target
session; and holds no allowlist — authorisation on the peer lane is the
router's, evidenced by the read-only mount.

### Inbound attachments (amap-spec v2.3.0 §5)

`inbox-mcp-vol` exposes attachment descriptors on every `read_message`
(`filename`/`media_type`/`size_bytes`/`disposition`) — never hidden — plus a
third, opt-in tool, `read_attachment(message_id, index)`, that resolves ONE
runtime-published grant's bytes back. Per §5, `content_ref`/`sha256` appear
on a descriptor **only** when the runtime asserts `disposition == "clean"`;
everything else (`stripped`, `quarantined`, `unscanned`, or a `clean`
descriptor the runtime chose not to publish bytes for) renders with bytes
withheld, never fetchable. `disposition` is the runtime's assertion, not a
scanner verdict — this connector doesn't know or care how the runtime
reached it.

This connector is a model **consumer** for the tolerance obligation §5 adds:
`content_ref`'s presence is a grant, never a fetch requirement. Nothing in
`read_message`/`list_messages` ever dereferences it — a message with a
`content_ref` a caller never resolves relays and renders exactly like one
with none. `read_attachment`, when a caller does invoke it, resolves ONLY a
path it recomputes itself from the message's own spool key + the requested
0-based index (`<key>.attachments/<index>`) — the descriptor's own
`content_ref` string is checked against that expected form and otherwise
never parsed as a path, so an attacker-controlled `filename` is never a path
component and a hostile/forged/cross-message `content_ref` is inert (see
`read_attachment`'s docstring in `bin/inbox-mcp-vol` for the full
containment/symlink/nlink/sha256 gate). A descriptor whose bytes were never
published (no sidecar on disk) is a clean `isError`, not a crash.

### Attachments (attachments-outbound-v0.1)

`submit` carries local files into the drop-box as sidecars — CLI `--attach
PATH` (repeatable) or MCP `attachments: [{path, filename?, media_type?}]`.
For each input the connector streams the bytes to
`req-<id>.attachments/<n>` (0-based ordinal == its position in the
`draft.attachments[]` descriptor array — the sole binding authority, no
other reference exists), computing `sha256`/`size_bytes` over the decoded
bytes as written. **Write ordering is the commit sentinel:** every sidecar
is fully written (`.tmp` + `os.replace`) before `req-<id>.json` itself is
written — the JSON's presence is the only publish signal, so a crash
mid-attachment never yields a half-published request. Per-file cap
25 MiB (override `$INBOX_ATTACH_MAX_BYTES`), up to 16 files per request;
bad input (missing file, non-regular file, over-cap) fails before anything
touches the drop-box.

```sh
MAILBOX_ROOT_DIR=/path/to/your/.amp bin/inbox-submit submit --to a@example.com \
  --subject "see attached" --body 'hello' --attach ./report.pdf --attach ./notes.txt
```

**Attachment handling is the relay's, and you should not assume a human sees
it.** On the peer lane, attachment-bearing requests have been measured as
`accepted` and delivered with no human step (9 independent observations,
2026-09-21); mail-lane behaviour is untested. Nothing in this path screens
attachment content — there is no DLP/AV screener here, so do not describe one.
Treat the submitting agent as the last check, and call `submit_result` for the
outcome: the status returned by `submit` is a receipt, not a verdict. See
`amap-spec/spec/attachments-v0.1.md`.

## Provenance / deliberate duplication

Extracted from inbox-lab (ccc-v0.1) where these lived as `bin/inbox-mcp-vol`
and `mail-cli`'s `submit`/`submit-result` subcommands (a one-way channel
shim, `bin/inbox-channel`, came along too and was retired 2026-09-15 — see
`CLAUDE.md`, History).

`inbox-submit` carries about 40 lines that originated in the workbench's JMAP
core (`lib/inboxlab_core.py`): the submit-request builder, the monotonic
`req_id` derivation (max over pending + processed + results files), and the
atomic `.tmp` + `os.replace` write. They were duplicated rather than imported
because a standalone connector can't depend on the workbench it was extracted
from — a rule that still holds, and holds harder now the connector is
published and the workbench is not.

**There is no second copy.** That core, `mail-cli` and `bin/inbox-mcp` were
deleted when the lab moved off Fastmail JMAP (2026-09-17); the workbench kept
its git history and nothing else. So this is the single copy, "duplication"
names only where the code came from, and the `agent_id` omission is
simply this code's behaviour rather than a divergence from a living twin.

## Future factoring (not built yet)

`inbox-mcp-vol` and `inbox-submit` are generic (any MCP-capable agent runtime
could use them as-is); `inbox-delivery` is Claude-Code-specific (it speaks the
session's UDS messaging socket). A future
`common/` package could hold the first two once a second connector
(`codex/`, `gemini-cli/`, …) actually needs them. Not worth building for one
consumer — noted here so it isn't forgotten, not started.

## Security posture

- No mail credentials anywhere in this package; no network I/O anywhere in
  this package (`inbox-mcp-vol` is a pure local file read, `inbox-submit` a
  pure local file write, `inbox-delivery` reads local spools and speaks only
  to a session socket on the same host). Outbound send
  capability lives entirely on the host-side relay this package hands
  requests to.
- `inbox-mcp-vol`'s `read_message` id is basename-only, charset-restricted,
  and realpath-containment-checked after resolution — traversal-proof even
  against a symlinked spool entry (verified M6b).
- `inbox-mcp-vol`'s `read_attachment` (amap-spec v2.3.0 §5)
  never treats a descriptor's own `filename` or `content_ref` as a path — the
  sidecar path is always recomputed from the resolved spool key + the
  requested index, then containment/symlink/nlink/sha256-checked before any
  byte is returned; only a `disposition == "clean"` descriptor with an
  exact-form, runtime-published `content_ref` is ever readable, and an
  unresolved/hostile/forged grant is a clean `isError`.
- `inbox-delivery` frames everything it injects as untrusted content with the
  runtime-asserted sender, sanitizes the from/subject fields it uses (control
  chars/newlines stripped, length-bounded), injects only `kind == "peer"`
  notices from the peer tree and content-free doorbells for mail, and refuses
  a notice from the router's own address or one not addressed to itself.
- `inbox-submit`'s MCP `submit_result` req_id gets the identical guard
  (basename-only, charset-restricted, realpath-containment-checked within
  `results/`) — a hostile req_id is a clean `isError`, never a read outside
  `results/`. `submit` itself only ever writes an inert request file: no
  socket/urllib/smtplib/imaplib import anywhere in the file, no mail-cred
  env var read.
- Both MCP servers keep stdout protocol-pure (JSON-RPC lines only)
  under every error condition, including the config fail-loud path.
