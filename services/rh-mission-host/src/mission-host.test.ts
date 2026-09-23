import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test, { afterEach, beforeEach, mock } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

import {
  GoalEpochCancelledError,
  GoalEpochMissionConsistencyError,
  GoalEpochSharedAuthorityLossError,
  resolveGoalEpochInstructions,
  type BoundedGoalEpochRequest,
  type GoalEpochDescendantToolGrantBinding,
  type GoalEpochDescendantToolGrantInput,
  type GoalEpochDiagnosticEvent,
  type GoalEpochOperationContext,
  type GoalEpochStopReason,
} from '../../../packages/codex-thread-core/dist/index.js'

import { PINNED_MODEL_CATALOG_RELATIVE_PATH, type MissionHostConfig } from './config.js'
import { consumePendingForceStop } from './main.js'
import { MissionExecutionObserver } from './execution-observer.js'
import { readRhObservationRows } from './observation-store.js'
import {
  CheckpointCommittedSourceHandoffBridgeError,
  CheckpointDispositionUnverifiedBridgeError,
  MissionBridgeCancelledError,
  MissionBridgeError,
  PythonMissionBridge,
  type AdmissionBinding,
  type AdmissionGrantBinding,
  type BindExecutiveEpochInput,
  type CandidateA1ReviewBinding,
  type CandidateA1ReviewGrantBinding,
  type CheckpointSourceFailureWarning,
  type CheckpointSourceFailureWarningObserver,
  type DirectFailedExecutiveEpochInput,
  type ExecutiveEpochBinding,
  type FormalAttemptRequest,
  type GoalStopContext,
  type HistoricalReadGrantBinding,
  type DelegatedReadBinding,
  type ResearchReadGrantBinding,
  type ResearchReadBinding,
  type JsonObject,
  type MissionAuthorizationCut,
  type MissionOwnerBridge,
  type SemanticOperationBinding,
  type SuspendedEpochCutInput,
} from './mission-bridge.js'
import {
  HOST_STATE_SCHEMA_VERSION,
  inspectMissionGoalLaunch,
  constructHostContinuity,
  constructMissionGoalRequest,
  requireMissionHostSnapshot,
  JsonMissionHostStateStore,
  MissionHost,
  prospectiveGoalWorkspacePath,
  type CheckpointStopIntentConsumer,
  type CodexEpochBoundary,
  type CodexEpochBoundaryCallbacks,
  type CodexEpochBoundaryFactory,
  type MissionHostState,
  type MissionHostStateStore,
  type SingleEpochCanaryIntentConsumer,
} from './mission-host.js'
import type {
  AgentCommunicationsNotification,
  AgentCommunicationsNotificationAdmission,
  MissionNotificationSink,
} from './agent-communications.js'

const SOURCE_REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../..',
)
const RH_INSTRUCTION_RELATIVE_PATH = path.join('docs', 'instructions')
const immutableCatalogFixtures = new Set<string>()
const nativeLstatSync = fs.lstatSync

// Compute runs unprivileged. Model only the deployed UID for exact disposable
// catalog fixtures; preserve real bytes, modes, path types and symlink status.
beforeEach(() => {
  mock.method(fs, 'lstatSync', ((...args: Parameters<typeof fs.lstatSync>) => {
    const stat = Reflect.apply(nativeLstatSync, fs, args) as fs.Stats | fs.BigIntStats | undefined
    if (!stat || typeof args[0] !== 'string' || !immutableCatalogFixtures.has(args[0])) return stat
    return Object.assign(Object.create(Object.getPrototypeOf(stat)), stat, {
      uid: typeof stat.uid === 'bigint' ? 0n : 0,
    })
  }) as typeof fs.lstatSync)
})
afterEach(() => {
  mock.restoreAll()
  immutableCatalogFixtures.clear()
})

/** The developer text Core actually resolves; deliberately excludes owner data. */
function resolvedInstructionsForTest(request: unknown): string {
  assert.ok(isRecordForTest(request))
  const typed = request as unknown as BoundedGoalEpochRequest
  assert.ok(typed.instructionHierarchy)
  return resolveGoalEpochInstructions(typed, [typed.instructionHierarchy.authorityRoot]).developerInstructions
}

/** Read a maintained optional guide without pretending it is injected at launch. */
function scientificGuideForTest(name: string): string {
  return fs.readFileSync(path.join(SOURCE_REPO_ROOT, RH_INSTRUCTION_RELATIVE_PATH, name), 'utf8')
    .replace(/\s+/g, ' ')
}

function stableJsonForTest(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableJsonForTest(item)).join(',')}]`
  }
  if (isRecordForTest(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJsonForTest(value[key])}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

function instructionJson(instructions: string, field: 'executive_orientation' | 'host_continuity'): JsonObject {
  const marker = field === 'executive_orientation' ? 'Current Executive orientation:\n' : 'Current Host continuity:\n'
  const offset = instructions.indexOf(marker)
  assert.notEqual(offset, -1)
  const encoded = instructions.slice(offset + marker.length).split('\n')[0]!
  const parsed = JSON.parse(encoded) as JsonObject
  assert.ok(isRecordForTest(parsed[field]))
  return parsed[field]
}

function nativeObservationIdForTest(input: Record<string, unknown>): string {
  return createHash('sha256').update(stableJsonForTest(input)).digest('hex')
}

function resolvePythonExecutable(): string {
  const completed = spawnSync(
    'python',
    ['-c', 'import sys; print(sys.executable)'],
    { cwd: SOURCE_REPO_ROOT, encoding: 'utf8', windowsHide: true },
  )
  assert.equal(completed.status, 0, completed.stderr)
  const executable = completed.stdout.trim()
  assert.equal(path.isAbsolute(executable), true)
  return executable
}

function initializeRealMissionWorkspace(
  pythonPath: string,
  workspaceRoot: string,
  projectId: string,
  missionId: string,
  releaseSha: string,
  model?: 'gpt-5.6-sol',
): void {
  const code = [
    'import sys',
    'from pathlib import Path',
    'repo=Path(sys.argv[1])',
    'workspace=Path(sys.argv[2])',
    'project_id=sys.argv[3]',
    'mission_id=sys.argv[4]',
    'release_sha=sys.argv[5]',
    'model=sys.argv[6]',
    "research_root=repo/'packages'/'research-core'",
    "test_root=research_root/'tests'",
    "wc_packages=repo/'packages'/'research-attempt-adapter'",
    'sys.path[:0]=[str(research_root),str(test_root),str(wc_packages)]',
    'from research_core.canonical_snapshot import load_canonical_snapshot',
    'from research_core.mission_interface import MissionInterface',
    'from test_mission_interface_direct import _direct_genesis_fixture',
    'loaded=load_canonical_snapshot(repo, source_commit=release_sha)',
    'assert loaded.ok and loaded.value is not None, loaded.failure',
    'seed=_direct_genesis_fixture(project_id=project_id, mission_id=mission_id)',
    "if model: seed['mission']['execution_policy']['model']=model",
    'MissionInterface.initialize_from_owner(',
    ' workspace, project_id=project_id, mission_id=mission_id,',
    ' canonical_snapshot=loaded.value,',
    ' genesis_seed=seed,',
    " owner_command_id='mission-host-cross-language.bootstrap',",
    ')',
  ].join('\n')
  const completed = spawnSync(
    pythonPath,
    ['-c', code, SOURCE_REPO_ROOT, workspaceRoot, projectId, missionId, releaseSha, model ?? ''],
    {
      cwd: SOURCE_REPO_ROOT,
      encoding: 'utf8',
      windowsHide: true,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' },
    },
  )
  assert.equal(completed.status, 0, completed.stderr)
}

function writeDeterministicFormalMissionBridge(scriptPath: string): void {
  const sourceRoot = JSON.stringify(SOURCE_REPO_ROOT)
  const source = [
    'from __future__ import annotations',
    'import os',
    'import runpy',
    'import sys',
    'from pathlib import Path',
    `repo = Path(${sourceRoot})`,
    "research_root = repo / 'packages' / 'research-core'",
    "test_root = research_root / 'tests'",
    "wc_packages = repo / 'packages' / 'research-attempt-adapter'",
    'sys.path[:0] = [str(research_root), str(test_root), str(wc_packages)]',
    "assert os.environ.get('PATH') == '/usr/bin:/bin'",
    "assert 'CODEX_HOME' not in os.environ",
    "assert 'HOME' not in os.environ",
    'import research_core.mission_attempt_runtime as runtime_module',
    'from research_attempt_adapter import (',
    '    AttemptJournal, FakeAttemptProvider, FakeProviderBackend, FakeSourceVerifier,',
    '    OutputArtifact, ProviderObservation, ProviderState, ResearchAttemptAdapter, sha256_file,',
    ')',
    '',
    'def build_deterministic_adapter(*, cas, runtime):',
    '    runtime.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)',
    "    context_root = runtime.state_root / 'context-packages'",
    '    context_root.mkdir(mode=0o700, exist_ok=True)',
    '    backend = FakeProviderBackend()',
    "    provider = FakeAttemptProvider(backend, provider_id='codex_exec')",
    '    def succeed(request):',
    "        output = Path(request.execution_spec.output_directory) / 'formal-result.txt'",
    "        output.write_bytes(b'Deterministic formal fixture result.\\n')",
    "        scratch = Path(request.execution_spec.scratch_directory) / 'app-server-events.jsonl'",
    "        scratch.write_bytes(b'{\"event\":\"provider trace\",\"credential\":\"' + b's' + b'k-' + b'x' * 24 + b'\"}\\n')",
    '        backend.observations[request.attempt_id] = ProviderObservation(',
    "            provider_id='codex_exec', provider_ref=f'fake:{request.attempt_id}',",
    "            state=ProviderState.SUCCEEDED, detail_code='fake_succeeded', exit_code=0,",
    '            artifacts=(',
    '                OutputArtifact(',
    "                    name='formal-result.txt', path=str(output), sha256=sha256_file(output),",
    "                    size_bytes=output.stat().st_size, media_type='text/plain', encoding='utf-8',",
    "                    custody_root='output', relative_path='formal-result.txt',",
    '                ),',
    '                OutputArtifact(',
    "                    name='app-server-events.jsonl', path=str(scratch), sha256=sha256_file(scratch),",
    "                    size_bytes=scratch.stat().st_size, media_type='application/x-ndjson', encoding='utf-8',",
    "                    custody_root='scratch', relative_path='app-server-events.jsonl',",
    '                ),',
    '            ),',
    '        )',
    '    backend.on_launch = succeed',
    '    source_roots = (context_root,)',
    '    if cas.cas_root.exists():',
    '        source_roots += (cas.cas_root,)',
    '    return ResearchAttemptAdapter(',
    "        journal=AttemptJournal(runtime.state_root / 'attempt-journal.sqlite3'),",
    '        providers={provider.provider_id: provider},',
    '        source_verifier=FakeSourceVerifier(),',
    "        input_stage_store_root=runtime.state_root / 'input-stages',",
    "        scratch_store_root=runtime.state_root / 'scratch',",
    "        provider_output_root=runtime.state_root / 'provider-output',",
    "        artifact_store_root=runtime.state_root / 'sealed-output',",
    '        protected_root=runtime.codex_home,',
    '        source_attachment_roots=source_roots,',
    '        outer_containment=runtime.outer_containment,',
    '    )',
    '',
    'runtime_module.build_formal_attempt_adapter = build_deterministic_adapter',
    "runpy.run_path(str(repo / 'scripts' / 'rh_mission.py'), run_name='__main__')",
    '',
  ].join('\n')
  fs.writeFileSync(scriptPath, source, { encoding: 'utf8', flag: 'wx', mode: 0o600 })
}

type EpochScenario =
  | Readonly<{
      kind: 'checkpoint'
      workerCount: number
      missionContinuation: 'continue' | 'closeout'
      terminalGoalStatus?: 'complete' | 'blocked'
      goalRemainsActiveAfterCheckpoint?: boolean
      goalBecomesUsageLimitedAfterCheckpoint?: boolean
      transientBlockedSameTurnBeforeCheckpoint?: boolean
      recoverRejectedNativeMaterial?: boolean
      attemptPredecessorCaptureRecovery?: 'exact' | 'omitted_lineage' | 'wrong_channel'
      leaveWorkspaceNonempty?: boolean
    }>
  | Readonly<{ kind: 'semantic_stop'; terminalGoalStatus?: 'complete' | 'blocked' }>
  | Readonly<{ kind: 'candidate_a1' }>
  | Readonly<{ kind: 'candidate_a1_review' }>
  | Readonly<{ kind: 'candidate_a1_review_grant_mismatch' }>
  | Readonly<{
      kind: 'complete_claim_admission'
      decisionDisposition?: 'authorize_exact_delta' | 'reject'
    }>
  | Readonly<{ kind: 'blocked_without_checkpoint' }>
  | Readonly<{ kind: 'usage_limited'; captureRecovery?: boolean }>
  | Readonly<{ kind: 'model_contract_violation' }>
  | Readonly<{ kind: 'mission_fence' }>
  | Readonly<{ kind: 'historical_advisory' }>
  | Readonly<{ kind: 'historical_advisory_historian' }>
  | Readonly<{ kind: 'historical_advisory_race' }>
  | Readonly<{ kind: 'historical_grant_mismatch' }>
  | Readonly<{ kind: 'research_read'; rejectGrant?: boolean; rejectResult?: boolean }>
  | Readonly<{ kind: 'research_read_mixed_role' }>
  | Readonly<{ kind: 'large_read' }>
  | Readonly<{ kind: 'large_read_tamper' }>
  | Readonly<{ kind: 'large_read_unconsumed_usage_limited' }>
  | Readonly<{ kind: 'large_read_final_page_unacknowledged_usage_limited' }>
  | Readonly<{ kind: 'resume_large_read' }>
  | Readonly<{ kind: 'resume_final_page_replay' }>
  | Readonly<{ kind: 'core_cancel_dynamic' }>
  | Readonly<{ kind: 'core_cancel_material' }>
  | Readonly<{ kind: 'formal_attempt' }>
  | Readonly<{ kind: 'formal_attempt_failed_continue' }>
  | Readonly<{ kind: 'real_formal_lifecycle' }>
  | Readonly<{ kind: 'corrective_error' }>
  | Readonly<{ kind: 'pre_identity_start_failure'; stopFailure?: string }>
  | Readonly<{ kind: 'resume_contract_failure' }>
  | Readonly<{ kind: 'resume_usage_limited' }>
  | Readonly<{
      kind: 'recovery'
      stoppedStatus?: 'complete' | 'usageLimited' | 'paused'
      discoverMaterializedStart?: boolean
      goalAbsent?: boolean
    }>

type LargePageDescriptor = Readonly<{
  handle: string
  nextOffset: number
  content: string
}>

type FinalPageReplay = Readonly<{
  handle: string
  offset: number
  projectedText: string
}>

function isRecordForTest(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function scientificSnapshotForTest(snapshot: JsonObject): JsonObject {
  const rootReference = { kind: 'context', identity: 'mission-science', revision: 4,
    payload_sha256: '4'.repeat(64) }
  const citedReference = { kind: 'context', identity: 'endpoint-source', revision: 1,
    payload_sha256: '5'.repeat(64) }
  const currentReference = { ...citedReference, revision: 2, payload_sha256: '6'.repeat(64) }
  const selection = { mode: 'treatments', treatment_ids: ['physical-gap'] }
  const treatment = {
    question: 'What can the surviving endpoint capability establish for the parent program?',
    account: 'Keep the endpoint construction while the consumer normalization is corrected.',
    qualifications: ['Lower physical-gap bounds do not establish an upper mismatch estimate.'],
    sources: [{ reference: citedReference, selection, roles: ['reliance'],
      why: 'Use the exact historical physical-gap ingredient, not its current-head substitute.',
      dependency: null }],
  }
  const orientation = snapshot.executive_orientation as JsonObject
  ;(orientation.mission as JsonObject).scientific_context_id = rootReference.identity
  ;(((snapshot.current_state as JsonObject).mission as JsonObject).summary as JsonObject)
    .scientific_context_id = rootReference.identity
  orientation.scientific_context = {
    state: 'available', binding: { context_id: rootReference.identity }, root_reference: rootReference,
    purpose: 'Mission-wide scientific understanding independent of current Strategy selection.',
    question: 'Which surviving constructions can address the parent question?',
    known_omissions: ['No complete-target proof has been reconstructed.'],
    restricted_uses: ['ROOT_RESTRICTION: do not treat a surviving contour as its discarded saddle normalization.'],
    restrictions: ['Preserve each consumer domain and quantifier.'],
    independence_treatment: { meaning: 'Independent proof review remains separate.' },
    treatments: [{ context_reference: rootReference, treatment_id: 'endpoint-to-consumer', content: treatment }, {
      context_reference: { kind: 'context', identity: 'deeper-consumer', revision: 2, payload_sha256: '7'.repeat(64) },
      treatment_id: 'unfinished-consumer',
      content: { question: 'Which fixed-order consumer question remains open?',
        account: 'The parent program survives a correction to this consumer.',
        qualifications: ['No uniform-order conclusion follows.'], sources: [] },
      context_qualifications: {
        known_omissions: ['The missing upper estimate remains an open question.'],
        restricted_uses: ['DEEPER_RESTRICTION: fixed-order only, with no uniform-order inference.'],
        restrictions: ['Keep the exact consumer normalization and domain.'],
        independence_treatment: { meaning: 'Exposure is not independent proof review.' },
      },
    }],
    source_changes: [{ cited_reference: citedReference, selection, current_reference: currentReference,
      state: 'advanced_same_identity',
      affected: [{ context_reference: rootReference, treatment_id: 'endpoint-to-consumer', roles: ['reliance'] }],
      qualification: { selection,
        context_qualifications: {
          known_omissions: ['The endpoint estimate does not supply the missing upper bound.'],
          restricted_uses: ['SOURCE_RESTRICTION: no uniform complex-domain inference.'],
          restrictions: ['Keep the literal physical-gap normalization.'],
          independence_treatment: { meaning: 'Source correction is not independent proof review.' },
        },
        treatments: { 'physical-gap': {
        question: 'For which normalized domain does the physical-gap estimate hold?',
        account: 'The physical-gap estimate survives on the explicitly qualified domain.',
        qualifications: ['NORMALIZATION_CORRECTION: gap > 0 only for every real x >= 1, not every complex x.'],
        sources: [],
      } } },
      qualification_ref: null,
      read_call: { operation: 'retrieve', input: { mode: 'read',
        purpose: 'Inspect the exact scientific owner and its qualifications.', ids: ['context:endpoint-source@2'] } },
    }],
    unavailable: [],
  }
  return snapshot
}

function assertHistoricalAdvisoryMaterialityContract(instructions: string): void {
  assert.match(instructions, /Grounding_Discovery_And_Capabilities\.md/)
  const guide = scientificGuideForTest('Grounding_Discovery_And_Capabilities.md')
  assert.match(guide, /separate decision-relative history obligation exists only when all three conditions hold/)
  assert.match(guide, /There is a named pending scientific or continuity decision/)
  assert.match(guide, /There is a plausible materially relevant retained relation, result, residue, or source family/)
  assert.match(guide, /What that history says could change the decision, not merely add background or confidence/)
  assert.match(guide, /Only the affected decision waits for a valid answer/)
  assert.match(guide, /A failed assignment is not an answer/)
  assert.match(guide, /Do not turn this trigger into a general opening gate or checkpoint prerequisite/)
}

function rootGoalOperationContext(
  threadId: string,
  signal: AbortSignal = new AbortController().signal,
): GoalEpochOperationContext {
  return {
    signal,
    rootThreadId: threadId,
    callerThreadId: threadId,
    parentThreadId: null,
    depth: 0,
    turnId: `turn.${threadId}`,
    status: 'root',
    grantId: null,
    assignmentId: null,
  }
}

const MISSION_OBJECTIVE =
  'Advance the Riemann Hypothesis frontier through the highest-value mathematically credible work available: select the next question, delegate adaptively when independent work helps, preserve and interpret useful results, revise Strategy from what was learned, and commit a concise truthful Continuation Checkpoint.'
const LEGACY_SMOKE_SHORTCUT =
  'After the opening investigation is interpreted and synthesized, or after a truthful finding that it cannot be completed, pause and checkpoint.'
const MISSION_PURPOSE: JsonObject = {
  objective: MISSION_OBJECTIVE,
  proof_standard: 'Only an independently reconstructable proof or disproof can establish RH.',
  non_goals: ['Do not mistake computation, agreement, or system state for proof.'],
  closeout_conditions: {
    strategy_mission_continuation: 'closeout',
    semantic_effect: 'Closeout is a Strategy coordination decision, not a proof verdict.',
  },
}
const MISSION_EXECUTION_POLICY: JsonObject = {
  provider: 'openai',
  model: 'gpt-6-astra',
  reasoning_effort: 'ultra',
  model_fallback: false,
  baseline_capabilities: ['rh_mission', 'shell', 'web_search', 'native_delegation'],
  selected_capabilities: {
    local_roots: [],
    mcp_servers: [],
    apps: [],
    browser: null,
  },
  network: {
    public_egress: true,
    secretless: true,
    credential_inheritance: false,
    denied_destinations: ['loopback', 'private_internal', 'link_local', 'cloud_metadata'],
  },
  filesystem: {
    release_access: 'read_only',
    goal_workspace: 'fresh_private_writable',
    protected_credentials_access: 'denied',
  },
  automatic_installation: false,
}
const ADMITTED_RESULT: JsonObject = {
  target: 'riemann_hypothesis',
  disposition: 'proved',
  theorem_or_counterexample_claim: 'Every nontrivial zero of the Riemann zeta function has real part one half.',
  candidate_ref: {
    mission_id: 'mission.rh.public.1',
    candidate_id: 'candidate:synthetic-admitted-proof',
    revision: 3,
    digest_sha256: 'd'.repeat(64),
  },
  admission_decision_ref: {
    decision_id: 'evidence:complete-claim-admission-decision:synthetic',
    digest_sha256: 'e'.repeat(64),
  },
}
const SEMANTIC_OPERATIONS = [
  'orient',
  'retrieve',
  'record_context',
  'interpret_material',
  'record_candidate',
  'record_branch',
  'synthesize',
  'record_strategy',
  'checkpoint',
] as const

function assertSemanticRequestForTest(value: unknown): asserts value is JsonObject {
  assert.equal(isRecordForTest(value), true)
  const request = value as JsonObject
  assert.deepEqual(Object.keys(request).sort(), ['input', 'operation', 'schema_version'])
  assert.equal(request.schema_version, 'mathematical_research.mission_semantic_request.v1')
  assert.equal(SEMANTIC_OPERATIONS.includes(request.operation as typeof SEMANTIC_OPERATIONS[number]), true)
  assert.equal(isRecordForTest(request.input), true)
  const input = request.input as JsonObject
  if (request.operation === 'retrieve') {
    if (input.mode === 'search') {
      assert.deepEqual(Object.keys(input).sort(), ['mode', 'purpose', 'query'])
      assert.equal(typeof input.query, 'string')
    } else {
      assert.equal(input.mode, 'read')
      assert.deepEqual(Object.keys(input).sort(), ['ids', 'mode', 'purpose'])
      assert.equal(Array.isArray(input.ids) && input.ids.length > 0, true)
    }
  } else if (request.operation === 'interpret_material') {
    assert.equal(typeof input.evidence_id, 'string')
    assert.equal(Array.isArray(input.capture_scopes) && input.capture_scopes.length > 0, true)
    assert.equal(typeof input.interpretation, 'string')
    assert.equal(typeof input.scope, 'string')
    assert.equal(typeof input.strength, 'string')
    assert.equal(Array.isArray(input.limitations), true)
    assert.equal(typeof input.significance, 'string')
  } else if (request.operation === 'record_strategy') {
    assert.equal(['continue', 'closeout'].includes(String(input.mission_continuation)), true)
    assert.equal(typeof input.integrated_comparison, 'string')
    assert.equal(Array.isArray(input.reconsideration_conditions), true)
  } else if (request.operation === 'checkpoint') {
    assert.deepEqual(input, {})
  }
}

function freshState(config: MissionHostConfig): MissionHostState {
  return {
    schemaVersion: HOST_STATE_SCHEMA_VERSION,
    missionId: config.missionId,
    activeGoal: null,
    captureRecoveryRequired: [],
    updatedAt: new Date(0).toISOString(),
  }
}

type SeededTerminal = Readonly<{
  kind: 'checkpoint' | 'failed_epoch'
  ownerId: string
  failureReason: string | null
  epochThreadId: string | null
  executiveEpochId: string
}>

class MemoryStateStore implements MissionHostStateStore {
  readonly saves: MissionHostState[] = []
  saveFailuresRemaining = 0
  failCheckpointedSaveOnce = false
  failTerminalGoalClearOnce = false
  deferSaves = false
  private readonly deferredSaves: Array<Readonly<{
    state: MissionHostState
    resolve: () => void
    reject: (error: Error) => void
  }>> = []
  private readonly deferredSaveWaiters: Array<Readonly<{
    count: number
    resolve: () => void
  }>> = []

  constructor(public state: MissionHostState) {}

  async load(): Promise<MissionHostState> {
    return structuredClone(this.state)
  }

  async save(state: MissionHostState): Promise<void> {
    if (
      this.failCheckpointedSaveOnce &&
      state.activeGoal?.phase === 'checkpointed'
    ) {
      this.failCheckpointedSaveOnce = false
      throw new Error('simulated checkpointed Host-state save failure')
    }
    if (
      this.failTerminalGoalClearOnce &&
      this.state.activeGoal !== null &&
      state.activeGoal === null
    ) {
      this.failTerminalGoalClearOnce = false
      throw new Error('simulated terminal Goal-clear save failure')
    }
    if (this.saveFailuresRemaining > 0) {
      this.saveFailuresRemaining -= 1
      throw new Error('simulated durable Host-state save failure')
    }
    const snapshot = structuredClone(state)
    if (this.deferSaves) {
      await new Promise<void>((resolve, reject) => {
        this.deferredSaves.push({
          state: snapshot,
          resolve: () => {
            this.state = structuredClone(snapshot)
            this.saves.push(structuredClone(snapshot))
            resolve()
          },
          reject,
        })
        this.resolveDeferredSaveWaiters()
      })
      return
    }
    this.state = snapshot
    this.saves.push(structuredClone(snapshot))
  }

  async waitForDeferredSaveCount(count: number): Promise<void> {
    if (this.deferredSaves.length >= count) {
      return
    }
    await new Promise<void>((resolve) => {
      this.deferredSaveWaiters.push({ count, resolve })
    })
  }

  resolveLastDeferredSave(): void {
    const pending = this.deferredSaves.pop()
    assert.ok(pending)
    pending.resolve()
  }

  rejectFirstDeferredSave(): void {
    const pending = this.deferredSaves.shift()
    assert.ok(pending)
    pending.reject(new Error('simulated deferred Host-state save failure'))
  }

  private resolveDeferredSaveWaiters(): void {
    for (let index = this.deferredSaveWaiters.length - 1; index >= 0; index -= 1) {
      const waiter = this.deferredSaveWaiters[index]!
      if (this.deferredSaves.length < waiter.count) {
        continue
      }
      this.deferredSaveWaiters.splice(index, 1)
      waiter.resolve()
    }
  }
}

class FakeOwnerBridge implements MissionOwnerBridge {
  snapshotTransform: ((snapshot: JsonObject) => JsonObject) | null = null
  readonly rootQueryContexts: Array<Readonly<{ cursor_mac_key: string }> | undefined> = []
  hostSnapshotCalls = 0
  reconstructCalls = 0

  private missionSummary(): JsonObject {
    return {
      lifecycle: this.missionLifecycle,
      effective: this.missionEffective,
      fence_reason: this.missionFenceReason,
      fenced_at: this.missionFencedAt,
      autonomous: true,
      scientific_context_id: null,
      strategy_ids: this.strategyAvailable ? ['strategy.current'] : [],
      purpose: structuredClone(this.missionPurpose),
      execution_policy: structuredClone(this.missionExecutionPolicy),
    }
  }

  private strategySummary(): JsonObject {
    return {
      mission_continuation: this.missionContinuation,
      integrated_comparison: 'Current test Strategy comparison.',
      formal_requests: [],
    }
  }

  private orientationOpenCandidateA1(): JsonObject[] {
    return this.openCandidateA1.map((item, index) => {
      assert.ok(isRecordForTest(item.candidate_ref), `OPEN A1 ${index} has no Candidate reference`)
      const candidate = item.candidate_ref
      return {
        candidate_ref: {
          id: `candidate:${String(candidate.identity)}`,
          revision: candidate.revision,
          payload_sha256: candidate.payload_sha256,
        },
        retrieval_handle: item.retrieval_handle,
        claim_disposition: item.disposition,
        stage: 'awaiting_a1_review',
        triage: null,
        admission: null,
      }
    })
  }

  private readLatestExecutiveEpoch(): JsonObject | null {
    let latestExecutiveEpoch = this.latestExecutiveEpoch
    if (this.nextLatestExecutiveEpochOverride !== undefined) {
      latestExecutiveEpoch = this.nextLatestExecutiveEpochOverride
      this.nextLatestExecutiveEpochOverride = undefined
    } else if (
      this.checkpointPreterminalEpoch !== null &&
      this.staleCheckpointReadbacksRemaining > 0
    ) {
      latestExecutiveEpoch = this.checkpointPreterminalEpoch
      this.staleCheckpointReadbacksRemaining -= 1
    } else if (
      isRecordForTest(this.latestExecutiveEpoch) &&
      this.failedCheckpointReadbacksRemaining > 0
    ) {
      latestExecutiveEpoch = {
        ...this.latestExecutiveEpoch,
        state: 'failed_before_checkpoint',
        checkpoint_ref: null,
        reconciliation: {
          stage: 'goal_runtime',
          failure_reason: 'boundary_failure',
        },
      }
      this.failedCheckpointReadbacksRemaining -= 1
    }
    return structuredClone(latestExecutiveEpoch)
  }

  async hostSnapshot(signal?: AbortSignal): Promise<JsonObject> {
    this.hostSnapshotCalls += 1
    this.onHostSnapshotStarted?.()
    if (this.blockHostSnapshotResponse) {
      this.blockHostSnapshotResponse = false
      await this.waitForCancellation(signal)
    }
    const mission = this.missionSummary()
    const strategy = this.strategySummary()
    const currentStrategy: JsonObject = { handle: 'strategy:strategy.current@1', mission_continuation: this.missionContinuation,
      integrated_comparison: 'Current test Strategy comparison.', formal_requests: [] }
    for (const field of ['causal_inputs', 'serious_opportunities', 'selected_bets', 'attention_actions', 'context_treatment',
      'creativity_treatment', 'reconsideration_conditions', 'reversal_conditions', 'revival_conditions', 'owner_refs']) {
      currentStrategy[field] = []
    }
    currentStrategy.reconsideration_conditions = [
      { condition: LEGACY_SMOKE_SHORTCUT, owner_refs: [] },
    ]
    const checkpointDocument = isRecordForTest(this.recoveryCut?.document)
      ? this.recoveryCut.document
      : null
    const handle = (reference: JsonObject) => `${String(reference.kind)}:${String(reference.identity)}@${String(reference.revision)}`
    const latest = this.readLatestExecutiveEpoch()
    const checkpoint = checkpointDocument === null || !isRecordForTest(this.recoveryCut?.reference)
      ? null
      : {
          reference: structuredClone(this.recoveryCut.reference),
          project_commit: checkpointDocument.project_commit,
          authoring_epoch_id: checkpointDocument.authoring_epoch_id,
        }
    const snapshot = {
      schema_version: 'mathematical_research.mission_host_snapshot.v4',
      authorization_cut: this.currentAuthorizationCut(),
      current_state: {
        schema_version: 'mathematical_research.mission_host_current_state.v2',
        project_id: 'project.riemann_hypothesis',
        mission_id: 'mission.rh.public.1',
        observed_project_commit: this.observedProjectCommit,
        mission: {
          reference: structuredClone(this.missionReference),
          summary: structuredClone(mission),
        },
        strategy: this.strategyAvailable
          ? {
              reference: structuredClone(this.strategyReference),
              summary: structuredClone(strategy),
            }
          : null,
        checkpoint,
        latest_executive_epoch: latest,
        open_candidate_a1: structuredClone(this.openCandidateA1),
        admitted_result: structuredClone(this.admittedResult),
        canonical_authority: {
          source_commit: 'a'.repeat(40),
          canonical_state_sha256: 'b'.repeat(64),
          canonical_authority_digest: 'c'.repeat(64),
        },
      },
      executive_orientation: {
        schema_version: 'mathematical_research.executive_orientation.v3',
        target: { target: 'riemann_hypothesis', statement: 'Every nontrivial zero of the Riemann zeta function has real part one half.', canonical_status: isRecordForTest(this.admittedResult) ? this.admittedResult.disposition : 'open' },
        mission: { handle: 'mission:mission.rh.public.1@1', lifecycle: mission.lifecycle, effective: mission.effective, autonomous: mission.autonomous, purpose: mission.purpose, scientific_context_id: mission.scientific_context_id },
        current_strategy: currentStrategy,
        scientific_context: { state: 'unbound', binding: null, root_reference: null, purpose: null,
          question: null, known_omissions: [], restricted_uses: [], restrictions: [], independence_treatment: null,
          treatments: [], source_changes: [], unavailable: [] },
        continuity: { checkpoint: checkpointDocument === null ? null : { checkpoint_id: checkpointDocument.checkpoint_id,
          mission_root_handle: handle(checkpointDocument.mission_root as JsonObject), strategy_root_handle: handle(checkpointDocument.strategy_root as JsonObject),
          predecessor_checkpoint_id: checkpointDocument.predecessor_checkpoint === null ? null : (checkpointDocument.predecessor_checkpoint as JsonObject).checkpoint_id },
          mission_relation_to_checkpoint: checkpointDocument === null ? 'no_checkpoint' : 'same_revision',
          strategy_relation_to_checkpoint: checkpointDocument === null ? 'no_checkpoint' : 'same_revision',
          mission_strategy_changes_since_checkpoint: { state: checkpointDocument === null ? 'no_checkpoint' : 'none', counts_by_kind: {}, retrieve_call: null },
          checkpoint_attention: { unresolved_pointer_count: 0, retrieve_call: null } },
        proof_attention: { open_candidate_a1: this.orientationOpenCandidateA1(), admitted_result: structuredClone(this.admittedResult) },
        formal_attention: [], retrieval: { usage_call: { operation: 'usage', input: { for_operation: this.admittedResult === null ? 'retrieve' : 'checkpoint' } },
          available_modes: this.admittedResult === null ? ['read', 'search', 'inventory', 'checkpoint', 'changes_since_checkpoint', 'hooks', 'captures', 'proof_attention'] : [], recommended_calls: [] },
      },
    }
    return this.snapshotTransform?.(snapshot) ?? snapshot
  }
  readonly authorizations: string[] = []
  readonly authorizationCuts: MissionAuthorizationCut[] = []
  readonly suspendedCutValidations: SuspendedEpochCutInput[] = []
  readonly bindings: BindExecutiveEpochInput[] = []
  readonly bindingSignals: AbortSignal[] = []
  readonly semantics: Array<{ request: unknown; binding: SemanticOperationBinding }> = []
  readonly historicalGrantRequests: Array<{
    request: unknown
    binding: HistoricalReadGrantBinding
  }> = []
  readonly delegatedReads: Array<{
    request: unknown
    grant: JsonObject
    binding: DelegatedReadBinding
  }> = []
  readonly researchGrantRequests: Array<{
    request: unknown
    binding: ResearchReadGrantBinding
  }> = []
  readonly researchReads: Array<{
    request: unknown
    grant: JsonObject
    binding: ResearchReadBinding
    researchQueryContext: Readonly<{ cursor_mac_key: string }> | undefined
  }> = []
  readonly candidateA1ReviewGrantRequests: Array<{
    request: unknown
    binding: CandidateA1ReviewGrantBinding
  }> = []
  readonly candidateA1Reviews: Array<{
    request: unknown
    grant: JsonObject
    binding: CandidateA1ReviewBinding
  }> = []
  readonly admissionCaseRequests: Array<{
    request: unknown
    binding: SemanticOperationBinding
  }> = []
  readonly admissionReviewGrantRequests: Array<{
    request: unknown
    binding: AdmissionGrantBinding
  }> = []
  readonly admissionDecisionGrantRequests: Array<{
    request: unknown
    binding: AdmissionGrantBinding
  }> = []
  readonly admissionReviews: Array<{
    request: unknown
    grant: JsonObject
    binding: AdmissionBinding
  }> = []
  readonly admissionDecisions: Array<{
    request: unknown
    grant: JsonObject
    binding: AdmissionBinding
  }> = []
  readonly formalAttempts: Array<{
    request: FormalAttemptRequest
    binding: ExecutiveEpochBinding
  }> = []
  readonly nativeMaterial: unknown[] = []
  readonly nativeBindings: ExecutiveEpochBinding[] = []
  readonly failures: DirectFailedExecutiveEpochInput[] = []
  readonly fences: GoalStopContext[] = []
  readonly cancellations: unknown[] = []
  readonly semanticSignals: AbortSignal[] = []
  readonly formalSignals: AbortSignal[] = []
  readonly nativeMaterialSignals: AbortSignal[] = []
  captureFailuresRemaining = 0
  captureFailureCode = 'invalid_invocation'
  nativeMaterialIdOverride: string | null = null
  nativeMaterialRevision = 1
  largeReadContent: string | null = null
  historicalGrantEnvelopeMutator: ((envelope: JsonObject) => JsonObject) | null = null
  researchGrantEnvelopeMutator: ((envelope: JsonObject) => JsonObject) | null = null
  researchReadResultMutator: ((result: JsonObject) => JsonObject) | null = null
  candidateA1ReviewGrantEnvelopeMutator: ((envelope: JsonObject) => JsonObject) | null = null
  blockCheckpointResponse = false
  blockReconstructionResponse = false
  blockHostSnapshotResponse = false
  blockSemanticResponse = false
  blockNativeMaterialResponse = false
  failAuthorizationResponseAfterCommit = false
  failBindingResponseAfterCommit = false
  staleCheckpointReadbacksRemaining = 0
  failedCheckpointReadbacksRemaining = 0
  onCheckpointCommitted: (() => void) | null = null
  missionPurpose: JsonObject = structuredClone(MISSION_PURPOSE)
  missionExecutionPolicy: JsonObject = structuredClone(MISSION_EXECUTION_POLICY)
  onReconstructStarted: (() => void) | null = null
  onHostSnapshotStarted: (() => void) | null = null
  onAuthorize: ((expectedCut: MissionAuthorizationCut) => void) | null = null
  onBind: ((input: BindExecutiveEpochInput) => void) | null = null
  formalAttemptState: 'succeeded' | 'failed' = 'succeeded'
  recoveryRawCaptures: JsonObject[] = []
  openCandidateA1: JsonObject[] = []
  admittedResult: unknown = null
  // Most legacy finite-epoch scenarios use Strategy closeout only as a test
  // terminator. Model the Store's canonical-result coupling for those fixtures;
  // focused unsolved-closeout tests disable this explicitly.
  admitCanonicalResultOnCloseout = true
  malformedCandidateNotificationProjection = false
  checkpointSourceFailureWarning: CheckpointSourceFailureWarning | null = null
  checkpointCommittedSourceHandoffSafety: 'safe' | 'unsafe' | 'unverified' | null = null
  checkpointDispositionUnverified = false
  nextLatestExecutiveEpochOverride: JsonObject | null | undefined

  private readonly nativeCaptureRefs = new Set<string>()
  private readonly exactEvidenceRevisions = new Map<string, {
    revision: number
    payloadSha256: string
    readableContent?: string
  }>([
    [
      'evidence:synthetic-conditional-rh',
      {
        revision: 7,
        payloadSha256: 'c'.repeat(64),
        readableContent: JSON.stringify({
          statement: 'Synthetic fixture conditional: P and Q implies RH.',
          scope: 'Only the deliberately synthetic Recognition regression fixture.',
          strength: 'fixture_exact',
        }),
      },
    ],
  ])
  private recoveryCut: JsonObject | null = null
  private latestExecutiveEpoch: JsonObject | null = null
  private authorizationOriginCut: MissionAuthorizationCut | null = null
  private checkpointPreterminalEpoch: JsonObject | null = null
  private observedProjectCommit = 0
  private currentRootDigest = 'd'.repeat(64)
  private transitionHeadDigest: string | null = null
  private readonly missionReference: JsonObject = {
    kind: 'mission',
    identity: 'mission.rh.public.1',
    revision: 1,
    payload_sha256: 'a'.repeat(64),
  }
  private readonly strategyReference: JsonObject = {
    kind: 'strategy',
    identity: 'strategy.current',
    revision: 1,
    payload_sha256: 'b'.repeat(64),
  }
  private missionContinuation: 'continue' | 'pause' | 'closeout' = 'continue'
  private missionEffective = true
  private missionLifecycle = 'active'
  private missionFenceReason: 'revoked' | null = null
  private missionFencedAt: string | null = null
  private strategyAvailable = true
  private pendingOwnerOperation: Promise<void> | null = null
  private rejectPendingOwnerOperation: ((error: Error) => void) | null = null

  setContinuation(value: 'continue' | 'pause' | 'closeout'): void {
    this.missionContinuation = value
  }

  setMissionState(lifecycle: string, effective: boolean, fenceReason: 'revoked' | null = null): void {
    this.missionLifecycle = lifecycle
    this.missionEffective = effective
    this.missionFenceReason = fenceReason
    this.missionFencedAt = fenceReason === null ? null : '2026-09-04T00:00:00Z'
  }

  setStrategyAvailable(value: boolean): void {
    this.strategyAvailable = value
  }

  private nextProjectCommit(): number {
    this.observedProjectCommit += 1
    this.currentRootDigest = createHash('sha256')
      .update(`test-root:${this.observedProjectCommit}`)
      .digest('hex')
    this.transitionHeadDigest = createHash('sha256')
      .update(`test-transition:${this.observedProjectCommit}`)
      .digest('hex')
    return this.observedProjectCommit
  }

  private currentAuthorizationCut(): MissionAuthorizationCut {
    return {
      project_commit: this.observedProjectCommit,
      current_root_digest: this.currentRootDigest,
      transition_head_digest: this.transitionHeadDigest,
      canonical_authority_digest: 'c'.repeat(64),
    }
  }

  advanceStoreCut(): MissionAuthorizationCut {
    this.nextProjectCommit()
    return this.currentAuthorizationCut()
  }

  authorizationCut(): MissionAuthorizationCut {
    return structuredClone(this.currentAuthorizationCut())
  }

  private requireExactSyntheticEvidenceRefs(
    refs: unknown,
    location: string,
    requireDigest: boolean,
  ): void {
    if (!Array.isArray(refs) || refs.length !== 3) {
      throw new Error(`${location}: synthetic fake owner requires exactly three Evidence refs`)
    }
    const seen = new Set<string>()
    for (const [index, ref] of refs.entries()) {
      if (!isRecordForTest(ref)) {
        throw new Error(`${location}[${index}]: synthetic fake owner requires an exact Evidence ref`)
      }
      const evidenceId = String(ref.id)
      const exact = this.exactEvidenceRevisions.get(evidenceId)
      if (!exact || Number(ref.revision) !== exact.revision) {
        throw new Error(`${location}[${index}]: synthetic fake owner rejected an unresolved Evidence revision`)
      }
      if (requireDigest && String(ref.digest_sha256) !== exact.payloadSha256) {
        throw new Error(`${location}[${index}]: synthetic fake owner rejected a mismatched Evidence digest`)
      }
      if (seen.has(evidenceId)) {
        throw new Error(`${location}[${index}]: synthetic fake owner rejected a duplicate Evidence ref`)
      }
      seen.add(evidenceId)
    }
    assert.deepEqual(
      [...seen].sort(),
      [
        'evidence:synthetic-conditional-rh',
        'evidence:synthetic-proof-p',
        'evidence:synthetic-proof-q',
      ],
    )
  }

  async cancelOutstanding(reason?: unknown): Promise<void> {
    this.cancellations.push(reason)
    const pending = this.pendingOwnerOperation
    this.rejectPendingOwnerOperation?.(new MissionBridgeCancelledError(reason))
    if (pending) {
      await Promise.allSettled([pending])
    }
  }

  private async waitForCancellation(signal?: AbortSignal): Promise<void> {
    let abortListener: (() => void) | null = null
    const pending = new Promise<void>((_resolve, reject) => {
      this.rejectPendingOwnerOperation = reject
      abortListener = () => reject(new MissionBridgeCancelledError(signal?.reason))
      if (signal?.aborted) {
        abortListener()
      } else {
        signal?.addEventListener('abort', abortListener, { once: true })
      }
    })
    this.pendingOwnerOperation = pending
    try {
      await pending
    } finally {
      if (signal && abortListener) {
        signal.removeEventListener('abort', abortListener)
      }
      this.pendingOwnerOperation = null
      this.rejectPendingOwnerOperation = null
    }
  }

  seedCheckpoint(
    threadId: string,
    continuation: 'continue' | 'pause' | 'closeout',
    executiveEpochId = `epoch.${threadId}`,
  ): SeededTerminal {
    const checkpointId = `checkpoint.${threadId}`
    const projectCommit = this.nextProjectCommit()
    const reference: JsonObject = {
      checkpoint_id: checkpointId,
      payload_sha256: 'c'.repeat(64),
    }
    const predecessorCheckpoint = isRecordForTest(this.recoveryCut?.reference)
      ? structuredClone(this.recoveryCut.reference)
      : null
    this.missionContinuation = continuation
    this.recoveryCut = {
      reference,
      document: {
        schema_version: 1,
        kind: 'direct_continuation_checkpoint',
        checkpoint_id: checkpointId,
        mission_root: structuredClone(this.missionReference),
        strategy_root: structuredClone(this.strategyReference),
        transitive_owner_refs: [],
        causal_pointers: [],
        unresolved_pointers: [],
        pending_capture_locators: [],
        predecessor_checkpoint: predecessorCheckpoint,
        authoring_epoch_id: executiveEpochId,
        project_commit: projectCommit,
      },
    }
    this.latestExecutiveEpoch = {
      executive_epoch_id: executiveEpochId,
      state: 'checkpointed',
      goal_thread_id: threadId,
      last_event_project_commit: projectCommit,
      checkpoint_ref: structuredClone(reference),
      reconciliation: null,
    }
    return {
      kind: 'checkpoint',
      ownerId: checkpointId,
      failureReason: null,
      epochThreadId: threadId,
      executiveEpochId,
    }
  }

  setRecoveryPendingCaptureLocators(locators: JsonObject[]): void {
    assert.ok(isRecordForTest(this.recoveryCut) && isRecordForTest(this.recoveryCut.document))
    this.recoveryCut.document.pending_capture_locators = structuredClone(locators)
  }

  seedFailure(
    threadId: string | null,
    reason: string,
    executiveEpochId = `epoch.${threadId ?? 'unbound'}`,
  ): SeededTerminal {
    const projectCommit = this.nextProjectCommit()
    this.latestExecutiveEpoch = {
      executive_epoch_id: executiveEpochId,
      state: 'failed_before_checkpoint',
      goal_thread_id: threadId,
      last_event_project_commit: projectCommit,
      checkpoint_ref: null,
      reconciliation: {
        stage: threadId === null ? 'authorization_only' : 'goal_runtime',
        failure_reason: reason,
      },
    }
    return {
      kind: 'failed_epoch',
      ownerId: executiveEpochId,
      failureReason: reason,
      epochThreadId: threadId,
      executiveEpochId,
    }
  }

  async reconstruct(signal?: AbortSignal): Promise<JsonObject> {
    this.reconstructCalls += 1
    this.onReconstructStarted?.()
    if (this.blockReconstructionResponse) {
      this.blockReconstructionResponse = false
      await this.waitForCancellation(signal)
    }
    const cutDocument = isRecordForTest(this.recoveryCut?.document)
      ? this.recoveryCut.document
      : null
    const missionBefore = isRecordForTest(cutDocument?.mission_root)
      ? structuredClone(cutDocument.mission_root)
      : null
    const strategyBefore = isRecordForTest(cutDocument?.strategy_root)
      ? structuredClone(cutDocument.strategy_root)
      : null
    const ownerDeltas: JsonObject[] = [
      {
        before_reference: missionBefore,
        current_reference: structuredClone(this.missionReference),
        relation: missionBefore === null ? 'no_prior_cut' : 'unchanged',
        retrieval_handle: 'mission:mission.rh.public.1@1',
        decision_connections: [],
        summary: {
          lifecycle: this.missionLifecycle,
          effective: this.missionEffective,
          fence_reason: this.missionFenceReason,
          fenced_at: this.missionFencedAt,
          autonomous: true,
          strategy_ids: this.strategyAvailable ? ['strategy.current'] : [],
          purpose: structuredClone(this.missionPurpose),
          execution_policy: structuredClone(this.missionExecutionPolicy),
        },
      },
    ]
    if (this.strategyAvailable) {
      ownerDeltas.push({
        before_reference: strategyBefore,
        current_reference: structuredClone(this.strategyReference),
        relation: strategyBefore === null ? 'no_prior_cut' : 'unchanged',
        retrieval_handle: 'strategy:strategy.current@1',
        decision_connections: [],
        summary: {
          mission_continuation: this.missionContinuation,
          integrated_comparison: 'Current test Strategy comparison.',
        },
      })
    }
    const latestExecutiveEpoch = this.readLatestExecutiveEpoch()
    return structuredClone({
      schema_version: 'mathematical_research.direct_mission_reconstruction.v4',
      project_id: 'project.riemann_hypothesis',
      mission_id: 'mission.rh.public.1',
      observed_project_commit: this.observedProjectCommit,
      recovery_opening: {
        cut: this.recoveryCut,
        owner_deltas: ownerDeltas,
        hooks: {
          unresolved: [],
          revival: [],
          recombination: [],
          reconsideration: [],
          reversal: [],
        },
        opportunity_portfolio: this.strategyAvailable
          ? {
              strategy_ref: structuredClone(this.strategyReference),
              mission_continuation: this.missionContinuation,
              integrated_comparison: 'Current test Strategy comparison.',
              causal_inputs: [],
              serious_opportunities: [],
              selected_bets: [],
              attention_actions: [],
              context_treatment: [],
              creativity_treatment: [],
              reconsideration_conditions: [
                { condition: LEGACY_SMOKE_SHORTCUT, owner_refs: [] },
              ],
              reversal_conditions: [],
              revival_conditions: [],
              branch_revival_hooks: [],
              synthesis_hooks: [],
              historical_hooks: [],
              unincorporated_owner_refs: [],
            }
          : null,
        open_candidate_a1: structuredClone(this.openCandidateA1),
        admitted_result: structuredClone(this.admittedResult),
        raw_captures: structuredClone(this.recoveryRawCaptures),
        post_cut_transitions: [],
      },
      latest_executive_epoch: latestExecutiveEpoch,
    })
  }

  async authorizeExecutiveEpoch(
    expectedCut: MissionAuthorizationCut,
  ): Promise<JsonObject> {
    this.authorizationCuts.push(structuredClone(expectedCut))
    if (
      isRecordForTest(this.latestExecutiveEpoch) &&
      this.latestExecutiveEpoch.state === 'authorized' &&
      this.latestExecutiveEpoch.goal_thread_id === null &&
      this.authorizationOriginCut !== null &&
      stableJsonForTest(expectedCut) === stableJsonForTest(this.authorizationOriginCut)
    ) {
      return {
        executive_epoch_id: String(this.latestExecutiveEpoch.executive_epoch_id),
        state: 'authorized',
      }
    }
    this.onAuthorize?.(expectedCut)
    if (stableJsonForTest(expectedCut) !== stableJsonForTest(this.currentAuthorizationCut())) {
      throw new MissionBridgeError(
        'simulated stale Mission Host Store cut',
        2,
        [{ code: 'mission_host_store_cut_stale' }],
      )
    }
    const executiveEpochId = `epoch.${this.authorizations.length}`
    this.authorizations.push(executiveEpochId)
    this.authorizationOriginCut = structuredClone(expectedCut)
    const projectCommit = this.nextProjectCommit()
    this.latestExecutiveEpoch = {
      executive_epoch_id: executiveEpochId,
      state: 'authorized',
      goal_thread_id: null,
      last_event_project_commit: projectCommit,
      checkpoint_ref: null,
      reconciliation: null,
    }
    if (this.failAuthorizationResponseAfterCommit) {
      this.failAuthorizationResponseAfterCommit = false
      throw new MissionBridgeError('simulated lost authorization response', 1)
    }
    return {
      executive_epoch_id: executiveEpochId,
      state: 'authorized',
    }
  }

  async validateSuspendedEpochCut(input: SuspendedEpochCutInput): Promise<JsonObject> {
    this.suspendedCutValidations.push(structuredClone(input))
    const latest = this.latestExecutiveEpoch
    const exactBinding = this.bindings.some((binding) =>
      binding.executiveEpochId === input.executiveEpochId &&
      binding.rootThreadId === input.rootThreadId &&
      binding.workspaceRoot === input.workspaceRoot
    )
    if (
      stableJsonForTest(input.expectedCut) !== stableJsonForTest(this.currentAuthorizationCut()) ||
      !isRecordForTest(latest) ||
      latest.state !== 'bound' ||
      latest.executive_epoch_id !== input.executiveEpochId ||
      latest.goal_thread_id !== input.rootThreadId ||
      !exactBinding
    ) {
      throw new MissionBridgeError(
        'simulated stale suspended Mission Host Store cut',
        2,
        [{ code: 'mission_host_store_cut_stale' }],
      )
    }
    return {
      executive_epoch_id: input.executiveEpochId,
      root_thread_id: input.rootThreadId,
      state: 'bound',
      cut_validated: true,
    }
  }

  async bindExecutiveEpoch(
    input: BindExecutiveEpochInput,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    if (signal) {
      this.bindingSignals.push(signal)
      if (signal.aborted) {
        throw new MissionBridgeCancelledError(signal.reason)
      }
    }
    this.onBind?.(input)
    this.bindings.push(structuredClone(input))
    assert.equal(this.latestExecutiveEpoch?.executive_epoch_id, input.executiveEpochId)
    const projectCommit = this.nextProjectCommit()
    this.latestExecutiveEpoch = {
      executive_epoch_id: input.executiveEpochId,
      state: 'bound',
      goal_thread_id: input.rootThreadId,
      last_event_project_commit: projectCommit,
      checkpoint_ref: null,
      reconciliation: null,
    }
    if (this.failBindingResponseAfterCommit) {
      this.failBindingResponseAfterCommit = false
      throw new MissionBridgeError('simulated lost binding response', 1)
    }
    return {
      executive_epoch_id: input.executiveEpochId,
      state: 'bound',
    }
  }

  async executeSemanticOperation(
    request: unknown,
    binding: SemanticOperationBinding,
    signal?: AbortSignal,
    rootQueryContext?: Readonly<{ cursor_mac_key: string }>,
    warningObserver?: CheckpointSourceFailureWarningObserver,
  ): Promise<JsonObject> {
    this.rootQueryContexts.push(rootQueryContext)
    if (
      isRecordForTest(request) &&
      request.operation === 'record_context' &&
      isRecordForTest(request.input) &&
      request.input.context_id === 'context:semantic-rejection'
    ) {
      return {
        schema_version: 'mathematical_research.mission_semantic_result.v1',
        operation: 'record_context',
        status: 'rejected',
        result: null,
        error: {
          code: 'mission_operation_request_invalid',
          message: '$.input.context_id: must use context:<identity> without @revision',
          location: '$.input.context_id',
          property: 'request_shape',
          failure_scope: 'call',
          correction: 'revise_request',
        },
      }
    }
    if (
      isRecordForTest(request) &&
      request.operation === 'record_context' &&
      isRecordForTest(request.input) &&
      request.input.context_id === 'context:state-conflict'
    ) {
      return {
        schema_version: 'mathematical_research.mission_semantic_result.v1',
        operation: 'record_context',
        status: 'rejected',
        result: null,
        error: {
          code: 'mission_operation_state_conflict',
          message: 'The selected current owner head changed.',
          property: 'current_owner_state',
          failure_scope: 'call',
          correction: 'refresh_then_rejudge_if_semantics_changed',
        },
      }
    }
    if (
      isRecordForTest(request) &&
      request.operation === 'record_context' &&
      isRecordForTest(request.input) &&
      request.input.context_id === 'context:owner-unavailable'
    ) {
      return {
        schema_version: 'mathematical_research.mission_semantic_result.v1',
        operation: 'record_context',
        status: 'unavailable',
        result: null,
        error: {
          code: 'mission_operation_unavailable',
          message: 'The direct owner interface is unavailable.',
          property: 'interface_availability',
          failure_scope: 'operation',
          correction: 'repair_owner_interface',
        },
      }
    }
    if (
      isRecordForTest(request) &&
      request.operation === 'record_candidate' &&
      isRecordForTest(request.input) &&
      isRecordForTest(request.input.standing) &&
      request.input.standing.basis === ''
    ) {
      throw new MissionBridgeError('Mission semantic request was rejected by the owner contract', 2, [
        {
          code: 'mission_operation_request_invalid',
          message: '$.input.standing.basis is required.',
          location: '$.input.standing.basis',
        },
      ])
    }
    if (
      isRecordForTest(request) &&
      request.operation === 'synthesize' &&
      isRecordForTest(request.input) &&
      request.input.relation_question === 'Exercise one accepted and one rejected consequence.'
    ) {
      return {
        schema_version: 'mathematical_research.mission_semantic_result.v1',
        operation: 'synthesize',
        status: 'completed',
        result: {
          records: [
            {
              record_id: 'evidence:accepted-local-consequence',
              revision: 1,
              semantic_summary: 'The first consequence was accepted.',
            },
          ],
          rejections: [
            {
              consequence_index: 1,
              message: '$.input.consequences[1].candidate.candidate_id: wrong owner kind',
            },
          ],
          semantic_summary: 'One consequence committed and one remained rejected.',
        },
        error: null,
      }
    }
    assertSemanticRequestForTest(request)
    if (signal) {
      this.semanticSignals.push(signal)
    }
    this.semantics.push({ request: structuredClone(request), binding: structuredClone(binding) })
    const operation =
      typeof request === 'object' && request !== null && 'operation' in request
        ? String(request.operation)
        : 'unknown'
    if (
      operation === 'interpret_material' &&
      isRecordForTest(request) &&
      isRecordForTest(request.input) &&
      ['evidence:synthetic-proof-p', 'evidence:synthetic-proof-q'].includes(
        String(request.input.evidence_id),
      )
    ) {
      const evidenceId = String(request.input.evidence_id)
      const captureScopes = request.input.capture_scopes
      const captureRef = Array.isArray(captureScopes) && isRecordForTest(captureScopes[0])
        ? String(captureScopes[0].captured_material_id)
        : ''
      if (!this.nativeCaptureRefs.has(captureRef)) {
        throw new Error('synthetic fake owner rejected Evidence without an exact retained capture')
      }
      const exact = {
        revision: 1,
        payloadSha256: evidenceId.endsWith('-p') ? 'a'.repeat(64) : 'b'.repeat(64),
      }
      this.exactEvidenceRevisions.set(evidenceId, exact)
      return {
        status: 'completed',
        operation,
        result: {
          record_id: evidenceId,
          revision: exact.revision,
          payload_sha256: exact.payloadSha256,
          semantic_summary: String(request.input.interpretation),
        },
      }
    }
    if (
      operation === 'synthesize' &&
      isRecordForTest(request) &&
      isRecordForTest(request.input) &&
      request.input.relation_question ===
        'Do synthetic child results P and Q make the exact pre-existing conditional Evidence C applicable?'
    ) {
      this.requireExactSyntheticEvidenceRefs(
        request.input.inputs,
        '$.input.inputs',
        false,
      )
    }
    if (operation === 'record_strategy' && isRecordForTest(request) && isRecordForTest(request.input)) {
      const continuation = request.input.mission_continuation
      if (['continue', 'pause', 'closeout'].includes(String(continuation))) {
        this.missionContinuation = continuation as 'continue' | 'pause' | 'closeout'
        if (continuation === 'closeout' && this.admitCanonicalResultOnCloseout) {
          this.admittedResult = structuredClone(ADMITTED_RESULT)
        }
      }
    }
    if (operation === 'record_candidate' && isRecordForTest(request) && isRecordForTest(request.input)) {
      const recordId = String(request.input.candidate_id)
      if (recordId === 'candidate:synthetic-distributed-rh') {
        this.requireExactSyntheticEvidenceRefs(
          request.input.supporting_refs,
          '$.input.supporting_refs',
          true,
        )
      }
      const candidateId = recordId.startsWith('candidate:')
        ? recordId.slice('candidate:'.length)
        : recordId
      this.openCandidateA1 = [{
        candidate_ref: {
          kind: 'candidate',
          identity: candidateId,
          revision: 1,
          payload_sha256: 'e'.repeat(64),
        },
        retrieval_handle: `candidate:${candidateId}@1`,
        classification: 'purported_complete_rh_proof_or_disproof',
        disposition: 'proof',
        hold_lifecycle: 'open',
        canonical_effect: 'none',
        mathematical_effect: 'none',
      }]
      return {
        status: 'completed',
        operation,
        result: {
          record_id: recordId,
          revision: 1,
          semantic_summary: String(request.input.exact_statement),
          open_candidate_a1: this.malformedCandidateNotificationProjection
            ? {
                candidate_ref: {
                  kind: 'candidate',
                  identity: candidateId,
                  revision: 'malformed-notification-only-revision',
                },
                retrieval_handle: `candidate:${candidateId}@1`,
                classification: 'malformed-notification-only-classification',
                disposition: 'proof',
                hold_lifecycle: 'open',
              }
            : {
            candidate_ref: {
              kind: 'candidate',
              identity: candidateId,
              revision: 1,
              payload_sha256: 'e'.repeat(64),
            },
            retrieval_handle: `candidate:${candidateId}@1`,
            classification: 'purported_complete_rh_proof_or_disproof',
            disposition: 'proof',
            hold_lifecycle: 'open',
          },
        },
      }
    }
    if (operation === 'checkpoint') {
      const input =
        typeof request === 'object' && request !== null && 'input' in request &&
        typeof request.input === 'object' && request.input !== null
          ? request.input as Record<string, unknown>
          : {}
      assert.deepEqual(input, {})
      this.checkpointPreterminalEpoch = structuredClone(this.latestExecutiveEpoch)
      const terminal = this.seedCheckpoint(
        binding.rootThreadId,
        this.missionContinuation,
        binding.executiveEpochId,
      )
      this.onCheckpointCommitted?.()
      if (this.checkpointDispositionUnverified) {
        throw new CheckpointDispositionUnverifiedBridgeError(
          'simulated settled checkpoint with an unverified bridge result',
          0,
          [],
          null,
          {
            exitCode: 0,
            terminationSignal: null,
            stderrTail: '',
            stderrTruncated: false,
            responseError: 'checkpoint_disposition_unverified',
          },
          binding.rootThreadId,
          binding.executiveEpochId,
        )
      }
      if (this.checkpointCommittedSourceHandoffSafety !== null) {
        const checkpoint = {
          checkpoint_id: terminal.ownerId,
          executive_epoch_id: binding.executiveEpochId,
          state: 'checkpointed',
        }
        throw new CheckpointCommittedSourceHandoffBridgeError(
          1,
          [{
            code: 'checkpoint_committed_source_handoff_failed',
            reason_code: 'checkpoint_source_reacquisition_failed',
            checkpoint_completed: true,
            checkpoint_id: terminal.ownerId,
          }],
          null,
          {
            exitCode: 1,
            terminationSignal: null,
            stderrTail: '',
            stderrTruncated: false,
            responseError: null,
          },
          checkpoint,
          this.checkpointCommittedSourceHandoffSafety,
          this.checkpointCommittedSourceHandoffSafety === 'unsafe'
            ? 'published'
            : 'unknown',
          this.checkpointCommittedSourceHandoffSafety === 'unsafe'
            ? 'checkpoint_source_reacquisition_failed'
            : this.checkpointCommittedSourceHandoffSafety === 'safe'
              ? 'checkpoint_source_path_unsafe'
              : 'checkpoint_source_writer_state_unverified',
        )
      }
      if (this.blockCheckpointResponse) {
        await this.waitForCancellation()
      }
      if (this.checkpointSourceFailureWarning !== null) {
        warningObserver?.(structuredClone(this.checkpointSourceFailureWarning))
      }
      return {
        schema_version: 'mathematical_research.mission_semantic_result.v1',
        status: 'completed',
        operation,
        result: {
          checkpoint_id: terminal.ownerId,
          executive_epoch_id: binding.executiveEpochId,
          state: 'checkpointed',
        },
        error: null,
      }
    }
    if (
      operation === 'retrieve' &&
      isRecordForTest(request) &&
      isRecordForTest(request.input) &&
      Array.isArray(request.input.ids) &&
      request.input.ids.includes('evidence:synthetic-conditional-rh@7')
    ) {
      const exact = this.exactEvidenceRevisions.get('evidence:synthetic-conditional-rh')
      if (!exact || exact.revision !== 7) {
        throw new Error('synthetic fake owner has no exact pre-existing conditional Evidence revision')
      }
      return {
        status: 'completed',
        operation,
        result: {
          items: [{
            record_id: 'evidence:synthetic-conditional-rh',
            revision: exact.revision,
            payload_sha256: exact.payloadSha256,
            readable_content: exact.readableContent,
          }],
        },
      }
    }
    if (operation === 'retrieve' && this.largeReadContent !== null) {
      if (this.blockSemanticResponse) {
        await this.waitForCancellation(signal)
      }
      return {
        status: 'completed',
        operation,
        result: { readable_content: this.largeReadContent },
      }
    }
    return { status: 'completed', operation, result: { accepted: true } }
  }

  async issueHistoricalReadGrant(
    request: unknown,
    binding: HistoricalReadGrantBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    const grantRequest = request as JsonObject
    assert.equal(isRecordForTest(grantRequest.context), true)
    this.historicalGrantRequests.push({
      request: structuredClone(request),
      binding: structuredClone(binding),
    })
    const envelope: JsonObject = {
      schema_version: 'mathematical_research.historical_read_grant.v1',
      grant_id: 'f'.repeat(64),
      project_id: 'project.riemann_hypothesis',
      mission_id: 'mission.rh.public.1',
      executive_epoch_id: binding.executiveEpochId,
      root_thread_id: binding.rootThreadId,
      child_thread_id: binding.childThreadId,
      parent_thread_id: binding.parentThreadId,
      direct_depth: 1,
      assignment_id: `historical-assignment:${'e'.repeat(64)}`,
      assignment_mode: grantRequest.assignment_mode,
      assignment: grantRequest.assignment,
      context: {
        ...(grantRequest.context as JsonObject),
        payload_sha256: 'd'.repeat(64),
      },
      allowed_operations: ['orient', 'retrieve'],
      source_families: [...(grantRequest.source_families as string[])].sort(),
      raw_body_policy: grantRequest.raw_body_policy,
      project_commit_cut: this.nextProjectCommit(),
    }
    return this.historicalGrantEnvelopeMutator?.(envelope) ?? envelope
  }

  async executeDelegatedRead(
    request: unknown,
    grant: JsonObject,
    binding: DelegatedReadBinding,
  ): Promise<JsonObject> {
    this.delegatedReads.push({
      request: structuredClone(request),
      grant: structuredClone(grant),
      binding: structuredClone(binding),
    })
    const operation = isRecordForTest(request) ? String(request.operation) : 'unknown'
    return {
      status: 'completed',
      operation,
      result: operation !== 'retrieve' || this.largeReadContent === null
        ? { accepted: true }
        : { readable_content: this.largeReadContent },
    }
  }

  async issueResearchReadGrant(
    request: unknown,
    binding: ResearchReadGrantBinding,
  ): Promise<JsonObject> {
    assert.ok(isRecordForTest(request))
    this.researchGrantRequests.push({
      request: structuredClone(request),
      binding: structuredClone(binding),
    })
    const envelope: JsonObject = {
      schema_version: 'mathematical_research.research_read_grant.v1',
      grant_id: '7'.repeat(64),
      project_id: 'project.riemann_hypothesis',
      mission_id: 'mission.rh.public.1',
      executive_epoch_id: binding.executiveEpochId,
      root_thread_id: binding.rootThreadId,
      child_thread_id: binding.childThreadId,
      parent_thread_id: binding.parentThreadId,
      direct_depth: 1,
      assignment_id: `research-assignment:${'8'.repeat(64)}`,
      assignment: request.assignment,
      allowed_modes: ['usage', 'retrieve'],
      source_families: [...(request.source_families as string[])].sort(),
      raw_body_policy: request.raw_body_policy,
    }
    return this.researchGrantEnvelopeMutator?.(envelope) ?? envelope
  }

  async executeResearchRead(
    request: unknown,
    grant: JsonObject,
    binding: ResearchReadBinding,
    _signal?: AbortSignal,
    researchQueryContext?: Readonly<{ cursor_mac_key: string }>,
  ): Promise<JsonObject> {
    assert.ok(isRecordForTest(request))
    this.researchReads.push({
      request: structuredClone(request),
      grant: structuredClone(grant),
      binding: structuredClone(binding),
      researchQueryContext: structuredClone(researchQueryContext),
    })
    const result: JsonObject = {
      schema_version: 'mathematical_research.research_read_result.v1',
      grant_id: grant.grant_id,
      assignment_id: grant.assignment_id,
      mode: request.mode,
      // Independent observations may advance; no grant freezes this cut.
      project_commit_cut: 40 + this.researchReads.length,
      result: request.mode === 'retrieve' && this.largeReadContent !== null
        ? { readable_content: this.largeReadContent }
        : { accepted: true },
    }
    return this.researchReadResultMutator?.(result) ?? result
  }

  async issueCandidateA1ReviewGrant(
    request: unknown,
    binding: CandidateA1ReviewGrantBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    const grantRequest = request as JsonObject
    assert.equal(isRecordForTest(grantRequest.context), true)
    assert.equal(isRecordForTest(grantRequest.candidate_ref), true)
    this.candidateA1ReviewGrantRequests.push({
      request: structuredClone(request),
      binding: structuredClone(binding),
    })
    const requestedCandidate = grantRequest.candidate_ref as JsonObject
    const envelope: JsonObject = {
      schema_version: 'mathematical_research.candidate_a1_review_grant.v1',
      grant_id: 'c'.repeat(64),
      grant_digest_sha256: 'c'.repeat(64),
      project_id: 'project.riemann_hypothesis',
      mission_id: 'mission.rh.public.1',
      executive_epoch_id: binding.executiveEpochId,
      root_thread_id: binding.rootThreadId,
      child_thread_id: binding.childThreadId,
      parent_thread_id: binding.parentThreadId,
      direct_depth: 1,
      assignment_id: `candidate-a1-review-assignment:${'b'.repeat(64)}`,
      assignment: grantRequest.assignment,
      reviewer_identity: `candidate-a1-reviewer:${binding.childThreadId}`,
      context: {
        ...(grantRequest.context as JsonObject),
        payload_sha256: 'd'.repeat(64),
      },
      candidate_ref: {
        candidate_id: String(requestedCandidate.id).slice('candidate:'.length),
        revision: requestedCandidate.revision,
        payload_sha256: requestedCandidate.payload_sha256,
      },
      source_closure: [{
        kind: 'evidence',
        identity: 'review-basis',
        revision: 3,
        payload_sha256: 'e'.repeat(64),
      }],
      allowed_modes: ['usage', 'retrieve', 'submit'],
      project_commit_cut: this.nextProjectCommit(),
    }
    return this.candidateA1ReviewGrantEnvelopeMutator?.(envelope) ?? envelope
  }

  async executeCandidateA1Review(
    request: unknown,
    grant: JsonObject,
    binding: CandidateA1ReviewBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    const reviewRequest = request as JsonObject
    this.candidateA1Reviews.push({
      request: structuredClone(request),
      grant: structuredClone(grant),
      binding: structuredClone(binding),
    })
    const common = {
      mode: reviewRequest.mode,
      candidate_ref: structuredClone(grant.candidate_ref),
      canonical_effect: 'none',
      public_effect: 'none',
    }
    if (reviewRequest.mode === 'usage') {
      return {
        schema_version: 'mathematical_research.candidate_a1_review_usage.v1',
        ...common,
        purpose: 'Independently review one exact frozen Candidate A1.',
        allowed_modes: ['usage', 'retrieve', 'submit'],
        context: structuredClone(grant.context),
        dispositions: ['invalidated', 'admission_ready'],
      }
    }
    if (reviewRequest.mode === 'retrieve') {
      return {
        schema_version: 'mathematical_research.candidate_a1_review_retrieval.v1',
        ...common,
        grant_id: grant.grant_id,
        context: structuredClone(grant.context),
        source_closure: 'frozen_candidate_authored_exact_owner_refs',
        items: [{ role: 'frozen_candidate_a1', reference: structuredClone(grant.candidate_ref) }],
        next_cursor: null,
      }
    }
    return {
      schema_version: 'mathematical_research.candidate_a1_review_submission.v1',
      ...common,
      status: 'completed',
      disposition: reviewRequest.disposition,
      triage_evidence_ref: {
        evidence_id: 'candidate-a1-triage:test',
        revision: 1,
        payload_sha256: 'f'.repeat(64),
      },
      replayed: false,
    }
  }

  async openCompleteClaimAdmissionCase(
    request: unknown,
    binding: SemanticOperationBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    const candidateRef = (request as JsonObject).candidate_ref
    assert.equal(isRecordForTest(candidateRef), true)
    this.admissionCaseRequests.push({
      request: structuredClone(request),
      binding: structuredClone(binding),
    })
    return {
      schema_version: 'mathematical_research.complete_claim_admission_case_result.v1',
      status: 'opened',
      candidate_ref: structuredClone(candidateRef),
      case_ref: {
        id: 'evidence:complete-claim-admission-case-test',
        revision: 1,
        payload_sha256: '1'.repeat(64),
      },
      replayed: false,
      canonical_effect: 'none',
      mathematical_effect: 'none',
      mission_effect: 'none',
      strategy_effect: 'none',
      public_effect: 'none',
    }
  }

  private admissionGrantEnvelope(
    request: JsonObject,
    binding: AdmissionGrantBinding,
    role: 'independent_complete_claim_admission_reviewer' | 'independent_complete_claim_admitter',
  ): JsonObject {
    const caseRef = request.case_ref as JsonObject
    const isReviewer = role === 'independent_complete_claim_admission_reviewer'
    const grantId = isReviewer ? '2'.repeat(64) : '3'.repeat(64)
    return {
      schema_version: 'mathematical_research.complete_claim_admission_grant.v1',
      grant_id: grantId,
      grant_digest_sha256: grantId,
      project_id: 'project.riemann_hypothesis',
      mission_id: 'mission.rh.public.1',
      executive_epoch_id: binding.executiveEpochId,
      root_thread_id: binding.rootThreadId,
      child_thread_id: binding.childThreadId,
      parent_thread_id: binding.parentThreadId,
      direct_depth: 1,
      assignment_id: `${isReviewer ? 'admission-review' : 'admission-decision'}:${'4'.repeat(64)}`,
      assignment: request.assignment,
      actor_identity: `${role}:${binding.childThreadId}`,
      role,
      context: {
        ...(request.context as JsonObject),
        payload_sha256: '5'.repeat(64),
      },
      case_ref: {
        evidence_id: String(caseRef.id).slice('evidence:'.length),
        revision: caseRef.revision,
        payload_sha256: caseRef.payload_sha256,
      },
      review_ref: isReviewer
        ? null
        : {
            evidence_id: 'complete-claim-admission-review-test',
            revision: 1,
            payload_sha256: '6'.repeat(64),
          },
      source_closure: [{
        kind: 'candidate',
        identity: 'complete-rh',
        revision: 2,
        payload_sha256: 'a'.repeat(64),
      }],
      allowed_modes: ['usage', 'retrieve', 'submit'],
      project_commit_cut: this.nextProjectCommit(),
    }
  }

  async issueAdmissionReviewGrant(
    request: unknown,
    binding: AdmissionGrantBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    this.admissionReviewGrantRequests.push({
      request: structuredClone(request),
      binding: structuredClone(binding),
    })
    return this.admissionGrantEnvelope(
      request as JsonObject,
      binding,
      'independent_complete_claim_admission_reviewer',
    )
  }

  async issueAdmissionDecisionGrant(
    request: unknown,
    binding: AdmissionGrantBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    this.admissionDecisionGrantRequests.push({
      request: structuredClone(request),
      binding: structuredClone(binding),
    })
    return this.admissionGrantEnvelope(
      request as JsonObject,
      binding,
      'independent_complete_claim_admitter',
    )
  }

  private admissionExecutionResult(
    request: JsonObject,
    grant: JsonObject,
  ): JsonObject {
    const mode = String(request.mode)
    const common = {
      mode,
      role: grant.role,
      case_ref: structuredClone(grant.case_ref),
      canonical_effect: 'none',
      public_effect: 'none',
    }
    if (mode === 'usage') {
      return {
        schema_version: 'mathematical_research.complete_claim_admission_usage.v1',
        ...common,
        allowed_modes: ['usage', 'retrieve', 'submit'],
      }
    }
    if (mode === 'retrieve') {
      return {
        schema_version: 'mathematical_research.complete_claim_admission_retrieval.v1',
        ...common,
        items: [{ role: 'admission_case', reference: structuredClone(grant.case_ref) }],
        next_cursor: null,
      }
    }
    const reviewer = grant.role === 'independent_complete_claim_admission_reviewer'
    return {
      schema_version: 'mathematical_research.complete_claim_admission_submission.v1',
      ...common,
      status: 'completed',
      disposition: request.disposition,
      record_ref: reviewer
        ? {
            id: 'evidence:complete-claim-admission-review-test',
            revision: 1,
            payload_sha256: '6'.repeat(64),
          }
        : {
            id: 'evidence:complete-claim-admission-decision-test',
            revision: 1,
            payload_sha256: '7'.repeat(64),
          },
      canonical_result_binding: reviewer || request.disposition !== 'authorize_exact_delta'
        ? null
        : { result: 'proved', claim: 'RH' },
      replayed: false,
    }
  }

  async executeAdmissionReview(
    request: unknown,
    grant: JsonObject,
    binding: AdmissionBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    this.admissionReviews.push({
      request: structuredClone(request),
      grant: structuredClone(grant),
      binding: structuredClone(binding),
    })
    return this.admissionExecutionResult(request as JsonObject, grant)
  }

  async executeAdmissionDecision(
    request: unknown,
    grant: JsonObject,
    binding: AdmissionBinding,
  ): Promise<JsonObject> {
    assert.equal(isRecordForTest(request), true)
    this.admissionDecisions.push({
      request: structuredClone(request),
      grant: structuredClone(grant),
      binding: structuredClone(binding),
    })
    return this.admissionExecutionResult(request as JsonObject, grant)
  }

  async executeFormalAttempt(
    request: FormalAttemptRequest,
    binding: ExecutiveEpochBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    this.formalAttempts.push({
      request: structuredClone(request),
      binding: structuredClone(binding),
    })
    if (signal) {
      this.formalSignals.push(signal)
    }
    return {
      schema: 'mr.formal_attempt_reconciliation.v1',
      session_id: 'session.formal.test',
      attempt_id: 'attempt.formal.test',
      attempt_state: this.formalAttemptState,
      provider_effect_certainty: 'known',
      result_digest_sha256: 'd'.repeat(64),
      raw_capture: null,
      session_was_terminal: false,
      session_is_terminal: this.formalAttemptState === 'succeeded',
      result_bound_to_session: this.formalAttemptState === 'succeeded',
      terminal_transition_replayed: false,
    }
  }

  async captureNativeMaterialObservation(
    observation: unknown,
    binding: ExecutiveEpochBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    if (signal) {
      this.nativeMaterialSignals.push(signal)
    }
    if (this.blockNativeMaterialResponse) {
      await this.waitForCancellation(signal)
    }
    if (this.captureFailuresRemaining > 0) {
      this.captureFailuresRemaining -= 1
      throw new MissionBridgeError('owner rejected one material capture', 2, [
        { code: this.captureFailureCode },
      ])
    }
    this.nativeBindings.push(structuredClone(binding))
    this.nativeMaterial.push(structuredClone(observation))
    const observed = observation as Record<string, unknown>
    const captureDigest = createHash('sha256')
      .update(String(observed.observationId))
      .digest('hex')
    const materialId = this.nativeMaterialIdOverride ??
      `capture:raw-capture:${captureDigest.slice(0, 48)}`
    this.nativeCaptureRefs.add(materialId)
    return {
      material_id: materialId,
      revision: this.nativeMaterialRevision,
      custody_status: 'store_cas_verified',
      canonical_effect: 'none',
    }
  }

  async fenceMission(context: GoalStopContext): Promise<JsonObject> {
    this.fences.push(structuredClone(context))
    this.setMissionState('held', false, 'revoked')
    return { mission_id: 'mission.rh.public.1', state: 'fenced' }
  }

  async recordDirectFailedExecutiveEpoch(
    input: DirectFailedExecutiveEpochInput,
  ): Promise<JsonObject> {
    this.failures.push(structuredClone(input))
    const reconciliation = input.reconciliation
    const reason = String(reconciliation.failure_reason)
    const threadId = isRecordForTest(this.latestExecutiveEpoch)
      && typeof this.latestExecutiveEpoch.goal_thread_id === 'string'
      ? this.latestExecutiveEpoch.goal_thread_id
      : null
    this.seedFailure(threadId, reason, input.executiveEpochId)
    return {
      executive_epoch_id: input.executiveEpochId,
      state: 'failed_before_checkpoint',
    }
  }
}

class ScriptedBoundary {
  private request: Record<string, unknown> | null = null
  private acted = false
  private monitorReadCount = 0
  private readonly operationController = new AbortController()
  actFailure: string | null = null
  largeReadResult: JsonObject | null = null
  realFormalFailure: string | null = null
  realFormalReconciliation: JsonObject | null = null
  tamperFailureText: string | null = null
  readonly checkpointTerminalHandoffs: Array<string | null> = []
  readonly rootToolResultContentItemCounts: number[] = []
  readonly rootToolResultPayloads: JsonObject[] = []
  readonly nativeMaterialCustodyOutcomes: JsonObject[] = []
  readonly stopCalls: Array<{ threadId: string; reason: string }> = []
  readonly recoveryCalls: Array<{
    threadId: string
    reason: string
    recovery: { phase: 'pending' | 'registered'; expectedObjective: string }
  }> = []
  readonly monitorEvents: Array<{
    kind: 'goal_state' | 'same_turn_normal_activity'
    status: 'active' | 'blocked'
    activeTurnId: string
  }> = []
  readonly directChildThreadIds = new Set<string>()
  readonly installedHistoricalGrants: GoalEpochDescendantToolGrantBinding[] = []
  workspacePresentAtStop: boolean | null = null
  stopFailuresRemaining = 0

  constructor(
    readonly workspaceRoot: string,
    private readonly runtimeDir: string,
    private readonly callbacks: CodexEpochBoundaryCallbacks,
    private readonly scenario: EpochScenario,
    private threadId: string,
    private readonly onLargePageDescriptor:
      ((descriptor: LargePageDescriptor) => Promise<void> | void) | null,
    private readonly retainedLargePage: () => LargePageDescriptor | null,
    private readonly onFinalPageReplay:
      ((descriptor: FinalPageReplay) => Promise<void> | void) | null,
    private readonly retainedFinalPageReplay: () => FinalPageReplay | null,
    private readonly mutateDelegatedPageCustody:
      ((handle: string) => (() => void)) | null,
  ) {}

  async start(): Promise<void> {}

  async waitForFatalBoundaryFence(): Promise<void> {}

  async stop(): Promise<void> {
    try {
      await fs.promises.lstat(this.workspaceRoot)
      this.workspacePresentAtStop = true
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        throw error
      }
      this.workspacePresentAtStop = false
    }
    if (this.scenario.kind === 'pre_identity_start_failure' && this.scenario.stopFailure) {
      throw new Error(this.scenario.stopFailure)
    }
  }

  resolveDirectChildToolGrantTarget(input: {
    rootThreadId: string
    childThreadId: string
  }): any {
    if (input.rootThreadId !== this.threadId || !this.directChildThreadIds.has(input.childThreadId)) {
      throw new Error('test historical-read grant target is not one exact direct child')
    }
    return {
      rootThreadId: this.threadId,
      childThreadId: input.childThreadId,
      parentThreadId: this.threadId,
      depth: 1,
      status: 'active',
      activeTurnId: `turn.${input.childThreadId}`,
    }
  }

  installDescendantToolGrant(
    input: GoalEpochDescendantToolGrantInput,
  ): GoalEpochDescendantToolGrantBinding {
    const target = this.resolveDirectChildToolGrantTarget(input)
    if (this.scenario.kind === 'research_read_mixed_role') {
      // Core owns role eligibility. This fixture supplies only its rejected
      // install outcome so the Host's grant rollback can be exercised.
      const prior = this.installedHistoricalGrants.find(({ childThreadId }) => childThreadId === input.childThreadId)
      if (prior && JSON.stringify(prior.allowedToolNames) !== JSON.stringify(input.allowedToolNames)) {
        throw new Error('simulated Core rejection of a mixed-role grant')
      }
    }
    const binding: GoalEpochDescendantToolGrantBinding = {
      ...target,
      grantId: input.grantId,
      assignmentId: input.assignmentId,
      allowedToolNames: [...input.allowedToolNames],
    }
    this.installedHistoricalGrants.push(binding)
    return binding
  }

  async revokeLatestHistoricalGrant(
    reason: 'child_turn_completed' | 'goal_suspended' = 'child_turn_completed',
  ): Promise<void> {
    const installed = this.installedHistoricalGrants.at(-1)
    assert.ok(installed)
    await this.callbacks.onDescendantToolGrantRevoked({
      rootThreadId: installed.rootThreadId,
      childThreadId: installed.childThreadId,
      parentThreadId: installed.parentThreadId,
      depth: installed.depth,
      grantId: installed.grantId,
      assignmentId: installed.assignmentId,
      reason,
    })
  }

  async findMaterializedGoalEpoch(): Promise<{ threadId: string } | null> {
    return this.scenario.kind === 'recovery' && this.scenario.discoverMaterializedStart === true
      ? { threadId: this.threadId }
      : null
  }

  async startBoundedGoalEpoch(request: unknown): Promise<any> {
    this.request = request as Record<string, unknown>
    if (this.scenario.kind === 'pre_identity_start_failure') {
      throw new Error('simulated pre-identity Goal start failure')
    }
    await this.callbacks.onEpochIdentityMaterialized({
      threadId: this.threadId,
      workspaceRoot: this.workspaceRoot,
      appServerPid: 8000,
    } as any)
    return {
      threadId: this.threadId,
      turnId: `turn.${this.threadId}`,
      workspaceRoot: this.workspaceRoot,
      instructionSources: [],
    }
  }

  async resumeBoundedGoalEpoch(threadId: string, request: unknown): Promise<any> {
    this.threadId = threadId
    this.request = request as Record<string, unknown>
    if (this.scenario.kind === 'resume_contract_failure') {
      throw new Error('simulated resumed Goal contract failure')
    }
    if (this.scenario.kind === 'resume_usage_limited') {
      await this.callbacks.onGoalTermination({
        threadId: this.threadId,
        turnId: `turn.${this.threadId}`,
        reason: 'usage_limited',
        containmentScope: 'goal_local',
      })
      throw new Error('simulated usage limit while resume was activating')
    }
    return { threadId }
  }

  private toolCall(tool: string, argumentsValue: Record<string, unknown>, suffix: string): any {
    return this.toolCallFor(this.threadId, tool, argumentsValue, suffix)
  }

  private toolCallFor(
    callerThreadId: string,
    tool: string,
    argumentsValue: Record<string, unknown>,
    suffix: string,
  ): any {
    return {
      threadId: callerThreadId,
      turnId: `turn.${callerThreadId}`,
      callId: `call.${callerThreadId}.${suffix}`,
      tool,
      namespace: null,
      arguments: argumentsValue,
    }
  }

  private rootOperationContext(
    signal: AbortSignal = this.operationController.signal,
  ): GoalEpochOperationContext {
    return rootGoalOperationContext(this.threadId, signal)
  }

  private async requireSuccessfulToolCall(call: any): Promise<any> {
    const result = await this.callbacks.onDynamicToolCall(call, this.rootOperationContext())
    if (!result.success) {
      throw new Error(`scripted dynamic tool call failed: ${JSON.stringify(result.contentItems)}`)
    }
    this.rootToolResultContentItemCounts.push(result.contentItems.length)
    this.rootToolResultPayloads.push(
      JSON.parse(result.contentItems[0]?.text ?? '{}') as JsonObject,
    )
    if (call?.arguments?.operation === 'checkpoint') {
      this.checkpointTerminalHandoffs.push(result.terminalHandoff ?? null)
    }
    return result
  }

  private async recordContinuation(
    continuation: 'continue' | 'closeout',
    suffix: string,
  ): Promise<void> {
    await this.requireSuccessfulToolCall(
      this.toolCall(
        'rh_mission',
        {
          operation: 'record_strategy',
          input: {
            mission_continuation: continuation,
            integrated_comparison: `Test Strategy chooses ${continuation}.`,
            reconsideration_conditions: [
              { condition: 'Reconsider when the mathematical frontier changes.' },
            ],
          },
        },
        suffix,
      ),
    )
  }

  private async act(): Promise<void> {
    if (this.acted || this.scenario.kind === 'recovery') {
      return
    }
    this.acted = true
    if (this.scenario.kind === 'usage_limited') {
      if (this.scenario.captureRecovery) {
        const childThreadId = `${this.threadId}.child.0`
        const assignment = {
          materialKind: 'assignment',
          content: 'Investigate useful RH route 0',
          rootThreadId: this.threadId,
          parentThreadId: this.threadId,
          childThreadId,
        } as const
        const assignmentOutcome = await this.callbacks.onNativeMaterialObserved({
          observationId: nativeObservationIdForTest(assignment),
          ...assignment,
        }, this.rootOperationContext())
        if (assignmentOutcome) {
          this.nativeMaterialCustodyOutcomes.push(structuredClone(assignmentOutcome))
        }
        const output = {
          materialKind: 'output',
          content: 'Unrelated useful RH output remains capturable.',
          rootThreadId: this.threadId,
          parentThreadId: this.threadId,
          childThreadId,
        } as const
        const outputOutcome = await this.callbacks.onNativeMaterialObserved({
          observationId: nativeObservationIdForTest(output),
          ...output,
        }, this.rootOperationContext())
        if (outputOutcome) {
          this.nativeMaterialCustodyOutcomes.push(structuredClone(outputOutcome))
        }
      }
      return
    }
    if (this.scenario.kind === 'blocked_without_checkpoint') {
      return
    }
    if (this.scenario.kind === 'model_contract_violation') {
      await this.callbacks.onGoalTermination({
        threadId: this.threadId,
        turnId: `turn.${this.threadId}`,
        reason: 'model_contract_violation',
        containmentScope: 'goal_local',
      })
      throw new Error('simulated model contract violation')
    }
    if (this.scenario.kind === 'mission_fence') {
      await this.callbacks.onMissionFence({
        threadId: this.threadId,
        turnId: `turn.${this.threadId}`,
        reason: 'mission_consistency_failure',
        containmentScope: 'mission_fence',
      })
      throw new Error('simulated Mission fence')
    }
    if (this.scenario.kind === 'research_read') {
      const childThreadId = `${this.threadId}.child.research`
      this.directChildThreadIds.add(childThreadId)
      const grantRequest = {
        child_thread_id: childThreadId,
        assignment: 'Investigate the exact scientific source and report qualified mathematics.',
        source_families: ['evidence', 'missions', 'sessions'],
        raw_body_policy: 'metadata_only',
      }
      const deny = async (call: any, context = this.rootOperationContext()): Promise<void> => {
        const result = await this.callbacks.onDynamicToolCall(call, context)
        assert.equal(result.success, false)
      }
      await deny(this.toolCall('rh_mission_research_read_grant', {
        ...grantRequest, child_thread_id: `${childThreadId}.unknown`,
      }, 'research-unknown-child'))
      await deny(this.toolCall('rh_mission_research_read_grant', {
        ...grantRequest, source_families: ['host_files'],
      }, 'research-unknown-family'))
      await deny(this.toolCall('rh_mission_research_read_grant', {
        ...grantRequest, context: { id: 'context:not-a-research-grant-prerequisite', revision: 1 },
      }, 'research-grant-extra-context'))

      const granted = await this.callbacks.onDynamicToolCall(
        this.toolCall('rh_mission_research_read_grant', grantRequest, 'research-grant'),
        this.rootOperationContext(),
      )
      if (this.scenario.rejectGrant) {
        assert.equal(granted.success, false)
        assert.equal(this.installedHistoricalGrants.length, 0)
        await this.recordContinuation('closeout', 'research-rejected-grant-closeout')
        await this.requireSuccessfulToolCall(
          this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
        )
        return
      }
      assert.equal(granted.success, true)
      const grantText = granted.contentItems.map(({ text }: { text: string }) => text).join('\n')
      assert.doesNotMatch(grantText, /grant_id|assignment_id|[a-f0-9]{64}/i)
      assert.deepEqual(JSON.parse(granted.contentItems[0]?.text ?? '{}'), {
        status: 'completed',
        mode: 'research_read_grant',
        result: {
          child_thread_id: childThreadId,
          allowed_modes: ['usage', 'retrieve'],
          source_families: ['evidence', 'missions', 'sessions'],
          raw_body_policy: 'metadata_only',
        },
      })
      const installed = this.installedHistoricalGrants.at(-1)
      assert.ok(installed)
      assert.deepEqual(installed.allowedToolNames, [
        'rh_mission_research_read', 'rh_mission_research_read_page',
      ])
      const childContext: GoalEpochOperationContext = {
        signal: this.operationController.signal,
        rootThreadId: this.threadId,
        callerThreadId: childThreadId,
        parentThreadId: this.threadId,
        depth: 1,
        turnId: `turn.${childThreadId}`,
        status: 'active',
        grantId: installed.grantId,
        assignmentId: installed.assignmentId,
      }
      const childCall = (tool: string, args: JsonObject, suffix: string) =>
        this.callbacks.onDynamicToolCall(this.toolCallFor(childThreadId, tool, args, suffix), childContext)
      const usage = await childCall('rh_mission_research_read', {
        mode: 'usage', topics: ['reads', 'authority'],
      }, 'research-usage')
      if (this.scenario.rejectResult) {
        assert.equal(usage.success, false)
        await this.recordContinuation('closeout', 'research-rejected-result-closeout')
        await this.requireSuccessfulToolCall(
          this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
        )
        return
      }
      assert.equal(usage.success, true)
      const usagePayload = JSON.parse(usage.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(usagePayload.mode, 'usage')
      assert.equal(usagePayload.project_commit_cut, 41)
      assert.equal(Object.hasOwn(usagePayload, 'status'), false)
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission_research_read_grant', grantRequest, 'research-active-grant-replay'),
      )
      const retrieve = await childCall('rh_mission_research_read', {
        mode: 'retrieve',
        selection: {
          mode: 'read',
          purpose: 'Recover exact assigned scientific source.',
          ids: ['evidence:research-input@1'],
        },
      }, 'research-retrieve')
      assert.equal(retrieve.success, true)
      const firstPage = JSON.parse(retrieve.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(firstPage.transport, 'host_private_json_pages')
      assert.match(String(firstPage.handle), /^delegated-result\./)
      assert.equal(firstPage.complete, false)
      assert.match(String(firstPage.content), /"project_commit_cut":42/)

      await deny(this.toolCall('rh_mission_research_read', { mode: 'usage' }, 'research-root'))
      for (const [tool, args] of [
        ['rh_mission', { operation: 'record_strategy', input: {} }],
        ['rh_mission_history', { operation: 'usage', input: {} }],
        ['rh_mission_a1_review', { mode: 'usage' }],
        ['rh_formal_attempt', {}],
        ['rh_mission_research_read_grant', grantRequest],
        ['rh_mission_research_read', { mode: 'record_strategy', input: {} }],
        ['rh_mission_research_read', { mode: 'usage', operation: 'record_strategy' }],
      ] as Array<[string, JsonObject]>) {
        await deny(this.toolCallFor(childThreadId, tool, args, `research-denied-${tool}`), childContext)
      }
      for (const [label, context] of [
        ['sibling', { ...childContext, callerThreadId: `${this.threadId}.child.sibling`,
          turnId: `turn.${this.threadId}.child.sibling` }],
        ['leaf', { ...childContext, callerThreadId: `${childThreadId}.leaf`,
          parentThreadId: childThreadId, depth: 2, turnId: `turn.${childThreadId}.leaf` }],
        ['assignment', { ...childContext, assignmentId: 'research-assignment:other' }],
        ['grant', { ...childContext, grantId: '6'.repeat(64) }],
        ['ungranted', { ...childContext, grantId: null, assignmentId: null }],
      ] as Array<[string, GoalEpochOperationContext]>) {
        await deny(this.toolCallFor(context.callerThreadId, 'rh_mission_research_read',
          { mode: 'usage' }, `research-denied-${label}`), context)
        await deny(this.toolCallFor(context.callerThreadId, 'rh_mission_research_read_page',
          { handle: firstPage.handle, offset: firstPage.next_offset }, `research-page-denied-${label}`), context)
      }
      const restoreCustody = this.mutateDelegatedPageCustody?.(String(firstPage.handle))
      if (restoreCustody) {
        try {
          await deny(this.toolCallFor(childThreadId, 'rh_mission_research_read_page',
            { handle: firstPage.handle, offset: firstPage.next_offset }, 'research-page-wrong-epoch'), childContext)
        } finally {
          restoreCustody()
        }
      }
      const page = await childCall('rh_mission_research_read_page',
        { handle: firstPage.handle, offset: firstPage.next_offset }, 'research-page')
      assert.equal(page.success, true)
      const pagePath = path.join(this.runtimeDir, '.rh-mission-owner-results', `${String(firstPage.handle)}.json`)
      assert.equal(fs.existsSync(pagePath), true)
      await this.revokeLatestHistoricalGrant()
      assert.equal(fs.existsSync(pagePath), false)
      await deny(this.toolCallFor(childThreadId, 'rh_mission_research_read',
        { mode: 'usage' }, 'research-after-revoke'), childContext)
      await deny(this.toolCallFor(childThreadId, 'rh_mission_research_read_page',
        { handle: firstPage.handle, offset: firstPage.next_offset }, 'research-page-after-revoke'), childContext)
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission_research_read_grant', grantRequest, 'research-regrant'),
      )
      const freshUsage = await childCall('rh_mission_research_read', { mode: 'usage' }, 'research-after-regrant')
      assert.equal(freshUsage.success, true)
      assert.equal((JSON.parse(freshUsage.contentItems[0]?.text ?? '{}') as JsonObject).project_commit_cut, 43)
      await deny(this.toolCallFor(childThreadId, 'rh_mission_research_read_page',
        { handle: firstPage.handle, offset: firstPage.next_offset }, 'research-old-page-after-regrant'), childContext)
      await this.recordContinuation('closeout', 'research-closeout')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'research_read_mixed_role') {
      const researcher = `${this.threadId}.child.research`
      const historian = `${this.threadId}.child.history`
      this.directChildThreadIds.add(researcher)
      this.directChildThreadIds.add(historian)
      const researchRequest = {
        child_thread_id: researcher,
        assignment: 'Investigate the assigned scientific source.',
        source_families: ['evidence'],
        raw_body_policy: 'metadata_only',
      }
      const historyRequest = {
        child_thread_id: historian,
        assignment_mode: 'lifecycle_historian',
        assignment: 'Recover the exact lineage for a named scientific decision.',
        context: { id: 'context:history-scope', revision: 1 },
        source_families: ['evidence'],
        raw_body_policy: 'metadata_only',
      }
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission_research_read_grant', researchRequest, 'research-role-grant'),
      )
      const historyOnResearch = await this.callbacks.onDynamicToolCall(
        this.toolCall('rh_mission_history_grant', { ...historyRequest, child_thread_id: researcher }, 'history-on-research'),
        this.rootOperationContext(),
      )
      assert.equal(historyOnResearch.success, false)
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission_history_grant', historyRequest, 'history-role-grant'),
      )
      const researchOnHistory = await this.callbacks.onDynamicToolCall(
        this.toolCall('rh_mission_research_read_grant', { ...researchRequest, child_thread_id: historian }, 'research-on-history'),
        this.rootOperationContext(),
      )
      assert.equal(researchOnHistory.success, false)
      assert.equal(this.installedHistoricalGrants.length, 2)
      for (const installed of this.installedHistoricalGrants) {
        const isResearch = installed.childThreadId === researcher
        const afterRejection = await this.callbacks.onDynamicToolCall(
          this.toolCallFor(installed.childThreadId,
            isResearch ? 'rh_mission_research_read' : 'rh_mission_history',
            isResearch ? { mode: 'usage' } : { operation: 'usage', input: {} },
            'original-role-after-rejected-install'),
          {
            signal: this.operationController.signal, rootThreadId: this.threadId,
            callerThreadId: installed.childThreadId, parentThreadId: this.threadId, depth: 1,
            turnId: `turn.${installed.childThreadId}`, status: 'active',
            grantId: installed.grantId, assignmentId: installed.assignmentId,
          },
        )
        assert.equal(afterRejection.success, true)
      }
      await this.recordContinuation('closeout', 'research-mixed-role-closeout')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'historical_grant_mismatch') {
      const childThreadId = `${this.threadId}.child.history-mismatch`
      this.directChildThreadIds.add(childThreadId)
      const rejected = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission_history_grant',
          {
            child_thread_id: childThreadId,
            assignment_mode: 'lifecycle_historian',
            assignment: 'Test whether exact prior work changes this decision.',
            context: { id: 'context:history-mismatch', revision: 1 },
            source_families: ['strategies'],
            raw_body_policy: 'metadata_only',
          },
          'history-grant-mismatch',
        ),
        this.rootOperationContext(),
      )
      assert.equal(rejected.success, false)
      assert.equal(this.installedHistoricalGrants.length, 0)
      const deniedRead = await this.callbacks.onDynamicToolCall(
        this.toolCallFor(
          childThreadId,
          'rh_mission_history',
          { operation: 'orient', input: {} },
          'history-after-mismatched-grant',
        ),
        {
          signal: this.operationController.signal,
          rootThreadId: this.threadId,
          callerThreadId: childThreadId,
          parentThreadId: this.threadId,
          depth: 1,
          turnId: `turn.${childThreadId}`,
          status: 'active',
          grantId: 'f'.repeat(64),
          assignmentId: `historical-assignment:${'e'.repeat(64)}`,
        },
      )
      assert.equal(deniedRead.success, false)
      await this.recordContinuation('closeout', 'history-mismatch-strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'historical_advisory_race') {
      const childThreadId = `${this.threadId}.child.history-race`
      this.directChildThreadIds.add(childThreadId)
      await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission_history_grant',
          {
            child_thread_id: childThreadId,
            assignment_mode: 'historical_opportunity_scout',
            assignment: 'Search retained history for one overlooked composable route.',
            context: { id: 'context:history-race', revision: 1 },
            source_families: ['branches'],
            raw_body_policy: 'metadata_only',
          },
          'history-race-grant',
        ),
      )
      const installed = this.installedHistoricalGrants.at(-1)
      assert.ok(installed)
      const projected = await this.callbacks.onDynamicToolCall(
        this.toolCallFor(
          childThreadId,
          'rh_mission_history',
          {
            operation: 'retrieve',
            input: {
              mode: 'search',
              purpose: 'Return one large historical result for the revoke race.',
              query: 'large retained branch',
            },
          },
          'history-race-retrieve',
        ),
        {
          signal: this.operationController.signal,
          rootThreadId: this.threadId,
          callerThreadId: childThreadId,
          parentThreadId: this.threadId,
          depth: 1,
          turnId: `turn.${childThreadId}`,
          status: 'active',
          grantId: installed.grantId,
          assignmentId: installed.assignmentId,
        },
      )
      assert.equal(projected.success, false)
      await this.recordContinuation('closeout', 'history-race-strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'candidate_a1_review_grant_mismatch') {
      const childThreadId = `${this.threadId}.child.a1-review`
      this.directChildThreadIds.add(childThreadId)
      const projected = await this.callbacks.onDynamicToolCall(
        this.toolCall('rh_mission_a1_review_grant', {
          child_thread_id: childThreadId,
          assignment: 'Independently review the exact frozen complete-target claim.',
          context: { id: 'context:a1-review', revision: 1 },
          candidate_ref: {
            id: 'candidate:complete-rh',
            revision: 2,
            payload_sha256: 'a'.repeat(64),
          },
        }, 'a1-review-grant-mismatch'),
        this.rootOperationContext(),
      )
      assert.equal(projected.success, false)
      await this.recordContinuation('closeout', 'a1-review-grant-mismatch-strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'candidate_a1_review') {
      const childThreadId = `${this.threadId}.child.a1-review`
      this.directChildThreadIds.add(childThreadId)
      const exactGrantRequest = {
        child_thread_id: childThreadId,
        assignment: 'Independently review the exact frozen complete-target claim.',
        context: { id: 'context:a1-review', revision: 1 },
        candidate_ref: {
          id: 'candidate:complete-rh',
          revision: 2,
          payload_sha256: 'a'.repeat(64),
        },
      }
      const grantResult = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission_a1_review_grant',
          exactGrantRequest,
          'a1-review-grant',
        ),
      )
      const grantText = grantResult.contentItems.map(({ text }: { text: string }) => text).join('\n')
      assert.doesNotMatch(grantText, /grant_id|assignment_id|candidate-a1-review-assignment/i)
      const installed = this.installedHistoricalGrants.at(-1)
      assert.ok(installed)
      assert.deepEqual(installed.allowedToolNames, [
        'rh_mission_a1_review',
        'rh_mission_a1_review_page',
      ])
      const childContext: GoalEpochOperationContext = {
        signal: this.operationController.signal,
        rootThreadId: this.threadId,
        callerThreadId: childThreadId,
        parentThreadId: this.threadId,
        depth: 1,
        turnId: `turn.${childThreadId}`,
        status: 'active',
        grantId: installed.grantId,
        assignmentId: installed.assignmentId,
      }
      const childCall = (
        argumentsValue: Record<string, unknown>,
        suffix: string,
        callerThreadId = childThreadId,
        context: GoalEpochOperationContext = childContext,
      ): Promise<any> => this.callbacks.onDynamicToolCall(
        this.toolCallFor(callerThreadId, 'rh_mission_a1_review', argumentsValue, suffix),
        context,
      )
      const usage = await childCall({ mode: 'usage' }, 'a1-review-usage')
      assert.equal(usage.success, true)
      const usagePayload = JSON.parse(usage.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(usagePayload.mode, 'usage')
      assert.deepEqual(usagePayload.dispositions, ['invalidated', 'admission_ready'])

      const retrieval = await childCall({ mode: 'retrieve' }, 'a1-review-retrieve')
      assert.equal(retrieval.success, true)
      const retrievalPayload = JSON.parse(retrieval.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(retrievalPayload.source_closure, 'frozen_candidate_authored_exact_owner_refs')

      const siblingThreadId = `${this.threadId}.child.sibling`
      const siblingDenied = await childCall(
        { mode: 'usage' },
        'a1-review-sibling',
        siblingThreadId,
        { ...childContext, callerThreadId: siblingThreadId, turnId: `turn.${siblingThreadId}` },
      )
      assert.equal(siblingDenied.success, false)
      const historyAliasDenied = await this.callbacks.onDynamicToolCall(
        this.toolCallFor(
          childThreadId,
          'rh_mission_history',
          { operation: 'usage', input: {} },
          'a1-review-history-alias',
        ),
        childContext,
      )
      assert.equal(historyAliasDenied.success, false)

      const submission = await childCall({
        mode: 'submit',
        disposition: 'admission_ready',
        review_finding: 'Independent reconstruction leaves no material mathematical objection.',
        no_remaining_material_objection: true,
        cited_basis: [{
          id: 'evidence:review-basis',
          revision: 3,
          payload_sha256: 'e'.repeat(64),
        }],
      }, 'a1-review-submit')
      assert.equal(submission.success, true)
      const submissionPayload = JSON.parse(submission.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(submissionPayload.disposition, 'admission_ready')
      assert.equal(submissionPayload.canonical_effect, 'none')
      assert.equal(submissionPayload.public_effect, 'none')

      await this.callbacks.onDescendantToolGrantRevoked?.({
        rootThreadId: this.threadId,
        childThreadId,
        parentThreadId: this.threadId,
        depth: 1,
        grantId: installed.grantId,
        assignmentId: installed.assignmentId,
        reason: 'child_turn_completed',
      })
      const revoked = await childCall({ mode: 'usage' }, 'a1-review-after-revoke')
      assert.equal(revoked.success, false)
      await this.recordContinuation('closeout', 'a1-review-strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'complete_claim_admission') {
      const reviewerThreadId = `${this.threadId}.child.admission-reviewer`
      const admitterThreadId = `${this.threadId}.child.admission-admitter`
      this.directChildThreadIds.add(reviewerThreadId)
      this.directChildThreadIds.add(admitterThreadId)
      const opened = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission_admission_open', {
          candidate_ref: {
            id: 'candidate:complete-rh',
            revision: 2,
            payload_sha256: 'a'.repeat(64),
          },
        }, 'admission-open'),
      )
      const openedPayload = JSON.parse(opened.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(openedPayload.canonical_effect, 'none')
      assert.equal(openedPayload.public_effect, 'none')
      assert.ok(isRecordForTest(openedPayload.case_ref))
      const caseRef = openedPayload.case_ref as JsonObject

      const grant = async (
        role: 'reviewer' | 'admitter',
        childThreadId: string,
      ): Promise<GoalEpochDescendantToolGrantBinding> => {
        const result = await this.requireSuccessfulToolCall(
          this.toolCall('rh_mission_admission_grant', {
            role,
            child_thread_id: childThreadId,
            assignment: role === 'reviewer'
              ? 'Independently reconstruct the exact frozen Admission Case.'
              : 'Make the role-disjoint exact Admission decision.',
            context: { id: 'context:admission-test', revision: 1 },
            case_ref: caseRef,
          }, `admission-${role}-grant`),
        )
        const projected = result.contentItems.map(({ text }: { text: string }) => text).join('\n')
        assert.doesNotMatch(projected, /grant_id|assignment_id/i)
        const installed = this.installedHistoricalGrants.at(-1)
        assert.ok(installed)
        assert.equal(installed.childThreadId, childThreadId)
        assert.deepEqual(installed.allowedToolNames, [
          'rh_mission_admission',
          'rh_mission_admission_page',
        ])
        return installed
      }
      const callAs = (
        childThreadId: string,
        installed: GoalEpochDescendantToolGrantBinding,
        argumentsValue: Record<string, unknown>,
        suffix: string,
      ): Promise<any> => this.callbacks.onDynamicToolCall(
        this.toolCallFor(childThreadId, 'rh_mission_admission', argumentsValue, suffix),
        {
          signal: this.operationController.signal,
          rootThreadId: this.threadId,
          callerThreadId: childThreadId,
          parentThreadId: this.threadId,
          depth: 1,
          turnId: `turn.${childThreadId}`,
          status: 'active',
          grantId: installed.grantId,
          assignmentId: installed.assignmentId,
        },
      )

      const reviewGrant = await grant('reviewer', reviewerThreadId)
      const reviewUsage = await callAs(
        reviewerThreadId,
        reviewGrant,
        { mode: 'usage' },
        'admission-review-usage',
      )
      assert.equal(reviewUsage.success, true)
      const reviewRetrieval = await callAs(
        reviewerThreadId,
        reviewGrant,
        { mode: 'retrieve' },
        'admission-review-retrieve',
      )
      assert.equal(reviewRetrieval.success, true)
      const wrongRole = await callAs(
        reviewerThreadId,
        reviewGrant,
        {
          mode: 'submit',
          disposition: 'authorize_exact_delta',
          decision_basis: 'A reviewer must not be able to issue the Admission decision.',
        },
        'admission-review-wrong-role',
      )
      assert.equal(wrongRole.success, false)
      const reviewSubmission = await callAs(
        reviewerThreadId,
        reviewGrant,
        {
          mode: 'submit',
          disposition: 'no_material_objection',
          review_finding: 'Independent reconstruction found no remaining material objection.',
          cited_basis: [{
            kind: 'candidate',
            identity: 'complete-rh',
            revision: 2,
            payload_sha256: 'a'.repeat(64),
          }],
        },
        'admission-review-submit',
      )
      assert.equal(reviewSubmission.success, true)

      const decisionGrant = await grant('admitter', admitterThreadId)
      const genericReject = await callAs(
        admitterThreadId,
        decisionGrant,
        {
          mode: 'submit',
          disposition: 'reject',
          decision_basis: 'RH is extraordinary and I want more confirmation.',
        },
        'admission-decision-generic-reject',
      )
      assert.equal(genericReject.success, false)
      const rejectDecision = this.scenario.decisionDisposition === 'reject'
      const decisionRequest = rejectDecision
        ? {
            mode: 'submit',
            disposition: 'reject',
            decision_basis: 'The admitter found a fatal gap in the frozen implication.',
            objections: [{
              exact_objection: 'The final implication uses its converse without proof.',
              affected_scope: 'The frozen claim final implication.',
              materiality_basis: 'Without the missing direction, the argument does not establish RH.',
            }],
            cited_basis: [{
              kind: 'candidate',
              identity: 'complete-rh',
              revision: 2,
              payload_sha256: 'a'.repeat(64),
            }],
          }
        : {
            mode: 'submit',
            disposition: 'authorize_exact_delta',
            decision_basis: 'The frozen exact claim and role-disjoint review qualify.',
            limitations: ['Canonical mutation remains an external exact writer action.'],
          }
      const decision = await callAs(
        admitterThreadId,
        decisionGrant,
        decisionRequest,
        'admission-decision-submit',
      )
      assert.equal(decision.success, true)
      const decisionPayload = JSON.parse(decision.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(decisionPayload.disposition, rejectDecision ? 'reject' : 'authorize_exact_delta')
      assert.equal(decisionPayload.canonical_effect, 'none')
      assert.equal(decisionPayload.public_effect, 'none')

      const unrelatedThreadId = `${this.threadId}.child.unrelated`
      const unrelated = await this.callbacks.onDynamicToolCall(
        this.toolCallFor(unrelatedThreadId, 'rh_mission_admission', { mode: 'usage' }, 'admission-unrelated'),
        {
          signal: this.operationController.signal,
          rootThreadId: this.threadId,
          callerThreadId: unrelatedThreadId,
          parentThreadId: this.threadId,
          depth: 1,
          turnId: `turn.${unrelatedThreadId}`,
          status: 'active',
          grantId: decisionGrant.grantId,
          assignmentId: decisionGrant.assignmentId,
        },
      )
      assert.equal(unrelated.success, false)
      await this.recordContinuation('closeout', 'admission-strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'historical_advisory_historian') {
      const childThreadId = `${this.threadId}.child.historian`
      this.directChildThreadIds.add(childThreadId)
      const assignment = {
        materialKind: 'assignment' as const,
        content: 'Determine whether the exact retained lineage changes Strategy decision strategy:historian-pending.',
        rootThreadId: this.threadId,
        parentThreadId: this.threadId,
        childThreadId,
      }
      const assignmentCustody = await this.callbacks.onNativeMaterialObserved({
        observationId: nativeObservationIdForTest(assignment),
        ...assignment,
      }, this.rootOperationContext())
      assert.equal(assignmentCustody?.status, 'captured')
      this.nativeMaterialCustodyOutcomes.push(structuredClone(assignmentCustody!))

      await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission_history_grant',
          {
            child_thread_id: childThreadId,
            assignment_mode: 'lifecycle_historian',
            assignment: 'Determine whether the exact retained lineage changes Strategy decision strategy:historian-pending.',
            context: { id: 'context:history-historian', revision: 1 },
            source_families: ['strategies', 'candidates'],
            raw_body_policy: 'metadata_only',
          },
          'historian-grant',
        ),
      )
      const installed = this.installedHistoricalGrants.at(-1)
      assert.ok(installed)
      const childContext: GoalEpochOperationContext = {
        signal: this.operationController.signal,
        rootThreadId: this.threadId,
        callerThreadId: childThreadId,
        parentThreadId: this.threadId,
        depth: 1,
        turnId: `turn.${childThreadId}`,
        status: 'active',
        grantId: installed.grantId,
        assignmentId: installed.assignmentId,
      }
      const childCall = (
        operation: 'usage' | 'orient' | 'retrieve',
        input: Record<string, unknown>,
        suffix: string,
      ): Promise<any> => this.callbacks.onDynamicToolCall(
        this.toolCallFor(
          childThreadId,
          'rh_mission_history',
          { operation, input },
          suffix,
        ),
        childContext,
      )
      assert.equal((await childCall('usage', {}, 'historian-usage')).success, true)
      assert.equal((await childCall('orient', {}, 'historian-orient')).success, true)
      assert.equal((await childCall(
        'retrieve',
        {
          mode: 'read',
          purpose: 'Read the exact retained Strategy revision for the named pending decision.',
          ids: ['strategy:historian-prior@2'],
        },
        'historian-retrieve',
      )).success, true)

      const output = {
        materialKind: 'output' as const,
        content: 'The exact retained lineage narrows strategy:historian-pending to its surviving compatible branch.',
        rootThreadId: this.threadId,
        parentThreadId: this.threadId,
        childThreadId,
      }
      const outputCustody = await this.callbacks.onNativeMaterialObserved({
        observationId: nativeObservationIdForTest(output),
        ...output,
      }, this.rootOperationContext())
      assert.equal(outputCustody?.status, 'captured')
      this.nativeMaterialCustodyOutcomes.push(structuredClone(outputCustody!))
      await this.callbacks.onDescendantToolGrantRevoked?.({
        rootThreadId: this.threadId,
        childThreadId,
        parentThreadId: this.threadId,
        depth: 1,
        grantId: installed.grantId,
        assignmentId: installed.assignmentId,
        reason: 'child_turn_completed',
      })

      await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'interpret_material',
            input: {
              evidence_id: 'evidence:historian-decision-lineage',
              capture_scopes: [{
                captured_material_id: outputCustody!.captureRef.materialId,
                artifact_ordinal: 0,
                exact_scope: 'The complete Lifecycle Historian return for strategy:historian-pending.',
                coverage: 'complete_artifact',
              }],
              interpretation: 'The retained lineage narrows the named Strategy decision to its compatible surviving branch.',
              scope: 'Only Strategy decision strategy:historian-pending and its exact retained lineage.',
              strength: 'heuristic',
              limitations: ['The advisory return does not certify the surviving branch.'],
              significance: 'The named pending decision changes before it is resolved.',
            },
          },
          'historian-interpret',
        ),
      )
      await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_strategy',
            input: {
              mission_continuation: 'closeout',
              integrated_comparison: 'The interpreted historical lineage resolves the named pending decision narrowly.',
              causal_inputs: [{
                source: { id: 'evidence:historian-decision-lineage', revision: 1 },
                decision_consequence: 'Narrow strategy:historian-pending to the surviving compatible branch.',
              }],
              reconsideration_conditions: [{
                condition: 'Reconsider if the exact retained lineage or compatibility boundary changes.',
                owner_refs: [{ id: 'evidence:historian-decision-lineage', revision: 1 }],
              }],
            },
          },
          'historian-strategy',
        ),
      )
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'historical_advisory') {
      const childThreadId = `${this.threadId}.child.history`
      this.directChildThreadIds.add(childThreadId)
      const exactGrantRequest = {
        child_thread_id: childThreadId,
        assignment_mode: 'historical_opportunity_scout',
        assignment: 'Search retained history for one overlooked composable route.',
        context: { id: 'context:history-scout', revision: 1 },
        source_families: ['branches', 'evidence'],
        raw_body_policy: 'metadata_only',
      }
      const unknownGrant = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission_history_grant',
          { ...exactGrantRequest, child_thread_id: `${this.threadId}.child.unknown` },
          'history-grant-unknown-child',
        ),
        this.rootOperationContext(),
      )
      assert.equal(unknownGrant.success, false)

      const grantResult = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission_history_grant',
          exactGrantRequest,
          'history-grant',
        ),
      )
      const grantText = grantResult.contentItems.map(({ text }: { text: string }) => text).join('\n')
      assert.doesNotMatch(grantText, /grant_id|assignment_id|[a-f0-9]{64}/i)
      const installed = this.installedHistoricalGrants.at(-1)
      assert.ok(installed)
      assert.deepEqual(installed.allowedToolNames, [
        'rh_mission_history',
        'rh_mission_history_page',
      ])
      const childContext: GoalEpochOperationContext = {
        signal: this.operationController.signal,
        rootThreadId: this.threadId,
        callerThreadId: childThreadId,
        parentThreadId: this.threadId,
        depth: 1,
        turnId: `turn.${childThreadId}`,
        status: 'active',
        grantId: installed.grantId,
        assignmentId: installed.assignmentId,
      }
      const childCall = (
        tool: string,
        argumentsValue: Record<string, unknown>,
        suffix: string,
        context: GoalEpochOperationContext = childContext,
      ): Promise<any> => this.callbacks.onDynamicToolCall(
        this.toolCallFor(childThreadId, tool, argumentsValue, suffix),
        context,
      )

      const usage = await childCall(
        'rh_mission_history',
        { operation: 'usage', input: {} },
        'history-usage',
      )
      assert.equal(usage.success, true)
      const usagePayload = JSON.parse(usage.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(usagePayload.tool, 'rh_mission_history')
      assert.deepEqual(
        (usagePayload.operations as JsonObject[]).map(({ operation }) => operation),
        ['orient', 'retrieve'],
      )
      const orient = await childCall(
        'rh_mission_history',
        { operation: 'orient', input: {} },
        'history-orient',
      )
      assert.equal(orient.success, true)
      const retrieve = await childCall(
        'rh_mission_history',
        {
          operation: 'retrieve',
          input: {
            mode: 'search',
            purpose: 'Find one exact historical composition candidate.',
            query: 'complementary residue',
          },
        },
        'history-retrieve',
      )
      assert.equal(retrieve.success, true)
      const firstPage = JSON.parse(retrieve.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(firstPage.transport, 'host_private_json_pages')
      assert.match(String(firstPage.handle), /^delegated-result\./)

      const deny = async (
        call: any,
        context: GoalEpochOperationContext,
      ): Promise<void> => {
        const denied = await this.callbacks.onDynamicToolCall(call, context)
        assert.equal(denied.success, false)
      }
      await deny(
        this.toolCall('rh_mission_history', { operation: 'usage', input: {} }, 'root-history'),
        this.rootOperationContext(),
      )
      await deny(
        this.toolCallFor(childThreadId, 'rh_mission', { operation: 'usage', input: {} }, 'child-full'),
        childContext,
      )
      await deny(
        this.toolCallFor(childThreadId, 'rh_formal_attempt', {}, 'child-formal'),
        childContext,
      )
      await deny(
        this.toolCallFor(childThreadId, 'rh_mission_history_grant', exactGrantRequest, 'child-grant'),
        childContext,
      )
      await deny(
        this.toolCallFor(
          childThreadId,
          'rh_mission_history',
          { operation: 'record_strategy', input: {} },
          'child-write',
        ),
        childContext,
      )
      const siblingThreadId = `${this.threadId}.child.sibling`
      await deny(
        this.toolCallFor(
          siblingThreadId,
          'rh_mission_history',
          { operation: 'orient', input: {} },
          'sibling-history',
        ),
        { ...childContext, callerThreadId: siblingThreadId, turnId: `turn.${siblingThreadId}` },
      )
      const leafThreadId = `${childThreadId}.leaf`
      await deny(
        this.toolCallFor(
          leafThreadId,
          'rh_mission_history',
          { operation: 'orient', input: {} },
          'leaf-history',
        ),
        {
          ...childContext,
          callerThreadId: leafThreadId,
          parentThreadId: childThreadId,
          depth: 2,
          turnId: `turn.${leafThreadId}`,
        },
      )
      const ungrantedThreadId = `${this.threadId}.child.ungranted`
      await deny(
        this.toolCallFor(
          ungrantedThreadId,
          'rh_mission_history',
          { operation: 'orient', input: {} },
          'ungranted-history',
        ),
        {
          ...childContext,
          callerThreadId: ungrantedThreadId,
          turnId: `turn.${ungrantedThreadId}`,
          grantId: null,
          assignmentId: null,
        },
      )
      await deny(
        this.toolCallFor(
          siblingThreadId,
          'rh_mission_history_page',
          { handle: firstPage.handle, offset: firstPage.next_offset },
          'sibling-history-page',
        ),
        { ...childContext, callerThreadId: siblingThreadId, turnId: `turn.${siblingThreadId}` },
      )
      await deny(
        this.toolCallFor(
          childThreadId,
          'rh_mission_history_page',
          { handle: firstPage.handle, offset: firstPage.next_offset },
          'same-caller-wrong-grant-page',
        ),
        { ...childContext, grantId: 'a'.repeat(64) },
      )
      const restoreDelegatedPageCustody = this.mutateDelegatedPageCustody?.(
        String(firstPage.handle),
      )
      if (restoreDelegatedPageCustody) {
        try {
          await deny(
            this.toolCallFor(
              childThreadId,
              'rh_mission_history_page',
              { handle: firstPage.handle, offset: firstPage.next_offset },
              'same-caller-wrong-epoch-page',
            ),
            childContext,
          )
        } finally {
          restoreDelegatedPageCustody()
        }
      }
      const page = await childCall(
        'rh_mission_history_page',
        { handle: firstPage.handle, offset: firstPage.next_offset },
        'history-page',
      )
      assert.equal(page.success, true)
      const delegatedPagePath = path.join(
        this.runtimeDir,
        '.rh-mission-owner-results',
        `${String(firstPage.handle)}.json`,
      )
      assert.equal(fs.existsSync(delegatedPagePath), true)
      await this.callbacks.onDescendantToolGrantRevoked?.({
        rootThreadId: this.threadId,
        childThreadId,
        parentThreadId: this.threadId,
        depth: 1,
        grantId: installed.grantId,
        assignmentId: installed.assignmentId,
        reason: 'child_turn_completed',
      })
      assert.equal(fs.existsSync(delegatedPagePath), false)
      await deny(
        this.toolCallFor(
          childThreadId,
          'rh_mission_history',
          { operation: 'orient', input: {} },
          'history-after-revoke',
        ),
        childContext,
      )
      await this.recordContinuation('closeout', 'history-strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'large_read_unconsumed_usage_limited') {
      const projected = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', {
          operation: 'retrieve',
          input: { mode: 'read', purpose: 'Read the exact large owner result.', ids: ['ordinary.large'] },
        }, 'large'),
      )
      const page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
      assert.equal(page.transport, 'host_private_json_pages')
      assert.equal(page.complete, false)
      assert.equal(typeof page.handle, 'string')
      assert.equal(Number.isSafeInteger(page.next_offset), true)
      await this.onLargePageDescriptor?.({
        handle: String(page.handle),
        nextOffset: Number(page.next_offset),
        content: String(page.content),
      })
      return
    }
    if (this.scenario.kind === 'large_read_final_page_unacknowledged_usage_limited') {
      let projected = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', {
          operation: 'retrieve',
          input: { mode: 'read', purpose: 'Read the exact large owner result.', ids: ['ordinary.large'] },
        }, 'large'),
      )
      let page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
      assert.equal(page.transport, 'host_private_json_pages')
      while (page.complete !== true) {
        const offset = Number(page.next_offset)
        projected = await this.requireSuccessfulToolCall(
          this.toolCall(
            'rh_mission_page',
            { handle: page.handle, offset },
            `unacknowledged-page.${String(offset)}`,
          ),
        )
        page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
        if (page.complete === true) {
          await this.onFinalPageReplay?.({
            handle: String(page.handle),
            offset,
            projectedText: String(projected.contentItems[0].text),
          })
        }
      }
      return
    }
    if (this.scenario.kind === 'core_cancel_dynamic') {
      const controller = new AbortController()
      const pending = this.callbacks.onDynamicToolCall(
        this.toolCall('rh_mission', {
          operation: 'retrieve',
          input: { mode: 'read', purpose: 'Read the exact large owner result.', ids: ['ordinary.large'] },
        }, 'cancel'),
        this.rootOperationContext(controller.signal),
      )
      controller.abort('core_usage_limited')
      const cancelled = await pending
      assert.equal(cancelled.success, false)
      assert.match(cancelled.contentItems[0]?.text ?? '', /explicitly cancelled/i)
      return
    }
    if (this.scenario.kind === 'core_cancel_material') {
      const controller = new AbortController()
      const pending = this.callbacks.onNativeMaterialObserved({
        observationId: `output.${this.threadId}.cancelled`,
        materialKind: 'output',
        content: 'large native result whose owner capture is still running',
        rootThreadId: this.threadId,
        parentThreadId: this.threadId,
        childThreadId: `${this.threadId}.child.cancelled`,
      }, this.rootOperationContext(controller.signal))
      controller.abort('core_fatal_fence')
      await assert.rejects(pending, MissionBridgeCancelledError)
      return
    }
    if (this.scenario.kind === 'large_read_tamper') {
      const projected = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', {
          operation: 'retrieve',
          input: { mode: 'read', purpose: 'Read the exact large owner result.', ids: ['ordinary.large'] },
        }, 'large'),
      )
      const page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
      assert.equal(page.transport, 'host_private_json_pages')
      await this.onLargePageDescriptor?.({
        handle: String(page.handle),
        nextOffset: Number(page.next_offset),
        content: String(page.content),
      })
      const tampered = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission_page',
          { handle: page.handle, offset: page.next_offset },
          'tampered-page',
        ),
        this.rootOperationContext(),
      )
      assert.equal(tampered.success, false)
      this.tamperFailureText = tampered.contentItems.map(({ text }) => text).join('\n')
      await this.recordContinuation('closeout', 'strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'resume_large_read') {
      const retained = this.retainedLargePage()
      assert.ok(retained)
      let encoded = retained.content
      let offset = retained.nextOffset
      for (;;) {
        const projected = await this.requireSuccessfulToolCall(
          this.toolCall(
            'rh_mission_page',
            { handle: retained.handle, offset },
            `resumed-page.${String(offset)}`,
          ),
        )
        const page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
        encoded += String(page.content)
        if (page.complete === true) {
          break
        }
        offset = Number(page.next_offset)
      }
      this.largeReadResult = JSON.parse(encoded) as JsonObject
      await this.recordContinuation('closeout', 'strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'resume_final_page_replay') {
      const retained = this.retainedFinalPageReplay()
      assert.ok(retained)
      const projected = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission_page',
          { handle: retained.handle, offset: retained.offset },
          `replayed-final-page.${String(retained.offset)}`,
        ),
      )
      assert.equal(projected.contentItems[0].text, retained.projectedText)
      const page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
      assert.equal(page.complete, true)
      await this.recordContinuation('closeout', 'strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'large_read') {
      let projected = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', {
          operation: 'retrieve',
          input: { mode: 'read', purpose: 'Read the exact large owner result.', ids: ['ordinary.large'] },
        }, 'large'),
      )
      let page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
      let encoded = ''
      if (page.transport === 'host_private_json_pages') {
        for (;;) {
          encoded += String(page.content)
          if (page.complete === true) {
            break
          }
          projected = await this.requireSuccessfulToolCall(
            this.toolCall(
              'rh_mission_page',
              { handle: page.handle, offset: page.next_offset },
              `page.${String(page.next_offset)}`,
            ),
          )
          page = JSON.parse(projected.contentItems[0].text) as Record<string, unknown>
        }
      } else {
        encoded = projected.contentItems[0].text
      }
      this.largeReadResult = JSON.parse(encoded) as JsonObject
      await this.recordContinuation('closeout', 'strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'formal_attempt') {
      const projected = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_formal_attempt',
          {
            selected_bet_sha256: 'f'.repeat(64),
            correction_basis: null,
          },
          'formal-attempt',
        ),
      )
      const factual = JSON.parse(projected.contentItems[0].text) as JsonObject
      assert.equal(factual.session_id, 'session.formal.test')
      assert.equal(factual.attempt_id, 'attempt.formal.test')
      await this.recordContinuation('closeout', 'strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'formal_attempt_failed_continue') {
      const projected = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_formal_attempt',
          {
            selected_bet_sha256: 'f'.repeat(64),
            correction_basis: null,
          },
          'formal-attempt-failed',
        ),
      )
      const factual = JSON.parse(projected.contentItems[0].text) as JsonObject
      assert.equal(factual.attempt_state, 'failed')
      assert.equal(factual.session_is_terminal, false)
      assert.equal(factual.result_bound_to_session, false)
      await this.recordContinuation('continue', 'formal-failure-local')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'real_formal_lifecycle') {
      const contextCall = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_context',
            input: {
              context_id: 'context:host-cross-language-formal',
              subject: 'One deterministic construction for the integrated Host formal lifecycle.',
              question: 'Does the deterministic formal fixture complete through the owner boundary?',
              material: [
                {
                  id: 'mission:mission.rh.public.1',
                  why: 'The Mission owns the exact integrated lifecycle scope.',
                },
              ],
            },
          },
          'real-formal-context',
        ),
      )
      const contextPayload = JSON.parse(contextCall.contentItems[0].text) as JsonObject
      assert.equal(contextPayload.status, 'completed')
      const contextId = String((contextPayload.result as JsonObject).record_id)
      const strategyCall = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_strategy',
            input: {
              mission_continuation: 'continue',
              integrated_comparison: 'The deterministic formal request is the exact next discriminator.',
              selected_bets: [
                {
                  bet: 'Execute the deterministic integrated formal verification.',
                  discriminator: 'The real Python owner returns one terminal factual reconciliation.',
                  formal_request: {
                    purpose: 'targeted_verification',
                    context: { id: contextId },
                  },
                },
              ],
              reconsideration_conditions: [
                { condition: 'Reconsider after the formal reconciliation is retained.' },
              ],
            },
          },
          'real-formal-strategy',
        ),
      )
      const strategyPayload = JSON.parse(strategyCall.contentItems[0].text) as JsonObject
      assert.equal(strategyPayload.status, 'completed')
      const formalRequests = (strategyPayload.result as JsonObject).formal_requests as JsonObject[]
      assert.equal(formalRequests.length, 1)
      const selectedBetSha256 = String(formalRequests[0]?.selected_bet_sha256)
      assert.match(selectedBetSha256, /^[0-9a-f]{64}$/)
      const formalCall = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_formal_attempt',
          { selected_bet_sha256: selectedBetSha256, correction_basis: null },
          'real-formal-attempt',
        ),
        this.rootOperationContext(),
      )
      if (!formalCall.success) {
        this.realFormalFailure = formalCall.contentItems.map(({ text }) => text).join('\n')
        throw new Error(`real formal lifecycle call failed: ${this.realFormalFailure}`)
      }
      this.realFormalReconciliation = JSON.parse(formalCall.contentItems[0].text) as JsonObject
      assert.equal(this.realFormalReconciliation.schema, 'mr.formal_attempt_reconciliation.v1')
      assert.equal(this.realFormalReconciliation.attempt_state, 'succeeded')
      assert.equal(this.realFormalReconciliation.provider_effect_certainty, 'known')
      assert.equal(this.realFormalReconciliation.session_is_terminal, true)
      assert.equal(this.realFormalReconciliation.result_bound_to_session, true)
      assert.equal(isRecordForTest(this.realFormalReconciliation.raw_capture), true)
      const captureId = String(
        (this.realFormalReconciliation.raw_capture as JsonObject).capture_id,
      )
      const descriptorCall = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'retrieve',
            input: {
              mode: 'read',
              purpose: 'Inspect the exact formal capture before interpretation.',
              ids: [`capture:${captureId}`],
            },
          },
          'real-formal-capture-descriptor',
        ),
      )
      const descriptorEnvelope = JSON.parse(
        descriptorCall.contentItems[0].text,
      ) as JsonObject
      const descriptorItems = (descriptorEnvelope.result as JsonObject).items as JsonObject[]
      assert.equal(descriptorItems.length, 1)
      const captureDescriptor = JSON.parse(
        String(descriptorItems[0]?.readable_content),
      ) as JsonObject
      const artifacts = captureDescriptor.artifacts as JsonObject[]
      assert.equal(artifacts.length, 2)
      assert.equal(artifacts[0]?.role, 'accepted_output')
      assert.equal(artifacts[0]?.logical_name, 'output/formal-result.txt')
      assert.equal(artifacts[1]?.role, 'evidence_only')
      assert.equal(artifacts[1]?.logical_name, 'scratch/app-server-events.jsonl')
      const artifactHandle = `capture-artifact:${captureId}#0`
      const artifactCall = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'retrieve',
            input: {
              mode: 'read',
              purpose: 'Read the exact accepted formal result.',
              ids: [artifactHandle],
            },
          },
          'real-formal-artifact',
        ),
      )
      const artifactEnvelope = JSON.parse(artifactCall.contentItems[0].text) as JsonObject
      const artifactItems = (artifactEnvelope.result as JsonObject).items as JsonObject[]
      assert.equal(artifactItems.length, 1)
      const transfer = JSON.parse(String(artifactItems[0]?.readable_content)) as JsonObject
      assert.equal(transfer.representation, 'text')
      assert.equal(transfer.content, 'Deterministic formal fixture result.\n')
      const quarantinedCall = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'retrieve',
            input: {
              mode: 'read',
              purpose: 'Confirm the operational sibling stays exact-local.',
              ids: [`capture-artifact:${captureId}#1`],
            },
          },
          'real-formal-quarantined-sibling',
        ),
        this.rootOperationContext(),
      )
      assert.equal(quarantinedCall.success, true)
      const quarantinedPayload = JSON.parse(
        quarantinedCall.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      const unavailableItems = (quarantinedPayload.result as JsonObject).items as JsonObject[]
      assert.equal(unavailableItems.length, 1)
      assert.equal(unavailableItems[0]!.requested_id, `capture-artifact:${captureId}#1`)
      assert.equal(unavailableItems[0]!.status, 'unavailable')
      assert.equal(unavailableItems[0]!.reason, 'quarantined')
      assert.match(String(unavailableItems[0]!.correction), /select other available material deliberately/)
      const interpretation = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'interpret_material',
            input: {
              evidence_id: 'evidence:host-cross-language-formal',
              capture_scopes: [
                {
                  captured_material_id: `capture:${captureId}`,
                  artifact_ordinal: 0,
                  exact_scope: 'The complete deterministic accepted formal result.',
                  coverage: 'complete_artifact',
                },
              ],
              interpretation: 'The deterministic formal fixture returned its exact declared result.',
              scope: 'Only the integrated deterministic formal lifecycle fixture.',
              strength: 'exact_finite_identity',
              limitations: ['This fixture establishes transport composition, not RH.'],
              significance: 'The accepted formal result is retrievable and interpretable before checkpoint.',
            },
          },
          'real-formal-interpretation',
        ),
      )
      const interpretationEnvelope = JSON.parse(
        interpretation.contentItems[0].text,
      ) as JsonObject
      assert.equal(interpretationEnvelope.status, 'completed')
      await this.recordContinuation('continue', 'real-formal-continuation')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'corrective_error') {
      for (const hostileOperation of ['__proto__', 'toString']) {
        const hostile = await this.callbacks.onDynamicToolCall(
          this.toolCall(
            'rh_mission',
            { operation: hostileOperation, input: {} },
            `hostile-operation.${hostileOperation}`,
          ),
          this.rootOperationContext(),
        )
        assert.equal(hostile.success, false)
        const hostilePayload = JSON.parse(hostile.contentItems[0]?.text ?? '{}') as JsonObject
        assert.deepEqual((hostilePayload.correction as JsonObject).usage_call, {
          operation: 'usage',
          input: {},
        })
      }
      const hostileUsage = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          { operation: 'usage', input: { for_operation: '__proto__' } },
          'hostile-usage-selector',
        ),
        this.rootOperationContext(),
      )
      assert.equal(hostileUsage.success, false)
      const hostileUsagePayload = JSON.parse(
        hostileUsage.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      assert.equal((hostileUsagePayload.error as JsonObject).code, 'mission_usage_request_invalid')
      const unsupported = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          { operation: 'invented_operation', input: {} },
          'unsupported-operation',
        ),
        this.rootOperationContext(),
      )
      assert.equal(unsupported.success, false)
      const unsupportedPayload = JSON.parse(
        unsupported.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      assert.deepEqual((unsupportedPayload.correction as JsonObject).usage_call, {
        operation: 'usage',
        input: {},
      })
      assert.deepEqual(
        (unsupportedPayload.correction as JsonObject).allowed_semantic_operations,
        [...SEMANTIC_OPERATIONS],
      )
      const rejected = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_candidate',
            input: {
              candidate_id: 'candidate:missing-standing-basis',
              proposal_kind: 'lemma',
              exact_statement: 'A bounded candidate statement.',
              standing: { status: 'open', basis: '' },
            },
          },
          'corrective-error',
        ),
        this.rootOperationContext(),
      )
      assert.equal(rejected.success, false)
      const payload = JSON.parse(rejected.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal((payload.correction as JsonObject).location, '$.input.standing.basis')
      assert.deepEqual((payload.correction as JsonObject).usage_call, {
        operation: 'usage',
        input: { for_operation: 'record_candidate' },
      })
      assert.equal(isRecordForTest((payload.correction as JsonObject).input_schema), true)
      assert.equal(Array.isArray((payload.correction as JsonObject).examples), true)
      const semanticRejected = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_context',
            input: {
              context_id: 'context:semantic-rejection',
              subject: 'One exact tool-handshake regression.',
              question: 'Does a semantic rejection teach the exact correction?',
              material: [{ id: 'evidence:current', why: 'It is the current exact ground.' }],
            },
          },
          'semantic-rejection',
        ),
        this.rootOperationContext(),
      )
      assert.equal(semanticRejected.success, false)
      const semanticPayload = JSON.parse(
        semanticRejected.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      const semanticCorrection = semanticPayload.correction as JsonObject
      assert.deepEqual(semanticCorrection.usage_call, {
        operation: 'usage',
        input: { for_operation: 'record_context' },
      })
      assert.equal(isRecordForTest(semanticCorrection.input_schema), true)
      assert.equal(Array.isArray(semanticCorrection.examples), true)
      const semanticOwnerErrors = (semanticPayload.error as JsonObject).owner_errors
      assert.ok(Array.isArray(semanticOwnerErrors) && isRecordForTest(semanticOwnerErrors[0]))
      assert.match(String(semanticOwnerErrors[0].message), /^\$\.input\.context_id:/)
      assert.equal(semanticCorrection.location, '$.input.context_id')
      assert.deepEqual(semanticCorrection.owner_directive, {
        code: 'mission_operation_request_invalid',
        property: 'request_shape',
        failure_scope: 'call',
        correction: 'revise_request',
      })
      assert.match(String(semanticCorrection.instruction), /correct the exact rejected field/i)
      const stateConflict = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_context',
            input: {
              context_id: 'context:state-conflict',
              subject: 'One stale owner judgment.',
              question: 'Does the current owner still support it?',
              material: [{ id: 'evidence:current', why: 'It was the prior current ground.' }],
            },
          },
          'state-conflict',
        ),
        this.rootOperationContext(),
      )
      assert.equal(stateConflict.success, false)
      const statePayload = JSON.parse(
        stateConflict.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      const stateCorrection = statePayload.correction as JsonObject
      assert.equal(
        (stateCorrection.owner_directive as JsonObject).correction,
        'refresh_then_rejudge_if_semantics_changed',
      )
      assert.match(String(stateCorrection.instruction), /Run orient.*rejudge/i)
      assert.equal(Object.hasOwn(stateCorrection, 'usage_call'), false)
      assert.equal(Object.hasOwn(stateCorrection, 'input_schema'), false)
      assert.equal(Object.hasOwn(stateCorrection, 'examples'), false)
      const unavailable = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_context',
            input: {
              context_id: 'context:owner-unavailable',
              subject: 'One preserved judgment.',
              question: 'Can it be written now?',
              material: [{ id: 'evidence:current', why: 'It is exact preserved ground.' }],
            },
          },
          'owner-unavailable',
        ),
        this.rootOperationContext(),
      )
      assert.equal(unavailable.success, false)
      const unavailablePayload = JSON.parse(
        unavailable.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      const unavailableCorrection = unavailablePayload.correction as JsonObject
      assert.equal(
        (unavailableCorrection.owner_directive as JsonObject).correction,
        'repair_owner_interface',
      )
      assert.match(String(unavailableCorrection.instruction), /Do not retry.*unavailable/i)
      assert.equal(Object.hasOwn(unavailableCorrection, 'usage_call'), false)
      assert.equal(Object.hasOwn(unavailableCorrection, 'input_schema'), false)
      assert.equal(Object.hasOwn(unavailableCorrection, 'examples'), false)
      const partialSynthesis = await this.callbacks.onDynamicToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'synthesize',
            input: {
              relation_question: 'Exercise one accepted and one rejected consequence.',
              inputs: [
                { id: 'evidence:first', role: 'premise' },
                { id: 'candidate:second', role: 'comparison' },
              ],
              compatibility_analysis: 'The fixture exercises local consequence handling.',
              derivation_or_incompatibility: 'Only the first fixture consequence survives.',
              scope: 'The exact fixture only.',
              strength: 'heuristic',
              edge_survival: 'The accepted local consequence survives.',
            },
          },
          'partial-synthesis',
        ),
        this.rootOperationContext(),
      )
      assert.equal(partialSynthesis.success, true)
      const partialPayload = JSON.parse(
        partialSynthesis.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      assert.equal(partialPayload.status, 'completed')
      const partialResult = partialPayload.result as JsonObject
      assert.equal((partialResult.records as unknown[]).length, 1)
      assert.equal((partialResult.rejections as unknown[]).length, 1)
      await this.recordContinuation('closeout', 'strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'semantic_stop') {
      if (String(this.request?.objective).includes('final Strategy closeout')) {
        const specialistDenied = await this.callbacks.onDynamicToolCall(
          this.toolCall('rh_mission_admission_open', {
            candidate_ref: {
              id: 'candidate:must-remain-unavailable-in-closeout',
              revision: 1,
              payload_sha256: 'a'.repeat(64),
            },
          }, 'closeout-admission-denied'),
          this.rootOperationContext(),
        )
        assert.equal(specialistDenied.success, false)
        const researchDenied = await this.callbacks.onDynamicToolCall(
          this.toolCall('rh_mission', {
            operation: 'retrieve',
            input: {
              mode: 'search',
              purpose: 'This research read must be unavailable in closeout.',
              query: 'unavailable closeout research',
            },
          }, 'closeout-retrieve-denied'),
          this.rootOperationContext(),
        )
        assert.equal(researchDenied.success, false)
      }
      await this.recordContinuation('closeout', 'strategy')
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind === 'candidate_a1') {
      const syntheticComponents = [
        {
          proposition: 'P',
          evidenceId: 'evidence:synthetic-proof-p',
          digestSha256: 'a'.repeat(64),
          childThreadId: `${this.threadId}.child.synthetic-p`,
        },
        {
          proposition: 'Q',
          evidenceId: 'evidence:synthetic-proof-q',
          digestSha256: 'b'.repeat(64),
          childThreadId: `${this.threadId}.child.synthetic-q`,
        },
      ] as const
      const supportingRefs: JsonObject[] = []
      for (const component of syntheticComponents) {
        const output = {
          materialKind: 'output' as const,
          content: `Synthetic Recognition fixture only: a child purports to prove ${component.proposition} under the fixture normalization.`,
          rootThreadId: this.threadId,
          parentThreadId: this.threadId,
          childThreadId: component.childThreadId,
        }
        const custody = await this.callbacks.onNativeMaterialObserved({
          observationId: nativeObservationIdForTest(output),
          ...output,
        }, this.rootOperationContext())
        if (!custody || custody.status !== 'captured') {
          throw new Error('synthetic distributed Recognition fixture lost exact child custody')
        }
        this.nativeMaterialCustodyOutcomes.push(structuredClone(custody))
        const interpretation = await this.requireSuccessfulToolCall(
          this.toolCall(
            'rh_mission',
            {
              operation: 'interpret_material',
              input: {
                evidence_id: component.evidenceId,
                capture_scopes: [{
                  captured_material_id: custody.captureRef.materialId,
                  artifact_ordinal: 0,
                  exact_scope: `The complete synthetic child output purporting to prove ${component.proposition}.`,
                  coverage: 'complete_artifact',
                }],
                interpretation: `The child output purports to establish synthetic proposition ${component.proposition}.`,
                scope: 'Only the deliberately synthetic Recognition regression fixture.',
                strength: 'purported_deduction',
                limitations: ['This fixture is not mathematical evidence about the actual Riemann Hypothesis.'],
                significance: `Synthetic proposition ${component.proposition} is one premise of the pre-existing conditional Evidence C.`,
              },
            },
            `synthetic-interpret-${component.proposition.toLowerCase()}`,
          ),
        )
        const interpretationEnvelope = JSON.parse(
          interpretation.contentItems[0]?.text ?? '{}',
        ) as JsonObject
        const exactEvidence = interpretationEnvelope.result as JsonObject
        assert.equal(exactEvidence.record_id, component.evidenceId)
        assert.equal(exactEvidence.revision, 1)
        assert.equal(exactEvidence.payload_sha256, component.digestSha256)
        supportingRefs.push({
          kind: 'evidence',
          id: exactEvidence.record_id,
          revision: exactEvidence.revision,
          digest_sha256: exactEvidence.payload_sha256,
        })
      }
      const conditionalRead = await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'retrieve',
            input: {
              mode: 'read',
              purpose: 'Read the exact pre-existing synthetic conditional Evidence C used by the combined-reach fixture.',
              ids: ['evidence:synthetic-conditional-rh@7'],
            },
          },
          'synthetic-conditional-read',
        ),
      )
      const conditionalEnvelope = JSON.parse(
        conditionalRead.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      const conditionalItems = (conditionalEnvelope.result as JsonObject).items as JsonObject[]
      const exactConditional = conditionalItems[0]
      assert.equal(exactConditional?.record_id, 'evidence:synthetic-conditional-rh')
      assert.equal(exactConditional?.revision, 7)
      assert.equal(exactConditional?.payload_sha256, 'c'.repeat(64))
      const conditionalRef = {
        kind: 'evidence',
        id: exactConditional.record_id,
        revision: exactConditional.revision,
        digest_sha256: exactConditional.payload_sha256,
      }
      supportingRefs.push(conditionalRef)
      const synthesisRoles = [
        'purported premise P',
        'purported premise Q',
        'pre-existing conditional P and Q implies RH',
      ]
      await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'synthesize',
            input: {
              relation_question: 'Do synthetic child results P and Q make the exact pre-existing conditional Evidence C applicable?',
              inputs: supportingRefs.map(({ id, revision }, index) => ({
                id,
                role: synthesisRoles[index],
                revision,
              })),
              compatibility_analysis: 'The three synthetic owner meanings use the same fixture hypotheses, domain, and normalization.',
              derivation_or_incompatibility: 'Purported P and purported Q discharge the two premises of the pre-existing synthetic conditional C.',
              scope: 'Only the deliberately synthetic distributed-recognition fixture.',
              strength: 'purported_deduction',
              limitations: ['This synthesis is a Host regression and makes no real RH claim.'],
              non_inferences: ['No actual theorem about RH is established by synthetic fixture text.'],
              edge_survival: 'The exact P, Q, and C references remain the basis of the proposed composition.',
            },
          },
          'synthetic-distributed-synthesis',
        ),
      )
      await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_candidate',
            input: {
              candidate_id: 'candidate:synthetic-distributed-rh',
              proposal_kind: 'proof_architecture',
              exact_statement: 'Synthetic fixture only: the exact purported proofs of P and Q together with pre-existing conditional Evidence C purport to prove the Riemann Hypothesis.',
              scope_and_reach: 'A synthetic complete-target proof claim formed only by the combined reach of exact A, B, and C owner meanings.',
              complete_target_claim: {
                target: 'riemann_hypothesis',
                disposition: 'proof',
              },
              argument_edges: [{
                edge_id: 'synthetic-composition-p-q-c',
                edge_kind: 'composition',
                premises: [
                  'Synthetic Evidence A purports to prove P.',
                  'Synthetic Evidence B purports to prove Q.',
                  'Pre-existing synthetic Evidence C states P and Q implies RH.',
                ],
                conclusion: 'The synthetic fixture purports to prove RH.',
                authority: {
                  class: 'evidence_interpretation',
                  refs: supportingRefs.map(({ id, revision }) => ({ id, revision })),
                },
              }],
              supporting_refs: supportingRefs,
              limitations: ['Synthetic test material is not evidence about actual RH.'],
              non_inferences: ['This regression does not establish RH or canonical mathematical truth.'],
              standing: {
                status: 'open',
                basis: 'The combined synthetic claim is unverified and requires independent reconstruction.',
              },
            },
          },
          'candidate-a1',
        ),
      )
      await this.requireSuccessfulToolCall(
        this.toolCall(
          'rh_mission',
          {
            operation: 'record_strategy',
            input: {
              mission_continuation: 'continue',
              integrated_comparison: 'The exact synthetic distributed Candidate requires focused reconstruction and falsification.',
              causal_inputs: [{
                source: { id: 'candidate:synthetic-distributed-rh', revision: 1 },
                decision_consequence: 'Pivot the affected work from broad exploration to focused verification of this exact Candidate.',
              }],
              selected_bets: [{
                bet: 'Independently reconstruct and attack the exact synthetic distributed Candidate.',
                discriminator: 'Either preserve a concrete defect or determine that no material objection remains.',
                owner_refs: [{ id: 'candidate:synthetic-distributed-rh', revision: 1 }],
              }],
              reconsideration_conditions: [{
                condition: 'Reconsider after independent reconstruction identifies a concrete defect or clears every material objection.',
                owner_refs: [{ id: 'candidate:synthetic-distributed-rh', revision: 1 }],
              }],
            },
          },
          'candidate-a1-strategy',
        ),
      )
      await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'checkpoint', input: {} }, 'checkpoint'),
      )
      return
    }
    if (this.scenario.kind !== 'checkpoint') {
      return
    }
    const usageIndex = await this.requireSuccessfulToolCall(
      this.toolCall('rh_mission', { operation: 'usage', input: {} }, 'usage-index'),
    )
    const usageIndexPayload = JSON.parse(usageIndex.contentItems[0]?.text ?? '{}') as JsonObject
    assert.equal(usageIndexPayload.semantic_operation_count, SEMANTIC_OPERATIONS.length)
    assert.deepEqual(
      (usageIndexPayload.operations as JsonObject[]).map(({ operation }) => operation),
      [...SEMANTIC_OPERATIONS],
    )
    const retrieveGuide = await this.requireSuccessfulToolCall(
      this.toolCall(
        'rh_mission',
        { operation: 'usage', input: { for_operation: 'retrieve' } },
        'usage-retrieve',
      ),
    )
    const retrieveGuidePayload = JSON.parse(retrieveGuide.contentItems[0]?.text ?? '{}') as JsonObject
    assert.equal(retrieveGuidePayload.operation, 'retrieve')
    assert.equal(isRecordForTest(retrieveGuidePayload.input_schema), true)
    assert.equal(Array.isArray(retrieveGuidePayload.examples), true)
    for (let index = 0; index < this.scenario.workerCount; index += 1) {
      const childThreadId = `${this.threadId}.child.${index}`
      const assignment = {
        materialKind: 'assignment',
        content: `Investigate useful RH route ${index}`,
        rootThreadId: this.threadId,
        parentThreadId: this.threadId,
        childThreadId,
      } as const
      const assignmentOutcome = await this.callbacks.onNativeMaterialObserved({
        observationId: nativeObservationIdForTest(assignment),
        ...assignment,
      }, this.rootOperationContext())
      if (assignmentOutcome) {
        this.nativeMaterialCustodyOutcomes.push(structuredClone(assignmentOutcome))
      }
      const output = {
        materialKind: 'output',
        content: `Mathematically useful result ${index}`,
        rootThreadId: this.threadId,
        parentThreadId: this.threadId,
        childThreadId,
      } as const
      this.callbacks.onExecutionObservation({
        identity: {
          root_thread_id: this.threadId, thread_id: childThreadId,
          parent_thread_id: this.threadId, turn_id: `turn.${childThreadId}`,
          item_id: `output.${childThreadId}`, operation_id: null,
        },
        source_method: 'item/completed', received_at: new Date().toISOString(),
        source_time: null, phase: 'received', caused_by: null,
        kind: 'message', summary: 'Fixture worker returned output', details: {},
        contents: [{
          content_key: `output.${childThreadId}`, field: 'message', text: output.content,
          update_mode: 'snapshot', complete: true,
        }],
      })
      const outputOutcome = await this.callbacks.onNativeMaterialObserved({
        observationId: nativeObservationIdForTest(output),
        ...output,
      }, this.rootOperationContext())
      if (outputOutcome) {
        this.nativeMaterialCustodyOutcomes.push(structuredClone(outputOutcome))
      }
    }
    const usesCaptureRecovery =
      this.scenario.recoverRejectedNativeMaterial ||
      this.scenario.attemptPredecessorCaptureRecovery
    if (usesCaptureRecovery) {
      const recoveryNotice = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'usage', input: {} }, 'capture-recovery'),
      )
      const recoveryPayload = JSON.parse(
        recoveryNotice.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      assert.equal(Array.isArray(recoveryPayload.capture_recovery_required), true)
    }
    const predecessorAdoptedRootMaterial: JsonObject | null =
      this.scenario.attemptPredecessorCaptureRecovery === 'omitted_lineage'
        ? {
            channel: 'native_assignment',
            content: 'Investigate useful RH route 0',
          }
        : this.scenario.attemptPredecessorCaptureRecovery === 'wrong_channel'
          ? {
              channel: 'native_output',
              content: 'Investigate useful RH route 0',
              native_lineage: {
                material_kind: 'assignment',
                parent_thread_id: 'thread.0',
                child_thread_id: 'thread.0.child.0',
              },
            }
          : this.scenario.attemptPredecessorCaptureRecovery === 'exact'
            ? {
                channel: 'native_assignment',
                content: 'Investigate useful RH route 0',
                native_lineage: {
                  material_kind: 'assignment',
                  parent_thread_id: 'thread.0',
                  child_thread_id: 'thread.0.child.0',
                },
              }
            : null
    const captureScopes: JsonObject[] = this.scenario.recoverRejectedNativeMaterial
      ? [
          {
            adopted_root_material: {
              channel: 'native_assignment',
              content: 'Investigate useful RH route 0',
              native_lineage: {
                material_kind: 'assignment',
                parent_thread_id: this.threadId,
                child_thread_id: `${this.threadId}.child.0`,
              },
            },
            exact_scope: 'The complete returned worker assignment.',
            coverage: 'complete_artifact',
          },
        ]
      : this.scenario.attemptPredecessorCaptureRecovery
        ? [
            {
              adopted_root_material: predecessorAdoptedRootMaterial!,
              exact_scope: 'The complete returned predecessor worker assignment.',
              coverage: 'complete_artifact',
            },
          ]
      : [
          {
            captured_material_id: 'raw-capture:useful-worker-material',
            artifact_ordinal: 0,
            exact_scope: 'The complete returned worker result.',
            coverage: 'complete_artifact',
          },
        ]
    const interpretationCall = this.toolCall(
      'rh_mission',
      {
        operation: 'interpret_material',
        input: {
          evidence_id: 'evidence:useful-worker-material',
          capture_scopes: captureScopes,
          interpretation: 'The captured result advances the exact assigned mathematical route.',
          scope: 'The exact returned worker result.',
          strength: 'heuristic',
          limitations: ['No broader theorem is inferred by this test.'],
          significance: 'The result is relevant to the current Strategy decision.',
        },
      },
      'interpret',
    )
    if (this.scenario.attemptPredecessorCaptureRecovery === 'exact') {
      const rejected = await this.callbacks.onDynamicToolCall(
        interpretationCall,
        this.rootOperationContext(),
      )
      assert.equal(rejected.success, false)
      const rejectedPayload = JSON.parse(
        rejected.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      assert.equal(rejectedPayload.status, 'error')
      assert.match(
        String((rejectedPayload.error as JsonObject).message),
        /predecessor Goal or Executive Epoch/,
      )
      assert.equal(Array.isArray(rejectedPayload.capture_recovery_required), true)
      this.rootToolResultContentItemCounts.push(rejected.contentItems.length)
      this.rootToolResultPayloads.push(rejectedPayload)
    } else {
      await this.requireSuccessfulToolCall(interpretationCall)
    }
    if (this.scenario.recoverRejectedNativeMaterial) {
      const recovered = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'usage', input: {} }, 'capture-recovered'),
      )
      const recoveredPayload = JSON.parse(recovered.contentItems[0]?.text ?? '{}') as JsonObject
      assert.equal(Object.hasOwn(recoveredPayload, 'capture_recovery_required'), false)
    }
    if (this.scenario.attemptPredecessorCaptureRecovery) {
      const unresolved = await this.requireSuccessfulToolCall(
        this.toolCall('rh_mission', { operation: 'usage', input: {} }, 'capture-unresolved'),
      )
      const unresolvedPayload = JSON.parse(
        unresolved.contentItems[0]?.text ?? '{}',
      ) as JsonObject
      assert.equal(Array.isArray(unresolvedPayload.capture_recovery_required), true)
    }
    if (this.scenario.leaveWorkspaceNonempty) {
      await fs.promises.writeFile(
        path.join(this.workspaceRoot, 'retained-owner-material.txt'),
        'The Host must preserve this nonempty terminal workspace.\n',
        { encoding: 'utf8', flag: 'wx' },
      )
    }
    await this.recordContinuation(this.scenario.missionContinuation, 'strategy')
    await this.requireSuccessfulToolCall(
      this.toolCall(
        'rh_mission',
        { operation: 'checkpoint', input: {} },
        'checkpoint',
      ),
    )
  }

  private state(status: string, activeTurnIdOverride?: string | null): any {
    const activeTurnId = activeTurnIdOverride === undefined
      ? status !== 'complete' && status !== 'blocked'
        ? `turn.${this.threadId}`
        : null
      : activeTurnIdOverride
    return {
      goal: {
        threadId: this.threadId,
        objective: String(this.request?.objective ?? MISSION_OBJECTIVE),
        status,
        tokensUsed: 0,
        timeUsedSeconds: 0,
        createdAt: 1,
        updatedAt: 1,
        workspaceRoot: this.workspaceRoot,
      },
      threadStatus: activeTurnId === null ? 'idle' : 'active',
      activeTurnId,
      descendants: [],
      nativeMaterialObservations: [],
    }
  }

  private observedGoalStatus(): string {
    if (
      this.scenario.kind === 'usage_limited' ||
      this.scenario.kind === 'large_read_unconsumed_usage_limited' ||
      this.scenario.kind === 'large_read_final_page_unacknowledged_usage_limited' ||
      this.scenario.kind === 'core_cancel_dynamic' ||
      this.scenario.kind === 'core_cancel_material'
    ) {
      return 'usageLimited'
    }
    if (this.scenario.kind === 'blocked_without_checkpoint') {
      return 'blocked'
    }
    if (
      this.scenario.kind === 'large_read' ||
      this.scenario.kind === 'large_read_tamper' ||
      this.scenario.kind === 'resume_large_read' ||
      this.scenario.kind === 'resume_final_page_replay'
    ) {
      return 'complete'
    }
    if (this.scenario.kind === 'checkpoint' || this.scenario.kind === 'semantic_stop') {
      if (
        this.scenario.kind === 'checkpoint' &&
        this.scenario.goalBecomesUsageLimitedAfterCheckpoint
      ) {
        return 'usageLimited'
      }
      if (
        this.scenario.kind === 'checkpoint' &&
        this.scenario.goalRemainsActiveAfterCheckpoint
      ) {
        return 'active'
      }
      return this.scenario.terminalGoalStatus ?? 'complete'
    }
    return 'complete'
  }

  async getThreadGoal(threadId: string): Promise<any> {
    this.threadId = threadId
    if (this.scenario.kind === 'recovery' && this.scenario.goalAbsent === true) {
      return null
    }
    const status =
      this.scenario.kind === 'recovery'
        ? this.scenario.stoppedStatus ?? 'complete'
        : this.observedGoalStatus()
    return this.state(status).goal
  }

  async readGoalEpochState(): Promise<any> {
    this.monitorReadCount += 1
    if (
      this.scenario.kind === 'checkpoint' &&
      this.scenario.transientBlockedSameTurnBeforeCheckpoint
    ) {
      const activeTurnId = `turn.${this.threadId}`
      if (this.monitorReadCount === 1) {
        this.monitorEvents.push({ kind: 'goal_state', status: 'active', activeTurnId })
        return this.state('active', activeTurnId)
      }
      if (this.monitorReadCount === 2) {
        this.monitorEvents.push({ kind: 'goal_state', status: 'blocked', activeTurnId })
        return this.state('blocked', activeTurnId)
      }
      if (this.monitorReadCount === 3) {
        await this.requireSuccessfulToolCall(
          this.toolCall(
            'rh_mission',
            { operation: 'usage', input: {} },
            'same-turn-activity-after-blocked',
          ),
        )
        this.monitorEvents.push({
          kind: 'same_turn_normal_activity',
          status: 'blocked',
          activeTurnId,
        })
        return this.state('blocked', activeTurnId)
      }
    }
    try {
      await this.act()
    } catch (error) {
      this.actFailure = error instanceof Error ? error.message : String(error)
      throw error
    }
    return this.state(this.observedGoalStatus())
  }

  async stopBoundedGoalEpoch(
    threadId = this.threadId,
    reason: GoalEpochStopReason = 'operator_stop',
    recovery?: { phase: 'pending' | 'registered'; expectedObjective: string },
  ): Promise<any> {
    this.threadId = threadId
    this.stopCalls.push({ threadId, reason })
    if (this.stopFailuresRemaining > 0) {
      this.stopFailuresRemaining -= 1
      throw new Error('simulated first owner-checkpoint containment failure')
    }
    if (recovery) {
      this.recoveryCalls.push({ threadId, reason, recovery: structuredClone(recovery) })
    }
    await this.callbacks.onGoalTermination({
      threadId,
      turnId: `turn.${threadId}`,
      reason,
      containmentScope: 'goal_local',
    })
    const status = this.scenario.kind === 'recovery' ? this.scenario.stoppedStatus ?? 'complete' : 'complete'
    return {
      threadId: this.threadId,
      goal: this.scenario.kind === 'recovery' && this.scenario.goalAbsent === true
        ? null
        : this.state(status).goal,
      interruptedTurnId: null,
      goalCleared: true,
      descendantsContained: true,
      appServerTerminated: true,
    }
  }

  async suspendBoundedGoalEpoch(
    threadId: string,
    recovery?: { expectedObjective?: string },
    reason: 'usage_limited' | 'operator_stop' = 'usage_limited',
  ): Promise<any> {
    this.threadId = threadId
    if (typeof recovery?.expectedObjective === 'string') {
      this.request = { objective: recovery.expectedObjective }
    }
    await this.callbacks.onGoalTermination({
      threadId: this.threadId,
      turnId: `turn.${this.threadId}`,
      reason,
      containmentScope: 'goal_local',
    })
    return {
      threadId: this.threadId,
      goal: this.state(reason === 'usage_limited' ? 'usageLimited' : 'paused').goal,
      interruptedTurnId: `turn.${this.threadId}`,
      goalCleared: false,
      descendantsContained: 0,
      appServerTerminated: false,
    }
  }

  async finalizeCompletedGoalEpoch(): Promise<any> {
    return this.state(this.observedGoalStatus())
  }
}

class ScriptedBoundaryFactory implements CodexEpochBoundaryFactory {
  readonly boundaries: ScriptedBoundary[] = []
  readonly callbackSets: CodexEpochBoundaryCallbacks[] = []
  readonly requests: Record<string, unknown>[] = []
  readonly resumeRequests: Array<{ threadId: string; request: Record<string, unknown> }> = []
  retainedLargePage: LargePageDescriptor | null = null
  retainedFinalPageReplay: FinalPageReplay | null = null
  onLargePageDescriptor:
    ((descriptor: LargePageDescriptor) => Promise<void> | void) | null = null
  mutateDelegatedPageCustody: ((handle: string) => (() => void)) | null = null
  onCreate: ((boundary: ScriptedBoundary) => void) | null = null
  private sequence = 0

  constructor(
    private readonly scenarios: EpochScenario[],
    private readonly runtimeDir: string,
  ) {}

  create(workspaceRoot: string, callbacks: CodexEpochBoundaryCallbacks): CodexEpochBoundary {
    const scenario = this.scenarios.shift()
    if (!scenario) {
      throw new Error('no scripted Boundary scenario remains')
    }
    this.callbackSets.push(callbacks)
    const boundary = new ScriptedBoundary(
      workspaceRoot,
      this.runtimeDir,
      callbacks,
      scenario,
      `thread.${this.sequence++}`,
      async (descriptor) => {
        this.retainedLargePage = descriptor
        await this.onLargePageDescriptor?.(descriptor)
      },
      () => this.retainedLargePage,
      async (descriptor) => {
        this.retainedFinalPageReplay = descriptor
      },
      () => this.retainedFinalPageReplay,
      this.mutateDelegatedPageCustody,
    )
    this.boundaries.push(boundary)
    const originalStart = boundary.startBoundedGoalEpoch.bind(boundary)
    boundary.startBoundedGoalEpoch = async (request: unknown) => {
      this.requests.push(structuredClone(request as Record<string, unknown>))
      return originalStart(request)
    }
    const originalResume = boundary.resumeBoundedGoalEpoch.bind(boundary)
    boundary.resumeBoundedGoalEpoch = async (threadId: string, request: unknown) => {
      this.resumeRequests.push({
        threadId,
        request: structuredClone(request as Record<string, unknown>),
      })
      return originalResume(threadId, request)
    }
    this.onCreate?.(boundary)
    return boundary as unknown as CodexEpochBoundary
  }
}

type BoundaryCancellationPhase = 'start' | 'goal_start' | 'goal_start_known' | 'read'

class SignalBlockingBoundary implements CodexEpochBoundary {
  receivedSignal: AbortSignal | undefined
  stopCalls: Array<{ threadId: string; reason: string }> = []
  suspendCalls: Array<{ threadId: string; reason: string }> = []
  stopCount = 0

  constructor(
    private readonly workspaceRoot: string,
    private readonly callbacks: CodexEpochBoundaryCallbacks,
    private readonly phase: BoundaryCancellationPhase,
    private readonly entered: () => void,
    private readonly suspensionStatus: 'usageLimited' | 'paused' | null = null,
    private readonly afterSuspension: (() => Promise<void> | void) | null = null,
  ) {}

  private async waitForCancellation(signal: AbortSignal | undefined, phase: string): Promise<never> {
    this.receivedSignal = signal
    this.entered()
    if (!signal) {
      throw new Error(`test ${phase} did not receive the operator signal`)
    }
    return new Promise<never>((_resolve, reject) => {
      const abort = (): void => {
        signal.removeEventListener('abort', abort)
        reject(new GoalEpochCancelledError(phase, signal.reason))
      }
      if (signal.aborted) {
        abort()
      } else {
        signal.addEventListener('abort', abort, { once: true })
      }
    })
  }

  async start(signal?: AbortSignal): Promise<void> {
    if (this.phase === 'start') {
      await this.waitForCancellation(signal, 'test boundary startup')
    }
  }

  async stop(): Promise<void> {
    this.stopCount += 1
  }

  async waitForFatalBoundaryFence(): Promise<void> {}

  async findMaterializedGoalEpoch(): Promise<null> {
    return null
  }

  async startBoundedGoalEpoch(
    _request: unknown,
    signal?: AbortSignal,
  ): Promise<any> {
    if (this.phase === 'goal_start') {
      await this.waitForCancellation(signal, 'test Goal start')
    }
    const threadId = 'thread.signal-cancellation'
    await this.callbacks.onEpochIdentityMaterialized({ threadId, workspaceRoot: this.workspaceRoot })
    if (this.phase === 'goal_start_known') {
      await this.waitForCancellation(signal, 'test known-root Goal start')
    }
    return { threadId }
  }

  async resumeBoundedGoalEpoch(
    _threadId: string,
    _request: unknown,
    signal?: AbortSignal,
  ): Promise<any> {
    await this.waitForCancellation(signal, 'test Goal resume')
  }

  async getThreadGoal(): Promise<any> {
    throw new Error('unexpected persisted Goal read')
  }

  async readGoalEpochState(_threadId: string, signal?: AbortSignal): Promise<any> {
    if (this.phase === 'read') {
      await this.waitForCancellation(signal, 'test Goal state read')
    }
    throw new Error('unexpected nonblocking Goal state read')
  }

  async stopBoundedGoalEpoch(threadId: string, reason: any): Promise<any> {
    this.stopCalls.push({ threadId, reason })
    await this.callbacks.onGoalTermination({
      threadId,
      turnId: null,
      reason,
      containmentScope: 'goal_local',
    })
    return {
      threadId,
      goal: null,
      interruptedTurnId: null,
      goalCleared: true,
      descendantsContained: 0,
      appServerTerminated: false,
    }
  }

  async suspendBoundedGoalEpoch(
    threadId: string,
    _recovery?: unknown,
    reason: 'usage_limited' | 'operator_stop' = 'usage_limited',
  ): Promise<any> {
    this.suspendCalls.push({ threadId, reason })
    const status = this.suspensionStatus ?? (reason === 'usage_limited' ? 'usageLimited' : 'paused')
    const actualReason = status === 'usageLimited' ? 'usage_limited' : 'operator_stop'
    await this.callbacks.onGoalTermination({
      threadId,
      turnId: null,
      reason: actualReason,
      containmentScope: 'goal_local',
    })
    await this.afterSuspension?.()
    return {
      threadId,
      goal: {
        threadId,
        objective: MISSION_OBJECTIVE,
        status,
      },
      interruptedTurnId: null,
      goalCleared: false,
      descendantsContained: 0,
      appServerTerminated: false,
    }
  }

  async finalizeCompletedGoalEpoch(): Promise<any> {
    throw new Error('unexpected Goal finalization')
  }

  resolveDirectChildToolGrantTarget(): any {
    throw new Error('unexpected descendant grant target resolution')
  }

  installDescendantToolGrant(): GoalEpochDescendantToolGrantBinding {
    throw new Error('unexpected descendant grant installation')
  }
}

class SignalBlockingBoundaryFactory implements CodexEpochBoundaryFactory {
  boundary: SignalBlockingBoundary | null = null
  readonly entered: Promise<void>
  private resolveEntered!: () => void

  constructor(
    private readonly phase: BoundaryCancellationPhase,
    private readonly suspensionStatus: 'usageLimited' | 'paused' | null = null,
    private readonly afterSuspension: (() => Promise<void> | void) | null = null,
  ) {
    this.entered = new Promise<void>((resolve) => {
      this.resolveEntered = resolve
    })
  }

  create(workspaceRoot: string, callbacks: CodexEpochBoundaryCallbacks): CodexEpochBoundary {
    if (this.boundary) {
      throw new Error('test cancellation factory only supports one boundary')
    }
    this.boundary = new SignalBlockingBoundary(
      workspaceRoot,
      callbacks,
      this.phase,
      this.resolveEntered,
      this.suspensionStatus,
      this.afterSuspension,
    )
    return this.boundary
  }
}

interface Harness {
  root: string
  config: MissionHostConfig
  bridge: FakeOwnerBridge
  store: MemoryStateStore
  boundaryFactory: ScriptedBoundaryFactory
  allocateWorkspace: (prospectivePath?: string) => Promise<string>
  allocatedWorkspaces: string[]
}

async function createHarness(scenarios: EpochScenario[]): Promise<Harness> {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-host-lean-'))
  const repoRoot = path.join(root, 'a'.repeat(40))
  const missionWorkspaceRoot = path.join(root, 'mission')
  const goalsRoot = path.join(root, 'goals')
  const runtimeDir = path.join(root, 'runtime')
  const codexHome = path.join(root, 'codex-home')
  await Promise.all([
    fs.promises.mkdir(repoRoot, { recursive: true }),
    fs.promises.mkdir(missionWorkspaceRoot, { recursive: true }),
    fs.promises.mkdir(goalsRoot, { recursive: true }),
    fs.promises.mkdir(runtimeDir, { recursive: true }),
    fs.promises.mkdir(codexHome, { recursive: true }),
  ])
  await fs.promises.cp(
    path.join(SOURCE_REPO_ROOT, RH_INSTRUCTION_RELATIVE_PATH),
    path.join(repoRoot, RH_INSTRUCTION_RELATIVE_PATH),
    { recursive: true },
  )
  const modelCatalogPath = path.join(repoRoot, PINNED_MODEL_CATALOG_RELATIVE_PATH)
  await fs.promises.mkdir(path.dirname(modelCatalogPath), { recursive: true })
  await fs.promises.copyFile(path.join(SOURCE_REPO_ROOT, 'services/rh-mission-host/test-fixtures/codex-model-catalog.0.153.4.json'), modelCatalogPath)
  immutableCatalogFixtures.add(modelCatalogPath)
  const operationGuides = Object.fromEntries(
    SEMANTIC_OPERATIONS.map((operation) => [
      operation,
      {
        schema_version: 'mathematical_research.mission_model_usage.v1',
        tool: 'rh_mission',
        status: 'ok',
        operation,
        input_schema:
          operation === 'retrieve'
            ? {
                oneOf: [
                  {
                    type: 'object',
                    properties: {
                      mode: { const: 'search' },
                      purpose: { type: 'string' },
                      query: { type: 'string' },
                    },
                    required: ['mode', 'purpose', 'query'],
                    additionalProperties: false,
                  },
                  {
                    type: 'object',
                    properties: {
                      mode: { const: 'read' },
                      purpose: { type: 'string' },
                      ids: { type: 'array', items: { type: 'string' }, minItems: 1 },
                    },
                    required: ['mode', 'purpose', 'ids'],
                    additionalProperties: false,
                  },
                ],
              }
            : operation === 'record_strategy'
              ? {
                  type: 'object',
                  properties: {
                    mission_continuation: {
                      type: 'string',
                      enum: ['continue'],
                    },
                  },
                }
              : { type: 'object' },
        examples: [
          {
            call: {
              operation,
              input:
                operation === 'retrieve'
                  ? { mode: 'read', purpose: 'Read one exact owner result.', ids: ['owner:example'] }
                  : operation === 'record_strategy'
                    ? { mission_continuation: 'continue' }
                  : {},
            },
          },
        ],
      },
    ]),
  )
  const config: MissionHostConfig = {
    repoRoot,
    releaseSha: 'a'.repeat(40),
    serviceEntrypointPath: path.join(repoRoot, 'dist', 'main.js'),
    missionWorkspaceRoot,
    goalsRoot,
    runtimeDir,
    codexHome,
    statePath: path.join(runtimeDir, 'host-state.json'),
    lockPath: path.join(runtimeDir, 'host.lock'),
    pythonPath: '/usr/bin/python3',
    codexCliPath: '/usr/bin/codex',
    modelCatalogPath,
    missionScriptPath: path.join(repoRoot, 'scripts', 'rh_mission.py'),
    missionModelProjectionPath: path.join(repoRoot, '.mathematical-research-model-projection.json'),
    missionModelProjection: {
      inputSchema: {
        type: 'object',
        properties: {
          operation: { enum: ['usage', ...SEMANTIC_OPERATIONS] },
          input: { type: 'object' },
        },
        required: ['operation', 'input'],
        additionalProperties: false,
      },
      semanticRequestSchemaVersion: 'mathematical_research.mission_semantic_request.v1',
      usageIndex: {
        schema_version: 'mathematical_research.mission_model_usage.v1',
        tool: 'rh_mission',
        status: 'ok',
        semantic_operation_count: SEMANTIC_OPERATIONS.length,
        operations: SEMANTIC_OPERATIONS.map((operation) => ({ operation })),
      },
      operationGuides,
    },
    closeoutMissionModelProjection: {
      inputSchema: {
        type: 'object',
        properties: {
          operation: { enum: ['usage', 'record_strategy', 'checkpoint'] },
          input: { type: 'object' },
        },
        required: ['operation', 'input'],
        additionalProperties: false,
      },
      semanticRequestSchemaVersion: 'mathematical_research.mission_semantic_request.v1',
      usageIndex: {
        schema_version: 'mathematical_research.mission_model_usage.v1',
        tool: 'rh_mission',
        status: 'ok',
        semantic_operation_count: 2,
        operations: ['record_strategy', 'checkpoint'].map((operation) => ({ operation })),
      },
      operationGuides: {
        record_strategy: {
          ...operationGuides.record_strategy!,
          input_schema: {
            type: 'object',
            properties: {
              mission_continuation: {
                type: 'string',
                enum: ['closeout'],
              },
            },
          },
          examples: [{
            call: {
              operation: 'record_strategy',
              input: { mission_continuation: 'closeout' },
            },
          }],
        },
        checkpoint: operationGuides.checkpoint!,
      },
    },
    historicalReadModelProjection: {
      inputSchema: {
        type: 'object',
        properties: {
          operation: { enum: ['usage', 'orient', 'retrieve'] },
          input: { type: 'object' },
        },
        required: ['operation', 'input'],
        additionalProperties: false,
      },
      semanticRequestSchemaVersion: 'mathematical_research.mission_semantic_request.v1',
      usageIndex: {
        schema_version: 'mathematical_research.mission_model_usage.v1',
        tool: 'rh_mission_history',
        status: 'ok',
        semantic_operation_count: 2,
        operations: ['orient', 'retrieve'].map((operation) => ({ operation })),
      },
      operationGuides: {
        orient: operationGuides.orient!,
        retrieve: operationGuides.retrieve!,
      },
    },
    historicalReadGrantInputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        child_thread_id: { type: 'string', minLength: 1 },
        assignment_mode: {
          type: 'string',
          enum: ['historical_opportunity_scout', 'lifecycle_historian'],
        },
        assignment: { type: 'string', minLength: 1 },
        context: {
          type: 'object',
          additionalProperties: false,
          properties: {
            id: { type: 'string', pattern: '^context:[A-Za-z0-9][A-Za-z0-9._:-]*$' },
            revision: { type: 'integer', minimum: 1 },
          },
          required: ['id', 'revision'],
        },
        source_families: {
          type: 'array',
          items: {
            type: 'string',
            enum: [
              'branches',
              'candidates',
              'strategies',
              'contexts',
              'evidence',
              'capture_annotations',
              'captures',
              'capture_artifacts',
            ],
          },
          minItems: 1,
          uniqueItems: true,
        },
        raw_body_policy: {
          type: 'string',
          enum: ['metadata_only', 'allow_untrusted_material'],
        },
      },
      required: [
        'child_thread_id',
        'assignment_mode',
        'assignment',
        'context',
        'source_families',
        'raw_body_policy',
      ],
    },
    researchReadGrantInputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        child_thread_id: { type: 'string', minLength: 1 },
        assignment: { type: 'string', minLength: 1 },
        source_families: {
          type: 'array',
          items: {
            type: 'string',
            enum: ['branches', 'candidates', 'strategies', 'contexts', 'evidence',
              'capture_annotations', 'captures', 'capture_artifacts', 'missions', 'sessions'],
          },
          minItems: 1,
          uniqueItems: true,
        },
        raw_body_policy: {
          type: 'string',
          enum: ['metadata_only', 'allow_untrusted_material'],
        },
      },
      required: ['child_thread_id', 'assignment', 'source_families', 'raw_body_policy'],
    },
    researchReadRequestSchema: {
      oneOf: [
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'usage' },
            topics: {
              type: 'array', minItems: 1, uniqueItems: true,
              items: { enum: ['reads', 'sources', 'continuations', 'authority'] },
            },
          },
          required: ['mode'],
        },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'retrieve' },
            selection: operationGuides.retrieve!.input_schema,
          },
          required: ['mode', 'selection'],
        },
      ],
    },
    candidateA1ReviewGrantInputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        child_thread_id: { type: 'string', minLength: 1 },
        assignment: { type: 'string', minLength: 1 },
        context: {
          type: 'object',
          additionalProperties: false,
          properties: {
            id: { type: 'string', pattern: '^context:[A-Za-z0-9][A-Za-z0-9._:-]*$' },
            revision: { type: 'integer', minimum: 1 },
          },
          required: ['id', 'revision'],
        },
        candidate_ref: {
          type: 'object',
          additionalProperties: false,
          properties: {
            id: { type: 'string', pattern: '^candidate:[A-Za-z0-9][A-Za-z0-9._:-]*$' },
            revision: { type: 'integer', minimum: 1 },
            payload_sha256: { type: 'string', pattern: '^[0-9a-f]{64}$' },
          },
          required: ['id', 'revision', 'payload_sha256'],
        },
      },
      required: ['child_thread_id', 'assignment', 'context', 'candidate_ref'],
    },
    candidateA1ReviewRequestSchema: {
      oneOf: [
        {
          type: 'object',
          additionalProperties: false,
          properties: { mode: { const: 'usage' } },
          required: ['mode'],
        },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'retrieve' },
            page_size: { type: 'integer', minimum: 1, maximum: 50 },
            cursor: { type: 'string', minLength: 1 },
          },
          required: ['mode'],
        },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'submit' },
            disposition: { type: 'string', enum: ['invalidated', 'admission_ready'] },
            review_finding: { type: 'string', minLength: 1 },
            cited_basis: { type: 'array' },
            concrete_defects: { type: 'array' },
            no_remaining_material_objection: { type: 'boolean' },
            limitations: { type: 'array' },
            non_inferences: { type: 'array' },
          },
          required: [
            'mode',
            'disposition',
            'review_finding',
            'no_remaining_material_objection',
          ],
        },
      ],
    },
    admissionCaseInputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        candidate_ref: {
          type: 'object',
          additionalProperties: false,
          properties: {
            id: { type: 'string', pattern: '^candidate:[A-Za-z0-9][A-Za-z0-9._:-]*$' },
            revision: { type: 'integer', minimum: 1 },
            payload_sha256: { type: 'string', pattern: '^[0-9a-f]{64}$' },
          },
          required: ['id', 'revision', 'payload_sha256'],
        },
      },
      required: ['candidate_ref'],
    },
    admissionGrantInputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        role: { type: 'string', enum: ['reviewer', 'admitter'] },
        child_thread_id: { type: 'string', minLength: 1 },
        assignment: { type: 'string', minLength: 1 },
        context: {
          type: 'object',
          additionalProperties: false,
          properties: {
            id: { type: 'string', pattern: '^context:[A-Za-z0-9][A-Za-z0-9._:-]*$' },
            revision: { type: 'integer', minimum: 1 },
          },
          required: ['id', 'revision'],
        },
        case_ref: {
          type: 'object',
          additionalProperties: false,
          properties: {
            id: { type: 'string', pattern: '^evidence:[A-Za-z0-9][A-Za-z0-9._:-]*$' },
            revision: { type: 'integer', minimum: 1 },
            payload_sha256: { type: 'string', pattern: '^[0-9a-f]{64}$' },
          },
          required: ['id', 'revision', 'payload_sha256'],
        },
      },
      required: ['role', 'child_thread_id', 'assignment', 'context', 'case_ref'],
    },
    admissionRequestSchema: {
      oneOf: [
        {
          type: 'object',
          additionalProperties: false,
          properties: { mode: { const: 'usage' } },
          required: ['mode'],
        },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'retrieve' },
            page_size: { type: 'integer', minimum: 1, maximum: 50 },
            cursor: { type: 'string', minLength: 1 },
          },
          required: ['mode'],
        },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'submit' },
            disposition: {
              type: 'string',
              enum: ['no_material_objection', 'material_objection'],
            },
            review_finding: { type: 'string', minLength: 1 },
            objections: { type: 'array' },
            cited_basis: { type: 'array' },
            limitations: { type: 'array' },
            non_inferences: { type: 'array' },
          },
          required: ['mode', 'disposition', 'review_finding'],
        },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'submit' },
            disposition: { const: 'authorize_exact_delta' },
            decision_basis: { type: 'string', minLength: 1 },
            limitations: { type: 'array' },
            non_inferences: { type: 'array' },
          },
          required: ['mode', 'disposition', 'decision_basis'],
        },
        {
          type: 'object',
          additionalProperties: false,
          properties: {
            mode: { const: 'submit' },
            disposition: { const: 'reject' },
            decision_basis: { type: 'string', minLength: 1 },
            objections: { type: 'array', minItems: 1 },
            cited_basis: { type: 'array', minItems: 1 },
            limitations: { type: 'array' },
            non_inferences: { type: 'array' },
          },
          required: ['mode', 'disposition', 'decision_basis', 'objections', 'cited_basis'],
        },
      ],
    },
    projectId: 'project.riemann_hypothesis',
    missionId: 'mission.rh.public.1',
    pollIntervalMs: 2000,
    permissionProfileId: 'rh_mission_host',
    trustedMcpServerIds: [],
    trustedAppIds: [],
    expectedCliVersion: '0.153.4',
    expectedModelProvider: 'openai',
    expectedModel: 'gpt-6-astra',
    reasoningEffort: 'ultra',
  }
  let workspaceSequence = 0
  const allocatedWorkspaces: string[] = []
  const allocateWorkspace = async (prospectivePath?: string): Promise<string> => {
    const workspace = prospectivePath ?? path.join(goalsRoot, `epoch-${workspaceSequence++}`)
    await fs.promises.mkdir(workspace)
    allocatedWorkspaces.push(workspace)
    return workspace
  }
  const bridge = new FakeOwnerBridge()
  const store = new MemoryStateStore(freshState(config))
  return {
    root,
    config,
    bridge,
    store,
    boundaryFactory: new ScriptedBoundaryFactory([...scenarios], runtimeDir),
    allocateWorkspace,
    allocatedWorkspaces,
  }
}

function createHost(
  harness: Harness,
  notificationSink: MissionNotificationSink | null = null,
  consumeCheckpointStopIntent: CheckpointStopIntentConsumer = async () => false,
  consumeSingleEpochCanaryIntent: SingleEpochCanaryIntentConsumer = async () => false,
  executionObserver: MissionExecutionObserver | null = null,
): MissionHost {
  return new MissionHost(
    harness.config,
    () => harness.bridge,
    harness.boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
    notificationSink,
    consumeCheckpointStopIntent,
    consumeSingleEpochCanaryIntent,
    executionObserver,
  )
}

class RecordingNotificationSink implements MissionNotificationSink {
  readonly notifications: AgentCommunicationsNotification[] = []

  admit(
    notification: AgentCommunicationsNotification,
  ): AgentCommunicationsNotificationAdmission {
    this.notifications.push(structuredClone(notification))
    return {
      operationId: notification.operationId,
      disposition: 'admitted',
    }
  }
}

class FailingNotificationSink implements MissionNotificationSink {
  readonly notifications: AgentCommunicationsNotification[] = []

  admit(
    notification: AgentCommunicationsNotification,
  ): AgentCommunicationsNotificationAdmission {
    this.notifications.push(structuredClone(notification))
    const error = new Error('injected outbox failure SECRET_BODY')
    error.name = 'SecretBearingCustomErrorName'
    throw error
  }
}

async function cleanup(harness: Harness): Promise<void> {
  immutableCatalogFixtures.delete(harness.config.modelCatalogPath)
  await fs.promises.rm(harness.root, { recursive: true, force: true })
}

async function makeFixtureTreeWritable(root: string): Promise<void> {
  const details = await fs.promises.lstat(root)
  if (details.isDirectory() && !details.isSymbolicLink()) {
    for (const entry of await fs.promises.readdir(root)) {
      await makeFixtureTreeWritable(path.join(root, entry))
    }
  }
  await fs.promises.chmod(root, 0o700)
}

for (const openingKind of ['initial', 'failed_recovery', 'historical_pause', 'historical_closeout', 'canonical_closeout'] as const) {
  test(`launch inspection exactly matches the live Host request for ${openingKind} without launching`, async () => {
    const harness = await createHarness([{ kind: 'semantic_stop' }])
    if (openingKind === 'failed_recovery') {
      harness.bridge.seedFailure(null, 'boundary_failure', 'epoch.inspection-predecessor')
      harness.bridge.recoveryRawCaptures = [{
        capture_id: 'capture.inspection.retained',
        capture_kind: 'output',
        executive_epoch_id: 'epoch.inspection-predecessor',
        observation_id: 'output.thread.inspection-child',
        assignment_id: 'assignment.thread.inspection-child',
        project_commit: 1,
        retrieval_handle: 'capture:capture.inspection.retained',
        late_classification: 'at_or_before_terminal',
        artifacts: [],
      }]
    } else if (openingKind === 'historical_pause') {
      harness.bridge.seedCheckpoint('thread.inspection-predecessor', 'pause', 'epoch.inspection-predecessor')
    } else if (openingKind === 'historical_closeout') {
      harness.bridge.seedCheckpoint('thread.inspection-predecessor', 'closeout', 'epoch.inspection-predecessor')
    } else if (openingKind === 'canonical_closeout') {
      harness.bridge.seedCheckpoint('thread.inspection-predecessor', 'continue', 'epoch.inspection-predecessor')
      harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
    }
    harness.bridge.missionPurpose.proof_standard = 'Exact Unicode fixture: \u{1f9ed} and \u{1f600}; no proof claim.'
    const workspaceRoot = prospectiveGoalWorkspacePath(
      harness.config,
      '12345678-1234-4234-8234-123456789abc',
    )
    let allocations = 0
    harness.allocateWorkspace = async (prospectivePath) => {
      allocations += 1
      await fs.promises.mkdir(prospectivePath!)
      harness.allocatedWorkspaces.push(prospectivePath!)
      return prospectivePath!
    }
    try {
      const reconstruction = await harness.bridge.hostSnapshot()
      const inputBefore = structuredClone(reconstruction)
      const storeBefore = structuredClone(harness.store.state)
      const filesBefore = fs.readdirSync(harness.root, { recursive: true })
      const inspection = inspectMissionGoalLaunch(harness.config, reconstruction, workspaceRoot)

      assert.deepEqual(reconstruction, inputBefore)
      assert.deepEqual(await harness.bridge.hostSnapshot(), inputBefore)
      assert.deepEqual(harness.store.state, storeBefore)
      assert.equal(harness.store.saves.length, 0)
      assert.deepEqual(harness.bridge.authorizations, [])
      assert.deepEqual(harness.bridge.bindings, [])
      assert.deepEqual(harness.bridge.semantics, [])
      assert.deepEqual(harness.bridge.failures, [])
      assert.deepEqual(harness.boundaryFactory.requests, [])
      assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
      assert.deepEqual(harness.boundaryFactory.boundaries, [])
      assert.equal(allocations, 0)
      assert.equal(fs.existsSync(workspaceRoot), false)
      assert.deepEqual(fs.readdirSync(harness.root, { recursive: true }), filesBefore)
      assert.equal(inspection.model, harness.config.expectedModel)
      assert.equal(inspection.construction.request_json, JSON.stringify(inspection.request))
      assert.deepEqual(JSON.parse(inspection.construction.request_json), inspection.request)
      assert.equal(inspection.construction.closeout_only, openingKind === 'canonical_closeout')
      if (openingKind === 'canonical_closeout') {
        assert.equal(inspection.executive_orientation.target.canonical_status, ADMITTED_RESULT.disposition)
        assert.equal(inspection.executive_orientation.target.canonical_status, 'proved')
      }

      for (const [text, segments] of [
        [inspection.request.developerInstructions, inspection.construction.instruction_segments],
        [inspection.request.initialContextText!, inspection.construction.data_segments],
      ] as const) {
        const codepoints = Array.from(text)
        assert.equal(segments[0]!.start_char, 0)
        assert.equal(segments.at(-1)!.end_char, codepoints.length)
        assert.equal(segments.map(({ start_char, end_char }) =>
          codepoints.slice(start_char, end_char).join('')).join('\n'), text)
        for (let index = 1; index < segments.length; index += 1) {
          assert.equal(segments[index]!.start_char, segments[index - 1]!.end_char + 1)
          assert.equal(codepoints[segments[index - 1]!.end_char], '\n')
        }
      }
      const data = inspection.request.initialContextText!
      assert.ok(Array.from(data).length < data.length)
      assert.deepEqual(instructionJson(data, 'executive_orientation'), inspection.executive_orientation)
      assert.deepEqual(instructionJson(data, 'host_continuity'), inspection.host_continuity)
      const resolved = resolveGoalEpochInstructions(inspection.request, [harness.config.repoRoot])
      assert.equal(inspection.construction.resolved_developer_instructions, resolved.developerInstructions)
      assert.deepEqual(inspection.construction.instruction_sources, resolved.instructionSources)
      assert.deepEqual(resolved.instructionSources.map(({ path: source }) => path.relative(
        path.join(harness.config.repoRoot, RH_INSTRUCTION_RELATIVE_PATH), source,
      ).replaceAll('\\', '/')), openingKind === 'canonical_closeout'
        ? ['AGENTS.md'] : ['AGENTS.md', 'executive/AGENTS.md'])
      for (const instructions of [inspection.request.developerInstructions, resolved.developerInstructions]) {
        assert.doesNotMatch(instructions, /Exact Unicode fixture|🧭|😀/)
        assert.doesNotMatch(instructions, /# Vector Mosaic repository instructions|codex_thread_sketchpad/)
      }
      assert.equal(inspection.compatibility.measurement.components.resolved_developer_instructions.utf8_bytes,
        Buffer.byteLength(resolved.developerInstructions, 'utf8'))
      if (openingKind === 'historical_pause') {
        assert.equal(inspection.host_continuity.launch_mode, 'reopen_historical_pause')
      } else if (openingKind === 'historical_closeout') {
        assert.equal(inspection.host_continuity.launch_mode, 'reopen_historical_closeout')
      } else if (openingKind === 'canonical_closeout') {
        assert.equal(inspection.construction.instruction_segments[0]!.kind, 'static_closeout_instructions')
        assert.equal(inspection.request.nativeAgentRoles, undefined)
      }
      if (openingKind !== 'canonical_closeout') {
        assert.match(resolved.developerInstructions, /Ordinary research may write only `continue`/)
        assert.match(resolved.developerInstructions, /research request does not authorize it to repair source, test, publish, install, or administer runtime/)
      }
      const result = await createHost(harness).run()
      assert.equal(result.status, 'stopped_semantically')
      assert.equal(allocations, 1)
      assert.equal(harness.boundaryFactory.requests.length, 1)
      const submitted = harness.boundaryFactory.requests[0]! as typeof inspection.request
      const exactInspection = inspectMissionGoalLaunch(harness.config, reconstruction, submitted.environments![0]!.cwd)
      assert.deepEqual(submitted, exactInspection.request)
      assert.equal(harness.store.saves.find((saved) => saved.activeGoal?.phase === 'planned')!.activeGoal!.objective, submitted.objective)
    } finally {
      await cleanup(harness)
    }
  })
}

test('Host continuity excludes historical failure and output panoramas from the bounded launch cut', async () => {
  const harness = await createHarness([])
  try {
    harness.bridge.seedCheckpoint('thread.cut', 'continue', 'epoch.cut')
    const raw = await harness.bridge.hostSnapshot()
    const state = raw.current_state as JsonObject
    const baseline = (state.checkpoint as JsonObject).project_commit as number
    state.observed_project_commit = baseline + 20
    ;(raw.authorization_cut as JsonObject).project_commit = baseline + 20
    state.latest_executive_epoch = { executive_epoch_id: 'epoch.failed.3',
      state: 'failed_before_checkpoint', goal_thread_id: 'thread.failed.3', last_event_project_commit: baseline + 3,
      checkpoint_ref: null, reconciliation: { stage: 'goal_runtime', failure_reason: 'operator_stop' } }
    const snapshot = requireMissionHostSnapshot(raw, harness.config)
    const continuity = constructHostContinuity(snapshot, harness.store.state)
    assert.deepEqual(Object.keys(continuity).sort(), [
      'capture_recovery_required', 'launch_mode', 'operational_effect_on_mathematics', 'resumed_goal', 'schema_version',
    ])
    assert.equal('reconstruction' in raw, false)
    assert.equal('host_recovery_facts' in raw, false)
    assert.equal('failed_epochs_since_checkpoint' in continuity, false)
    assert.equal('retained_unintegrated_outputs' in continuity, false)
    assert.equal(continuity.operational_effect_on_mathematics, 'none')
    assert.deepEqual(harness.store.saves, [])
    assert.deepEqual(harness.bridge.semantics, [])
  } finally { await cleanup(harness) }
})

test('nonempty OPEN A1 compares exact anchors across distinct private and model shapes', async () => {
  const harness = await createHarness([])
  try {
    harness.bridge.openCandidateA1 = [{
      candidate_ref: {
        kind: 'candidate',
        identity: 'complete-rh-snapshot',
        revision: 3,
        payload_sha256: '8'.repeat(64),
      },
      retrieval_handle: 'candidate:complete-rh-snapshot@3',
      classification: 'purported_complete_rh_proof_or_disproof',
      disposition: 'proof',
      hold_lifecycle: 'open',
      canonical_effect: 'none',
      mathematical_effect: 'none',
    }]
    const raw = await harness.bridge.hostSnapshot()
    const state = raw.current_state as JsonObject
    const orientation = raw.executive_orientation as JsonObject
    assert.notDeepEqual(
      state.open_candidate_a1,
      (orientation.proof_attention as JsonObject).open_candidate_a1,
    )
    const snapshot = requireMissionHostSnapshot(raw, harness.config)
    assert.equal(snapshot.current_state.open_candidate_a1.length, 1)
    assert.equal(snapshot.executive_orientation.proof_attention.open_candidate_a1.length, 1)

    const mismatched = structuredClone(raw)
    const mismatchedProof = (mismatched.executive_orientation as JsonObject)
      .proof_attention as JsonObject
    const mismatchedItem = (mismatchedProof.open_candidate_a1 as JsonObject[])[0]!
    ;(mismatchedItem.candidate_ref as JsonObject).payload_sha256 = '7'.repeat(64)
    assert.throws(
      () => requireMissionHostSnapshot(mismatched, harness.config),
      /proof attention/,
    )

    const triaged = structuredClone(raw)
    const triagedStateItem = ((triaged.current_state as JsonObject).open_candidate_a1 as JsonObject[])[0]!
    const triagedProofItem = (((triaged.executive_orientation as JsonObject)
      .proof_attention as JsonObject).open_candidate_a1 as JsonObject[])[0]!
    triagedStateItem.triage = {
      disposition: 'admission_ready',
      evidence_ref: {
        kind: 'evidence',
        identity: 'candidate-a1-triage.complete-rh-snapshot',
        revision: 1,
        payload_sha256: '6'.repeat(64),
      },
      retrieval_handle: 'evidence:candidate-a1-triage.complete-rh-snapshot@1',
    }
    triagedProofItem.triage = {
      disposition: 'admission_ready',
      evidence_ref: {
        id: 'evidence:candidate-a1-triage.complete-rh-snapshot',
        revision: 1,
      },
      retrieval_handle: 'evidence:candidate-a1-triage.complete-rh-snapshot@1',
    }
    triagedProofItem.stage = 'awaiting_admission_case'
    assert.doesNotThrow(() => requireMissionHostSnapshot(triaged, harness.config))

    const omittedTriage = structuredClone(triaged)
    const omittedTriageItem = (((omittedTriage.executive_orientation as JsonObject)
      .proof_attention as JsonObject).open_candidate_a1 as JsonObject[])[0]!
    omittedTriageItem.triage = null
    omittedTriageItem.stage = 'awaiting_a1_review'
    assert.throws(
      () => requireMissionHostSnapshot(omittedTriage, harness.config),
      /proof attention/,
    )

    const caseReference = {
      id: 'evidence:admission-case.complete-rh-snapshot',
      revision: 1,
      retrieval_handle: 'evidence:admission-case.complete-rh-snapshot@1',
    }
    const reviewReference = {
      id: 'evidence:admission-review.complete-rh-snapshot',
      revision: 1,
      retrieval_handle: 'evidence:admission-review.complete-rh-snapshot@1',
      disposition: 'no_material_objection',
    }
    const decisionReference = {
      id: 'evidence:admission-decision.complete-rh-snapshot',
      revision: 1,
      retrieval_handle: 'evidence:admission-decision.complete-rh-snapshot@1',
      disposition: 'authorize_exact_delta',
    }
    for (const [stage, admission] of [
      ['awaiting_admission_review', { case: caseReference, review: null, decision: null }],
      ['awaiting_admission_decision', { case: caseReference, review: reviewReference, decision: null }],
      ['awaiting_external_canonical_rebind', {
        case: caseReference,
        review: reviewReference,
        decision: decisionReference,
      }],
    ] as const) {
      const staged = structuredClone(triaged)
      const stagedItem = (((staged.executive_orientation as JsonObject)
        .proof_attention as JsonObject).open_candidate_a1 as JsonObject[])[0]!
      stagedItem.stage = stage
      stagedItem.admission = admission
      assert.doesNotThrow(() => requireMissionHostSnapshot(staged, harness.config))
    }

    const impossibleStage = structuredClone(triaged)
    const impossibleStageItem = (((impossibleStage.executive_orientation as JsonObject)
      .proof_attention as JsonObject).open_candidate_a1 as JsonObject[])[0]!
    impossibleStageItem.stage = 'awaiting_external_canonical_rebind'
    impossibleStageItem.admission = { case: caseReference, review: null, decision: decisionReference }
    assert.throws(
      () => requireMissionHostSnapshot(impossibleStage, harness.config),
      /Admission|stage/,
    )

    const terminalPrivate = structuredClone(triaged)
    const terminalPrivateItem = ((terminalPrivate.current_state as JsonObject)
      .open_candidate_a1 as JsonObject[])[0]!
    terminalPrivateItem.admission_rejection = {}
    assert.throws(
      () => requireMissionHostSnapshot(terminalPrivate, harness.config),
      /terminal Admission rejection/,
    )
  } finally { await cleanup(harness) }
})

test('v3 orientation retains complete current Strategy and rejects retired automatic ground', async () => {
  const harness = await createHarness([])
  try {
    const raw = await harness.bridge.hostSnapshot()
    const orientation = raw.executive_orientation as JsonObject
    const expectedStrategy = structuredClone(orientation.current_strategy)
    const snapshot = requireMissionHostSnapshot(raw, harness.config)
    assert.deepEqual(snapshot.executive_orientation.current_strategy, expectedStrategy)
    assert.equal(Object.hasOwn(snapshot.executive_orientation, 'strategy_ground'), false)
    for (const oldShape of [
      { ...orientation, schema_version: 'mathematical_research.executive_orientation.v2' },
      { ...orientation, strategy_ground: [] },
      { ...orientation, strategy_ground: [{ handle: 'strategy:strategy.predecessor@1' }] },
    ]) {
      assert.throws(() => requireMissionHostSnapshot({ ...raw, executive_orientation: oldShape }, harness.config),
        /Executive orientation/)
    }
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
  } finally { await cleanup(harness) }
})

test('private canonical authority facts validate without changing any model launch field', async () => {
  const harness = await createHarness([])
  try {
    const raw = await harness.bridge.hostSnapshot()
    const workspace = prospectiveGoalWorkspacePath(harness.config, '12345678-1234-4234-8234-123456789abc')
    const baseline = inspectMissionGoalLaunch(harness.config, raw, workspace)
    const authority = { source_commit: 'a'.repeat(40), canonical_state_sha256: 'b'.repeat(64), canonical_authority_digest: 'c'.repeat(64) }
    ;(raw.current_state as JsonObject).canonical_authority = authority
    const snapshot = requireMissionHostSnapshot(raw, harness.config)
    assert.deepEqual(snapshot.current_state.canonical_authority, authority)
    const enriched = inspectMissionGoalLaunch(harness.config, raw, workspace)
    assert.deepEqual(enriched, baseline)
    assert.doesNotMatch(JSON.stringify(enriched.request), /canonical_authority|canonical_state_sha256|source_commit/)
    for (const malformed of [null, { ...authority, source_commit: 'HEAD' },
      { ...authority, canonical_state_sha256: 'not-a-digest' },
      { ...authority, canonical_authority_digest: 'not-a-digest' }, { ...authority, extra: 'private' }]) {
      ;(raw.current_state as JsonObject).canonical_authority = malformed
      assert.throws(() => requireMissionHostSnapshot(raw, harness.config), /Host canonical authority/)
    }
    assert.deepEqual(harness.store.saves, [])
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.bridge.semantics, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
    assert.deepEqual(harness.allocatedWorkspaces, [])
  } finally { await cleanup(harness) }
})

test('the initial request preserves independent science, Strategy, scoped corrections and OPEN A1 together', async (t) => {
  const harness = await createHarness([])
  try {
    harness.bridge.openCandidateA1 = [{
      candidate_ref: { kind: 'candidate', identity: 'complete-rh-snapshot', revision: 3,
        payload_sha256: '8'.repeat(64) },
      retrieval_handle: 'candidate:complete-rh-snapshot@3',
      classification: 'purported_complete_rh_proof_or_disproof', disposition: 'proof',
      hold_lifecycle: 'open', canonical_effect: 'none', mathematical_effect: 'none',
    }]
    const raw = scientificSnapshotForTest(await harness.bridge.hostSnapshot())
    const expectedOrientation = structuredClone(raw.executive_orientation as JsonObject)
    const expectedScience = expectedOrientation.scientific_context as JsonObject
    const workspace = prospectiveGoalWorkspacePath(harness.config, '12345678-1234-4234-8234-123456789abc')
    const snapshot = requireMissionHostSnapshot(raw, harness.config)
    const continuity = constructHostContinuity(snapshot, null)
    const contract = { objective: MISSION_PURPOSE.objective as string,
      selectedCapabilities: { localRoots: [], mcpServers: [], apps: [], browser: null } }
    const constructed = constructMissionGoalRequest(contract, snapshot.executive_orientation,
      continuity, workspace, harness.config)
    const inspected = inspectMissionGoalLaunch(harness.config, raw, workspace)
    const requestJson = JSON.stringify(constructed.request)
    assert.deepEqual(inspected.request, constructed.request)
    const initial = instructionJson(String(constructed.request.initialContextText), 'executive_orientation')
    assert.deepEqual(initial, expectedOrientation)
    assert.deepEqual(initial.current_strategy, expectedOrientation.current_strategy)
    assert.deepEqual(initial.proof_attention, expectedOrientation.proof_attention)
    assert.deepEqual(initial.scientific_context, expectedScience)
    assert.equal(Object.hasOwn(initial, 'strategy_ground'), false)
    const sourceChange = (expectedScience.source_changes as JsonObject[])[0]!
    assert.equal((sourceChange.cited_reference as JsonObject).revision, 1)
    assert.equal((sourceChange.current_reference as JsonObject).revision, 2)
    assert.match(requestJson, /NORMALIZATION_CORRECTION/)
    assert.match(requestJson, /ROOT_RESTRICTION/)
    assert.match(requestJson, /SOURCE_RESTRICTION/)
    assert.match(requestJson, /DEEPER_RESTRICTION: fixed-order only, with no uniform-order inference/)
    assert.match(requestJson, /every real x >= 1, not every complex x/)
    assert.match(requestJson, /Lower physical-gap bounds do not establish an upper mismatch estimate/)
    assert.equal(inspected.compatibility.measurement.components.request_json.utf8_bytes,
      Buffer.byteLength(requestJson, 'utf8'))
    t.diagnostic(`CUMULATIVE_HOST_REQUEST_ORDINARY_UTF8_BYTES=${Buffer.byteLength(requestJson, 'utf8')}`)
    const deeper = (expectedScience.treatments as JsonObject[])[1]!
    for (const field of [null, 'known_omissions', 'restricted_uses', 'restrictions', 'independence_treatment']) {
      const malformed = structuredClone(raw)
      const entry = (((malformed.executive_orientation as JsonObject).scientific_context as JsonObject)
        .treatments as JsonObject[])[1]!
      if (field === null) delete entry.context_qualifications
      else delete (entry.context_qualifications as JsonObject)[field]
      assert.throws(() => requireMissionHostSnapshot(malformed, harness.config), Error, `omitted ${field}`)
    }
    for (const invalidMetadata of [
      { ...deeper.context_qualifications as JsonObject, unrelated_treatments: {} },
      { ...deeper.context_qualifications as JsonObject, restrictions: 'not an array' },
      { ...deeper.context_qualifications as JsonObject, independence_treatment: {} },
    ]) {
      const malformed = structuredClone(raw)
      const entry = (((malformed.executive_orientation as JsonObject).scientific_context as JsonObject)
        .treatments as JsonObject[])[1]!
      entry.context_qualifications = invalidMetadata
      assert.throws(() => requireMissionHostSnapshot(malformed, harness.config))
    }
    for (const mutation of ['root qualification', 'root digest drift', 'both bodies']) {
      const malformed = structuredClone(raw)
      const treatments = (((malformed.executive_orientation as JsonObject).scientific_context as JsonObject)
        .treatments as JsonObject[])
      if (mutation === 'root qualification') treatments[0]!.context_qualifications = deeper.context_qualifications
      else if (mutation === 'root digest drift') treatments[0]!.context_reference = {
        ...treatments[0]!.context_reference as JsonObject, payload_sha256: '9'.repeat(64),
      }
      else treatments[1]!.content_ref = { orientation_path: '/scientific_context/treatments/0/content' }
      assert.throws(() => requireMissionHostSnapshot(malformed, harness.config), Error, mutation)
    }
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
    assert.deepEqual(harness.allocatedWorkspaces, [])
  } finally { await cleanup(harness) }
})

test('scientific treatments retain complete inline bodies and reject obsolete or substituted body references', async () => {
  const harness = await createHarness([])
  try {
    const raw = scientificSnapshotForTest(await harness.bridge.hostSnapshot())
    const expected = structuredClone((raw.executive_orientation as JsonObject).scientific_context)
    assert.doesNotThrow(() => requireMissionHostSnapshot(raw, harness.config))
    assert.deepEqual(requireMissionHostSnapshot(raw, harness.config).executive_orientation.scientific_context, expected)
    for (const path of [
      '/strategy_ground/0/summary/treatments/endpoint-to-consumer',
      '/scientific_context/treatments/1/content',
      '/scientific_context/treatments/9/content',
      '/scientific_context/treatments/0/content',
      '/scientific_context/treatments/0/content_ref',
    ]) {
      const malformed = structuredClone(raw)
      const entry = ((((malformed.executive_orientation as JsonObject).scientific_context as JsonObject)
        .treatments as JsonObject[])[0]!)
      delete entry.content
      entry.content_ref = { orientation_path: path }
      assert.throws(() => requireMissionHostSnapshot(malformed, harness.config), Error, path)
    }
  } finally { await cleanup(harness) }
})

test('ordinary successor requests retain the independent scientific projection without Strategy citations', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  try {
    harness.bridge.snapshotTransform = scientificSnapshotForTest
    const expected = ((await harness.bridge.hostSnapshot()).executive_orientation as JsonObject)
      .scientific_context
    assert.equal((await createHost(harness).run()).status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.requests.length, 2)
    for (const request of harness.boundaryFactory.requests) {
      const orientation = instructionJson(String(request.initialContextText), 'executive_orientation')
      assert.deepEqual(orientation.scientific_context, expected)
      assert.equal(Object.hasOwn(orientation, 'strategy_ground'), false)
      assert.equal((orientation.mission as JsonObject).scientific_context_id, 'mission-science')
    }
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
  } finally { await cleanup(harness) }
})

test('source qualification reuse preserves exact current identity, selection and literal qualifications', async () => {
  const harness = await createHarness([])
  try {
    const raw = scientificSnapshotForTest(await harness.bridge.hostSnapshot())
    const science = (raw.executive_orientation as JsonObject).scientific_context as JsonObject
    const changes = science.source_changes as JsonObject[]
    const first = changes[0]!
    const earlierCurrent = structuredClone(first.current_reference as JsonObject)
    const newCurrent = { ...earlierCurrent, revision: 3, payload_sha256: '8'.repeat(64) }
    first.current_reference = newCurrent
    ;((first.read_call as JsonObject).input as JsonObject).ids = ['context:endpoint-source@3']
    const second = { ...structuredClone(first), cited_reference: earlierCurrent,
      qualification: null,
      qualification_ref: { orientation_path: '/scientific_context/source_changes/0/qualification' } }
    changes.push(second)
    const treatment = (science.treatments as JsonObject[])[0]!.content as JsonObject
    const sources = treatment.sources as JsonObject[]
    sources.push({ ...structuredClone(sources[0]!), reference: earlierCurrent })
    const accepted = requireMissionHostSnapshot(raw, harness.config).executive_orientation.scientific_context
    assert.deepEqual(accepted, science)
    assert.match(JSON.stringify(first.qualification), /NORMALIZATION_CORRECTION/)
    for (const [label, mutate] of [
      ['current digest drift', (items: JsonObject[]) => {
        ;(items[1]!.current_reference as JsonObject).payload_sha256 = '9'.repeat(64)
      }],
      ['selection substitution', (items: JsonObject[]) => { items[1]!.selection = { mode: 'whole_context' } }],
      ['qualification chain', (items: JsonObject[]) => {
        items[0]!.qualification = null
        items[0]!.qualification_ref = { orientation_path: '/scientific_context/source_changes/1/qualification' }
      }],
      ['missing qualification', (items: JsonObject[]) => {
        items[1]!.qualification_ref = { orientation_path: '/scientific_context/source_changes/9/qualification' }
      }],
      ['retired Strategy location', (items: JsonObject[]) => {
        items[1]!.qualification_ref = { orientation_path: '/strategy_ground/0/summary' }
      }],
    ] as const) {
      const malformed = structuredClone(raw)
      const changed = (((malformed.executive_orientation as JsonObject).scientific_context as JsonObject)
        .source_changes as JsonObject[])
      mutate(changed)
      assert.throws(() => requireMissionHostSnapshot(malformed, harness.config), Error, label)
    }
  } finally { await cleanup(harness) }
})

test('scientific source correction cannot substitute an identity or expose unrelated enclosing Context bodies', async () => {
  const harness = await createHarness([])
  try {
    const raw = scientificSnapshotForTest(await harness.bridge.hostSnapshot())
    assert.doesNotThrow(() => requireMissionHostSnapshot(raw, harness.config))
    const enclosing = structuredClone(raw)
    const enclosingOrientation = enclosing.executive_orientation as JsonObject
    const enclosingChange = (((enclosingOrientation.scientific_context as JsonObject).source_changes) as JsonObject[])[0]!
    enclosingChange.qualification = null
    enclosingChange.qualification_ref = { orientation_path: '/strategy_ground/0/summary' }
    assert.throws(() => requireMissionHostSnapshot(enclosing, harness.config))
    const wrongIdentity = structuredClone(raw)
    const wrongChange = ((((wrongIdentity.executive_orientation as JsonObject).scientific_context as JsonObject)
      .source_changes) as JsonObject[])[0]!
    ;(wrongChange.current_reference as JsonObject).identity = 'unrelated-context'
    assert.throws(() => requireMissionHostSnapshot(wrongIdentity, harness.config))
    const expanded = structuredClone(raw)
    const expandedChange = ((((expanded.executive_orientation as JsonObject).scientific_context as JsonObject)
      .source_changes) as JsonObject[])[0]!
    ;((expandedChange.qualification as JsonObject).treatments as JsonObject).unselected = {
      question: 'UNRELATED_DEEP_CONTEXT', account: 'This treatment was not selected.', qualifications: [], sources: [],
    }
    assert.throws(() => requireMissionHostSnapshot(expanded, harness.config))
    const silentlyOmitted = structuredClone(raw)
    const omittedChange = ((((silentlyOmitted.executive_orientation as JsonObject).scientific_context as JsonObject)
      .source_changes) as JsonObject[])[0]!
    ;(omittedChange.qualification as JsonObject).treatments = {}
    assert.throws(() => requireMissionHostSnapshot(silentlyOmitted, harness.config))
  } finally { await cleanup(harness) }
})

test('the narrow constructor cannot accept private current state and ignores unrelated private growth', async () => {
  const harness = await createHarness([])
  try {
    const snapshot = requireMissionHostSnapshot(await harness.bridge.hostSnapshot(), harness.config)
    const continuity = constructHostContinuity(snapshot, null)
    const contract = { objective: MISSION_PURPOSE.objective as string,
      selectedCapabilities: { localRoots: [], mcpServers: [], apps: [], browser: null } }
    const workspace = prospectiveGoalWorkspacePath(harness.config, '12345678-1234-4234-8234-123456789abc')
    const before = constructMissionGoalRequest(contract, snapshot.executive_orientation, continuity, workspace, harness.config)
    ;(snapshot.current_state as unknown as JsonObject).arbitrary_private_field = 'NEVER_MODEL_VISIBLE'.repeat(100000)
    const after = constructMissionGoalRequest(contract, snapshot.executive_orientation, continuity, workspace, harness.config)
    assert.deepEqual(after, before)
    assert.doesNotMatch(JSON.stringify(after), /NEVER_MODEL_VISIBLE|root_query_context|cursor_mac_key|strategic_recovery_opening/)
    if (false) {
      // @ts-expect-error The private current state is not a validated Executive orientation.
      constructMissionGoalRequest(contract, snapshot.current_state, continuity, workspace, harness.config)
    }
  } finally { await cleanup(harness) }
})

test('explicit whole-Context source use preserves the complete legacy qualification projection and its cost', async (t) => {
  const harness = await createHarness([])
  try {
    const raw = scientificSnapshotForTest(await harness.bridge.hostSnapshot())
    const science = (raw.executive_orientation as JsonObject).scientific_context as JsonObject
    const treatment = ((science.treatments as JsonObject[])[0]!.content as JsonObject)
    const source = (treatment.sources as JsonObject[])[0]!
    const change = (science.source_changes as JsonObject[])[0]!
    const selection = { mode: 'whole_context' }
    source.selection = selection
    change.selection = selection
    const legacy = {
      purpose: 'Retained exact scientific source.', question: 'What are the literal legacy normalization limits?',
      indispensable_ground: [], owner_source_references: [],
      known_omissions: ['No upper mismatch estimate has been established.'],
      restricted_uses: ['Do not extend the declared real domain to all complex arguments.'],
      independence_treatment: {}, restrictions: ['WHOLE_CONTEXT_LIMITATION '.repeat(100).trim()],
      invalidation_conditions: [],
    }
    change.qualification = { selection, context: legacy }
    const workspace = prospectiveGoalWorkspacePath(harness.config, '12345678-1234-4234-8234-123456789abc')
    const inspected = inspectMissionGoalLaunch(harness.config, raw, workspace)
    const initial = instructionJson(String(inspected.request.initialContextText), 'executive_orientation')
    assert.deepEqual(initial.scientific_context, science)
    assert.ok(inspected.construction.request_json.includes(legacy.restrictions[0]!))
    assert.equal(inspected.compatibility.measurement.components.request_json.utf8_bytes,
      Buffer.byteLength(inspected.construction.request_json, 'utf8'))
    t.diagnostic(`CUMULATIVE_HOST_REQUEST_WHOLE_CONTEXT_UTF8_BYTES=${Buffer.byteLength(inspected.construction.request_json, 'utf8')}`)
  } finally { await cleanup(harness) }
})

for (const [mode, oversizedField] of [
  ['fresh', 'strategy'], ['fresh', 'scientific_context'],
  ['resume', 'strategy'], ['resume', 'scientific_context'],
] as const) {
  test(`inconclusive ${mode} ${oversizedField} accounting retains complete data and does not refuse launch`, async () => {
    const harness = await createHarness(mode === 'resume'
      ? [{ kind: 'usage_limited' }, { kind: 'semantic_stop' }]
      : [{ kind: 'semantic_stop' }])
    try {
      if (mode === 'resume') assert.equal((await createHost(harness).run()).status, 'usage_limited')
      const allocations = harness.allocatedWorkspaces.length
      const authorizations = harness.bridge.authorizations.length
      const oversizedContent = '🧭PRIVATE_MATH'.repeat(40000)
      harness.bridge.snapshotTransform = (snapshot) => {
        scientificSnapshotForTest(snapshot)
        const orientation = snapshot.executive_orientation as JsonObject
        if (oversizedField === 'scientific_context') {
          const science = orientation.scientific_context as JsonObject
          const treatment = (science.treatments as JsonObject[])[0]!
          ;(treatment.content as JsonObject).account = oversizedContent
        } else {
          ;(orientation.current_strategy as JsonObject).selected_bets = [{
            bet: oversizedContent, discriminator: 'Exact oversized selected owner field.',
          }]
        }
        return snapshot
      }
      const oversized = await harness.bridge.hostSnapshot()
      const workspace = harness.store.state.activeGoal?.workspaceRoot
        ?? prospectiveGoalWorkspacePath(harness.config, '12345678-1234-4234-8234-123456789abc')
      const inspection = inspectMissionGoalLaunch(harness.config, oversized, workspace,
        mode === 'resume' ? harness.store.state : null)
      assert.equal(inspection.compatibility.status, 'inconclusive')
      assert.equal(inspection.compatibility.admission_effect, 'informational_only')
      assert.match(inspection.request.initialContextText!, /NORMALIZATION_CORRECTION/)
      assert.ok(inspection.request.initialContextText!.includes(oversizedContent))
      assert.equal(inspection.compatibility.measurement.components.request_json.utf8_bytes,
        Buffer.byteLength(inspection.construction.request_json, 'utf8'))
      for (const text of [inspection.request.developerInstructions,
        inspection.construction.resolved_developer_instructions, JSON.stringify(inspection.compatibility)]) {
        assert.doesNotMatch(text, /PRIVATE_MATH/)
      }

      assert.equal((await createHost(harness).run()).status, 'stopped_semantically')
      const submitted = mode === 'resume'
        ? harness.boundaryFactory.resumeRequests.at(-1)!.request
        : harness.boundaryFactory.requests.at(-1)!
      assert.ok(String(submitted.initialContextText).includes(oversizedContent))
      assert.doesNotMatch(resolvedInstructionsForTest(submitted), /PRIVATE_MATH/)
      assert.equal(harness.allocatedWorkspaces.length, allocations + (mode === 'fresh' ? 1 : 0))
      assert.equal(harness.bridge.authorizations.length, authorizations + (mode === 'fresh' ? 1 : 0))
      assert.equal(harness.boundaryFactory.resumeRequests.length, mode === 'resume' ? 1 : 0)
      assert.deepEqual(harness.bridge.failures, [])
      assert.equal(harness.store.state.activeGoal, null)
    } finally { await cleanup(harness) }
  })
}

test('a stale fresh current-state cut is rejected before Goal allocation or boundary effects', async () => {
    const harness = await createHarness([{ kind: 'semantic_stop' }])
    try {
      const before = structuredClone(harness.store.state)
      const saves = harness.store.saves.length
      const allocations = harness.allocatedWorkspaces.length
      const authorizations = harness.bridge.authorizations.length
      const boundaries = harness.boundaryFactory.boundaries.length
      const resumes = harness.boundaryFactory.resumeRequests.length
      let currentStateReads = 0
      harness.bridge.snapshotTransform = (snapshot) => {
        currentStateReads += 1
        if (currentStateReads === 2) {
          const currentState = snapshot.current_state as JsonObject
          currentState.observed_project_commit =
            Number(currentState.observed_project_commit) + 1
          const cut = snapshot.authorization_cut as JsonObject
          cut.project_commit = Number(cut.project_commit) + 1
          cut.current_root_digest = 'e'.repeat(64)
          cut.transition_head_digest = 'f'.repeat(64)
        }
        return snapshot
      }

      await assert.rejects(
        createHost(harness).run(),
        /changed during pre-effect construction/,
      )
      assert.equal(currentStateReads, 2)
      assert.deepEqual(harness.store.state, before)
      assert.equal(harness.store.saves.length, saves)
      assert.equal(harness.allocatedWorkspaces.length, allocations)
      assert.equal(harness.bridge.authorizations.length, authorizations)
      assert.equal(harness.boundaryFactory.boundaries.length, boundaries)
      assert.equal(harness.boundaryFactory.resumeRequests.length, resumes)
      assert.deepEqual(harness.bridge.failures, [])
      assert.equal(harness.bridge.reconstructCalls, 0)
    } finally {
      await cleanup(harness)
    }
  })

test('a suspended cut that moves after boundary creation rebuilds once and resumes only the fresh request', async () => {
  const harness = await createHarness([
    { kind: 'usage_limited' },
    { kind: 'semantic_stop' },
  ])
  try {
    assert.equal((await createHost(harness).run()).status, 'usage_limited')
    const allocations = harness.allocatedWorkspaces.length
    const authorizations = harness.bridge.authorizations.length
    const resumes = harness.boundaryFactory.resumeRequests.length
    const validationStart = harness.bridge.suspendedCutValidations.length
    const cutA = harness.bridge.authorizationCut()
    let cutB: MissionAuthorizationCut | null = null
    harness.boundaryFactory.onCreate = () => {
      if (cutB === null) {
        harness.bridge.setContinuation('pause')
        cutB = harness.bridge.advanceStoreCut()
      }
    }

    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.ok(cutB !== null)
    assert.deepEqual(
      harness.bridge.suspendedCutValidations
        .slice(validationStart)
        .map(({ expectedCut }) => expectedCut),
      [cutA, cutA, cutB, cutB],
    )
    assert.equal(harness.allocatedWorkspaces.length, allocations)
    assert.equal(harness.bridge.authorizations.length, authorizations)
    assert.equal(harness.boundaryFactory.resumeRequests.length, resumes + 1)
    const resumed = harness.boundaryFactory.resumeRequests.at(-1)
    assert.equal(
      instructionJson(
        String(resumed?.request.initialContextText),
        'executive_orientation',
      ).current_strategy instanceof Object,
      true,
    )
    assert.equal(
      (instructionJson(
        String(resumed?.request.initialContextText),
        'executive_orientation',
      ).current_strategy as JsonObject).mission_continuation,
      'pause',
    )
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('stale authorization cut discards only its local plan and retries once through ordinary gates', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  let attempts = 0
  let cutB: MissionAuthorizationCut | null = null
  harness.bridge.onAuthorize = () => {
    attempts += 1
    if (attempts === 1) {
      harness.bridge.setContinuation('pause')
      cutB = harness.bridge.advanceStoreCut()
    }
  }
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizationCuts.length, 2)
    assert.notDeepEqual(
      harness.bridge.authorizationCuts[0],
      harness.bridge.authorizationCuts[1],
    )
    assert.deepEqual(harness.bridge.authorizationCuts[1], cutB)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 1)
    assert.equal(harness.boundaryFactory.boundaries.length, 1)
    const orientation = instructionJson(
      String(harness.boundaryFactory.requests[0]?.initialContextText),
      'executive_orientation',
    )
    assert.equal(
      (orientation.current_strategy as JsonObject).mission_continuation,
      'pause',
    )
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('stale authorization cut stops after one ordinary fresh retry', async () => {
  const harness = await createHarness([])
  harness.bridge.onAuthorize = () => {
    harness.bridge.advanceStoreCut()
  }
  try {
    await assert.rejects(
      createHost(harness).run(),
      /changed again during the one ordinary pre-effect retry/,
    )
    assert.equal(harness.bridge.authorizationCuts.length, 2)
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.allocatedWorkspaces, [])
    assert.deepEqual(harness.boundaryFactory.boundaries, [])
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('stale authorization cut never adopts a concurrently active owner Epoch', async () => {
  const harness = await createHarness([])
  harness.bridge.onAuthorize = () => {
    const cut = harness.bridge.advanceStoreCut()
    harness.bridge.nextLatestExecutiveEpochOverride = {
      executive_epoch_id: 'epoch.concurrent-owner',
      state: 'authorized',
      goal_thread_id: null,
      last_event_project_commit: cut.project_commit,
      checkpoint_ref: null,
      reconciliation: null,
    }
  }
  try {
    await assert.rejects(
      createHost(harness).run(),
      /changed to an active Executive Epoch/,
    )
    assert.equal(harness.bridge.authorizationCuts.length, 1)
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.allocatedWorkspaces, [])
    assert.deepEqual(harness.boundaryFactory.boundaries, [])
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('terminal owner history after a stale cut still permits the one fresh retry', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  let attempts = 0
  harness.bridge.onAuthorize = () => {
    attempts += 1
    if (attempts === 1) {
      harness.bridge.seedFailure(null, 'concurrent_terminal_history', 'epoch.concurrent-terminal')
    }
  }
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizationCuts.length, 2)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 1)
    assert.equal(harness.boundaryFactory.boundaries.length, 1)
  } finally {
    await cleanup(harness)
  }
})

test('a fresh canonical-result closeout discovered after stale authorization stops before retry effects', async () => {
  const harness = await createHarness([])
  harness.bridge.onAuthorize = () => {
    harness.bridge.onAuthorize = null
    harness.bridge.setContinuation('closeout')
    harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
    harness.bridge.advanceStoreCut()
  }
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizationCuts.length, 1)
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.allocatedWorkspaces, [])
    assert.deepEqual(harness.boundaryFactory.boundaries, [])
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('a fresh historical unsolved closeout discovered after stale authorization uses the bounded retry', async () => {
  const harness = await createHarness([{
    kind: 'checkpoint',
    workerCount: 0,
    missionContinuation: 'continue',
  }])
  harness.bridge.onAuthorize = () => {
    harness.bridge.onAuthorize = null
    harness.bridge.setContinuation('closeout')
    harness.bridge.advanceStoreCut()
  }
  try {
    const result = await createHost(
      harness,
      null,
      async () => true,
    ).run()

    assert.equal(result.status, 'operator_stopped_after_checkpoint')
    assert.equal(harness.bridge.authorizationCuts.length, 2)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 1)
    assert.equal(harness.boundaryFactory.boundaries.length, 1)
    assert.equal(
      instructionJson(String(harness.boundaryFactory.requests[0]?.initialContextText), 'host_continuity').launch_mode,
      'reopen_historical_closeout',
    )
  } finally {
    await cleanup(harness)
  }
})

test('local compatibility uses exact catalog allowance and one resolved base template, not a tokenizer estimate', async (t) => {
  const harness = await createHarness([])
  try {
    const snapshot = await harness.bridge.hostSnapshot()
    const workspace = prospectiveGoalWorkspacePath(harness.config, '12345678-1234-4234-8234-123456789abc')
    const result = inspectMissionGoalLaunch(harness.config, snapshot, workspace)
    const report = result.compatibility
    assert.equal(report.status, 'compatible')
    assert.equal(report.context_allowance.tokens, 258400)
    assert.equal(report.context_allowance.model_context_window_tokens, 272000)
    assert.equal(report.context_allowance.effective_context_window_percent, 95)
    assert.equal(report.compaction_threshold.configured_tokens, null)
    assert.equal(report.compaction_threshold.effective_tokens, null)
    assert.equal(report.compaction_threshold.runtime_default_established, false)
    assert.equal(report.measurement.base_instructions_count, 1)
    assert.equal(report.measurement.tokenizer_estimate, null)
    assert.equal(report.measurement.tokenizer_estimate_is_certification, false)
    assert.equal(report.measurement.method, 'utf8_byte_level_bpe_conservative_upper_bound')
    const resolvedRequestJson = JSON.stringify({
      ...JSON.parse(result.construction.request_json),
      developerInstructions: result.construction.resolved_developer_instructions,
    })
    assert.ok(Buffer.byteLength(resolvedRequestJson, 'utf8') > Buffer.byteLength(result.construction.request_json, 'utf8'))
    assert.equal(report.measurement.accounted_token_upper_bound, Buffer.byteLength(resolvedRequestJson, 'utf8') +
      report.measurement.components.resolved_base_instructions.utf8_bytes)
    assert.equal(report.measurement.components.resolved_request_json.sha256, createHash('sha256').update(resolvedRequestJson).digest('hex'))
    assert.equal(report.measurement.components.request_json.sha256, createHash('sha256').update(result.construction.request_json).digest('hex'))
    assert.ok(report.unmeasured.includes('provider_message_framing'))
    assert.ok(report.unmeasured.includes('runtime_builtin_tools'))
    assert.equal(report.provider_wire_equivalence_claimed, false)
    assert.doesNotMatch(JSON.stringify(report), /Act as the sole|Current Executive orientation|cursor_mac_key/)
    const catalogBytes = await fs.promises.readFile(harness.config.modelCatalogPath)
    const catalog = JSON.parse(catalogBytes.toString('utf8')) as { models: JsonObject[] }
    catalog.models[0]!.effective_context_window_percent = 0
    const readFileSync = fs.readFileSync
    const malformedRead = t.mock.method(fs, 'readFileSync', (...args: Parameters<typeof fs.readFileSync>) => {
      const [file, options] = args
      if (file === harness.config.modelCatalogPath && options === 'utf8') return JSON.stringify(catalog)
      return readFileSync(...args)
    })
    try {
      assert.throws(() => inspectMissionGoalLaunch(harness.config, snapshot, workspace), /accounting is not established/)
    } finally {
      malformedRead.mock.restore()
      assert.deepEqual(await fs.promises.readFile(harness.config.modelCatalogPath), catalogBytes)
    }
    assert.equal(inspectMissionGoalLaunch(harness.config, snapshot, workspace).compatibility.status, 'compatible')
    assert.deepEqual(harness.allocatedWorkspaces, [])
    assert.deepEqual(harness.bridge.authorizations, [])
  } finally { await cleanup(harness) }
})

test('root query signing context is transient Host-private and stable only within one Host instance', async () => {
  const harness = await createHarness([{ kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' }, { kind: 'semantic_stop' }])
  try {
    await createHost(harness).run()
    const keys = harness.bridge.rootQueryContexts.map((context) => context!.cursor_mac_key)
    assert.ok(keys.length > 2)
    assert.match(keys[0]!, /^[0-9a-f]{64}$/)
    assert.ok(keys.every((key) => key === keys[0]))
    assert.doesNotMatch(JSON.stringify(harness.boundaryFactory.requests), new RegExp(keys[0]!))
    assert.doesNotMatch(JSON.stringify(harness.store.state), new RegExp(keys[0]!))
    const next = await createHarness([{ kind: 'semantic_stop' }])
    try {
      await createHost(next).run()
      assert.notEqual(next.bridge.rootQueryContexts[0]!.cursor_mac_key, keys[0])
    } finally { await cleanup(next) }
  } finally { await cleanup(harness) }
})

test('prospective Goal workspace selection is pure and rejects noncanonical identities', async () => {
  const harness = await createHarness([])
  try {
    const identity = '12345678-1234-4234-8234-123456789abc'
    const workspace = prospectiveGoalWorkspacePath(harness.config, identity)
    assert.equal(workspace, path.join(harness.config.goalsRoot, `epoch-${identity}`))
    assert.equal(fs.existsSync(workspace), false)
    assert.deepEqual(fs.readdirSync(harness.config.goalsRoot), [])
    for (const invalid of ['', '../escape', identity.toUpperCase(),
      '12345678-1234-7234-8234-123456789abc', '12345678-1234-4234-0234-123456789abc']) {
      assert.throws(() => prospectiveGoalWorkspacePath(harness.config, invalid), /canonical UUIDv4/)
    }
    assert.deepEqual(fs.readdirSync(harness.config.goalsRoot), [])
    assert.deepEqual(harness.allocatedWorkspaces, [])
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.store.saves, [])
  } finally {
    await cleanup(harness)
  }
})

test('workspace allocation failure releases only its durable exact empty planned directory', async () => {
  const harness = await createHarness([])
  const originalAllocate = harness.allocateWorkspace
  let plannedWorkspace: string | null = null
  harness.allocateWorkspace = async (prospectivePath) => {
    assert.ok(prospectivePath)
    plannedWorkspace = await originalAllocate(prospectivePath)
    assert.equal(harness.store.state.activeGoal?.phase, 'authorized')
    assert.equal(harness.store.state.activeGoal?.workspaceRoot, plannedWorkspace)
    assert.deepEqual(await fs.promises.readdir(plannedWorkspace), [])
    throw new Error('simulated allocation failure after exact directory creation')
  }
  try {
    await assert.rejects(
      createHost(harness).run(),
      /simulated allocation failure after exact directory creation/,
    )

    assert.ok(plannedWorkspace)
    assert.equal(fs.existsSync(plannedWorkspace), false)
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.deepEqual(harness.boundaryFactory.boundaries, [])
  } finally {
    await cleanup(harness)
  }
})

test('productive epoch uses adaptive functional pods and preserves useful worker material', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 1, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.onAuthorize = () => {
    assert.equal(harness.store.state.activeGoal?.phase, 'planned')
    assert.equal(harness.store.state.activeGoal?.threadId, null)
    assert.equal(harness.store.state.activeGoal?.executiveEpochId, null)
  }
  harness.bridge.onBind = (input) => {
    assert.equal(harness.store.state.activeGoal?.phase, 'pending')
    assert.equal(harness.store.state.activeGoal?.threadId, input.rootThreadId)
    assert.equal(harness.store.state.activeGoal?.executiveEpochId, input.executiveEpochId)
    assert.equal(harness.store.state.activeGoal?.workspaceRoot, input.workspaceRoot)
  }
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.historicalGrantRequests.length, 0)
    assert.equal(harness.bridge.delegatedReads.length, 0)
    assert.equal(harness.bridge.nativeMaterial.length, 2)
    assert.deepEqual(Object.keys(harness.bridge.nativeMaterial[0] as Record<string, unknown>).sort(), [
      'childThreadId',
      'content',
      'materialKind',
      'observationId',
      'parentThreadId',
      'rootThreadId',
    ])
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as Record<string, unknown>).operation),
      ['interpret_material', 'record_strategy', 'checkpoint', 'record_strategy', 'checkpoint'],
    )
    assert.equal(
      harness.bridge.semantics.some(({ request }) =>
        isRecordForTest(request) && request.operation === 'record_candidate'),
      false,
    )
    assert.equal(
      JSON.stringify(harness.bridge.semantics).includes('complete_target_claim'),
      false,
    )
    assert.equal(
      harness.bridge.semantics.every(({ request }) =>
        !('executiveEpochId' in (request as Record<string, unknown>))),
      true,
    )
    assert.deepEqual(
      harness.bridge.semantics
        .map(({ request }) => request as Record<string, unknown>)
        .filter(({ operation }) => operation === 'checkpoint'),
      [
        {
          schema_version: 'mathematical_research.mission_semantic_request.v1',
          operation: 'checkpoint',
          input: {},
        },
        {
          schema_version: 'mathematical_research.mission_semantic_request.v1',
          operation: 'checkpoint',
          input: {},
        },
      ],
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ binding }) => binding),
      [
        {
          rootThreadId: 'thread.0',
          executiveEpochId: 'epoch.0',
        },
        {
          rootThreadId: 'thread.0',
          executiveEpochId: 'epoch.0',
        },
        {
          rootThreadId: 'thread.0',
          executiveEpochId: 'epoch.0',
        },
        {
          rootThreadId: 'thread.1',
          executiveEpochId: 'epoch.1',
        },
        {
          rootThreadId: 'thread.1',
          executiveEpochId: 'epoch.1',
        },
      ],
    )
    const request = harness.boundaryFactory.requests[0]!
    const prompt = resolvedInstructionsForTest(request)
    assert.match(prompt, /noncanonical research epoch/)
    assert.match(prompt, /sole writer of noncanonical Mission research state/)
    assert.match(prompt, /Ordinary research may write only `continue`/)
    assert.match(prompt, /ordinary branch-read grant/)
    assert.match(prompt, /rh_mission_research_read and rh_mission_research_read_page/)
    assert.match(prompt, /Grants are mutually exclusive per child/)
    assert.match(prompt, /no record for every thought|require a record for every thought/)
    assert.match(prompt, /Native assignment and output capture must retain attributable custody at both delegation levels/)
    assert.match(prompt, /complete_target_claim/)
    assert.match(prompt, /OPEN A1 atomically/)
    assert.match(prompt, /authorize_exact_delta/)
    assert.match(prompt, /Neither lane grants canonical writing or public communication/)
    assert.match(prompt, /research request does not authorize it to repair source, test, publish, install, or administer runtime/)
    assertHistoricalAdvisoryMaterialityContract(prompt)
    assert.deepEqual((request.nativeAgentRoles as Array<{ name: string }>).map(({ name }) => name),
      ['rh_researcher', 'rh_helper', 'rh_historical', 'rh_restricted_review', 'default', 'worker', 'explorer'])
    assert.doesNotMatch(prompt, /retained_unintegrated_outputs|failed_epochs_since_checkpoint/)
    assert.doesNotMatch(prompt, /# Vector Mosaic repository instructions|codex_thread_sketchpad/)
    assert.doesNotMatch(prompt, /must create one Context per worker|must create one Evidence per worker|must annotate every output/i)
    assert.equal('tokenBudget' in harness.boundaryFactory.requests[0]!, false)
    assert.equal('wallClockMs' in harness.boundaryFactory.requests[0]!, false)
    assert.deepEqual(request.instructionHierarchy, {
      authorityRoot: path.join(harness.config.repoRoot, RH_INSTRUCTION_RELATIVE_PATH),
      targetPath: path.join(harness.config.repoRoot, RH_INSTRUCTION_RELATIVE_PATH, 'executive'),
    })
    assert.equal(harness.boundaryFactory.requests[0]?.objective, MISSION_OBJECTIVE)
    assert.deepEqual(harness.boundaryFactory.requests[0]?.environments, [
      {
        environmentId: 'local',
        cwd: harness.boundaryFactory.boundaries[0]?.workspaceRoot,
      },
    ])
    assert.deepEqual(harness.boundaryFactory.requests[0]?.selectedCapabilities, {
      localRoots: [],
      mcpServers: [],
      apps: [],
      browser: null,
    })
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
    assert.equal(harness.bridge.bindings.length, 2)
    const successorRequest = harness.boundaryFactory.requests[1]!
    assert.deepEqual(successorRequest.environments, [
      {
        environmentId: 'local',
        cwd: harness.boundaryFactory.boundaries[1]?.workspaceRoot,
      },
    ])
    assert.deepEqual(successorRequest.selectedCapabilities, {
      localRoots: [],
      mcpServers: [],
      apps: [],
      browser: null,
    })
    assert.deepEqual(
      (successorRequest.dynamicTools as Array<{ name: string }>).map(({ name }) => name),
      [
        'rh_mission',
        'rh_mission_history_grant',
        'rh_mission_history',
        'rh_mission_research_read_grant',
        'rh_mission_research_read',
        'rh_mission_a1_review_grant',
        'rh_mission_a1_review',
        'rh_mission_admission_open',
        'rh_mission_admission_grant',
        'rh_mission_admission',
        'rh_formal_attempt',
        'rh_mission_page',
        'rh_mission_history_page',
        'rh_mission_research_read_page',
        'rh_mission_a1_review_page',
        'rh_mission_admission_page',
      ],
    )
    const missionTool = (successorRequest.dynamicTools as Array<{
      name: string
      description: string
    }>).find(({ name }) => name === 'rh_mission')
    assert.ok(missionTool)
    assert.match(missionTool.description, /Safe first call/)
    assert.match(missionTool.description, /Usage has no effect/)
    assert.match(missionTool.description, /other nine operations execute immediately/)
    assert.match(missionTool.description, /current Strategy is active direction/)
    assert.match(missionTool.description, /checkpoint is a terminal owner handoff/i)
    assert.match(missionTool.description, /orient refreshes Mission, independent scientific Context, complete current Strategy, proof attention and formal attention/)
    assert.match(missionTool.description, /Strategy references remain exact handles, not automatic owner-body expansion or an all-owner inventory/)
    assert.match(missionTool.description, /search, inventory, checkpoint, changes_since_checkpoint, hooks, captures, and proof_attention/)
    assert.match(missionTool.description, /read consumes an exact selected handle/)
    const supportTools = successorRequest.dynamicTools as Array<{
      name: string
      description: string
      inputSchema: JsonObject
    }>
    const formalTool = supportTools.find(({ name }) => name === 'rh_formal_attempt')
    const pageTool = supportTools.find(({ name }) => name === 'rh_mission_page')
    assert.ok(formalTool)
    assert.ok(pageTool)
    assert.match(formalTool.description, /Effectful operational invocation/i)
    assert.match(formalTool.description, /copy its exact selected_bet_sha256/i)
    assert.match(formalTool.description, /record_strategy, orient, or retrieve/i)
    assert.match(formalTool.description, /Never calculate or invent it/i)
    assert.match(pageTool.description, /Read-only continuation/i)
    assert.match(pageTool.description, /copy its handle and exact next_offset/i)
    assert.match(
      String(
        ((formalTool.inputSchema.properties as JsonObject).selected_bet_sha256 as JsonObject)
          .description,
      ),
      /current Strategy owner state/i,
    )
    assert.match(
      String(((pageTool.inputSchema.properties as JsonObject).offset as JsonObject).description),
      /exact next_offset/i,
    )
    const missionInputSchema = (missionTool as unknown as { inputSchema: JsonObject }).inputSchema
    assert.equal('oneOf' in missionInputSchema, false)
    assert.deepEqual(
      ((missionInputSchema.properties as JsonObject).operation as JsonObject).enum,
      ['usage', ...SEMANTIC_OPERATIONS],
    )
    assert.equal('schema_version' in (missionInputSchema.properties as JsonObject), false)
    const successorInstructions = resolvedInstructionsForTest(successorRequest)
    assert.match(successorInstructions, /fresh epoch begins undecided/i)
    assertHistoricalAdvisoryMaterialityContract(successorInstructions)
    assert.doesNotMatch(successorInstructions, /Recovery-first task/)
    assert.doesNotMatch(successorInstructions, /Current Executive orientation:\n/)
    assert.match(successorInstructions, /Polling timeouts, elapsed time, provider telemetry, worker counts, tool failures/)
    const injected = instructionJson(String(successorRequest.initialContextText), 'executive_orientation')
    assert.deepEqual(Object.keys(injected).sort(), ['schema_version', 'target', 'mission', 'current_strategy',
      'scientific_context', 'continuity', 'proof_attention', 'formal_attention', 'retrieval'].sort())
    assert.deepEqual((injected.mission as JsonObject).purpose, MISSION_PURPOSE)
    assert.equal('execution_policy' in (injected.mission as JsonObject), false)
    const injectedJson = JSON.stringify(injected)
    assert.match(injectedJson, /cannot be completed/)
    assert.doesNotMatch(
      injectedJson,
      /owner_panorama|current_mission|latest_checkpoint|latest_executive_epoch|canonical_effect|"events"|receipt|score|quota|transcript|readable_content|pending_capture_locators|raw_captures/i,
    )
    assert.doesNotMatch(
      successorInstructions,
      /first use|before owner Executive Epoch authority/i,
    )
  } finally {
    await cleanup(harness)
  }
})

test('explicit checkpoint-stop intent exits after the committed checkpoint without starting a successor', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  let pending = true
  let consumeCalls = 0
  try {
    const sibling = path.join(harness.config.goalsRoot, 'owner-retained-sibling')
    await fs.promises.mkdir(sibling)
    const result = await createHost(harness, null, async () => {
      consumeCalls += 1
      if (!pending) {
        return false
      }
      pending = false
      return true
    }).run()

    assert.equal(result.status, 'operator_stopped_after_checkpoint')
    assert.equal(pending, false)
    assert.equal(consumeCalls, 1)
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.boundaryFactory.boundaries[0]?.workspacePresentAtStop, true)
    assert.equal(fs.existsSync(harness.allocatedWorkspaces[0]!), false)
    assert.equal(fs.existsSync(sibling), true)
    assert.deepEqual(await fs.promises.readdir(harness.config.goalsRoot), [
      'owner-retained-sibling',
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs, [
      'owner_checkpoint',
    ])
  } finally {
    await cleanup(harness)
  }
})

test('checkpoint-source observation failure cannot change the checkpoint result or Host phase', async (t) => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.checkpointSourceFailureWarning = {
    code: 'checkpoint_source_snapshot_skipped_writer_preserved',
    reason_code: 'checkpoint_source_busy',
    database_bytes: null,
    handoff_elapsed_ms: 3,
  }
  const observer = new MissionExecutionObserver(harness.config, {
    incarnation: 'checkpoint-source-observer-failure',
    limits: { diskReserveBytes: 0, flushMilliseconds: 60_000 },
  })
  let observationCalls = 0
  t.mock.method(observer, 'observeCheckpointSourceFailure', () => {
    observationCalls += 1
    throw new Error('injected checkpoint-source observer failure')
  })
  try {
    const result = await createHost(
      harness,
      null,
      async () => true,
      undefined,
      observer,
    ).run()

    assert.equal(result.status, 'operator_stopped_after_checkpoint')
    assert.equal(observationCalls, 1)
    const checkpointed = harness.store.saves.find(
      ({ activeGoal }) => activeGoal?.phase === 'checkpointed',
    )
    assert.ok(checkpointed?.activeGoal)
    assert.equal(checkpointed.activeGoal.phase, 'checkpointed')
    assert.equal(checkpointed.activeGoal.failureReason, null)
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs, [
      'owner_checkpoint',
    ])
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await observer.close()
    await cleanup(harness)
  }
})

test('checkpointed Host-state save failure cannot turn a committed checkpoint into a failed tool call', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.store.failCheckpointedSaveOnce = true
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/i)
        assert.match(String(error.cause), /checkpointed Host-state save failure/)
        return true
      },
    )

    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'record_strategy').length, 1)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs, [
      'owner_checkpoint',
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    const checkpointPayloads = harness.boundaryFactory.boundaries[0]?.rootToolResultPayloads.filter(
      (payload) => payload.operation === 'checkpoint',
    )
    assert.equal(checkpointPayloads?.length, 1)
    assert.equal(checkpointPayloads?.[0]?.status, 'completed')
    assert.equal((checkpointPayloads?.[0]?.result as JsonObject).state, 'checkpointed')
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as JsonObject).state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('unverified checkpoint bridge output is reconciled to the exact committed checkpoint before containment', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.checkpointDispositionUnverified = true
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /bridge result was unverified/i)
        assert.ok(error.cause instanceof CheckpointDispositionUnverifiedBridgeError)
        return true
      },
    )

    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'record_strategy').length, 1)
    assert.equal(harness.bridge.hostSnapshotCalls >= 2, true)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs, [
      'owner_checkpoint',
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    const checkpointPayload = harness.boundaryFactory.boundaries[0]?.rootToolResultPayloads.find(
      (payload) => payload.operation === 'checkpoint',
    )
    assert.equal(checkpointPayload?.status, 'completed')
    assert.equal((checkpointPayload?.result as JsonObject).state, 'checkpointed')
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('a committed-checkpoint containment failure is not retried on the same boundary and recovers on restart', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  let boundaryCount = 0
  harness.boundaryFactory.onCreate = (boundary) => {
    if (boundaryCount === 0) {
      boundary.stopFailuresRemaining = 1
    }
    boundaryCount += 1
  }
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/i)
        assert.match(error.message, /containment did not complete/i)
        assert.match(String(error.cause), /first owner-checkpoint containment failure/)
        return true
      },
    )

    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'record_strategy').length, 1)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs, [
      'owner_checkpoint',
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.equal(harness.store.state.activeGoal?.phase, 'checkpointed')
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.deepEqual(harness.bridge.failures, [])

    const recovered = await createHost(harness, null, async () => true).run()

    assert.equal(recovered.status, 'operator_stopped_after_checkpoint')
    assert.deepEqual(harness.boundaryFactory.boundaries[1]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('source-handoff and checkpointed-save failures aggregate only after owner checkpoint containment', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.checkpointCommittedSourceHandoffSafety = 'unsafe'
  harness.store.failCheckpointedSaveOnce = true
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof AggregateError)
        assert.equal(error.errors.length, 2)
        assert.ok(error.errors[0] instanceof GoalEpochSharedAuthorityLossError)
        assert.ok(error.errors[1] instanceof GoalEpochMissionConsistencyError)
        assert.match(String((error.errors[1] as Error).cause), /checkpointed Host-state save failure/)
        return true
      },
    )

    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'record_strategy').length, 1)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs, [
      'owner_checkpoint',
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('unsafe source handoff completes exact checkpoint containment before stopping without replay', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.checkpointCommittedSourceHandoffSafety = 'unsafe'
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochSharedAuthorityLossError)
        assert.match(error.message, /checkpoint committed/)
        assert.ok(error.cause instanceof CheckpointCommittedSourceHandoffBridgeError)
        assert.equal(error.cause.continuationSafety, 'unsafe')
        return true
      },
    )

    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'record_strategy').length, 1)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.deepEqual(
      harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs,
      ['owner_checkpoint'],
    )
    const checkpointPayload = harness.boundaryFactory.boundaries[0]?.rootToolResultPayloads.find(
      (payload) => payload.operation === 'checkpoint',
    )
    assert.ok(checkpointPayload)
    assert.deepEqual(Object.keys(checkpointPayload).sort(), [
      'error',
      'operation',
      'result',
      'schema_version',
      'status',
    ])
    assert.equal(
      checkpointPayload.schema_version,
      'mathematical_research.mission_semantic_result.v1',
    )
    assert.equal(checkpointPayload.status, 'completed')
    assert.equal((checkpointPayload.result as JsonObject).state, 'checkpointed')
    assert.equal(checkpointPayload.error, null)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.boundaryFactory.boundaries[0]?.workspacePresentAtStop, true)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('unverified source handoff completes checkpoint containment before consistency failure', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.checkpointCommittedSourceHandoffSafety = 'unverified'
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/)
        assert.ok(error.cause instanceof CheckpointCommittedSourceHandoffBridgeError)
        assert.equal(error.cause.continuationSafety, 'unverified')
        return true
      },
    )
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.deepEqual(
      harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs,
      ['owner_checkpoint'],
    )
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('safe-writer source integrity failure completes checkpoint containment before consistency failure', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.checkpointCommittedSourceHandoffSafety = 'safe'
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/)
        assert.ok(error.cause instanceof CheckpointCommittedSourceHandoffBridgeError)
        assert.equal(error.cause.continuationSafety, 'safe')
        assert.equal(
          error.cause.sourceHandoffFailureCode,
          'checkpoint_source_path_unsafe',
        )
        return true
      },
    )
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.deepEqual(
      harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs,
      ['owner_checkpoint'],
    )
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('unsafe source handoff on a resumed epoch cannot be swallowed into successor admission', async () => {
  const harness = await createHarness([
    { kind: 'usage_limited' },
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  try {
    const first = await createHost(harness).run()
    assert.equal(first.status, 'usage_limited')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    harness.bridge.checkpointCommittedSourceHandoffSafety = 'unsafe'

    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochSharedAuthorityLossError)
        assert.ok(error.cause instanceof CheckpointCommittedSourceHandoffBridgeError)
        return true
      },
    )

    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 1)
    assert.equal(harness.boundaryFactory.boundaries.length, 2)
    assert.equal(harness.bridge.semantics.filter(({ request }) =>
      isRecordForTest(request) && request.operation === 'checkpoint').length, 1)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[1]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.deepEqual(
      harness.boundaryFactory.boundaries[1]?.checkpointTerminalHandoffs,
      ['owner_checkpoint'],
    )
    assert.equal(harness.boundaryFactory.boundaries[1]?.workspacePresentAtStop, true)
    assert.equal(harness.store.state.activeGoal, null)
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('terminal cleanup preserves a nonempty canonical Goal workspace without sweeping siblings', async () => {
  const harness = await createHarness([{
    kind: 'checkpoint',
    workerCount: 0,
    missionContinuation: 'closeout',
    leaveWorkspaceNonempty: true,
  }])
  try {
    const sibling = path.join(harness.config.goalsRoot, 'owner-retained-sibling')
    await fs.promises.mkdir(sibling)

    const result = await createHost(harness).run()
    const workspace = harness.allocatedWorkspaces[0]!

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.boundaries[0]?.workspacePresentAtStop, true)
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(
      await fs.promises.readFile(path.join(workspace, 'retained-owner-material.txt'), 'utf8'),
      'The Host must preserve this nonempty terminal workspace.\n',
    )
    assert.equal(fs.existsSync(sibling), true)
    assert.deepEqual((await fs.promises.readdir(harness.config.goalsRoot)).sort(), [
      path.basename(workspace),
      'owner-retained-sibling',
    ].sort())
  } finally {
    await cleanup(harness)
  }
})

test('terminal cleanup retries an already-absent exact workspace after the Goal-clear save fails', async () => {
  const harness = await createHarness([{
    kind: 'checkpoint',
    workerCount: 0,
    missionContinuation: 'closeout',
  }])
  harness.store.failTerminalGoalClearOnce = true
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/i)
        assert.match(error.message, /terminal boundary cleanup failed/i)
        assert.match(String(error.cause), /simulated terminal Goal-clear save failure/)
        return true
      },
    )
    const workspace = harness.allocatedWorkspaces[0]!

    assert.equal(harness.boundaryFactory.boundaries[0]?.workspacePresentAtStop, true)
    assert.equal(fs.existsSync(workspace), false)
    assert.notEqual(harness.store.state.activeGoal, null)

    const replay = await createHost(harness).run()

    assert.equal(replay.status, 'stopped_semantically')
    assert.equal(fs.existsSync(workspace), false)
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.boundaryFactory.boundaries.length, 1)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
  } finally {
    await cleanup(harness)
  }
})

test('restart consumes a checkpoint-stop intent before admitting a successor', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  harness.bridge.seedCheckpoint('thread.predecessor', 'continue')
  let consumeCalls = 0
  try {
    const result = await createHost(harness, null, async () => {
      consumeCalls += 1
      return true
    }).run()

    assert.equal(result.status, 'operator_stopped_after_checkpoint')
    assert.equal(consumeCalls, 1)
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('single-epoch canary waits through transient live blocking and stops after the exact checkpointed epoch', async () => {
  const harness = await createHarness([
    {
      kind: 'checkpoint',
      workerCount: 0,
      missionContinuation: 'continue',
      transientBlockedSameTurnBeforeCheckpoint: true,
    },
    { kind: 'semantic_stop' },
  ])
  let pending = true
  let consumeCalls = 0
  try {
    const result = await createHost(harness, null, undefined, async () => {
      consumeCalls += 1
      if (!pending) {
        return false
      }
      pending = false
      return true
    }).run()
    const reconstruction = await harness.bridge.reconstruct()
    const terminal = reconstruction.latest_executive_epoch
    const boundary = harness.boundaryFactory.boundaries[0]
    assert.ok(isRecordForTest(terminal))
    assert.ok(boundary)

    assert.equal(result.status, 'operator_stopped_after_single_epoch')
    assert.equal(pending, false)
    assert.equal(consumeCalls, 1)
    assert.deepEqual(
      {
        executiveEpochId: terminal.executive_epoch_id,
        state: terminal.state,
        rootThreadId: terminal.goal_thread_id,
      },
      {
        executiveEpochId: 'epoch.0',
        state: 'checkpointed',
        rootThreadId: 'thread.0',
      },
    )
    assert.deepEqual(boundary.monitorEvents, [
      { kind: 'goal_state', status: 'active', activeTurnId: 'turn.thread.0' },
      { kind: 'goal_state', status: 'blocked', activeTurnId: 'turn.thread.0' },
      { kind: 'same_turn_normal_activity', status: 'blocked', activeTurnId: 'turn.thread.0' },
    ])
    assert.equal(boundary.rootToolResultPayloads[0]?.status, 'ok')
    assert.deepEqual(boundary.checkpointTerminalHandoffs, ['owner_checkpoint'])
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.boundaryFactory.boundaries.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('single-epoch canary stops after the exact failed-before-checkpoint epoch without admitting a successor', async () => {
  const harness = await createHarness([
    { kind: 'blocked_without_checkpoint' },
    { kind: 'semantic_stop' },
  ])
  let consumeCalls = 0
  try {
    const result = await createHost(harness, null, undefined, async () => {
      consumeCalls += 1
      return true
    }).run()
    const reconstruction = await harness.bridge.reconstruct()
    const terminal = reconstruction.latest_executive_epoch
    assert.ok(isRecordForTest(terminal))
    assert.ok(isRecordForTest(terminal.reconciliation))

    assert.equal(result.status, 'operator_stopped_after_single_epoch')
    assert.equal(consumeCalls, 1)
    assert.deepEqual(
      {
        executiveEpochId: terminal.executive_epoch_id,
        state: terminal.state,
        rootThreadId: terminal.goal_thread_id,
        failureReason: terminal.reconciliation.failure_reason,
      },
      {
        executiveEpochId: 'epoch.0',
        state: 'failed_before_checkpoint',
        rootThreadId: 'thread.0',
        failureReason: 'boundary_failure',
      },
    )
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.boundaryFactory.boundaries.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('single-epoch canary never consumes a terminal predecessor and instead stops after one newly run epoch', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.seedCheckpoint('thread.predecessor', 'continue', 'epoch.predecessor')
  let consumeCalls = 0
  try {
    const result = await createHost(harness, null, undefined, async () => {
      consumeCalls += 1
      return true
    }).run()
    const reconstruction = await harness.bridge.reconstruct()
    const terminal = reconstruction.latest_executive_epoch
    assert.ok(isRecordForTest(terminal))

    assert.equal(result.status, 'operator_stopped_after_single_epoch')
    assert.equal(consumeCalls, 1)
    assert.equal(terminal.executive_epoch_id, 'epoch.0')
    assert.notEqual(terminal.executive_epoch_id, 'epoch.predecessor')
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
  } finally {
    await cleanup(harness)
  }
})

test('single-epoch canary may remain unarmed for one terminal and consume after a later single run', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  let consumeCalls = 0
  try {
    const result = await createHost(harness, null, undefined, async () => {
      consumeCalls += 1
      return consumeCalls === 2
    }).run()

    assert.equal(result.status, 'operator_stopped_after_single_epoch')
    assert.equal(consumeCalls, 2)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
    assert.equal(harness.allocatedWorkspaces.length, 2)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.equal(harness.boundaryFactory.boundaries.length, 2)
  } finally {
    await cleanup(harness)
  }
})

test('consumed single-epoch canary is one-shot and a later Host start follows ordinary unarmed behavior', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  let pending = true
  let consumeCalls = 0
  const consumeSingleEpochCanary = async (): Promise<boolean> => {
    consumeCalls += 1
    if (!pending) {
      return false
    }
    pending = false
    return true
  }
  try {
    const canaryResult = await createHost(
      harness,
      null,
      undefined,
      consumeSingleEpochCanary,
    ).run()

    assert.equal(canaryResult.status, 'operator_stopped_after_single_epoch')
    assert.equal(pending, false)
    assert.equal(consumeCalls, 1)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 1)

    const laterResult = await createHost(
      harness,
      null,
      undefined,
      consumeSingleEpochCanary,
    ).run()

    assert.equal(laterResult.status, 'stopped_semantically')
    assert.equal(consumeCalls, 2)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
    assert.equal(harness.allocatedWorkspaces.length, 2)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.equal(harness.boundaryFactory.boundaries.length, 2)
  } finally {
    await cleanup(harness)
  }
})

test('single-epoch canary rejects a stale predecessor terminal returned by an unmaterialized run', async () => {
  const harness = await createHarness([{ kind: 'pre_identity_start_failure' }])
  harness.bridge.seedCheckpoint('thread.predecessor', 'continue', 'epoch.predecessor')
  const predecessorReconstruction = await harness.bridge.reconstruct()
  const predecessorTerminal = predecessorReconstruction.latest_executive_epoch
  assert.ok(isRecordForTest(predecessorTerminal))
  harness.bridge.failAuthorizationResponseAfterCommit = true
  harness.bridge.onAuthorize = () => {
    harness.bridge.setMissionState('held', false)
    harness.bridge.nextLatestExecutiveEpochOverride = structuredClone(predecessorTerminal)
  }
  let consumeCalls = 0
  try {
    const result = await createHost(harness, null, undefined, async () => {
      consumeCalls += 1
      return true
    }).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(consumeCalls, 0)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.allocatedWorkspaces.length, 0)
    assert.equal(harness.boundaryFactory.boundaries.length, 0)
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('semantic closeout and checkpoint-stop retain result precedence while settling single-epoch canary', async () => {
  const semanticHarness = await createHarness([{
    kind: 'checkpoint',
    workerCount: 0,
    missionContinuation: 'closeout',
  }])
  let semanticCanaryCalls = 0
  try {
    const result = await createHost(semanticHarness, null, undefined, async () => {
      semanticCanaryCalls += 1
      return true
    }).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(semanticCanaryCalls, 1)
  } finally {
    await cleanup(semanticHarness)
  }

  const checkpointHarness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  let checkpointCanaryCalls = 0
  try {
    const result = await createHost(
      checkpointHarness,
      null,
      async () => true,
      async () => {
        checkpointCanaryCalls += 1
        return true
      },
    ).run()

    assert.equal(result.status, 'operator_stopped_after_checkpoint')
    assert.equal(checkpointCanaryCalls, 1)
  } finally {
    await cleanup(checkpointHarness)
  }
})

test('Executive orientation is independent of large private capture inventories', async () => {
  async function projectedOpening(captureCount: number): Promise<JsonObject> {
    const harness = await createHarness([{ kind: 'semantic_stop' }])
    harness.bridge.seedCheckpoint('thread.previous', 'pause', 'epoch.previous')
    harness.bridge.recoveryRawCaptures = Array.from({ length: captureCount }, (_, index) => ({
      capture_id: `capture.bulk.${index}`,
      capture_kind: 'output',
      executive_epoch_id: 'epoch.previous',
      observation_id: `output.thread.${index}`,
      assignment_id: `assignment.thread.${index}`,
      project_commit: 1,
      retrieval_handle: `capture:capture.bulk.${index}`,
      late_classification: 'at_or_before_terminal',
      artifacts: [{
        ordinal: 0,
        role: 'returned_output',
        logical_name: `output-${index}.txt`,
        blob_sha256: 'f'.repeat(64),
        retrieval_handle: `capture-artifact:capture.bulk.${index}#0`,
        pending_at_cut: true,
        pending_current: true,
      }],
    }))
    harness.bridge.setRecoveryPendingCaptureLocators(
      Array.from({ length: captureCount }, (_, index) => ({
        capture_id: `capture.bulk.${index}`,
        artifact_ordinal: 0,
      })),
    )
    try {
      await createHost(harness).run()
      const instructions = String(harness.boundaryFactory.requests[0]!.initialContextText)
      return instructionJson(instructions, 'executive_orientation')
    } finally {
      await cleanup(harness)
    }
  }

  const one = await projectedOpening(1)
  const many = await projectedOpening(200)
  for (const opening of [one, many]) {
    const serialized = JSON.stringify(opening)
    assert.doesNotMatch(serialized, /capture\.bulk\.|capture-artifact:|"raw_captures"|pending_capture_locators/)
    assert.doesNotMatch(serialized, new RegExp('f'.repeat(64)))
  }
  assert.deepEqual(many, one)
})

test('owner validation rejection keeps its exact location and targeted model correction', async () => {
  const harness = await createHarness([{ kind: 'corrective_error' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
      ['record_strategy', 'checkpoint'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('an initial Strategy closeout terminates after checkpoint without forcing another epoch', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.deepEqual(harness.bridge.bindings.map(({ rootThreadId }) => rootThreadId), ['thread.0'])
  } finally {
    await cleanup(harness)
  }
})

test('a rebound canonical admitted result opens one fresh closeout-only successor', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  harness.bridge.seedCheckpoint('thread.predecessor', 'continue', 'epoch.predecessor')
  harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.deepEqual(
      harness.bridge.bindings.map(({ rootThreadId }) => rootThreadId),
      ['thread.0'],
    )
    assert.notEqual(harness.bridge.bindings[0]?.rootThreadId, 'thread.predecessor')
    assert.equal(harness.boundaryFactory.requests.length, 1)
    const request = harness.boundaryFactory.requests[0]!
    assert.match(
      String(request.objective),
      /final Strategy closeout warranted by the exact canonical admitted result/i,
    )
    const instructions = String(request.developerInstructions)
    assert.match(instructions, /final Strategy closeout epoch/i)
    assert.match(instructions, /Admission created the canonical result/i)
    assert.match(instructions, /Strategy closeout and checkpoint are downstream coordination reactions, not proof validity, Admission evidence or canonical authority/i)
    assert.match(instructions, /Do not reopen the predecessor, Candidate A1, Admission Review or Decision/i)
    assert.match(instructions, /Do not commission research, historical advisory work, proof review or worker consensus/i)
    assert.doesNotMatch(instructions, /At every successor orientation, perform a lightweight historical-relevance evaluation/)
    assert.doesNotMatch(instructions, /all three materiality predicates/)
    assert.match(instructions, /discover targeted record_strategy usage/i)
    assert.match(instructions, /record the truthful final closeout/i)
    assert.match(instructions, /checkpoint with exactly empty input/i)
    assert.match(instructions, /No public disclosure or external effect is authorized/i)
    assert.doesNotMatch(instructions, /Begin undecided/i)
    assert.doesNotMatch(instructions, /branch researcher|ordinary research|active research direction/i)
    const injected = instructionJson(String(request.initialContextText), 'executive_orientation')
    assert.deepEqual(
      (injected.proof_attention as JsonObject).admitted_result,
      ADMITTED_RESULT,
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ request: semanticRequest }) =>
        isRecordForTest(semanticRequest) ? semanticRequest.operation : null,
      ),
      ['record_strategy', 'checkpoint'],
    )
    assert.deepEqual(
      (request.dynamicTools as Array<{ name: string }>).map(({ name }) => name),
      [
        'rh_mission',
        'rh_mission_page',
      ],
    )
    const closeoutMissionTool = (request.dynamicTools as Array<{
      name: string
      inputSchema: JsonObject
    }>).find(({ name }) => name === 'rh_mission')
    assert.ok(closeoutMissionTool)
    assert.deepEqual(
      ((closeoutMissionTool.inputSchema.properties as JsonObject).operation as JsonObject).enum,
      ['usage', 'record_strategy', 'checkpoint'],
    )
    assert.deepEqual(request.selectedCapabilities, {
      localRoots: [],
      mcpServers: [],
      apps: [],
      browser: null,
    })
  } finally {
    await cleanup(harness)
  }
})

test('the Host rejects a malformed canonical admitted-result projection before successor admission', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  harness.bridge.seedCheckpoint('thread.predecessor', 'continue', 'epoch.predecessor')
  harness.bridge.admittedResult = {
    ...structuredClone(ADMITTED_RESULT),
    closeout_authorized: true,
  }
  try {
    await assert.rejects(
      createHost(harness).run(),
      /canonical admitted result has the wrong exact shape/,
    )
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
  } finally {
    await cleanup(harness)
  }
})

test('formal execution is one explicit operational tool outside the nine semantic operations', async () => {
  const harness = await createHarness([{ kind: 'formal_attempt' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(harness.bridge.formalAttempts, [
      {
        request: {
          selected_bet_sha256: 'f'.repeat(64),
          correction_basis: null,
        },
        binding: {
          rootThreadId: 'thread.0',
          executiveEpochId: 'epoch.0',
        },
      },
    ])
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) =>
        isRecordForTest(request) ? request.operation : null,
      ),
      ['record_strategy', 'checkpoint'],
    )
    const tools = harness.boundaryFactory.requests[0]?.dynamicTools as Array<{
      name: string
      inputSchema: Record<string, unknown>
    }>
    assert.deepEqual(tools.map(({ name }) => name), [
      'rh_mission',
      'rh_mission_history_grant',
      'rh_mission_history',
      'rh_mission_research_read_grant',
      'rh_mission_research_read',
      'rh_mission_a1_review_grant',
      'rh_mission_a1_review',
      'rh_mission_admission_open',
      'rh_mission_admission_grant',
      'rh_mission_admission',
      'rh_formal_attempt',
      'rh_mission_page',
      'rh_mission_history_page',
      'rh_mission_research_read_page',
      'rh_mission_a1_review_page',
      'rh_mission_admission_page',
    ])
    const formalTool = tools.find(({ name }) => name === 'rh_formal_attempt')
    assert.ok(formalTool)
    assert.deepEqual(Object.keys(formalTool.inputSchema).sort(), [
      'additionalProperties',
      'properties',
      'required',
      'type',
    ])
    assert.match(
      resolvedInstructionsForTest(harness.boundaryFactory.requests[0]),
      /Formal attempts are operational execution, not another scientific workspace semantic operation/,
    )
    assert.match(
      resolvedInstructionsForTest(harness.boundaryFactory.requests[0]),
      /selected-bet hash from its owner-returned Strategy or orientation\/read result/,
    )
    assert.match(
      resolvedInstructionsForTest(harness.boundaryFactory.requests[0]),
      /never invent one/,
    )
  } finally {
    await cleanup(harness)
  }
})

test('failed formal execution does not block an explicit continue decision or its successor', async () => {
  const harness = await createHarness([
    { kind: 'formal_attempt_failed_continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.formalAttemptState = 'failed'
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.formalAttempts.length, 1)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) =>
        isRecordForTest(request) ? request.operation : null,
      ),
      ['record_strategy', 'checkpoint', 'record_strategy', 'checkpoint'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('real TypeScript Host completes Python formal reconciliation before checkpoint continuation', async () => {
  const harness = await createHarness([
    { kind: 'real_formal_lifecycle' },
    { kind: 'semantic_stop' },
  ])
  try {
    const pythonPath = resolvePythonExecutable()
    const modelCatalogRelative = path.join(
      'services',
      'rh-mission-host',
      'assets',
      'codex-model-catalog.0.153.4.json',
    )
    const modelCatalogPath = path.join(harness.config.repoRoot, modelCatalogRelative)
    await fs.promises.mkdir(path.dirname(modelCatalogPath), { recursive: true })
    await fs.promises.copyFile(path.join(SOURCE_REPO_ROOT, 'services/rh-mission-host/test-fixtures/codex-model-catalog.0.153.4.json'), modelCatalogPath)
    await fs.promises.writeFile(
      path.join(harness.config.repoRoot, '.mathematical-research-release.json'),
      JSON.stringify({
        schema_version: 'wc.rh_mission_host_release.v1',
        release_sha: harness.config.releaseSha,
        bundle_sha256: '7'.repeat(64),
        bundle_size: 4096,
        node_version: process.version,
        pnpm_version: '10.16.1',
        codex_version: 'codex-cli 0.153.4',
        service_package: '@workstation-control/rh-mission-host',
      }),
    )
    const missionScriptPath = path.join(harness.root, 'deterministic-rh-mission-bridge.py')
    writeDeterministicFormalMissionBridge(missionScriptPath)
    await fs.promises.rmdir(harness.config.missionWorkspaceRoot)
    initializeRealMissionWorkspace(
      pythonPath,
      harness.config.missionWorkspaceRoot,
      harness.config.projectId,
      harness.config.missionId,
      harness.config.releaseSha,
    )
    const config: MissionHostConfig = {
      ...harness.config,
      pythonPath,
      codexCliPath: process.execPath,
      missionScriptPath,
      modelCatalogPath,
    }
    const bridge = new PythonMissionBridge(config)
    const host = new MissionHost(
      config,
      () => bridge,
      harness.boundaryFactory,
      harness.store,
      async () => {},
      harness.allocateWorkspace,
    )

    const result = await host.run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    const formalBoundary = harness.boundaryFactory.boundaries[0]
    assert.ok(
      formalBoundary?.realFormalReconciliation,
      formalBoundary?.realFormalFailure ?? formalBoundary?.actFailure ?? undefined,
    )
    assert.equal(formalBoundary.actFailure, null, formalBoundary.actFailure ?? undefined)
    assert.deepEqual(formalBoundary.checkpointTerminalHandoffs, ['owner_checkpoint'])
    assert.deepEqual(formalBoundary.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[1]?.checkpointTerminalHandoffs, [
      'owner_checkpoint',
    ])
    assert.equal(
      fs.existsSync(path.join(config.runtimeDir, 'formal-attempt', 'attempt-journal.sqlite3')),
      true,
    )
    const reconstruction = await bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as JsonObject).state, 'checkpointed')
    const ownerDeltas = (reconstruction.recovery_opening as JsonObject).owner_deltas as JsonObject[]
    const strategyDelta = ownerDeltas.find(
      (item) => (item.current_reference as JsonObject).kind === 'strategy',
    )
    assert.equal((strategyDelta?.summary as JsonObject).mission_continuation, 'closeout')
    const sessionDeltas = ownerDeltas.filter(
      (item) => (item.current_reference as JsonObject).kind === 'session',
    )
    assert.equal(sessionDeltas.length, 1)
    const sessionDelta = sessionDeltas[0] as JsonObject
    assert.equal(sessionDelta.relation, 'unchanged')
    const sessionReference = sessionDelta.current_reference as JsonObject
    assert.equal(sessionReference.kind, 'session')
    assert.match(String(sessionReference.identity), /^session\.formal\./)
    const sessionSummary = sessionDelta.summary as JsonObject
    assert.equal(sessionSummary.lifecycle, 'terminal')
    const terminalBinding = sessionSummary.terminal_binding as JsonObject
    assert.equal(isRecordForTest(terminalBinding.attempt_result_ref), true)
    assert.equal(isRecordForTest(terminalBinding.raw_capture_ref), true)
    const evidenceDeltas = ownerDeltas.filter(
      (item) => (item.current_reference as JsonObject).kind === 'evidence',
    )
    assert.equal(evidenceDeltas.length, 1)
    assert.equal(
      (evidenceDeltas[0]?.current_reference as JsonObject).identity,
      'host-cross-language-formal',
    )
  } finally {
    if (process.platform === 'win32') {
      await makeFixtureTreeWritable(harness.root)
    }
    await cleanup(harness)
  }
})

test('real TypeScript Host rejects Python cut A before allocation and launches only refreshed cut B', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const pythonPath = resolvePythonExecutable()
  const concurrentWorkspaceRoot = path.join(harness.root, 'concurrent-goal')
  const strategyBMarker = 'Concurrent owner Strategy B is the only fresh launch ground.'
  let cutB: MissionAuthorizationCut | null = null
  let staleRejected = false
  try {
    await fs.promises.writeFile(
      path.join(harness.config.repoRoot, '.mathematical-research-release.json'),
      JSON.stringify({
        schema_version: 'wc.rh_mission_host_release.v1',
        release_sha: harness.config.releaseSha,
        bundle_sha256: '7'.repeat(64),
        bundle_size: 4096,
        node_version: process.version,
        pnpm_version: '10.16.1',
        codex_version: 'codex-cli 0.153.4',
        service_package: '@workstation-control/rh-mission-host',
      }),
    )
    await fs.promises.rmdir(harness.config.missionWorkspaceRoot)
    await fs.promises.mkdir(concurrentWorkspaceRoot)
    initializeRealMissionWorkspace(
      pythonPath,
      harness.config.missionWorkspaceRoot,
      harness.config.projectId,
      harness.config.missionId,
      harness.config.releaseSha,
    )
    const config: MissionHostConfig = {
      ...harness.config,
      pythonPath,
      codexCliPath: process.execPath,
      missionScriptPath: path.join(SOURCE_REPO_ROOT, 'scripts', 'rh_mission.py'),
    }
    const bridge = new PythonMissionBridge(config)
    const authorize = bridge.authorizeExecutiveEpoch.bind(bridge)
    let hostAuthorizationAttempts = 0
    bridge.authorizeExecutiveEpoch = async (expectedCut, signal) => {
      hostAuthorizationAttempts += 1
      if (hostAuthorizationAttempts === 1) {
        assert.equal(harness.store.state.activeGoal?.phase, 'planned')
        assert.deepEqual(harness.allocatedWorkspaces, [])
        assert.deepEqual(harness.boundaryFactory.boundaries, [])
        assert.deepEqual(harness.boundaryFactory.requests, [])

        const concurrent = await authorize(expectedCut)
        const concurrentEpochId = String(concurrent.executive_epoch_id)
        const concurrentRootThreadId = 'thread.concurrent-cut-b'
        await bridge.bindExecutiveEpoch({
          executiveEpochId: concurrentEpochId,
          rootThreadId: concurrentRootThreadId,
          workspaceRoot: concurrentWorkspaceRoot,
        })
        const binding = {
          executiveEpochId: concurrentEpochId,
          rootThreadId: concurrentRootThreadId,
        }
        const strategy = await bridge.executeSemanticOperation({
          schema_version: 'mathematical_research.mission_semantic_request.v1',
          operation: 'record_strategy',
          input: {
            mission_continuation: 'continue',
            integrated_comparison: strategyBMarker,
            reconsideration_conditions: [
              { condition: 'Reconsider only if the exact current owner facts change.' },
            ],
          },
        }, binding)
        assert.equal(strategy.status, 'completed')
        const checkpoint = await bridge.executeSemanticOperation({
          schema_version: 'mathematical_research.mission_semantic_request.v1',
          operation: 'checkpoint',
          input: {},
        }, binding)
        assert.equal(checkpoint.status, 'completed')
        const refreshed = await bridge.hostSnapshot()
        cutB = refreshed.authorization_cut as MissionAuthorizationCut

        try {
          await authorize(expectedCut, signal)
          assert.fail('cut A authorization unexpectedly succeeded after Strategy B')
        } catch (error) {
          assert.ok(error instanceof MissionBridgeError)
          assert.deepEqual(
            error.ownerErrors.map((item) => (item as Record<string, unknown>).code),
            ['mission_host_store_cut_stale'],
          )
          const afterRejected = await bridge.hostSnapshot()
          const latest = (afterRejected.current_state as JsonObject)
            .latest_executive_epoch as JsonObject
          assert.equal(latest.executive_epoch_id, concurrentEpochId)
          assert.equal(latest.state, 'checkpointed')
          assert.deepEqual(harness.allocatedWorkspaces, [])
          assert.deepEqual(harness.boundaryFactory.boundaries, [])
          assert.deepEqual(harness.boundaryFactory.requests, [])
          staleRejected = true
          throw error
        }
      }
      assert.equal(staleRejected, true)
      assert.deepEqual(expectedCut, cutB)
      return authorize(expectedCut, signal)
    }
    harness.boundaryFactory.onCreate = () => {
      assert.equal(staleRejected, true)
    }
    const allocateWorkspace = async (prospectivePath?: string): Promise<string> => {
      assert.equal(staleRejected, true)
      return harness.allocateWorkspace(prospectivePath)
    }
    const host = new MissionHost(
      config,
      () => bridge,
      harness.boundaryFactory,
      harness.store,
      async () => {},
      allocateWorkspace,
    )

    const result = await host.run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(hostAuthorizationAttempts, 2)
    assert.equal(harness.allocatedWorkspaces.length, 1)
    assert.equal(harness.boundaryFactory.boundaries.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    const orientation = instructionJson(
      String(harness.boundaryFactory.requests[0]?.initialContextText),
      'executive_orientation',
    )
    assert.equal(
      (orientation.current_strategy as JsonObject).integrated_comparison,
      strategyBMarker,
    )
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    if (process.platform === 'win32') {
      await makeFixtureTreeWritable(harness.root)
    }
    await cleanup(harness)
  }
})

test('Mission-selected capability families reach the Goal boundary exactly', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    const selectedRoot = path.join(harness.config.repoRoot, 'capabilities', 'research')
    await fs.promises.mkdir(selectedRoot, { recursive: true })
    harness.bridge.missionExecutionPolicy.selected_capabilities = {
      local_roots: [
        {
          kind: 'plugin',
          id: 'rh research',
          release_relative_path: 'capabilities/research',
        },
      ],
      mcp_servers: [
        { server_id: 'source server', enabled_tools: ['source/read', 'source check'] },
      ],
      apps: [
        { app_id: 'source app', enabled_tools: ['read/item'] },
      ],
      browser: 'isolated_ephemeral_unauthenticated',
    }

    await createHost(harness).run()

    assert.deepEqual(harness.boundaryFactory.requests[0]?.selectedCapabilities, {
      localRoots: [
        {
          kind: 'plugin',
          id: 'rh research',
          location: {
            type: 'environment',
            environmentId: 'local',
            path: selectedRoot,
          },
        },
      ],
      mcpServers: [
        { id: 'source server', enabledTools: ['source/read', 'source check'] },
      ],
      apps: [
        { id: 'source app', enabledTools: ['read/item'] },
      ],
      browser: { mode: 'isolated_ephemeral_unauthenticated' },
    })
  } finally {
    await cleanup(harness)
  }
})

test('Mission purpose or execution-policy drift fails before epoch authorization', async () => {
  const purposeHarness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    delete purposeHarness.bridge.missionPurpose.proof_standard
    await assert.rejects(
      createHost(purposeHarness).run(),
      /Executive Mission purpose has the wrong closed shape/,
    )
    assert.deepEqual(purposeHarness.bridge.authorizations, [])
    assert.deepEqual(purposeHarness.boundaryFactory.requests, [])
  } finally {
    await cleanup(purposeHarness)
  }

  const policyHarness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    policyHarness.bridge.missionExecutionPolicy.model = 'fallback-model'
    await assert.rejects(
      createHost(policyHarness).run(),
      /execution policy is incompatible with the fixed Host boundary/,
    )
    assert.deepEqual(policyHarness.bridge.authorizations, [])
    assert.deepEqual(policyHarness.boundaryFactory.requests, [])
  } finally {
    await cleanup(policyHarness)
  }
})

test('Mission selections are closed and roots cannot escape the immutable release', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    harness.bridge.missionExecutionPolicy.selected_capabilities = {
      local_roots: [
        {
          kind: 'skill',
          id: 'ambient',
          release_relative_path: '../ambient-skills',
        },
      ],
      mcp_servers: [],
      apps: [],
      browser: null,
    }
    await assert.rejects(
      createHost(harness).run(),
      /canonical release-relative path|escapes the immutable release/,
    )
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
  } finally {
    await cleanup(harness)
  }

  const authHarness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    authHarness.bridge.missionExecutionPolicy.selected_capabilities = {
      local_roots: [],
      mcp_servers: [
        {
          server_id: 'trusted',
          enabled_tools: ['read'],
          oauth_token: 'must-not-enter-Mission-state',
        },
      ],
      apps: [],
      browser: null,
    }
    await assert.rejects(
      createHost(authHarness).run(),
      /wrong closed shape/,
    )
    assert.deepEqual(authHarness.bridge.authorizations, [])
    assert.deepEqual(authHarness.boundaryFactory.requests, [])
  } finally {
    await cleanup(authHarness)
  }
})

test('historical Strategy pause is reopened and cannot stop automatic successors', async () => {
  const harness = await createHarness([
    {
      kind: 'checkpoint',
      workerCount: 0,
      missionContinuation: 'continue',
    },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.setContinuation('pause')
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizations.length, 2)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.deepEqual(
      harness.boundaryFactory.boundaries[0]?.checkpointTerminalHandoffs,
      ['owner_checkpoint'],
    )
    assert.deepEqual(harness.bridge.failures, [])
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.equal(
      instructionJson(String(harness.boundaryFactory.requests[0]?.initialContextText), 'host_continuity').launch_mode,
      'reopen_historical_pause',
    )
  } finally {
    await cleanup(harness)
  }
})

test('lagged checkpoint readback preserves the validated terminal for restart instead of recording failure', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'recovery' },
    { kind: 'recovery' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.staleCheckpointReadbacksRemaining = 3
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/i)
        assert.match(error.message, /containment did not complete/i)
        assert.match(String(error.cause), /owner reconstruction did not expose the validated checkpoint terminal/)
        return true
      },
    )

    assert.notEqual(harness.store.state.activeGoal, null)
    assert.equal(harness.store.state.activeGoal?.phase, 'checkpointed')
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])

    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/i)
        assert.match(error.message, /containment did not complete/i)
        assert.match(String(error.cause), /owner reconstruction did not expose the validated checkpoint terminal/)
        return true
      },
    )
    assert.equal(harness.store.state.activeGoal?.phase, 'checkpointed')
    assert.deepEqual(harness.bridge.failures, [])

    const restarted = await createHost(harness).run()
    assert.equal(restarted.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[2]?.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    assert.deepEqual(harness.boundaryFactory.boundaries[3]?.stopCalls, [
      { threadId: 'thread.3', reason: 'owner_checkpoint' },
    ])
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('conflicting failed readback cannot replace a validated checkpoint terminal', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'recovery' },
    { kind: 'recovery' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.failedCheckpointReadbacksRemaining = 3
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/i)
        assert.match(error.message, /containment did not complete/i)
        assert.match(String(error.cause), /owner reconstruction did not expose the validated checkpoint terminal/)
        return true
      },
    )

    assert.notEqual(harness.store.state.activeGoal, null)
    assert.equal(harness.store.state.activeGoal?.phase, 'checkpointed')
    assert.deepEqual(harness.bridge.failures, [])

    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof GoalEpochMissionConsistencyError)
        assert.match(error.message, /checkpoint committed/i)
        assert.match(error.message, /containment did not complete/i)
        assert.match(String(error.cause), /owner reconstruction did not expose the validated checkpoint terminal/)
        return true
      },
    )
    assert.equal(harness.store.state.activeGoal?.phase, 'checkpointed')
    assert.deepEqual(harness.bridge.failures, [])

    const restarted = await createHost(harness).run()
    assert.equal(restarted.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    assert.deepEqual(harness.boundaryFactory.boundaries[3]?.stopCalls, [
      { threadId: 'thread.3', reason: 'owner_checkpoint' },
    ])
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('explicit Host start replaces a historical pause and continues directly into a successor', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.seedCheckpoint('thread.legacy-pause', 'pause', 'epoch.legacy-pause')
  try {
    const reconsidered = await createHost(harness).run()

    assert.equal(reconsidered.status, 'stopped_semantically')
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    assert.deepEqual(
      harness.boundaryFactory.boundaries.map(({ stopCalls }) => stopCalls),
      [
        [{ threadId: 'thread.0', reason: 'owner_checkpoint' }],
        [{ threadId: 'thread.1', reason: 'owner_checkpoint' }],
      ],
    )
    assert.equal(
      instructionJson(String(harness.boundaryFactory.requests[0]!.initialContextText), 'host_continuity').launch_mode,
      'reopen_historical_pause',
    )
    for (const request of harness.boundaryFactory.requests) {
      assertHistoricalAdvisoryMaterialityContract(resolvedInstructionsForTest(request))
    }
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) =>
        (request as Record<string, unknown>).operation),
      [
        'interpret_material',
        'record_strategy',
        'checkpoint',
        'record_strategy',
        'checkpoint',
      ],
    )
    assert.deepEqual(Object.keys(harness.store.state).sort(), [
      'activeGoal',
      'captureRecoveryRequired',
      'missionId',
      'schemaVersion',
      'updatedAt',
    ])
  } finally {
    await cleanup(harness)
  }
})

test('explicit Host start reopens one reconsideration epoch after a failed terminal under Strategy pause', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  harness.bridge.seedFailure(null, 'boundary_failure', 'epoch.owner-terminal')
  harness.bridge.setContinuation('pause')
  harness.bridge.recoveryRawCaptures = [
    {
      capture_id: 'capture.recovered.output.1',
      capture_kind: 'output',
      executive_epoch_id: 'epoch.owner-terminal',
      observation_id: 'output.thread.child',
      assignment_id: 'assignment.thread.child',
      project_commit: 1,
      retrieval_handle: 'capture:capture.recovered.output.1',
      late_classification: 'at_or_before_terminal',
      artifacts: [],
    },
  ]
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(
      instructionJson(String(harness.boundaryFactory.requests[0]!.initialContextText), 'host_continuity').launch_mode,
      'reopen_historical_pause',
    )
    const instructions = resolvedInstructionsForTest(harness.boundaryFactory.requests[0]!)
    assertHistoricalAdvisoryMaterialityContract(instructions)
    assert.doesNotMatch(instructions, /Recovery-first task|Before commissioning mathematically new work/)
    assert.match(instructions, /Exact source and history retrieval remains available when the mathematics needs it/)
    assert.match(instructions, /retain valid results, and continue independent work/)
    assert.match(instructions, /Diagnose the affected uncertainty/)
    assert.doesNotMatch(instructions, /checkpoint, and pause/)
    assert.doesNotMatch(instructions, /one relevant public-source lookup|one useful deterministic shell computation/)
    assert.equal(instructionJson(String(harness.boundaryFactory.requests[0]!.initialContextText), 'host_continuity').operational_effect_on_mathematics, 'none')
    assert.match(instructions, /Ordinary researchers do not choose operator start, stop, suspend, resume, provider lifecycle, or closeout modes/)
  } finally {
    await cleanup(harness)
  }
})

test('explicit Host start reopens an eligible historical unsolved Strategy closeout through ordinary continue', async () => {
  const harness = await createHarness([{
    kind: 'checkpoint',
    workerCount: 0,
    missionContinuation: 'continue',
  }])
  harness.bridge.seedCheckpoint(
    'thread.historical-unsolved-closeout',
    'closeout',
    'epoch.historical-unsolved-closeout',
  )
  let checkpointStopChecks = 0
  try {
    const result = await createHost(
      harness,
      null,
      async () => {
        checkpointStopChecks += 1
        return checkpointStopChecks > 1
      },
    ).run()

    assert.equal(result.status, 'operator_stopped_after_checkpoint')
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    const request = harness.boundaryFactory.requests[0]!
    const instructions = resolvedInstructionsForTest(request)
    assert.equal(instructionJson(String(request.initialContextText), 'host_continuity').launch_mode, 'reopen_historical_closeout')
    assert.match(instructions, /record the required truthful `continue` before checkpoint/)
    assert.match(instructions, /Ordinary research may write only `continue`/)
    assert.deepEqual(
      harness.bridge.semantics.map(({ request: semanticRequest }) =>
        (semanticRequest as JsonObject).operation),
      ['interpret_material', 'record_strategy', 'checkpoint'],
    )
    const strategy = harness.bridge.semantics[1]?.request as JsonObject
    assert.equal((strategy.input as JsonObject).mission_continuation, 'continue')
    assert.equal(harness.bridge.admittedResult, null)
  } finally {
    await cleanup(harness)
  }
})

test('ordinary unsolved Strategy closeout is an invariant failure rather than Mission success', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  harness.bridge.admitCanonicalResultOnCloseout = false
  try {
    await assert.rejects(
      createHost(harness).run(),
      /ordinary Executive Epoch produced an unsolved Strategy closeout without a canonical admitted result/,
    )
    assert.equal(harness.bridge.admittedResult, null)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
  } finally {
    await cleanup(harness)
  }
})

test('lost Host state terminalizes an owner-authorized epoch without inventing a Goal', async () => {
  const harness = await createHarness([])
  await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  harness.bridge.setContinuation('closeout')
  harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.deepEqual(harness.bridge.failures, [
      {
        executiveEpochId: 'epoch.0',
        reconciliation: {
          stage: 'authorization_only',
          failure_reason: 'host_state_missing',
        },
      },
    ])
  } finally {
    await cleanup(harness)
  }
})

test('restart recovers a materialized root from its unique Goal workspace before terminalizing it', async () => {
  const harness = await createHarness([
    { kind: 'recovery', discoverMaterializedStart: true },
  ])
  const workspaceRoot = await harness.allocateWorkspace()
  await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  harness.store.state.activeGoal = {
    phase: 'authorized',
    threadId: null,
    objective: MISSION_OBJECTIVE,
    workspaceRoot,
    executiveEpochId: 'epoch.0',
    failureReason: null,
  }
  harness.bridge.setContinuation('closeout')
  harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(harness.bridge.bindings, [
      {
        executiveEpochId: 'epoch.0',
        rootThreadId: 'thread.0',
        workspaceRoot,
      },
    ])
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation),
      [{ stage: 'goal_runtime', failure_reason: 'dead_runner_recovery' }],
    )
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('lost Host workspace refuses to launch over an already-bound owner epoch', async () => {
  const harness = await createHarness([])
  const workspaceRoot = await harness.allocateWorkspace()
  await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  await harness.bridge.bindExecutiveEpoch({
    executiveEpochId: 'epoch.0',
    rootThreadId: 'thread.owner-bound',
    workspaceRoot,
  })
  try {
    await assert.rejects(
      createHost(harness).run(),
      /live epoch absent from Host pending state/,
    )

    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('inactive fenced Mission stops before Host plans or authorizes another epoch', async () => {
  const harness = await createHarness([])
  harness.bridge.setMissionState('held', false, 'revoked')
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.bridge.authorizations.length, 0)
    assert.equal(harness.boundaryFactory.requests.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('inactive Mission still terminalizes an authorization orphan before stopping', async () => {
  const harness = await createHarness([])
  await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  harness.bridge.setMissionState('held', false, 'revoked')
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation),
      [{ stage: 'authorization_only', failure_reason: 'host_state_missing' }],
    )
    assert.equal(harness.boundaryFactory.requests.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('Host accepts the exact owner reconstruction without proof-neutral effect constants', async () => {
  const harness = await createHarness([])
  harness.bridge.seedCheckpoint('thread.effect-neutral', 'closeout')
  harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizations.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('late native material uses the retained historical direct epoch binding', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  try {
    const result = await host.run()
    assert.equal(result.status, 'stopped_semantically')

    await host.onNativeMaterialObserved(
      {
        observationId: 'late.output.thread.0',
        materialKind: 'output',
        content: 'Late but authentic mathematical material.',
        rootThreadId: 'thread.0',
        parentThreadId: 'thread.0',
        childThreadId: 'thread.0.child.late',
      } as any,
      rootGoalOperationContext('thread.0'),
    )

    assert.deepEqual(harness.bridge.nativeBindings.at(-1), {
      rootThreadId: 'thread.0',
      executiveEpochId: 'epoch.0',
    })
  } finally {
    await cleanup(harness)
  }
})

test('Candidate A1 review stays exact-child proof-neutral ephemeral and bridge-scoped', async () => {
  const harness = await createHarness([{ kind: 'candidate_a1_review' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(harness.bridge.candidateA1ReviewGrantRequests, [{
      request: {
        child_thread_id: 'thread.0.child.a1-review',
        assignment: 'Independently review the exact frozen complete-target claim.',
        context: { id: 'context:a1-review', revision: 1 },
        candidate_ref: {
          id: 'candidate:complete-rh',
          revision: 2,
          payload_sha256: 'a'.repeat(64),
        },
      },
      binding: {
        rootThreadId: 'thread.0',
        executiveEpochId: 'epoch.0',
        childThreadId: 'thread.0.child.a1-review',
        parentThreadId: 'thread.0',
        depth: 1,
      },
    }])
    const boundary = harness.boundaryFactory.boundaries[0]
    assert.deepEqual(boundary?.installedHistoricalGrants[0], {
      rootThreadId: 'thread.0',
      childThreadId: 'thread.0.child.a1-review',
      parentThreadId: 'thread.0',
      depth: 1,
      status: 'active',
      activeTurnId: 'turn.thread.0.child.a1-review',
      grantId: 'c'.repeat(64),
      assignmentId: `candidate-a1-review-assignment:${'b'.repeat(64)}`,
      allowedToolNames: ['rh_mission_a1_review', 'rh_mission_a1_review_page'],
    })
    assert.deepEqual(
      harness.bridge.candidateA1Reviews.map(({ request }) => request),
      [
        { mode: 'usage' },
        { mode: 'retrieve' },
        {
          mode: 'submit',
          disposition: 'admission_ready',
          review_finding: 'Independent reconstruction leaves no material mathematical objection.',
          no_remaining_material_objection: true,
          cited_basis: [{
            id: 'evidence:review-basis',
            revision: 3,
            payload_sha256: 'e'.repeat(64),
          }],
        },
      ],
    )
    assert.equal(
      harness.bridge.candidateA1Reviews.every(({ grant, binding }) =>
        grant.grant_id === 'c'.repeat(64) &&
        grant.grant_digest_sha256 === 'c'.repeat(64) &&
        binding.rootThreadId === 'thread.0' &&
        binding.executiveEpochId === 'epoch.0' &&
        binding.callerThreadId === 'thread.0.child.a1-review' &&
        binding.parentThreadId === 'thread.0' &&
        binding.depth === 1 &&
        binding.turnId === 'turn.thread.0.child.a1-review'),
      true,
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
      ['record_strategy', 'checkpoint'],
    )
    assert.deepEqual(Object.keys(harness.store.state).sort(), [
      'activeGoal',
      'captureRecoveryRequired',
      'missionId',
      'schemaVersion',
      'updatedAt',
    ])
    assert.doesNotMatch(
      JSON.stringify(harness.store.saves),
      /candidate_a1_review_grant|grant_id|assignment_id/,
    )
  } finally {
    await cleanup(harness)
  }
})

test('Admission reaches one persisted Decision through exact disjoint child grants without canonical effect', async () => {
  const harness = await createHarness([{ kind: 'complete_claim_admission' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(
      harness.boundaryFactory.boundaries[0]?.actFailure,
      null,
      harness.boundaryFactory.boundaries[0]?.actFailure ?? undefined,
    )
    assert.deepEqual(harness.bridge.admissionCaseRequests, [{
      request: {
        candidate_ref: {
          id: 'candidate:complete-rh',
          revision: 2,
          payload_sha256: 'a'.repeat(64),
        },
      },
      binding: { rootThreadId: 'thread.0', executiveEpochId: 'epoch.0' },
    }])
    assert.equal(harness.bridge.admissionReviewGrantRequests.length, 1)
    assert.equal(harness.bridge.admissionDecisionGrantRequests.length, 1)
    assert.equal(
      harness.bridge.admissionReviewGrantRequests[0]?.binding.childThreadId,
      'thread.0.child.admission-reviewer',
    )
    assert.equal(
      harness.bridge.admissionDecisionGrantRequests[0]?.binding.childThreadId,
      'thread.0.child.admission-admitter',
    )
    assert.notEqual(
      harness.bridge.admissionReviewGrantRequests[0]?.binding.childThreadId,
      harness.bridge.admissionDecisionGrantRequests[0]?.binding.childThreadId,
    )
    assert.deepEqual(
      harness.bridge.admissionReviews.map(({ request }) => request),
      [
        { mode: 'usage' },
        { mode: 'retrieve' },
        {
          mode: 'submit',
          disposition: 'no_material_objection',
          review_finding: 'Independent reconstruction found no remaining material objection.',
          cited_basis: [{
            kind: 'candidate',
            identity: 'complete-rh',
            revision: 2,
            payload_sha256: 'a'.repeat(64),
          }],
        },
      ],
    )
    assert.deepEqual(
      harness.bridge.admissionDecisions.map(({ request }) => request),
      [{
        mode: 'submit',
        disposition: 'authorize_exact_delta',
        decision_basis: 'The frozen exact claim and role-disjoint review qualify.',
        limitations: ['Canonical mutation remains an external exact writer action.'],
      }],
    )
    assert.equal(
      harness.bridge.admissionDecisions[0]?.grant.role,
      'independent_complete_claim_admitter',
    )
    assert.equal(
      harness.bridge.admissionDecisions[0]?.binding.callerThreadId,
      'thread.0.child.admission-admitter',
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
      ['record_strategy', 'checkpoint'],
    )
    assert.doesNotMatch(
      JSON.stringify(harness.store.saves),
      /complete_claim_admission_grant|grant_id|assignment_id/,
    )
  } finally {
    await cleanup(harness)
  }
})

test('Admission routes an admitter-discovered concrete rejection after a no-objection review', async () => {
  const harness = await createHarness([{
    kind: 'complete_claim_admission',
    decisionDisposition: 'reject',
  }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(
      harness.boundaryFactory.boundaries[0]?.actFailure,
      null,
      harness.boundaryFactory.boundaries[0]?.actFailure ?? undefined,
    )
    assert.deepEqual(
      harness.bridge.admissionDecisions.map(({ request }) => request),
      [{
        mode: 'submit',
        disposition: 'reject',
        decision_basis: 'The admitter found a fatal gap in the frozen implication.',
        objections: [{
          exact_objection: 'The final implication uses its converse without proof.',
          affected_scope: 'The frozen claim final implication.',
          materiality_basis: 'Without the missing direction, the argument does not establish RH.',
        }],
        cited_basis: [{
          kind: 'candidate',
          identity: 'complete-rh',
          revision: 2,
          payload_sha256: 'a'.repeat(64),
        }],
      }],
    )
    assert.equal(
      harness.bridge.admissionDecisions[0]?.grant.role,
      'independent_complete_claim_admitter',
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
      ['record_strategy', 'checkpoint'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('binding-mismatched Candidate A1 review grant never reaches Core or reviewer dispatch', async () => {
  const harness = await createHarness([{ kind: 'candidate_a1_review_grant_mismatch' }])
  harness.bridge.candidateA1ReviewGrantEnvelopeMutator = (envelope) => ({
    ...envelope,
    executive_epoch_id: 'epoch.other',
  })
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.candidateA1ReviewGrantRequests.length, 1)
    assert.equal(harness.boundaryFactory.boundaries[0]?.installedHistoricalGrants.length, 0)
    assert.equal(harness.bridge.candidateA1Reviews.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('ordinary research grant binds read calls and pages to one child and refreshes cursor custody after revocation', async () => {
  const harness = await createHarness([{ kind: 'research_read' }])
  harness.bridge.largeReadContent = 'ordinary research source:' + 'x'.repeat(5 * 1024 * 1024)
  const host = createHost(harness)
  harness.boundaryFactory.mutateDelegatedPageCustody = (handle) => {
    const pages = (host as unknown as { pagedToolResults: Map<string, { executiveEpochId: string }> }).pagedToolResults
    const page = pages.get(handle)
    assert.ok(page)
    const epoch = page.executiveEpochId
    page.executiveEpochId = 'epoch.unrelated'
    return () => { page.executiveEpochId = epoch }
  }
  try {
    const result = await host.run()
    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.researchGrantRequests.length, 3)
    const expectedGrantRequest = {
      request: {
        child_thread_id: 'thread.0.child.research',
        assignment: 'Investigate the exact scientific source and report qualified mathematics.',
        source_families: ['evidence', 'missions', 'sessions'],
        raw_body_policy: 'metadata_only',
      },
      binding: {
        rootThreadId: 'thread.0', executiveEpochId: 'epoch.0',
        childThreadId: 'thread.0.child.research', parentThreadId: 'thread.0', depth: 1,
      },
    }
    assert.deepEqual(harness.bridge.researchGrantRequests, [expectedGrantRequest, expectedGrantRequest, expectedGrantRequest])
    const grants = harness.boundaryFactory.boundaries[0]!.installedHistoricalGrants
    assert.equal(grants.length, 3)
    assert.deepEqual(grants[0], {
      rootThreadId: 'thread.0', childThreadId: 'thread.0.child.research', parentThreadId: 'thread.0',
      depth: 1, status: 'active', activeTurnId: 'turn.thread.0.child.research',
      grantId: '7'.repeat(64), assignmentId: `research-assignment:${'8'.repeat(64)}`,
      allowedToolNames: ['rh_mission_research_read', 'rh_mission_research_read_page'],
    })
    assert.deepEqual(grants[1], grants[0])
    assert.deepEqual(grants[2], grants[0])
    assert.deepEqual(harness.bridge.researchReads.map(({ request }) => request), [
      { mode: 'usage', topics: ['reads', 'authority'] },
      { mode: 'retrieve', selection: {
        mode: 'read', purpose: 'Recover exact assigned scientific source.', ids: ['evidence:research-input@1'],
      } },
      { mode: 'usage' },
    ])
    for (const read of harness.bridge.researchReads) {
      assert.deepEqual(read.binding, {
        rootThreadId: 'thread.0', executiveEpochId: 'epoch.0', callerThreadId: 'thread.0.child.research',
        parentThreadId: 'thread.0', depth: 1, turnId: 'turn.thread.0.child.research',
        grantId: '7'.repeat(64), assignmentId: `research-assignment:${'8'.repeat(64)}`,
      })
      assert.deepEqual(Object.keys(read.grant).sort(), [
        'schema_version', 'grant_id', 'project_id', 'mission_id', 'executive_epoch_id', 'root_thread_id',
        'child_thread_id', 'parent_thread_id', 'direct_depth', 'assignment_id', 'assignment',
        'allowed_modes', 'source_families', 'raw_body_policy',
      ].sort())
      assert.match(read.researchQueryContext?.cursor_mac_key ?? '', /^[a-f0-9]{64}$/)
    }
    const keys = harness.bridge.researchReads.map(({ researchQueryContext }) => researchQueryContext!.cursor_mac_key)
    assert.equal(keys[0], keys[1])
    assert.notEqual(keys[1], keys[2])
    assert.equal(harness.bridge.historicalGrantRequests.length, 0)
    assert.equal(harness.bridge.delegatedReads.length, 0)
    assert.deepEqual(harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
      ['record_strategy', 'checkpoint'])
    assert.deepEqual(harness.bridge.fences, [])
    assert.doesNotMatch(JSON.stringify(harness.store.saves), /research_read_grant|grant_id|assignment_id|cursor_mac_key/)
    assert.equal(fs.existsSync(path.join(harness.config.runtimeDir, '.rh-mission-owner-results')), false)
  } finally {
    await cleanup(harness)
  }
})

test('rejected native mixed-role installs leave only the two original Host grant families usable', async () => {
  const harness = await createHarness([{ kind: 'research_read_mixed_role' }])
  harness.bridge.historicalGrantEnvelopeMutator = (envelope) => ({
    ...envelope,
    grant_id: String(envelope.child_thread_id).endsWith('.research') ? 'a'.repeat(64) : 'f'.repeat(64),
  })
  harness.bridge.researchGrantEnvelopeMutator = (envelope) => ({
    ...envelope,
    grant_id: String(envelope.child_thread_id).endsWith('.history') ? '9'.repeat(64) : '7'.repeat(64),
  })
  const host = createHost(harness)
  try {
    const result = await host.run()
    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.researchGrantRequests.length, 2)
    assert.equal(harness.bridge.historicalGrantRequests.length, 2)
    assert.equal(harness.bridge.researchReads.length, 1)
    assert.equal(harness.bridge.researchReads[0]?.binding.callerThreadId, 'thread.0.child.research')
    assert.equal(harness.boundaryFactory.boundaries[0]?.installedHistoricalGrants.length, 2)
    assert.deepEqual(harness.bridge.fences, [])
    const active = host as unknown as { researchReadGrants: Map<string, unknown>; historicalReadGrants: Map<string, unknown> }
    assert.equal(active.researchReadGrants.size, 0)
    assert.equal(active.historicalReadGrants.size, 0)
  } finally {
    await cleanup(harness)
  }
})

for (const [label, mutation] of [
  ['wrong child', { child_thread_id: 'thread.0.child.other' }],
  ['wrong Mission', { mission_id: 'mission.other' }],
  ['writer mode', { allowed_modes: ['usage', 'retrieve', 'record_strategy'] }],
  ['fixed cut from a different grant family', { project_commit_cut: 17 }],
] as Array<[string, JsonObject]>) {
  test(`ordinary research grant rejects ${label} before installing descendant authority`, async () => {
    const harness = await createHarness([{ kind: 'research_read', rejectGrant: true }])
    harness.bridge.researchGrantEnvelopeMutator = (envelope) => ({ ...envelope, ...mutation })
    try {
      assert.equal((await createHost(harness).run()).status, 'stopped_semantically')
      assert.equal(harness.bridge.researchGrantRequests.length, 1)
      assert.equal(harness.boundaryFactory.boundaries[0]?.installedHistoricalGrants.length, 0)
      assert.equal(harness.bridge.researchReads.length, 0)
      assert.deepEqual(harness.bridge.fences, [])
    } finally {
      await cleanup(harness)
    }
  })
}

for (const [label, mutation] of [
  ['wrong grant', { grant_id: '6'.repeat(64) }],
  ['wrong assignment', { assignment_id: 'research-assignment:other' }],
  ['wrong mode', { mode: 'retrieve' }],
  ['invalid read cut', { project_commit_cut: -1 }],
  ['foreign status field', { status: 'completed' }],
] as Array<[string, JsonObject]>) {
  test(`ordinary research result rejects ${label} without fencing the executive`, async () => {
    const harness = await createHarness([{ kind: 'research_read', rejectResult: true }])
    harness.bridge.researchReadResultMutator = (result) => ({ ...result, ...mutation })
    try {
      assert.equal((await createHost(harness).run()).status, 'stopped_semantically')
      assert.equal(harness.bridge.researchReads.length, 1)
      assert.deepEqual(harness.bridge.fences, [])
      assert.deepEqual(harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
        ['record_strategy', 'checkpoint'])
    } finally {
      await cleanup(harness)
    }
  })
}

test('historical advisory grant stays exact-child read-only ephemeral and bridge-scoped', async () => {
  const harness = await createHarness([{ kind: 'historical_advisory' }])
  harness.bridge.largeReadContent = 'delegated historical result:' + 'x'.repeat(5 * 1024 * 1024)
  const host = createHost(harness)
  harness.boundaryFactory.mutateDelegatedPageCustody = (handle) => {
    const pages = (host as unknown as {
      pagedToolResults: Map<string, { executiveEpochId: string }>
    }).pagedToolResults
    const result = pages.get(handle)
    assert.ok(result)
    const retainedEpochId = result.executiveEpochId
    result.executiveEpochId = 'epoch.other'
    return () => {
      result.executiveEpochId = retainedEpochId
    }
  }
  try {
    const result = await host.run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.historicalGrantRequests.length, 1)
    assert.deepEqual(harness.bridge.historicalGrantRequests[0], {
      request: {
        child_thread_id: 'thread.0.child.history',
        assignment_mode: 'historical_opportunity_scout',
        assignment: 'Search retained history for one overlooked composable route.',
        context: { id: 'context:history-scout', revision: 1 },
        source_families: ['branches', 'evidence'],
        raw_body_policy: 'metadata_only',
      },
      binding: {
        rootThreadId: 'thread.0',
        executiveEpochId: 'epoch.0',
        childThreadId: 'thread.0.child.history',
        parentThreadId: 'thread.0',
        depth: 1,
      },
    })
    const boundary = harness.boundaryFactory.boundaries[0]
    assert.equal(boundary?.installedHistoricalGrants.length, 1)
    assert.deepEqual(boundary?.installedHistoricalGrants[0], {
      rootThreadId: 'thread.0',
      childThreadId: 'thread.0.child.history',
      parentThreadId: 'thread.0',
      depth: 1,
      status: 'active',
      activeTurnId: 'turn.thread.0.child.history',
      grantId: 'f'.repeat(64),
      assignmentId: `historical-assignment:${'e'.repeat(64)}`,
      allowedToolNames: ['rh_mission_history', 'rh_mission_history_page'],
    })
    assert.deepEqual(
      harness.bridge.delegatedReads.map(({ request }) => request),
      [
        {
          schema_version: 'mathematical_research.mission_semantic_request.v1',
          operation: 'orient',
          input: {},
        },
        {
          schema_version: 'mathematical_research.mission_semantic_request.v1',
          operation: 'retrieve',
          input: {
            mode: 'search',
            purpose: 'Find one exact historical composition candidate.',
            query: 'complementary residue',
          },
        },
      ],
    )
    assert.equal(
      harness.bridge.delegatedReads.every(({ binding }) =>
        binding.rootThreadId === 'thread.0' &&
        binding.executiveEpochId === 'epoch.0' &&
        binding.callerThreadId === 'thread.0.child.history' &&
        binding.parentThreadId === 'thread.0' &&
        binding.depth === 1 &&
        binding.turnId === 'turn.thread.0.child.history' &&
        binding.grantId === 'f'.repeat(64)),
      true,
    )
    assert.equal(
      harness.bridge.delegatedReads.every(({ grant }) =>
        grant.grant_id === 'f'.repeat(64) &&
        grant.assignment_id === `historical-assignment:${'e'.repeat(64)}`),
      true,
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
      ['record_strategy', 'checkpoint'],
    )
    assert.deepEqual(Object.keys(harness.store.state).sort(), [
      'activeGoal',
      'captureRecoveryRequired',
      'missionId',
      'schemaVersion',
      'updatedAt',
    ])
    assert.doesNotMatch(JSON.stringify(harness.store.saves), /historical_read_grant|grant_id|assignment_id/)
    assert.equal(
      fs.existsSync(path.join(harness.config.runtimeDir, '.rh-mission-owner-results')),
      false,
    )
  } finally {
    await cleanup(harness)
  }
})

test('named-decision Lifecycle Historian uses the positive Host grant-read-capture-integration route', async () => {
  const harness = await createHarness([{ kind: 'historical_advisory_historian' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.historicalGrantRequests.length, 1)
    assert.deepEqual(harness.bridge.historicalGrantRequests[0]?.request, {
      child_thread_id: 'thread.0.child.historian',
      assignment_mode: 'lifecycle_historian',
      assignment: 'Determine whether the exact retained lineage changes Strategy decision strategy:historian-pending.',
      context: { id: 'context:history-historian', revision: 1 },
      source_families: ['strategies', 'candidates'],
      raw_body_policy: 'metadata_only',
    })
    const boundary = harness.boundaryFactory.boundaries[0]
    assert.deepEqual(
      boundary?.installedHistoricalGrants[0] && {
        rootThreadId: boundary.installedHistoricalGrants[0].rootThreadId,
        childThreadId: boundary.installedHistoricalGrants[0].childThreadId,
        parentThreadId: boundary.installedHistoricalGrants[0].parentThreadId,
        depth: boundary.installedHistoricalGrants[0].depth,
      },
      {
        rootThreadId: 'thread.0',
        childThreadId: 'thread.0.child.historian',
        parentThreadId: 'thread.0',
        depth: 1,
      },
    )
    assert.deepEqual(boundary?.installedHistoricalGrants[0]?.allowedToolNames, [
      'rh_mission_history',
      'rh_mission_history_page',
    ])
    assert.deepEqual(
      harness.bridge.delegatedReads.map(({ request }) =>
        (request as JsonObject).operation),
      ['orient', 'retrieve'],
    )
    assert.equal(
      harness.bridge.delegatedReads.every(({ binding }) =>
        binding.rootThreadId === 'thread.0' &&
        binding.executiveEpochId === 'epoch.0' &&
        binding.callerThreadId === 'thread.0.child.historian' &&
        binding.parentThreadId === 'thread.0' &&
        binding.depth === 1 &&
        binding.grantId === 'f'.repeat(64)),
      true,
    )
    const historicalRetrieve = harness.bridge.delegatedReads[1]?.request as JsonObject
    assert.deepEqual(historicalRetrieve.input, {
      mode: 'read',
      purpose: 'Read the exact retained Strategy revision for the named pending decision.',
      ids: ['strategy:historian-prior@2'],
    })
    assert.deepEqual(
      harness.bridge.nativeMaterial.map((observation) => ({
        materialKind: (observation as JsonObject).materialKind,
        parentThreadId: (observation as JsonObject).parentThreadId,
        childThreadId: (observation as JsonObject).childThreadId,
      })),
      [
        {
          materialKind: 'assignment',
          parentThreadId: 'thread.0',
          childThreadId: 'thread.0.child.historian',
        },
        {
          materialKind: 'output',
          parentThreadId: 'thread.0',
          childThreadId: 'thread.0.child.historian',
        },
      ],
    )
    assert.deepEqual(
      boundary?.nativeMaterialCustodyOutcomes.map(({ status }) => status),
      ['captured', 'captured'],
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) =>
        (request as JsonObject).operation),
      ['interpret_material', 'record_strategy', 'checkpoint'],
    )
    const strategy = harness.bridge.semantics[1]?.request as JsonObject
    assert.deepEqual((strategy.input as JsonObject).causal_inputs, [{
      source: { id: 'evidence:historian-decision-lineage', revision: 1 },
      decision_consequence: 'Narrow strategy:historian-pending to the surviving compatible branch.',
    }])
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.doesNotMatch(
      JSON.stringify(harness.store.saves),
      /historical_read_grant|grant_id|assignment_id/,
    )
  } finally {
    await cleanup(harness)
  }
})

test('binding-mismatched owner historical grant never reaches Core or delegated dispatch', async () => {
  const harness = await createHarness([{ kind: 'historical_grant_mismatch' }])
  harness.bridge.historicalGrantEnvelopeMutator = (envelope) => ({
    ...envelope,
    executive_epoch_id: 'epoch.other',
  })
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.historicalGrantRequests.length, 1)
    assert.equal(
      harness.boundaryFactory.boundaries[0]?.installedHistoricalGrants.length,
      0,
    )
    assert.equal(harness.bridge.delegatedReads.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('late delegated projection after grant suspension removes its newly written page', async () => {
  const harness = await createHarness([
    { kind: 'historical_advisory_race' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.largeReadContent = 'late delegated historical result:' + 'x'.repeat(5 * 1024 * 1024)
  const host = createHost(harness)
  const filePromises = fs.promises as unknown as {
    writeFile: (...argumentsValue: any[]) => Promise<void>
  }
  const originalWriteFile = filePromises.writeFile.bind(fs.promises)
  let releaseLateWrite!: () => void
  const lateWriteMayReturn = new Promise<void>((resolve) => {
    releaseLateWrite = resolve
  })
  let observeDelegatedWrite!: () => void
  const delegatedWriteObserved = new Promise<void>((resolve) => {
    observeDelegatedWrite = resolve
  })
  let intercepted = false
  filePromises.writeFile = async (...argumentsValue: any[]): Promise<void> => {
    await originalWriteFile(...argumentsValue)
    const filePath = String(argumentsValue[0])
    if (!intercepted && path.basename(filePath).startsWith('delegated-result.')) {
      intercepted = true
      observeDelegatedWrite()
      await lateWriteMayReturn
    }
  }
  try {
    const running = host.run()
    await delegatedWriteObserved
    const boundary = harness.boundaryFactory.boundaries[0]
    assert.ok(boundary)
    await boundary.revokeLatestHistoricalGrant('goal_suspended')
    releaseLateWrite()

    const result = await running

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.delegatedReads.length, 1)
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as JsonObject).operation),
      ['record_strategy', 'checkpoint'],
    )
    assert.equal(
      (host as unknown as { pagedToolResults: Map<string, unknown> }).pagedToolResults.size,
      0,
    )
    const transportDirectory = path.join(
      harness.config.runtimeDir,
      '.rh-mission-owner-results',
    )
    assert.equal(fs.existsSync(transportDirectory), false)
  } finally {
    releaseLateWrite()
    filePromises.writeFile = originalWriteFile
    await cleanup(harness)
  }
})

test('ordinary owner reads larger than four MiB remain fully available through Host-private pages', async () => {
  const harness = await createHarness([{ kind: 'large_read' }])
  // Transport fixture only: exact source ownership/scopes are established by
  // Core retrieval tests. Here many literal relations must survive paging,
  // including repeated artifact handles with distinct qualified scopes.
  const navigation = {
    schema_version: 'mathematical_research.evidence_source_navigation.v1',
    relation: 'recorded_capture_scopes',
    items: Array.from({ length: 6000 }, (_, ordinal) => ({
      ordinal,
      handle: `capture-artifact:raw-capture:${'a'.repeat(48)}#${ordinal % 3}`,
      exact_scope: { description: `Scoped source ${ordinal}: é🙂 ${'qualification; '.repeat(70)}` },
    })),
  }
  const large = JSON.stringify({ subtype: 'evidence_meaning', source_navigation: navigation })
  assert.ok(Buffer.byteLength(large, 'utf8') > 5 * 1024 * 1024)
  harness.bridge.largeReadContent = large
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    const projected = harness.boundaryFactory.boundaries[0]?.largeReadResult
    assert.equal(projected?.operation, 'retrieve')
    assert.equal(
      ((projected?.result as Record<string, unknown>)?.readable_content),
      large,
    )
    assert.deepEqual(JSON.parse(String((projected?.result as Record<string, unknown>)?.readable_content)).source_navigation,
      navigation)
    assert.equal(
      fs.existsSync(path.join(harness.config.runtimeDir, '.rh-mission-owner-results')),
      false,
    )
    assert.deepEqual(
      (harness.boundaryFactory.requests[0]?.dynamicTools as Array<{ name: string }>).map(({ name }) => name),
      [
        'rh_mission',
        'rh_mission_history_grant',
        'rh_mission_history',
        'rh_mission_research_read_grant',
        'rh_mission_research_read',
        'rh_mission_a1_review_grant',
        'rh_mission_a1_review',
        'rh_mission_admission_open',
        'rh_mission_admission_grant',
        'rh_mission_admission',
        'rh_formal_attempt',
        'rh_mission_page',
        'rh_mission_history_page',
        'rh_mission_research_read_page',
        'rh_mission_a1_review_page',
        'rh_mission_admission_page',
      ],
    )
  } finally {
    await cleanup(harness)
  }
})

test('an active Host page replacement never exposes the replacement target', async () => {
  const harness = await createHarness([{ kind: 'large_read_tamper' }])
  harness.bridge.largeReadContent = 'private mathematical read:' + 'x'.repeat(5 * 1024 * 1024)
  const externalPath = path.join(harness.root, 'external-owner-secret.txt')
  const externalContent = 'replacement target must never be returned to the Goal'
  await fs.promises.writeFile(externalPath, externalContent)
  let replacementKind: 'symlink' | 'ordinary-file' = 'symlink'
  harness.boundaryFactory.onLargePageDescriptor = async ({ handle }) => {
    const pagePath = path.join(
      harness.config.runtimeDir,
      '.rh-mission-owner-results',
      `${handle}.json`,
    )
    await fs.promises.unlink(pagePath)
    try {
      await fs.promises.symlink(externalPath, pagePath, 'file')
    } catch (error) {
      if (!['EPERM', 'EACCES'].includes(String((error as NodeJS.ErrnoException).code))) {
        throw error
      }
      replacementKind = 'ordinary-file'
      await fs.promises.copyFile(externalPath, pagePath)
    }
  }
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    const failure = harness.boundaryFactory.boundaries[0]?.tamperFailureText ?? ''
    assert.match(failure, /result page changed after Host storage/i)
    assert.doesNotMatch(failure, new RegExp(externalContent))
    assert.equal(await fs.promises.readFile(externalPath, 'utf8'), externalContent)
    assert.ok(['symlink', 'ordinary-file'].includes(replacementKind))
    assert.equal(
      fs.existsSync(path.join(harness.config.runtimeDir, '.rh-mission-owner-results')),
      false,
    )
  } finally {
    await cleanup(harness)
  }
})

test('usage suspension preserves and resumes an unconsumed large owner result', async () => {
  const harness = await createHarness([
    { kind: 'large_read_unconsumed_usage_limited' },
    { kind: 'resume_large_read' },
  ])
  const large = 'unconsumed mathematical read:' + 'x'.repeat(5 * 1024 * 1024)
  harness.bridge.largeReadContent = large
  try {
    const first = await createHost(harness).run()

    assert.equal(first.status, 'usage_limited')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(
      fs.existsSync(path.join(harness.config.runtimeDir, '.rh-mission-owner-results')),
      true,
    )

    const second = await createHost(harness).run()

    assert.equal(second.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 1)
    const projected = harness.boundaryFactory.boundaries[1]?.largeReadResult
    assert.equal(
      ((projected?.result as Record<string, unknown>)?.readable_content),
      large,
    )
    assert.equal(
      fs.existsSync(path.join(harness.config.runtimeDir, '.rh-mission-owner-results')),
      false,
    )
  } finally {
    await cleanup(harness)
  }
})

test('usage suspension replays a final page whose response was not durably received', async () => {
  const harness = await createHarness([
    { kind: 'large_read_final_page_unacknowledged_usage_limited' },
    { kind: 'resume_final_page_replay' },
  ])
  harness.bridge.largeReadContent = 'unacknowledged final page:' + 'x'.repeat(5 * 1024 * 1024)
  try {
    const first = await createHost(harness).run()
    const retained = harness.boundaryFactory.retainedFinalPageReplay

    assert.equal(first.status, 'usage_limited')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.ok(retained)
    const pagePath = path.join(
      harness.config.runtimeDir,
      '.rh-mission-owner-results',
      `${retained.handle}.json`,
    )
    assert.equal(fs.existsSync(pagePath), true)

    const second = await createHost(harness).run()

    assert.equal(second.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 1)
    assert.equal(fs.existsSync(pagePath), false)
  } finally {
    await cleanup(harness)
  }
})

test('startup removes only stale Host page files and never follows paging symlinks', async () => {
  const harness = await createHarness([])
  try {
    const unrelatedRuntimePath = path.join(harness.config.runtimeDir, 'mission-host-state.keep')
    const transportDirectory = path.join(harness.config.runtimeDir, '.rh-mission-owner-results')
    const stalePath = path.join(
      transportDirectory,
      'result.11111111-1111-4111-8111-111111111111.json',
    )
    await fs.promises.mkdir(transportDirectory, { mode: 0o700 })
    await fs.promises.writeFile(unrelatedRuntimePath, 'keep')
    await fs.promises.writeFile(stalePath, 'x'.repeat(1024 * 1024))

    const externalDirectory = await fs.promises.mkdtemp(path.join(harness.root, 'external-pages-'))
    const externalPath = path.join(externalDirectory, 'external.json')
    await fs.promises.writeFile(externalPath, 'external must remain')
    const linkedPath = path.join(
      transportDirectory,
      'result.22222222-2222-4222-8222-222222222222.json',
    )
    let fileSymlinkCreated = false
    try {
      await fs.promises.symlink(externalPath, linkedPath, 'file')
      fileSymlinkCreated = true
    } catch (error) {
      if (!['EPERM', 'EACCES'].includes(String((error as NodeJS.ErrnoException).code))) {
        throw error
      }
    }

    harness.bridge.seedCheckpoint('thread.stopped.cleanup', 'closeout')
    harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(fs.existsSync(stalePath), false)
    assert.equal(await fs.promises.readFile(unrelatedRuntimePath, 'utf8'), 'keep')
    assert.equal(await fs.promises.readFile(externalPath, 'utf8'), 'external must remain')
    if (fileSymlinkCreated) {
      assert.equal(fs.existsSync(linkedPath), false)
    }

    const externalTransportDirectory = await fs.promises.mkdtemp(path.join(harness.root, 'external-dir-'))
    const externalTransportFile = path.join(externalTransportDirectory, 'external.json')
    await fs.promises.writeFile(externalTransportFile, 'external directory must remain')
    let directorySymlinkCreated = false
    try {
      await fs.promises.symlink(
        externalTransportDirectory,
        transportDirectory,
        process.platform === 'win32' ? 'junction' : 'dir',
      )
      directorySymlinkCreated = true
    } catch (error) {
      if (!['EPERM', 'EACCES'].includes(String((error as NodeJS.ErrnoException).code))) {
        throw error
      }
    }
    if (directorySymlinkCreated) {
      const second = await createHost(harness).run()
      assert.equal(second.status, 'stopped_semantically')
      assert.equal(
        await fs.promises.readFile(externalTransportFile, 'utf8'),
        'external directory must remain',
      )
      assert.equal((await fs.promises.lstat(transportDirectory)).isSymbolicLink(), true)
    }
  } finally {
    await cleanup(harness)
  }
})

test('Core Goal cancellation aborts and drains an active owner semantic operation', async () => {
  const harness = await createHarness([{ kind: 'core_cancel_dynamic' }])
  harness.bridge.largeReadContent = 'owner result that remains pending until Core cancellation'
  harness.bridge.blockSemanticResponse = true
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'usage_limited')
    assert.equal(harness.bridge.semanticSignals.length, 1)
    assert.equal(harness.bridge.semanticSignals[0]?.aborted, true)
    assert.equal(harness.bridge.semanticSignals[0]?.reason, 'core_usage_limited')
  } finally {
    await cleanup(harness)
  }
})

test('Core Goal cancellation aborts and drains an active native-material owner capture', async () => {
  const harness = await createHarness([{ kind: 'core_cancel_material' }])
  harness.bridge.blockNativeMaterialResponse = true
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'usage_limited')
    assert.equal(harness.bridge.nativeMaterialSignals.length, 1)
    assert.equal(harness.bridge.nativeMaterialSignals[0]?.aborted, true)
    assert.equal(harness.bridge.nativeMaterialSignals[0]?.reason, 'core_fatal_fence')
    assert.equal(harness.bridge.nativeMaterial.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('operator cancellation drains a blocked initial current-state snapshot without starting a Goal', async () => {
  const harness = await createHarness([])
  harness.bridge.blockHostSnapshotResponse = true
  let started!: () => void
  const reconstructStarted = new Promise<void>((resolve) => {
    started = resolve
  })
  harness.bridge.onHostSnapshotStarted = started
  const controller = new AbortController()
  try {
    const running = createHost(harness).run(controller.signal)
    await reconstructStarted
    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.equal(harness.bridge.authorizations.length, 0)
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.store.state.activeGoal, null)
    assert.ok(harness.bridge.cancellations.length >= 1)
  } finally {
    await cleanup(harness)
  }
})

test('operator cancellation after the durable local plan stops before owner authorization or Goal allocation', async () => {
  const harness = await createHarness([])
  const controller = new AbortController()
  harness.store.deferSaves = true
  try {
    const running = createHost(harness).run(controller.signal)
    await harness.store.waitForDeferredSaveCount(1)
    controller.abort('operator_stop')
    harness.store.resolveLastDeferredSave()
    await harness.store.waitForDeferredSaveCount(1)
    harness.store.resolveLastDeferredSave()

    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.deepEqual(harness.bridge.authorizationCuts, [])
    assert.deepEqual(harness.bridge.authorizations, [])
    assert.deepEqual(harness.allocatedWorkspaces, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await cleanup(harness)
  }
})

test('explicit force-stop reason remains distinct while draining an owner operation', async () => {
  const harness = await createHarness([])
  harness.bridge.blockHostSnapshotResponse = true
  let started!: () => void
  const reconstructStarted = new Promise<void>((resolve) => {
    started = resolve
  })
  harness.bridge.onHostSnapshotStarted = started
  const controller = new AbortController()
  try {
    const running = createHost(harness).run(controller.signal)
    await reconstructStarted
    controller.abort('explicit_force_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.ok(harness.bridge.cancellations.includes('explicit_force_stop'))
  } finally {
    await cleanup(harness)
  }
})

test('initial current-state snapshot cancellation suspends a persisted bound Goal instead of swallowing it', async () => {
  const harness = await createHarness([])
  const threadId = 'thread.persisted-initial-stop'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  await harness.bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId: threadId, workspaceRoot })
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'registered',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: null,
    },
  }
  harness.bridge.blockHostSnapshotResponse = true
  let started!: () => void
  const reconstructionStarted = new Promise<void>((resolve) => {
    started = resolve
  })
  harness.bridge.onHostSnapshotStarted = started
  const boundaryFactory = new SignalBlockingBoundaryFactory('read')
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await reconstructionStarted
    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, threadId)
    assert.equal(harness.store.state.activeGoal?.failureReason, 'operator_stop')
    assert.deepEqual(boundaryFactory.boundary?.suspendCalls, [
      { threadId, reason: 'operator_stop' },
    ])
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('force during initial current-state snapshot terminalizes the persisted bound Goal', async () => {
  const harness = await createHarness([])
  const threadId = 'thread.persisted-initial-force'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  await harness.bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId: threadId, workspaceRoot })
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'registered',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: null,
    },
  }
  harness.bridge.blockHostSnapshotResponse = true
  let started!: () => void
  const reconstructionStarted = new Promise<void>((resolve) => {
    started = resolve
  })
  harness.bridge.onHostSnapshotStarted = started
  const boundaryFactory = new SignalBlockingBoundaryFactory('read')
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await reconstructionStarted
    controller.abort('operator_stop')
    await host.requestExplicitForceStop()
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(boundaryFactory.boundary?.stopCalls, [
      { threadId, reason: 'explicit_force_stop' },
    ])
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['explicit_force_stop'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('initial cancellation reconciles a late materialized root from persisted authorization', async (t) => {
  for (const mode of ['ordinary', 'force'] as const) {
    await t.test(mode, async () => {
      const harness = await createHarness([
        { kind: 'recovery', discoverMaterializedStart: true },
      ])
      const workspaceRoot = await harness.allocateWorkspace()
      const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
      const executiveEpochId = String(authorized.executive_epoch_id)
      harness.store.state = {
        ...freshState(harness.config),
        activeGoal: {
          phase: 'authorized',
          threadId: null,
          objective: MISSION_OBJECTIVE,
          workspaceRoot,
          executiveEpochId,
          failureReason: null,
        },
      }
      harness.bridge.blockHostSnapshotResponse = true
      let started!: () => void
      const reconstructionStarted = new Promise<void>((resolve) => {
        started = resolve
      })
      harness.bridge.onHostSnapshotStarted = started
      const controller = new AbortController()
      const host = createHost(harness)
      try {
        const running = host.run(controller.signal)
        await reconstructionStarted
        controller.abort('operator_stop')
        if (mode === 'force') {
          await host.requestExplicitForceStop()
        }
        const result = await running

        assert.equal(result.status, 'operator_stopped')
        assert.deepEqual(harness.bridge.bindings, [
          {
            executiveEpochId,
            rootThreadId: 'thread.0',
            workspaceRoot,
          },
        ])
        assert.equal(harness.bridge.bindingSignals.length, 0)
        if (mode === 'ordinary') {
          assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
          assert.equal(harness.store.state.activeGoal?.threadId, 'thread.0')
          assert.equal(harness.store.state.activeGoal?.failureReason, 'operator_stop')
          assert.deepEqual(harness.bridge.failures, [])
        } else {
          assert.equal(harness.store.state.activeGoal, null)
          assert.deepEqual(
            harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
            ['explicit_force_stop'],
          )
        }
      } finally {
        await cleanup(harness)
      }
    })
  }
})

test('cancellation during persisted recovery boundary startup still suspends the exact root', async () => {
  const harness = await createHarness([])
  const threadId = 'thread.persisted-boundary-start'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  await harness.bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId: threadId, workspaceRoot })
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'registered',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: null,
    },
  }
  const interruptedFactory = new SignalBlockingBoundaryFactory('start')
  const recoveryFactory = new SignalBlockingBoundaryFactory('read')
  let factoryCall = 0
  const boundaryFactory: CodexEpochBoundaryFactory = {
    create: (root, callbacks) => {
      factoryCall += 1
      return factoryCall === 1
        ? interruptedFactory.create(root, callbacks)
        : recoveryFactory.create(root, callbacks)
    },
  }
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await interruptedFactory.entered
    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.equal(factoryCall, 2)
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, threadId)
    assert.equal(harness.store.state.activeGoal?.failureReason, 'operator_stop')
    assert.deepEqual(recoveryFactory.boundary?.suspendCalls, [
      { threadId, reason: 'operator_stop' },
    ])
  } finally {
    await cleanup(harness)
  }
})

test('cancellation during suspended exact-thread resume leaves that suspension intact', async () => {
  const harness = await createHarness([])
  const threadId = 'thread.persisted-resume-start'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  await harness.bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId: threadId, workspaceRoot })
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'suspended',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: 'operator_stop',
    },
  }
  const boundaryFactory = new SignalBlockingBoundaryFactory('start')
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered
    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, threadId)
    assert.equal(harness.store.state.activeGoal?.failureReason, 'operator_stop')
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('operator cancellation reaches boundary startup and pre-identity Goal startup', async (t) => {
  for (const phase of ['start', 'goal_start'] as const) {
    await t.test(phase, async () => {
      const harness = await createHarness([])
      const boundaryFactory = new SignalBlockingBoundaryFactory(phase)
      const controller = new AbortController()
      const host = new MissionHost(
        harness.config,
        () => harness.bridge,
        boundaryFactory,
        harness.store,
        async () => {},
        harness.allocateWorkspace,
      )
      try {
        const running = host.run(controller.signal)
        await boundaryFactory.entered
        assert.equal(boundaryFactory.boundary?.receivedSignal, controller.signal)

        controller.abort('operator_stop')
        const result = await running

        assert.equal(result.status, 'operator_stopped')
        assert.equal(boundaryFactory.boundary?.stopCount, 1)
        assert.deepEqual(boundaryFactory.boundary?.stopCalls, [])
        assert.equal(harness.bridge.authorizations.length, 1)
        assert.deepEqual(
          harness.bridge.failures.map(({ reconciliation }) => reconciliation),
          [{ stage: 'authorization_only', failure_reason: 'operator_stop' }],
        )
        assert.equal(harness.store.state.activeGoal, null)
      } finally {
        await cleanup(harness)
      }
    })
  }
})

test('lost authorization response terminalizes once and preserves the original failure', async () => {
  const harness = await createHarness([{ kind: 'recovery' }])
  harness.bridge.failAuthorizationResponseAfterCommit = true
  harness.bridge.onAuthorize = () => {
    assert.equal(harness.store.state.activeGoal?.phase, 'planned')
    assert.equal(harness.store.state.activeGoal?.executiveEpochId, null)
  }
  try {
    await assert.rejects(
      createHost(harness).run(),
      /simulated lost authorization response/,
    )
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ executiveEpochId, reconciliation }) => ({
        executiveEpochId,
        reconciliation,
      })),
      [
        {
          executiveEpochId: 'epoch.0',
          reconciliation: {
            stage: 'authorization_only',
            failure_reason: 'turn_start_failure',
          },
        },
      ],
    )
  } finally {
    await cleanup(harness)
  }
})

test('pre-identity Goal start failure waits for an explicit corrected restart', async () => {
  const harness = await createHarness([
    { kind: 'pre_identity_start_failure' },
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'closeout' },
  ])
  try {
    await assert.rejects(
      createHost(harness).run(),
      /simulated pre-identity Goal start failure/,
    )

    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.bridge.bindings.length, 0)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation),
      [{ stage: 'authorization_only', failure_reason: 'turn_start_failure' }],
    )
    assert.equal(harness.store.state.activeGoal, null)

    const restarted = await createHost(harness).run()
    assert.equal(restarted.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizations.length, 2)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.equal(harness.bridge.bindings.length, 1)
    assert.equal(harness.bridge.failures.length, 1)
  } finally {
    await cleanup(harness)
  }
})

test('pre-identity Goal start failure is preserved when Boundary cleanup also fails', async () => {
  const harness = await createHarness([
    {
      kind: 'pre_identity_start_failure',
      stopFailure: 'simulated Boundary cleanup failure',
    },
  ])
  try {
    await assert.rejects(
      createHost(harness).run(),
      (error: unknown) => {
        assert.ok(error instanceof AggregateError)
        assert.match(error.message, /operation failed and its Codex boundary did not stop cleanly/)
        assert.deepEqual(
          error.errors.map((nested) => nested instanceof Error ? nested.message : String(nested)),
          [
            'simulated pre-identity Goal start failure',
            'simulated Boundary cleanup failure',
          ],
        )
        return true
      },
    )
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.bridge.bindings.length, 0)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation),
      [{ stage: 'authorization_only', failure_reason: 'turn_start_failure' }],
    )
    const workspace = harness.allocatedWorkspaces[0]!
    assert.equal(harness.store.state.activeGoal?.workspaceRoot, workspace)
    assert.equal(fs.existsSync(workspace), true)
  } finally {
    await cleanup(harness)
  }
})

test('lost owner bind response terminalizes once and preserves the original failure', async () => {
  const harness = await createHarness([{ kind: 'recovery' }])
  harness.bridge.failBindingResponseAfterCommit = true
  try {
    await assert.rejects(
      createHost(harness).run(),
      /simulated lost binding response/,
    )
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.bridge.bindings.length, 1)
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation),
      [{ stage: 'goal_runtime', failure_reason: 'turn_start_failure' }],
    )
  } finally {
    await cleanup(harness)
  }
})

test('operator cancellation interrupts Goal-state reads and suspends the materialized root', async () => {
  const harness = await createHarness([])
  const boundaryFactory = new SignalBlockingBoundaryFactory('read')
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered
    assert.equal(boundaryFactory.boundary?.receivedSignal, controller.signal)
    assert.equal(harness.bridge.bindings.length, 1)

    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.deepEqual(boundaryFactory.boundary?.suspendCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'operator_stop' },
    ])
    assert.equal(boundaryFactory.boundary?.stopCount, 1)
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, 'thread.signal-cancellation')
    assert.equal(harness.store.state.activeGoal?.failureReason, 'operator_stop')
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('provider usage suspension wins a race with ordinary operator cancellation', async () => {
  const harness = await createHarness([])
  const boundaryFactory = new SignalBlockingBoundaryFactory('read', 'usageLimited')
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered

    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'usage_limited')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, 'thread.signal-cancellation')
    assert.equal(harness.store.state.activeGoal?.failureReason, 'usageLimited')
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('explicit force-stop terminalizes an active bound Goal', async () => {
  const harness = await createHarness([])
  const boundaryFactory = new SignalBlockingBoundaryFactory('read')
  const controller = new AbortController()
  let canaryConsumeCalls = 0
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
    null,
    async () => false,
    async () => {
      canaryConsumeCalls += 1
      return true
    },
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered

    controller.abort('explicit_force_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.equal(canaryConsumeCalls, 0)
    assert.deepEqual(boundaryFactory.boundary?.stopCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'explicit_force_stop' },
    ])
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['explicit_force_stop'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('explicit force-stop escalates an in-flight ordinary suspension', async () => {
  const harness = await createHarness([])
  let suspensionEntered!: () => void
  const enteredSuspension = new Promise<void>((resolve) => {
    suspensionEntered = resolve
  })
  let releaseSuspension!: () => void
  const suspensionRelease = new Promise<void>((resolve) => {
    releaseSuspension = resolve
  })
  const boundaryFactory = new SignalBlockingBoundaryFactory(
    'read',
    null,
    async () => {
      suspensionEntered()
      await suspensionRelease
    },
  )
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered

    controller.abort('operator_stop')
    await enteredSuspension
    const forcing = host.requestExplicitForceStop()
    await forcing
    releaseSuspension()
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.deepEqual(boundaryFactory.boundary?.suspendCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'operator_stop' },
    ])
    assert.deepEqual(boundaryFactory.boundary?.stopCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'explicit_force_stop' },
    ])
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['explicit_force_stop'],
    )
  } finally {
    releaseSuspension?.()
    await cleanup(harness)
  }
})

test('explicit force-stop terminalizes a Goal after ordinary suspension has already returned', async () => {
  const harness = await createHarness([])
  const suspensionFactory = new SignalBlockingBoundaryFactory('read')
  const forceFactory = new SignalBlockingBoundaryFactory('read')
  let boundaryCount = 0
  const boundaryFactory: CodexEpochBoundaryFactory = {
    create: (workspaceRoot, callbacks) => {
      boundaryCount += 1
      return (boundaryCount === 1 ? suspensionFactory : forceFactory).create(
        workspaceRoot,
        callbacks,
      )
    },
  }
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await suspensionFactory.entered

    controller.abort('operator_stop')
    const result = await running
    const suspensionBoundary = suspensionFactory.boundary

    assert.equal(result.status, 'operator_stopped')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.deepEqual(suspensionBoundary?.suspendCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'operator_stop' },
    ])

    await host.requestExplicitForceStop()
    const forceBoundary = forceFactory.boundary

    assert.notEqual(forceBoundary, suspensionBoundary)
    assert.equal(boundaryCount, 2)
    assert.deepEqual(forceBoundary?.stopCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'explicit_force_stop' },
    ])
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['explicit_force_stop'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('a committed owner checkpoint keeps precedence over explicit force-stop', async () => {
  const harness = await createHarness([])
  const boundaryFactory = new SignalBlockingBoundaryFactory('read')
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered
    const binding = harness.bridge.bindings.at(-1)
    assert.ok(binding)
    harness.bridge.seedCheckpoint(
      'thread.signal-cancellation',
      'closeout',
      binding.executiveEpochId,
    )

    controller.abort('explicit_force_stop')
    const result = await running

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as JsonObject).state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('a pending force marker terminalizes the retained suspension before a fresh Host starts', async () => {
  const harness = await createHarness([{ kind: 'recovery' }, { kind: 'semantic_stop' }])
  const markerPath = path.join(harness.config.runtimeDir, 'force-stop.pending')
  const threadId = 'thread.pending-force-intent'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  await harness.bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId: threadId, workspaceRoot })
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'suspended',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: 'operator_stop',
    },
  }
  await fs.promises.writeFile(markerPath, '', { flag: 'wx' })
  let hostCount = 0
  const createFreshHost = () => {
    hostCount += 1
    return createHost(harness)
  }
  try {
    await consumePendingForceStop(harness.config.runtimeDir, createFreshHost)

    assert.equal(fs.existsSync(markerPath), false)
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['explicit_force_stop'],
    )
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId, reason: 'explicit_force_stop' },
    ])
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)

    const result = await createFreshHost().run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(hostCount, 2)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    assert.equal(harness.bridge.authorizations.length, 2)
  } finally {
    await cleanup(harness)
  }
})

test('a pending force marker preserves a committed owner checkpoint before fresh startup', async () => {
  const harness = await createHarness([{ kind: 'recovery' }, { kind: 'semantic_stop' }])
  const markerPath = path.join(harness.config.runtimeDir, 'force-stop.pending')
  const threadId = 'thread.pending-force-checkpoint'
  const workspaceRoot = await harness.allocateWorkspace()
  const terminal = harness.bridge.seedCheckpoint(threadId, 'continue')
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'suspended',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId: terminal.executiveEpochId,
      failureReason: 'operator_stop',
    },
  }
  await fs.promises.writeFile(markerPath, '', { flag: 'wx' })
  try {
    await consumePendingForceStop(
      harness.config.runtimeDir,
      () => createHost(harness),
    )

    assert.equal(fs.existsSync(markerPath), false)
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId, reason: 'owner_checkpoint' },
    ])
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)

    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as JsonObject).state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('operator cancellation during known-root Goal startup preserves the resumable root', async () => {
  const harness = await createHarness([])
  const boundaryFactory = new SignalBlockingBoundaryFactory('goal_start_known')
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered
    assert.equal(boundaryFactory.boundary?.receivedSignal, controller.signal)
    assert.equal(harness.bridge.bindings.length, 1)

    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'operator_stopped')
    assert.deepEqual(boundaryFactory.boundary?.suspendCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'operator_stop' },
    ])
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, 'thread.signal-cancellation')
    assert.equal(harness.store.state.activeGoal?.failureReason, 'operator_stop')
    assert.deepEqual(harness.bridge.failures, [])
  } finally {
    await cleanup(harness)
  }
})

test('operator cancellation after an owner checkpoint commit reconciles without a duplicate write on restart', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  harness.bridge.blockCheckpointResponse = true
  let committed!: () => void
  const ownerCommitted = new Promise<void>((resolve) => {
    committed = resolve
  })
  harness.bridge.onCheckpointCommitted = committed
  const controller = new AbortController()
  try {
    const running = createHost(harness).run(controller.signal)
    await ownerCommitted
    controller.abort('operator_stop')
    const cancelled = await running

    assert.equal(cancelled.status, 'stopped_semantically')
    assert.equal(harness.bridge.failures.length, 0)
    assert.ok(harness.bridge.cancellations.length >= 1)
    assert.equal(harness.store.state.activeGoal, null)

    harness.bridge.blockCheckpointResponse = false
    const restarted = await createHost(harness).run()
    assert.equal(restarted.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.bridge.failures.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('owner checkpoint committing during operator suspension wins without a failure terminal', async () => {
  const harness = await createHarness([])
  const boundaryFactory = new SignalBlockingBoundaryFactory(
    'read',
    'paused',
    () => {
      const binding = harness.bridge.bindings.at(-1)
      assert.ok(binding)
      harness.bridge.seedCheckpoint(
        'thread.signal-cancellation',
        'closeout',
        binding.executiveEpochId,
      )
    },
  )
  const controller = new AbortController()
  const host = new MissionHost(
    harness.config,
    () => harness.bridge,
    boundaryFactory,
    harness.store,
    async () => {},
    harness.allocateWorkspace,
  )
  try {
    const running = host.run(controller.signal)
    await boundaryFactory.entered

    controller.abort('operator_stop')
    const result = await running

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(boundaryFactory.boundary?.stopCalls, [
      { threadId: 'thread.signal-cancellation', reason: 'owner_checkpoint' },
    ])
  } finally {
    await cleanup(harness)
  }
})

test('one rejected material capture keeps the restart-stable visible recovery route', async () => {
  const harness = await createHarness([
    {
      kind: 'checkpoint',
      workerCount: 1,
      missionContinuation: 'continue',
      recoverRejectedNativeMaterial: true,
    },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.captureFailuresRemaining = 1
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.nativeMaterial.length, 1)
    assert.equal(
      (harness.bridge.nativeMaterial[0] as Record<string, unknown>).materialKind,
      'output',
    )
    const firstBoundary = harness.boundaryFactory.boundaries[0]
    assert.deepEqual(
      firstBoundary?.nativeMaterialCustodyOutcomes.map((outcome) => outcome.status),
      ['capture_recovery_required', 'captured'],
    )
    const recoveryOutcome = firstBoundary?.nativeMaterialCustodyOutcomes[0] as JsonObject
    assert.equal(recoveryOutcome.ownerCode, 'invalid_invocation')
    assert.deepEqual(recoveryOutcome.nativeLineage, {
      materialKind: 'assignment',
      rootThreadId: 'thread.0',
      parentThreadId: 'thread.0',
      childThreadId: 'thread.0.child.0',
    })
    assert.equal(
      JSON.stringify(recoveryOutcome).includes('Investigate useful RH route 0'),
      false,
    )
    const visibleNotice = firstBoundary?.rootToolResultPayloads.find(
      (payload) => Array.isArray(payload.capture_recovery_required),
    )
    assert.ok(visibleNotice)
    assert.equal(
      JSON.stringify(visibleNotice.capture_recovery_required).includes(
        'Investigate useful RH route 0',
      ),
      false,
    )
    assert.deepEqual(
      harness.bridge.semantics.map(({ request }) => (request as Record<string, unknown>).operation),
      ['interpret_material', 'record_strategy', 'checkpoint', 'record_strategy', 'checkpoint'],
    )
    const interpretation = harness.bridge.semantics[0]?.request as JsonObject
    const interpretationInput = interpretation.input as JsonObject
    assert.deepEqual(interpretationInput.capture_scopes, [
      {
        adopted_root_material: {
          channel: 'native_assignment',
          content: 'Investigate useful RH route 0',
          native_lineage: {
            material_kind: 'assignment',
            parent_thread_id: 'thread.0',
            child_thread_id: 'thread.0.child.0',
          },
        },
        exact_scope: 'The complete returned worker assignment.',
        coverage: 'complete_artifact',
      },
    ])
    assert.deepEqual(firstBoundary?.rootToolResultContentItemCounts.slice(-3), [1, 1, 1])
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [])
    assert.equal(harness.boundaryFactory.requests.length, 2)
    for (const request of harness.boundaryFactory.requests) {
      assert.match(resolvedInstructionsForTest(request), /Delegating_And_Continuing\.md/)
      const guide = scientificGuideForTest('Delegating_And_Continuing.md')
      assert.match(guide, /A missing capture or custody failure does not authorize repeating the research/)
      assert.match(guide, /`interpret_material`/)
      assert.match(guide, /`capture_scopes\[\]\.adopted_root_material`/)
      assert.match(guide, /`native_lineage`/)
      assert.match(guide, /`material_kind`, `parent_thread_id`, and `child_thread_id`/)
      assert.match(guide, /Successful custody and notice clearing are distinct/)
    }
  } finally {
    await cleanup(harness)
  }
})

test('capture recovery notice survives same-root Host restart and clears after exact adoption', async () => {
  const harness = await createHarness([
    { kind: 'usage_limited', captureRecovery: true },
    {
      kind: 'checkpoint',
      workerCount: 0,
      missionContinuation: 'continue',
      recoverRejectedNativeMaterial: true,
    },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.captureFailuresRemaining = 1
  try {
    const first = await createHost(harness).run()

    assert.equal(first.status, 'usage_limited')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, 'thread.0')
    assert.equal(harness.store.state.captureRecoveryRequired.length, 1)
    const retained = harness.store.state.captureRecoveryRequired[0]!
    assert.equal(retained.status, 'capture_recovery_required')
    assert.equal(retained.plaintextReference.kind, 'codex_native_material')
    assert.equal(retained.nativeLineage.childThreadId, 'thread.0.child.0')
    assert.equal(
      JSON.stringify(retained).includes('Investigate useful RH route 0'),
      false,
    )
    assert.equal(harness.bridge.nativeMaterial.length, 1)
    assert.equal(
      (harness.bridge.nativeMaterial[0] as Record<string, unknown>).content,
      'Unrelated useful RH output remains capturable.',
    )

    const inspected = inspectMissionGoalLaunch(harness.config, await harness.bridge.hostSnapshot(),
      harness.store.state.activeGoal!.workspaceRoot, harness.store.state)
    assert.equal(inspected.host_continuity.capture_recovery_required.length, 1)
    const inspectedNotice = inspected.host_continuity.capture_recovery_required[0]!
    assert.equal(inspectedNotice.observation_id, retained.observationId)
    assert.equal((inspectedNotice.native_lineage as JsonObject).child_thread_id, retained.nativeLineage.childThreadId)
    assert.doesNotMatch(JSON.stringify(inspected.host_continuity), /Investigate useful RH route 0|Unrelated useful RH output/)
    assert.equal(harness.store.state.captureRecoveryRequired.length, 1)

    const resumed = await createHost(harness).run()

    assert.equal(resumed.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.resumeRequests[0]?.threadId, 'thread.0')
    const resumedBoundary = harness.boundaryFactory.boundaries[1]
    const visibleNotice = resumedBoundary?.rootToolResultPayloads.find(
      (payload) => Array.isArray(payload.capture_recovery_required),
    )
    assert.ok(visibleNotice)
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [])
    assert.equal(harness.bridge.nativeMaterial.length, 1)
    const adopted = harness.bridge.semantics.find(
      ({ request }) => (request as Record<string, unknown>).operation === 'interpret_material',
    )?.request as JsonObject
    const adoptedInput = adopted.input as JsonObject
    assert.deepEqual(adoptedInput.capture_scopes, [
      {
        adopted_root_material: {
          channel: 'native_assignment',
          content: 'Investigate useful RH route 0',
          native_lineage: {
            material_kind: 'assignment',
            parent_thread_id: 'thread.0',
            child_thread_id: 'thread.0.child.0',
          },
        },
        exact_scope: 'The complete returned worker assignment.',
        coverage: 'complete_artifact',
      },
    ])
  } finally {
    await cleanup(harness)
  }
})

for (const predecessorRecoveryShape of ['exact', 'omitted_lineage', 'wrong_channel'] as const) {
test(`a successor cannot clear predecessor capture via ${predecessorRecoveryShape}`, async () => {
  const harness = await createHarness([
    { kind: 'usage_limited', captureRecovery: true },
    {
      kind: 'checkpoint',
      workerCount: 0,
      missionContinuation: 'continue',
    },
    {
      kind: 'checkpoint',
      workerCount: 0,
      missionContinuation: 'closeout',
      attemptPredecessorCaptureRecovery: predecessorRecoveryShape,
    },
  ])
  harness.bridge.captureFailuresRemaining = 1
  try {
    assert.equal((await createHost(harness).run()).status, 'usage_limited')

    const resumed = await createHost(harness).run()

    assert.equal(resumed.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.resumeRequests[0]?.threadId, 'thread.0')
    assert.equal(harness.store.state.captureRecoveryRequired.length, 1)
    assert.equal(
      harness.store.state.captureRecoveryRequired[0]?.nativeLineage.rootThreadId,
      'thread.0',
    )
    const successorBoundary = harness.boundaryFactory.boundaries[2]
    assert.ok(successorBoundary)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.equal(
      successorBoundary.rootToolResultPayloads.some(
        (payload) => Array.isArray(payload.capture_recovery_required),
      ),
      true,
    )
    const semanticRequests = harness.bridge.semantics.map(({ request }) => request as JsonObject)
    const operations = semanticRequests.map(({ operation }) => operation)
    if (predecessorRecoveryShape === 'exact') {
      assert.deepEqual(
        operations,
        ['interpret_material', 'record_strategy', 'checkpoint', 'record_strategy', 'checkpoint'],
      )
    } else {
      assert.deepEqual(
        operations,
        [
          'interpret_material',
          'record_strategy',
          'checkpoint',
          'interpret_material',
          'record_strategy',
          'checkpoint',
        ],
      )
    }
    assert.equal(
      semanticRequests.some((request) => JSON.stringify(request).includes(
        'The complete returned predecessor worker assignment.',
      )),
      predecessorRecoveryShape !== 'exact',
    )
    assert.match(
      scientificGuideForTest('Delegating_And_Continuing.md'),
      /If predecessor plaintext is not actually visible, do not invent access or clear the notice by assertion/,
    )
  } finally {
    await cleanup(harness)
  }
})
}

test('native material binding mismatch remains an explicit Mission consistency failure', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  try {
    const result = await host.run()
    assert.equal(result.status, 'stopped_semantically')
    harness.bridge.captureFailuresRemaining = 1
    harness.bridge.captureFailureCode = 'mission_host_bridge_binding_mismatch'

    await assert.rejects(
      host.onNativeMaterialObserved(
        {
          observationId: 'late.binding-mismatch',
          materialKind: 'output',
          content: 'Late material with a contradictory owner binding.',
          rootThreadId: 'thread.0',
          parentThreadId: 'thread.0',
          childThreadId: 'thread.0.child.binding-mismatch',
        },
        rootGoalOperationContext('thread.0'),
      ),
      { name: 'GoalEpochMissionConsistencyError' },
    )
  } finally {
    await cleanup(harness)
  }
})

test('confirmed shared material integrity failure remains a Mission-wide authority loss', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  try {
    const result = await host.run()
    assert.equal(result.status, 'stopped_semantically')
    harness.bridge.captureFailuresRemaining = 1
    harness.bridge.captureFailureCode = 'mission_shared_integrity_failure'

    await assert.rejects(
      host.onNativeMaterialObserved(
        {
          observationId: 'late.shared-integrity-failure',
          materialKind: 'output',
          content: 'Late material whose shared custody path reports integrity loss.',
          rootThreadId: 'thread.0',
          parentThreadId: 'thread.0',
          childThreadId: 'thread.0.child.shared-integrity-failure',
        },
        rootGoalOperationContext('thread.0'),
      ),
      { name: 'GoalEpochSharedAuthorityLossError' },
    )
  } finally {
    await cleanup(harness)
  }
})

test('capture recovery persistence failure stays local and exact capture can clear it', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  const observation = {
    observationId: 'late.recovery-persistence',
    materialKind: 'output' as const,
    content: 'Late exact material whose ordinary capture requires recovery.',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.recovery-persistence',
  }
  try {
    assert.equal((await host.run()).status, 'stopped_semantically')
    harness.bridge.captureFailuresRemaining = 1
    harness.store.saveFailuresRemaining = 1

    const recovery = await host.onNativeMaterialObserved(
      observation,
      rootGoalOperationContext('thread.0'),
    )
    assert.equal(recovery.status, 'capture_recovery_required')
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [])
    assert.equal(
      (host as unknown as { state: MissionHostState }).state
        .captureRecoveryRequired.length,
      1,
    )

    const captured = await host.onNativeMaterialObserved(
      observation,
      rootGoalOperationContext('thread.0'),
    )
    assert.equal(captured.status, 'captured')
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [])
  } finally {
    await cleanup(harness)
  }
})

test('capture recovery retains one exact identity across changing local owner failures', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  const material = {
    materialKind: 'output' as const,
    content: 'One exact observation can encounter more than one local capture failure.',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.recovery-retry',
  }
  const observation = {
    observationId: nativeObservationIdForTest(material),
    ...material,
  }
  try {
    assert.equal((await host.run()).status, 'stopped_semantically')
    harness.bridge.captureFailuresRemaining = 1
    harness.bridge.captureFailureCode = 'invalid_invocation'

    const first = await host.onNativeMaterialObserved(
      observation,
      rootGoalOperationContext('thread.0'),
    )
    assert.equal(first.status, 'capture_recovery_required')
    harness.bridge.captureFailuresRemaining = 1
    harness.bridge.captureFailureCode = 'mission_operation_unavailable'

    const second = await host.onNativeMaterialObserved(
      observation,
      rootGoalOperationContext('thread.0'),
    )
    assert.equal(second.status, 'capture_recovery_required')
    assert.equal(harness.store.state.captureRecoveryRequired.length, 1)
    assert.equal(
      harness.store.state.captureRecoveryRequired[0]?.ownerCode,
      'invalid_invocation',
    )
  } finally {
    await cleanup(harness)
  }
})

test('concurrent capture recovery retention durably preserves every exact observation', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  const firstMaterial = {
    materialKind: 'output' as const,
    content: 'First concurrent observation whose ordinary capture requires recovery.',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.concurrent-retain-first',
  }
  const secondMaterial = {
    materialKind: 'output' as const,
    content: 'Second concurrent observation whose ordinary capture requires recovery.',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.concurrent-retain-second',
  }
  const firstObservation = {
    observationId: nativeObservationIdForTest(firstMaterial),
    ...firstMaterial,
  }
  const secondObservation = {
    observationId: nativeObservationIdForTest(secondMaterial),
    ...secondMaterial,
  }
  try {
    assert.equal((await host.run()).status, 'stopped_semantically')
    harness.bridge.captureFailuresRemaining = 2
    harness.store.deferSaves = true

    const firstRecovery = host.onNativeMaterialObserved(
      firstObservation,
      rootGoalOperationContext('thread.0'),
    )
    const secondRecovery = host.onNativeMaterialObserved(
      secondObservation,
      rootGoalOperationContext('thread.0'),
    )

    await harness.store.waitForDeferredSaveCount(1)
    await new Promise<void>((resolve) => setImmediate(resolve))
    harness.store.resolveLastDeferredSave()
    await harness.store.waitForDeferredSaveCount(1)
    harness.store.resolveLastDeferredSave()

    assert.deepEqual(
      (await Promise.all([firstRecovery, secondRecovery])).map(({ status }) => status),
      ['capture_recovery_required', 'capture_recovery_required'],
    )
    assert.deepEqual(
      harness.store.state.captureRecoveryRequired
        .map(({ observationId }) => observationId)
        .sort(),
      [firstObservation.observationId, secondObservation.observationId].sort(),
    )
  } finally {
    await cleanup(harness)
  }
})

test('a failed concurrent capture recovery clear cannot erase its retained notice', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  const firstMaterial = {
    materialKind: 'output' as const,
    content: 'First retained observation with a concurrent clear attempt.',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.concurrent-clear-first',
  }
  const secondMaterial = {
    materialKind: 'output' as const,
    content: 'Second retained observation with a concurrent clear attempt.',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.concurrent-clear-second',
  }
  const firstObservation = {
    observationId: nativeObservationIdForTest(firstMaterial),
    ...firstMaterial,
  }
  const secondObservation = {
    observationId: nativeObservationIdForTest(secondMaterial),
    ...secondMaterial,
  }
  try {
    assert.equal((await host.run()).status, 'stopped_semantically')
    harness.bridge.captureFailuresRemaining = 2
    assert.equal(
      (await host.onNativeMaterialObserved(
        firstObservation,
        rootGoalOperationContext('thread.0'),
      )).status,
      'capture_recovery_required',
    )
    assert.equal(
      (await host.onNativeMaterialObserved(
        secondObservation,
        rootGoalOperationContext('thread.0'),
      )).status,
      'capture_recovery_required',
    )
    assert.equal(harness.store.state.captureRecoveryRequired.length, 2)
    harness.store.deferSaves = true

    const firstClear = host.onNativeMaterialObserved(
      firstObservation,
      rootGoalOperationContext('thread.0'),
    )
    const secondClear = host.onNativeMaterialObserved(
      secondObservation,
      rootGoalOperationContext('thread.0'),
    )

    await harness.store.waitForDeferredSaveCount(1)
    await new Promise<void>((resolve) => setImmediate(resolve))
    harness.store.rejectFirstDeferredSave()
    await harness.store.waitForDeferredSaveCount(1)
    harness.store.resolveLastDeferredSave()

    assert.deepEqual(
      (await Promise.all([firstClear, secondClear])).map(({ status }) => status),
      ['captured', 'captured'],
    )
    assert.deepEqual(
      harness.store.state.captureRecoveryRequired.map(({ observationId }) => observationId),
      [firstObservation.observationId],
    )
  } finally {
    await cleanup(harness)
  }
})

test('a failed capture recovery clear remains durable across an ordinary concurrent state save', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  const material = {
    materialKind: 'output' as const,
    content: 'One retained observation interleaved with an ordinary Host-state save.',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.concurrent-state-save',
  }
  const observation = {
    observationId: nativeObservationIdForTest(material),
    ...material,
  }
  try {
    assert.equal((await host.run()).status, 'stopped_semantically')
    harness.bridge.captureFailuresRemaining = 1
    assert.equal(
      (await host.onNativeMaterialObserved(
        observation,
        rootGoalOperationContext('thread.0'),
      )).status,
      'capture_recovery_required',
    )
    const retained = structuredClone(harness.store.state.captureRecoveryRequired)
    harness.store.deferSaves = true

    const failedClear = host.onNativeMaterialObserved(
      observation,
      rootGoalOperationContext('thread.0'),
    )
    await harness.store.waitForDeferredSaveCount(1)
    const ordinarySave = (
      host as unknown as { saveState(): Promise<void> }
    ).saveState()
    await new Promise<void>((resolve) => setImmediate(resolve))
    harness.store.rejectFirstDeferredSave()
    await harness.store.waitForDeferredSaveCount(1)
    harness.store.resolveLastDeferredSave()

    assert.equal((await failedClear).status, 'captured')
    await ordinarySave
    assert.deepEqual(
      (await harness.store.load()).captureRecoveryRequired,
      retained,
    )
  } finally {
    await cleanup(harness)
  }
})

test('capture recovery clearing failure preserves the notice without rejecting owner success', async () => {
  const harness = await createHarness([
    { kind: 'usage_limited', captureRecovery: true },
  ])
  const host = createHost(harness)
  const observation = {
    materialKind: 'assignment' as const,
    content: 'Investigate useful RH route 0',
    rootThreadId: 'thread.0',
    parentThreadId: 'thread.0',
    childThreadId: 'thread.0.child.0',
  }
  harness.bridge.captureFailuresRemaining = 1
  try {
    assert.equal((await host.run()).status, 'usage_limited')
    const retained = harness.store.state.captureRecoveryRequired[0]!
    assert.equal(retained.observationId, nativeObservationIdForTest(observation))
    harness.store.saveFailuresRemaining = 1

    const firstCaptured = await host.onNativeMaterialObserved(
      { observationId: retained.observationId, ...observation },
      rootGoalOperationContext('thread.0'),
    )
    assert.equal(firstCaptured.status, 'captured')
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [retained])

    const captured = await host.onNativeMaterialObserved(
      { observationId: retained.observationId, ...observation },
      rootGoalOperationContext('thread.0'),
    )
    assert.equal(captured.status, 'captured')
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [])

    const privateHost = host as unknown as {
      retainCaptureRecoveryRequired(
        recovery: typeof retained,
      ): Promise<void>
      clearAdoptedCaptureRecoveryRequired(request: JsonObject): Promise<void>
    }
    await privateHost.retainCaptureRecoveryRequired(retained)
    harness.store.saveFailuresRemaining = 1
    const adoptedRequest = {
      operation: 'interpret_material',
      input: {
        capture_scopes: [{
          adopted_root_material: {
            channel: 'native_assignment',
            content: observation.content,
            native_lineage: {
              material_kind: 'assignment',
              parent_thread_id: observation.parentThreadId,
              child_thread_id: observation.childThreadId,
            },
          },
        }],
      },
    }
    await privateHost.clearAdoptedCaptureRecoveryRequired(adoptedRequest)
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [retained])
    await privateHost.clearAdoptedCaptureRecoveryRequired(adoptedRequest)
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [])
  } finally {
    await cleanup(harness)
  }
})

test('malformed owner capture identity remains recovery-required rather than typed success', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const host = createHost(harness)
  try {
    assert.equal((await host.run()).status, 'stopped_semantically')
    harness.bridge.nativeMaterialIdOverride = 'not-a-capture-reference'

    const outcome = await host.onNativeMaterialObserved(
      {
        observationId: 'a'.repeat(64),
        materialKind: 'output',
        content: 'Exact material whose malformed owner response cannot establish custody.',
        rootThreadId: 'thread.0',
        parentThreadId: 'thread.0',
        childThreadId: 'thread.0.child.invalid-capture-ref',
      },
      rootGoalOperationContext('thread.0'),
    )

    assert.equal(outcome.status, 'capture_recovery_required')
    assert.equal(outcome.ownerCode, 'mission_native_capture_bridge_failure')
    assert.equal(harness.store.state.captureRecoveryRequired.length, 1)
  } finally {
    await cleanup(harness)
  }
})

test('Strategy continue at a blocked idle Goal starts a fresh successor', async () => {
  const harness = await createHarness([
    {
      kind: 'checkpoint',
      workerCount: 0,
      missionContinuation: 'continue',
      terminalGoalStatus: 'blocked',
    },
    { kind: 'semantic_stop', terminalGoalStatus: 'blocked' },
  ])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.authorizations.length, 2)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.equal(harness.bridge.failures.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('unavailable observation publication preserves Host material custody and checkpoint completion', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 1, missionContinuation: 'closeout' },
  ])
  // A real file at the publisher's directory prevents initialization without
  // changing the Host state store or mathematical owner fixture.
  const obstruction = path.join(harness.config.runtimeDir, 'observations')
  await fs.promises.writeFile(obstruction, 'fixture observation root obstruction', { flag: 'wx' })
  const observer = new MissionExecutionObserver(harness.config, { limits: { diskReserveBytes: 0 } })
  try {
    await observer.flush()
    assert.equal(observer.store.health().error_code, 'unsafe_path')

    const host = createHost(harness, null, async () => false, async () => false, observer)
    assert.equal((await host.run()).status, 'stopped_semantically')
    await observer.flush()

    assert.equal(observer.store.health().observed_sequence, 1)
    assert.equal(observer.store.health().committed_sequence, 0)
    assert.equal(observer.store.health().dropped_observations, 1)
    assert.equal(observer.store.health().error_code, 'unsafe_path')
    assert.equal(await fs.promises.readFile(obstruction, 'utf8'), 'fixture observation root obstruction')
    assert.deepEqual(harness.bridge.nativeMaterial, [
      { materialKind: 'assignment', content: 'Investigate useful RH route 0' },
      { materialKind: 'output', content: 'Mathematically useful result 0' },
    ].map((material) => {
      const observation = {
        ...material, rootThreadId: 'thread.0', parentThreadId: 'thread.0', childThreadId: 'thread.0.child.0',
      }
      return { observationId: nativeObservationIdForTest(observation), ...observation }
    }))
    assert.deepEqual(harness.bridge.nativeBindings, [
      { rootThreadId: 'thread.0', executiveEpochId: 'epoch.0' },
      { rootThreadId: 'thread.0', executiveEpochId: 'epoch.0' },
    ])
    assert.deepEqual(
      harness.boundaryFactory.boundaries[0]!.nativeMaterialCustodyOutcomes.map(({ status }) => status),
      ['captured', 'captured'],
    )
    assert.equal((harness.bridge.semantics.at(-1)?.request as JsonObject).operation, 'checkpoint')
    assert.deepEqual(harness.store.state.captureRecoveryRequired, [])
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.equal(harness.store.state.activeGoal, null)
  } finally {
    await observer.close()
    await cleanup(harness)
  }
})

test('execution observations retain the exact Host epoch after native ownership is released', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const observer = new MissionExecutionObserver(harness.config, { limits: { diskReserveBytes: 0 } })
  try {
    const host = createHost(harness, null, async () => false, async () => false, observer)
    const result = await host.run()
    assert.equal(result.status, 'stopped_semantically')
    const callbacks = harness.boundaryFactory.callbackSets[0]!
    callbacks.onExecutionObservation({
      identity: { root_thread_id: 'thread.0', thread_id: 'thread.0', parent_thread_id: null, turn_id: 'turn.thread.0', item_id: null, operation_id: null },
      source_method: 'core/worker/ownership', received_at: new Date().toISOString(), source_time: null,
      phase: 'processed', caused_by: null, kind: 'lifecycle', summary: 'Fixture lifecycle observed',
      details: { stage: 'removed' }, contents: [],
    })
    await observer.close()
    const { observations: rows } = await readRhObservationRows(harness.config.runtimeDir, observer.incarnation)
    assert.equal(rows.length, 1)
    assert.equal(rows[0]!.identity.mission_id, harness.config.missionId)
    assert.equal(rows[0]!.identity.epoch_id, 'epoch.0')
    assert.equal(rows[0]!.identity.root_thread_id, 'thread.0')
    assert.equal(harness.bridge.failures.length, 0)
  } finally {
    await observer.close()
    await cleanup(harness)
  }
})

test('Goal lifecycle diagnostics retain the exact Mission and Executive Epoch binding', async () => {
  const harness = await createHarness([
    { kind: 'semantic_stop' },
  ])
  try {
    await createHost(harness).run()
    const callbacks = harness.boundaryFactory.callbackSets[0]
    assert.ok(callbacks)
    const diagnostic: GoalEpochDiagnosticEvent = {
      sequence: 7,
      kind: 'app_server_error',
      rootThreadId: 'thread.0',
      activeTurnId: 'turn.thread.0',
      turnId: 'turn.thread.0',
      priorGoalStatus: 'active',
      newGoalStatus: 'blocked',
      transitionSource: 'app_server_notification',
      transitionCategory: 'thread/goal/updated',
      appServerEventCategory: 'error',
      errorCategory: 'app_server_turn_error',
      errorSubtype: 'responseStreamDisconnected',
      errorClass: 'TurnError',
      errorCode: 503,
      errorWillRetry: false,
      errorAssociatedWithRootTurn: true,
      sameTurnActivityAfterError: true,
      sameTurnActivityAfterBlocked: true,
      compactionEventCategory: 'context_compaction_started',
      blockedRecoveredWithoutHostIntervention: null,
      rootTurnTerminalStatus: null,
      hostTurnInterruptRequested: false,
    }
    const stderr: string[] = []
    const originalWrite = process.stderr.write
    process.stderr.write = ((chunk: unknown) => {
      stderr.push(Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk))
      return true
    }) as typeof process.stderr.write
    try {
      callbacks.onGoalEpochDiagnostic(diagnostic)
    } finally {
      process.stderr.write = originalWrite
    }

    assert.deepEqual(JSON.parse(stderr.join('').trim()), {
      status: 'info',
      system: 'workstation_control',
      component: 'rh_mission_host',
      event: 'rh_mission.goal_lifecycle_diagnostic',
      mission_id: 'mission.rh.public.1',
      executive_epoch_id: 'epoch.0',
      diagnostic_sequence: 7,
      diagnostic_kind: 'app_server_error',
      root_thread_id: 'thread.0',
      active_turn_id: 'turn.thread.0',
      turn_id: 'turn.thread.0',
      prior_goal_status: 'active',
      new_goal_status: 'blocked',
      transition_source: 'app_server_notification',
      transition_category: 'thread/goal/updated',
      app_server_event_category: 'error',
      error_category: 'app_server_turn_error',
      error_subtype: 'responseStreamDisconnected',
      error_class: 'TurnError',
      error_code: 503,
      error_will_retry: false,
      error_associated_with_root_turn: true,
      same_turn_activity_after_error: true,
      same_turn_activity_after_blocked: true,
      compaction_event_category: 'context_compaction_started',
      blocked_recovered_without_host_intervention: null,
      root_turn_terminal_status: null,
      host_turn_interrupt_requested: false,
    })
  } finally {
    await cleanup(harness)
  }
})

test('transient blocked live root continues same-turn activity to owner checkpoint without premature containment', async () => {
  const harness = await createHarness([{
    kind: 'checkpoint',
    workerCount: 0,
    missionContinuation: 'closeout',
    transientBlockedSameTurnBeforeCheckpoint: true,
  }])
  try {
    const result = await createHost(harness).run()
    const boundary = harness.boundaryFactory.boundaries[0]
    assert.ok(boundary)

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(boundary.monitorEvents, [
      {
        kind: 'goal_state',
        status: 'active',
        activeTurnId: 'turn.thread.0',
      },
      {
        kind: 'goal_state',
        status: 'blocked',
        activeTurnId: 'turn.thread.0',
      },
      {
        kind: 'same_turn_normal_activity',
        status: 'blocked',
        activeTurnId: 'turn.thread.0',
      },
    ])
    assert.deepEqual(harness.bridge.failures, [])
    assert.equal(boundary.rootToolResultPayloads[0]?.status, 'ok')
    assert.deepEqual(boundary.checkpointTerminalHandoffs, ['owner_checkpoint'])
    assert.deepEqual(boundary.stopCalls, [
      { threadId: 'thread.0', reason: 'owner_checkpoint' },
    ])
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0'])
  } finally {
    await cleanup(harness)
  }
})

test('blocked idle Goal failure stays local when unchanged Strategy continues to a successor', async () => {
  const harness = await createHarness([
    { kind: 'blocked_without_checkpoint' },
    { kind: 'semantic_stop' },
  ])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
    assert.equal(harness.allocatedWorkspaces.length, 2)
    assert.equal(harness.boundaryFactory.requests.length, 2)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['boundary_failure'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('blocked idle Goal failure stays local even under a historical Strategy pause', async () => {
  const harness = await createHarness([
    { kind: 'blocked_without_checkpoint' },
    { kind: 'semantic_stop' },
  ])
  harness.bridge.setContinuation('pause')
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['boundary_failure'],
    )
    assert.deepEqual(harness.bridge.authorizations, ['epoch.0', 'epoch.1'])
  } finally {
    await cleanup(harness)
  }
})

test('Host continues through checkpoints until one checkpoint semantically stops', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    {
      kind: 'checkpoint',
      workerCount: 1,
      missionContinuation: 'continue',
    },
    { kind: 'semantic_stop' },
  ])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.bridge.nativeMaterial.length, 2)
    assert.equal(harness.bridge.authorizations.length, 3)
    assert.deepEqual(Object.keys(harness.store.state).sort(), [
      'activeGoal',
      'captureRecoveryRequired',
      'missionId',
      'schemaVersion',
      'updatedAt',
    ])

    assert.equal(harness.boundaryFactory.requests.length, 3)
    assert.equal(harness.bridge.reconstructCalls, 0)
  } finally {
    await cleanup(harness)
  }
})

test('provider usage limit suspends the exact bound epoch without a mathematical failure event', async () => {
  const harness = await createHarness([{ kind: 'usage_limited' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'usage_limited')
    assert.deepEqual(harness.bridge.failures, [])
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.failureReason, 'usageLimited')
    assert.equal(harness.store.state.activeGoal?.threadId, 'thread.0')
    assert.equal(harness.store.state.activeGoal?.executiveEpochId, 'epoch.0')
    assert.equal(harness.boundaryFactory.boundaries[0]?.workspacePresentAtStop, true)
    assert.equal(fs.existsSync(harness.allocatedWorkspaces[0]!), true)
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'bound')
  } finally {
    await cleanup(harness)
  }
})

test('a later explicit Host start resumes the same suspended thread and owner epoch', async () => {
  const harness = await createHarness([{ kind: 'usage_limited' }, { kind: 'semantic_stop' }])
  try {
    const first = await createHost(harness).run()
    const suspended = structuredClone(harness.store.state.activeGoal)

    assert.equal(first.status, 'usage_limited')
    assert.equal(suspended?.phase, 'suspended')
    assert.equal(fs.existsSync(suspended!.workspaceRoot), true)
    const inspected = inspectMissionGoalLaunch(harness.config, await harness.bridge.hostSnapshot(),
      suspended!.workspaceRoot, harness.store.state)
    assert.equal(inspected.host_continuity.launch_mode, 'resume_suspended_goal')
    assert.deepEqual(inspected.host_continuity.resumed_goal, { executive_epoch_id: suspended!.executiveEpochId,
      root_thread_id: suspended!.threadId, suspension_reason: 'usageLimited' })
    assert.equal(inspected.request.objective, suspended!.objective)
    assert.ok(inspected.compatibility.unmeasured.includes('retained_native_thread_history'))
    assert.equal(inspected.compatibility.history_erasure_claimed, false)
    const second = await createHost(harness).run()

    assert.equal(second.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.boundaryFactory.boundaries[1]?.workspacePresentAtStop, true)
    assert.equal(fs.existsSync(suspended!.workspaceRoot), false)
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 1)
    assert.equal(harness.boundaryFactory.resumeRequests[0]?.threadId, suspended?.threadId)
    assert.deepEqual(harness.boundaryFactory.resumeRequests[0]?.request, inspected.request)
    assert.equal(harness.bridge.failures.length, 0)
    assert.equal(harness.bridge.reconstructCalls, 0)
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal(
      (reconstruction.latest_executive_epoch as any)?.executive_epoch_id,
      suspended?.executiveEpochId,
    )
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('a later Host start resumes the exact operator-suspended thread and owner epoch', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    const threadId = 'thread.operator-suspended'
    const workspaceRoot = await harness.allocateWorkspace()
    const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
    const executiveEpochId = String(authorized.executive_epoch_id)
    await harness.bridge.bindExecutiveEpoch({
      executiveEpochId,
      rootThreadId: threadId,
      workspaceRoot,
    })
    harness.store.state = {
      ...freshState(harness.config),
      activeGoal: {
        phase: 'suspended',
        threadId,
        objective: MISSION_OBJECTIVE,
        workspaceRoot,
        executiveEpochId,
        failureReason: 'operator_stop',
      },
    }

    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 1)
    assert.equal(harness.boundaryFactory.resumeRequests[0]?.threadId, threadId)
    assert.deepEqual(harness.bridge.failures, [])
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal(
      (reconstruction.latest_executive_epoch as any)?.executive_epoch_id,
      executiveEpochId,
    )
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('restart preserves pre-marker crash-window pages and later resumes the exact usage suspension', async () => {
  const harness = await createHarness([
    { kind: 'recovery', stoppedStatus: 'usageLimited' },
    { kind: 'resume_large_read' },
  ])
  try {
    const threadId = 'thread.interrupted-usage-suspension'
    const workspaceRoot = await harness.allocateWorkspace()
    const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
    const executiveEpochId = String(authorized.executive_epoch_id)
    await harness.bridge.bindExecutiveEpoch({
      executiveEpochId,
      rootThreadId: threadId,
      workspaceRoot,
    })
    harness.store.state = {
      ...freshState(harness.config),
      activeGoal: {
        phase: 'registered',
        threadId,
        objective: MISSION_OBJECTIVE,
        workspaceRoot,
        executiveEpochId,
        failureReason: null,
      },
    }
    const large = 'crash-window owner result:' + 'x'.repeat(5 * 1024 * 1024)
    const encoded = JSON.stringify({ readable_content: large })
    const pageBytes = 512 * 1024
    const handle = 'result.33333333-3333-4333-8333-333333333333'
    const delegatedHandle = 'delegated-result.44444444-4444-4444-8444-444444444444'
    const transportDirectory = path.join(harness.config.runtimeDir, '.rh-mission-owner-results')
    const pagePath = path.join(transportDirectory, `${handle}.json`)
    const delegatedPagePath = path.join(transportDirectory, `${delegatedHandle}.json`)
    await fs.promises.mkdir(transportDirectory, { recursive: true, mode: 0o700 })
    await fs.promises.writeFile(pagePath, encoded, { mode: 0o600 })
    await fs.promises.writeFile(delegatedPagePath, encoded, { mode: 0o600 })
    harness.boundaryFactory.retainedLargePage = {
      handle,
      nextOffset: pageBytes,
      content: encoded.slice(0, pageBytes),
    }

    const result = await createHost(harness).run()

    assert.equal(result.status, 'usage_limited')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, threadId)
    assert.equal(harness.store.state.activeGoal?.executiveEpochId, executiveEpochId)
    assert.deepEqual(harness.bridge.failures, [])
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    assert.equal(fs.existsSync(pagePath), true)
    assert.equal(fs.existsSync(delegatedPagePath), false)

    const resumed = await createHost(harness).run()

    assert.equal(resumed.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 1)
    assert.equal(
      ((harness.boundaryFactory.boundaries[1]?.largeReadResult as Record<string, unknown>)
        ?.readable_content),
      large,
    )
    assert.equal(fs.existsSync(pagePath), false)
  } finally {
    await cleanup(harness)
  }
})

test('resume contract failure terminally reconciles the bound root instead of leaving it unmanaged', async () => {
  const harness = await createHarness([{ kind: 'usage_limited' }, { kind: 'resume_contract_failure' }])
  try {
    assert.equal((await createHost(harness).run()).status, 'usage_limited')
    const savesBeforeResume = harness.store.saves.length

    await assert.rejects(
      createHost(harness).run(),
      /simulated resumed Goal contract failure/,
    )

    assert.equal(
      harness.store.saves.slice(savesBeforeResume).some(({ activeGoal }) =>
        activeGoal?.phase === 'registered' && activeGoal.failureReason === 'usageLimited'),
      true,
    )
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['boundary_failure'],
    )
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'failed_before_checkpoint')
  } finally {
    await cleanup(harness)
  }
})

test('a fresh usage limit while resume activates returns the same epoch to suspension', async () => {
  const harness = await createHarness([{ kind: 'usage_limited' }, { kind: 'resume_usage_limited' }])
  try {
    assert.equal((await createHost(harness).run()).status, 'usage_limited')
    const suspended = structuredClone(harness.store.state.activeGoal)

    assert.equal((await createHost(harness).run()).status, 'usage_limited')
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, suspended?.threadId)
    assert.equal(harness.store.state.activeGoal?.executiveEpochId, suspended?.executiveEpochId)
    assert.deepEqual(harness.bridge.failures, [])
    assert.equal(harness.boundaryFactory.resumeRequests.length, 1)
  } finally {
    await cleanup(harness)
  }
})

test('model contract violation retains its exact durable failure cause', async () => {
  const harness = await createHarness([
    { kind: 'model_contract_violation' },
    { kind: 'semantic_stop' },
  ])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['model_contract_violation'],
    )
  } finally {
    await cleanup(harness)
  }
})

test('Mission fence still permits the exact direct failed-before-checkpoint terminal', async () => {
  const harness = await createHarness([{ kind: 'mission_fence' }])
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(harness.bridge.fences.map(({ reason }) => reason), [
      'mission_consistency_failure',
    ])
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(
      harness.bridge.failures.map(({ reconciliation }) => reconciliation.failure_reason),
      ['mission_consistency_failure'],
    )
  } finally {
    await cleanup(harness)
  }
})

async function seedStoppedBoundEpoch(harness: Harness): Promise<{
  executiveEpochId: string
  rootThreadId: string
}> {
  const rootThreadId = 'thread.stopped-fatal-root'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  await harness.bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId, workspaceRoot })
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'registered',
      threadId: rootThreadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: 'mission_consistency_failure',
    },
  }
  return { executiveEpochId, rootThreadId }
}

test('real Python owner and Host preserve fatal history through explicit reauthorization and a fresh Astra epoch', async () => {
  const harness = await createHarness([
    { kind: 'recovery', stoppedStatus: 'paused', goalAbsent: true },
    { kind: 'usage_limited' },
  ])
  try {
    const pythonPath = resolvePythonExecutable()
    await fs.promises.rmdir(harness.config.missionWorkspaceRoot)
    initializeRealMissionWorkspace(
      pythonPath, harness.config.missionWorkspaceRoot, harness.config.projectId,
      harness.config.missionId, harness.config.releaseSha, 'gpt-5.6-sol',
    )
    const config: MissionHostConfig = {
      ...harness.config,
      pythonPath,
      missionScriptPath: path.join(SOURCE_REPO_ROOT, 'scripts', 'rh_mission.py'),
    }
    // Test setup and exact Store readback use the same real owner as the bridge.
    // Only the native Goal boundary is scripted; no provider is launched.
    const ownerFixture = (body: string[], input: JsonObject = {}): JsonObject => {
      const code = [
        'import hashlib, json, sqlite3, sys',
        'from contextlib import closing',
        'from pathlib import Path',
        'repo=Path(sys.argv[1])',
        "sys.path[:0]=[str(repo/'packages'/'research-core'),str(repo/'packages'/'research-attempt-adapter')]",
        'from research_core.mission_interface import MissionInterface',
        'from research_core.research_model import deep_thaw',
        'from research_core.workspace_schema import IdentityKind, TypedWorkspaceId',
        'interface=MissionInterface.open(sys.argv[2],sys.argv[3],sys.argv[4],canonical_repo_root=repo)',
        'store=interface._store',
        'mission_id=TypedWorkspaceId(IdentityKind.MISSION,sys.argv[4])',
        'input=json.loads(sys.argv[5])',
        ...body,
      ].join('\n')
      const completed = spawnSync(pythonPath, [
        '-c', code, SOURCE_REPO_ROOT, config.missionWorkspaceRoot,
        config.projectId, config.missionId, JSON.stringify(input),
      ], {
        cwd: SOURCE_REPO_ROOT, encoding: 'utf8', windowsHide: true,
        env: { ...process.env, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' },
      })
      assert.equal(completed.status, 0, completed.stderr)
      return JSON.parse(completed.stdout) as JsonObject
    }
    const predecessorWorkspace = await harness.allocateWorkspace()
    const predecessor = ownerFixture([
      "epoch=interface.authorize_executive_epoch_from_owner({'expected_cut':deep_thaw(interface.host_snapshot()['authorization_cut'])})['executive_epoch_id']",
      "interface.bind_executive_epoch_from_owner({'executiveEpochId':epoch,'rootThreadId':'thread.real-predecessor','workspaceRoot':input['workspace']})",
      "candidate=interface.execute_semantic_operation({'schema_version':'mathematical_research.mission_semantic_request.v1','operation':'record_candidate','input':{'candidate_id':'candidate:candidate.reauthorization-predecessor','proposal_kind':'lemma','exact_statement':'A synthetic retained predecessor has unchanged standing.','standing':{'status':'open','basis':'This provider-free fixture makes no proof claim.'}}},executive_epoch_id=epoch)",
      "assert candidate['status']=='completed', candidate",
      "result=interface.execute_semantic_operation({'schema_version':'mathematical_research.mission_semantic_request.v1','operation':'checkpoint','input':{}},executive_epoch_id=epoch)",
      "assert result['status']=='completed', result",
      'print(json.dumps(deep_thaw(store.read_continuation_checkpoint(executive_epoch_id=epoch))))',
    ], { workspace: predecessorWorkspace })
    assert.equal(typeof predecessor.checkpoint_id, 'string')
    const bridge = new PythonMissionBridge(config)
    let authorizations = 0
    let fences = 0
    let failedTerminals = 0
    const authorize = bridge.authorizeExecutiveEpoch.bind(bridge)
    bridge.authorizeExecutiveEpoch = async (expectedCut, signal) => {
      authorizations += 1
      return authorize(expectedCut, signal)
    }
    const fence = bridge.fenceMission.bind(bridge)
    bridge.fenceMission = async (context, binding, signal) => {
      fences += 1
      return fence(context, binding, signal)
    }
    const recordFailed = bridge.recordDirectFailedExecutiveEpoch.bind(bridge)
    bridge.recordDirectFailedExecutiveEpoch = async (input, signal) => {
      failedTerminals += 1
      return recordFailed(input, signal)
    }
    const rootThreadId = 'thread.real-held-revoked-root'
    const workspaceRoot = await harness.allocateWorkspace()
    const authorizationSnapshot = await bridge.hostSnapshot()
    const authorization = await bridge.authorizeExecutiveEpoch(
      authorizationSnapshot.authorization_cut as MissionAuthorizationCut,
    )
    const executiveEpochId = String(authorization.executive_epoch_id)
    await bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId, workspaceRoot })
    const semanticRequest = (operation: string, input: JsonObject = {}): JsonObject => ({
      schema_version: 'mathematical_research.mission_semantic_request.v1', operation, input,
    })
    const binding = { rootThreadId, executiveEpochId }
    const recordedContext = await bridge.executeSemanticOperation(semanticRequest('record_context', {
      context_id: 'context:context.reauthorization-history',
      subject: 'Preserved incident history', question: 'Which retained branches remain relevant?',
      material: [{ id: 'branch:branch.theta', why: 'Preserve the original opening branch.' }],
      known_omissions: ['external literature'], restricted_uses: ['No proof claim.'],
      restrictions: ['Read retained Mission history only.'], dependencies: [],
      historical_advisory: {
        assignment_mode: 'historical_opportunity_scout', search_lens: 'Review retained branches.',
        source_families: ['branches'], historical_references: [], untrusted_material_locators: [],
        known_omissions: ['external literature'], coverage_limits: ['retained Mission history'],
      },
    }), binding)
    assert.equal(recordedContext.status, 'completed')
    const childThreadId = `${rootThreadId}.history`
    const oldGrant = await bridge.issueHistoricalReadGrant({
      child_thread_id: childThreadId, assignment_mode: 'historical_opportunity_scout',
      assignment: 'Review retained branches.',
      context: { id: 'context:context.reauthorization-history', revision: 1 },
      source_families: ['branches'], raw_body_policy: 'metadata_only',
    }, { ...binding, childThreadId, parentThreadId: rootThreadId, depth: 1 })
    const childBinding = {
      ...binding, callerThreadId: childThreadId, parentThreadId: rootThreadId,
      depth: 1 as const, turnId: 'turn.real-incident-history', grantId: String(oldGrant.grant_id),
    }
    const historicalInventory = await bridge.executeDelegatedRead(
      semanticRequest('retrieve', {
        mode: 'history_inventory',
        purpose: 'Review retained branches.',
        page_size: 25,
      }), oldGrant, childBinding,
    )
    assert.equal(historicalInventory.status, 'completed')
    assert.equal((historicalInventory.result as JsonObject).mode, 'history_inventory')
    harness.store.state = {
      ...freshState(config),
      activeGoal: {
        phase: 'registered', threadId: rootThreadId, objective: MISSION_OBJECTIVE,
        workspaceRoot, executiveEpochId, failureReason: 'mission_consistency_failure',
      },
    }
    await bridge.fenceMission({
      threadId: rootThreadId, reason: 'mission_consistency_failure', containmentScope: 'mission_fence',
    }, { rootThreadId, executiveEpochId })
    const fenced = await bridge.reconstruct()
    const missionDelta = (reconstruction: JsonObject): JsonObject => {
      const deltas = (reconstruction.recovery_opening as JsonObject).owner_deltas as JsonObject[]
      return deltas.find((delta) => (delta.current_reference as JsonObject).kind === 'mission')!
    }
    const fencedMission = missionDelta(fenced)
    const summary = fencedMission.summary as JsonObject
    assert.equal(summary.lifecycle, 'held')
    assert.equal(summary.effective, false)
    assert.equal(summary.fence_reason, 'revoked')
    assert.equal(typeof summary.fenced_at, 'string')
    assert.equal((fenced.latest_executive_epoch as JsonObject).state, 'bound')

    const host = new MissionHost(config, () => bridge, harness.boundaryFactory, harness.store,
      async () => {}, harness.allocateWorkspace)
    const expected = { executiveEpochId, rootThreadId }
    const result = await host.reconcileStoppedEpoch(expected)
    assert.deepEqual(result, {
      ...expected, state: 'failed_before_checkpoint', checkpointId: null,
      failureReason: 'mission_consistency_failure',
    })
    assert.equal(harness.store.state.activeGoal, null)
    const after = await bridge.reconstruct()
    assert.deepEqual(missionDelta(after), fencedMission)
    assert.equal((after.latest_executive_epoch as JsonObject).state, 'failed_before_checkpoint')
    assert.equal(authorizations, 1)
    assert.equal(fences, 1)
    assert.equal(failedTerminals, 1)
    assert.deepEqual(harness.boundaryFactory.requests, [])
    assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
    assert.deepEqual(await host.reconcileStoppedEpoch(expected), result)
    assert.deepEqual(await bridge.reconstruct(), after)
    assert.equal(authorizations, 1)
    assert.equal(fences, 1)
    assert.equal(failedTerminals, 1)

    const snapshot = (): JsonObject => ownerFixture([
      'head=store.get_head(mission_id)',
      "chain=store.read_executive_epoch_events(executive_epoch_id=input['epoch'])",
      'with closing(sqlite3.connect(store.paths.database)) as connection:',
      ' tables=[row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type=\'table\' ORDER BY name")]',
      ' table_hashes={name:hashlib.sha256(repr(tuple(connection.execute(f\'SELECT * FROM "{name}"\'))).encode()).hexdigest() for name in tables}',
      'store.verify_integrity()',
      "print(json.dumps(deep_thaw({'mission':head.payload,'revision':head.reference.revision,'payload_sha256':head.payload_digest,'canonical':store.read_metadata()['canonical_authority_digest'],'incident':chain,'checkpoint':store.read_continuation_checkpoint(latest_mission_id=interface.mission_id),'tables':table_hashes})))",
    ], { epoch: executiveEpochId })
    const beforeGrant = snapshot()
    assert.deepEqual(beforeGrant.checkpoint, predecessor)
    const terminal = (beforeGrant.incident as JsonObject[]).at(-1)!
    assert.equal(terminal.event_kind, 'failed_before_checkpoint')
    const grantInput = {
      expected_mission_revision: beforeGrant.revision,
      expected_mission_payload_sha256: beforeGrant.payload_sha256,
      executive_epoch_id: executiveEpochId, root_thread_id: rootThreadId,
      expected_terminal_event_sha256: terminal.row_digest,
      expected_canonical_authority_digest: beforeGrant.canonical,
    }
    const grantBody = ['print(json.dumps(deep_thaw(interface.reauthorize_mission_from_owner(**input))))']
    const grantResult = ownerFixture(grantBody, grantInput)
    assert.equal(grantResult.idempotent, false)
    const reauthorized = snapshot()
    const expectedGrant = structuredClone(beforeGrant.mission as JsonObject)
    Object.assign(expectedGrant, {
      lifecycle: 'active', effective: true, fence_reason: null, fenced_at: null,
      control_revision: Number(expectedGrant.control_revision) + 1,
    })
    assert.deepEqual(reauthorized.mission, expectedGrant)
    assert.equal(reauthorized.revision, Number(beforeGrant.revision) + 1)
    const migration = ownerFixture([
      'print(json.dumps(deep_thaw(interface.migrate_model_policy_from_owner(**input))))',
    ], {
      expected_mission_revision: reauthorized.revision,
      expected_mission_payload_sha256: reauthorized.payload_sha256,
      expected_canonical_authority_digest: reauthorized.canonical,
    })
    assert.equal(migration.model, 'gpt-6-astra')
    const migrated = snapshot()
    const expectedMigration = structuredClone(expectedGrant)
    ;(expectedMigration.execution_policy as JsonObject).model = 'gpt-6-astra'
    expectedMigration.control_revision = Number(expectedMigration.control_revision) + 1
    assert.deepEqual(migrated.mission, expectedMigration)
    assert.equal(migrated.revision, Number(reauthorized.revision) + 1)
    const changedTables = Object.keys(beforeGrant.tables as JsonObject).filter((name) =>
      (beforeGrant.tables as JsonObject)[name] !== (migrated.tables as JsonObject)[name])
    assert.deepEqual(changedTables.sort(), [
      'mission_revision', 'mission_head', 'project_commit', 'transition_journal',
      'command_result', 'current_dependency_projection', 'workspace_metadata',
    ].sort())
    assert.deepEqual(migrated.incident, beforeGrant.incident)
    assert.deepEqual(migrated.checkpoint, predecessor)
    assert.equal(migrated.canonical, beforeGrant.canonical)
    assert.equal(authorizations, 1)
    assert.deepEqual(harness.boundaryFactory.requests, [])
    assert.equal(harness.store.state.activeGoal, null)

    const fresh = await host.run()
    assert.equal(fresh.status, 'usage_limited')
    const active = harness.store.state.activeGoal!
    assert.ok(active.threadId)
    assert.ok(active.executiveEpochId)
    assert.notEqual(active.executiveEpochId, executiveEpochId)
    assert.notEqual(active.threadId, rootThreadId)
    assert.notEqual(active.workspaceRoot, workspaceRoot)
    assert.equal(authorizations, 2)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
    assert.equal(inspectMissionGoalLaunch(
      config, await bridge.hostSnapshot(), active.workspaceRoot, harness.store.state,
    ).model, 'gpt-6-astra')
    const current = ownerFixture(['print(json.dumps(deep_thaw(interface.current_epoch()["entry"])))'])
    assert.equal(current.state, 'bound')
    assert.equal((current.mission_root as JsonObject).revision, migrated.revision)
    assert.deepEqual(current.predecessor_checkpoint, {
      checkpoint_id: predecessor.checkpoint_id, payload_sha256: predecessor.payload_digest,
    })
    const freshChild = `${active.threadId}.history`
    const freshBinding = { rootThreadId: active.threadId, executiveEpochId: active.executiveEpochId }
    const freshGrant = await bridge.issueHistoricalReadGrant({
      child_thread_id: freshChild, assignment_mode: 'historical_opportunity_scout',
      assignment: 'Review retained branches.',
      context: { id: 'context:context.reauthorization-history', revision: 1 },
      source_families: ['branches'], raw_body_policy: 'metadata_only',
    }, { ...freshBinding, childThreadId: freshChild, parentThreadId: active.threadId, depth: 1 })
    assert.notEqual(freshGrant.grant_id, oldGrant.grant_id)
    assert.equal((await bridge.executeDelegatedRead(semanticRequest('orient'), freshGrant, {
      ...freshBinding, callerThreadId: freshChild, parentThreadId: active.threadId,
      depth: 1, turnId: 'turn.real-successor-history', grantId: String(freshGrant.grant_id),
    })).status, 'completed')
    const historical = snapshot()
    assert.deepEqual(historical.incident, beforeGrant.incident)
    assert.deepEqual(historical.checkpoint, predecessor)
    for (const name of Object.keys(migrated.tables as JsonObject)) {
      if (/^(branch|strategy|context|session|attempt|candidate|evidence|admission|continuation_checkpoint)/.test(name)) {
        assert.equal((historical.tables as JsonObject)[name], (migrated.tables as JsonObject)[name], name)
      }
    }
    const originalRevoked = ownerFixture([
      "from research_core.workspace_schema import RevisionRef",
      "reference=RevisionRef(mission_id,input['revision'])",
      'print(json.dumps(deep_thaw(store.get_revision(reference).payload)))',
    ], { revision: beforeGrant.revision })
    assert.deepEqual(originalRevoked, beforeGrant.mission)
    const beforeReplay = snapshot()
    assert.deepEqual(ownerFixture(grantBody, grantInput), { ...grantResult, idempotent: true })
    assert.deepEqual(snapshot(), beforeReplay)
    await assert.rejects(bridge.executeSemanticOperation(semanticRequest('orient'), binding), MissionBridgeError)
    await assert.rejects(bridge.executeSemanticOperation(semanticRequest('orient'), {
      rootThreadId, executiveEpochId: active.executiveEpochId,
    }), MissionBridgeError)
    await assert.rejects(bridge.executeDelegatedRead(
      semanticRequest('orient'), oldGrant, childBinding,
    ), MissionBridgeError)
    await assert.rejects(bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId, workspaceRoot }), MissionBridgeError)
    assert.deepEqual(snapshot(), beforeReplay)
  } finally {
    await cleanup(harness)
  }
})

for (const goalAbsent of [false, true]) {
  test(`stopped reconciliation preserves fatal failure over ${goalAbsent ? 'archived null Goal' : 'containment pause'} without research start`, async () => {
    const harness = await createHarness([{ kind: 'recovery', stoppedStatus: 'paused', goalAbsent }])
    try {
      const expected = await seedStoppedBoundEpoch(harness)
      const host = createHost(harness)
      const result = await host.reconcileStoppedEpoch(expected)

      assert.deepEqual(result, {
        ...expected,
        state: 'failed_before_checkpoint',
        checkpointId: null,
        failureReason: 'mission_consistency_failure',
      })
      assert.equal(harness.store.state.activeGoal, null)
      assert.deepEqual(harness.bridge.failures, [{
        executiveEpochId: expected.executiveEpochId,
        reconciliation: { stage: 'goal_runtime', failure_reason: 'mission_consistency_failure' },
      }])
      assert.deepEqual(harness.bridge.fences, [{
        threadId: expected.rootThreadId,
        reason: 'mission_consistency_failure',
        containmentScope: 'mission_fence',
      }])
      assert.deepEqual(harness.boundaryFactory.boundaries[0]?.recoveryCalls, [{
        threadId: expected.rootThreadId,
        reason: 'dead_runner_recovery',
        recovery: { phase: 'registered', expectedObjective: MISSION_OBJECTIVE },
      }])
      assert.equal(harness.bridge.authorizations.length, 1)
      assert.equal(harness.bridge.bindings.length, 1)
      assert.deepEqual(harness.boundaryFactory.requests, [])
      assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
      assert.deepEqual(harness.bridge.semantics, [])

      const saves = harness.store.saves.length
      const reconstruction = await harness.bridge.reconstruct()
      assert.deepEqual(await createHost(harness).reconcileStoppedEpoch(expected), result)
      assert.deepEqual(await harness.bridge.reconstruct(), reconstruction)
      assert.equal(harness.store.saves.length, saves)
      assert.equal(harness.boundaryFactory.boundaries.length, 1)
      assert.equal(harness.bridge.fences.length, 1)
      assert.equal(harness.bridge.failures.length, 1)
      assert.equal(harness.bridge.authorizations.length, 1)
    } finally {
      await cleanup(harness)
    }
  })
}

for (const priorFailure of ['mission_consistency_failure', 'usageLimited', 'operator_stop']) {
  test(`stopped reconciliation gives an exact owner checkpoint precedence over retained ${priorFailure}`, async () => {
    const harness = await createHarness([{ kind: 'recovery', stoppedStatus: 'paused', goalAbsent: true }])
    try {
      const expected = await seedStoppedBoundEpoch(harness)
      harness.store.state.activeGoal!.failureReason = priorFailure
      if (priorFailure !== 'mission_consistency_failure') {
        harness.store.state.activeGoal!.phase = 'suspended'
      }
      const checkpoint = harness.bridge.seedCheckpoint(expected.rootThreadId, 'continue', expected.executiveEpochId)
      const result = await createHost(harness).reconcileStoppedEpoch(expected)

      assert.deepEqual(result, {
        ...expected,
        state: 'checkpointed',
        checkpointId: checkpoint.ownerId,
        failureReason: null,
      })
      assert.equal(harness.store.state.activeGoal, null)
      assert.deepEqual(harness.bridge.failures, [])
      assert.deepEqual(harness.bridge.fences, [])
      assert.deepEqual(harness.bridge.semantics, [])
      assert.equal(harness.bridge.authorizations.length, 1)
      assert.deepEqual(harness.boundaryFactory.requests, [])
      assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
      assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [{
        threadId: expected.rootThreadId, reason: 'owner_checkpoint',
      }])
    } finally {
      await cleanup(harness)
    }
  })
}

for (const wrongIdentity of ['executiveEpochId', 'rootThreadId', 'persistedRoot'] as const) {
  test(`stopped reconciliation rejects mismatched ${wrongIdentity} before effects`, async () => {
    const harness = await createHarness([])
    try {
      const expected = await seedStoppedBoundEpoch(harness)
      if (wrongIdentity === 'persistedRoot') {
        harness.store.state.activeGoal!.threadId = 'thread.wrong-persisted-root'
      } else {
        expected[wrongIdentity] += '.wrong'
      }
      const before = structuredClone(harness.store.state)
      const ownerBefore = await harness.bridge.reconstruct()
      await assert.rejects(createHost(harness).reconcileStoppedEpoch(expected), /exact/)
      assert.deepEqual(harness.store.state, before)
      assert.deepEqual(await harness.bridge.reconstruct(), ownerBefore)
      assert.deepEqual(harness.store.saves, [])
      assert.deepEqual(harness.bridge.fences, [])
      assert.deepEqual(harness.bridge.failures, [])
      assert.deepEqual(harness.bridge.cancellations, [])
      assert.deepEqual(harness.bridge.semantics, [])
      assert.deepEqual(harness.boundaryFactory.boundaries, [])
      assert.equal(harness.bridge.authorizations.length, 1)
    } finally {
      await cleanup(harness)
    }
  })
}

for (const failureReason of ['usageLimited', 'operator_stop']) {
  test(`stopped reconciliation preserves intentionally suspended ${failureReason} without effects`, async () => {
    const harness = await createHarness([])
    try {
      const expected = await seedStoppedBoundEpoch(harness)
      harness.store.state.activeGoal!.phase = 'suspended'
      harness.store.state.activeGoal!.failureReason = failureReason
      const before = structuredClone(harness.store.state)
      const ownerBefore = await harness.bridge.reconstruct()
      await assert.rejects(createHost(harness).reconcileStoppedEpoch(expected), /nonsuspended/)
      assert.deepEqual(harness.store.state, before)
      assert.deepEqual(await harness.bridge.reconstruct(), ownerBefore)
      assert.deepEqual(harness.store.saves, [])
      assert.deepEqual(harness.bridge.fences, [])
      assert.deepEqual(harness.bridge.failures, [])
      assert.deepEqual(harness.boundaryFactory.boundaries, [])
      assert.equal(harness.bridge.authorizations.length, 1)
    } finally {
      await cleanup(harness)
    }
  })
}

test('stopped reconciliation preserves a genuine Codex pause discovered without a retained failure', async () => {
  const harness = await createHarness([{ kind: 'recovery', stoppedStatus: 'paused' }])
  try {
    const expected = await seedStoppedBoundEpoch(harness)
    harness.store.state.activeGoal!.failureReason = null
    const before = structuredClone(harness.store.state.activeGoal)
    const ownerBefore = await harness.bridge.reconstruct()
    await assert.rejects(createHost(harness).reconcileStoppedEpoch(expected), /preserved a resumable Goal suspension/)
    assert.deepEqual(harness.store.state.activeGoal, {
      ...before, phase: 'suspended', failureReason: 'operator_stop',
    })
    assert.deepEqual(await harness.bridge.reconstruct(), ownerBefore)
    assert.deepEqual(harness.bridge.failures, [])
    assert.deepEqual(harness.bridge.fences, [])
    assert.deepEqual(harness.boundaryFactory.requests, [])
    assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [])
    assert.equal(harness.bridge.authorizations.length, 1)
  } finally {
    await cleanup(harness)
  }
})

for (const fenceFailure of ['reject', 'false_success'] as const) {
  test(`stopped reconciliation retains the exact pointer when the durable fence ${fenceFailure === 'reject' ? 'rejects' : 'is not present after local cleanup'}`, async () => {
    const harness = await createHarness([{ kind: 'recovery', stoppedStatus: 'paused', goalAbsent: true }])
    try {
      const expected = await seedStoppedBoundEpoch(harness)
      const before = structuredClone(harness.store.state.activeGoal)
      const ownerBefore = await harness.bridge.reconstruct()
      harness.bridge.fenceMission = async (context) => {
        harness.bridge.fences.push(structuredClone(context))
        if (fenceFailure === 'reject') throw new Error('injected durable fence rejection')
        return { mission_id: 'mission.rh.public.1', state: 'fenced' }
      }
      await assert.rejects(createHost(harness).reconcileStoppedEpoch(expected),
        fenceFailure === 'reject' ? /durable fence rejection/ : /required durable Mission fence/)
      assert.deepEqual(harness.store.state.activeGoal, before)
      assert.deepEqual(await harness.bridge.reconstruct(), ownerBefore)
      assert.deepEqual(harness.bridge.failures, [])
      assert.deepEqual(harness.bridge.semantics, [])
      assert.equal(harness.bridge.fences.length, 1)
      assert.equal(harness.bridge.authorizations.length, 1)
      assert.equal(harness.bridge.bindings.length, 1)
      assert.deepEqual(harness.boundaryFactory.requests, [])
      assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
      assert.equal(harness.boundaryFactory.boundaries[0]?.stopCalls.length,
        fenceFailure === 'reject' ? 0 : 1)
    } finally {
      await cleanup(harness)
    }
  })
}

test('Host delivers the retained Core fatal aggregate even after its boundary operation returns', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  try {
    const originalError = new GoalEpochMissionConsistencyError('PRIVATE_ORIGINAL_CORE_ERROR')
    const callbackError = new Error('PRIVATE_OWNER_CALLBACK_ERROR')
    const retainedFatal = new AggregateError([originalError, new AggregateError([callbackError])])
    let waits = 0
    const originalCreate = harness.boundaryFactory.create.bind(harness.boundaryFactory)
    harness.boundaryFactory.create = (workspaceRoot, callbacks) => {
      const boundary = originalCreate(workspaceRoot, callbacks)
      boundary.waitForFatalBoundaryFence = async () => {
        waits += 1
        throw retainedFatal
      }
      return boundary
    }
    await assert.rejects(createHost(harness).run(), (error) => error === retainedFatal)
    assert.equal(waits, 1)
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.deepEqual(harness.boundaryFactory.resumeRequests, [])
    const workspace = harness.allocatedWorkspaces[0]!
    assert.equal(harness.store.state.activeGoal?.workspaceRoot, workspace)
    assert.equal(fs.existsSync(workspace), true)
  } finally {
    await cleanup(harness)
  }
})

test('restart reconciles an already-archived registered root and continues to a fresh successor', async () => {
  const harness = await createHarness([
    { kind: 'recovery', goalAbsent: true },
    { kind: 'semantic_stop' },
  ])
  const threadId = 'thread.archived-registered-root'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  await harness.bridge.bindExecutiveEpoch({ executiveEpochId, rootThreadId: threadId, workspaceRoot })
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'registered',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: null,
    },
  }
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [
      {
        executiveEpochId,
        reconciliation: {
          stage: 'goal_runtime',
          failure_reason: 'dead_runner_recovery',
        },
      },
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.recoveryCalls, [
      {
        threadId,
        reason: 'dead_runner_recovery',
        recovery: {
          phase: 'registered',
          expectedObjective: MISSION_OBJECTIVE,
        },
      },
    ])
    assert.equal(harness.bridge.authorizations.length, 2)
    assert.deepEqual(harness.bridge.bindings.map(({ rootThreadId }) => rootThreadId), [
      threadId,
      'thread.1',
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[1]?.stopCalls, [
      { threadId: 'thread.1', reason: 'owner_checkpoint' },
    ])
  } finally {
    await cleanup(harness)
  }
})

test('crash recovery trusts the owner checkpoint terminal and resumes with a fresh epoch', async () => {
  const harness = await createHarness([{ kind: 'recovery' }, { kind: 'semantic_stop' }])
  try {
    const threadId = 'thread.crashed'
    const workspaceRoot = await harness.allocateWorkspace()
    harness.bridge.seedCheckpoint(threadId, 'continue')
    harness.store.state = {
      ...freshState(harness.config),
      activeGoal: {
        phase: 'suspended',
        threadId,
        objective: MISSION_OBJECTIVE,
        workspaceRoot,
        executiveEpochId: `epoch.${threadId}`,
        failureReason: null,
      },
    }

    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.bridge.authorizations.length, 1)
  } finally {
    await cleanup(harness)
  }
})

test('one explicit start reconciles a paused checkpoint crash and opens its fresh successor', async () => {
  const harness = await createHarness([{ kind: 'recovery' }, { kind: 'semantic_stop' }])
  try {
    const threadId = 'thread.archived-paused-checkpoint'
    const workspaceRoot = await harness.allocateWorkspace()
    harness.bridge.seedCheckpoint(threadId, 'pause')
    harness.store.state = {
      ...freshState(harness.config),
      activeGoal: {
        phase: 'checkpointed',
        threadId,
        objective: MISSION_OBJECTIVE,
        workspaceRoot,
        executiveEpochId: `epoch.${threadId}`,
        failureReason: null,
      },
    }

    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.requests.length, 1)
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId, reason: 'owner_checkpoint' },
    ])
    assert.deepEqual(harness.boundaryFactory.boundaries[1]?.stopCalls, [
      { threadId: 'thread.1', reason: 'owner_checkpoint' },
    ])
  } finally {
    await cleanup(harness)
  }
})

test('crash after Goal materialization but before owner bind contains the Goal and terminalizes authorization', async () => {
  const harness = await createHarness([{ kind: 'recovery' }])
  const threadId = 'thread.pending-before-bind'
  const workspaceRoot = await harness.allocateWorkspace()
  const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
  const executiveEpochId = String(authorized.executive_epoch_id)
  harness.bridge.setContinuation('closeout')
  harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
  harness.store.state = {
    ...freshState(harness.config),
    activeGoal: {
      phase: 'pending',
      threadId,
      objective: MISSION_OBJECTIVE,
      workspaceRoot,
      executiveEpochId,
      failureReason: null,
    },
  }
  try {
    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [
      {
        executiveEpochId,
        reconciliation: {
          stage: 'goal_runtime',
          failure_reason: 'dead_runner_recovery',
        },
      },
    ])
  } finally {
    await cleanup(harness)
  }
})

test('restart after a durable stop checkpoint starts no new Goal or owner epoch', async () => {
  const harness = await createHarness([])
  try {
    harness.bridge.seedCheckpoint('thread.stopped', 'closeout')
    harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)

    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.bridge.authorizations.length, 0)
  } finally {
    await cleanup(harness)
  }
})

test('restart gives a durable owner checkpoint precedence over an unfinished usage suspension', async () => {
  const harness = await createHarness([
    { kind: 'recovery', stoppedStatus: 'usageLimited' },
    { kind: 'semantic_stop' },
  ])
  try {
    const threadId = 'thread.checkpoint-usage-race'
    const workspaceRoot = await harness.allocateWorkspace()
    const terminal = harness.bridge.seedCheckpoint(threadId, 'pause')
    harness.store.state = {
      ...freshState(harness.config),
      activeGoal: {
        phase: 'suspended',
        threadId,
        objective: MISSION_OBJECTIVE,
        workspaceRoot,
        executiveEpochId: terminal.executiveEpochId,
        failureReason: 'usageLimited',
      },
    }

    const result = await createHost(harness).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(harness.store.state.activeGoal, null)
    assert.deepEqual(harness.bridge.failures, [])
    assert.equal(harness.bridge.authorizations.length, 1)
    assert.deepEqual(harness.boundaryFactory.boundaries[0]?.stopCalls, [
      { threadId, reason: 'owner_checkpoint' },
    ])
    assert.equal(harness.boundaryFactory.resumeRequests.length, 0)
    assert.deepEqual(harness.boundaryFactory.boundaries[1]?.stopCalls, [
      { threadId: 'thread.1', reason: 'owner_checkpoint' },
    ])
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'checkpointed')
  } finally {
    await cleanup(harness)
  }
})

test('restart recovers a durable Codex usage limit even when the Host marker was not saved', async () => {
  const harness = await createHarness([{ kind: 'recovery', stoppedStatus: 'usageLimited' }])
  try {
    const threadId = 'thread.usage-crash'
    const workspaceRoot = await harness.allocateWorkspace()
    const authorized = await harness.bridge.authorizeExecutiveEpoch(harness.bridge.authorizationCut())
    const executiveEpochId = String(authorized.executive_epoch_id)
    await harness.bridge.bindExecutiveEpoch({
      executiveEpochId,
      rootThreadId: threadId,
      workspaceRoot,
    })
    harness.store.state = {
      ...freshState(harness.config),
      activeGoal: {
        phase: 'registered',
        threadId,
        objective: MISSION_OBJECTIVE,
        workspaceRoot,
        executiveEpochId,
        failureReason: null,
      },
    }

    const result = await createHost(harness).run()

    assert.equal(result.status, 'usage_limited')
    assert.equal(harness.boundaryFactory.requests.length, 0)
    assert.equal(harness.store.state.activeGoal?.phase, 'suspended')
    assert.equal(harness.store.state.activeGoal?.threadId, threadId)
    assert.equal(harness.store.state.activeGoal?.executiveEpochId, executiveEpochId)
    assert.equal(harness.store.state.activeGoal?.failureReason, 'usageLimited')
    assert.deepEqual(harness.bridge.failures, [])
    const reconstruction = await harness.bridge.reconstruct()
    assert.equal((reconstruction.latest_executive_epoch as any)?.state, 'bound')
  } finally {
    await cleanup(harness)
  }
})

test('JSON state store rejects unsupported state schemas', async () => {
  const harness = await createHarness([])
  try {
    await fs.promises.writeFile(
      harness.config.statePath,
      JSON.stringify({
        schemaVersion: 'workstation_control.rh_mission_host_state.v2',
        missionId: harness.config.missionId,
      }),
    )
    const store = new JsonMissionHostStateStore(harness.config)
    await assert.rejects(store.load(), /lean current schema/)
  } finally {
    await cleanup(harness)
  }
})

test('JSON state store preserves an operator-suspended Goal for exact-thread resume', async () => {
  const harness = await createHarness([])
  try {
    const workspaceRoot = await harness.allocateWorkspace()
    const state: MissionHostState = {
      ...freshState(harness.config),
      activeGoal: {
        phase: 'suspended',
        threadId: 'thread.operator-suspended-json',
        objective: MISSION_OBJECTIVE,
        workspaceRoot,
        executiveEpochId: 'epoch.operator-suspended-json',
        failureReason: 'operator_stop',
      },
    }
    const store = new JsonMissionHostStateStore(harness.config)

    await store.save(state)
    const loaded = await store.load()

    assert.deepEqual(loaded.activeGoal, state.activeGoal)
  } finally {
    await cleanup(harness)
  }
})

test('JSON state store upgrades prior lean v4, v5, and v6 states without inventing custody data', async () => {
  for (const schemaVersion of [
    'workstation_control.rh_mission_host_state.v4',
    'workstation_control.rh_mission_host_state.v5',
    'workstation_control.rh_mission_host_state.v6',
  ]) {
    const harness = await createHarness([])
    try {
      await fs.promises.writeFile(
        harness.config.statePath,
        JSON.stringify({
          schemaVersion,
          missionId: harness.config.missionId,
          activeGoal: null,
          updatedAt: new Date(0).toISOString(),
        }),
      )
      const loaded = await new JsonMissionHostStateStore(harness.config).load()

      assert.equal(loaded.schemaVersion, HOST_STATE_SCHEMA_VERSION)
      assert.equal(loaded.activeGoal, null)
      assert.deepEqual(loaded.captureRecoveryRequired, [])
      assert.deepEqual(Object.keys(loaded).sort(), [
        'activeGoal',
        'captureRecoveryRequired',
        'missionId',
        'schemaVersion',
        'updatedAt',
      ])
    } finally {
      await cleanup(harness)
    }
  }
})

test('JSON state store rejects a checkpoint claim under a pre-checkpoint Host schema', async () => {
  for (const schemaVersion of [
    'workstation_control.rh_mission_host_state.v4',
    'workstation_control.rh_mission_host_state.v5',
  ]) {
    const harness = await createHarness([])
    try {
      const workspaceRoot = await harness.allocateWorkspace()
      await fs.promises.writeFile(
        harness.config.statePath,
        JSON.stringify({
          schemaVersion,
          missionId: harness.config.missionId,
          activeGoal: {
            phase: 'checkpointed',
            threadId: 'thread.false-legacy-checkpoint',
            objective: MISSION_OBJECTIVE,
            workspaceRoot,
            executiveEpochId: 'epoch.false-legacy-checkpoint',
            failureReason: null,
          },
          updatedAt: new Date(0).toISOString(),
        }),
      )

      await assert.rejects(
        new JsonMissionHostStateStore(harness.config).load(),
        /legacy RH Mission Host state claims a phase its writer did not support/,
      )
    } finally {
      await cleanup(harness)
    }
  }
})

test('JSON state store upgrades a v6 checkpoint and preserves current capture recovery', async () => {
  const harness = await createHarness([])
  try {
    const workspaceRoot = await harness.allocateWorkspace()
    await fs.promises.writeFile(
      harness.config.statePath,
      JSON.stringify({
        schemaVersion: 'workstation_control.rh_mission_host_state.v6',
        missionId: harness.config.missionId,
        activeGoal: {
          phase: 'checkpointed',
          threadId: 'thread.v6-checkpoint',
          objective: MISSION_OBJECTIVE,
          workspaceRoot,
          executiveEpochId: 'epoch.v6-checkpoint',
          failureReason: null,
        },
        updatedAt: new Date(0).toISOString(),
      }),
    )
    const store = new JsonMissionHostStateStore(harness.config)
    const upgraded = await store.load()
    assert.equal(upgraded.schemaVersion, HOST_STATE_SCHEMA_VERSION)
    assert.equal(upgraded.activeGoal?.phase, 'checkpointed')
    assert.deepEqual(upgraded.captureRecoveryRequired, [])

    const recovery = {
      status: 'capture_recovery_required' as const,
      observationId: 'observation.restart-safe',
      ownerCode: 'invalid_invocation',
      executiveEpochId: 'epoch.v6-checkpoint',
      nativeLineage: {
        materialKind: 'output' as const,
        rootThreadId: 'thread.v6-checkpoint',
        parentThreadId: 'thread.v6-checkpoint',
        childThreadId: 'thread.v6-checkpoint.child.0',
      },
      plaintextReference: {
        kind: 'codex_native_material' as const,
        observationId: 'observation.restart-safe',
        rootThreadId: 'thread.v6-checkpoint',
        parentThreadId: 'thread.v6-checkpoint',
        childThreadId: 'thread.v6-checkpoint.child.0',
      },
      recovery: {
        operation: 'interpret_material' as const,
        inputRoute: 'capture_scopes[].adopted_root_material' as const,
        channel: 'native_output' as const,
      },
    }
    await store.save({ ...upgraded, captureRecoveryRequired: [recovery] })
    assert.deepEqual((await store.load()).captureRecoveryRequired, [recovery])
  } finally {
    await cleanup(harness)
  }
})

test('OPEN Candidate A1 reconstruction accepts the exact proof-neutral owner projection', async () => {
  const harness = await createHarness([])
  const notifications = new RecordingNotificationSink()
  harness.bridge.seedCheckpoint('thread.a1-reconstruction', 'closeout')
  harness.bridge.admittedResult = structuredClone(ADMITTED_RESULT)
  harness.bridge.openCandidateA1 = [{
    candidate_ref: {
      kind: 'candidate',
      identity: 'complete-rh-reconstructed',
      revision: 2,
      payload_sha256: '9'.repeat(64),
    },
    retrieval_handle: 'candidate:complete-rh-reconstructed@2',
    classification: 'purported_complete_rh_proof_or_disproof',
    disposition: 'disproof',
    hold_lifecycle: 'open',
    canonical_effect: 'none',
    mathematical_effect: 'none',
  }]
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'stopped_semantically')
    const candidateNotifications = notifications.notifications.filter(
      ({ title }) => title === 'RH Candidate A1 requires review',
    )
    assert.equal(candidateNotifications.length, 1)
    assert.match(candidateNotifications[0]?.summary ?? '', /complete-rh-reconstructed@2/)
    assert.match(candidateNotifications[0]?.summary ?? '', /disposition=disproof/)
    assert.match(candidateNotifications[0]?.summary ?? '', new RegExp(`digest=${'9'.repeat(64)}`))
  } finally {
    await cleanup(harness)
  }
})

test('distributed synthetic Recognition composes A plus B plus existing C into one OPEN Candidate A1', async () => {
  const harness = await createHarness([{ kind: 'candidate_a1' }, { kind: 'semantic_stop' }])
  const notifications = new RecordingNotificationSink()
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'stopped_semantically')
    const semanticRequests = harness.bridge.semantics.map(({ request }) => request as JsonObject)
    const knownExactReadIndex = semanticRequests.findIndex(({ operation, input }) =>
      operation === 'retrieve' &&
      isRecordForTest(input) &&
      Array.isArray(input.ids) &&
      input.ids.includes('evidence:synthetic-conditional-rh@7'))
    const candidateRecordIndex = semanticRequests.findIndex(({ operation }) =>
      operation === 'record_candidate')
    assert.ok(knownExactReadIndex >= 0 && knownExactReadIndex < candidateRecordIndex)
    assert.equal(harness.bridge.historicalGrantRequests.length, 0)
    assert.equal(harness.bridge.delegatedReads.length, 0)
    const candidateNotifications = notifications.notifications.filter(
      ({ title }) => title === 'RH Candidate A1 requires review',
    )
    assert.equal(candidateNotifications.length, 1)
    assert.equal(candidateNotifications[0]?.reason, 'critical')
    const summary = candidateNotifications[0]?.summary ?? ''
    assert.match(summary, /synthetic-distributed-rh@1/)
    assert.match(summary, new RegExp(`digest=${'e'.repeat(64)}`))
    assert.match(summary, /disposition=proof/)
    assert.match(summary, /retrieval_handle=candidate:synthetic-distributed-rh@1/)
    assert.match(summary, /contains no proof bytes/)
    assert.doesNotMatch(summary, /Synthetic fixture only/)
    assert.match(
      candidateNotifications[0]?.operationId ?? '',
      /^rh-mission:candidate-a1:/,
    )
    assert.match(candidateNotifications[0]?.operationId ?? '', new RegExp('e{64}'))
    const candidateRequest = harness.bridge.semantics.find(
      ({ request }) => isRecordForTest(request) && request.operation === 'record_candidate',
    )?.request as JsonObject
    const candidateInput = candidateRequest.input as JsonObject
    assert.deepEqual(candidateInput.complete_target_claim, {
      target: 'riemann_hypothesis',
      disposition: 'proof',
    })
    assert.deepEqual(candidateInput.supporting_refs, [
      {
        kind: 'evidence',
        id: 'evidence:synthetic-proof-p',
        revision: 1,
        digest_sha256: 'a'.repeat(64),
      },
      {
        kind: 'evidence',
        id: 'evidence:synthetic-proof-q',
        revision: 1,
        digest_sha256: 'b'.repeat(64),
      },
      {
        kind: 'evidence',
        id: 'evidence:synthetic-conditional-rh',
        revision: 7,
        digest_sha256: 'c'.repeat(64),
      },
    ])
    assert.deepEqual(
      harness.bridge.nativeMaterial.map((observation) => ({
        materialKind: (observation as JsonObject).materialKind,
        childThreadId: (observation as JsonObject).childThreadId,
      })),
      [
        { materialKind: 'output', childThreadId: 'thread.0.child.synthetic-p' },
        { materialKind: 'output', childThreadId: 'thread.0.child.synthetic-q' },
      ],
    )
    const interpretationInputs = harness.bridge.semantics
      .map(({ request }) => request as JsonObject)
      .filter(({ operation }) => operation === 'interpret_material')
      .map(({ input }) => input as JsonObject)
    assert.deepEqual(
      interpretationInputs.map(({ evidence_id }) => evidence_id),
      ['evidence:synthetic-proof-p', 'evidence:synthetic-proof-q'],
    )
    assert.equal(
      interpretationInputs.some(({ evidence_id }) =>
        evidence_id === 'evidence:synthetic-conditional-rh'),
      false,
    )
    const synthesisInput = (
      harness.bridge.semantics.find(({ request }) =>
        isRecordForTest(request) && request.operation === 'synthesize')
        ?.request as JsonObject
    ).input as JsonObject
    assert.deepEqual(synthesisInput.inputs, [
      { id: 'evidence:synthetic-proof-p', role: 'purported premise P', revision: 1 },
      { id: 'evidence:synthetic-proof-q', role: 'purported premise Q', revision: 1 },
      { id: 'evidence:synthetic-conditional-rh', role: 'pre-existing conditional P and Q implies RH', revision: 7 },
    ])
    const strategyInput = (
      harness.bridge.semantics.find(({ request }) =>
        isRecordForTest(request) &&
        request.operation === 'record_strategy' &&
        isRecordForTest(request.input) &&
        Array.isArray(request.input.causal_inputs))
        ?.request as JsonObject
    ).input as JsonObject
    assert.deepEqual(strategyInput.causal_inputs, [{
      source: { id: 'candidate:synthetic-distributed-rh', revision: 1 },
      decision_consequence: 'Pivot the affected work from broad exploration to focused verification of this exact Candidate.',
    }])
    assert.equal(harness.bridge.openCandidateA1[0]?.canonical_effect, 'none')
    assert.equal(harness.bridge.openCandidateA1[0]?.mathematical_effect, 'none')
    assert.deepEqual(
      harness.bridge.semantics
        .map(({ request }) => request as JsonObject)
        .filter(({ operation }) => operation === 'checkpoint')
        .map(({ input }) => input),
      [{}, {}],
    )
  } finally {
    await cleanup(harness)
  }
})

test('malformed notification-only Candidate projections cannot rewrite the committed tool or Mission result', async () => {
  const harness = await createHarness([{ kind: 'candidate_a1' }, { kind: 'semantic_stop' }])
  const notifications = new RecordingNotificationSink()
  harness.bridge.malformedCandidateNotificationProjection = true
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(
      harness.bridge.semantics.some(({ request }) =>
        isRecordForTest(request) && request.operation === 'record_candidate'),
      true,
    )
    const candidateNotices = notifications.notifications.filter(
      ({ title }) => title === 'RH Candidate A1 requires review',
    )
    assert.equal(candidateNotices.length, 1)
    assert.match(candidateNotices[0]!.summary, /candidate:synthetic-distributed-rh@1/)
    assert.doesNotMatch(candidateNotices[0]!.summary, /malformed-notification-only/)
  } finally {
    await cleanup(harness)
  }
})

test('historical Strategy pause is reopened without a generic owner-attention stop', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  harness.bridge.setContinuation('pause')
  const notifications = new RecordingNotificationSink()
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(
      notifications.notifications.map(({ reason, title }) => ({ reason, title })),
      [{ reason: 'completed', title: 'RH Mission requested completion' }],
    )
    assert.equal(harness.bridge.authorizations.length, 1)
  } finally {
    await cleanup(harness)
  }
})

test('healthy successor checkpoint does not notify before requested Mission completion', async () => {
  const harness = await createHarness([
    { kind: 'checkpoint', workerCount: 0, missionContinuation: 'continue' },
    { kind: 'semantic_stop' },
  ])
  const notifications = new RecordingNotificationSink()
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.deepEqual(
      notifications.notifications.map(({ reason, title }) => ({ reason, title })),
      [{ reason: 'completed', title: 'RH Mission requested completion' }],
    )
  } finally {
    await cleanup(harness)
  }
})

test('outbox admission failure never changes Mission lifecycle truth', async () => {
  const harness = await createHarness([{ kind: 'semantic_stop' }])
  const notifications = new FailingNotificationSink()
  const stderr: string[] = []
  const originalWrite = process.stderr.write
  process.stderr.write = ((chunk: unknown) => {
    stderr.push(Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk))
    return true
  }) as typeof process.stderr.write
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(notifications.notifications.length, 1)
    assert.equal(notifications.notifications[0]?.reason, 'completed')
    const output = stderr.join('')
    assert.doesNotMatch(output, /SECRET_BODY|SecretBearingCustomErrorName/)
    assert.match(output, /"error_type":"Error"/)
  } finally {
    process.stderr.write = originalWrite
    await cleanup(harness)
  }
})

test('provider usage suspension emits the locked critical alert without abandoning the retained Goal', async () => {
  const harness = await createHarness([{ kind: 'usage_limited' }])
  const notifications = new RecordingNotificationSink()
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'usage_limited')
    assert.equal(notifications.notifications.length, 1)
    assert.equal(notifications.notifications[0]?.reason, 'critical')
    assert.equal(notifications.notifications[0]?.title, 'RH Mission usage suspended')
    assert.match(notifications.notifications[0]?.summary ?? '', /without abandoning/)
  } finally {
    await cleanup(harness)
  }
})

test('ineffective Mission owner state is surfaced as an abnormal closeout', async () => {
  const harness = await createHarness([])
  const notifications = new RecordingNotificationSink()
  harness.bridge.setMissionState('held', false)
  try {
    const result = await createHost(harness, notifications).run()

    assert.equal(result.status, 'stopped_semantically')
    assert.equal(notifications.notifications.length, 1)
    assert.equal(notifications.notifications[0]?.reason, 'critical')
    assert.equal(notifications.notifications[0]?.title, 'RH Mission stopped abnormally')
    assert.match(notifications.notifications[0]?.summary ?? '', /held and ineffective/)
  } finally {
    await cleanup(harness)
  }
})

test('Mission Host runtime failure is surfaced without exposing a failure body', async () => {
  const harness = await createHarness([{ kind: 'recovery' }])
  const notifications = new RecordingNotificationSink()
  harness.bridge.failBindingResponseAfterCommit = true
  try {
    await assert.rejects(
      createHost(harness, notifications).run(),
      /simulated lost binding response/,
    )
    assert.equal(notifications.notifications.length, 1)
    assert.equal(notifications.notifications[0]?.reason, 'critical')
    assert.equal(notifications.notifications[0]?.title, 'RH Mission Host failed')
    assert.doesNotMatch(
      notifications.notifications[0]?.summary ?? '',
      /simulated lost binding response/,
    )
  } finally {
    await cleanup(harness)
  }
})
