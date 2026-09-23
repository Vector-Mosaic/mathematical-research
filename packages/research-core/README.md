# research-core

`research-core` is the Mathematical Research package for proof-neutral research
state, autonomous Mission and formal Session semantics, durable Evidence,
operational control, recovery, and exact settlement. It does not decide whether
a mathematical claim is true, publish a result, or silently promote work into
the canonical mathematical record.

Formal execution is an optional, explicit Host operational action. It is not a
tenth Mission semantic operation and is never an automatic scheduler. Its
current lifecycle is:

1. The coordinating executive explicitly identifies one exact `formal_request`
   selected by the current Strategy through its `selected_bet_sha256`. Only an
   explicit corrected retry adds a non-empty correction basis.
2. Mathematical Research deterministically creates or replays the open
   revision-one formal Session from the exact current Mission, Strategy,
   selected bet, and Context facts. The WorkspaceStore persists it before any
   provider effect.
3. The Host constructs the Attempt intent from protected runtime facts, the
   immutable release, current Mission policy, and the exact persisted Context.
   The model cannot author provider parameters, tools, paths, credentials,
   limits, clocks, identities, or digests.
4. Workstation Control prepares, dispatches, and reconciles the one concrete
   Attempt. Its Attempt-owned durable worker survives a replacement controller
   process; restart observes the same running or terminal Attempt and never
   relaunches it.
5. Mathematical Research accepts operational facts only after fresh
   `verify_result()` and sealed-artifact verification. Verified files are
   retained through exact file-backed Raw Capture; this boundary creates zero
   Evidence, Admission, canonical, public, or mathematical meaning.
6. Only a known `succeeded` or nonretryable `fenced` Attempt with an eligible
   verified result can settle the Session. A known `failed`, `cancelled`, or
   `force_stopped` Attempt preserves its exact result and Raw Capture but leaves
   the Session open; only an explicit materially corrected request may create a
   distinct successor Attempt under that same immutable Session. `UNKNOWN`,
   active, mismatched, or unverifiable results remain factual custody and cannot
   retry. Fenced or late output remains evidence-only even when the nonretryable
   fenced result closes the Session; no fake receipt or artifact is
   manufactured.
7. Exact replay after a lost reply reuses the Session, Attempt, Raw Capture,
   and any terminal revision rather than duplicating or automatically retrying
   any of them. Retry has no count ceiling, but every successor requires its own
   explicit correction basis and a material operational or readiness change.

This boundary uses owner-rooted Mission, Strategy, selected-bet, Context,
formal-Session, executive-authority, Attempt, source, capability, containment,
provider, and execution-policy identities. Agents pursue the Mission directly
with the applicable mathematical context and tools allowed by its execution
envelope. Owner authorization governs resource, credential, infrastructure,
destructive, and public-disclosure boundaries.

The reusable safety properties remain strict:

- one canonical WorkspaceStore writer and CAS-bound transitions;
- stable Mission, Session, Attempt, authorization, and coordination identity;
- persist-before-effect dispatch and reconciliation without relaunch after
  restart;
- exact source, provider, configuration, capability, Context, and policy
  binding;
- append-only Attempt journaling, cancellation, fencing, and reconciliation;
- complete sealed-artifact custody with settlement-time reopen and rehash;
- explicit uncertainty, provenance, collision checks, alerts, and holds;
- no automatic Strategy, canonical-state, proof, publication, or public effect.

The repository adds no token, elapsed-time, Attempt-count, worker-count,
tool-count, output-size, Context-size, objective-length, RPC, shutdown, or other
research ceiling to this operation. Polling cadence is a transport fact, not a
deadline. Graceful cancellation calls the Workstation cancellation path and
waits without hidden escalation; explicit force-stop is a distinct operator
choice against the exact owned process tree and also has no hidden deadline.

The package now supplies the direct Mission owner, Strategy-selected formal
request discovery, deterministic Session owner, persisted Context staging, and
the explicit formal-Attempt controller. The controller still chooses no
Strategy, Context, synthesis, Evidence meaning, Admission, or mathematical
result: the coordinating executive owns those semantic decisions. The absence
of an automatic formal scheduler is deliberate and must not be confused with
proof or research completion.

Source entrypoints:

- research_core/mission_interface.py: the nine semantic operations and Host binding.
- research_core/workspace_store.py: single-writer durable state and replay.
- research_core/mission_attempt_runtime.py: explicit formal Session execution.
- research_core/canonical_admission.py: operator-owned canonical admission.
- ../../scripts/rh_mission.py: CLI and authenticated Host bridge.
- ../../scripts/rh_admission.py: operator admission effects.

Schemas live in ../../contracts/schemas; fresh public project data lives in
../../projects/riemann_hypothesis. Run this package from a repository checkout:
its canonical authority binds the exact repository resources and commit, so an
isolated wheel without those resources is not a supported runtime.

The public tree omits the private installation's one-time migration commands,
private research fixtures, and historical remote-qualification job catalogs.
Reusable SQL migrations, integrity checks, replay, checkpoint capture and
recovery primitives remain in the core. The omission does not claim that a
private production database can be imported into this public checkout.
