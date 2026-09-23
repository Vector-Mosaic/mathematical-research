# What was checked

This example separates the mathematical argument, deterministic software
behavior, and a real model execution. They answer different questions.

## Mathematics and durable storage

The exact checker passed with four explicit degree-two counterexamples and six
repaired cases of degrees 1, 2, 3, 5, 8 and 12. It checks coefficient identities,
Sturm root counts, critical points and rational inequalities used by the proof.
The [written argument](mathematics.md), including its cited theorem dependencies,
supplies the general statements. Finite cases do not prove those statements.

On Linux, eight focused Python checks passed for the bundled seed, installation
binding, the existing default initialization, and the scripted research-store
walkthrough. The walkthrough used the real core to retain raw material,
interpretation, candidate revisions and dependencies, commit a checkpoint and
reopen the store. Its scientific judgments were supplied fixtures; no model ran
in that check.

## Live execution

The live reproduction uses Codex 0.153.4, `gpt-6-astra` with `ultra` reasoning,
and the actual Mission Host. Source commit
[`9021eb1`](https://github.com/Vector-Mosaic/mathematical-research/commit/9021eb1df3ee38055715567928cc7c5273599e9c)
was built and installed under an isolated Linux account. The source archive's
SHA-256 is `65774476c7e3acb99264fdf4d812d9107948a90c94629da3e7150199cf9b6929`.
The run uses fresh public state and disclosed reference material, with no private
research input. It is a reference-assisted reproduction, not blind rediscovery
or an estimate of research success rate.

An initial attempt at commit `7ac0080` exposed a real installation bug: the shell
sandbox could not read the native Codex executable outside standard system
paths. Research-tool interaction and checkpoints worked, but shell commands did
not execute. That attempt was stopped and is not counted as a mathematical
success. The fix grants read access to the exact executable without exposing its
directory or the protected credential home. Five focused permission-profile
checks passed, followed by successful shell execution in the new live run.

The first executive, thread `01a0cf79-9fe0-76e1-ba9e-9484d63bc792`, read
the disclosed sources and ran `python3 examples/finite-free-localization/check.py`
successfully. Its first command used an unavailable `python` alias; it corrected
the command and retained the actual successful result.

It created two interpreted Evidence records, `finite-free-counterfamily@1` and
`finite-free-repaired-localization@1`, then
`candidate:finite-free-localization-repair@1`. The candidate explicitly includes
`B/D <= 4`, the localization constant **10**, and the other stated hypotheses.
Its standing is **open**, supported by the scoped derivation and checks; writing
it did not admit it into canonical research.

After the first checkpoint, the Host created fresh executive thread
`01a0cf7d-b134-77a3-b761-c9b41ee35ee7`. Its recorded actions were:

1. Retrieve the predecessor checkpoint and its exact retained record selectors.
2. Read both Evidence revisions and Candidate revision 1 through `rh_mission`.
   All three returned as readable, with the corrected assumptions preserved.
3. Read the public mathematical derivation and rerun the checker successfully.
4. Record `evidence:finite-free-successor-refinement@1`, explicitly confirming
   the retained assumptions and the absence of a discrepancy. It also derived a
   [sharper bound from the retained argument](successor-refinement.md).
5. Save Strategy revision 3 and a second checkpoint, at project commit 17.

The operator requested `stop --at-checkpoint` during this successor epoch. The
Host exited with code 0 after the checkpoint, released its lock, left no active
Goal, and closed its observation stream cleanly. Final owner inspection showed
no admitted result and no capture-recovery obligation. The existing separate
research and training services remained active.

This stops the bounded demonstration; it does not semantically close the RH
Mission. The ordinary Strategy remained `continue`. The initial example seed
had asked for closeout, but the real owner reserves semantic closeout for an
admitted result. The example's instructions were clarified after this run to use
the existing operator checkpoint stop. No Admission rule was relaxed, and no
second live run is claimed for that wording-only clarification.

## Inspect the evidence

[live-run.json](live-run.json) contains selected actual tool requests, returned
record identities, distinct root/epoch identities, checkpoint handles and final
state. [checker-output.json](checker-output.json) is the complete checker output
observed in both epochs. Observation sequence numbers identify the relevant
events within the recorded run; they are not mathematical certificates.

The selection was prepared from the Host's retained observations and checked
against its final owner inspection. It is not a complete public trace or an
independently authenticated archive. Credentials, provider instructions,
unrelated research and machine access details are omitted. The supplied
reference solution was available to both executives; no blind-discovery or
general reliability claim follows from this one reproduction.
