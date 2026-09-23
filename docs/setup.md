# Standalone Linux setup

This installs the actual Python research owner and TypeScript Mission Host. The
Host launches the pinned official Codex runtime. Initialization creates a fresh
public RH workspace; it does not import the author's research or connect to any
of the author's infrastructure. Starting a Mission uses your own Codex account
and its available model usage.

Use a dedicated unprivileged Linux account and a separate installation directory.
Do not reuse a research service account, source directory, database, virtual
environment, Codex home or running process from another installation. This
procedure does not create a daemon, scheduler or system service. A shared host's
administrator should choose independent CPU/memory limits before running it;
the examples do not change machine-wide resource settings.

## Prerequisites and paths

- Linux with working native Codex sandbox support. Retain the sandbox and network
  proxy; do not disable either to bypass a failed launch.
- Python 3.12 or newer, Node.js 22 or newer, Git, and pnpm 10.14.0.
- The official `@openai/codex@0.153.4` executable. Use an installation-specific
  package prefix, not a replacement for a shared global Codex installation.
- Your own account with access to `gpt-6-astra` and `ultra` reasoning. This version
  has no model/provider fallback.
- A dedicated authenticated `CODEX_HOME`, mode `0700`, owned by the runtime
  account. Authenticate with the normal pinned Codex login flow. Do not copy an
  existing home, plugins, instructions, configuration or session history. The
  runtime wrapper does not read or display authentication contents and does not
  pass ambient API keys from your shell to the Host.
- A normal `models_cache.json` from your own pinned Codex use. The importer reads
  model metadata from the explicitly selected file; it never reads auth/session
  files. The resulting catalog contains provider instruction text, is generated
  locally, and must not be committed or redistributed as this project's source.

Keep these locations separate:

| Location | Owner and role |
| --- | --- |
| A source checkout | Operator; ordinary Git development and later canonical Admission changes. |
| `/opt/mathematical-research/releases/<40-character-commit>` | Root; immutable built runtime source, public initial canonical state, generated projection and locally imported model catalog. |
| `/var/lib/mathematical-research/installation` | Dedicated runtime account; workspace, Goal scratch, Host state and lock. |
| `/var/lib/mathematical-research/codex-home` | Dedicated runtime account; its own Codex authentication and runtime state. |
| Tool executables | Administrator-installed, readable/executable by the runtime account; independent of other projects' environments. |

These are example paths, not defaults that discover an existing installation.
`scripts/research.py` requires explicit absolute paths. The release directory
must be named for its exact source commit, with no symlink components. Source,
workspace, Goal scratch, Host runtime and Codex home must not overlap.

### Ubuntu 24.04 user namespaces

Ubuntu 24.04's AppArmor policy can block capabilities inside unprivileged user
namespaces, preventing Codex from constructing its native sandbox. If this occurs,
an administrator can add an application-specific profile for this installation's
exact root-owned **native Codex executable**, following the
[Ubuntu release notes](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions).
Use the native binary's absolute path, not the npm JavaScript launcher or a
wildcard, and keep its installation root-owned and non-writable by runtime users.

For example, create a new `/etc/apparmor.d/mathematical-research-codex` with the
placeholder replaced by that exact executable path:

```text
abi <abi/4.0>,
include <tunables/global>
"/ABSOLUTE/ROOT-OWNED/PATH/TO/codex" flags=(unconfined) {
  userns,
}
```

Load only this new profile with
`sudo apparmor_parser -a /etc/apparmor.d/mathematical-research-codex`. Do not replace
another application's profile or disable the system-wide user-namespace
restriction. This profile permits the selected application to construct its own
sandbox; the Codex sandbox and network policy remain enabled.

As the dedicated runtime account, check the selected binary before starting a
Mission:

```bash
"$CODEX" sandbox -- /usr/bin/printf sandbox-ok
```

For pinned Codex 0.153.4, the command is `sandbox` directly, without a `linux`
subcommand. Expect `sandbox-ok` and exit status 0. This checks sandbox launch;
it does not establish provider authentication or research lifecycle success.

## Build and prepare a release

Perform build work in an unprivileged staging directory. Supply paths for your
actual tools and your own model cache; the following shell variables illustrate
that selection:

```bash
SOURCE_CHECKOUT=/absolute/path/to/mathematical-research
STAGING=/absolute/path/to/empty-build-parent
NODE=/absolute/path/to/node
PNPM=/absolute/path/to/pnpm
PYTHON=/absolute/path/to/python3.12
CODEX=/absolute/path/to/codex
MODEL_CACHE=/absolute/path/to/your/models_cache.json
```

Create an archive from the exact commit you intend to install. An archive with a
fresh public history contains no private monorepo history. Build output belongs
in this disposable staging tree, not in the source commit:

```bash
REV=$(git -C "$SOURCE_CHECKOUT" rev-parse HEAD)
mkdir -p "$STAGING/$REV"
git -C "$SOURCE_CHECKOUT" archive --format=tar --output="$STAGING/$REV.tar" "$REV"
tar -xf "$STAGING/$REV.tar" -C "$STAGING/$REV"
cd "$STAGING/$REV"
"$PYTHON" -m venv "$STAGING/build-venv"
BUILD_PYTHON="$STAGING/build-venv/bin/python"
"$BUILD_PYTHON" -m pip install -r requirements-dev.txt
"$PNPM" install --frozen-lockfile
"$PNPM" run build
"$BUILD_PYTHON" -B scripts/research.py prepare-release \
  --release-sha "$REV" --source-bundle "$STAGING/$REV.tar" \
  --model-cache "$MODEL_CACHE" --node "$NODE" --pnpm "$PNPM" --codex "$CODEX"
```

The preparation command checks the archive's commit and exact source file bytes,
records the actual archive SHA-256/size and installed tool versions, generates
the scientific tool projection from Python's owner contract, and imports the
selected model catalog. It does not start a provider. The generated release
record's bundle digest identifies the source archive; build artifacts and the
locally imported catalog are subsequent installation enrichment. This is not a
claim that the Git commit alone hashes every installed file.

An administrator then copies this prepared directory to its final exact release
path, makes the complete release root-owned and non-group/world-writable, and
ensures the runtime account can read it. Do not make a prepared tree writable by
the runtime account. Create the dedicated runtime account and its private state
parent/Codex home using the host's normal administration procedure. No special
user name, server account, SSH key, hosted service or existing research process
is required by this repository.

Use a separate root-owned runtime Python virtual environment, created at its
final path (for example `/opt/mathematical-research/python`) with Python 3.12 or
newer. Set `PYTHON` below to that environment's `bin/python`. Do not move a venv
or leave runtime editable-package links pointing into a disposable staging tree.
The owner scripts load their exact installed source directly. Formal Attempt
workers start as separate Python modules, so the administrator must also install
the adapter into this runtime environment from the selected release:

```bash
"$PYTHON" -m pip install --no-deps \
  /opt/mathematical-research/releases/REPLACE_WITH_EXACT_40_CHARACTER_COMMIT/packages/research-attempt-adapter
```

The adapter has no third-party runtime dependencies. `requirements-dev.txt`
installs the local packages and pinned `jsonschema` used by schema-parity tests
in the separate development environment. Neither environment modifies shared
or global Python packages.

## Initialize fresh research state

Run this as the dedicated runtime account, using the installed release's script.
The installation root must be absent or empty; the Codex home must already exist
with mode `0700`. Its parent must exist and belong to the intended installation.

```bash
RELEASE=/opt/mathematical-research/releases/REPLACE_WITH_EXACT_40_CHARACTER_COMMIT
STATE=/var/lib/mathematical-research/installation
CODEX_HOME=/var/lib/mathematical-research/codex-home
"$PYTHON" -B "$RELEASE/scripts/research.py" init \
  --state-root "$STATE" --codex-home "$CODEX_HOME" \
  --python "$PYTHON" --node "$NODE" --codex "$CODEX"
```

Initialization invokes `rh_mission.py owner-genesis` with the public seed and
exact installed release identity. It creates the real SQLite/CAS research
workspace and a non-secret `installation.json`. It does not authenticate a
provider, launch a Goal, admit a mathematical result or authorize disclosure.
An existing or partially initialized state root is never silently replaced.

The initial canonical state is the public source release's
`projects/riemann_hypothesis/research_state.json`. It is bound to that release
commit. It contains open research obligations, not an RH proof or a copy of any
private result.

## Start, inspect, stop and restart

```bash
# Read the Python owner's current snapshot without launching an agent.
"$PYTHON" -B "$RELEASE/scripts/research.py" inspect --state-root "$STATE"

# Run the actual Host in the foreground. This begins paid/account-metered model use.
"$PYTHON" -B "$RELEASE/scripts/research.py" start --state-root "$STATE"
```

The start command replaces itself with the Node Host. It supplies the exact
installation bindings and a small environment; it does not inherit arbitrary
provider secrets, another `CODEX_HOME` or `PYTHONPATH`. Run `env --state-root
"$STATE"` with the same script to print only this constructed environment.

From another shell under the same runtime account:

```bash
# Request graceful cancellation of this installation's Host only.
"$PYTHON" -B "$RELEASE/scripts/research.py" stop --state-root "$STATE"

# Alternatively, let the current research produce its next durable checkpoint.
"$PYTHON" -B "$RELEASE/scripts/research.py" stop --state-root "$STATE" --at-checkpoint
```

Stop validates the exact runtime lock, process owner, Host entrypoint and working
directory before acting. Linux pidfds keep a reused numeric PID from receiving
the signal. No process-name kill, global stop, time-triggered escalation or
deletion is used. A stop request is not a claim that shutdown has finished: wait
for the foreground Host to exit and inspect its durable state.

Restart by running the same `start` command with the same installation. Do not
delete the database, lock or Host state to disguise an unsuccessful run. The Host
owns restart reconciliation and retains uncertain effects rather than blindly
relaunching them. See the [Host operations](../services/rh-mission-host/README.md)
for the separate exact stopped-epoch reconciliation entrypoint.

For an explicitly selected installation canary, `start --state-root "$STATE"
--single-epoch` asks the Host to stop after one terminal epoch. This is an
operator-selected lifecycle exercise, not a general research worker/time budget
or a claim about mathematical progress. It can still incur model usage, and one
epoch can delegate research. Use the supported stop command when needed.

## Later canonical Admission

Research workspace writes and canonical Admission are distinct. A validated
Admission decision can authorize `scripts/rh_admission.py write-canonical-result`
against an operator-controlled clean source checkout. Read that command's usage
for the exact decision/prestate binding, commit the resulting canonical delta,
build and install a new immutable release, then use its stopped
`rebind-canonical-result` operation with the required exact predecessor/decision
bindings. Keep subsequent private research in your own local/private Git repo if
you do not intend to disclose it. No command here pushes research or opens a
public repository automatically.

Changing `installation.json` alone does not migrate a Mission or establish a
new canonical authority. Preserve the old installation and use the existing
owner's explicit rebind procedure; do not redirect the initial canonical path to
an unrelated writable directory.

## Qualification scope

Installation acceptance is a separate observation: initialize, inspect, run one
small real interaction, request stop, restart and inspect preserved state. Record
the exact source commit, provider/runtime versions and actual result. A passing
software/lifecycle check establishes neither mathematical correctness nor RH
progress. A failed native process or uncertain provider effect calls for
diagnosis, not an automatic retry loop.
