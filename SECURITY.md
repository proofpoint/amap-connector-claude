# Security Policy

## Reporting a vulnerability

Report security issues to **resero-labs@proofpoint.com**.

Please do not open a public issue or pull request for a security problem, and
please do not include a working exploit in the first message — a description of
the class of problem and how to reach it is enough to start.

Useful to include, if you have it:

- what an attacker controls, and where that input enters
- which of the two sides of the seam you believe is affected (see below)
- the version or commit you looked at
- anything you already tried that did *not* work, which is often the fastest
  way to tell a real finding from a near miss

## What this component is, and what it is not

This repository is the **connector** — the untrusted half of a mailbox seam. It
runs inside an agent's sandbox. It holds no mail credentials, makes no network
calls, and has no send capability. Outbound mail is written as an inert
drop-box request that a host-side relay picks up and applies policy to.

A **runtime** on the other side of the seam holds the credentials and does the
sending. That is a different codebase. If a report concerns policy decisions,
recipient allowlists, or anything that actually transmits mail, it probably
belongs to the runtime rather than here — but send it anyway and we will route
it; a misrouted report is much better than an unsent one.

## Properties this component is meant to have

These are the invariants a report is most usefully measured against. If you can
break one, that is a security bug:

- **No credential is ever read or written by this code**, and none belongs in a
  sandbox that runs it.
- **Attachment resolution cannot escape its message.** A path is recomputed from
  the resolved spool key and the requested index, then containment-, symlink-,
  link-count- and digest-checked. An attacker-controlled filename is never a
  path component, and a forged or cross-message reference is inert.
- **A grant is never dereferenced unasked.** The presence of a content
  reference is permission to fetch, never a requirement, and the read paths do
  not resolve one.
- **Injected content is framed as untrusted.** A message body is never sent as
  the whole content string; it is wrapped so that an envelope-shaped body is
  just text.
- **stdout stays protocol-pure** on every path, including startup failure.
- **Exactly one consumer per notice directory**, enforced by a claim. Two
  consumers racing one directory lose messages silently, which is worse than
  delivering twice.

## What is not a vulnerability here

- **Attachment content is unscanned.** There is no DLP or anti-virus screener in
  this path and the code does not claim one. Attachment bytes are untrusted data
  to the agent, exactly like a fetched web page. That is documented behaviour,
  not a gap to report — though a case where the code *implies* screening that
  does not happen is worth reporting, because that is a misleading invariant.
- **Message content is untrusted by design.** Sender names, subjects and bodies
  are text the sender chose. Prompt-injection *content* arriving in a mailbox is
  expected; a path by which such content is treated as instruction rather than
  data is not, and is worth a report.
- **Anything requiring write access to the read-only mounts.** The notice trees
  are mounted read-only into the sandbox; an attack that presumes the agent can
  write there is presuming a broken deployment rather than a flaw in this code.

## Disclosure

We will confirm receipt and tell you what we think the issue is and whether it
lands here or on the runtime side. We would rather hear about something small
and real than about something large and speculative, and we would much rather
hear about it privately first.
