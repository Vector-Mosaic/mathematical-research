# Formal research Attempt adapter

This package owns concrete formal research Attempt execution. It stages an
exact immutable input set, journals external effects,
owns process cancellation and reconciliation, seals provider output, and
returns verified operational facts. The code retains the `wc` schema prefix
from its original Workstation Control boundary. It does not choose research, interpret
mathematics, create Strategy or Context, or admit an Evidence claim.

## Boundary

The research core issues the Session. The adapter accepts only
`FormalSessionBinding`, an opaque binding containing the MR-issued Session ID
and digest plus its exact Mission, Strategy, selected-bet hash, and Context
references. Assignment content is carried in staged files. Workstation does
not copy the question, purpose, expected result, Branch ontology, or a
delegation policy into a competing Session contract.

Install this package into the same Python environment used by the Mission Host:

```sh
python -m pip install -e packages/research-attempt-adapter
```

It requires Python 3.11 or later and has no third-party Python runtime
dependencies. Real execution additionally requires the pinned Codex CLI, the
operator's own authentication, a release-local model catalog, and the Host's
independently established runtime containment. Installing the package creates
no account, starts no process, and connects to no server.

`AttemptIntent` is operational. It binds one new Attempt identity to the
Session, readiness state, coordination and authorization, exact source commit
and source-owner digest, provider/model/effort, actual requested resources,
baseline capabilities, exact selected capabilities, network boundary reference,
immutable input files, and launch worktree. Source verification is injected by
the Host. The included `GitSourceVerifier` verifies a clean checkout, its exact
commit, and the SHA-256 of `git ls-tree -r -z --full-tree HEAD`. An installation
using an archive digest must supply a verifier for that exact algorithm; tree
and archive digests are not interchangeable. `GitSourceVerifier` requires Git
on the operator's executable search path. The selected-capability projection
is the Mission/Host shape: local skill/plugin release roots, exact MCP-server
and app tool allowlists, and an optional
`isolated_ephemeral_unauthenticated` browser. It has no credential, command, or
URL field.

There is no repository-owned token, elapsed-time, output-size, Context-size,
attachment-size/count, cost, Attempt-count, retry-count, or tool-count ceiling.
Physical/provider limitations are observed as provider facts; they are not
invented here as research budgets or automatic stop conditions.

## Staged input

`prepare(session, intent, fence_id=...)` copies zero or more
`SourceAttachment` files into an Attempt-owned read-only staging directory.
Attachment sources must be strictly beneath one of the zero-or-more trusted
source roots independently injected into the adapter constructor. Those roots
are exact normalized non-link directories disjoint from the protected root and
all Attempt-owned stores; an Attempt cannot authorize its own source path. Zero
configured roots is valid only when the Attempt has no attachments.
Every source and copy is checked for exact size/digest, regular-file identity,
portable collision-free logical name, and link/reparse safety. The fixed
`bootstrap.manifest.json` contains only opaque owner bindings, the Attempt
intent digest, and staged logical-name/digest/media facts. It contains no
private source/CAS path and no research body.

The worker receives a small fixed bootstrap object pointing to that manifest
and naming the exact output and scratch roots. Full Context is never serialized
into a receipt or stdin prompt.
The adapter and Codex provider both rehash the manifest and every staged file
immediately before provider use. Extra, missing, linked, hard-linked, moved, or
changed input fails closed.

## Effect lifecycle

The public lifecycle is:

- `install_fence(fence)`
- `prepare(session, intent, fence_id=...)`
- `dispatch(attempt_id)`
- `reconcile(attempt_id)` or `recover()`
- `request_cancel(attempt_id)` for graceful process-group signalling
- `force_stop(attempt_id)` only for an explicit owned-process-tree kill
- `retry(previous_attempt_id, corrected_intent, fence_id=...)`
- `verify_result(attempt_id)`

`dispatch` appends `launch_requested` before calling the provider, then
re-verifies source and staged custody. Stable operation keys make effect calls
idempotent. `CodexExecProvider` persists one immutable launch binding and
starts one detached Attempt-owned worker. That worker owns one dedicated Codex
App Server, records its process-birth identity and process-group/Job-Object
binding, and writes one atomic terminal observation itself. A replacement
provider process observes the same worker as `RUNNING` or reads that terminal
record; it never relaunches. The detached worker runs the installed
`research_attempt_adapter.codex_attempt_worker` module using the Host's Python
interpreter and its own state directory as its working directory. It does not
depend on a monorepo path or the caller's working directory. `UNKNOWN` is reserved for genuinely missing,
corrupt, or dead-without-terminal supervisor facts, not ordinary provider
restart. Late output after UNKNOWN or fencing is sealed as evidence-only and
cannot change the trusted head.

Retry is contextual, not counted. It requires a known terminal failure,
graceful cancellation, or explicit force-stop plus a new Attempt identity,
exact same Session, an explicit correction basis, and a changed operational
binding or readiness-state digest. An unchanged retry is rejected and UNKNOWN
cannot retry. There is no maximum chain length.

`request_cancel` records a durable graceful request which the worker delivers
as `turn/interrupt`; it never waits on a repository deadline and never
escalates to KILL. `force_stop` is a separate durable operation. The worker
terminates only its exact App Server process group on POSIX or named Job Object
on Windows. Reconciliation observes the eventual terminal state. Polling
cadence is an injected transport fact, not an elapsed-time limit.

## Selected capabilities

`baseline_capabilities` selects only the three built-in baseline families: shell, live
web search, and native delegation. It is not a total semantic-tool list. The
unbounded exact additional tool projection is `selected_capabilities`:

- local skill/plugin roots are resolved strictly inside the immutable release,
  must be non-link directories, and are passed as App Server
  `selectedCapabilityRoots`;
- every selected MCP server and app must match an ID from the provider's
  server-side trusted preconfiguration, and only its named tools are enabled;
- every trusted but unselected connector is explicitly disabled;
- zero selections disable skills, plugins, MCP, apps, and browser;
- browser selection enables only the isolated ephemeral unauthenticated mode;
- automatic skill/MCP dependency installation, plugin sharing, remote plugins,
  inherited app defaults, destructive tools, and open-world tools stay off.

When native delegation is selected, the formal root uses the same clean
depth-two hierarchy as the Mission: the root may create branch researchers, a
researcher may create leaf helpers, and leaves cannot create descendants. Width
remains adaptive and has no repository worker-count quota. The provider-native
tool schemas remain the usage authority; the adapter adds compact role and
capability guidance rather than a wrapper or a second tool manual.

There is no three-tool semantic ceiling: any number of exact tools can be
selected across the MCP/app allowlists, and local roots can expose their
selected capabilities. Each Attempt fixes its Mission-selected OpenAI model
and `ultra` effort with provider fallback disabled. Current policy selects
`gpt-6-astra`; historical `gpt-5.6-sol` intents retain their original model.
The Codex 0.153.4 configuration retains the runtime's signed-integer
representational maximum to remove its default
thread backpressure while retaining the depth-two parentage boundary. This does
not allocate workers or impose a repository quota; provider and physical
capacity remain observed facts. The formal provider
also requires the exact pinned model catalog file from inside the Host-selected
immutable release and passes it explicitly to App Server. The provider rejects a
relative, linked, multiply linked, non-regular, missing, or out-of-release catalog,
so Formal Attempts cannot fall back to Codex's bundled finite internal result
truncation. The pinned release retains the same signed-integer maximum for
internal result truncation; remove that override when native disabled or
unbounded values are qualified for the pinned release. The model-policy and
custody checks remain part of this implementation and the Mission Host;
supplying a different account does not relax them.

## Public networking and credentials

`NetworkPolicy.PUBLIC` is valid only when the Attempt carries the exact ID and
digest of a `VerifiedOuterContainment` independently injected into the adapter.
The Codex provider is separately constructed with that same verified fact and
rechecks equality. An Attempt request cannot self-attest its own containment.
The boundary fact requires public-only egress plus denial of private/internal,
metadata, and inbound access. If the real outer sandbox cannot establish those
facts, PUBLIC execution is rejected; Codex's own network toggle is never
misrepresented as public-secretless containment.

The outer worker and Codex App Server may receive `CODEX_HOME` for provider authentication.
The exact path must equal the configured protected root. Codex receives the
current strict configuration used by `codex-thread-core`:

- `shell_environment_policy.inherit="none"` plus the fixed, secretless
  `/usr/bin:/bin` executable-search path;
- a permission profile that reads source/staged input, writes only private
  scratch/output, and denies the protected root;
- live web search for verified PUBLIC execution;
- no inherited user config or repository rules;
- fixed `openai` and the Mission-bound model (`gpt-6-astra` for new Missions,
  with historical `gpt-5.6-sol` bindings supported) / `ultra` with no fallback.

The adapter adds no thread, worker, turn, token, output, Context, objective,
RPC, shutdown, duration, or Attempt limit. Only documented top-level App Server
envelopes and process exit classify the Attempt: top-level model reroute,
top-level error, failed/interrupted terminal turn, and unrequested process exit
are provider failures. Nested tool payloads and handled tool errors remain raw
retained material and do not fail the whole Attempt. Delegation events remain
ordinary factual runtime observations.

## Output custody and result

After the owned App Server tree is terminal, the worker recursively inventories
every file under both exact Attempt output and scratch roots. It follows no
links, streams every digest, rechecks file identity after hashing, and rejects
symlinks/reparse points, hardlinks, special files, and scan-time mutation. There
is no file-count, byte-size, depth, extension, or media-type ceiling. Provider
control files live only under the disjoint provider state root, so no research
file is silently excluded. Each artifact retains its custody root and exact
relative path. The adapter copies the complete inventory into a disjoint
content-addressed seal store, verifies source identity again, and journals all
sealed entries, including distinct paths with identical bytes.

The worker also projects each attributable descendant final plaintext into one
ordinary output text artifact carrying the child thread ID and its direct
parent thread ID. Only final returned plaintext is projected; routine tool logs
and hidden reasoning are not. These files use the existing complete output
inventory and seal path, not a receipt, transcript schema, or parallel custody
mechanism. The raw App Server event file remains retained as before.

`verify_result(attempt_id)` rereads the current append-only journal and freshly
rehashes the exact staged inventory and every sealed artifact. It returns a
typed `AttemptResult` binding the Session/Attempt, source, provider/model,
fence/effect certainty, requested and observed resource facts, manifest and
inventory digests, evidence-only classification, event-history digest, and
canonical `result_digest`. It is a factual operational projection, not an
issued receipt, HMAC attestation, settlement, or mathematical acceptance.

## Tests

Run:

```sh
python -m unittest discover -s packages/research-attempt-adapter/research_attempt_adapter/tests -p 'test_*.py'
```

Install the package first, including for tests which launch a detached worker.
These tests use temporary directories and deterministic providers; they do not
authenticate to an account or start real model inference. The explicit
`research_attempt_adapter.testing` module contains `FakeAttemptProvider`,
`FakeProviderBackend`, and `FakeSourceVerifier`. They are test doubles, never
evidence of actual source custody or a real model run, and are not exported from
the production package API.

The suite covers zero/many/large staged files without repository caps,
constructor-owned source authorization and protected/store disjointness,
append-before-effect crash recovery, post-journal rehash, contextual unbounded
retry, UNKNOWN blocking, graceful cancel versus explicit force-stop,
constructor-bound containment, exact selected-capability projection with 100
tools, zero-selection disablement, strict App Server configuration, top-level
terminal classification, a real detached worker against a fake App Server,
RUNNING and terminal reconciliation across provider replacement without
relaunch, complete recursive output/scratch inventory, hardlink rejection,
exact Git source verification, origin-preserving sealing, and fresh tamper
rejection.
