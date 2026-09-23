import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import path from 'node:path'
import readline from 'node:readline'

import type {
  CodexRawThreadReadResult,
  CodexRawThreadSummary,
  CreateThreadResponse,
  SubmitPromptResponse,
} from '../../codex-remote-core/dist/index.js'

import { createLogger } from './logger.js'
import {
  classifyCollaborationPrompt,
  decodeNativeAgentMessage,
  nativeObservationThreadId,
  projectNativeObservation,
  type CoreObservationDraft,
  type CoreObservationScope,
  type DecodedNativeAgentMessage,
} from './native-observation.js'
import type {
  RhObservationDetails,
  RhObservationPosition,
} from '../../codex-remote-core/dist/rh-observation.js'

export type CodexExecutionObservation = CoreObservationDraft & {
  phase: 'received' | 'processed' | 'rejected'
  caused_by: RhObservationPosition | null
  /** Actual handshake evidence for Host source provenance, never an expectation. */
  source_facts?: { codex_version: string }
}

type RuntimeObservationContext = {
  rawAgentMessage: DecodedNativeAgentMessage | null
  scope: CoreObservationScope
  message: unknown
  receipt: RhObservationPosition | null
  projected: boolean
  rejected: boolean
  identity: CoreObservationDraft['identity'] | null
}

export type GoalFailureDiagnostic = RhObservationDetails['error']

type PendingRequest = {
  method: string
  observation?: { identity: CoreObservationDraft['identity']; receipt: RhObservationPosition | null }
  resolve: (value: any) => void
  reject: (error: Error) => void
  cleanup: () => void
}

export interface CodexBoundary {
  start(signal?: AbortSignal): Promise<void>
  stop(): Promise<void>
  isReady(): boolean
  getReadyReason(): string | null
  sanityCheck(signal?: AbortSignal): Promise<void>
  listThreads(): Promise<CodexRawThreadSummary[]>
  readThread(threadId: string): Promise<CodexRawThreadReadResult>
  resumeThread(threadId: string): Promise<void>
  createThread(prompt: string): Promise<CreateThreadResponse>
  submitPrompt(threadId: string, prompt: string): Promise<SubmitPromptResponse>
  subscribe(listener: (message: any) => void): () => void
}

export type ThreadGoalStatus =
  | 'active'
  | 'paused'
  | 'blocked'
  | 'usageLimited'
  | 'complete'

export type ThreadGoal = {
  threadId: string
  objective: string
  status: ThreadGoalStatus
  tokensUsed: number
  timeUsedSeconds: number
  createdAt: number
  updatedAt: number
}

export type GoalEpochDiagnosticTransitionSource =
  | 'app_server_notification'
  | 'host_issued_mutation'
  | 'readback'

export type GoalEpochDiagnosticErrorSubtype =
  | 'contextWindowExceeded'
  | 'sessionBudgetExceeded'
  | 'usageLimitExceeded'
  | 'serverOverloaded'
  | 'cyberPolicy'
  | 'misalignmentPolicyViolation'
  | 'httpConnectionFailed'
  | 'responseStreamConnectionFailed'
  | 'internalServerError'
  | 'unauthorized'
  | 'badRequest'
  | 'threadRollbackFailed'
  | 'sandboxError'
  | 'responseStreamDisconnected'
  | 'responseTooManyFailedAttempts'
  | 'activeTurnNotSteerableReview'
  | 'activeTurnNotSteerableCompact'
  | 'other'
  | 'unclassified'

export type GoalEpochDiagnosticEventKind =
  | 'goal_status_transition'
  | 'app_server_error'
  | 'same_turn_activity_after_transition'
  | 'context_management'
  | 'root_turn_terminal'
  | 'host_turn_interrupt_requested'

export type GoalEpochDiagnosticEvent = Readonly<{
  sequence: number
  kind: GoalEpochDiagnosticEventKind
  rootThreadId: string
  activeTurnId: string | null
  turnId: string | null
  priorGoalStatus: ThreadGoalStatus | null
  newGoalStatus: ThreadGoalStatus | null
  transitionSource: GoalEpochDiagnosticTransitionSource | null
  transitionCategory: 'thread/goal/updated' | 'thread/goal/set' | 'thread/goal/get' | null
  appServerEventCategory: string | null
  errorCategory: 'app_server_turn_error' | null
  errorSubtype: GoalEpochDiagnosticErrorSubtype | null
  errorClass: 'TurnError' | null
  errorCode: number | null
  errorWillRetry: boolean | null
  errorAssociatedWithRootTurn: boolean | null
  sameTurnActivityAfterError: boolean | null
  sameTurnActivityAfterBlocked: boolean | null
  compactionEventCategory:
    | 'context_compaction_started'
    | 'context_compaction_completed'
    | 'thread_compacted'
    | null
  blockedRecoveredWithoutHostIntervention: boolean | null
  rootTurnTerminalStatus: 'completed' | 'failed' | 'interrupted' | null
  hostTurnInterruptRequested: boolean
}>

/** Process-wide policy inherited by the root Goal and its native children. */
export type GoalProcessConfig = {
  permissionProfileId: string
  expectedCliVersion: string
  expectedModelProvider: 'openai'
  expectedModel: string
  reasoningEffort: string
  modelCatalogPath: string
  appServerCwd: string
  protectedRoot: string
  readOnlyRoots?: readonly string[]
  /** Exact runtime files needed inside the sandbox; never grants their parents. */
  readOnlyFiles?: readonly string[]
  /** Defaults to true. False confines model tools to local material and output. */
  networkAccess?: boolean
}

export type GoalEpochInstructionHierarchy = Readonly<{
  authorityRoot: string
  targetPath: string
  instructionFileName?: string
}>

export type GoalEpochEnvironmentSelection = Readonly<{
  environmentId: string
  cwd: string
}>

export type GoalEpochCapabilityRootSelection = Readonly<{
  kind: 'skill' | 'plugin'
  id: string
  location: Readonly<{
    type: 'environment'
    environmentId: string
    path: string
  }>
}>

export type GoalEpochToolAllowlistSelection = Readonly<{
  id: string
  enabledTools: readonly string[]
}>

export type GoalEpochBrowserSelection = Readonly<{
  mode: 'isolated_ephemeral_unauthenticated'
}>

export type GoalEpochCapabilitySelections = Readonly<{
  localRoots: readonly GoalEpochCapabilityRootSelection[]
  mcpServers: readonly GoalEpochToolAllowlistSelection[]
  apps: readonly GoalEpochToolAllowlistSelection[]
  browser: GoalEpochBrowserSelection | null
}>

export type GoalEpochNativeAgentRole = Readonly<{
  name: string
  description: string
  /** Release-owned, instruction-only generated TOML within an authorized read-only root. */
  configFile: string
}>

export type BoundedGoalEpochRequest = {
  objective: string
  developerInstructions: string
  instructionHierarchy?: GoalEpochInstructionHierarchy
  nativeAgentRoles?: readonly GoalEpochNativeAgentRole[]
  /** Root history data, appended before activation; never developer instructions. */
  initialContextText?: string
  dynamicTools: readonly DynamicToolSpec[]
  environments?: readonly GoalEpochEnvironmentSelection[]
  selectedCapabilities?: GoalEpochCapabilitySelections
}

export type BoundedGoalEpochIdentity = {
  threadId: string
}

export type DynamicToolSpec = {
  type: 'function'
  name: string
  description: string
  inputSchema: unknown
  deferLoading?: boolean
}

export type DynamicToolCall = {
  threadId: string
  turnId: string
  callId: string
  tool: string
  namespace: string | null
  arguments: unknown
}

export type DynamicToolCallResult = {
  success: boolean
  contentItems: readonly { type: 'inputText'; text: string }[]
  /** Internal lifecycle intent consumed by Core and never sent over App Server JSON-RPC. */
  terminalHandoff?: 'owner_checkpoint'
}

export type GoalEpochCallerStatus = 'root' | GoalEpochDescendantStatus

export type GoalEpochOperationContext = Readonly<{
  signal: AbortSignal
  rootThreadId: string
  callerThreadId: string
  parentThreadId: string | null
  depth: number
  turnId: string | null
  status: GoalEpochCallerStatus
  grantId: string | null
  assignmentId: string | null
}>

export class GoalEpochCancelledError extends Error {
  constructor(
    readonly phase: string,
    readonly reason: unknown,
  ) {
    super(`bounded Goal operation was explicitly cancelled during ${phase}`)
    this.name = 'GoalEpochCancelledError'
  }
}

export class GoalEpochStartEffectUnknownError extends Error {
  constructor(readonly reason: unknown, options?: ErrorOptions) {
    super('thread/start ended without one reconcilable root thread identity after cancellation', options)
    this.name = 'GoalEpochStartEffectUnknownError'
  }
}

class GoalEpochInitialContextAppendError extends Error {
  constructor(cause: unknown) {
    super(
      cause instanceof Error ? cause.message : 'initial Goal context append was not acknowledged',
      { cause },
    )
    this.name = 'GoalEpochInitialContextAppendError'
  }
}

export class GoalEpochMissionConsistencyError extends Error {
  constructor(message: string, options?: ErrorOptions, readonly diagnostic?: GoalFailureDiagnostic) {
    super(message, options)
    this.name = 'GoalEpochMissionConsistencyError'
  }
}

export class GoalEpochSharedAuthorityLossError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
    this.name = 'GoalEpochSharedAuthorityLossError'
  }
}

export type GoalEpochDescendantStatus =
  | 'pending'
  | 'active'
  | 'complete'
  | 'interrupted'
  | 'failed'
  | 'archived'

export type GoalEpochDescendantState = {
  threadId: string
  parentThreadId: string
  activeTurnId: string | null
  status: GoalEpochDescendantStatus
}

export type GoalEpochDescendantToolGrantInput = Readonly<{
  rootThreadId: string
  childThreadId: string
  grantId: string
  assignmentId: string
  allowedToolNames: readonly string[]
}>

export type GoalEpochDirectChildToolGrantTargetInput = Readonly<{
  rootThreadId: string
  childThreadId: string
}>

export type GoalEpochDirectChildToolGrantTarget = Readonly<{
  rootThreadId: string
  childThreadId: string
  parentThreadId: string
  depth: number
  status: 'pending' | 'active'
  activeTurnId: string | null
}>

export type GoalEpochDescendantToolGrantBinding = Readonly<{
  rootThreadId: string
  childThreadId: string
  parentThreadId: string
  depth: number
  status: 'pending' | 'active'
  activeTurnId: string | null
  grantId: string
  assignmentId: string
  allowedToolNames: readonly string[]
}>

export type GoalEpochDescendantToolGrantRevocationReason =
  | 'child_turn_completed'
  | 'child_turn_failed'
  | 'child_turn_interrupted'
  | 'child_archived'
  | 'root_checkpoint'
  | 'goal_suspended'
  | 'goal_stopped'
  | 'goal_finalized'
  | 'boundary_process_exit'
  | 'runtime_removed'

export type GoalEpochDescendantToolGrantRevocation = Readonly<{
  rootThreadId: string
  childThreadId: string
  parentThreadId: string
  depth: number
  grantId: string
  assignmentId: string
  reason: GoalEpochDescendantToolGrantRevocationReason
}>

export type GoalEpochNativeMaterialObservation = Readonly<{
  /** Opaque custody idempotency key; not an attestation or completion witness. */
  observationId: string
  materialKind: 'assignment' | 'output'
  content: string
  rootThreadId: string
  parentThreadId: string
  childThreadId: string
}>

export type GoalEpochNativeMaterialCustodyOutcome =
  | Readonly<{
      status: 'captured'
      observationId: string
      captureRef: Readonly<{
        materialId: string
        revision: number
      }>
    }>
  | Readonly<{
      status: 'capture_recovery_required'
      observationId: string
      ownerCode: string
      executiveEpochId: string
      nativeLineage: Readonly<{
        materialKind: 'assignment' | 'output'
        rootThreadId: string
        parentThreadId: string
        childThreadId: string
      }>
      plaintextReference: Readonly<{
        kind: 'codex_native_material'
        observationId: string
        rootThreadId: string
        parentThreadId: string
        childThreadId: string
      }>
      recovery: Readonly<{
        operation: 'interpret_material'
        inputRoute: 'capture_scopes[].adopted_root_material'
        channel: 'native_assignment' | 'native_output'
      }>
    }>

export type GoalEpochState = {
  goal: ThreadGoal | null
  threadStatus: unknown
  activeTurnId: string | null
  descendants: readonly GoalEpochDescendantState[]
  nativeMaterialObservations: readonly GoalEpochNativeMaterialObservation[]
}

export type GoalEpochStopReason =
  | 'owner_checkpoint'
  | 'usage_limited'
  | 'operator_stop'
  | 'explicit_force_stop'
  | 'boundary_shutdown'
  | 'turn_start_failure'
  | 'model_contract_violation'
  | 'boundary_failure'
  | 'shared_authority_loss'
  | 'unknown_effect'
  | 'mission_consistency_failure'
  | 'dead_runner_recovery'

export type GoalEpochSuspensionReason = 'usage_limited' | 'operator_stop'

export type GoalEpochContainmentScope = 'goal_local' | 'mission_fence'

export type GoalEpochStopContext = {
  threadId: string
  turnId: string | null
  reason: GoalEpochStopReason
  containmentScope: GoalEpochContainmentScope
}

export type GoalEpochStopResult = {
  threadId: string
  goal: ThreadGoal | null
  interruptedTurnId: string | null
  goalCleared: boolean
  descendantsContained: number
  appServerTerminated: boolean
}

export type DeadRunnerRecoveryRequest = Readonly<{
  phase: 'pending' | 'registered'
  expectedObjective: string
}>

export type GoalEpochMaterializedIdentity = Readonly<{
  threadId: string
  workspaceRoot: string
}>

export interface CodexGoalEpochBoundary {
  findMaterializedGoalEpoch(signal?: AbortSignal): Promise<BoundedGoalEpochIdentity | null>
  startBoundedGoalEpoch(
    request: BoundedGoalEpochRequest,
    signal?: AbortSignal,
  ): Promise<BoundedGoalEpochIdentity>
  resumeBoundedGoalEpoch(
    threadId: string,
    request: BoundedGoalEpochRequest,
    signal?: AbortSignal,
  ): Promise<BoundedGoalEpochIdentity>
  getThreadGoal(threadId: string): Promise<ThreadGoal | null>
  readGoalEpochState(threadId: string, signal?: AbortSignal): Promise<GoalEpochState>
  pauseAndInterruptGoalEpoch(threadId: string): Promise<GoalEpochStopResult>
  suspendBoundedGoalEpoch(
    threadId: string,
    recovery?: DeadRunnerRecoveryRequest,
    reason?: GoalEpochSuspensionReason,
  ): Promise<GoalEpochStopResult>
  stopBoundedGoalEpoch(
    threadId: string,
    reason: GoalEpochStopReason,
    recovery?: DeadRunnerRecoveryRequest,
  ): Promise<GoalEpochStopResult>
  finalizeCompletedGoalEpoch(threadId: string): Promise<GoalEpochState>
  waitForFatalBoundaryFence(): Promise<void>
  clearThreadGoal(threadId: string): Promise<boolean>
  resolveDirectChildToolGrantTarget(
    input: GoalEpochDirectChildToolGrantTargetInput,
  ): GoalEpochDirectChildToolGrantTarget
  installDescendantToolGrant(
    input: GoalEpochDescendantToolGrantInput,
  ): GoalEpochDescendantToolGrantBinding
}

export type CodexAppServerBoundaryOptions = {
  restartOnUnexpectedExit?: boolean
  goalPolicy?: GoalProcessConfig
  dynamicToolHandler?: (
    call: DynamicToolCall,
    context: GoalEpochOperationContext,
  ) => Promise<DynamicToolCallResult>
  onGoalTermination?: (context: GoalEpochStopContext) => Promise<void>
  onMissionFence?: (context: GoalEpochStopContext) => Promise<void>
  onNativeMaterialObserved?: (
    observation: GoalEpochNativeMaterialObservation,
    context: GoalEpochOperationContext,
  ) => Promise<GoalEpochNativeMaterialCustodyOutcome | void>
  onGoalEpochIdentityMaterialized?: (identity: GoalEpochMaterializedIdentity) => Promise<void>
  onGoalEpochDiagnostic?: (event: GoalEpochDiagnosticEvent) => void
  /** Observer admission is synchronous and best effort; it never owns research effects. */
  onExecutionObservation?: (event: CodexExecutionObservation) => RhObservationPosition | void
  onDescendantToolGrantRevoked?: (
    event: GoalEpochDescendantToolGrantRevocation,
  ) => Promise<void>
  /** Exhaustive ids of protected App-Server MCP definitions selectable by a Mission. */
  trustedMcpServerIds?: readonly string[]
  /** Exhaustive ids of protected connector definitions selectable by a Mission. */
  trustedAppIds?: readonly string[]
}

type ResolvedInstructionSource = Readonly<{ path: string; content: string }>

type ValidatedBoundedGoalEpochRequest = {
  objective: string
  developerInstructions: string
  nativeAgentRoles: readonly GoalEpochNativeAgentRole[]
  initialContextText: string | null
  readOnlyRoots: readonly string[]
  permissionProfileId: string
  expectedModelProvider: 'openai'
  expectedModel: string
  reasoningEffort: string
  networkAccess: boolean
  dynamicTools: readonly DynamicToolSpec[]
  environments: readonly GoalEpochEnvironmentSelection[]
  selectedCapabilities: GoalEpochCapabilitySelections
  trustedMcpServerIds: readonly string[]
  trustedAppIds: readonly string[]
  workspaceRoot: string
}

type DescendantRuntime = {
  threadId: string
  parentThreadId: string
  admittedForMaterial: boolean
  agentPath: string | null
  depth: number
  activeTurnId: string | null
  status: GoalEpochDescendantStatus
  toolGrant: DescendantToolGrantRuntime | null
  /** First granted family survives revocation for this registered child's lifetime. */
  toolGrantFamily: DescendantToolGrantFamily | null
  /** Undefined until native thread-start source metadata establishes a role or its absence. */
  nativeAgentRole: string | null | undefined
}

type DescendantToolGrantRuntime = {
  grantId: string
  assignmentId: string
  family: DescendantToolGrantFamily
  allowedToolNames: ReadonlySet<string>
  controller: AbortController
}

type PendingNativeAgentMessage = Readonly<{
  decoded: DecodedNativeAgentMessage
  receiverThreadId: string
  observation: RuntimeObservationContext | null
}>

type GoalRuntime = {
  threadId: string
  workspaceRoot: string
  expectedObjective: string
  expectedModelProvider: 'openai'
  expectedModel: string
  reasoningEffort: string
  goal: ThreadGoal | null
  threadStatus: unknown
  activeTurnId: string | null
  toolNames: ReadonlySet<string>
  acceptingTools: boolean
  stopping: boolean
  dynamicOperationController: AbortController
  materialOperationController: AbortController
  stopPromise: Promise<GoalEpochStopResult> | null
  transitionKind: 'suspending' | 'stopping' | null
  firstTurn: Promise<string>
  resolveFirstTurn: (turnId: string) => void
  rejectFirstTurn: (error: Error) => void
  descendants: Map<string, DescendantRuntime>
  pendingAgentMessages: PendingNativeAgentMessage[]
  materials: Map<string, GoalEpochNativeMaterialObservation>
  pendingMaterials: Map<string, Promise<GoalEpochNativeMaterialCustodyOutcome | void>>
  diagnostic: {
    sequence: number
    blockedTurnId: string | null
    errorTurnId: string | null
    sameTurnActivityAfterBlockedEmitted: boolean
    sameTurnActivityAfterErrorEmitted: boolean
    hostInterventionAfterBlocked: boolean
    pendingReadbackTransition: {
      priorStatus: ThreadGoalStatus | null
      newStatus: ThreadGoalStatus
    } | null
  }
}

type GoalEpochDiagnosticEventInput = Readonly<
  Pick<GoalEpochDiagnosticEvent, 'kind'> &
    Partial<Omit<GoalEpochDiagnosticEvent, 'sequence' | 'kind' | 'rootThreadId' | 'activeTurnId'>>
>

type CachedDynamicToolCall = {
  fingerprint: string
  result: Promise<DynamicToolCallResult>
}

type ThreadBinding = {
  rootThreadId: string
  runtime: GoalRuntime
  descendant: DescendantRuntime | null
}

type DirectChildThreadBinding = ThreadBinding & {
  descendant: DescendantRuntime & { status: 'pending' | 'active' }
}

type CodexCliInvocation = {
  executable: string
  prefixArgs: readonly string[]
  shell: boolean
}

const REMOTE_CONTROL_DISABLED_ENV_VAR = 'CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED'
const DYNAMIC_TOOL_NAME = /^[A-Za-z0-9_-]+$/
const DESCENDANT_HISTORY_TOOL_NAMES = [
  'rh_mission_history',
  'rh_mission_history_page',
] as const
const DESCENDANT_CANDIDATE_A1_REVIEW_TOOL_NAMES = [
  'rh_mission_a1_review',
  'rh_mission_a1_review_page',
] as const
const DESCENDANT_RESEARCH_READ_TOOL_NAMES = [
  'rh_mission_research_read',
  'rh_mission_research_read_page',
] as const
const DESCENDANT_ADMISSION_TOOL_NAMES = [
  'rh_mission_admission',
  'rh_mission_admission_page',
] as const
const DESCENDANT_TOOL_FAMILIES = [
  {
    family: 'historical_read',
    toolNames: DESCENDANT_HISTORY_TOOL_NAMES,
  },
  {
    family: 'candidate_a1_review',
    toolNames: DESCENDANT_CANDIDATE_A1_REVIEW_TOOL_NAMES,
  },
  {
    family: 'research_read',
    toolNames: DESCENDANT_RESEARCH_READ_TOOL_NAMES,
  },
  {
    family: 'admission',
    toolNames: DESCENDANT_ADMISSION_TOOL_NAMES,
  },
] as const
type DescendantToolGrantFamily = (typeof DESCENDANT_TOOL_FAMILIES)[number]['family']
const DESCENDANT_TOOL_FAMILY_ROLES: Record<DescendantToolGrantFamily, string> = {
  historical_read: 'rh_historical',
  candidate_a1_review: 'rh_restricted_review',
  research_read: 'rh_researcher',
  admission: 'rh_restricted_review',
}
const DESCENDANT_TOOL_FAMILY_BY_NAME = new Map<string, DescendantToolGrantFamily>(
  DESCENDANT_TOOL_FAMILIES.flatMap(({ family, toolNames }) =>
    toolNames.map((name) => [name, family] as const),
  ),
)
const DESCENDANT_TOOL_NAME_SET = new Set<string>(DESCENDANT_TOOL_FAMILY_BY_NAME.keys())

function normalizeDescendantToolGrantSelection(
  input: readonly string[],
): Readonly<{ family: DescendantToolGrantFamily; allowedToolNames: readonly string[] }> {
  const invalidSelection = (): never => {
    throw new Error(
      'descendant tool grant must select a duplicate-free historical read-tool subset or Candidate A1 review-tool subset or research read-tool subset or Admission tool subset',
    )
  }
  if (input.length === 0 || new Set(input).size !== input.length) {
    return invalidSelection()
  }
  const families = input.map((name) => DESCENDANT_TOOL_FAMILY_BY_NAME.get(name) ?? null)
  const family = families[0]
  if (!family || families.some((candidate) => candidate !== family)) {
    return invalidSelection()
  }
  const definition = DESCENDANT_TOOL_FAMILIES.find((candidate) => candidate.family === family)
  if (!definition) {
    return invalidSelection()
  }
  return {
    family,
    allowedToolNames: definition.toolNames.filter((name) => input.includes(name)),
  }
}

function descendantToolGrantLabel(family: DescendantToolGrantFamily): string {
  const labels: Record<DescendantToolGrantFamily, string> = {
    historical_read: 'historical read',
    candidate_a1_review: 'Candidate A1 review',
    research_read: 'research read',
    admission: 'Admission',
  }
  return labels[family]
}

const PERMISSION_PROFILE_ID = /^[A-Za-z0-9_.-]+$/
const THREAD_SOURCE_KINDS = [
  'cli',
  'vscode',
  'exec',
  'appServer',
  'subAgent',
  'subAgentReview',
  'subAgentCompact',
  'subAgentThreadSpawn',
  'subAgentOther',
  'unknown',
] as const

function cancellationError(signal: AbortSignal, phase: string): GoalEpochCancelledError {
  return new GoalEpochCancelledError(phase, signal.reason)
}

function isExplicitForceStop(signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true && signal.reason === 'explicit_force_stop'
}

function throwIfCancelled(signal: AbortSignal | undefined, phase: string): void {
  if (signal?.aborted) {
    throw cancellationError(signal, phase)
  }
}

function requireNonEmpty(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(field + ' must be a non-empty string')
  }
  return value.trim()
}

function requireClosedObject(
  value: unknown,
  keys: readonly string[],
  field: string,
): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(field + ' must be one object')
  }
  const record = value as Record<string, unknown>
  const actual = Object.keys(record).sort()
  const expected = [...keys].sort()
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new Error(field + ' has the wrong closed shape')
  }
  return record
}

function requireExactIdentifier(value: unknown, field: string): string {
  if (
    typeof value !== 'string' ||
    !value ||
    value.trim() !== value ||
    [...value].some((character) => {
      const codePoint = character.codePointAt(0) ?? 0
      return codePoint < 32 || codePoint === 127
    })
  ) {
    throw new Error(field + ' must be one exact trimmed identifier without control characters')
  }
  return value
}

function normalizeRoots(values: readonly string[] | undefined, field: string): string[] {
  if (values === undefined) {
    return []
  }
  if (!Array.isArray(values)) {
    throw new Error(field + ' must be an array')
  }
  const roots = values.map((value) => {
    const candidate = requireNonEmpty(value, field)
    if (!path.isAbsolute(candidate)) {
      throw new Error(field + ' must contain only absolute paths')
    }
    return path.resolve(candidate)
  })
  const keys = roots.map((value) => (process.platform === 'win32' ? value.toLowerCase() : value))
  if (new Set(keys).size !== keys.length) {
    throw new Error(field + ' must not contain duplicate roots')
  }
  return roots.sort()
}

function pathIsWithin(root: string, candidate: string): boolean {
  const relative = path.relative(path.resolve(root), path.resolve(candidate))
  return relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative))
}

function resolveInstructionHierarchy(
  hierarchy: GoalEpochInstructionHierarchy | undefined,
  readOnlyRoots: readonly string[],
): ResolvedInstructionSource[] {
  if (!hierarchy) {
    return []
  }
  const authorityRoot = path.resolve(requireNonEmpty(hierarchy.authorityRoot, 'instructionHierarchy.authorityRoot'))
  const targetPath = path.resolve(requireNonEmpty(hierarchy.targetPath, 'instructionHierarchy.targetPath'))
  const fileName = hierarchy.instructionFileName ?? 'AGENTS.md'
  if (path.basename(fileName) !== fileName || fileName === '.' || fileName === '..') {
    throw new Error('instructionHierarchy.instructionFileName must be one file name')
  }
  if (!pathIsWithin(authorityRoot, targetPath)) {
    throw new Error('instructionHierarchy.targetPath must remain inside authorityRoot')
  }
  if (!readOnlyRoots.some((root) => pathIsWithin(root, authorityRoot))) {
    throw new Error('instructionHierarchy.authorityRoot must be covered by readOnlyRoots')
  }
  const directories: string[] = []
  let current = targetPath
  while (true) {
    directories.push(current)
    if (path.resolve(current) === authorityRoot) {
      break
    }
    current = path.dirname(current)
  }
  directories.reverse()
  const sources: ResolvedInstructionSource[] = []
  for (const directory of directories) {
    const sourcePath = path.join(directory, fileName)
    if (!fs.existsSync(sourcePath)) {
      continue
    }
    const stat = fs.lstatSync(sourcePath)
    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw new Error('instruction source must be one regular non-symlink file: ' + sourcePath)
    }
    sources.push({ path: sourcePath, content: fs.readFileSync(sourcePath, 'utf8') })
  }
  return sources
}

function renderInstructions(
  developerInstructions: string,
  instructionSources: readonly ResolvedInstructionSource[],
): string {
  if (instructionSources.length === 0) {
    return developerInstructions
  }
  const inherited = instructionSources
    .map((source) => 'Instruction source: ' + source.path + '\n\n' + source.content)
    .join('\n\n')
  return developerInstructions + '\n\nInherited repository instructions:\n\n' + inherited
}

/** The same resolved instruction text is used for inspection and native delivery. */
export function resolveGoalEpochInstructions(
  request: Pick<BoundedGoalEpochRequest, 'developerInstructions' | 'instructionHierarchy'>,
  readOnlyRoots: readonly string[],
): Readonly<{ developerInstructions: string; instructionSources: readonly ResolvedInstructionSource[] }> {
  const developerInstructions = requireNonEmpty(request.developerInstructions, 'developerInstructions')
  const instructionSources = resolveInstructionHierarchy(request.instructionHierarchy, readOnlyRoots)
  return {
    developerInstructions: renderInstructions(developerInstructions, instructionSources),
    instructionSources,
  }
}

function normalizeDynamicTools(values: readonly DynamicToolSpec[]): DynamicToolSpec[] {
  if (!Array.isArray(values)) {
    throw new Error('dynamicTools must be an array')
  }
  const normalized = values.map((value) => {
    if (!value || value.type !== 'function' || !DYNAMIC_TOOL_NAME.test(value.name)) {
      throw new Error('dynamicTools contains an invalid function tool')
    }
    if (!value.inputSchema || typeof value.inputSchema !== 'object' || Array.isArray(value.inputSchema)) {
      throw new Error('dynamicTools.' + value.name + '.inputSchema must be an object schema')
    }
    return {
      type: 'function' as const,
      name: value.name,
      description: requireNonEmpty(value.description, 'dynamicTools.' + value.name + '.description'),
      inputSchema: value.inputSchema,
      ...(value.deferLoading === undefined ? {} : { deferLoading: value.deferLoading }),
    }
  })
  if (new Set(normalized.map((value) => value.name)).size !== normalized.length) {
    throw new Error('dynamicTools must not contain duplicate names')
  }
  return normalized
}

function existingNonSymlinkDirectory(value: unknown, field: string): string {
  const candidate = requireNonEmpty(value, field)
  if (!path.isAbsolute(candidate)) {
    throw new Error(field + ' must be an absolute local path')
  }
  let stat: fs.Stats
  try {
    stat = fs.lstatSync(candidate)
  } catch (error) {
    throw new Error(field + ' must be one existing local directory', { cause: error })
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(field + ' must be one existing non-symlink local directory')
  }
  return fs.realpathSync(candidate)
}

function rejectSymlinksBelow(root: string, field: string): void {
  const pending = [root]
  while (pending.length > 0) {
    const directory = pending.pop()!
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const child = path.join(directory, entry.name)
      if (entry.isSymbolicLink()) {
        throw new Error(field + ' must not contain symlinks: ' + child)
      }
      if (entry.isDirectory()) {
        pending.push(child)
      }
    }
  }
}

function canonicalExistingRoots(values: readonly string[], field: string): string[] {
  return values.map((value, index) =>
    existingNonSymlinkDirectory(value, `${field}[${index}]`),
  )
}

function canonicalProtectedRoot(value: string): string {
  try {
    return fs.realpathSync(value)
  } catch {
    return path.resolve(value)
  }
}

const LOCAL_ENVIRONMENT_ID = 'local'

function normalizeEnvironmentSelections(
  values: readonly GoalEpochEnvironmentSelection[] | undefined,
  writableRoot: string,
): GoalEpochEnvironmentSelection[] {
  const supplied = values ?? [{ environmentId: LOCAL_ENVIRONMENT_ID, cwd: writableRoot }]
  if (!Array.isArray(supplied)) {
    throw new Error('environments must be an array')
  }
  if (supplied.length !== 1) {
    throw new Error('bounded Goal environments must select exactly one local environment')
  }
  const [value] = supplied
  const environmentId = requireNonEmpty(value?.environmentId, 'environments.environmentId')
  if (environmentId !== LOCAL_ENVIRONMENT_ID) {
    throw new Error('bounded Goal environments must use the local environment')
  }
  const cwdInput = requireNonEmpty(value?.cwd, 'environments[0].cwd')
  if (!path.isAbsolute(cwdInput)) {
    throw new Error('environments[0].cwd must be an absolute local path')
  }
  const cwd = path.resolve(cwdInput)
  const canonicalWritableRoot = path.resolve(writableRoot)
  if (
    !pathIsWithin(canonicalWritableRoot, cwd) ||
    !pathIsWithin(cwd, canonicalWritableRoot)
  ) {
    throw new Error('bounded Goal local environment cwd must equal its writable Goal workspace')
  }
  return [{ environmentId: LOCAL_ENVIRONMENT_ID, cwd: canonicalWritableRoot }]
}

function normalizeCapabilityRootSelections(
  values: readonly GoalEpochCapabilityRootSelection[],
  environments: readonly GoalEpochEnvironmentSelection[],
  readOnlyRoots: readonly string[],
  writableRoot: string,
  protectedRoot: string,
): GoalEpochCapabilityRootSelection[] {
  if (!Array.isArray(values)) {
    throw new Error('selectedCapabilities.localRoots must be an array')
  }
  const environmentById = new Map(
    environments.map((environment) => [environment.environmentId, environment]),
  )
  const canonicalReadOnlyRoots = canonicalExistingRoots(
    readOnlyRoots,
    'goalPolicy.readOnlyRoots',
  )
  const canonicalProtected = canonicalProtectedRoot(protectedRoot)
  const normalized = values.map((value, index) => {
    const root = requireClosedObject(
      value,
      ['kind', 'id', 'location'],
      `selectedCapabilities.localRoots[${index}]`,
    )
    if (root.kind !== 'skill' && root.kind !== 'plugin') {
      throw new Error('selectedCapabilities.localRoots.kind must be skill or plugin')
    }
    const kind: 'skill' | 'plugin' = root.kind
    const location = requireClosedObject(
      root.location,
      ['type', 'environmentId', 'path'],
      `selectedCapabilities.localRoots[${index}].location`,
    )
    if (location.type !== 'environment') {
      throw new Error('selectedCapabilities.localRoots must use an environment location')
    }
    const environmentId = requireNonEmpty(
      location.environmentId,
      'selectedCapabilities.localRoots.location.environmentId',
    )
    const environment = environmentById.get(environmentId)
    if (!environment) {
      throw new Error('selectedCapabilities.localRoots must reference a supplied environment')
    }
    if (environmentId !== LOCAL_ENVIRONMENT_ID) {
      throw new Error('selectedCapabilities.localRoots currently support only the local environment')
    }
    const selectedRootInput = requireNonEmpty(
      location.path,
      `selectedCapabilities.localRoots[${index}].location.path`,
    )
    if (!path.isAbsolute(selectedRootInput)) {
      throw new Error(`selectedCapabilities.localRoots[${index}].location.path must be an absolute local path`)
    }
    const lexicalSelectedRoot = path.resolve(selectedRootInput)
    const lexicalWritableRoot = path.resolve(writableRoot)
    const lexicalProtectedRoot = path.resolve(protectedRoot)
    if (
      pathIsWithin(lexicalWritableRoot, lexicalSelectedRoot) ||
      pathIsWithin(lexicalSelectedRoot, lexicalWritableRoot)
    ) {
      throw new Error('selectedCapabilities.localRoots must not overlap the writable Goal root')
    }
    if (
      pathIsWithin(lexicalProtectedRoot, lexicalSelectedRoot) ||
      pathIsWithin(lexicalSelectedRoot, lexicalProtectedRoot)
    ) {
      throw new Error('selectedCapabilities.localRoots must not overlap the protected Goal root')
    }
    const selectedRoot = existingNonSymlinkDirectory(
      selectedRootInput,
      `selectedCapabilities.localRoots[${index}].location.path`,
    )
    if (!canonicalReadOnlyRoots.some((root) => pathIsWithin(root, selectedRoot))) {
      throw new Error(
        'selectedCapabilities.localRoots must stay inside one authorized read-only Goal root',
      )
    }
    const canonicalWritableRoot = canonicalProtectedRoot(writableRoot)
    if (
      pathIsWithin(canonicalWritableRoot, selectedRoot) ||
      pathIsWithin(selectedRoot, canonicalWritableRoot)
    ) {
      throw new Error('selectedCapabilities.localRoots must not overlap the writable Goal root')
    }
    if (
      pathIsWithin(canonicalProtected, selectedRoot) ||
      pathIsWithin(selectedRoot, canonicalProtected)
    ) {
      throw new Error('selectedCapabilities.localRoots must not overlap the protected Goal root')
    }
    rejectSymlinksBelow(
      selectedRoot,
      `selectedCapabilities.localRoots[${index}].location.path`,
    )
    return {
      kind,
      id: requireExactIdentifier(root.id, 'selectedCapabilities.localRoots.id'),
      location: {
        type: 'environment' as const,
        environmentId,
        path: selectedRoot,
      },
    }
  })
  if (new Set(normalized.map((value) => value.id)).size !== normalized.length) {
    throw new Error('selectedCapabilities.localRoots must not contain duplicate ids')
  }
  const selectedPathKeys = normalized.map((value) =>
    process.platform === 'win32'
      ? value.location.path.toLowerCase()
      : value.location.path,
  )
  if (new Set(selectedPathKeys).size !== normalized.length) {
    throw new Error('selectedCapabilities.localRoots must not contain duplicate paths')
  }
  return normalized
}

function normalizeToolAllowlistSelections(
  values: readonly GoalEpochToolAllowlistSelection[],
  field: string,
): GoalEpochToolAllowlistSelection[] {
  if (!Array.isArray(values)) {
    throw new Error(field + ' must be an array')
  }
  const normalized = values.map((value, index) => {
    const location = `${field}[${index}]`
    const record = requireClosedObject(value, ['id', 'enabledTools'], location)
    const id = requireExactIdentifier(record.id, `${location}.id`)
    if (!Array.isArray(record.enabledTools)) {
      throw new Error(`${location}.enabledTools must be an array`)
    }
    const enabledTools = record.enabledTools.map((tool, toolIndex) =>
      requireExactIdentifier(tool, `${location}.enabledTools[${toolIndex}]`),
    )
    if (enabledTools.length === 0) {
      throw new Error(`${location}.enabledTools must name at least one exact tool`)
    }
    if (new Set(enabledTools).size !== enabledTools.length) {
      throw new Error(`${location}.enabledTools must not contain duplicate tools`)
    }
    return { id, enabledTools }
  })
  if (new Set(normalized.map((value) => value.id)).size !== normalized.length) {
    throw new Error(field + ' must not contain duplicate ids')
  }
  return normalized
}

function normalizeTrustedCapabilityIds(
  values: readonly string[] | undefined,
  field: 'trustedMcpServerIds' | 'trustedAppIds',
): string[] {
  if (values === undefined) {
    return []
  }
  if (!Array.isArray(values)) {
    throw new Error(field + ' must be an array')
  }
  const normalized = values.map((value, index) =>
    requireExactIdentifier(value, `${field}[${index}]`),
  )
  if (new Set(normalized).size !== normalized.length) {
    throw new Error(field + ' must not contain duplicate ids')
  }
  return normalized
}

function normalizeCapabilitySelections(
  value: GoalEpochCapabilitySelections | undefined,
  environments: readonly GoalEpochEnvironmentSelection[],
  readOnlyRoots: readonly string[],
  writableRoot: string,
  protectedRoot: string,
  trustedMcpServerIds: readonly string[],
  trustedAppIds: readonly string[],
): GoalEpochCapabilitySelections {
  const supplied = value ?? {
    localRoots: [],
    mcpServers: [],
    apps: [],
    browser: null,
  }
  const selection = requireClosedObject(
    supplied,
    ['localRoots', 'mcpServers', 'apps', 'browser'],
    'selectedCapabilities',
  )
  const localRoots = normalizeCapabilityRootSelections(
    selection.localRoots as readonly GoalEpochCapabilityRootSelection[],
    environments,
    readOnlyRoots,
    writableRoot,
    protectedRoot,
  )
  const mcpServers = normalizeToolAllowlistSelections(
    selection.mcpServers as readonly GoalEpochToolAllowlistSelection[],
    'selectedCapabilities.mcpServers',
  )
  const trusted = new Set(trustedMcpServerIds)
  const untrusted = mcpServers.find((server) => !trusted.has(server.id))
  if (untrusted) {
    throw new Error(
      `selectedCapabilities.mcpServers contains untrusted preconfigured server id ${untrusted.id}`,
    )
  }
  const apps = normalizeToolAllowlistSelections(
    selection.apps as readonly GoalEpochToolAllowlistSelection[],
    'selectedCapabilities.apps',
  )
  const trustedApps = new Set(trustedAppIds)
  const untrustedApp = apps.find((app) => !trustedApps.has(app.id))
  if (untrustedApp) {
    throw new Error(
      `selectedCapabilities.apps contains untrusted connector id ${untrustedApp.id}`,
    )
  }
  const browserRecord = selection.browser === null
    ? null
    : requireClosedObject(selection.browser, ['mode'], 'selectedCapabilities.browser')
  if (
    browserRecord !== null &&
    browserRecord.mode !== 'isolated_ephemeral_unauthenticated'
  ) {
    throw new Error(
      'selectedCapabilities.browser must select only the isolated ephemeral unauthenticated browser',
    )
  }
  return {
    localRoots,
    mcpServers,
    apps,
    browser: browserRecord === null
      ? null
      : { mode: 'isolated_ephemeral_unauthenticated' },
  }
}

function goalProcessFeatureConfig(networkAccess = true): Record<string, boolean> {
  return {
    browser_use_full_cdp_access: false,
    goals: true,
    in_app_browser: false,
    multi_agent: networkAccess,
    plugin_sharing: false,
    plugins: false,
    remote_plugin: false,
    remote_models: false,
    rollout_budget: false,
    shell_tool: true,
    skill_mcp_dependency_install: false,
    token_budget: false,
    // Provider-hosted tools and ambient hooks are not confined by shell network policy.
    ...(!networkAccess ? {
      apps: false,
      browser_use: false,
      browser_use_external: false,
      computer_use: false,
      enable_mcp_apps: false,
      hooks: false,
      image_generation: false,
      memories: false,
      standalone_web_search: false,
    } : {}),
  }
}

function goalThreadFeatureConfig(
  selected: GoalEpochCapabilitySelections,
  networkAccess: boolean,
): Record<string, boolean> {
  const hasApps = selected.apps.length > 0
  const hasBrowser = selected.browser !== null
  const hasPlugins = selected.localRoots.some((root) => root.kind === 'plugin')
  return {
    ...goalProcessFeatureConfig(networkAccess),
    apps: hasApps,
    browser_use: hasBrowser,
    browser_use_external: hasBrowser,
    computer_use: hasBrowser,
    enable_mcp_apps: hasApps,
    plugins: hasPlugins,
  }
}

function quotedTomlKeySegment(value: string): string {
  return JSON.stringify(value)
}

function goalCapabilityThreadConfig(
  selected: GoalEpochCapabilitySelections,
  trustedMcpServerIds: readonly string[],
  trustedAppIds: readonly string[],
): Record<string, unknown> {
  const config: Record<string, unknown> = {
    'orchestrator.skills.enabled': selected.localRoots.length > 0,
    'orchestrator.mcp.enabled': selected.mcpServers.length > 0,
    'apps._default.enabled': false,
    'apps._default.destructive_enabled': false,
    'apps._default.open_world_enabled': false,
  }
  const selectedMcpById = new Map(
    selected.mcpServers.map((server) => [server.id, server]),
  )
  for (const id of trustedMcpServerIds) {
    const key = `mcp_servers.${quotedTomlKeySegment(id)}`
    const selection = selectedMcpById.get(id)
    config[`${key}.enabled`] = selection !== undefined
    config[`${key}.enabled_tools`] = selection ? [...selection.enabledTools] : []
  }
  const selectedAppById = new Map(selected.apps.map((app) => [app.id, app]))
  for (const id of trustedAppIds) {
    const key = `apps.${quotedTomlKeySegment(id)}`
    const app = selectedAppById.get(id)
    config[`${key}.enabled`] = app !== undefined
    config[`${key}.default_tools_enabled`] = false
    config[`${key}.default_tools_approval_mode`] = 'auto'
    config[`${key}.destructive_enabled`] = false
    config[`${key}.open_world_enabled`] = false
    for (const tool of app?.enabledTools ?? []) {
      const toolKey = `${key}.tools.${quotedTomlKeySegment(tool)}`
      config[`${toolKey}.enabled`] = true
      config[`${toolKey}.approval_mode`] = 'auto'
    }
  }
  return config
}

// Codex 0.144.1 has no null/unbounded representation for its V1 thread capacity.
// The pinned amd64 runtime accepts signed TOML integers, so its representational
// maximum removes the product's default six-thread ceiling without allocating
// workers in advance. Provider and finite host capacity remain the real bounds.
const CODEX_V1_THREAD_CAPACITY_TOML = '9223372036854775807'
const GOAL_DELEGATION_MAX_DEPTH = 2

function normalizeNativeAgentRoles(
  values: readonly GoalEpochNativeAgentRole[] | undefined,
  readOnlyRoots: readonly string[],
): GoalEpochNativeAgentRole[] {
  if (values === undefined) return []
  if (!Array.isArray(values)) throw new Error('nativeAgentRoles must be an array')
  if (values.length === 0) return []
  const roots = canonicalExistingRoots(readOnlyRoots, 'goalPolicy.readOnlyRoots')
  const normalized = values.map((value, index) => {
    const field = `nativeAgentRoles[${index}]`
    const record = requireClosedObject(value, ['name', 'description', 'configFile'], field)
    const name = requireExactIdentifier(record.name, field + '.name')
    if (!/^[a-z][a-z0-9_-]*$/.test(name)) {
      throw new Error(field + '.name must be one lowercase native role identifier')
    }
    const description = requireNonEmpty(record.description, field + '.description')
    const candidate = requireNonEmpty(record.configFile, field + '.configFile')
    if (!path.isAbsolute(candidate)) {
      throw new Error(field + '.configFile must be an absolute local path')
    }
    const stat = fs.lstatSync(candidate)
    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw new Error(field + '.configFile must be one regular non-symlink file')
    }
    const configFile = fs.realpathSync(candidate)
    if (!roots.some((root) => pathIsWithin(root, configFile))) {
      throw new Error(field + '.configFile must remain inside an authorized read-only root')
    }
    // The native loader warns and skips bad roles. Reject an invalid generated
    // file here instead of silently falling back to inherited root instructions.
    // This closed format cannot override model, provider, effort or permissions.
    const content = fs.readFileSync(configFile, 'utf8')
    const assignment = /^developer_instructions = ("[\s\S]*")\n$/.exec(content)
    let instructions: unknown
    try {
      instructions = assignment === null ? null : JSON.parse(assignment[1]!)
    } catch {
      instructions = null
    }
    if (
      typeof instructions !== 'string' ||
      !instructions.trim() ||
      content !== `developer_instructions = ${JSON.stringify(instructions)}\n`
    ) {
      throw new Error(field + '.configFile must contain only the canonical generated developer_instructions assignment')
    }
    return { name, description, configFile }
  })
  if (new Set(normalized.map(({ name }) => name)).size !== normalized.length) {
    throw new Error('nativeAgentRoles must not contain duplicate names')
  }
  return normalized
}

function nativeAgentRoleThreadConfig(
  roles: readonly GoalEpochNativeAgentRole[],
): Record<string, string> {
  return Object.fromEntries(roles.flatMap(({ name, description, configFile }) => {
    const key = 'agents.' + quotedTomlKeySegment(name)
    return [[key + '.description', description], [key + '.config_file', configFile]]
  }))
}

function goalDelegationThreadConfig(): Record<string, boolean | number> {
  return {
    'features.multi_agent_v2.enabled': false,
    'agents.max_depth': GOAL_DELEGATION_MAX_DEPTH,
  }
}

function normalizeGoalProcessConfig(config: GoalProcessConfig, workspaceRoot: string): GoalProcessConfig & {
  appServerCwd: string
  modelCatalogPath: string
  protectedRoot: string
  readOnlyRoots: readonly string[]
  readOnlyFiles: readonly string[]
  networkAccess: boolean
} {
  if (config.networkAccess !== undefined && typeof config.networkAccess !== 'boolean') {
    throw new Error('goalPolicy.networkAccess must be a boolean')
  }
  const networkAccess = config.networkAccess ?? true
  const permissionProfileId = requireNonEmpty(config.permissionProfileId, 'goalPolicy.permissionProfileId')
  if (!PERMISSION_PROFILE_ID.test(permissionProfileId)) {
    throw new Error('goalPolicy.permissionProfileId is invalid')
  }
  const expectedCliVersion = requireNonEmpty(
    config.expectedCliVersion,
    'goalPolicy.expectedCliVersion',
  )
  if (!/^\d+\.\d+\.\d+$/.test(expectedCliVersion)) {
    throw new Error('goalPolicy.expectedCliVersion must be one exact stable Codex version')
  }
  if (config.expectedModelProvider !== 'openai') {
    throw new Error('goalPolicy.expectedModelProvider must be openai')
  }
  const expectedModel = requireNonEmpty(config.expectedModel, 'goalPolicy.expectedModel')
  const reasoningEffort = requireNonEmpty(config.reasoningEffort, 'goalPolicy.reasoningEffort')
  const modelCatalogPathInput = requireNonEmpty(
    config.modelCatalogPath,
    'goalPolicy.modelCatalogPath',
  )
  if (!path.isAbsolute(modelCatalogPathInput)) {
    throw new Error('goalPolicy.modelCatalogPath must be an absolute path')
  }
  const modelCatalogPath = path.resolve(modelCatalogPathInput)
  const appServerCwdInput = requireNonEmpty(config.appServerCwd, 'goalPolicy.appServerCwd')
  if (!path.isAbsolute(appServerCwdInput)) {
    throw new Error('goalPolicy.appServerCwd must be an absolute path')
  }
  const appServerCwd = path.resolve(appServerCwdInput)
  const protectedRootInput = requireNonEmpty(config.protectedRoot, 'goalPolicy.protectedRoot')
  if (!path.isAbsolute(protectedRootInput)) {
    throw new Error('goalPolicy.protectedRoot must be an absolute path')
  }
  const protectedRoot = path.resolve(protectedRootInput)
  const readOnlyRoots = normalizeRoots(config.readOnlyRoots, 'goalPolicy.readOnlyRoots')
  const readOnlyFiles = normalizeRoots(config.readOnlyFiles, 'goalPolicy.readOnlyFiles').map((file) => {
    const stat = fs.lstatSync(file)
    const canonical = fs.realpathSync.native(file)
    const samePath = process.platform === 'win32'
      ? canonical.toLowerCase() === file.toLowerCase()
      : canonical === file
    if (!stat.isFile() || stat.isSymbolicLink() || !samePath) {
      throw new Error('goalPolicy.readOnlyFiles must contain ordinary files at their canonical paths')
    }
    return canonical
  })
  const readablePaths = [...readOnlyRoots, ...readOnlyFiles]
  const writableRoot = path.resolve(workspaceRoot)
  if (
    !path.isAbsolute(workspaceRoot) ||
    readablePaths.some(
      (root) => pathIsWithin(writableRoot, root) || pathIsWithin(root, writableRoot),
    )
  ) {
    throw new Error('goalPolicy roots do not preserve one separate writable Goal root')
  }
  if (
    pathIsWithin(protectedRoot, writableRoot) ||
    pathIsWithin(writableRoot, protectedRoot) ||
    readablePaths.some(
      (root) => pathIsWithin(protectedRoot, root) || pathIsWithin(root, protectedRoot),
    )
  ) {
    throw new Error('goalPolicy.protectedRoot must not overlap any readable or writable Goal root')
  }
  if (
    pathIsWithin(appServerCwd, writableRoot) ||
    pathIsWithin(writableRoot, appServerCwd) ||
    pathIsWithin(appServerCwd, protectedRoot) ||
    pathIsWithin(protectedRoot, appServerCwd) ||
    readablePaths.some(
      (root) => pathIsWithin(appServerCwd, root) || pathIsWithin(root, appServerCwd),
    )
  ) {
    throw new Error('goalPolicy.appServerCwd must be separate from Goal readable, writable, and protected roots')
  }
  if (!readOnlyRoots.some((root) => pathIsWithin(root, modelCatalogPath))) {
    throw new Error('goalPolicy.modelCatalogPath must stay inside one read-only Goal root')
  }
  return {
    permissionProfileId,
    expectedCliVersion,
    expectedModelProvider: 'openai',
    expectedModel,
    reasoningEffort,
    modelCatalogPath,
    appServerCwd,
    protectedRoot,
    readOnlyRoots,
    readOnlyFiles,
    networkAccess,
  }
}

export function goalConfigArgs(config: GoalProcessConfig | undefined, workspaceRoot: string): string[] {
  if (!config) {
    return []
  }
  const policy = normalizeGoalProcessConfig(config, workspaceRoot)
  const writableRoot = path.resolve(workspaceRoot).replaceAll('\\', '/')
  const protectedRoot = policy.protectedRoot.replaceAll('\\', '/')
  const readOnlyRoots = policy.readOnlyRoots.map((root) => root.replaceAll('\\', '/'))
  const readOnlyFiles = policy.readOnlyFiles.map((file) => file.replaceAll('\\', '/'))
  const workspaceRoots = JSON.stringify(writableRoot) + ' = true'
  const filesystem = [
    '":root" = "deny"',
    '":minimal" = "read"',
    '":workspace_roots" = { "." = "write" }',
    ...readOnlyRoots.map((root) => JSON.stringify(root) + ' = "read"'),
    ...readOnlyFiles.map((file) => JSON.stringify(file) + ' = "read"'),
    JSON.stringify(protectedRoot) + ' = "deny"',
  ].join(', ')
  const profile = [
    '{ description = "Bounded Goal workspace confinement"',
    'extends = ":workspace"',
    'workspace_roots = { ' + workspaceRoots + ' }',
    'filesystem = { ' + filesystem + ' }',
    policy.networkAccess
      ? 'network = { enabled = true, mode = "limited", allow_local_binding = false, allow_upstream_proxy = false, dangerously_allow_non_loopback_proxy = false, dangerously_allow_all_unix_sockets = false, domains = { "*" = "allow" } } }'
      : 'network = { enabled = false } }',
  ].join(', ')
  const overrides = [
    'permissions.' + policy.permissionProfileId + '=' + profile,
    'default_permissions=' + JSON.stringify(policy.permissionProfileId),
    'model=' + JSON.stringify(policy.expectedModel),
    'model_provider="openai"',
    'model_reasoning_effort=' + JSON.stringify(policy.reasoningEffort),
    'agents.default_subagent_model=' + JSON.stringify(policy.expectedModel),
    'agents.default_subagent_reasoning_effort=' + JSON.stringify(policy.reasoningEffort),
    'model_catalog_json=' + JSON.stringify(policy.modelCatalogPath),
    'web_search=' + JSON.stringify(policy.networkAccess ? 'live' : 'disabled'),
    'apps._default.enabled=false',
    'apps._default.destructive_enabled=false',
    'apps._default.open_world_enabled=false',
    'shell_environment_policy.inherit="none"',
    'shell_environment_policy.set.PATH="/usr/bin:/bin"',
    'shell_environment_policy.ignore_default_excludes=false',
    'project_root_markers=[]',
    'skills.bundled.enabled=false',
    'orchestrator.skills.enabled=' + policy.networkAccess,
    ...(!policy.networkAccess ? ['orchestrator.mcp.enabled=false'] : []),
    'tools.experimental_request_user_input.enabled=false',
    'features.multi_agent_v2.enabled=false',
    'agents.max_depth=' + GOAL_DELEGATION_MAX_DEPTH,
    'agents.max_threads=' + CODEX_V1_THREAD_CAPACITY_TOML,
    'features.network_proxy.enabled=' + policy.networkAccess,
    ...Object.entries(goalProcessFeatureConfig(policy.networkAccess)).map(([name, enabled]) => 'features.' + name + '=' + enabled),
  ]
  return ['--strict-config', ...overrides.flatMap((value) => ['-c', value])]
}

function assertConfigIsolatedDirectory(root: string, label: string): void {
  let stat: fs.Stats
  try {
    stat = fs.lstatSync(root)
  } catch (error) {
    throw new Error(label + ' must be one existing local directory', { cause: error })
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(label + ' must be one existing non-symlink directory')
  }
  for (const name of ['.codex', '.agents']) {
    const entry = path.join(root, name)
    const message = label + ' must not contain ambient Codex configuration, plugin, or skill material'
    let metadata: fs.Stats
    try {
      metadata = fs.lstatSync(entry)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue
      throw new Error(message, { cause: error })
    }
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
      throw new Error(message)
    }
    // An empty reserved directory contains no ambient configuration. Keep it
    // intact; pinned Codex still owns protection of these metadata paths.
    let entries: string[]
    try {
      entries = fs.readdirSync(entry)
    } catch (error) {
      throw new Error(message, { cause: error })
    }
    if (entries.length !== 0) throw new Error(message)
  }
}

function codexCliInvocation(cliPath: string): CodexCliInvocation {
  if (process.platform === 'win32' && path.extname(cliPath).toLowerCase() === '.cmd') {
    const resolved = path.resolve(cliPath)
    const executable = path.join(
      path.dirname(resolved),
      'node_modules',
      '@openai',
      'codex',
      'node_modules',
      '@openai',
      'codex-win32-x64',
      'vendor',
      'x86_64-pc-windows-msvc',
      'bin',
      'codex.exe',
    )
    if (path.basename(resolved).toLowerCase() !== 'codex.cmd' || !fs.existsSync(executable)) {
      throw new Error('absolute codex.cmd could not be resolved to the native Codex executable')
    }
    return { executable, prefixArgs: [], shell: false }
  }
  return { executable: cliPath, prefixArgs: [], shell: process.platform === 'win32' }
}

function initializedCodexVersion(response: any): string | undefined {
  const userAgent = extractResult(response)?.userAgent
  // Codex 0.144.1 derives this leading originator from the client surface, not the server build.
  return (
    typeof userAgent === 'string'
      ? /^[^/\r\n]+\/(\d+\.\d+\.\d+)(?:\s|$)/.exec(userAgent)?.[1]
      : undefined
  )
}

function assertInitializedCodexVersion(response: any, expectedVersion: string): void {
  const version = initializedCodexVersion(response)
  if (version !== expectedVersion) {
    throw new Error(`Codex App Server must be the pinned ${expectedVersion} runtime`)
  }
}

function directValueDrift(value: unknown, expected: string): boolean {
  return value !== undefined && value !== expected
}

function effectiveModelDrift(
  settings: any,
  expectedModelProvider: string,
  expectedModel: string,
  reasoningEffort: string,
): boolean {
  return (
    directValueDrift(settings?.modelProvider, expectedModelProvider) ||
    directValueDrift(settings?.model, expectedModel) ||
    directValueDrift(settings?.reasoningEffort, reasoningEffort) ||
    directValueDrift(settings?.effort, reasoningEffort)
  )
}

function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (error: Error) => void
} {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function extractResult(message: any): any {
  return message?.result ?? message
}

function extractThreadResult(message: any): any {
  const result = extractResult(message)
  return result?.data ?? result
}

function extractThreadId(value: any): string | null {
  const candidate = value?.thread?.id ?? value?.threadId ?? value?.id
  return typeof candidate === 'string' && candidate.trim() ? candidate : null
}

function extractTurnId(value: any): string | null {
  const candidate = value?.turn?.id ?? value?.turnId
  return typeof candidate === 'string' && candidate.trim() ? candidate : null
}

function latestActiveTurnId(thread: any): string | null {
  const turns = Array.isArray(thread?.turns) ? thread.turns : []
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index]
    if (turn?.status === 'inProgress' || turn?.status === 'active') {
      return typeof turn.id === 'string' ? turn.id : null
    }
  }
  return null
}

function coerceThreadGoal(value: unknown): ThreadGoal {
  const candidate = value as Partial<ThreadGoal> | null
  if (
    !candidate ||
    typeof candidate.threadId !== 'string' ||
    typeof candidate.objective !== 'string' ||
    !['active', 'paused', 'blocked', 'usageLimited', 'complete'].includes(
      String(candidate.status),
    ) ||
    !Number.isSafeInteger(candidate.tokensUsed) ||
    !Number.isFinite(candidate.timeUsedSeconds) ||
    !Number.isFinite(candidate.createdAt) ||
    !Number.isFinite(candidate.updatedAt)
  ) {
    throw new Error('thread Goal has the wrong shape')
  }
  return candidate as ThreadGoal
}

function asThreadSummary(value: unknown): CodexRawThreadSummary | null {
  const candidate = value as { id?: unknown; name?: unknown } | null
  if (!candidate || typeof candidate.id !== 'string') {
    return null
  }
  return {
    ...(candidate as Record<string, unknown>),
    id: candidate.id,
    name: typeof candidate.name === 'string' ? candidate.name : null,
  } as CodexRawThreadSummary
}

function coerceThreadListPage(message: any): Readonly<{
  data: readonly any[]
  nextCursor: string | null
}> {
  const result = extractResult(message)
  if (!result || typeof result !== 'object' || Array.isArray(result) || !Array.isArray(result.data)) {
    throw new Error('thread/list returned an invalid pagination envelope')
  }
  const nextCursor = result.nextCursor
  if (nextCursor !== null && (typeof nextCursor !== 'string' || nextCursor.length === 0)) {
    throw new Error('thread/list returned an invalid nextCursor')
  }
  return { data: result.data, nextCursor }
}

function coerceThreadReadResult(message: any): CodexRawThreadReadResult {
  const result = extractThreadResult(message)
  const thread = asThreadSummary(result?.thread ?? result)
  if (!thread) {
    throw new Error('thread/read returned no thread')
  }
  return { thread }
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value) ?? 'null'
  }
  if (Array.isArray(value)) {
    return '[' + value.map((item) => stableJson(item)).join(',') + ']'
  }
  const record = value as Record<string, unknown>
  return (
    '{' +
    Object.keys(record)
      .sort()
      .map((key) => JSON.stringify(key) + ':' + stableJson(record[key]))
      .join(',') +
    '}'
  )
}

function coerceDynamicToolCall(value: any): DynamicToolCall {
  if (
    !value ||
    typeof value.threadId !== 'string' ||
    typeof value.turnId !== 'string' ||
    typeof value.callId !== 'string' ||
    typeof value.tool !== 'string' ||
    (value.namespace !== null && value.namespace !== undefined && typeof value.namespace !== 'string')
  ) {
    throw new Error('item/tool/call has the wrong shape')
  }
  return {
    threadId: value.threadId,
    turnId: value.turnId,
    callId: value.callId,
    tool: value.tool,
    namespace: value.namespace ?? null,
    arguments: value.arguments,
  }
}

function coerceDynamicToolResult(value: DynamicToolCallResult): DynamicToolCallResult {
  if (
    !value ||
    typeof value.success !== 'boolean' ||
    !Array.isArray(value.contentItems) ||
    value.contentItems.some(
      (item) => !item || item.type !== 'inputText' || typeof item.text !== 'string',
    ) ||
    (
      value.terminalHandoff !== undefined &&
      value.terminalHandoff !== 'owner_checkpoint'
    ) ||
    (value.terminalHandoff !== undefined && !value.success)
  ) {
    throw new Error('dynamic tool handler returned the wrong shape')
  }
  return {
    success: value.success,
    contentItems: value.contentItems,
    ...(value.terminalHandoff === undefined
      ? {}
      : { terminalHandoff: value.terminalHandoff }),
  }
}

class JsonRpcResponseError extends Error {
  readonly jsonRpcCode: number | string | null
  readonly jsonRpcMethod: string

  constructor(method: string, error: any) {
    super(typeof error?.message === 'string' ? error.message : 'Codex App Server request failed')
    this.name = 'JsonRpcResponseError'
    this.jsonRpcCode =
      typeof error?.code === 'number' || typeof error?.code === 'string' ? error.code : null
    this.jsonRpcMethod = method
  }
}

class GoalDelegationDepthError extends Error {
  constructor(readonly diagnostic: GoalFailureDiagnostic = { code: 'delegation_depth' }) {
    super('Goal delegation rejected a descendant beyond maximum depth 2')
    this.name = 'GoalDelegationDepthError'
  }
}

class GoalDelegationParentError extends Error {
  constructor(readonly diagnostic: GoalFailureDiagnostic = { code: 'delegation_parent' }) {
    super('Goal delegation rejected an inconsistent communication sender')
    this.name = 'GoalDelegationParentError'
  }
}

function stderrLogFields(value: unknown): Record<string, unknown> {
  const stderr = String(value)
  const byteCount = Buffer.byteLength(stderr, 'utf8')
  return {
    stderr_present: byteCount > 0,
    stderr_character_count: Array.from(stderr).length,
    stderr_byte_count: byteCount,
    stderr_sha256:
      byteCount === 0
        ? null
        : createHash('sha256').update(stderr, 'utf8').digest('hex'),
  }
}

function boundaryErrorLogFields(error: unknown): Record<string, unknown> {
  if (error instanceof GoalDelegationDepthError) {
    return { error_class: 'GoalDelegationDepthError' }
  }
  if (error instanceof GoalDelegationParentError) {
    return { error_class: 'GoalDelegationParentError' }
  }
  if (error instanceof GoalEpochCancelledError) {
    return { error_class: 'GoalEpochCancelledError' }
  }
  if (error instanceof GoalEpochStartEffectUnknownError) {
    return { error_class: 'GoalEpochStartEffectUnknownError' }
  }
  if (error instanceof GoalEpochMissionConsistencyError) {
    return { error_class: 'GoalEpochMissionConsistencyError' }
  }
  if (error instanceof GoalEpochSharedAuthorityLossError) {
    return { error_class: 'GoalEpochSharedAuthorityLossError' }
  }
  if (error instanceof JsonRpcResponseError) {
    return {
      error_class: 'JsonRpcResponseError',
      json_rpc_code:
        typeof error.jsonRpcCode === 'number' && Number.isSafeInteger(error.jsonRpcCode)
          ? error.jsonRpcCode
          : null,
      json_rpc_code_present: error.jsonRpcCode !== null,
    }
  }
  if (error instanceof AggregateError) {
    return { error_class: 'AggregateError' }
  }
  return { error_class: error instanceof Error ? 'Error' : 'UnknownError' }
}

const SAFE_CODEX_ERROR_SUBTYPES = new Set<GoalEpochDiagnosticErrorSubtype>([
  'contextWindowExceeded',
  'sessionBudgetExceeded',
  'usageLimitExceeded',
  'serverOverloaded',
  'cyberPolicy',
  'misalignmentPolicyViolation',
  'httpConnectionFailed',
  'responseStreamConnectionFailed',
  'internalServerError',
  'unauthorized',
  'badRequest',
  'threadRollbackFailed',
  'sandboxError',
  'responseStreamDisconnected',
  'responseTooManyFailedAttempts',
  'other',
])

const NORMAL_ROOT_ACTIVITY_METHODS = new Set([
  'hook/started',
  'hook/completed',
  'turn/diff/updated',
  'turn/plan/updated',
  'rawResponseItem/completed',
  'rawResponse/completed',
  'item/agentMessage/delta',
  'item/plan/delta',
  'item/commandExecution/outputDelta',
  'item/commandExecution/terminalInteraction',
  'item/fileChange/outputDelta',
  'item/fileChange/patchUpdated',
  'item/mcpToolCall/progress',
  'item/reasoning/summaryTextDelta',
  'item/reasoning/summaryPartAdded',
  'item/reasoning/textDelta',
  'thread/tokenUsage/updated',
])

function safeCodexErrorInfo(value: unknown): Readonly<{
  subtype: GoalEpochDiagnosticErrorSubtype
  code: number | null
}> {
  if (
    typeof value === 'string' &&
    SAFE_CODEX_ERROR_SUBTYPES.has(value as GoalEpochDiagnosticErrorSubtype)
  ) {
    return { subtype: value as GoalEpochDiagnosticErrorSubtype, code: null }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { subtype: 'unclassified', code: null }
  }
  const entries = Object.entries(value)
  if (entries.length !== 1) {
    return { subtype: 'unclassified', code: null }
  }
  const [key, detail] = entries[0]!
  if (key === 'activeTurnNotSteerable') {
    const turnKind = (detail as { turnKind?: unknown } | null)?.turnKind
    return {
      subtype:
        turnKind === 'review'
          ? 'activeTurnNotSteerableReview'
          : turnKind === 'compact'
            ? 'activeTurnNotSteerableCompact'
            : 'unclassified',
      code: null,
    }
  }
  if (!SAFE_CODEX_ERROR_SUBTYPES.has(key as GoalEpochDiagnosticErrorSubtype)) {
    return { subtype: 'unclassified', code: null }
  }
  const statusCode = (detail as { httpStatusCode?: unknown } | null)?.httpStatusCode
  return {
    subtype: key as GoalEpochDiagnosticErrorSubtype,
    code:
      typeof statusCode === 'number' &&
      Number.isSafeInteger(statusCode) &&
      statusCode >= 100 &&
      statusCode <= 599
        ? statusCode
        : null,
  }
}

function rootNotificationIdentity(message: any): Readonly<{
  threadId: string | null
  turnId: string | null
}> {
  return {
    threadId:
      typeof message?.params?.threadId === 'string' && message.params.threadId.length > 0
        ? message.params.threadId
        : null,
    turnId: extractTurnId(message?.params),
  }
}

export class CodexAppServerBoundary
  extends EventEmitter
  implements CodexBoundary, CodexGoalEpochBoundary
{
  private readonly logger = createLogger('codex_boundary')
  private readonly pending = new Map<string, PendingRequest>()
  private readonly resumedThreadIds = new Set<string>()
  private readonly activeGoals = new Map<string, GoalRuntime>()
  private readonly suspendedStates = new Map<string, GoalEpochState>()
  private readonly suspendedObjectives = new Map<string, string>()
  private readonly suspensionErrors = new Map<string, unknown>()
  private readonly terminalStates = new Map<string, GoalEpochState>()
  private readonly dynamicToolCalls = new Map<string, CachedDynamicToolCall>()
  private child: ReturnType<typeof spawn> | null = null
  private childClosed = true
  private lineReader: readline.Interface | null = null
  private nextId = 1
  private ready = false
  private readyReason: string | null = 'starting'
  private startPromise: Promise<void> | null = null
  private stopped = false
  private intentionalTerminationReason: string | null = null
  private fatalBoundaryError: string | null = null
  // Background containment can settle before its owner starts waiting. Keep the
  // completion barrier fulfilled and deliver retained failures through the API.
  private fatalFencePromise: Promise<void> = Promise.resolve()
  private fatalFenceError: Error | null = null
  private restartTimer: NodeJS.Timeout | null = null
  private messageQueue: Promise<void> = Promise.resolve()
  private readonly observationSourceId = randomUUID()
  private observationReceiptSequence = 0
  private observationNativeProcessSequence = 0
  private observationStderrSeen = false

  constructor(
    private readonly cliPath: string,
    private readonly workspaceRoot: string,
    private readonly options: CodexAppServerBoundaryOptions = {},
  ) {
    super()
  }

  async start(signal?: AbortSignal): Promise<void> {
    throwIfCancelled(signal, 'boundary startup')
    if (this.ready) {
      return
    }
    if (this.startPromise) {
      return this.startPromise
    }
    if (this.fatalBoundaryError) {
      throw new Error(this.fatalBoundaryError)
    }
    this.startPromise = this.doStart(signal).finally(() => {
      this.startPromise = null
    })
    return this.startPromise
  }

  private async doStart(signal?: AbortSignal): Promise<void> {
    this.observationNativeProcessSequence += 1
    this.observationStderrSeen = false
    this.observeBoundaryLifecycle('native_startup', 'starting')
    this.stopped = false
    this.intentionalTerminationReason = null
    this.ready = false
    this.readyReason = 'starting'
    const normalizedGoalPolicy = this.options.goalPolicy
      ? normalizeGoalProcessConfig(this.options.goalPolicy, this.workspaceRoot)
      : null
    const appServerCwd = normalizedGoalPolicy?.appServerCwd ?? this.workspaceRoot
    if (normalizedGoalPolicy) {
      assertConfigIsolatedDirectory(appServerCwd, 'goalPolicy.appServerCwd')
      assertConfigIsolatedDirectory(this.workspaceRoot, 'Goal workspace root')
    }
    const invocation = codexCliInvocation(this.cliPath)
    const args = goalConfigArgs(this.options.goalPolicy, this.workspaceRoot)
    const child = spawn(invocation.executable, [...invocation.prefixArgs, 'app-server', ...args], {
      cwd: appServerCwd,
      env: {
        ...process.env,
        ...(this.options.goalPolicy ? { [REMOTE_CONTROL_DISABLED_ENV_VAR]: '1' } : {}),
      },
      shell: invocation.shell,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.child = child
    this.childClosed = false
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      this.observeNativeStderr(String(chunk), false)
      this.logger.warn('bridge.boundary.stderr', stderrLogFields(chunk))
    })
    child.once('exit', (code) => {
      this.markProcessExited(code)
    })
    child.once('close', (code, signal) => {
      void this.onProcessClose(code, signal)
    })
    this.lineReader = readline.createInterface({ input: child.stdout, crlfDelay: Infinity })
    this.lineReader.on('line', (line) => {
      const trimmed = line.trim()
      if (!trimmed) {
        return
      }
      try {
        this.dispatchDecodedMessage(JSON.parse(trimmed))
      } catch {
        this.logger.error('bridge.boundary.invalid_json_line', {})
      }
    })
    try {
      const initialized = await this.request(
        'initialize',
        {
          clientInfo: {
            name: 'workstation-control-bridge',
            title: 'Workstation Control Bridge',
            version: '0.0.0',
          },
          capabilities: { experimentalApi: true },
        },
        signal,
        'boundary startup',
      )
      const observedVersion = initializedCodexVersion(initialized)
      if (observedVersion) this.emitExecutionObservation({
        identity: { root_thread_id: null, thread_id: null, parent_thread_id: null, turn_id: null, item_id: null, operation_id: null },
        received_at: new Date().toISOString(), source_time: null, source_method: 'initialize', phase: 'processed', caused_by: null,
        kind: 'lifecycle', summary: 'Native source identity observed', details: { stage: 'native_handshake' }, contents: [],
        source_facts: { codex_version: observedVersion },
      })
      if (normalizedGoalPolicy) {
        try {
          assertInitializedCodexVersion(initialized, normalizedGoalPolicy.expectedCliVersion)
        } catch (error) {
          await this.terminateDedicatedAppServer('pinned Codex App Server identity mismatch')
          throw error
        }
      }
      await this.sanityCheck(signal)
      throwIfCancelled(signal, 'boundary startup')
      this.ready = true
      this.readyReason = null
      this.observeBoundaryLifecycle('native_startup', 'ready')
    } catch (error) {
      this.observeBoundaryLifecycle('native_startup_failed', 'failed', null, null, error)
      if (signal?.aborted) {
        this.intentionalTerminationReason = 'boundary startup explicitly cancelled'
        this.ready = false
        this.readyReason = this.intentionalTerminationReason
        await this.stopNativeAppServerProcess()
        this.cancelPendingRequests(this.readyReason)
        throw cancellationError(signal, 'boundary startup')
      }
      throw error
    }
  }

  private markProcessExited(code: number | null): void {
    this.ready = false
    this.readyReason =
      this.intentionalTerminationReason ?? 'boundary exited with code ' + (code ?? 'unknown')
  }

  private cancelPendingRequests(reason: string): void {
    for (const pending of this.pending.values()) {
      pending.cleanup()
      const error = new Error(reason)
      this.observeRequestOutcome(pending, 'rejected', error)
      pending.reject(error)
    }
    this.pending.clear()
  }

  private async onProcessClose(code: number | null, signal: NodeJS.Signals | null = null): Promise<void> {
    this.observeNativeStderr('', true)
    const intentional = this.stopped || this.intentionalTerminationReason !== null
    const identity = { root_thread_id: null, thread_id: null, parent_thread_id: null, turn_id: null, item_id: null, operation_id: null }
    const exitReceipt = this.emitExecutionObservation({
      identity, received_at: new Date().toISOString(), source_time: null, source_method: 'core/native_process/close',
      phase: 'received', caused_by: null, kind: 'lifecycle', summary: 'Native process exit observed',
      details: { stage: 'native_process_exit', state: intentional ? 'terminated' : code === 0 ? 'exited' : 'failed',
        reason: JSON.stringify({ exit_code: code, signal, intentional }) }, contents: [],
    })
    if (!intentional && code !== 0) this.emitExecutionObservation({
      identity, received_at: new Date().toISOString(), source_time: null, source_method: 'core/native_process/close',
      phase: 'rejected', caused_by: exitReceipt, kind: 'error', summary: 'Native process exited unexpectedly',
      details: { code: 'native_process_exit', actual: code ?? signal ?? 'unknown', expected: 0, containment: true }, contents: [],
    })
    this.childClosed = true
    this.markProcessExited(code)
    this.cancelPendingRequests(this.readyReason ?? 'Codex App Server process closed')
    if (this.stopped || this.intentionalTerminationReason !== null) {
      return
    }
    if (this.options.restartOnUnexpectedExit === false || this.activeGoals.size > 0) {
      this.fatalBoundaryError = this.readyReason
      this.observeBoundaryLifecycle('process_exit_containment', 'started', null, exitReceipt)
      this.fatalFencePromise = this.containAfterProcessExit().then(() => {
        this.observeBoundaryLifecycle('process_exit_containment', 'completed', null, exitReceipt)
      }).catch((error) => {
        this.observeBoundaryLifecycle('containment_failed', 'failed', null, exitReceipt, error)
        this.fatalFenceError = error instanceof Error ? error : new Error(String(error))
      })
      return
    }
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null
      void this.start()
    }, 1000)
  }

  private async containAfterProcessExit(): Promise<void> {
    const errors: unknown[] = []
    for (const runtime of [...this.activeGoals.values()]) {
      await this.closeAdmission(runtime, 'boundary_process_exit')
      this.cancelRuntimeOperations(runtime, 'Codex App Server process closed')
      await this.messageQueue
      try {
        await Promise.all([...runtime.pendingMaterials.values()])
      } catch (error) {
        errors.push(error)
      }
      try {
        await this.invokeScopedStopCallback(
          this.stopContext(runtime.threadId, runtime.activeTurnId, 'boundary_failure'),
        )
      } catch (error) {
        errors.push(error)
      }
      this.saveTerminalState(runtime)
      await this.removeRuntime(runtime.threadId)
    }
    if (errors.length > 0) {
      throw new AggregateError(errors, 'Codex process exit containment completed with errors')
    }
  }

  protected dispatchDecodedMessage(message: any): void {
    const responseId = message?.id
    if (responseId !== undefined && typeof message?.method !== 'string') {
      const requestId = String(responseId)
      const pending = this.pending.get(requestId)
      if (!pending) {
        return
      }
      this.pending.delete(requestId)
      pending.cleanup()
      if (message.error) {
        this.observeRequestOutcome(pending, 'rejected', message.error)
        pending.reject(new JsonRpcResponseError(pending.method, message.error))
      } else {
        this.observeRequestOutcome(pending, 'processed')
        pending.resolve(message)
      }
      return
    }
    void this.enqueueRuntimeMessage(message)
  }

  protected enqueueRuntimeMessage(message: any): Promise<void> {
    const observation = this.receiveRuntimeObservation(message)
    const next = this.messageQueue.then(async () => {
      this.projectRuntimeObservation(observation)
      try {
        if (message?.id !== undefined && typeof message?.method === 'string') {
          await this.handleServerRequest(message, observation)
        } else {
          await this.handleNotification(message, observation)
          this.emit('notification', message)
        }
        if (observation && !observation.rejected) {
          this.emitRuntimeObservationOutcome(observation, 'processed')
        }
      } catch (error) {
        this.rejectRuntimeObservation(observation, error)
        throw error
      }
    })
    this.messageQueue = next.catch((error) => {
      this.logger.error('bridge.boundary.message_failed', boundaryErrorLogFields(error))
      if (error instanceof GoalEpochSharedAuthorityLossError) {
        this.triggerFatalContainment('shared_authority_loss', error, observation)
      } else if (
        error instanceof GoalDelegationDepthError ||
        error instanceof GoalDelegationParentError ||
        error instanceof GoalEpochMissionConsistencyError
      ) {
        this.triggerFatalContainment('mission_consistency_failure', error, observation)
      }
    })
    return next
  }

  private emitExecutionObservation(event: CodexExecutionObservation): RhObservationPosition | null {
    try {
      const admitted = this.options.onExecutionObservation?.(event)
      // A mistakenly async consumer must not leak rejection into the native queue.
      if (admitted && typeof (admitted as unknown as Promise<unknown>).then === 'function') {
        void Promise.resolve(admitted).catch(() => undefined)
        return null
      }
      return admitted && typeof admitted.incarnation === 'string' &&
        Number.isSafeInteger(admitted.sequence) && admitted.sequence >= 1 ? admitted : null
    } catch {
      return null
    }
  }

  private receiveRuntimeObservation(message: unknown): RuntimeObservationContext | null {
    if (!this.options.onExecutionObservation) return null
    const observation: RuntimeObservationContext = {
      rawAgentMessage: (message as any)?.method === 'rawResponseItem/completed'
        ? decodeNativeAgentMessage((message as any)?.params?.item) : null,
      scope: {
        rootThreadId: null,
        parentThreadId: null,
        depth: null,
        receiptId: `${this.observationSourceId}:${++this.observationReceiptSequence}`,
        receivedAt: new Date().toISOString(),
      },
      message,
      receipt: null,
      projected: false,
      rejected: false,
      identity: null,
    }
    this.projectRuntimeObservation(observation)
    return observation
  }

  private projectRuntimeObservation(observation: RuntimeObservationContext | null): void {
    if (!observation || observation.projected) return
    try {
      const threadId = nativeObservationThreadId(observation.message)
      const message = observation.message as any
      const parentId = message?.params?.thread?.parentThreadId ??
        message?.params?.thread?.source?.subAgent?.thread_spawn?.parent_thread_id
      const binding = threadId ? this.findBinding(threadId) : null
      const parent = !binding && typeof parentId === 'string' ? this.findBinding(parentId) : null
      const source = binding ?? parent
      if (!source) return
      observation.scope.rootThreadId = source.rootThreadId
      observation.scope.parentThreadId = binding?.descendant?.parentThreadId ??
        (parent ? parentId : null)
      observation.scope.depth = binding ? binding.descendant?.depth ?? 0 :
        (parent?.descendant?.depth ?? 0) + 1
      const item = message?.params?.item
      if (item?.type === 'collabAgentToolCall') {
        const sender = typeof item.senderThreadId === 'string' ? this.findBinding(item.senderThreadId) : null
        const senderId = sender?.rootThreadId === source.rootThreadId && item.senderThreadId === threadId
          ? item.senderThreadId as string : null
        const parents = Array.isArray(item.receiverThreadIds) ? item.receiverThreadIds.map((id: unknown) => {
          const target = typeof id === 'string' ? this.findBinding(id) : null
          return target?.rootThreadId === source.rootThreadId ? target.descendant?.parentThreadId : null
        }) : []
        observation.scope.collaborationPromptField = classifyCollaborationPrompt(item.tool, senderId, parents)
      }
      if (observation.rawAgentMessage && threadId) {
        observation.scope.nativeAgentMessage = this.resolveNativeAgentMessage(source.runtime, observation.rawAgentMessage, threadId) ?? undefined
      }
      observation.projected = true
      const identity = {
        root_thread_id: source.rootThreadId,
        thread_id: threadId,
        parent_thread_id: observation.scope.parentThreadId,
        turn_id: extractTurnId(message?.params),
        item_id: typeof message?.params?.item?.id === 'string' ? message.params.item.id :
          typeof message?.params?.itemId === 'string' ? message.params.itemId : null,
        operation_id: typeof message?.params?.callId === 'string' ? message.params.callId : null,
      }
      observation.identity = identity
      observation.receipt = this.emitExecutionObservation({
        kind: 'lifecycle',
        phase: 'received',
        source_method: typeof message?.method === 'string' ? message.method : 'unknown',
        received_at: observation.scope.receivedAt,
        source_time: null,
        identity,
        summary: 'Native activity received',
        details: { stage: 'native_receipt' },
        contents: [],
        caused_by: null,
      })
      for (const projected of projectNativeObservation(observation.message, observation.scope)) {
        this.emitExecutionObservation({ ...projected, phase: 'received', caused_by: observation.receipt })
      }
    } catch {
      // Projection is independent of native parsing, authorization and custody.
    }
  }

  private emitRuntimeObservationOutcome(
    observation: RuntimeObservationContext,
    phase: 'processed' | 'rejected',
    error?: unknown,
    diagnostic?: GoalFailureDiagnostic,
  ): void {
    if (!observation.projected) return
    try {
      const message = observation.message as any
      const identity = observation.identity ?? {
        root_thread_id: observation.scope.rootThreadId,
        thread_id: nativeObservationThreadId(observation.message),
        parent_thread_id: observation.scope.parentThreadId,
        turn_id: extractTurnId(message?.params),
        item_id: null,
        operation_id: null,
      }
      const common = {
        phase,
        received_at: new Date().toISOString(),
        source_time: null,
        source_method: typeof message?.method === 'string' ? message.method : 'unknown',
        identity,
        caused_by: observation.receipt,
      }
      if (phase === 'processed') {
        this.emitExecutionObservation({
          ...common, kind: 'lifecycle', summary: 'Native activity processed',
          details: { stage: 'native_processing' }, contents: [],
        })
      } else {
        const details = diagnostic ?? (
          error instanceof GoalDelegationDepthError || error instanceof GoalDelegationParentError ||
          error instanceof GoalEpochMissionConsistencyError ? error.diagnostic : undefined
        ) ?? { code: 'native_processing_failed' }
        this.emitExecutionObservation({
          ...common, kind: 'error', summary: 'Native activity rejected', details,
          contents: error instanceof Error ? [{
            content_key: `${observation.scope.receiptId}:error`, field: 'error',
            text: error.message, update_mode: 'snapshot', complete: true,
          }] : [],
        })
      }
    } catch {
      // A malformed diagnostic may not replace the original research outcome.
    }
  }

  private rejectRuntimeObservation(
    observation: RuntimeObservationContext | null,
    error: unknown,
    diagnostic?: GoalFailureDiagnostic,
  ): void {
    if (!observation || observation.rejected) return
    observation.rejected = true
    this.emitRuntimeObservationOutcome(observation, 'rejected', error, diagnostic)
  }

  private observeWorkerState(binding: ThreadBinding, action: 'registered' | 'status', statusOverride?: string): void {
    if (!this.options.onExecutionObservation) return
    const descendant = binding.descendant
    this.emitExecutionObservation({
      phase: 'processed', kind: 'work', source_method: 'core/worker/ownership',
      received_at: new Date().toISOString(), source_time: null,
      identity: {
        root_thread_id: binding.rootThreadId,
        thread_id: descendant?.threadId ?? binding.rootThreadId,
        parent_thread_id: descendant?.parentThreadId ?? null,
        turn_id: descendant ? descendant.activeTurnId : binding.runtime.activeTurnId,
        item_id: null, operation_id: null,
      },
      summary: action === 'registered' ? 'Native worker registered' : 'Native worker status observed',
      details: {
        action, child_thread_id: descendant?.threadId ?? binding.rootThreadId,
        sender_thread_id: descendant?.parentThreadId ?? null,
        depth: descendant?.depth ?? 0,
        status: statusOverride ?? descendant?.status ?? (binding.runtime.activeTurnId === null ? 'pending' : 'active'),
      },
      contents: [], caused_by: null,
    })
  }

  private observeNativeStderr(text: string, complete: boolean): void {
    if (!this.options.onExecutionObservation) return
    if (!this.observationStderrSeen && text.length === 0) return
    this.observationStderrSeen = true
    this.emitExecutionObservation({
      identity: { root_thread_id: null, thread_id: null, parent_thread_id: null, turn_id: null, item_id: null, operation_id: null },
      received_at: new Date().toISOString(), source_time: null, source_method: 'core/native_process/stderr',
      phase: 'received', caused_by: null, kind: 'error', summary: 'Native process diagnostic output',
      details: { code: 'native_stderr' }, contents: [{ content_key: `${this.observationSourceId}:${this.observationNativeProcessSequence}:stderr`,
        field: 'error', text, update_mode: 'append', complete }],
    })
  }

  private observeRequestOutcome(pending: PendingRequest, phase: 'processed' | 'rejected', error?: unknown): void {
    if (!pending.observation) return
    const common = { identity: pending.observation.identity, caused_by: pending.observation.receipt,
      received_at: new Date().toISOString(), source_time: null, source_method: pending.method, phase }
    if (phase === 'processed') {
      this.emitExecutionObservation({ ...common, kind: 'lifecycle', summary: 'Native request completed',
        details: { stage: 'request_response' }, contents: [] })
    } else {
      const native = error as { code?: unknown; message?: unknown } | null
      this.emitExecutionObservation({ ...common, kind: 'error', summary: 'Native request failed',
        details: { code: 'native_request_failed', ...(typeof native?.code === 'number' ? { actual: native.code } : {}) },
        contents: typeof native?.message === 'string' ? [{ content_key: `${this.observationSourceId}:${common.identity.operation_id}:error`,
          field: 'error', text: native.message, update_mode: 'snapshot', complete: true }] : [] })
    }
  }

  private observeBoundaryLifecycle(
    stage: string,
    state: string,
    rootThreadId: string | null = null,
    causedBy: RhObservationPosition | null = null,
    error?: unknown,
  ): void {
    if (!this.options.onExecutionObservation) return
    const identity = { root_thread_id: rootThreadId, thread_id: rootThreadId,
      parent_thread_id: null, turn_id: null, item_id: null, operation_id: null }
    const common = { identity, received_at: new Date().toISOString(), source_time: null,
      phase: 'processed' as const, source_method: 'core/boundary/lifecycle', caused_by: causedBy }
    if (error === undefined) {
      this.emitExecutionObservation({ ...common, kind: 'lifecycle',
        summary: 'Native boundary lifecycle observed', details: { stage, state }, contents: [] })
    } else {
      this.emitExecutionObservation({ ...common, kind: 'error', phase: 'rejected',
        summary: 'Native boundary operation failed',
        details: { code: stage, containment: stage.startsWith('containment') },
        contents: error instanceof Error ? [{ content_key: `${this.observationSourceId}:${stage}:${++this.observationReceiptSequence}`,
          field: 'error', text: error.message, update_mode: 'snapshot', complete: true }] : [],
      })
    }
  }

  protected async ensureReady(signal?: AbortSignal): Promise<void> {
    throwIfCancelled(signal, 'boundary readiness')
    if (this.fatalBoundaryError) {
      throw new Error(this.fatalBoundaryError)
    }
    if (!this.ready) {
      await this.start(signal)
    }
    throwIfCancelled(signal, 'boundary readiness')
  }

  protected request(
    method: string,
    params: unknown,
    signal?: AbortSignal,
    cancellationPhase = 'App Server request',
  ): Promise<any> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(cancellationError(signal, cancellationPhase))
        return
      }
      const stdin = this.child?.stdin
      if (!stdin || this.childClosed || this.stopped || this.intentionalTerminationReason !== null) {
        reject(new Error('boundary stdin is unavailable'))
        return
      }
      const id = 'req-' + this.nextId++
      let abortListener: (() => void) | null = null
      const cleanup = (): void => {
        if (abortListener) {
          signal?.removeEventListener('abort', abortListener)
          abortListener = null
        }
      }
      const pending: PendingRequest = { method, resolve, reject, cleanup }
      if (this.options.onExecutionObservation) {
        const sourceParams = params as { threadId?: unknown; turnId?: unknown } | null
        const threadId = typeof sourceParams?.threadId === 'string' ? sourceParams.threadId : null
        const binding = threadId ? this.findBinding(threadId) : null
        const identity = { root_thread_id: binding?.rootThreadId ?? null, thread_id: threadId,
          parent_thread_id: binding?.descendant?.parentThreadId ?? null,
          turn_id: typeof sourceParams?.turnId === 'string' ? sourceParams.turnId : null,
          item_id: null, operation_id: id }
        pending.observation = { identity, receipt: this.emitExecutionObservation({
          identity, caused_by: null, received_at: new Date().toISOString(), source_time: null,
          source_method: method, phase: 'received', kind: 'lifecycle', summary: 'Native request sent',
          details: { stage: 'request_dispatch' }, contents: [],
        }) }
      }
      if (signal) {
        abortListener = () => {
          if (this.pending.get(id) !== pending) {
            return
          }
          this.pending.delete(id)
          cleanup()
          const error = cancellationError(signal, cancellationPhase)
          this.observeRequestOutcome(pending, 'rejected', error)
          reject(error)
        }
        signal.addEventListener('abort', abortListener, { once: true })
      }
      this.pending.set(id, pending)
      try {
        stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
      } catch (error) {
        this.pending.delete(id)
        cleanup()
        this.observeRequestOutcome(pending, 'rejected', error)
        reject(error instanceof Error ? error : new Error(String(error)))
      }
    })
  }

  protected respondToServerRequest(
    requestId: string | number,
    result: DynamicToolCallResult,
  ): void {
    if (this.childClosed) {
      return
    }
    const stdin = this.child?.stdin
    if (!stdin) {
      throw new Error('boundary stdin is unavailable for a server response')
    }
    stdin.write(JSON.stringify({ jsonrpc: '2.0', id: requestId, result }) + '\n')
  }

  private findBinding(threadId: string): ThreadBinding | null {
    const root = this.activeGoals.get(threadId)
    if (root) {
      return { rootThreadId: threadId, runtime: root, descendant: null }
    }
    for (const runtime of this.activeGoals.values()) {
      const descendant = runtime.descendants.get(threadId)
      if (descendant) {
        return { rootThreadId: runtime.threadId, runtime, descendant }
      }
    }
    return null
  }

  private registerDescendant(threadId: string, parentThreadId: string): ThreadBinding | null {
    if (!threadId || threadId === parentThreadId) {
      return null
    }
    const existing = this.findBinding(threadId)
    if (existing) {
      if (!existing.descendant || existing.descendant.parentThreadId !== parentThreadId) {
        throw new GoalEpochMissionConsistencyError(
          'native child identity changed its Goal parent',
          undefined,
          { code: 'child_parent_identity', actual: parentThreadId,
            expected: existing.descendant?.parentThreadId ?? null,
            source_location: 'codex-thread-core/registerDescendant', containment: true },
        )
      }
      return existing
    }
    const parent = this.findBinding(parentThreadId)
    if (!parent) {
      return null
    }
    const depth = (parent.descendant?.depth ?? 0) + 1
    if (depth > GOAL_DELEGATION_MAX_DEPTH) {
      throw new GoalDelegationDepthError({ code: 'child_registration_depth', actual: depth,
        expected: GOAL_DELEGATION_MAX_DEPTH,
        source_location: 'codex-thread-core/registerDescendant', containment: true })
    }
    const descendant: DescendantRuntime = {
      threadId,
      parentThreadId,
      admittedForMaterial: !parent.runtime.stopping,
      agentPath: null,
      depth,
      activeTurnId: null,
      status: 'pending',
      toolGrant: null,
      toolGrantFamily: null,
      nativeAgentRole: undefined,
    }
    parent.runtime.descendants.set(threadId, descendant)
    this.observeWorkerState({ rootThreadId: parent.rootThreadId, runtime: parent.runtime, descendant }, 'registered')
    return { rootThreadId: parent.rootThreadId, runtime: parent.runtime, descendant }
  }

  installDescendantToolGrant(
    input: GoalEpochDescendantToolGrantInput,
  ): GoalEpochDescendantToolGrantBinding {
    const target = this.resolveDirectChildToolGrantTarget(input)
    const grantId = requireNonEmpty(input.grantId, 'grant.grantId')
    const assignmentId = requireNonEmpty(input.assignmentId, 'grant.assignmentId')
    if (
      grantId !== input.grantId ||
      assignmentId !== input.assignmentId
    ) {
      throw new Error('descendant tool grant identities must be exact strings without outer whitespace')
    }
    if (
      !Array.isArray(input.allowedToolNames) ||
      input.allowedToolNames.some((name) => typeof name !== 'string')
    ) {
      throw new Error(
        'descendant tool grant must select a duplicate-free historical read-tool subset or Candidate A1 review-tool subset or research read-tool subset or Admission tool subset',
      )
    }
    const { family, allowedToolNames } = normalizeDescendantToolGrantSelection(
      input.allowedToolNames,
    )
    const binding = this.requireDirectChildToolGrantBinding(input)
    if (allowedToolNames.some((name) => !binding.runtime.toolNames.has(name))) {
      throw new Error('descendant tool grant selected a tool outside the root Goal catalog')
    }
    const existing = binding.descendant.toolGrant
    if (existing) {
      const existingDefinition = DESCENDANT_TOOL_FAMILIES.find(
        (candidate) => candidate.family === existing.family,
      )
      const existingAllowed = existingDefinition?.toolNames.filter((name) =>
        existing.allowedToolNames.has(name),
      ) ?? []
      if (
        existing.grantId !== grantId ||
        existing.assignmentId !== assignmentId ||
        existing.family !== family ||
        stableJson(existingAllowed) !== stableJson(allowedToolNames)
      ) {
        throw new Error(
          `live descendant already has a different ${descendantToolGrantLabel(existing.family)} grant`,
        )
      }
    }
    const priorFamily = binding.descendant.toolGrantFamily
    if (priorFamily !== null && priorFamily !== family) {
      throw new Error(
        `descendant remains bound to its ${descendantToolGrantLabel(priorFamily)} family after grant revocation`,
      )
    }
    const requiredRole = DESCENDANT_TOOL_FAMILY_ROLES[family]
    if (binding.descendant.nativeAgentRole !== requiredRole) {
      throw new Error(`descendant ${descendantToolGrantLabel(family)} grant requires observed native role ${requiredRole}`)
    }
    if (!existing) {
      binding.descendant.toolGrantFamily = family
      binding.descendant.toolGrant = {
        grantId,
        assignmentId,
        family,
        allowedToolNames: new Set(allowedToolNames),
        controller: new AbortController(),
      }
    }
    return {
      ...target,
      grantId,
      assignmentId,
      allowedToolNames,
    }
  }

  resolveDirectChildToolGrantTarget(
    input: GoalEpochDirectChildToolGrantTargetInput,
  ): GoalEpochDirectChildToolGrantTarget {
    const binding = this.requireDirectChildToolGrantBinding(input)
    return {
      rootThreadId: binding.rootThreadId,
      childThreadId: binding.descendant.threadId,
      parentThreadId: binding.descendant.parentThreadId,
      depth: binding.descendant.depth,
      status: binding.descendant.status,
      activeTurnId: binding.descendant.activeTurnId,
    }
  }

  private requireDirectChildToolGrantBinding(
    input: GoalEpochDirectChildToolGrantTargetInput,
  ): DirectChildThreadBinding {
    const rootThreadId = requireNonEmpty(input.rootThreadId, 'grant.rootThreadId')
    const childThreadId = requireNonEmpty(input.childThreadId, 'grant.childThreadId')
    if (rootThreadId !== input.rootThreadId || childThreadId !== input.childThreadId) {
      throw new Error('descendant tool grant identities must be exact strings without outer whitespace')
    }
    const binding = this.findBinding(childThreadId)
    if (
      !binding?.descendant ||
      binding.rootThreadId !== rootThreadId ||
      binding.descendant.parentThreadId !== rootThreadId ||
      binding.descendant.depth !== 1 ||
      binding.runtime.stopping ||
      (binding.descendant.status !== 'pending' && binding.descendant.status !== 'active')
    ) {
      throw new Error('descendant tool grant target is not one exact live direct child')
    }
    return binding as DirectChildThreadBinding
  }

  private operationContext(
    binding: ThreadBinding,
    turnId: string | null,
    signal: AbortSignal,
  ): GoalEpochOperationContext {
    const descendant = binding.descendant
    const grant = descendant?.toolGrant ?? null
    return {
      signal,
      rootThreadId: binding.rootThreadId,
      callerThreadId: descendant?.threadId ?? binding.rootThreadId,
      parentThreadId: descendant?.parentThreadId ?? null,
      depth: descendant?.depth ?? 0,
      turnId,
      status: descendant?.status ?? 'root',
      grantId: grant?.grantId ?? null,
      assignmentId: grant?.assignmentId ?? null,
    }
  }

  private async revokeDescendantToolGrant(
    binding: ThreadBinding,
    reason: GoalEpochDescendantToolGrantRevocationReason,
  ): Promise<void> {
    const descendant = binding.descendant
    const grant = descendant?.toolGrant
    if (!descendant || !grant) {
      return
    }
    descendant.toolGrant = null
    if (!grant.controller.signal.aborted) {
      grant.controller.abort(
        new Error(`descendant ${descendantToolGrantLabel(grant.family)} grant revoked: ${reason}`),
      )
    }
    const callback = this.options.onDescendantToolGrantRevoked
    if (!callback) {
      return
    }
    try {
      await callback({
        rootThreadId: binding.rootThreadId,
        childThreadId: descendant.threadId,
        parentThreadId: descendant.parentThreadId,
        depth: descendant.depth,
        grantId: grant.grantId,
        assignmentId: grant.assignmentId,
        reason,
      })
    } catch (error) {
      this.logger.error('bridge.boundary.descendant_grant_revocation_failed', boundaryErrorLogFields(error))
    }
  }

  private async revokeAllDescendantToolGrants(
    runtime: GoalRuntime,
    reason: GoalEpochDescendantToolGrantRevocationReason,
  ): Promise<void> {
    await Promise.all(
      [...runtime.descendants.values()].map((descendant) =>
        this.revokeDescendantToolGrant(
          { rootThreadId: runtime.threadId, runtime, descendant },
          reason,
        ),
      ),
    )
  }

  private materialId(input: Omit<GoalEpochNativeMaterialObservation, 'observationId'>): string {
    return createHash('sha256').update(stableJson(input)).digest('hex')
  }

  private observeMaterial(
    runtime: GoalRuntime,
    input: Omit<GoalEpochNativeMaterialObservation, 'observationId'>,
  ): Promise<GoalEpochNativeMaterialCustodyOutcome | void> {
    const observationId = this.materialId(input)
    const existing = runtime.materials.get(observationId)
    if (existing) {
      if (stableJson(existing) !== stableJson({ observationId, ...input })) {
        return Promise.reject(
          new GoalEpochMissionConsistencyError('native material identity changed on replay', undefined, {
            code: 'native_material_replay_identity', actual: observationId,
            source_location: 'codex-thread-core/observeMaterial', containment: true,
          }),
        )
      }
      return runtime.pendingMaterials.get(observationId) ?? Promise.resolve()
    }
    const observation = { observationId, ...input }
    runtime.materials.set(observationId, observation)
    const custodyIdentity = { root_thread_id: input.rootThreadId, thread_id: input.childThreadId,
      parent_thread_id: input.parentThreadId, turn_id: null, item_id: null, operation_id: observationId }
    const custodyReceipt = this.options.onExecutionObservation ? this.emitExecutionObservation({
      identity: custodyIdentity, received_at: new Date().toISOString(), source_time: null,
      source_method: 'core/native_material/custody', phase: 'received', caused_by: null,
      kind: 'lifecycle', summary: 'Native material custody requested',
      details: { stage: 'native_material_custody', state: 'requested' }, contents: [],
    }) : null
    const operationSignal = runtime.materialOperationController.signal
    const materialBinding = this.findBinding(input.childThreadId) ?? this.findBinding(runtime.threadId)
    const delivery = this.options.onNativeMaterialObserved
      ? this.options.onNativeMaterialObserved(
          observation,
          materialBinding
            ? this.operationContext(
                materialBinding,
                materialBinding.descendant?.activeTurnId ?? runtime.activeTurnId,
                operationSignal,
              )
            : {
                signal: operationSignal,
                rootThreadId: runtime.threadId,
                callerThreadId: runtime.threadId,
                parentThreadId: null,
                depth: 0,
                turnId: runtime.activeTurnId,
                status: 'root',
                grantId: null,
                assignmentId: null,
              },
        ).then((outcome) => {
          if (outcome && outcome.observationId !== observationId) {
            throw new GoalEpochMissionConsistencyError(
              'native material custody outcome differs from its observation identity',
              undefined,
              { code: 'native_material_custody_identity', actual: outcome.observationId,
                expected: observationId, source_location: 'codex-thread-core/observeMaterial',
                containment: true },
            )
          }
          this.emitExecutionObservation({ identity: custodyIdentity, received_at: new Date().toISOString(),
            source_time: null, source_method: 'core/native_material/custody', phase: 'processed', caused_by: custodyReceipt,
            kind: 'lifecycle', summary: 'Native material custody returned',
            details: { stage: 'native_material_custody', state: outcome?.status ?? 'acknowledged',
              ...(outcome?.status === 'capture_recovery_required' ? { reason: outcome.ownerCode } : {}) }, contents: [] })
          if (outcome?.status === 'captured') this.emitExecutionObservation({
            identity: custodyIdentity, received_at: new Date().toISOString(), source_time: null,
            source_method: 'core/native_material/custody', phase: 'processed', caused_by: custodyReceipt,
            kind: 'artifact', summary: 'Owner capture reference received',
            details: { owner_handle: outcome.captureRef.materialId, media_type: 'application/json' },
            contents: [{ content_key: `${this.observationSourceId}:${observationId}:capture`, field: 'artifact',
              text: JSON.stringify(outcome.captureRef), update_mode: 'snapshot', complete: true }],
          })
          return outcome
        }).catch((error) => {
          this.emitExecutionObservation({ identity: custodyIdentity, received_at: new Date().toISOString(),
            source_time: null, source_method: 'core/native_material/custody', phase: 'rejected', caused_by: custodyReceipt,
            kind: 'error', summary: 'Native material custody failed', details: { code: 'native_material_custody_failed' },
            contents: error instanceof Error ? [{ content_key: `${this.observationSourceId}:${observationId}:error`, field: 'error',
              text: error.message, update_mode: 'snapshot', complete: true }] : [],
          })
          if (runtime.materials.get(observationId) === observation) {
            runtime.materials.delete(observationId)
          }
          if (!operationSignal.aborted) {
            throw error
          }
        })
      : Promise.resolve(undefined)
    runtime.pendingMaterials.set(observationId, delivery)
    void delivery.then(
      () => runtime.pendingMaterials.delete(observationId),
      () => runtime.pendingMaterials.delete(observationId),
    )
    return delivery
  }

  private descendantByAgentPath(runtime: GoalRuntime, agentPath: string): DescendantRuntime | null {
    for (const descendant of runtime.descendants.values()) {
      if (descendant.agentPath === agentPath) {
        return descendant
      }
    }
    return null
  }

  private threadIdForAgentPath(runtime: GoalRuntime, agentPath: string): string | null {
    if (agentPath === '/root') {
      return runtime.threadId
    }
    return this.descendantByAgentPath(runtime, agentPath)?.threadId ?? null
  }

  private directParentAgentPath(agentPath: string): string | null {
    const separator = agentPath.lastIndexOf('/')
    return separator > 0 ? agentPath.slice(0, separator) : null
  }

  private async preserveNativeAgentMessage(
    runtime: GoalRuntime,
    message: PendingNativeAgentMessage,
  ): Promise<boolean> {
    const authorThreadId = this.threadIdForAgentPath(runtime, message.decoded.author)
    const recipientThreadId = this.threadIdForAgentPath(runtime, message.decoded.recipient)
    if (!authorThreadId || !recipientThreadId) {
      return false
    }
    const resolved = this.resolveNativeAgentMessage(runtime, message.decoded, message.receiverThreadId)
    if (!resolved) return true
    const observation = message.observation
    if (observation && !observation.scope.nativeAgentMessage) {
      observation.scope.nativeAgentMessage = resolved
      // Delayed registration uses the original native receipt, never the activity item's identity.
      try {
        for (const projected of projectNativeObservation(observation.message, observation.scope)) {
          this.emitExecutionObservation({ ...projected, phase: 'received', caused_by: observation.receipt })
        }
      } catch { /* Observation remains independent of final-output custody. */ }
    }
    const author = runtime.descendants.get(authorThreadId)
    if (message.decoded.kind !== 'FINAL_ANSWER' || !author?.admittedForMaterial) {
      return true
    }
    await this.observeMaterial(runtime, {
      materialKind: 'output',
      content: message.decoded.content,
      rootThreadId: runtime.threadId,
      parentThreadId: author.parentThreadId,
      childThreadId: author.threadId,
    })
    return true
  }

  private resolveNativeAgentMessage(
    runtime: GoalRuntime,
    message: DecodedNativeAgentMessage,
    receiverThreadId: string,
  ): CoreObservationScope['nativeAgentMessage'] | null {
    const senderThreadId = this.threadIdForAgentPath(runtime, message.author)
    const recipientThreadId = this.threadIdForAgentPath(runtime, message.recipient)
    if (!senderThreadId || !recipientThreadId || receiverThreadId !== recipientThreadId) return null
    return { message, senderThreadId, receiverThreadId: recipientThreadId }
  }

  private async drainPendingNativeAgentMessages(runtime: GoalRuntime): Promise<void> {
    while (runtime.pendingAgentMessages.length > 0) {
      const message = runtime.pendingAgentMessages[0]!
      if (!(await this.preserveNativeAgentMessage(runtime, message))) {
        return
      }
      runtime.pendingAgentMessages.shift()
    }
  }

  private async handleSubAgentActivityItem(message: any, binding: ThreadBinding): Promise<boolean> {
    const item = message?.params?.item
    const parentThreadId = message?.params?.threadId
    if (
      !item ||
      item.type !== 'subAgentActivity' ||
      typeof parentThreadId !== 'string' ||
      typeof item.agentThreadId !== 'string' ||
      typeof item.agentPath !== 'string' ||
      !item.agentPath
    ) {
      return false
    }
    let child: ThreadBinding | null
    if (item.kind === 'started') {
      child = this.registerDescendant(item.agentThreadId, parentThreadId)
    } else {
      child = this.findBinding(item.agentThreadId)
    }
    if (!child?.descendant || child.rootThreadId !== binding.rootThreadId) {
      return true
    }
    const parent = this.findBinding(child.descendant.parentThreadId)
    const parentAgentPath = parent?.descendant === null
      ? '/root'
      : parent?.descendant?.agentPath ?? null
    const pathOwner = this.descendantByAgentPath(binding.runtime, item.agentPath)
    if (
      !parentAgentPath ||
      this.directParentAgentPath(item.agentPath) !== parentAgentPath ||
      (pathOwner && pathOwner.threadId !== child.descendant.threadId) ||
      (child.descendant.agentPath && child.descendant.agentPath !== item.agentPath)
    ) {
      return true
    }
    child.descendant.agentPath = item.agentPath
    if (item.kind === 'interrupted') {
      child.descendant.status = 'interrupted'
      child.descendant.activeTurnId = null
      await this.revokeDescendantToolGrant(child, 'child_turn_interrupted')
    }
    await this.drainPendingNativeAgentMessages(binding.runtime)
    return true
  }

  private async handleRawAgentMessage(message: any, binding: ThreadBinding, observation: RuntimeObservationContext | null): Promise<void> {
    const item = message?.params?.item
    if (
      binding.descendant &&
      binding.descendant.admittedForMaterial &&
      item?.type === 'message' &&
      item.role === 'assistant' &&
      item.phase !== 'commentary' &&
      Array.isArray(item.content) &&
      item.content.length > 0 &&
      item.content.every(
        (part: any) => part?.type === 'output_text' && typeof part.text === 'string',
      )
    ) {
      const content = item.content.map((part: { text: string }) => part.text).join('\n')
      if (content) {
        await this.observeMaterial(binding.runtime, {
          materialKind: 'output',
          content,
          rootThreadId: binding.rootThreadId,
          parentThreadId: binding.descendant.parentThreadId,
          childThreadId: binding.descendant.threadId,
        })
      }
      return
    }
    const decoded = observation ? observation.rawAgentMessage : decodeNativeAgentMessage(item)
    if (!decoded) return
    const agentMessage = {
      decoded,
      receiverThreadId: message.params.threadId,
      observation,
    }
    if (!(await this.preserveNativeAgentMessage(binding.runtime, agentMessage)) && decoded.kind === 'FINAL_ANSWER') {
      binding.runtime.pendingAgentMessages.push(agentMessage)
    }
  }

  private async handleCollaborationItem(message: any, binding: ThreadBinding, observation: RuntimeObservationContext | null = null): Promise<void> {
    const item = message?.params?.item
    if (await this.handleSubAgentActivityItem(message, binding)) {
      return
    }
    if (!item || item.type !== 'collabAgentToolCall') {
      return
    }
    const senderThreadId =
      typeof item.senderThreadId === 'string' ? item.senderThreadId : message?.params?.threadId
    const receiverThreadIds = Array.isArray(item.receiverThreadIds)
      ? item.receiverThreadIds.filter((value: unknown): value is string => typeof value === 'string')
      : []
    if (!senderThreadId) {
      return
    }
    if (item.tool === 'spawnAgent') {
      if (binding.runtime.stopping) {
        return
      }
      const sender = this.findBinding(senderThreadId)
      if (
        (binding.descendant?.depth ?? 0) >= GOAL_DELEGATION_MAX_DEPTH ||
        (
          sender?.rootThreadId === binding.rootThreadId &&
          (sender.descendant?.depth ?? 0) >= GOAL_DELEGATION_MAX_DEPTH
        )
      ) {
        const error = new GoalDelegationDepthError({
          code: 'delegation_spawn_depth',
          actual: Math.max(binding.descendant?.depth ?? 0, sender?.descendant?.depth ?? 0),
          expected: GOAL_DELEGATION_MAX_DEPTH - 1,
          source_location: 'codex-thread-core/handleCollaborationItem:spawnAgent',
          containment: true,
        })
        this.rejectRuntimeObservation(observation, error)
        this.triggerFatalContainment(
          'mission_consistency_failure',
          error,
          observation,
        )
        return
      }
      if (
        !sender ||
        sender.rootThreadId !== binding.rootThreadId ||
        senderThreadId !== message?.params?.threadId
      ) {
        return
      }
      if (receiverThreadIds.length !== 1 || typeof item.prompt !== 'string') {
        return
      }
      const child = this.registerDescendant(receiverThreadIds[0], senderThreadId)
      if (!child?.descendant) {
        return
      }
      await this.observeMaterial(binding.runtime, {
        materialKind: 'assignment',
        content: item.prompt,
        rootThreadId: binding.rootThreadId,
        parentThreadId: child.descendant.parentThreadId,
        childThreadId: receiverThreadIds[0],
      })
      return
    }
    if (item.tool === 'sendInput' && typeof item.prompt === 'string') {
      if (binding.runtime.stopping) {
        return
      }
      for (const childThreadId of receiverThreadIds) {
        const child = this.findBinding(childThreadId)
        if (!child?.descendant || child.rootThreadId !== binding.rootThreadId) {
          continue
        }
        if (
          senderThreadId !== message?.params?.threadId
        ) {
          const error = new GoalDelegationParentError({
            code: 'delegation_send_input_parent',
            actual: [senderThreadId, String(message?.params?.threadId)],
            expected: String(message?.params?.threadId),
            source_location: 'codex-thread-core/handleCollaborationItem:sendInput',
            containment: true,
          })
          this.rejectRuntimeObservation(observation, error)
          this.triggerFatalContainment(
            'mission_consistency_failure',
            error,
            observation,
          )
          return
        }
        if (child.descendant.parentThreadId !== senderThreadId) continue
        await this.observeMaterial(binding.runtime, {
          materialKind: 'assignment',
          content: item.prompt,
          rootThreadId: binding.rootThreadId,
          parentThreadId: child.descendant.parentThreadId,
          childThreadId,
        })
      }
      return
    }
    if (item.tool === 'wait' && item.agentsStates && typeof item.agentsStates === 'object') {
      for (const childThreadId of receiverThreadIds) {
        const state = item.agentsStates[childThreadId]
        const child = this.findBinding(childThreadId)
        if (!child?.descendant || child.rootThreadId !== binding.rootThreadId) {
          continue
        }
        if (
          senderThreadId !== message?.params?.threadId
        ) {
          const error = new GoalDelegationParentError({
            code: 'delegation_wait_parent',
            actual: [senderThreadId, String(message?.params?.threadId)],
            expected: String(message?.params?.threadId),
            source_location: 'codex-thread-core/handleCollaborationItem:wait',
            containment: true,
          })
          this.rejectRuntimeObservation(observation, error)
          this.triggerFatalContainment(
            'mission_consistency_failure',
            error,
            observation,
          )
          return
        }
        if (!child.descendant.admittedForMaterial) {
          continue
        }
        if (state?.status !== 'completed' || typeof state.message !== 'string' || !state.message) {
          continue
        }
        await this.observeMaterial(binding.runtime, {
          materialKind: 'output',
          content: state.message,
          rootThreadId: binding.rootThreadId,
          parentThreadId: child.descendant.parentThreadId,
          childThreadId,
        })
      }
    }
  }

  private emitGoalEpochDiagnostic(
    runtime: GoalRuntime,
    input: GoalEpochDiagnosticEventInput,
  ): void {
    if (!this.options.onGoalEpochDiagnostic) {
      return
    }
    const event: GoalEpochDiagnosticEvent = {
      sequence: ++runtime.diagnostic.sequence,
      kind: input.kind,
      rootThreadId: runtime.threadId,
      activeTurnId: runtime.activeTurnId,
      turnId: input.turnId ?? null,
      priorGoalStatus: input.priorGoalStatus ?? null,
      newGoalStatus: input.newGoalStatus ?? null,
      transitionSource: input.transitionSource ?? null,
      transitionCategory: input.transitionCategory ?? null,
      appServerEventCategory: input.appServerEventCategory ?? null,
      errorCategory: input.errorCategory ?? null,
      errorSubtype: input.errorSubtype ?? null,
      errorClass: input.errorClass ?? null,
      errorCode: input.errorCode ?? null,
      errorWillRetry: input.errorWillRetry ?? null,
      errorAssociatedWithRootTurn: input.errorAssociatedWithRootTurn ?? null,
      sameTurnActivityAfterError: input.sameTurnActivityAfterError ?? null,
      sameTurnActivityAfterBlocked: input.sameTurnActivityAfterBlocked ?? null,
      compactionEventCategory: input.compactionEventCategory ?? null,
      blockedRecoveredWithoutHostIntervention:
        input.blockedRecoveredWithoutHostIntervention ?? null,
      rootTurnTerminalStatus: input.rootTurnTerminalStatus ?? null,
      hostTurnInterruptRequested: input.hostTurnInterruptRequested ?? false,
    }
    try {
      this.options.onGoalEpochDiagnostic(event)
    } catch (error) {
      this.logger.error(
        'bridge.boundary.goal_epoch_diagnostic_failed',
        boundaryErrorLogFields(error),
      )
    }
  }

  private observeGoalStatus(
    runtime: GoalRuntime,
    goal: ThreadGoal,
    source: GoalEpochDiagnosticTransitionSource,
    category: GoalEpochDiagnosticEvent['transitionCategory'],
    turnId: string | null,
  ): void {
    const priorStatus = runtime.goal?.status ?? null
    if (source === 'host_issued_mutation' && runtime.diagnostic.blockedTurnId !== null) {
      runtime.diagnostic.hostInterventionAfterBlocked = true
    }
    runtime.goal = goal
    if (priorStatus === goal.status) {
      const pendingReadback = runtime.diagnostic.pendingReadbackTransition
      if (
        source === 'app_server_notification' &&
        pendingReadback?.newStatus === goal.status
      ) {
        if (goal.status === 'blocked' && turnId !== null) {
          runtime.diagnostic.blockedTurnId = turnId
        }
        runtime.diagnostic.pendingReadbackTransition = null
        this.emitGoalEpochDiagnostic(runtime, {
          kind: 'goal_status_transition',
          turnId,
          priorGoalStatus: pendingReadback.priorStatus,
          newGoalStatus: pendingReadback.newStatus,
          transitionSource: source,
          transitionCategory: category,
          appServerEventCategory: 'thread/goal/updated',
        })
      }
      return
    }
    runtime.diagnostic.pendingReadbackTransition =
      source === 'readback'
        ? { priorStatus, newStatus: goal.status }
        : null
    if (goal.status === 'blocked') {
      runtime.diagnostic.blockedTurnId = turnId ?? runtime.activeTurnId
      runtime.diagnostic.sameTurnActivityAfterBlockedEmitted = false
      runtime.diagnostic.hostInterventionAfterBlocked = false
    }
    const recoveredWithoutHostIntervention =
      priorStatus === 'blocked' && goal.status !== 'blocked'
        ? source === 'host_issued_mutation'
          ? false
          : !runtime.diagnostic.hostInterventionAfterBlocked
        : null
    this.emitGoalEpochDiagnostic(runtime, {
      kind: 'goal_status_transition',
      turnId,
      priorGoalStatus: priorStatus,
      newGoalStatus: goal.status,
      transitionSource: source,
      transitionCategory: category,
      appServerEventCategory:
        source === 'app_server_notification' ? 'thread/goal/updated' : null,
      blockedRecoveredWithoutHostIntervention: recoveredWithoutHostIntervention,
    })
  }

  private observeRootDiagnosticNotification(message: any): void {
    const method = typeof message?.method === 'string' ? message.method : null
    if (!method) {
      return
    }
    const { threadId, turnId } = rootNotificationIdentity(message)
    const runtime = threadId ? this.activeGoals.get(threadId) : null
    if (!runtime) {
      return
    }
    if (method === 'error') {
      const errorInfo = safeCodexErrorInfo(message?.params?.error?.codexErrorInfo)
      runtime.diagnostic.errorTurnId = turnId
      runtime.diagnostic.sameTurnActivityAfterErrorEmitted = false
      this.emitGoalEpochDiagnostic(runtime, {
        kind: 'app_server_error',
        turnId,
        appServerEventCategory: method,
        errorCategory: 'app_server_turn_error',
        errorSubtype: errorInfo.subtype,
        errorClass: 'TurnError',
        errorCode: errorInfo.code,
        errorWillRetry:
          typeof message?.params?.willRetry === 'boolean' ? message.params.willRetry : null,
        errorAssociatedWithRootTurn:
          turnId !== null &&
          (turnId === runtime.activeTurnId || turnId === runtime.diagnostic.blockedTurnId),
      })
      return
    }
    const isItemLifecycle = method === 'item/started' || method === 'item/completed'
    if (isItemLifecycle && message?.params?.item?.type === 'contextCompaction') {
      this.emitGoalEpochDiagnostic(runtime, {
        kind: 'context_management',
        turnId,
        appServerEventCategory: method,
        compactionEventCategory:
          method === 'item/started'
            ? 'context_compaction_started'
            : 'context_compaction_completed',
      })
      return
    }
    if (method === 'thread/compacted') {
      this.emitGoalEpochDiagnostic(runtime, {
        kind: 'context_management',
        turnId,
        appServerEventCategory: method,
        compactionEventCategory: 'thread_compacted',
      })
      return
    }
    if (method === 'turn/completed') {
      const status = message?.params?.turn?.status
      this.emitGoalEpochDiagnostic(runtime, {
        kind: 'root_turn_terminal',
        turnId,
        appServerEventCategory: method,
        rootTurnTerminalStatus:
          status === 'failed' ? 'failed' : status === 'interrupted' ? 'interrupted' : 'completed',
      })
      return
    }
    if (!NORMAL_ROOT_ACTIVITY_METHODS.has(method) && !isItemLifecycle) {
      return
    }
    const afterError =
      turnId !== null &&
      turnId === runtime.diagnostic.errorTurnId &&
      !runtime.diagnostic.sameTurnActivityAfterErrorEmitted
    const afterBlocked =
      turnId !== null &&
      turnId === runtime.diagnostic.blockedTurnId &&
      !runtime.diagnostic.sameTurnActivityAfterBlockedEmitted
    if (!afterError && !afterBlocked) {
      return
    }
    runtime.diagnostic.sameTurnActivityAfterErrorEmitted ||= afterError
    runtime.diagnostic.sameTurnActivityAfterBlockedEmitted ||= afterBlocked
    this.emitGoalEpochDiagnostic(runtime, {
      kind: 'same_turn_activity_after_transition',
      turnId,
      appServerEventCategory: method,
      sameTurnActivityAfterError: afterError ? true : null,
      sameTurnActivityAfterBlocked: afterBlocked ? true : null,
    })
  }

  protected async handleNotification(message: any, observation: RuntimeObservationContext | null = null): Promise<void> {
    const method = typeof message?.method === 'string' ? message.method : null
    if (!method || this.activeGoals.size === 0) {
      return
    }
    this.observeRootDiagnosticNotification(message)
    if (method === 'model/rerouted' || method === 'thread/settings/updated') {
      const threadId = message?.params?.threadId
      const binding = typeof threadId === 'string' ? this.findBinding(threadId) : null
      if (!binding) {
        return
      }
      const runtime = binding.runtime
      const settings = message?.params?.threadSettings
      if (
        method === 'model/rerouted' ||
        effectiveModelDrift(
          settings,
          runtime.expectedModelProvider,
          runtime.expectedModel,
          runtime.reasoningEffort,
        )
      ) {
        this.scheduleStop(binding.rootThreadId, 'model_contract_violation')
      }
      return
    }
    if (method === 'thread/started') {
      const thread = message?.params?.thread
      const spawnSource = thread?.source?.subAgent?.thread_spawn
      const sourceParent = spawnSource?.parent_thread_id
      const parentThreadId = thread?.parentThreadId ?? sourceParent
      if (typeof thread?.id === 'string' && typeof parentThreadId === 'string') {
        if (typeof sourceParent === 'string' && sourceParent !== parentThreadId) {
          if (this.findBinding(thread.id) || this.findBinding(parentThreadId) || this.findBinding(sourceParent)) {
            throw new GoalEpochMissionConsistencyError('native child source disagrees with its Goal parent')
          }
          return
        }
        const binding = this.registerDescendant(thread.id, parentThreadId)
        if (binding?.descendant && typeof sourceParent === 'string') {
          const sourceRole = spawnSource.agent_role ?? null
          const projectedRole = thread.agentRole === undefined ? sourceRole : thread.agentRole
          if (
            (sourceRole !== null && (typeof sourceRole !== 'string' || !sourceRole || sourceRole.trim() !== sourceRole)) ||
            projectedRole !== sourceRole
          ) {
            throw new GoalEpochMissionConsistencyError('native child source disagrees with its agent role')
          }
          const priorRole = binding.descendant.nativeAgentRole
          if (priorRole !== undefined && priorRole !== sourceRole) {
            throw new GoalEpochMissionConsistencyError('native child identity changed its agent role')
          }
          binding.descendant.nativeAgentRole = sourceRole
        }
      }
      return
    }
    if (method === 'turn/started') {
      const threadId = message?.params?.threadId
      const turnId = extractTurnId(message?.params)
      if (typeof threadId !== 'string' || !turnId) {
        return
      }
      const binding = this.findBinding(threadId)
      if (!binding) {
        return
      }
      if (binding.descendant) {
        binding.descendant.activeTurnId = turnId
        binding.descendant.status = 'active'
      } else {
        binding.runtime.activeTurnId = turnId
        if (!binding.runtime.stopping) {
          binding.runtime.acceptingTools = true
        }
        binding.runtime.resolveFirstTurn(turnId)
      }
      this.observeWorkerState(binding, 'status', 'active')
      return
    }
    if (method === 'turn/completed') {
      const threadId = message?.params?.threadId
      if (typeof threadId !== 'string') {
        return
      }
      const binding = this.findBinding(threadId)
      if (!binding) {
        return
      }
      const status = message?.params?.turn?.status
      if (binding.descendant) {
        binding.descendant.activeTurnId = null
        binding.descendant.status =
          status === 'failed'
            ? 'failed'
            : status === 'interrupted'
              ? 'interrupted'
              : 'complete'
        await this.revokeDescendantToolGrant(
          binding,
          binding.descendant.status === 'failed'
            ? 'child_turn_failed'
            : binding.descendant.status === 'interrupted'
              ? 'child_turn_interrupted'
              : 'child_turn_completed',
        )
      } else {
        binding.runtime.activeTurnId = null
        binding.runtime.acceptingTools = false
      }
      this.observeWorkerState(binding, 'status', status === 'failed' ? 'failed' : status === 'interrupted' ? 'interrupted' : 'complete')
      return
    }
    if (method === 'thread/goal/updated') {
      const threadId = message?.params?.threadId
      const runtime = typeof threadId === 'string' ? this.activeGoals.get(threadId) : null
      if (!runtime) {
        return
      }
      const goal = coerceThreadGoal(message?.params?.goal)
      if (goal.threadId !== runtime.threadId || goal.objective !== runtime.expectedObjective) {
        throw new GoalEpochMissionConsistencyError('Goal update changed the bounded objective', undefined, {
          code: goal.threadId !== runtime.threadId ? 'goal_update_thread_binding' : 'goal_update_objective_binding',
          actual: goal.threadId !== runtime.threadId ? goal.threadId : 'changed',
          expected: goal.threadId !== runtime.threadId ? runtime.threadId : 'unchanged',
          source_location: 'codex-thread-core/handleNotification:thread/goal/updated', containment: true,
        })
      }
      this.observeGoalStatus(
        runtime,
        goal,
        'app_server_notification',
        'thread/goal/updated',
        extractTurnId(message?.params),
      )
      runtime.acceptingTools =
        !runtime.stopping &&
        runtime.activeTurnId !== null &&
        (goal.status === 'active' || goal.status === 'blocked')
      if (goal.status === 'usageLimited') {
        this.scheduleSuspension(runtime.threadId)
      }
      return
    }
    if (method === 'item/started' || method === 'item/completed') {
      const threadId = message?.params?.threadId
      const binding = typeof threadId === 'string' ? this.findBinding(threadId) : null
      if (!binding) {
        return
      }
      await this.handleCollaborationItem(message, binding, observation)
      return
    }
    if (method === 'rawResponseItem/completed') {
      const threadId = message?.params?.threadId
      const binding = typeof threadId === 'string' ? this.findBinding(threadId) : null
      if (!binding) {
        return
      }
      await this.handleRawAgentMessage(message, binding, observation)
      return
    }
    if (method === 'thread/status/changed' || method === 'thread/archived') {
      const threadId = message?.params?.threadId
      const binding = typeof threadId === 'string' ? this.findBinding(threadId) : null
      if (!binding) {
        return
      }
      if (binding.descendant) {
        if (method === 'thread/archived') {
          binding.descendant.status = 'archived'
          binding.descendant.activeTurnId = null
          await this.revokeDescendantToolGrant(binding, 'child_archived')
        } else if (
          message?.params?.status === 'active' ||
          message?.params?.status?.type === 'active'
        ) {
          binding.descendant.status = 'active'
        } else {
          const statusType =
            typeof message?.params?.status === 'string'
              ? message.params.status
              : message?.params?.status?.type
          if (statusType === 'archived') {
            binding.descendant.status = 'archived'
            binding.descendant.activeTurnId = null
            await this.revokeDescendantToolGrant(binding, 'child_archived')
          } else if (statusType === 'failed') {
            binding.descendant.status = 'failed'
            binding.descendant.activeTurnId = null
            await this.revokeDescendantToolGrant(binding, 'child_turn_failed')
          } else if (statusType === 'interrupted') {
            binding.descendant.status = 'interrupted'
            binding.descendant.activeTurnId = null
            await this.revokeDescendantToolGrant(binding, 'child_turn_interrupted')
          }
        }
      } else {
        binding.runtime.threadStatus = message?.params?.status ?? null
      }
      const observedStatus = method === 'thread/archived' ? 'archived' :
        typeof message?.params?.status === 'string' ? message.params.status : message?.params?.status?.type
      this.observeWorkerState(binding, 'status', typeof observedStatus === 'string' ? observedStatus : 'unknown')
      return
    }
  }

  protected async handleServerRequest(message: any, observation: RuntimeObservationContext | null = null): Promise<void> {
    if (message?.method !== 'item/tool/call') {
      const error = new Error('unsupported server request')
      this.rejectRuntimeObservation(observation, error, { code: 'unsupported_server_request', containment: true })
      this.triggerFatalContainment('unknown_effect', error, observation)
      this.respondToServerRequest(message.id, {
        success: false,
        contentItems: [{ type: 'inputText', text: 'unsupported server request' }],
      })
      return
    }
    let call: DynamicToolCall
    try {
      call = coerceDynamicToolCall(message?.params)
    } catch {
      this.rejectRuntimeObservation(observation, new Error('invalid dynamic tool request'), {
        code: 'dynamic_tool_request_shape', containment: false,
      })
      this.respondToServerRequest(message.id, {
        success: false,
        contentItems: [{ type: 'inputText', text: 'invalid dynamic tool request' }],
      })
      return
    }
    const binding = this.findBinding(call.threadId)
    const dynamicToolHandler = this.options.dynamicToolHandler
    const descendant = binding?.descendant ?? null
    const delegatedGrant = descendant?.toolGrant ?? null
    const rootAuthorized = Boolean(
      binding &&
      !descendant &&
      binding.runtime.acceptingTools &&
      binding.runtime.activeTurnId === call.turnId &&
      !DESCENDANT_TOOL_NAME_SET.has(call.tool) &&
      binding.runtime.toolNames.has(call.tool) &&
      dynamicToolHandler,
    )
    const descendantAuthorized = Boolean(
      binding &&
      descendant &&
      delegatedGrant &&
      !binding.runtime.stopping &&
      descendant.depth === 1 &&
      descendant.parentThreadId === binding.rootThreadId &&
      descendant.status === 'active' &&
      descendant.activeTurnId === call.turnId &&
      DESCENDANT_TOOL_NAME_SET.has(call.tool) &&
      delegatedGrant.allowedToolNames.has(call.tool) &&
      binding.runtime.toolNames.has(call.tool) &&
      dynamicToolHandler,
    )
    if (!binding || (!rootAuthorized && !descendantAuthorized)) {
      this.rejectRuntimeObservation(observation, new Error('dynamic tool call is outside the active Goal authority'), {
        code: 'dynamic_tool_authority', containment: false,
      })
      this.respondToServerRequest(message.id, {
        success: false,
        contentItems: [{ type: 'inputText', text: 'dynamic tool call is outside the active Goal authority' }],
      })
      return
    }
    const key = binding.rootThreadId + '\u0000' + call.threadId + '\u0000' + call.callId
    const fingerprint = stableJson({
      threadId: call.threadId,
      tool: call.tool,
      namespace: call.namespace,
      arguments: call.arguments,
      grantId: delegatedGrant?.grantId ?? null,
      assignmentId: delegatedGrant?.assignmentId ?? null,
    })
    const cached = this.dynamicToolCalls.get(key)
    if (cached && cached.fingerprint !== fingerprint) {
      this.rejectRuntimeObservation(observation, new Error('dynamic tool call id changed on replay'), {
        code: 'dynamic_tool_replay_identity', actual: call.callId, containment: !descendant,
      })
      if (!descendant) {
        this.triggerFatalContainment(
          'unknown_effect',
          new Error('dynamic tool call id changed after an effect may have occurred'),
          observation,
        )
      }
      this.respondToServerRequest(message.id, {
        success: false,
        contentItems: [{ type: 'inputText', text: 'dynamic tool call id changed on replay' }],
      })
      return
    }
    const operationSignal =
      delegatedGrant?.controller.signal ?? binding.runtime.dynamicOperationController.signal
    const resultPromise: Promise<DynamicToolCallResult> =
      cached?.result ??
      dynamicToolHandler!(
          call,
          this.operationContext(binding, call.turnId, operationSignal),
        )
        .then(coerceDynamicToolResult)
        .catch((error): DynamicToolCallResult => {
          if (!descendant && !operationSignal.aborted) {
            this.rejectRuntimeObservation(observation, error, { code: 'dynamic_tool_handler_failed', containment: true })
            this.triggerFatalContainment('unknown_effect', error, observation)
          }
          return {
            success: false,
            contentItems: [
              {
                type: 'inputText' as const,
                text: error instanceof Error ? error.message : String(error),
              },
            ],
          }
        })
    if (!cached) {
      this.dynamicToolCalls.set(key, { fingerprint, result: resultPromise })
    }
    let result = await resultPromise
    if (
      descendant &&
      (
        descendant.toolGrant !== delegatedGrant ||
        descendant.status !== 'active' ||
        descendant.activeTurnId !== call.turnId ||
        binding.runtime.stopping
      )
    ) {
      result = {
        success: false,
        contentItems: [{
          type: 'inputText',
          text: `descendant ${descendantToolGrantLabel(delegatedGrant!.family)} grant is no longer active`,
        }],
      }
    }
    const terminalRuntime =
      !descendant &&
      result.terminalHandoff === 'owner_checkpoint' &&
      this.activeGoals.get(binding.rootThreadId) === binding.runtime
        ? binding.runtime
        : null
    if (terminalRuntime) {
      // This runs while the checkpoint request still owns the serialized message
      // queue. It closes later tool/material admission before the success response
      // can let the root issue fresh native collaboration work.
      await this.closeAdmission(terminalRuntime, 'root_checkpoint')
    }
    try {
      this.emitExecutionObservation({ phase: 'processed', kind: 'tool', source_method: 'core/dynamic_tool/response',
        received_at: new Date().toISOString(), source_time: null, caused_by: observation?.receipt ?? null,
        identity: { root_thread_id: binding.rootThreadId, thread_id: call.threadId,
          parent_thread_id: descendant?.parentThreadId ?? null, turn_id: call.turnId, item_id: null, operation_id: call.callId },
        summary: 'Owner tool response supplied',
        details: { tool_name: call.tool, server: call.namespace ?? null, status: 'returned', success: result.success, output_mode: 'completion_only' },
        contents: result.contentItems.flatMap((content, index) => content.type === 'inputText' ? [{
          content_key: `${this.observationSourceId}:${call.threadId}:${call.callId}:result:${index}`,
          field: 'result' as const, text: content.text, update_mode: 'snapshot' as const, complete: true,
        }] : []),
      })
      this.respondToServerRequest(message.id, {
        success: result.success,
        contentItems: result.contentItems,
      })
    } finally {
      if (terminalRuntime) {
        // Do not await containment here: stop drains this same serialized queue.
        this.scheduleStop(binding.rootThreadId, 'owner_checkpoint')
      }
    }
  }

  private triggerFatalContainment(reason: GoalEpochStopReason, error: unknown, observation: RuntimeObservationContext | null = null): void {
    if (this.fatalBoundaryError) {
      return
    }
    const message = error instanceof Error ? error.message : String(error)
    const goals = [...this.activeGoals.keys()]
    this.observeBoundaryLifecycle('containment', 'started', observation?.scope.rootThreadId ?? null, observation?.receipt ?? null)
    this.logger.error('bridge.boundary.fatal_condition', {
      reason,
      ...boundaryErrorLogFields(error),
    })
    this.fatalFencePromise = (async () => {
      for (const threadId of goals) {
        await this.stopBoundedGoalEpoch(threadId, reason)
      }
      this.fatalBoundaryError = 'Goal boundary contained a fatal condition: ' + message
      this.readyReason = this.fatalBoundaryError
      this.ready = false
      this.observeBoundaryLifecycle('containment', 'completed', observation?.scope.rootThreadId ?? null, observation?.receipt ?? null)
    })().catch((containmentError) => {
      this.observeBoundaryLifecycle('containment_failed', 'failed', observation?.scope.rootThreadId ?? null, observation?.receipt ?? null, containmentError)
      this.fatalFenceError = new AggregateError(
        [error, containmentError],
        'Goal fatal containment failed after an original boundary failure',
      )
      this.fatalBoundaryError = this.fatalFenceError.message
      this.readyReason = this.fatalBoundaryError
      this.ready = false
    })
  }

  private scheduleStop(threadId: string, reason: GoalEpochStopReason): void {
    const runtime = this.activeGoals.get(threadId)
    if (!runtime || runtime.stopPromise) {
      return
    }
    void this.stopBoundedGoalEpoch(threadId, reason).catch((error) => {
      this.triggerFatalContainment('boundary_failure', error)
    })
  }

  private scheduleSuspension(threadId: string): void {
    const runtime = this.activeGoals.get(threadId)
    if (!runtime || runtime.stopPromise) {
      return
    }
    void this.suspendBoundedGoalEpoch(threadId).catch((error) => {
      this.triggerFatalContainment('boundary_failure', error)
    })
  }

  private containmentScope(reason: GoalEpochStopReason): GoalEpochContainmentScope {
    return ['shared_authority_loss', 'unknown_effect', 'mission_consistency_failure'].includes(reason)
      ? 'mission_fence'
      : 'goal_local'
  }

  private stopContext(
    threadId: string,
    turnId: string | null,
    reason: GoalEpochStopReason,
  ): GoalEpochStopContext {
    return { threadId, turnId, reason, containmentScope: this.containmentScope(reason) }
  }

  private async invokeScopedStopCallback(context: GoalEpochStopContext): Promise<void> {
    const callback =
      context.containmentScope === 'mission_fence'
        ? this.options.onMissionFence
        : this.options.onGoalTermination
    if (!callback) {
      throw new Error('bounded Goal stop callback is unavailable')
    }
    await callback(context)
  }

  private validateBoundedGoalEpochRequest(
    request: BoundedGoalEpochRequest,
  ): ValidatedBoundedGoalEpochRequest {
    const policy = this.options.goalPolicy
    if (!policy) {
      throw new Error('bounded Goal requires a process-wide Goal policy')
    }
    const normalizedPolicy = normalizeGoalProcessConfig(policy, this.workspaceRoot)
    if (this.options.restartOnUnexpectedExit !== false) {
      throw new Error('bounded Goal App Server restart must be disabled')
    }
    if (!this.options.onGoalTermination) {
      throw new Error('bounded Goal requires a Goal-local termination callback')
    }
    if (!this.options.onMissionFence) {
      throw new Error('bounded Goal requires a Mission-fence callback')
    }
    const objective = requireNonEmpty(request.objective, 'objective')
    const instructions = resolveGoalEpochInstructions(request, normalizedPolicy.readOnlyRoots)
    const workspaceRoot = path.resolve(this.workspaceRoot)
    const nativeAgentRoles = normalizeNativeAgentRoles(
      request.nativeAgentRoles,
      normalizedPolicy.readOnlyRoots,
    )
    if (request.initialContextText !== undefined) {
      requireNonEmpty(request.initialContextText, 'initialContextText')
    }
    const initialContextText = request.initialContextText ?? null
    const dynamicTools = normalizeDynamicTools(request.dynamicTools)
    const environments = normalizeEnvironmentSelections(
      request.environments,
      workspaceRoot,
    )
    const trustedMcpServerIds = normalizeTrustedCapabilityIds(
      this.options.trustedMcpServerIds,
      'trustedMcpServerIds',
    )
    const trustedAppIds = normalizeTrustedCapabilityIds(
      this.options.trustedAppIds,
      'trustedAppIds',
    )
    const selectedCapabilities = normalizeCapabilitySelections(
      request.selectedCapabilities,
      environments,
      normalizedPolicy.readOnlyRoots,
      workspaceRoot,
      normalizedPolicy.protectedRoot,
      trustedMcpServerIds,
      trustedAppIds,
    )
    if (
      !normalizedPolicy.networkAccess &&
      (selectedCapabilities.localRoots.length > 0 ||
        selectedCapabilities.mcpServers.length > 0 ||
        selectedCapabilities.apps.length > 0 ||
        selectedCapabilities.browser !== null)
    ) {
      throw new Error('offline bounded Goal requires empty selectedCapabilities')
    }
    if (dynamicTools.length > 0 && !this.options.dynamicToolHandler) {
      throw new Error('bounded Goal dynamic tools require one handler')
    }
    return {
      objective,
      developerInstructions: instructions.developerInstructions,
      readOnlyRoots: normalizedPolicy.readOnlyRoots,
      permissionProfileId: normalizedPolicy.permissionProfileId,
      expectedModelProvider: normalizedPolicy.expectedModelProvider,
      expectedModel: normalizedPolicy.expectedModel,
      reasoningEffort: normalizedPolicy.reasoningEffort,
      networkAccess: normalizedPolicy.networkAccess,
      nativeAgentRoles,
      initialContextText,
      dynamicTools,
      environments,
      selectedCapabilities,
      trustedMcpServerIds,
      trustedAppIds,
      workspaceRoot,
    }
  }

  private createGoalRuntime(
    threadId: string,
    request: ValidatedBoundedGoalEpochRequest,
    threadStatus: unknown,
    goal: ThreadGoal | null,
  ): GoalRuntime {
    const firstTurn = deferred<string>()
    void firstTurn.promise.catch(() => undefined)
    return {
      threadId,
      workspaceRoot: request.workspaceRoot,
      expectedObjective: request.objective,
      expectedModelProvider: request.expectedModelProvider,
      expectedModel: request.expectedModel,
      reasoningEffort: request.reasoningEffort,
      goal,
      threadStatus,
      activeTurnId: null,
      toolNames: new Set(request.dynamicTools.map((tool) => tool.name)),
      acceptingTools: false,
      stopping: false,
      dynamicOperationController: new AbortController(),
      materialOperationController: new AbortController(),
      stopPromise: null,
      transitionKind: null,
      firstTurn: firstTurn.promise,
      resolveFirstTurn: firstTurn.resolve,
      rejectFirstTurn: firstTurn.reject,
      descendants: new Map(),
      pendingAgentMessages: [],
      materials: new Map(),
      pendingMaterials: new Map(),
      diagnostic: {
        sequence: 0,
        blockedTurnId: null,
        errorTurnId: null,
        sameTurnActivityAfterBlockedEmitted: false,
        sameTurnActivityAfterErrorEmitted: false,
        hostInterventionAfterBlocked: false,
        pendingReadbackTransition: null,
      },
    }
  }

  private async containCancelledResume(
    threadId: string,
    request: ValidatedBoundedGoalEpochRequest,
    threadStatus: unknown,
    goal: ThreadGoal | null,
    signal: AbortSignal,
  ): Promise<never> {
    const cancellation = cancellationError(signal, 'suspended Goal thread/resume reconciliation')
    const runtime = this.createGoalRuntime(threadId, request, threadStatus, goal)
    this.suspendedStates.delete(threadId)
    this.suspendedObjectives.delete(threadId)
    this.suspensionErrors.delete(threadId)
    this.activeGoals.set(threadId, runtime)
    this.observeWorkerState({ rootThreadId: threadId, runtime, descendant: null }, 'registered')
    try {
      await this.containCancelledGoal(threadId, signal)
    } catch (error) {
      throw new AggregateError(
        [cancellation, error],
        'cancelled suspended Goal resume received its durable reply but containment failed',
      )
    }
    throw cancellation
  }

  private containCancelledGoal(
    threadId: string,
    signal: AbortSignal,
  ): Promise<GoalEpochStopResult> {
    return isExplicitForceStop(signal)
      ? this.stopBoundedGoalEpoch(threadId, 'explicit_force_stop')
      : this.suspendBoundedGoalEpoch(threadId, undefined, 'operator_stop')
  }

  private async injectInitialGoalContext(
    threadId: string,
    text: string | null,
    signal?: AbortSignal,
  ): Promise<void> {
    if (text === null) return
    throwIfCancelled(signal, 'before initial Goal context')
    // Native 0.153.4 records and flushes these history items without starting a
    // turn. Drain the mutation reply; do not retry an uncertain append.
    try {
      await this.request('thread/inject_items', {
        threadId,
        items: [{
          type: 'message',
          role: 'assistant',
          content: [{ type: 'output_text', text }],
        }],
      })
    } catch (error) {
      // A native rollout flush can fail after history changed. Preserve this
      // uncertainty separately from cancellation after an acknowledged append.
      throw new GoalEpochInitialContextAppendError(error)
    }
    throwIfCancelled(signal, 'initial Goal context reconciliation')
  }

  async startBoundedGoalEpoch(
    requestInput: BoundedGoalEpochRequest,
    signal?: AbortSignal,
  ): Promise<BoundedGoalEpochIdentity> {
    throwIfCancelled(signal, 'before thread/start')
    const request = this.validateBoundedGoalEpochRequest(requestInput)
    if (this.activeGoals.size !== 0 || this.suspendedStates.size !== 0) {
      throw new Error('this dedicated boundary already owns an active or suspended bounded Goal')
    }
    await this.ensureReady(signal)
    throwIfCancelled(signal, 'before thread/start')
    const selectedCapabilityRoots = request.selectedCapabilities.localRoots.map(
      ({ id, location }) => ({ id, location }),
    )
    let started: any
    try {
      // thread/start is a durable mutation. Once sent, explicit cancellation waits for
      // its reply so the exact returned thread can be contained rather than forgotten.
      started = await this.request('thread/start', {
        cwd: request.workspaceRoot,
        approvalPolicy: 'never',
        approvalsReviewer: 'user',
        developerInstructions: request.developerInstructions,
        model: request.expectedModel,
        modelProvider: request.expectedModelProvider,
        allowProviderModelFallback: false,
        config: {
          ...Object.fromEntries(
            Object.entries(goalThreadFeatureConfig(request.selectedCapabilities, request.networkAccess)).map(([name, enabled]) => [
              'features.' + name,
              enabled,
            ]),
          ),
          ...goalDelegationThreadConfig(),
          ...nativeAgentRoleThreadConfig(request.nativeAgentRoles),
          ...goalCapabilityThreadConfig(
            request.selectedCapabilities,
            request.trustedMcpServerIds,
            request.trustedAppIds,
          ),
          'tools.experimental_request_user_input.enabled': false,
          web_search: request.networkAccess ? 'live' : 'disabled',
        },
        dynamicTools: request.dynamicTools,
        environments: request.environments,
        selectedCapabilityRoots,
        runtimeWorkspaceRoots: [request.workspaceRoot],
        permissions: request.permissionProfileId,
        experimentalRawEvents: true,
        ephemeral: false,
      })
    } catch (error) {
      if (signal?.aborted) {
        throw new GoalEpochStartEffectUnknownError(signal.reason, { cause: error })
      }
      throw error
    }
    const result = extractThreadResult(started)
    const threadId = extractThreadId(result)
    if (!threadId) {
      if (signal?.aborted) {
        throw new GoalEpochStartEffectUnknownError(signal.reason)
      }
      throw new Error('thread/start returned no root thread id')
    }
    if (
      path.resolve(String(result.cwd)) !== request.workspaceRoot ||
      result.activePermissionProfile?.id !== request.permissionProfileId ||
      result.sandbox?.networkAccess !== request.networkAccess ||
      effectiveModelDrift(
        result,
        request.expectedModelProvider,
        request.expectedModel,
        request.reasoningEffort,
      )
    ) {
      await this.request('thread/archive', { threadId }).catch(() => undefined)
      throw new Error('thread/start did not preserve root Goal containment and model contract')
    }
    if (signal?.aborted) {
      try {
        await this.request('thread/archive', { threadId })
      } catch (error) {
        throw new GoalEpochStartEffectUnknownError(signal.reason, { cause: error })
      }
      throw cancellationError(signal, 'thread/start reconciliation')
    }
    const runtime = this.createGoalRuntime(
      threadId,
      request,
      result.thread?.status ?? null,
      null,
    )
    this.activeGoals.set(threadId, runtime)
    this.observeWorkerState({ rootThreadId: threadId, runtime, descendant: null }, 'registered')
    let signalStopPromise: Promise<GoalEpochStopResult> | null = null
    try {
      if (this.options.onGoalEpochIdentityMaterialized) {
        await this.options.onGoalEpochIdentityMaterialized({
          threadId,
          workspaceRoot: request.workspaceRoot,
        })
      }
      throwIfCancelled(signal, 'materialized Goal identity')
      await this.injectInitialGoalContext(threadId, request.initialContextText, signal)
      // thread/goal/set is also a mutation. Drain its exact reply before acting on
      // cancellation so a late activation cannot race after local containment.
      const goalResponse = await this.request('thread/goal/set', {
        threadId,
        objective: request.objective,
        status: 'active',
      })
      const goal = coerceThreadGoal(extractThreadResult(goalResponse).goal)
      if (
        goal.threadId !== threadId ||
        goal.objective !== request.objective ||
        goal.status !== 'active'
      ) {
        throw new Error('thread/goal/set did not preserve the bounded Goal objective')
      }
      this.observeGoalStatus(
        runtime,
        goal,
        'host_issued_mutation',
        'thread/goal/set',
        null,
      )
      if (signal?.aborted) {
        await this.containCancelledGoal(threadId, signal)
        throw cancellationError(signal, 'Goal activation reconciliation')
      }
      const requestSignalStop = (): void => {
        signalStopPromise ??= this.containCancelledGoal(threadId, signal!)
        void signalStopPromise.catch(() => undefined)
      }
      signal?.addEventListener('abort', requestSignalStop, { once: true })
      if (signal?.aborted) {
        requestSignalStop()
      }
      try {
        await runtime.firstTurn
        if (signal?.aborted) {
          requestSignalStop()
          await signalStopPromise
          throw cancellationError(signal, 'first Goal turn')
        }
      } finally {
        signal?.removeEventListener('abort', requestSignalStop)
      }
      return { threadId }
    } catch (error) {
      let containmentError: unknown = null
      try {
        await (
          signalStopPromise ??
          (signal?.aborted && runtime.goal !== null
            ? this.containCancelledGoal(threadId, signal)
            : this.stopBoundedGoalEpoch(
                threadId,
                signal?.aborted && isExplicitForceStop(signal)
                  ? 'explicit_force_stop'
                  : 'turn_start_failure',
              ))
        )
      } catch (stopError) {
        containmentError = stopError
      }
      if (containmentError) {
        throw new AggregateError(
          [error, containmentError],
          'bounded Goal start failed and its known root could not be reconciled',
        )
      }
      if (signal?.aborted) {
        throw cancellationError(signal, 'known Goal containment')
      }
      throw error
    }
  }

  async resumeBoundedGoalEpoch(
    threadIdInput: string,
    requestInput: BoundedGoalEpochRequest,
    signal?: AbortSignal,
  ): Promise<BoundedGoalEpochIdentity> {
    throwIfCancelled(signal, 'before thread/resume')
    const threadId = threadIdInput.trim()
    if (!threadId) {
      throw new Error('suspended Goal threadId must be non-empty')
    }
    const request = this.validateBoundedGoalEpochRequest(requestInput)
    if (this.activeGoals.size !== 0) {
      throw new Error('this dedicated boundary already owns an active bounded Goal')
    }
    if (this.suspendedStates.size !== 0 && !this.suspendedStates.has(threadId)) {
      throw new Error('this dedicated boundary already owns another suspended bounded Goal')
    }
    if (this.terminalStates.has(threadId)) {
      throw new Error('a terminal bounded Goal cannot be resumed')
    }
    if (this.suspensionErrors.has(threadId)) {
      throw new Error('suspended Goal has not completed safe suspension reconciliation')
    }
    await this.ensureReady(signal)
    throwIfCancelled(signal, 'before thread/resume')
    // thread/resume is a durable mutation. Drain its exact reply before acting
    // on cancellation so a late resume cannot escape local reconciliation.
    const resumed = await this.request(
      'thread/resume',
      {
        threadId,
        model: request.expectedModel,
        modelProvider: request.expectedModelProvider,
        cwd: request.workspaceRoot,
        runtimeWorkspaceRoots: [request.workspaceRoot],
        approvalPolicy: 'never',
        approvalsReviewer: 'user',
        permissions: request.permissionProfileId,
        config: {
          ...Object.fromEntries(
            Object.entries(goalThreadFeatureConfig(request.selectedCapabilities, request.networkAccess)).map(([name, enabled]) => [
              'features.' + name,
              enabled,
            ]),
          ),
          ...goalDelegationThreadConfig(),
          ...nativeAgentRoleThreadConfig(request.nativeAgentRoles),
          ...goalCapabilityThreadConfig(
            request.selectedCapabilities,
            request.trustedMcpServerIds,
            request.trustedAppIds,
          ),
          'tools.experimental_request_user_input.enabled': false,
          web_search: request.networkAccess ? 'live' : 'disabled',
        },
        developerInstructions: request.developerInstructions,
        excludeTurns: true,
      },
    )
    if (signal?.aborted) {
      await this.containCancelledResume(
        threadId,
        request,
        null,
        this.suspendedStates.get(threadId)?.goal ?? null,
        signal,
      )
    }
    const result = extractThreadResult(resumed)
    const resumedWorkspaceRoots = Array.isArray(result.runtimeWorkspaceRoots)
      ? result.runtimeWorkspaceRoots
      : []
    if (
      extractThreadId(result) !== threadId ||
      result.thread?.parentThreadId !== null ||
      result.thread?.ephemeral !== false ||
      path.resolve(String(result.cwd)) !== request.workspaceRoot ||
      resumedWorkspaceRoots.length !== 1 ||
      path.resolve(String(resumedWorkspaceRoots[0])) !== request.workspaceRoot ||
      result.approvalPolicy !== 'never' ||
      result.approvalsReviewer !== 'user' ||
      result.activePermissionProfile?.id !== request.permissionProfileId ||
      result.sandbox?.networkAccess !== request.networkAccess ||
      result.modelProvider !== request.expectedModelProvider ||
      result.model !== request.expectedModel ||
      result.reasoningEffort !== request.reasoningEffort ||
      effectiveModelDrift(
        result,
        request.expectedModelProvider,
        request.expectedModel,
        request.reasoningEffort,
      )
    ) {
      throw new Error('thread/resume did not preserve root Goal containment and model contract')
    }
    const goalResponse = await this.request(
      'thread/goal/get',
      { threadId },
    )
    const rawGoal = extractThreadResult(goalResponse).goal
    const goal = rawGoal == null ? null : coerceThreadGoal(rawGoal)
    if (
      !goal ||
      goal.threadId !== threadId ||
      goal.objective !== request.objective ||
      !['usageLimited', 'paused'].includes(goal.status)
    ) {
      throw new Error('thread/resume did not recover the exact suspended Goal')
    }
    if (signal?.aborted) {
      await this.containCancelledResume(
        threadId,
        request,
        result.thread?.status ?? null,
        goal,
        signal,
      )
    }
    const runtime = this.createGoalRuntime(
      threadId,
      request,
      result.thread?.status ?? null,
      goal,
    )
    this.suspendedStates.delete(threadId)
    this.suspendedObjectives.delete(threadId)
    this.suspensionErrors.delete(threadId)
    this.activeGoals.set(threadId, runtime)
    this.observeWorkerState({ rootThreadId: threadId, runtime, descendant: null }, 'registered')
    let signalStopPromise: Promise<GoalEpochStopResult> | null = null
    try {
      await this.injectInitialGoalContext(threadId, request.initialContextText, signal)
      const activatedResponse = await this.request('thread/goal/set', { threadId, status: 'active' })
      const activated = coerceThreadGoal(extractThreadResult(activatedResponse).goal)
      if (
        activated.threadId !== threadId ||
        activated.objective !== request.objective ||
        activated.status !== 'active'
      ) {
        throw new Error('thread/goal/set did not reactivate the exact suspended Goal')
      }
      this.observeGoalStatus(
        runtime,
        activated,
        'host_issued_mutation',
        'thread/goal/set',
        null,
      )
      const requestSignalStop = (): void => {
        signalStopPromise ??= this.containCancelledGoal(threadId, signal!)
        void signalStopPromise.catch(() => undefined)
      }
      signal?.addEventListener('abort', requestSignalStop, { once: true })
      if (signal?.aborted) {
        requestSignalStop()
      }
      try {
        await runtime.firstTurn
        if (runtime.stopPromise) {
          await runtime.stopPromise
          throw new Error('resumed Goal returned to suspension before admission opened')
        }
        if (signal?.aborted) {
          requestSignalStop()
          await signalStopPromise
          throw cancellationError(signal, 'resumed Goal first turn')
        }
      } finally {
        signal?.removeEventListener('abort', requestSignalStop)
      }
      return { threadId }
    } catch (error) {
      const uncertainInitialAppend = error instanceof GoalEpochInitialContextAppendError
      let containmentError: unknown = null
      if (uncertainInitialAppend || this.activeGoals.get(threadId) === runtime) {
        try {
          await (
            uncertainInitialAppend
              ? this.stopBoundedGoalEpoch(
                  threadId,
                  signal?.aborted && isExplicitForceStop(signal)
                    ? 'explicit_force_stop'
                    : 'turn_start_failure',
                )
              : signal?.aborted
                ? signalStopPromise ?? this.containCancelledGoal(threadId, signal)
                : runtime.stopPromise ?? this.stopBoundedGoalEpoch(threadId, 'turn_start_failure')
          )
        } catch (caught) {
          containmentError = caught
        }
      }
      if (containmentError) {
        throw new AggregateError(
          [error, containmentError],
          'suspended Goal resume failed and safe containment also failed',
        )
      }
      if (signal?.aborted && !uncertainInitialAppend) {
        throw cancellationError(signal, 'suspended Goal resume')
      }
      throw error
    }
  }

  async findMaterializedGoalEpoch(
    signal?: AbortSignal,
  ): Promise<BoundedGoalEpochIdentity | null> {
    if (this.activeGoals.size !== 0) {
      throw new Error('materialized Goal discovery requires an empty local boundary')
    }
    await this.ensureReady(signal)
    const workspaceRoot = path.resolve(this.workspaceRoot)
    const matches = new Set<string>()
    let cursor: string | null = null
    const seenNextCursors = new Set<string>()
    while (true) {
      const response = await this.request(
        'thread/list',
        {
          archived: false,
          limit: 100,
          sourceKinds: THREAD_SOURCE_KINDS,
          ...(cursor === null ? {} : { cursor }),
        },
        signal,
        'materialized Goal discovery',
      )
      const page = coerceThreadListPage(response)
      if (
        page.nextCursor !== null &&
        (page.nextCursor === cursor || seenNextCursors.has(page.nextCursor))
      ) {
        throw new Error('thread/list pagination cursor did not advance')
      }
      for (const row of page.data) {
        const thread = asThreadSummary(row)
        if (!thread) {
          throw new Error('thread/list returned an invalid root thread entry')
        }
        if (typeof row?.parentThreadId === 'string' && row.parentThreadId.length > 0) {
          continue
        }
        let candidateCwd = thread.cwd
        if (typeof candidateCwd !== 'string' || candidateCwd.length === 0) {
          const read = coerceThreadReadResult(
            await this.request(
              'thread/read',
              { threadId: thread.id, includeTurns: false },
              signal,
              'materialized Goal discovery',
            ),
          )
          candidateCwd = read.thread.cwd
        }
        if (
          typeof candidateCwd === 'string' &&
          pathIsWithin(workspaceRoot, candidateCwd) &&
          pathIsWithin(candidateCwd, workspaceRoot)
        ) {
          matches.add(thread.id)
        }
      }
      if (page.nextCursor === null) {
        break
      }
      seenNextCursors.add(page.nextCursor)
      cursor = page.nextCursor
    }
    if (matches.size > 1) {
      throw new Error('Goal workspace identifies more than one materialized root thread')
    }
    const [threadId] = matches
    return threadId ? { threadId } : null
  }

  async getThreadGoal(threadId: string): Promise<ThreadGoal | null> {
    const suspended = this.suspendedStates.get(threadId)
    if (suspended) {
      return suspended.goal
    }
    const terminal = this.terminalStates.get(threadId)
    if (terminal) {
      return terminal.goal
    }
    await this.ensureReady()
    let response: any
    try {
      response = await this.request('thread/goal/get', { threadId })
    } catch (error) {
      if (
        error instanceof JsonRpcResponseError &&
        error.jsonRpcMethod === 'thread/goal/get' &&
        error.jsonRpcCode === -32600 &&
        await this.isExactArchivedGoalRoot(threadId)
      ) {
        return null
      }
      throw error
    }
    const goal = extractThreadResult(response).goal
    return goal == null ? null : coerceThreadGoal(goal)
  }

  private descendantStates(runtime: GoalRuntime): GoalEpochDescendantState[] {
    return [...runtime.descendants.values()]
      .map(({ threadId, parentThreadId, activeTurnId, status }) => ({
        threadId,
        parentThreadId,
        activeTurnId,
        status,
      }))
      .sort((left, right) => left.threadId.localeCompare(right.threadId))
  }

  private stateFromRuntime(runtime: GoalRuntime): GoalEpochState {
    return {
      goal: runtime.goal,
      threadStatus: runtime.threadStatus,
      activeTurnId: runtime.activeTurnId,
      descendants: this.descendantStates(runtime),
      nativeMaterialObservations: [...runtime.materials.values()],
    }
  }

  async readGoalEpochState(threadId: string, signal?: AbortSignal): Promise<GoalEpochState> {
    const suspended = this.suspendedStates.get(threadId)
    if (suspended) {
      return structuredClone(suspended)
    }
    const terminal = this.terminalStates.get(threadId)
    if (terminal) {
      return structuredClone(terminal)
    }
    const runtime = this.activeGoals.get(threadId)
    if (!runtime) {
      throw new Error('bounded Goal is not owned by this boundary')
    }
    await this.ensureReady(signal)
    const tolerateReadFailure = async <T>(operation: Promise<T>, fallback: T): Promise<T> => {
      try {
        return await operation
      } catch (error) {
        if (error instanceof GoalEpochCancelledError) {
          throw error
        }
        return fallback
      }
    }
    const [goal, read] = await Promise.all([
      tolerateReadFailure(
        this.request(
          'thread/goal/get',
          { threadId },
          signal,
          'Goal state read',
        ).then((response) => {
          const value = extractThreadResult(response).goal
          return value == null ? null : coerceThreadGoal(value)
        }),
        runtime.goal,
      ),
      tolerateReadFailure(
        this.request(
          'thread/read',
          { threadId, includeTurns: true },
          signal,
          'Goal state read',
        ),
        null,
      ),
    ])
    const thread = extractThreadResult(read)?.thread
    if (thread) {
      runtime.threadStatus = thread.status
      runtime.activeTurnId ??= latestActiveTurnId(thread)
    }
    if (goal) {
      this.observeGoalStatus(runtime, goal, 'readback', 'thread/goal/get', null)
    }
    return structuredClone(this.stateFromRuntime(runtime))
  }

  async pauseAndInterruptGoalEpoch(threadId: string): Promise<GoalEpochStopResult> {
    return this.suspendBoundedGoalEpoch(threadId, undefined, 'operator_stop')
  }

  private async discoverDescendants(runtime: GoalRuntime): Promise<void> {
    const pendingParents = [runtime.threadId]
    const visited = new Set<string>()
    while (pendingParents.length > 0) {
      const parentThreadId = pendingParents.shift()!
      if (visited.has(parentThreadId)) {
        continue
      }
      visited.add(parentThreadId)
      let cursor: string | null = null
      const seenNextCursors = new Set<string>()
      while (true) {
        const response = await this.request('thread/list', {
          parentThreadId,
          archived: false,
          limit: 100,
          sourceKinds: THREAD_SOURCE_KINDS,
          ...(cursor === null ? {} : { cursor }),
        })
        const page = coerceThreadListPage(response)
        if (
          page.nextCursor !== null &&
          (page.nextCursor === cursor || seenNextCursors.has(page.nextCursor))
        ) {
          throw new Error('thread/list pagination cursor did not advance')
        }
        for (const row of page.data) {
          if (typeof row?.id !== 'string') {
            throw new Error('thread/list returned an invalid descendant entry')
          }
          const child = this.registerDescendant(row.id, parentThreadId)
          if (child?.descendant) {
            pendingParents.push(row.id)
          }
        }
        if (page.nextCursor === null) {
          break
        }
        seenNextCursors.add(page.nextCursor)
        cursor = page.nextCursor
      }
    }
  }

  private async currentTurnId(threadId: string, fallback: string | null): Promise<string | null> {
    if (fallback) {
      return fallback
    }
    const response = await this.request('thread/read', { threadId, includeTurns: true }).catch(() => null)
    return latestActiveTurnId(extractThreadResult(response)?.thread)
  }

  private async containThreads(runtime: GoalRuntime): Promise<{
    descendantsContained: number
    rootInterruptedTurnId: string | null
    appServerTerminated: boolean
  }> {
    let appServerTerminated = false
    try {
      await this.discoverDescendants(runtime)
    } catch {
      appServerTerminated = true
    }
    const descendants = [...runtime.descendants.values()].sort(
      (left, right) => right.depth - left.depth,
    )
    try {
      for (const descendant of descendants) {
        const turnId = await this.currentTurnId(descendant.threadId, descendant.activeTurnId)
        if (turnId) {
          await this.request('turn/interrupt', { threadId: descendant.threadId, turnId })
        }
        await this.request('thread/archive', { threadId: descendant.threadId })
        descendant.activeTurnId = null
        descendant.status = 'archived'
      }
      const rootTurnId = await this.currentTurnId(runtime.threadId, runtime.activeTurnId)
      if (rootTurnId) {
        if (runtime.diagnostic.blockedTurnId !== null) {
          runtime.diagnostic.hostInterventionAfterBlocked = true
        }
        this.emitGoalEpochDiagnostic(runtime, {
          kind: 'host_turn_interrupt_requested',
          turnId: rootTurnId,
          transitionSource: 'host_issued_mutation',
          hostTurnInterruptRequested: true,
        })
        await this.request('turn/interrupt', { threadId: runtime.threadId, turnId: rootTurnId })
      }
      runtime.activeTurnId = null
      return {
        descendantsContained: descendants.length,
        rootInterruptedTurnId: rootTurnId,
        appServerTerminated,
      }
    } catch {
      appServerTerminated = true
      return {
        descendantsContained: descendants.filter((item) => item.status === 'archived').length,
        rootInterruptedTurnId: runtime.activeTurnId,
        appServerTerminated,
      }
    }
  }

  private async reconstructRecoveryRuntime(
    threadId: string,
    recovery: DeadRunnerRecoveryRequest,
  ): Promise<GoalRuntime | null> {
    if (!this.options.goalPolicy) {
      throw new Error('dead-runner Goal recovery requires a process-wide Goal policy')
    }
    const normalizedPolicy = normalizeGoalProcessConfig(
      this.options.goalPolicy,
      this.workspaceRoot,
    )
    try {
      await this.request('thread/resume', { threadId, cwd: path.resolve(this.workspaceRoot) })
    } catch (error) {
      if (recovery.phase === 'pending') {
        await this.invokeScopedStopCallback(this.stopContext(threadId, null, 'dead_runner_recovery'))
        return null
      }
      throw error
    }
    const goal = await this.getThreadGoal(threadId)
    if (goal && goal.objective !== recovery.expectedObjective) {
      throw new Error('recovered Goal differs from its durable objective')
    }
    const firstTurn = deferred<string>()
    void firstTurn.promise.catch(() => undefined)
    const runtime: GoalRuntime = {
      threadId,
      workspaceRoot: path.resolve(this.workspaceRoot),
      expectedObjective: recovery.expectedObjective,
      expectedModelProvider: normalizedPolicy.expectedModelProvider,
      expectedModel: normalizedPolicy.expectedModel,
      reasoningEffort: normalizedPolicy.reasoningEffort,
      goal,
      threadStatus: null,
      activeTurnId: null,
      toolNames: new Set(),
      acceptingTools: false,
      stopping: false,
      dynamicOperationController: new AbortController(),
      materialOperationController: new AbortController(),
      stopPromise: null,
      transitionKind: null,
      firstTurn: firstTurn.promise,
      resolveFirstTurn: firstTurn.resolve,
      rejectFirstTurn: firstTurn.reject,
      descendants: new Map(),
      pendingAgentMessages: [],
      materials: new Map(),
      pendingMaterials: new Map(),
      diagnostic: {
        sequence: 0,
        blockedTurnId: null,
        errorTurnId: null,
        sameTurnActivityAfterBlockedEmitted: false,
        sameTurnActivityAfterErrorEmitted: false,
        hostInterventionAfterBlocked: false,
        pendingReadbackTransition: null,
      },
    }
    this.activeGoals.set(threadId, runtime)
    this.observeWorkerState({ rootThreadId: threadId, runtime, descendant: null }, 'registered')
    return runtime
  }

  private async isExactArchivedGoalRoot(threadId: string): Promise<boolean> {
    const workspaceRoot = path.resolve(this.workspaceRoot)
    let cursor: string | null = null
    const seenNextCursors = new Set<string>()
    while (true) {
      const response = await this.request('thread/list', {
        archived: true,
        limit: 100,
        sourceKinds: THREAD_SOURCE_KINDS,
        ...(cursor === null ? {} : { cursor }),
      })
      const page = coerceThreadListPage(response)
      if (
        page.nextCursor !== null &&
        (page.nextCursor === cursor || seenNextCursors.has(page.nextCursor))
      ) {
        throw new Error('thread/list pagination cursor did not advance')
      }
      for (const row of page.data) {
        const thread = asThreadSummary(row)
        if (!thread) {
          throw new Error('thread/list returned an invalid archived thread entry')
        }
        if (thread.id !== threadId) {
          continue
        }
        if (typeof row?.parentThreadId === 'string' && row.parentThreadId.length > 0) {
          throw new Error('archived Goal identity resolves to a descendant thread')
        }
        let candidateCwd = thread.cwd
        if (typeof candidateCwd !== 'string' || candidateCwd.length === 0) {
          const read = coerceThreadReadResult(
            await this.request('thread/read', { threadId, includeTurns: false }),
          )
          if (read.thread.id !== threadId) {
            throw new Error('thread/read returned the wrong archived Goal identity')
          }
          candidateCwd = read.thread.cwd
        }
        if (
          typeof candidateCwd !== 'string' ||
          !pathIsWithin(workspaceRoot, candidateCwd) ||
          !pathIsWithin(candidateCwd, workspaceRoot)
        ) {
          throw new Error('archived Goal differs from its durable workspace')
        }
        return true
      }
      if (page.nextCursor === null) {
        return false
      }
      seenNextCursors.add(page.nextCursor)
      cursor = page.nextCursor
    }
  }

  async suspendBoundedGoalEpoch(
    threadId: string,
    recovery?: DeadRunnerRecoveryRequest,
    requestedReason: GoalEpochSuspensionReason = 'usage_limited',
  ): Promise<GoalEpochStopResult> {
    const suspended = this.suspendedStates.get(threadId)
    if (suspended) {
      const suspensionError = this.suspensionErrors.get(threadId)
      if (suspensionError) {
        throw suspensionError
      }
      return {
        threadId,
        goal: suspended.goal,
        interruptedTurnId: null,
        goalCleared: false,
        descendantsContained: suspended.descendants.length,
        appServerTerminated: false,
      }
    }
    if (this.terminalStates.has(threadId)) {
      throw new Error('a terminal bounded Goal cannot be converted into a suspension')
    }
    await this.ensureReady()
    let runtime: GoalRuntime | null | undefined = this.activeGoals.get(threadId)
    if (!runtime) {
      if (!recovery) {
        throw new Error('bounded Goal epoch is not active in this boundary')
      }
      runtime = await this.reconstructRecoveryRuntime(threadId, recovery)
      if (!runtime) {
        throw new Error('suspended Goal recovery found no materialized root thread')
      }
    }
    if (runtime.stopPromise) {
      return runtime.stopPromise
    }
    await this.closeAdmission(runtime, 'goal_suspended')
    runtime.rejectFirstTurn(
      new Error('bounded Goal suspended before its first turn: ' + requestedReason),
    )
    this.cancelRuntimeDynamicOperations(
      runtime,
      'bounded Goal owner operation cancelled: ' + requestedReason,
    )
    const suspensionWork = (async () => {
      await this.drainRuntimeMessagesAndMaterials(runtime!)
      let goalError: unknown = null
      let goal = await this.getThreadGoal(threadId).catch((error) => {
        goalError = error
        return runtime!.goal
      })
      const reason: GoalEpochSuspensionReason =
        goal?.status === 'usageLimited' ? 'usage_limited' : requestedReason
      const targetStatus: ThreadGoalStatus =
        reason === 'usage_limited' ? 'usageLimited' : 'paused'
      let callbackError: unknown = null
      try {
        await this.invokeScopedStopCallback(
          this.stopContext(threadId, runtime!.activeTurnId, reason),
        )
      } catch (error) {
        callbackError = error
      }
      if (goal && goal.objective !== runtime!.expectedObjective) {
        goalError = new Error('suspended Goal differs from its bounded objective')
      } else if (goal?.status !== targetStatus) {
        try {
          const suspendedGoal = await this.request('thread/goal/set', {
            threadId,
            status: targetStatus,
          })
          goal = coerceThreadGoal(extractThreadResult(suspendedGoal).goal)
          if (
            goal.threadId !== threadId ||
            goal.objective !== runtime!.expectedObjective ||
            goal.status !== targetStatus
          ) {
            throw new Error('thread/goal/set did not preserve the suspended Goal')
          }
          this.observeGoalStatus(
            runtime!,
            goal,
            'host_issued_mutation',
            'thread/goal/set',
            null,
          )
        } catch (error) {
          goalError = error
        }
      }
      if (goal) {
        runtime!.goal = { ...goal, status: targetStatus }
      }
      const containment = await this.containThreads(runtime!)
      await this.drainRuntimeMessagesAndMaterials(runtime!)
      if (goalError) {
        containment.appServerTerminated = true
      }
      if (containment.appServerTerminated) {
        await this.terminateDedicatedAppServer(
          'suspended Goal containment required process termination',
        )
      }
      this.suspendedStates.set(
        threadId,
        structuredClone(this.stateFromRuntime(runtime!)),
      )
      this.suspendedObjectives.set(threadId, runtime!.expectedObjective)
      await this.removeRuntime(threadId)
      const result: GoalEpochStopResult = {
        threadId,
        goal: runtime!.goal,
        interruptedTurnId: containment.rootInterruptedTurnId,
        goalCleared: false,
        descendantsContained: containment.descendantsContained,
        appServerTerminated: containment.appServerTerminated,
      }
      const errors = [callbackError, goalError].filter((error) => error !== null)
      if (errors.length > 0) {
        const suspensionError = new AggregateError(
          errors,
          'Goal was locally suspended with reconciliation errors',
        )
        this.suspensionErrors.set(threadId, suspensionError)
        throw suspensionError
      }
      this.suspensionErrors.delete(threadId)
      return result
    })()
    const suspensionPromise = suspensionWork.catch((error) => {
      if (
        this.activeGoals.get(threadId) === runtime &&
        runtime!.transitionKind === 'suspending' &&
        runtime!.stopPromise === suspensionPromise
      ) {
        runtime!.stopPromise = null
        runtime!.transitionKind = null
      }
      throw error
    })
    runtime.transitionKind = 'suspending'
    runtime.stopPromise = suspensionPromise
    return suspensionPromise
  }

  async stopBoundedGoalEpoch(
    threadId: string,
    reason: GoalEpochStopReason,
    recovery?: DeadRunnerRecoveryRequest,
  ): Promise<GoalEpochStopResult> {
    const suspended = this.suspendedStates.get(threadId)
    if (suspended) {
      if (reason === 'usage_limited' || reason === 'boundary_shutdown') {
        return {
          threadId,
          goal: suspended.goal,
          interruptedTurnId: null,
          goalCleared: false,
          descendantsContained: suspended.descendants.length,
          appServerTerminated: false,
        }
      }
      if (reason === 'explicit_force_stop') {
        this.suspendedStates.delete(threadId)
        this.suspendedObjectives.delete(threadId)
        this.suspensionErrors.delete(threadId)
        await this.forceTerminateDedicatedAppServer(
          'explicit force-stop interrupted Goal suspension',
        )
        await this.invokeScopedStopCallback(this.stopContext(threadId, null, reason))
        this.terminalStates.set(threadId, structuredClone(suspended))
        return {
          threadId,
          goal: suspended.goal,
          interruptedTurnId: null,
          goalCleared: false,
          descendantsContained: suspended.descendants.length,
          appServerTerminated: true,
        }
      }
      this.suspendedStates.delete(threadId)
      const expectedObjective = this.suspendedObjectives.get(threadId)
      this.suspendedObjectives.delete(threadId)
      const suspensionError = this.suspensionErrors.get(threadId)
      this.suspensionErrors.delete(threadId)
      try {
        await this.ensureReady()
        const reconstructed = await this.reconstructRecoveryRuntime(threadId, {
          phase: 'registered',
          expectedObjective: expectedObjective ?? suspended.goal?.objective ?? '',
        })
        if (!reconstructed) {
          throw new Error('suspended Goal terminal containment found no materialized root thread')
        }
      } catch (error) {
        this.suspendedStates.set(threadId, suspended)
        if (expectedObjective !== undefined) {
          this.suspendedObjectives.set(threadId, expectedObjective)
        }
        if (suspensionError) {
          this.suspensionErrors.set(threadId, suspensionError)
        }
        throw error
      }
    }
    const terminal = this.terminalStates.get(threadId)
    if (terminal) {
      return {
        threadId,
        goal: terminal.goal,
        interruptedTurnId: null,
        goalCleared: terminal.goal === null,
        descendantsContained: terminal.descendants.length,
        appServerTerminated: false,
      }
    }
    await this.ensureReady()
    let runtime: GoalRuntime | null | undefined = this.activeGoals.get(threadId)
    if (!runtime) {
      if (!recovery) {
        throw new Error('bounded Goal epoch is not active in this boundary')
      }
      if (
        (reason === 'owner_checkpoint' || reason === 'dead_runner_recovery') &&
        await this.isExactArchivedGoalRoot(threadId)
      ) {
        await this.invokeScopedStopCallback(this.stopContext(threadId, null, reason))
        const result: GoalEpochStopResult = {
          threadId,
          goal: null,
          interruptedTurnId: null,
          goalCleared: false,
          descendantsContained: 0,
          appServerTerminated: false,
        }
        this.terminalStates.set(threadId, {
          goal: null,
          threadStatus: 'archived',
          activeTurnId: null,
          descendants: [],
          nativeMaterialObservations: [],
        })
        return result
      }
      runtime = await this.reconstructRecoveryRuntime(threadId, recovery)
      if (!runtime) {
        const result: GoalEpochStopResult = {
          threadId,
          goal: null,
          interruptedTurnId: null,
          goalCleared: false,
          descendantsContained: 0,
          appServerTerminated: false,
        }
        this.terminalStates.set(threadId, {
          goal: null,
          threadStatus: null,
          activeTurnId: null,
          descendants: [],
          nativeMaterialObservations: [],
        })
        return result
      }
    }
    if (runtime.stopPromise) {
      if (
        runtime.transitionKind === 'suspending' &&
        reason !== 'usage_limited' &&
        reason !== 'boundary_shutdown'
      ) {
        this.cancelRuntimeOperations(runtime, 'bounded Goal operation cancelled: ' + reason)
        if (reason === 'explicit_force_stop') {
          await this.forceTerminateDedicatedAppServer(
            'explicit force-stop interrupted an in-flight Goal suspension',
          )
        }
        let suspensionError: unknown = null
        try {
          await runtime.stopPromise
        } catch (error) {
          suspensionError = error
        }
        let terminalResult: GoalEpochStopResult
        try {
          terminalResult = await this.stopBoundedGoalEpoch(threadId, reason, recovery)
        } catch (error) {
          if (suspensionError) {
            throw new AggregateError(
              [suspensionError, error],
              'Goal suspension and requested terminal containment both reported errors',
            )
          }
          throw error
        }
        if (suspensionError) {
          throw new AggregateError(
            [suspensionError],
            'requested terminal containment completed after Goal suspension reported an error',
          )
        }
        return terminalResult
      }
      return runtime.stopPromise
    }
    runtime.stopping = true
    runtime.acceptingTools = false
    await this.revokeAllDescendantToolGrants(runtime, 'goal_stopped')
    runtime.rejectFirstTurn(new Error('bounded Goal stopped before its first turn: ' + reason))
    const ownerCheckpoint = reason === 'owner_checkpoint'
    const explicitForceStop = reason === 'explicit_force_stop'
    if (ownerCheckpoint) {
      this.cancelRuntimeDynamicOperations(runtime, 'bounded Goal owner operation cancelled: ' + reason)
    } else {
      this.cancelRuntimeOperations(runtime, 'bounded Goal operation cancelled: ' + reason)
    }
    const stopPromise = (async () => {
      let containment = {
        descendantsContained: runtime!.descendants.size,
        rootInterruptedTurnId: runtime!.activeTurnId,
        appServerTerminated: explicitForceStop,
      }
      if (explicitForceStop) {
        await this.forceTerminateDedicatedAppServer(
          'explicit force-stop terminated the active Goal App Server',
        )
        runtime!.activeTurnId = null
        for (const descendant of runtime!.descendants.values()) {
          descendant.activeTurnId = null
        }
      } else if (ownerCheckpoint) {
        await this.drainRuntimeMessagesAndMaterials(runtime!)
      } else {
        await this.messageQueue
        await Promise.all([...runtime!.pendingMaterials.values()])
      }
      let callbackError: unknown = null
      try {
        await this.invokeScopedStopCallback(this.stopContext(threadId, runtime!.activeTurnId, reason))
      } catch (error) {
        callbackError = error
      }
      if (!explicitForceStop) {
        containment = await this.containThreads(runtime!)
      }
      if (ownerCheckpoint && !explicitForceStop) {
        await this.drainRuntimeMessagesAndMaterials(runtime!)
      }
      let goal = explicitForceStop
        ? runtime!.goal
        : await this.getThreadGoal(threadId).catch(() => runtime!.goal)
      if (goal && ownerCheckpoint) {
        goal = { ...goal, status: 'complete' }
      } else if (!explicitForceStop && goal?.status === 'active') {
        const paused = await this.request('thread/goal/set', { threadId, status: 'paused' }).catch(
          () => null,
        )
        if (paused) {
          goal = coerceThreadGoal(extractThreadResult(paused).goal)
          this.observeGoalStatus(
            runtime!,
            goal,
            'host_issued_mutation',
            'thread/goal/set',
            null,
          )
        }
      }
      if (goal && reason === 'usage_limited') {
        goal = { ...goal, status: 'usageLimited' }
      }
      runtime!.goal = goal
      let goalCleared = false
      if (!explicitForceStop) {
        try {
          goalCleared = await this.clearThreadGoal(threadId)
        } catch {
          containment.appServerTerminated = true
        }
        try {
          await this.request('thread/archive', { threadId })
        } catch {
          containment.appServerTerminated = true
        }
      }
      if (containment.appServerTerminated && !explicitForceStop) {
        await this.terminateDedicatedAppServer('Goal containment required process termination')
      }
      this.saveTerminalState(runtime!)
      await this.removeRuntime(threadId)
      const result = {
        threadId,
        goal,
        interruptedTurnId: containment.rootInterruptedTurnId,
        goalCleared,
        descendantsContained: containment.descendantsContained,
        appServerTerminated: containment.appServerTerminated,
      }
      if (callbackError) {
        throw new AggregateError(
          [callbackError],
          'bounded Goal was locally contained after its owner stop callback failed',
        )
      }
      return result
    })()
    runtime.transitionKind = 'stopping'
    runtime.stopPromise = stopPromise
    return stopPromise
  }

  async finalizeCompletedGoalEpoch(threadId: string): Promise<GoalEpochState> {
    const terminal = this.terminalStates.get(threadId)
    if (terminal) {
      return structuredClone(terminal)
    }
    const runtime = this.activeGoals.get(threadId)
    if (!runtime || runtime.stopPromise) {
      throw new Error('bounded Goal epoch is unavailable for successful finalization')
    }
    runtime.acceptingTools = false
    runtime.stopping = true
    await this.revokeAllDescendantToolGrants(runtime, 'goal_finalized')
    await this.messageQueue
    if (runtime.stopPromise || this.activeGoals.get(threadId) !== runtime) {
      throw new Error('bounded Goal entered containment during successful finalization')
    }
    const toolPrefix = threadId + '\u0000'
    await Promise.all(
      [...this.dynamicToolCalls.entries()]
        .filter(([key]) => key.startsWith(toolPrefix))
        .map(([, cached]) => cached.result),
    )
    await Promise.all([...runtime.pendingMaterials.values()])
    if (runtime.stopPromise || this.activeGoals.get(threadId) !== runtime) {
      throw new Error('bounded Goal entered containment during successful finalization')
    }
    const goal = await this.getThreadGoal(threadId).catch(() => runtime.goal)
    if (
      goal?.status !== 'complete' &&
      !(goal?.status === 'blocked' && runtime.activeTurnId === null)
    ) {
      throw new Error('bounded Goal has not reached an inert terminal state')
    }
    runtime.goal = goal
    const containment = await this.containThreads(runtime)
    if (containment.appServerTerminated) {
      await this.terminateDedicatedAppServer('completed Goal cleanup required process termination')
    } else {
      await this.clearThreadGoal(threadId).catch(() => false)
      await this.request('thread/archive', { threadId }).catch(async () => {
        await this.terminateDedicatedAppServer('completed Goal root archive failed')
      })
    }
    this.saveTerminalState(runtime)
    await this.removeRuntime(threadId)
    return structuredClone(this.terminalStates.get(threadId)!)
  }

  private saveTerminalState(runtime: GoalRuntime): void {
    this.terminalStates.set(runtime.threadId, structuredClone(this.stateFromRuntime(runtime)))
  }

  private async closeAdmission(
    runtime: GoalRuntime,
    reason: 'root_checkpoint' | 'goal_suspended' | 'boundary_process_exit',
  ): Promise<void> {
    runtime.acceptingTools = false
    runtime.stopping = true
    await this.revokeAllDescendantToolGrants(runtime, reason)
  }

  private async drainRuntimeMessagesAndMaterials(runtime: GoalRuntime): Promise<void> {
    for (;;) {
      const queued = this.messageQueue
      await queued
      await Promise.all([...runtime.pendingMaterials.values()])
      if (queued === this.messageQueue && runtime.pendingMaterials.size === 0) {
        return
      }
    }
  }

  private cancelRuntimeDynamicOperations(runtime: GoalRuntime, reason: string): void {
    if (!runtime.dynamicOperationController.signal.aborted) {
      runtime.dynamicOperationController.abort(new Error(reason))
    }
  }

  private cancelRuntimeOperations(runtime: GoalRuntime, reason: string): void {
    this.cancelRuntimeDynamicOperations(runtime, reason)
    if (!runtime.materialOperationController.signal.aborted) {
      runtime.materialOperationController.abort(new Error(reason))
    }
  }

  private async removeRuntime(threadId: string): Promise<void> {
    const runtime = this.activeGoals.get(threadId)
    if (runtime) {
      runtime.acceptingTools = false
      runtime.stopping = true
      await this.revokeAllDescendantToolGrants(runtime, 'runtime_removed')
      this.observeWorkerState({ rootThreadId: threadId, runtime, descendant: null }, 'status', 'removed')
      for (const descendant of runtime.descendants.values()) {
        this.observeWorkerState({ rootThreadId: threadId, runtime, descendant }, 'status', 'removed')
      }
      this.activeGoals.delete(threadId)
    }
    const prefix = threadId + '\u0000'
    for (const key of this.dynamicToolCalls.keys()) {
      if (key.startsWith(prefix)) {
        this.dynamicToolCalls.delete(key)
      }
    }
  }

  async waitForFatalBoundaryFence(): Promise<void> {
    await this.fatalFencePromise
    if (this.fatalFenceError) {
      throw this.fatalFenceError
    }
  }

  async clearThreadGoal(threadId: string): Promise<boolean> {
    await this.ensureReady()
    const response = await this.request('thread/goal/clear', { threadId })
    const cleared = extractThreadResult(response).cleared
    if (typeof cleared !== 'boolean') {
      throw new Error('thread/goal/clear returned the wrong shape')
    }
    return cleared
  }

  isReady(): boolean {
    return this.ready
  }

  getReadyReason(): string | null {
    return this.readyReason
  }

  async sanityCheck(signal?: AbortSignal): Promise<void> {
    const result = await this.request('config/read', {}, signal, 'boundary startup')
    if (!extractResult(result)?.config) {
      throw new Error('boundary sanity check did not return config')
    }
  }

  async listThreads(): Promise<CodexRawThreadSummary[]> {
    await this.ensureReady()
    const threads: CodexRawThreadSummary[] = []
    let cursor: string | null = null
    const seenNextCursors = new Set<string>()
    while (true) {
      const page = coerceThreadListPage(
        await this.request('thread/list', {
          limit: 100,
          ...(cursor === null ? {} : { cursor }),
        }),
      )
      if (
        page.nextCursor !== null &&
        (page.nextCursor === cursor || seenNextCursors.has(page.nextCursor))
      ) {
        throw new Error('thread/list pagination cursor did not advance')
      }
      for (const row of page.data) {
        const thread = asThreadSummary(row)
        if (!thread) {
          throw new Error('thread/list returned an invalid thread entry')
        }
        threads.push(thread)
      }
      if (page.nextCursor === null) {
        return threads
      }
      seenNextCursors.add(page.nextCursor)
      cursor = page.nextCursor
    }
  }

  async readThread(threadId: string): Promise<CodexRawThreadReadResult> {
    await this.ensureReady()
    return coerceThreadReadResult(
      await this.request('thread/read', { threadId, includeTurns: true }),
    )
  }

  async resumeThread(threadId: string): Promise<void> {
    if (this.options.goalPolicy) {
      throw new Error('generic thread resume is disabled inside a bounded Goal boundary')
    }
    await this.ensureReady()
    if (this.resumedThreadIds.has(threadId)) {
      return
    }
    await this.request('thread/resume', { threadId, cwd: this.workspaceRoot })
    this.resumedThreadIds.add(threadId)
  }

  async createThread(prompt: string): Promise<CreateThreadResponse> {
    if (this.options.goalPolicy) {
      throw new Error('generic thread creation is disabled inside a bounded Goal boundary')
    }
    await this.ensureReady()
    const started = await this.request('thread/start', {
      cwd: this.workspaceRoot,
      approvalPolicy: 'never',
      sandbox: 'workspace-write',
      experimentalRawEvents: false,
      persistExtendedHistory: true,
    })
    const threadId = extractThreadId(extractResult(started))
    if (!threadId) {
      throw new Error('thread/start did not return a thread id')
    }
    const turn = await this.request('turn/start', {
      threadId,
      cwd: this.workspaceRoot,
      approvalPolicy: 'never',
      input: [{ type: 'text', text: prompt, text_elements: [] }],
    })
    return { threadId, turnId: extractTurnId(extractResult(turn)) }
  }

  async submitPrompt(threadId: string, prompt: string): Promise<SubmitPromptResponse> {
    if (this.options.goalPolicy) {
      throw new Error('generic prompt submission is disabled inside a bounded Goal boundary')
    }
    await this.ensureReady()
    const turn = await this.request('turn/start', {
      threadId,
      cwd: this.workspaceRoot,
      approvalPolicy: 'never',
      input: [{ type: 'text', text: prompt, text_elements: [] }],
    })
    return { threadId, turnId: extractTurnId(extractResult(turn)) }
  }

  subscribe(listener: (message: any) => void): () => void {
    this.on('notification', listener)
    return () => this.off('notification', listener)
  }

  private async stopNativeAppServerProcess(signal: NodeJS.Signals = 'SIGTERM'): Promise<void> {
    const child = this.child
    if (!child || this.childClosed) {
      return
    }
    const closed = new Promise<void>((resolve) => child.once('close', () => resolve()))
    if (
      child.exitCode === null &&
      child.signalCode === null &&
      (signal === 'SIGKILL' || !child.killed)
    ) {
      // A false return can race a natural child exit. `close` is the authoritative
      // lifecycle edge because it also proves the stdio streams have drained.
      child.kill(signal)
    }
    await closed
  }

  private async terminateDedicatedAppServer(reason: string): Promise<void> {
    this.intentionalTerminationReason = reason
    this.ready = false
    this.readyReason = reason
    await this.stopNativeAppServerProcess()
    this.cancelPendingRequests(reason)
  }

  private async forceTerminateDedicatedAppServer(reason: string): Promise<void> {
    this.intentionalTerminationReason = reason
    this.ready = false
    this.readyReason = reason
    await this.stopNativeAppServerProcess('SIGKILL')
    this.cancelPendingRequests(reason)
  }

  async stop(): Promise<void> {
    const errors: unknown[] = []
    for (const threadId of [...this.activeGoals.keys()]) {
      try {
        await this.stopBoundedGoalEpoch(threadId, 'boundary_shutdown')
      } catch (error) {
        errors.push(error)
      }
    }
    this.stopped = true
    this.intentionalTerminationReason ??= 'stopped'
    this.ready = false
    this.readyReason = this.intentionalTerminationReason
    if (this.restartTimer) {
      clearTimeout(this.restartTimer)
      this.restartTimer = null
    }
    try {
      await this.stopNativeAppServerProcess()
      this.cancelPendingRequests(this.readyReason)
    } catch (error) {
      errors.push(error)
    }
    this.lineReader?.close()
    this.lineReader = null
    this.child = null
    this.resumedThreadIds.clear()
    this.dynamicToolCalls.clear()
    if (errors.length > 0) {
      throw new AggregateError(errors, 'Codex Goal boundary stopped with containment errors')
    }
  }
}
