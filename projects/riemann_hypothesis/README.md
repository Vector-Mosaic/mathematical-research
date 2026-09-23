# Public RH starting state

`research_state.json` is a fresh canonical starting document for the public
system. It contains no private research history, proof candidate, admitted
result, source capture, or completed audit. The Riemann Hypothesis remains
unproved in this state.

The current canonical validator retains an inherited moment-program profile:
two named obligations, `B0` and `B1`, one first dependency gap, and an immediate
target implying both obligations. This seed makes that profile explicit without
importing a private moment sequence or asserting an equivalence to RH. The
sequence `m_n` is uninstantiated notation. Choosing its definition, establishing
the required positivity, and proving an exact connection to RH are all open.
These are research obligations, not facts established by initialization.

The profile does not restrict every possible RH argument to a moment program.
The Mission executive can explore other approaches in its noncanonical
workspace. The current canonical document and validator are RH-specific; this
release does not claim to provide a general theorem-project framework.

The separate Mission genesis lives at
`contracts/rh_autonomous_mission_seed.v1.json`. Its public identifiers designate
local state in a new installation. They are not credentials, server addresses,
or grants to the author's infrastructure. Initialization does not authenticate
a provider, start a Mission, or grant public-disclosure authority. An operator
must configure their own runtime and deliberately start it.

Keep each installation's mutable research state and workspace outside this
source directory using the supported runtime configuration. Do not import a
private workspace merely to make the initial state more elaborate.
