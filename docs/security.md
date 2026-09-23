# Security and deployment boundaries

This repository supplies software and fresh public project data. It supplies no
server access, provider account, credentials, private research store or deployment
connection. Every installation needs its operator's own machine and Codex
authentication. The original private cloud deployment, its administration tools,
data and account bindings are not part of this distribution.

Use the [Linux setup](setup.md) and [Mission Host configuration](../services/rh-mission-host/README.md).
The initial supported deployment is a dedicated unprivileged Linux runtime
account with a root-owned immutable release. The Host checks ownership and safe
file/path properties of its trusted launch inputs. Canonical research is bound to
the immutable source release. Mutable workspace, Goal scratch, runtime state and
the protected Codex home use separate explicit paths. Do not share these
directories, a runtime lock or an authenticated
Codex home with another installation. The setup does not provision a cloud
machine or authorize changes to other users' services and packages.

## Trusted and model-controlled surfaces

The operator, OS, installed source, Host and Python owners are trusted control
surfaces. The model works through scoped tools and scratch directories; it does
not receive the same authority as those processes. The Host binds semantic
writes to the current Mission and executive. Child research, historical inquiry,
Candidate triage and Admission use distinct identity-bound grants. A tool name
or instruction copied into research material does not confer a grant.

The provider process uses the operator's dedicated `CODEX_HOME` for
authentication. The model's filesystem profile denies that protected root,
keeps release source read-only and confines writes to the supplied research
scratch/output surfaces. The Host also grants read access to the exact resolved
native Codex executable for sandboxed shell launch, without granting its parent
directory or neighboring files. The formal Attempt provider filters its inherited
environment; shell tools receive a secretless environment. Do not place secrets
in project input, prompts, selected capability roots or scratch files, or reuse a
personal Codex home containing unrelated instructions, plugins and history.

Native Codex Linux sandboxing remains enabled. A missing or incompatible sandbox
is an installation failure, not permission to remove the boundary. The pinned
provider/model policy has no silent fallback. Model availability depends on the
operator's account; a source checkout alone does not grant access.

## Network and disclosure

The intended execution boundary permits public network access while denying
private/internal addresses, loopback, link-local/cloud metadata and inbound
access. Formal `PUBLIC` Attempts require matching Host-injected containment
identity and digest. These typed facts bind execution to the operator's trusted
boundary; they do not create a firewall or independently prove that a machine's
network policy works. Retain the native sandbox/network proxy and establish the
actual boundary before relying on public-network execution. The setup and this
document are not themselves evidence of network qualification.

Public outbound access is not a guarantee against disclosure of arbitrary text.
The [research instructions](instructions/AGENTS.md) prohibit deriving external
requests from retained untrusted material, but that is an agent-policy control,
not byte-level information-flow enforcement. Material requiring hard egress
prevention must remain unavailable as raw body in that environment. Model input
also reaches the configured model provider: choose accessible material in light
of that provider relationship.

Candidate preservation, A1 triage, Admission and a canonical Git change do not
authorize public disclosure. There is no automatic publishing service. Optional
notification output is local credential-free JSON in an explicitly configured
outbox; no external notification account or delivery service is included.

## Integrity, recovery and coexistence

The [core](../packages/research-core/README.md) owns the workspace writer and
exact revision transitions. The [Attempt adapter](../packages/research-attempt-adapter/README.md)
checks staged files, source binding and complete output custody, rejecting unsafe
links and changed material. It records intent before effects and reconciles
uncertain execution. Cancellation and force-stop target the exact owned process
tree; they are not permission to stop another installation.

Use independent runtime accounts, directories, lock paths and operator-managed
resource isolation when sharing a host. Do not reuse an existing research
service's virtual environment or modify machine-wide settings to make a new
installation pass. A filesystem boundary alone does not reserve CPU, memory or
disk capacity. Diagnose native faults and uncertain effects before retrying.

Captures, provider events, workspace databases and local observations can contain
research content. Keep them private by default and review exact files before
sharing; a log-redaction helper does not make a whole state directory public.
Preserve useful state for recovery. Never delete a workspace or provider journal
to bypass a failed integrity check.

These are the supported design and operating boundaries, not a claim of a
completed security audit or a passed live deployment check. Test doubles under
`research_attempt_adapter.testing` and fake App Server tests exercise software
behavior; they do not establish real credentials, containment or model behavior.
