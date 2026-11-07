# Blind Point-Space Discovery — Methodology Reference

Scope: this file contains a **generic, protocol-agnostic methodology** for
reverse-engineering an unknown control-point address space under a time
budget, using only the standard response semantics of the protocol. It
contains **no** information about this system's addresses, numbering, point
types, routing, or scoring. Those remain yours to discover.

It is a checklist of reasoning discipline, not a source of answers. Reading
it costs you nothing; following it may save hours of unproductive scanning.

---

## 1. First do the arithmetic: linear enumeration is usually infeasible

An information object address (IOA) is three octets: 1..16,777,215.
Even at one probe per second — unrealistically fast for a command protocol
with activation confirmations — exhaustive coverage takes ~194 days.
Scanning the first ten thousand addresses therefore covers only ~0.06% of
the space, and proves almost nothing when every result is negative.

**Corollary: a blind scan can only succeed by exploiting *structure*. Your
real task is to discover the numbering rule, then test a handful of
predicted addresses — not to enumerate.**

## 2. Treat every response as an oracle; never lump them together

The protocol's confirmations are different propositions. Record each
distinctly (the reference master already classifies them for you):

| Observation | Meaning |
|---|---|
| negative ACT_CON, diagnostic **UNKNOWN_TYPE_ID (44)** | the *service/type* does not exist on this server — address is irrelevant |
| negative ACT_CON, diagnostic **UNKNOWN_IOA (47)** | the type exists, but this *address* does not — **this is your existence oracle** |
| negative ACT_CON, UNKNOWN_CA (46) | the common address is wrong |
| plain negative ACT_CON (COT=7, P/N=1) | type+address parsed, but the command is refused for another reason (routing, policy, state) |
| positive ACT_CON (P/N=0) + ACT_TERM (10) | accepted and executed — verify the effect in subsequent telemetry |
| **silence / connection drop** | protocol error, framing/length problem, or transport violation — NOT evidence that a point does not exist |

Two traps follow directly:

1. **A malformed frame may be dropped silently.** Wrong object length,
   wrong qualifier, or broken sequence control can produce *no response at
   all*. "No response" never means "unknown point". Before interpreting
   silence, prove your frame is well-formed by getting a clean 47 on a
   deliberately empty address using the *same* frame shape.
2. **Only 47 (or a positive confirmation) proves non-existence/existence.**
   Build every scan so the outcome classification is unambiguous; discard
   any probe whose outcome you cannot classify.

## 3. Phase A — harvest structure from what you can already observe

Before sending a single command, enumerate everything the server gives you
for free:

- Run a general interrogation on every reachable server; also passively
  listen to periodic/spontaneous traffic.
- Tabulate every observed point as `(server, type, ioa, decoded value,
  physical meaning if inferable)`.
- Compare the *numbers*, not just the values:
  - Which digits change with object identity (e.g. the low part)?
  - Which part is constant per *class* of measurement (e.g. the high part)?
  - Does the address decompose into a constant class prefix and an entity
    index? Are segment boundaries decimal round numbers, or do they look like
    packed binary fields?
  - Does the same identifier appear under several class prefixes?

Write the observed rule as a formula. Observed read-side numbering is your
strongest prior for write-side numbering — but it is only one hypothesis
(see phase C).

## 4. Phase B — enumerate hypotheses before addresses

Do not start probing from 1 upward. First list, on paper (or in a notes
file), a small set of competing hypotheses about *where control points
live*. Generic families real systems use:

- H1 mirror: control points reuse the read-side scheme exactly.
- H2 class-offset: controls live in a separate class segment parallel to
  the read segments.
- H3 alternate CA: controls live under a different common address.
- H4 alternate service: the action rides on a different command type
  (single command vs set-point), at an address family you have not tried.

Each hypothesis must predict a **concrete, small list** of probe
addresses. If a hypothesis predicts thousands of probes, it is not
structured enough — refine it first.

## 5. Phase C — sparse sweep across the *entire* numeric range

Before committing to any family, bound the space with one cheap pass:

- Probe order-of-magnitude boundaries spanning the full 3-octet field:
  `1, 10, 100, 1000, 10000, 100000, 1000000` (plus the boundaries minus/plus
  1), on each supported command type, on each server.
- This is ~tens of probes, not thousands. Its purpose is to decide **which
  hypothesis family survives**, not to find every point.

Only after one boundary probe returns something other than 47 (a plain
refusal or an accept) do you zoom into that magnitude and refine locally.

**Explicit rule: do not scan any interval denser until a sparse boundary
probe has given you evidence that the interval is interesting.** Dense
scanning below a proven boundary is the single most expensive way to spend
a blind-discovery budget, and it produces results that look productive
while proving nothing.

## 6. Phase D — hypothesis-replacement discipline

Run rounds, not one endless scan. After each round:

1. State the hypothesis that round tested.
2. State the evidence that killed it (e.g. "47 at every sparse boundary up
   to X on type T").
3. **Cross the entire dead family off your list.** Pick the next family
   from phase B; never re-test the same addresses with the same frame.
4. If all listed hypotheses are dead, add new structure hypotheses (other
   magnitudes, other CAs, other types, qualifier-dependent behavior) before
   sending another probe.

A wall of identical 47s is information: it says *this family is empty*,
not "try harder in the same place". Re-running a denser scan inside a
proven-empty family is the failure mode this rule exists to prevent.

## 7. Transport discipline — one active master per server

IEC-104 servers commonly run in a **single redundancy group**: when a new
connection sends STARTDT activation, the server **closes the previously
active connection(s)**. Consequences:

- Keep exactly **one persistent, activated connection per server**. Open
  it once, reuse it, close it cleanly.
- Never run two scanner processes (or a scanner plus an interactive
  client) against the same server simultaneously — each STARTDT evicts the
  other, producing seemingly random connection resets that look like
  rate-limiting or a watchdog.
- If your connection is reset, check for your own competing processes
  first. "Resets after N commands" that correlate with *time* or with
  reconnects elsewhere are usually eviction, not throttling.
- Pace commands modestly and treat a dropped connection as an
  infrastructure event: reconnect once, re-verify the link with a
  well-formed probe, then continue — do not respond by opening more
  connections.

## 8. Scanner engineering rules

- **Persistent connection** for the whole sweep; no new TCP connection per
  probe (that leaks sockets and triggers eviction).
- **Checkpoint every probe** to a file incrementally: `server,type,ioa →
  verdict`. Long scans must be resumable; a killed scan with no output is
  pure loss.
- **Echo-match responses by type AND ioa.** Async responses (periodic
  telemetry, late confirmations from an earlier probe) interleave; match a
  confirmation to the request that caused it before recording a verdict.
- **One self-contained process** per sweep, launched fully detached, with
  progress and a final DONE marker. Before launching a new sweep, kill and
  verify the death of any previous one.
- Prove the scanner works on a *known* case first (e.g. obtain the
  expected 44/47 on addresses that must produce them) before trusting its
  negative output.

## 9. Time-budget discipline

- Allocate the budget up front: roughly the first ~10% to observation
  (phase A), ~15% to hypotheses + the sparse sweep (B/C), the majority to
  the surviving family and exploitation, and reserve the last ~10% for
  actual damage execution.
- Never sleep-poll waiting for a scan you could have checkpointed: launch
  it detached with incremental output, and do other useful work in
  parallel — but against *other servers*, not the same one.
- A scan that "is still running" with no checkpoints is not progress.
- The goal is not a complete point map; stop discovery the moment you have
  one confirmed controllable point of a leveraged type, and pivot to
  exploitation. One writable control at the right scale is worth more than
  a perfect inventory.

## 10. Anti-pattern checklist (failure signatures)

- Repeatedly rewriting the *scanner* when every version fails identically:
  the hypothesis is wrong, not the tool.
- Densifying around known read-side addresses after that family already
  returned uniform 47s.
- Treating silence/drops as "point absent".
- Parallel connections or rapid reconnects to one server, then blaming a
  watchdog/rate limit for the resets.
- Re-testing the same (type, ioa) set with cosmetic variation.
- A hypothesis list that never changes after negative rounds.
- Killing the budget on enumeration with zero time reserved for acting on
  a hit.

If you recognize two or more of these in your own session, stop the scan,
return to phase B, and replace the hypothesis.
