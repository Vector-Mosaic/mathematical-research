# A missing assumption, a counterexample, and a repaired bound

Can four inequalities force a family of polynomial roots to stay close to a
particular scale? This example follows an actual August 2026 research episode
that found the stated assumptions insufficient, constructed a degree-two
counterexample, and retained a corrected result.

The useful distinction is precise: the polynomial can keep distinct positive
roots while the claimed uniform localization fails. Adding a comparison between
the parameter scales repairs a localization theorem. The
[mathematical derivation](mathematics.md) states and proves the exact version
used here; the [provenance](provenance.md) distinguishes that derivation from the
historical agent output and the cited paper. This is not a proof or disproof of RH.

## Check the mathematics

From the repository root, with Python 3.12 or later:

```sh
python examples/finite-free-localization/check.py
```

The checker uses exact rational arithmetic and needs no account or third-party
package. Its JSON output states the scope of its checks. Finite computations
support the example's algebra; the written derivation supplies the argument for
the parameterized counterfamily and general repaired bound.

## Inspect the research store without a model

On Linux with Python 3.12 or later, install the public Python dependencies in
your own virtual environment. The mathematical checker above is portable; this
store walkthrough uses the core's supported Linux filesystem protections.

```sh
python -m pip install -r requirements-dev.txt
python scripts/research_example.py run --output .research/finite-free-walkthrough
python scripts/research_example.py inspect --output .research/finite-free-walkthrough
```

Use a fresh output directory. The walkthrough refuses to overwrite an existing
run. It uses the actual research core and SQLite/CAS store to retain supplied
material, interpretations, candidate revisions, dependencies and a checkpoint,
then reopens that state for inspection. It is a scripted demonstration with
authored judgments and fixture execution identities. It does not call a model,
admit a theorem, or claim autonomous discovery.

The raw checker output and source material remain distinct from their
interpretation. The failed coarse claim, corrected assumptions and useful
surviving result can be inspected together. A passing storage check does not
decide whether a mathematical statement is true.

## Reproduce through the real Host

First follow the [supported Linux setup](../../docs/setup.md), using your own
authenticated account and a dedicated installation. Select this example when
initializing a new, otherwise ordinary installation:

```sh
"$PYTHON" -B "$RELEASE/scripts/research.py" init \
  --state-root "$STATE" --codex-home "$CODEX_HOME" \
  --python "$PYTHON" --node "$NODE" --codex "$CODEX" \
  --example finite-free-localization
"$PYTHON" -B "$RELEASE/scripts/research.py" start --state-root "$STATE"
```

The bundled opening Strategy asks the executive to investigate this bounded
question, retain its conclusions and checkpoint for a fresh successor. The
successor should retrieve the stored correction and check its use. Reference
solutions are openly available in this repository: this is a reproduction,
not a blinded discovery evaluation. The agent may fail or leave work incomplete;
its actual records determine what happened.

Use `inspect` to read the current installation and the documented `stop` command
to finish. `stop --at-checkpoint` requests a stop at the next durable checkpoint.
A stopped native conversation can also resume, but that is different from a
fresh successor epoch. Evidence for the stronger handoff must identify distinct
threads and the successor's actual use of retained research.

## Why these engineering choices matter

The system lets a correction change the interpretation of work without erasing
its source or throwing away an independently useful result. The relevant code is
small enough to follow from this example:

- [Raw capture custody](../../packages/research-core/research_core/mission_evidence.py)
  retains source material separately from scientific judgments.
- [Mission operations](../../packages/research-core/research_core/mission_interface.py)
  own interpretations, versioned candidates, relationships and checkpoints.
- [Workspace storage](../../packages/research-core/research_core/workspace_store.py)
  persists the actual records inspected by the walkthrough.
- [Mission Host](../../services/rh-mission-host/src/mission-host.ts) manages
  executive epochs and recovery; the [Codex boundary](../../packages/codex-thread-core/src/boundary.ts)
  distinguishes fresh thread creation from resuming an existing conversation.

Justin Sublette directed the system's design and iterative development with AI
assistance. This example makes those engineering choices inspectable; it does
not attribute all generated mathematics or every implementation line to him.
The [validation notes](../../docs/validation.md) identify the observed software
and live behavior separately from mathematical claims.
