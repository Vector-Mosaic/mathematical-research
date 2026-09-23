import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import type { MissionHostConfig } from './config.js'

const BRIDGE_REQUEST_SCHEMA_VERSION = 'mathematical_research.mission_host_bridge_request.v1'
const TOOL_RESULT_SCHEMA_VERSION = 'tool_result.v1'
const TOOL_ID = 'tool.mathematical_research.rh_mission'
const BRIDGE_DIAGNOSTIC_TAIL_CHARACTERS = 32 * 1024
const MISSION_BRIDGE_PATH = '/usr/bin:/bin'
const MISSION_BRIDGE_RUNTIME_FILE =
  /^mission-bridge-(?:request|response)\.[1-9]\d*\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.json$/

export type JsonObject = Record<string, unknown>

export interface MissionBridgeConfig {
  repoRoot: string
  releaseSha: string
  missionWorkspaceRoot: string
  runtimeDir: string
  codexHome: string
  pythonPath: string
  codexCliPath: string
  missionScriptPath: string
  projectId: string
  missionId: string
  pollIntervalMs: number
  permissionProfileId: string
  trustedMcpServerIds: readonly string[]
  trustedAppIds: readonly string[]
}

export interface BridgeCommandOptions {
  cwd: string
  encoding: 'utf8'
  env: NodeJS.ProcessEnv
  shell: false
  windowsHide: true
  signal: AbortSignal
  forceSignal: AbortSignal
}

export interface BridgeCommandResult {
  exitCode: number | null
  terminationSignal: NodeJS.Signals | null
  stdout: string
  stderr: string
  stderrTruncated: boolean
}

export interface BridgeProcessDiagnostic {
  exitCode: number | null
  terminationSignal: NodeJS.Signals | null
  stderrTail: string
  stderrTruncated: boolean
  responseError: string | null
}

export type BridgeCommandRunner = (
  executable: string,
  args: readonly string[],
  options: BridgeCommandOptions,
) => Promise<BridgeCommandResult>

type BridgeRequestWriter = (filePath: string, encoded: string) => Promise<void>

export interface ExecutiveEpochBinding {
  rootThreadId: string
  executiveEpochId: string
}

export interface MissionAuthorizationCut extends JsonObject {
  project_commit: number
  current_root_digest: string
  transition_head_digest: string | null
  canonical_authority_digest: string
}

export interface SuspendedEpochCutInput extends ExecutiveEpochBinding {
  expectedCut: MissionAuthorizationCut
  workspaceRoot: string
}

export interface BindExecutiveEpochInput {
  executiveEpochId: string
  rootThreadId: string
  workspaceRoot: string
}

export interface DirectFailedEpochReconciliation extends JsonObject {
  stage: 'authorization_only' | 'goal_runtime'
  failure_reason: string
}

export interface DirectFailedExecutiveEpochInput {
  executiveEpochId: string
  reconciliation: DirectFailedEpochReconciliation
}

export interface SemanticOperationBinding {
  rootThreadId: string
  executiveEpochId: string
}

export interface CheckpointSourceFailureWarning {
  readonly code:
    | 'checkpoint_source_capture_failed_writer_reacquired'
    | 'checkpoint_source_operation_teardown_failed_writer_revalidated'
    | 'checkpoint_source_snapshot_skipped_writer_preserved'
  readonly reason_code: string
  readonly database_bytes: number | null
  readonly handoff_elapsed_ms: number
}

export type CheckpointSourceFailureWarningObserver = (
  warning: CheckpointSourceFailureWarning,
) => unknown

export interface HistoricalReadGrantBinding extends ExecutiveEpochBinding {
  childThreadId: string
  parentThreadId: string
  depth: number
}

export interface ResearchReadGrantBinding extends ExecutiveEpochBinding {
  childThreadId: string
  parentThreadId: string
  depth: number
}

export interface CandidateA1ReviewGrantBinding extends ExecutiveEpochBinding {
  childThreadId: string
  parentThreadId: string
  depth: number
}

export interface DelegatedReadBinding extends ExecutiveEpochBinding {
  callerThreadId: string
  parentThreadId: string
  depth: number
  turnId: string
  grantId: string
}

export interface ResearchReadBinding extends DelegatedReadBinding {
  assignmentId: string
}

export interface CandidateA1ReviewBinding extends ExecutiveEpochBinding {
  callerThreadId: string
  parentThreadId: string
  depth: number
  turnId: string
}

export interface AdmissionGrantBinding extends ExecutiveEpochBinding {
  childThreadId: string
  parentThreadId: string
  depth: number
}

export interface AdmissionBinding extends ExecutiveEpochBinding {
  callerThreadId: string
  parentThreadId: string
  depth: number
  turnId: string
}

export interface FormalAttemptRequest {
  selected_bet_sha256: string
  correction_basis: string | null
}

export interface GoalStopContext {
  threadId: string
  reason: string
  containmentScope: 'goal_local' | 'mission_fence'
}

export class MissionBridgeError extends Error {
  constructor(
    message: string,
    readonly exitCode: number | null,
    readonly ownerErrors: readonly unknown[] = [],
    readonly terminationSignal: NodeJS.Signals | null = null,
    readonly processDiagnostic: BridgeProcessDiagnostic = {
      exitCode,
      terminationSignal,
      stderrTail: '',
      stderrTruncated: false,
      responseError: null,
    },
  ) {
    super(message)
    this.name = 'MissionBridgeError'
  }
}

export class CheckpointCommittedSourceHandoffBridgeError extends MissionBridgeError {
  constructor(
    exitCode: number | null,
    ownerErrors: readonly unknown[],
    terminationSignal: NodeJS.Signals | null,
    processDiagnostic: BridgeProcessDiagnostic,
    readonly checkpoint: JsonObject,
    readonly continuationSafety: 'safe' | 'unsafe' | 'unverified',
    readonly sourcePublicationState: 'unknown' | 'published',
    readonly sourceHandoffFailureCode: string,
  ) {
    super(
      'RH Mission checkpoint committed, but post-commit operational handling requires owner-checkpoint containment; do not replay the research or checkpoint',
      exitCode,
      ownerErrors,
      terminationSignal,
      processDiagnostic,
    )
    this.name = 'CheckpointCommittedSourceHandoffBridgeError'
  }
}

export class CheckpointDispositionUnverifiedBridgeError extends MissionBridgeError {
  constructor(
    message: string,
    exitCode: number | null,
    ownerErrors: readonly unknown[],
    terminationSignal: NodeJS.Signals | null,
    processDiagnostic: BridgeProcessDiagnostic,
    readonly rootThreadId: string,
    readonly executiveEpochId: string,
  ) {
    super(message, exitCode, ownerErrors, terminationSignal, processDiagnostic)
    this.name = 'CheckpointDispositionUnverifiedBridgeError'
  }
}

export class MissionBridgeCancelledError extends Error {
  constructor(readonly reason: unknown) {
    super('RH Mission owner operation was explicitly cancelled')
    this.name = 'MissionBridgeCancelledError'
  }
}

export interface MissionOwnerBridge {
  cancelOutstanding(reason?: unknown): Promise<void>
  reconstruct(signal?: AbortSignal): Promise<JsonObject>
  hostSnapshot(signal?: AbortSignal): Promise<JsonObject>
  authorizeExecutiveEpoch(expectedCut: MissionAuthorizationCut, signal?: AbortSignal): Promise<JsonObject>
  validateSuspendedEpochCut(input: SuspendedEpochCutInput, signal?: AbortSignal): Promise<JsonObject>
  bindExecutiveEpoch(input: BindExecutiveEpochInput, signal?: AbortSignal): Promise<JsonObject>
  executeSemanticOperation(
    request: unknown,
    binding: SemanticOperationBinding,
    signal?: AbortSignal,
    rootQueryContext?: Readonly<{ cursor_mac_key: string }>,
    warningObserver?: CheckpointSourceFailureWarningObserver,
  ): Promise<JsonObject>
  issueHistoricalReadGrant(
    request: unknown,
    binding: HistoricalReadGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  issueResearchReadGrant(
    request: unknown,
    binding: ResearchReadGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  executeResearchRead(
    request: unknown,
    grant: JsonObject,
    binding: ResearchReadBinding,
    signal: AbortSignal | undefined,
    researchQueryContext: Readonly<{ cursor_mac_key: string }>,
  ): Promise<JsonObject>
  issueCandidateA1ReviewGrant(
    request: unknown,
    binding: CandidateA1ReviewGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  executeDelegatedRead(
    request: unknown,
    grant: JsonObject,
    binding: DelegatedReadBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  executeCandidateA1Review(
    request: unknown,
    grant: JsonObject,
    binding: CandidateA1ReviewBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  openCompleteClaimAdmissionCase(
    request: unknown,
    binding: SemanticOperationBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  issueAdmissionReviewGrant(
    request: unknown,
    binding: AdmissionGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  issueAdmissionDecisionGrant(
    request: unknown,
    binding: AdmissionGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  executeAdmissionReview(
    request: unknown,
    grant: JsonObject,
    binding: AdmissionBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  executeAdmissionDecision(
    request: unknown,
    grant: JsonObject,
    binding: AdmissionBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  executeFormalAttempt(
    request: FormalAttemptRequest,
    binding: ExecutiveEpochBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  captureNativeMaterialObservation(
    observation: unknown,
    binding: ExecutiveEpochBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject>
  fenceMission(context: GoalStopContext, binding: ExecutiveEpochBinding, signal?: AbortSignal): Promise<JsonObject>
  recordDirectFailedExecutiveEpoch(
    input: DirectFailedExecutiveEpochInput,
    signal?: AbortSignal,
  ): Promise<JsonObject>
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function exactCheckpointInvocation(
  action: string,
  payload: JsonObject,
): Readonly<{ rootThreadId: string; executiveEpochId: string }> | null {
  const request = isRecord(payload.request) ? payload.request : null
  const binding = isRecord(payload.binding) ? payload.binding : null
  if (
    action !== 'execute_semantic_operation' ||
    request === null ||
    Object.keys(request).sort().join(',') !== 'input,operation,schema_version' ||
    request.schema_version !== 'mathematical_research.mission_semantic_request.v1' ||
    request.operation !== 'checkpoint' ||
    !isRecord(request.input) ||
    Object.keys(request.input).length !== 0 ||
    binding === null ||
    Object.keys(binding).sort().join(',') !== 'executiveEpochId,rootThreadId' ||
    typeof binding.rootThreadId !== 'string' ||
    binding.rootThreadId.length === 0 ||
    typeof binding.executiveEpochId !== 'string' ||
    binding.executiveEpochId.length === 0
  ) {
    return null
  }
  return {
    rootThreadId: binding.rootThreadId,
    executiveEpochId: binding.executiveEpochId,
  }
}

function completedCheckpointResult(
  value: JsonObject,
  processResult: BridgeCommandResult,
  action: string,
  payload: JsonObject,
): JsonObject | null {
  const invocation = exactCheckpointInvocation(action, payload)
  const data = value.data
  const checkpoint = isRecord(data) && isRecord(data.result) ? data.result : null
  if (
    invocation === null ||
    processResult.exitCode !== 0 ||
    processResult.terminationSignal !== null ||
    value.status !== 'ok' ||
    (value.errors as unknown[]).length !== 0 ||
    !isRecord(data) ||
    Object.keys(data).sort().join(',') !== 'error,operation,result,schema_version,status' ||
    data.schema_version !== 'mathematical_research.mission_semantic_result.v1' ||
    data.operation !== 'checkpoint' ||
    data.status !== 'completed' ||
    data.error !== null ||
    checkpoint === null ||
    Object.keys(checkpoint).sort().join(',') !== 'checkpoint_id,executive_epoch_id,state' ||
    typeof checkpoint.checkpoint_id !== 'string' ||
    checkpoint.checkpoint_id.length === 0 ||
    checkpoint.executive_epoch_id !== invocation.executiveEpochId ||
    checkpoint.state !== 'checkpointed'
  ) {
    return null
  }
  return checkpoint
}

function rejectedCheckpointResult(
  value: JsonObject,
  processResult: BridgeCommandResult,
  action: string,
  payload: JsonObject,
): JsonObject | null {
  const invocation = exactCheckpointInvocation(action, payload)
  const data = value.data
  const error = isRecord(data) && isRecord(data.error) ? data.error : null
  const errorKeys = error === null ? [] : Object.keys(error).sort()
  const requiredErrorKeys = ['code', 'correction', 'failure_scope', 'message', 'property']
  const checkpointErrors = new Map<string, readonly [string, string, string]>([
    ['mission_operation_request_invalid', ['request_shape', 'call', 'revise_request']],
    ['mission_operation_owner_fact_forbidden', ['authority_separation', 'call', 'remove_owner_fact']],
    ['mission_operation_not_allowed', ['operation_authorization', 'call', 'choose_authorized_operation']],
    ['mission_operation_state_conflict', ['current_owner_state', 'call', 'refresh_then_rejudge_if_semantics_changed']],
    ['mission_operation_unavailable', ['interface_availability', 'operation', 'repair_owner_interface']],
    ['executive_epoch_unavailable', ['executive_authority', 'goal', 'authorize_fresh_goal']],
    ['mission_authorization_expired', ['mission_authority', 'operation', 'renew_mission_authorization']],
    ['mission_fenced', ['shared_operational_safety', 'mission', 'owner_reconciliation_required']],
    ['mission_checkpoint_rejected', ['checkpoint_domain_invariants', 'call', 'continue_epoch_before_checkpoint']],
  ])
  const exactError = error === null || typeof error.code !== 'string'
    ? undefined
    : checkpointErrors.get(error.code)
  const errorShapeValid =
    error !== null &&
    (errorKeys.join(',') === requiredErrorKeys.join(',') ||
      errorKeys.join(',') === [...requiredErrorKeys, 'location'].sort().join(',')) &&
    typeof error.message === 'string' &&
    error.message.length > 0 &&
    exactError !== undefined &&
    error.property === exactError[0] &&
    error.failure_scope === exactError[1] &&
    error.correction === exactError[2] &&
    (!Object.hasOwn(error, 'location') ||
      (typeof error.location === 'string' && error.location.length > 0))
  if (
    invocation === null ||
    processResult.exitCode !== 0 ||
    processResult.terminationSignal !== null ||
    value.status !== 'ok' ||
    (value.errors as unknown[]).length !== 0 ||
    !isRecord(data) ||
    Object.keys(data).sort().join(',') !== 'error,operation,result,schema_version,status' ||
    data.schema_version !== 'mathematical_research.mission_semantic_result.v1' ||
    data.operation !== 'checkpoint' ||
    (data.status !== 'rejected' && data.status !== 'unavailable') ||
    data.result !== null ||
    !errorShapeValid
  ) {
    return null
  }
  return data
}

const defaultCommandRunner: BridgeCommandRunner = (executable, args, options) =>
  new Promise((resolve) => {
    const child = spawn(executable, [...args], {
      cwd: options.cwd,
      env: options.env,
      shell: options.shell,
      windowsHide: options.windowsHide,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stderr = ''
    let stderrTruncated = false
    const appendStderr = (chunk: string): void => {
      const combined = `${stderr}${chunk}`
      if (combined.length > BRIDGE_DIAGNOSTIC_TAIL_CHARACTERS) {
        stderrTruncated = true
      }
      stderr = combined.slice(-BRIDGE_DIAGNOSTIC_TAIL_CHARACTERS)
    }
    const requestStop = (): void => {
      child.kill('SIGTERM')
    }
    const requestForceStop = (): void => {
      child.kill(process.platform !== 'win32' ? 'SIGUSR2' : 'SIGTERM')
    }
    if (options.signal.aborted) {
      requestStop()
    } else {
      options.signal.addEventListener('abort', requestStop, { once: true })
    }
    if (options.forceSignal.aborted) {
      requestForceStop()
    } else {
      options.forceSignal.addEventListener('abort', requestForceStop, { once: true })
    }
    child.stdout.resume()
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk: string) => appendStderr(chunk))
    child.on('error', (error) => {
      appendStderr(`${error.name}: ${error.message}`)
    })
    child.on('close', (code, signal) => {
      options.signal.removeEventListener('abort', requestStop)
      options.forceSignal.removeEventListener('abort', requestForceStop)
      resolve({
        exitCode: code,
        terminationSignal: signal,
        stdout: '',
        stderr,
        stderrTruncated,
      })
    })
  })

const defaultRequestWriter: BridgeRequestWriter = async (filePath, encoded) => {
  await fs.promises.writeFile(filePath, encoded, { encoding: 'utf8', flag: 'wx', mode: 0o600 })
}

function processDiagnostic(
  result: BridgeCommandResult,
  responseError: string | null = null,
): BridgeProcessDiagnostic {
  return {
    exitCode: result.exitCode,
    terminationSignal: result.terminationSignal,
    stderrTail: result.stderr,
    stderrTruncated: result.stderrTruncated,
    responseError,
  }
}

function assertClosedToolResult(
  value: unknown,
  result: BridgeCommandResult,
): asserts value is JsonObject {
  if (!isRecord(value)) {
    throw new MissionBridgeError(
      'RH Mission bridge emitted no JSON result object',
      result.exitCode,
      [],
      result.terminationSignal,
      processDiagnostic(result, 'invalid_owner_envelope'),
    )
  }
  const expectedKeys = ['data', 'errors', 'schema_version', 'status', 'summary', 'tool_id', 'verb', 'warnings']
  const actualKeys = Object.keys(value).sort()
  if (
    actualKeys.length !== expectedKeys.length ||
    actualKeys.some((key, index) => key !== expectedKeys[index]) ||
    value.schema_version !== TOOL_RESULT_SCHEMA_VERSION ||
    value.tool_id !== TOOL_ID ||
    value.verb !== 'host-bridge' ||
    !['ok', 'error'].includes(String(value.status)) ||
    typeof value.summary !== 'string' ||
    !isRecord(value.data) ||
    !Array.isArray(value.warnings) ||
    !Array.isArray(value.errors) ||
    (value.status === 'ok' && value.errors.length !== 0)
  ) {
    throw new MissionBridgeError(
      'RH Mission bridge emitted the wrong closed result envelope',
      result.exitCode,
      [],
      result.terminationSignal,
      processDiagnostic(result, 'invalid_owner_envelope'),
    )
  }
}

function invalidWarningEnvelope(result: BridgeCommandResult): MissionBridgeError {
  return new MissionBridgeError(
    'RH Mission bridge emitted the wrong closed result envelope',
    result.exitCode,
    [],
    result.terminationSignal,
    processDiagnostic(result, 'invalid_owner_envelope'),
  )
}

function checkpointSourceFailureWarning(
  value: JsonObject,
  result: BridgeCommandResult,
  action: string,
  payload: JsonObject,
): CheckpointSourceFailureWarning | null {
  const warnings = value.warnings as unknown[]
  if (warnings.length === 0) return null
  const request = isRecord(payload.request) ? payload.request : null
  const binding = isRecord(payload.binding) ? payload.binding : null
  const data = value.data
  const checkpoint = isRecord(data) && isRecord(data.result) ? data.result : null
  const warning = warnings.length === 1 && isRecord(warnings[0]) ? warnings[0] : null
  const expectedKeys = ['code', 'database_bytes', 'handoff_elapsed_ms', 'reason_code']
  const actualKeys = warning === null ? [] : Object.keys(warning).sort()
  const warningCode = warning?.code
  const reasonCode = warning?.reason_code
  const writerPreserved = warningCode === 'checkpoint_source_snapshot_skipped_writer_preserved'
  const preservedWriterReasons = new Set([
    'checkpoint_source_busy',
    'checkpoint_source_measurement_unavailable',
    'checkpoint_source_writer_release_failed',
  ])
  const teardownReasons = new Set([
    'checkpoint_source_published',
    'checkpoint_source_capture_failed',
    ...preservedWriterReasons,
  ])
  const warningCodeValid =
    warningCode === 'checkpoint_source_capture_failed_writer_reacquired' ||
    warningCode === 'checkpoint_source_operation_teardown_failed_writer_revalidated' ||
    writerPreserved
  const reasonCodeValid =
    (writerPreserved && preservedWriterReasons.has(String(reasonCode))) ||
    (warningCode === 'checkpoint_source_capture_failed_writer_reacquired' &&
      reasonCode === 'checkpoint_source_capture_failed') ||
    (warningCode === 'checkpoint_source_operation_teardown_failed_writer_revalidated' &&
      teardownReasons.has(String(reasonCode)))
  const nullableMeasurement =
    writerPreserved ||
    (warningCode === 'checkpoint_source_operation_teardown_failed_writer_revalidated' &&
      preservedWriterReasons.has(String(reasonCode)))
  const databaseMeasurementValid =
    (nullableMeasurement && warning?.database_bytes === null) ||
    (Number.isSafeInteger(warning?.database_bytes) && Number(warning?.database_bytes) > 0)
  if (
    action !== 'execute_semantic_operation' ||
    request === null ||
    Object.keys(request).sort().join(',') !== 'input,operation,schema_version' ||
    request.schema_version !== 'mathematical_research.mission_semantic_request.v1' ||
    request.operation !== 'checkpoint' ||
    !isRecord(request.input) ||
    Object.keys(request.input).length !== 0 ||
    binding === null ||
    Object.keys(binding).sort().join(',') !== 'executiveEpochId,rootThreadId' ||
    typeof binding.rootThreadId !== 'string' ||
    binding.rootThreadId.length === 0 ||
    typeof binding.executiveEpochId !== 'string' ||
    binding.executiveEpochId.length === 0 ||
    value.status !== 'ok' ||
    !isRecord(data) ||
    Object.keys(data).sort().join(',') !== 'error,operation,result,schema_version,status' ||
    data.schema_version !== 'mathematical_research.mission_semantic_result.v1' ||
    data.operation !== 'checkpoint' ||
    data.status !== 'completed' ||
    data.error !== null ||
    checkpoint === null ||
    Object.keys(checkpoint).sort().join(',') !== 'checkpoint_id,executive_epoch_id,state' ||
    typeof checkpoint.checkpoint_id !== 'string' ||
    checkpoint.checkpoint_id.length === 0 ||
    checkpoint.executive_epoch_id !== binding.executiveEpochId ||
    checkpoint.state !== 'checkpointed' ||
    warning === null ||
    actualKeys.length !== expectedKeys.length ||
    actualKeys.some((key, index) => key !== expectedKeys[index]) ||
    !warningCodeValid ||
    typeof reasonCode !== 'string' ||
    !reasonCodeValid ||
    !databaseMeasurementValid ||
    !Number.isSafeInteger(warning.handoff_elapsed_ms) ||
    (warning.handoff_elapsed_ms as number) < 0
  ) {
    throw invalidWarningEnvelope(result)
  }
  return {
    code: warningCode as CheckpointSourceFailureWarning['code'],
    reason_code: reasonCode as string,
    database_bytes: warning.database_bytes as number | null,
    handoff_elapsed_ms: warning.handoff_elapsed_ms as number,
  }
}

function checkpointCommittedSourceHandoffFailure(
  value: JsonObject,
  result: BridgeCommandResult,
  action: string,
  payload: JsonObject,
): Readonly<{
  checkpoint: JsonObject
  continuationSafety: 'safe' | 'unsafe' | 'unverified'
  sourcePublicationState: 'unknown' | 'published'
  sourceHandoffFailureCode: string
}> | null {
  const data = value.data
  const errors = value.errors as unknown[]
  const error = errors.length === 1 && isRecord(errors[0]) ? errors[0] : null
  const claimsCommittedCheckpoint = (
    isRecord(data) && data.checkpoint_completed === true
  ) || error?.code === 'checkpoint_committed_source_handoff_failed'
  if (!claimsCommittedCheckpoint) return null

  const request = isRecord(payload.request) ? payload.request : null
  const binding = isRecord(payload.binding) ? payload.binding : null
  const checkpoint = isRecord(data) && isRecord(data.checkpoint) ? data.checkpoint : null
  const dataKeys = isRecord(data) ? Object.keys(data).sort().join(',') : ''
  const errorKeys = error === null ? '' : Object.keys(error).sort().join(',')
  const publicationState = isRecord(data) ? data.source_publication_state : null
  const continuationSafety = isRecord(data) ? data.continuation_safety : null
  if (
    action !== 'execute_semantic_operation' ||
    value.status !== 'error' ||
    (value.warnings as unknown[]).length !== 0 ||
    request === null ||
    Object.keys(request).sort().join(',') !== 'input,operation,schema_version' ||
    request.schema_version !== 'mathematical_research.mission_semantic_request.v1' ||
    request.operation !== 'checkpoint' ||
    !isRecord(request.input) ||
    Object.keys(request.input).length !== 0 ||
    binding === null ||
    Object.keys(binding).sort().join(',') !== 'executiveEpochId,rootThreadId' ||
    typeof binding.rootThreadId !== 'string' ||
    binding.rootThreadId.length === 0 ||
    typeof binding.executiveEpochId !== 'string' ||
    binding.executiveEpochId.length === 0 ||
    dataKeys !== 'canonical_effect,checkpoint,checkpoint_completed,continuation_safety,mathematical_effect,source_handoff_failure_code,source_publication_state' ||
    !isRecord(data) ||
    data.canonical_effect !== 'none' ||
    data.mathematical_effect !== 'none' ||
    data.checkpoint_completed !== true ||
    !['unknown', 'published'].includes(String(publicationState)) ||
    !['safe', 'unsafe', 'unverified'].includes(String(continuationSafety)) ||
    typeof data.source_handoff_failure_code !== 'string' ||
    !/^[a-z][a-z0-9_]{0,127}$/.test(data.source_handoff_failure_code) ||
    checkpoint === null ||
    Object.keys(checkpoint).sort().join(',') !== 'checkpoint_id,executive_epoch_id,state' ||
    typeof checkpoint.checkpoint_id !== 'string' ||
    checkpoint.checkpoint_id.length === 0 ||
    checkpoint.executive_epoch_id !== binding.executiveEpochId ||
    checkpoint.state !== 'checkpointed' ||
    error === null ||
    errorKeys !== 'checkpoint_completed,checkpoint_id,checkpoint_state,code,continuation_safety,executive_epoch_id,message,reason_code,source_publication_state' ||
    error.code !== 'checkpoint_committed_source_handoff_failed' ||
    typeof error.message !== 'string' ||
    error.message.length === 0 ||
    error.reason_code !== data.source_handoff_failure_code ||
    error.checkpoint_completed !== true ||
    error.checkpoint_id !== checkpoint.checkpoint_id ||
    error.executive_epoch_id !== checkpoint.executive_epoch_id ||
    error.checkpoint_state !== checkpoint.state ||
    error.source_publication_state !== publicationState ||
    error.continuation_safety !== continuationSafety
  ) {
    throw invalidWarningEnvelope(result)
  }
  return {
    checkpoint,
    continuationSafety: continuationSafety as 'safe' | 'unsafe' | 'unverified',
    sourcePublicationState: publicationState as 'unknown' | 'published',
    sourceHandoffFailureCode: data.source_handoff_failure_code as string,
  }
}

export class PythonMissionBridge implements MissionOwnerBridge {
  private readonly config: MissionBridgeConfig
  private staleFileReconciliation: Promise<void> | null = null
  private readonly activeInvocations = new Set<Readonly<{
    cancelController: AbortController
    forceController: AbortController
    pending: Promise<unknown>
  }>>()

  constructor(
    config: MissionBridgeConfig | MissionHostConfig,
    private readonly run: BridgeCommandRunner = defaultCommandRunner,
    private readonly writeRequest: BridgeRequestWriter = defaultRequestWriter,
  ) {
    this.config = {
      repoRoot: config.repoRoot,
      releaseSha: config.releaseSha,
      missionWorkspaceRoot: config.missionWorkspaceRoot,
      runtimeDir: config.runtimeDir,
      codexHome: config.codexHome,
      pythonPath: config.pythonPath,
      codexCliPath: config.codexCliPath,
      missionScriptPath: config.missionScriptPath,
      projectId: config.projectId,
      missionId: config.missionId,
      pollIntervalMs: config.pollIntervalMs,
      permissionProfileId: config.permissionProfileId,
      trustedMcpServerIds: config.trustedMcpServerIds,
      trustedAppIds: config.trustedAppIds,
    }
  }

  async cancelOutstanding(reason: unknown = 'operator_stop'): Promise<void> {
    const active = [...this.activeInvocations]
    for (const invocation of active) {
      if (reason === 'explicit_force_stop') {
        invocation.forceController.abort(reason)
      } else {
        invocation.cancelController.abort(reason)
      }
    }
    await Promise.allSettled(active.map(({ pending }) => pending))
  }

  private prepareRuntimeAndStaleFileReconciliation(mayStart: boolean): Promise<void> {
    if (this.staleFileReconciliation === null && mayStart) {
      // Assign synchronously before the first await so concurrent invocations
      // cannot create live request files ahead of this one-time stale scan.
      this.staleFileReconciliation = (async () => {
        await fs.promises.mkdir(this.config.runtimeDir, { recursive: true, mode: 0o700 })
        await this.reconcileStaleBridgeFilesOnce()
      })()
    }
    return this.staleFileReconciliation ?? Promise.resolve()
  }

  private async reconcileStaleBridgeFilesOnce(): Promise<void> {
    for (const entry of await fs.promises.readdir(this.config.runtimeDir, { withFileTypes: true })) {
      if (
        !entry.isFile() ||
        entry.isSymbolicLink() ||
        !MISSION_BRIDGE_RUNTIME_FILE.test(entry.name)
      ) {
        continue
      }
      const filePath = path.join(this.config.runtimeDir, entry.name)
      let stat: fs.Stats
      try {
        stat = await fs.promises.lstat(filePath)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
          continue
        }
        throw error
      }
      if (!stat.isFile() || stat.isSymbolicLink()) {
        continue
      }
      try {
        await fs.promises.unlink(filePath)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
          throw error
        }
      }
    }
  }

  private invoke(
    action: string,
    payload: JsonObject,
    callerSignal?: AbortSignal,
    warningObserver?: CheckpointSourceFailureWarningObserver,
  ): Promise<JsonObject> {
    if (callerSignal?.aborted) {
      return Promise.reject(new MissionBridgeCancelledError(callerSignal.reason))
    }
    const runtimeReady = this.prepareRuntimeAndStaleFileReconciliation(
      this.activeInvocations.size === 0,
    )
    const cancelController = new AbortController()
    const forceController = new AbortController()
    const relayAbort = (): void => {
      if (callerSignal?.reason === 'explicit_force_stop') {
        forceController.abort(callerSignal.reason)
      } else {
        cancelController.abort(callerSignal?.reason)
      }
    }
    if (callerSignal?.aborted) {
      relayAbort()
    } else {
      callerSignal?.addEventListener('abort', relayAbort, { once: true })
    }
    let invocation: Readonly<{
      cancelController: AbortController
      forceController: AbortController
      pending: Promise<unknown>
    }>
    const pending = this.invokeOnce(
      action,
      payload,
      cancelController.signal,
      forceController.signal,
      warningObserver,
      runtimeReady,
    ).finally(() => {
      callerSignal?.removeEventListener('abort', relayAbort)
      this.activeInvocations.delete(invocation)
    })
    invocation = { cancelController, forceController, pending }
    this.activeInvocations.add(invocation)
    return pending
  }

  private async invokeOnce(
    action: string,
    payload: JsonObject,
    signal: AbortSignal,
    forceSignal: AbortSignal,
    warningObserver?: CheckpointSourceFailureWarningObserver,
    runtimeReady: Promise<void> = Promise.resolve(),
  ): Promise<JsonObject> {
    let preserveCheckpointOutcome = false
    const checkpointInvocation = exactCheckpointInvocation(action, payload)
    const request = {
      schema_version: BRIDGE_REQUEST_SCHEMA_VERSION,
      action,
      binding: {
        project_id: this.config.projectId,
        mission_id: this.config.missionId,
      },
      payload,
    }
    const encoded = `${JSON.stringify(request)}\n`
    await runtimeReady
    if (forceSignal.aborted) {
      throw new MissionBridgeCancelledError(forceSignal.reason)
    }
    if (signal.aborted) {
      throw new MissionBridgeCancelledError(signal.reason)
    }
    const invocationId = `${process.pid}.${randomUUID()}`
    const requestPath = path.join(
      this.config.runtimeDir,
      `mission-bridge-request.${invocationId}.json`,
    )
    const responsePath = path.join(this.config.runtimeDir, `mission-bridge-response.${invocationId}.json`)
    try {
      await this.writeRequest(requestPath, encoded)
      if (forceSignal.aborted) {
        throw new MissionBridgeCancelledError(forceSignal.reason)
      }
      if (signal.aborted) {
        throw new MissionBridgeCancelledError(signal.reason)
      }
      const args = [
        this.config.missionScriptPath,
        'host-bridge',
        '--workspace-root',
        this.config.missionWorkspaceRoot,
        '--project-id',
        this.config.projectId,
        '--mission-id',
        this.config.missionId,
        '--request',
        requestPath,
        '--response',
        responsePath,
        '--format',
        'json',
      ] as const
      // Once an exact checkpoint owner call starts, ordinary cancellation must
      // wait for its exact owner disposition. SIGTERM could otherwise land
      // after the durable checkpoint (or writer release) but before successor
      // ownership and the closed result are emitted. Explicit force-stop remains wired.
      const commandSignal = checkpointInvocation === null
        ? signal
        : new AbortController().signal
      let result: BridgeCommandResult
      try {
        result = await this.run(this.config.pythonPath, args, {
          cwd: this.config.repoRoot,
          encoding: 'utf8',
          env: {
            PATH: MISSION_BRIDGE_PATH,
            PYTHONUTF8: '1',
            PYTHONDONTWRITEBYTECODE: '1',
          },
          shell: false,
          windowsHide: true,
          signal: commandSignal,
          forceSignal,
        })
      } catch (error) {
        if (forceSignal.aborted) {
          if (checkpointInvocation !== null) preserveCheckpointOutcome = true
          throw new MissionBridgeCancelledError(forceSignal.reason)
        }
        if (checkpointInvocation !== null) {
          preserveCheckpointOutcome = true
          const responseError =
            error instanceof Error && 'code' in error
              ? String((error as NodeJS.ErrnoException).code)
              : error instanceof Error
                ? error.name
                : 'unknown_runner_error'
          throw new CheckpointDispositionUnverifiedBridgeError(
            'RH Mission checkpoint runner ended without a verifiable owner disposition',
            null,
            [],
            null,
            {
              exitCode: null,
              terminationSignal: null,
              stderrTail: '',
              stderrTruncated: false,
              responseError,
            },
            checkpointInvocation.rootThreadId,
            checkpointInvocation.executiveEpochId,
          )
        }
        if (signal.aborted) {
          throw new MissionBridgeCancelledError(signal.reason)
        }
        throw error
      }
      const unverifiedCheckpointDisposition = (
        message: string,
        responseError: string,
        ownerErrors: readonly unknown[] = [],
      ): CheckpointDispositionUnverifiedBridgeError => {
        if (checkpointInvocation === null) {
          throw new Error('checkpoint disposition error requires an exact checkpoint invocation')
        }
        preserveCheckpointOutcome = true
        return new CheckpointDispositionUnverifiedBridgeError(
          message,
          result.exitCode,
          ownerErrors,
          result.terminationSignal,
          processDiagnostic(result, responseError),
          checkpointInvocation.rootThreadId,
          checkpointInvocation.executiveEpochId,
        )
      }
      let parsed: unknown
      let warning: CheckpointSourceFailureWarning | null
      let committedCheckpoint: ReturnType<typeof checkpointCommittedSourceHandoffFailure>
      let completedCheckpoint: JsonObject | null
      let rejectedCheckpoint: JsonObject | null
      try {
        parsed = JSON.parse(await fs.promises.readFile(responsePath, 'utf8'))
        assertClosedToolResult(parsed, result)
        warning = checkpointSourceFailureWarning(parsed, result, action, payload)
        committedCheckpoint = checkpointCommittedSourceHandoffFailure(
          parsed,
          result,
          action,
          payload,
        )
        completedCheckpoint = completedCheckpointResult(parsed, result, action, payload)
        rejectedCheckpoint = rejectedCheckpointResult(parsed, result, action, payload)
      } catch (error) {
        if (forceSignal.aborted) {
          if (checkpointInvocation !== null) preserveCheckpointOutcome = true
          throw new MissionBridgeCancelledError(forceSignal.reason)
        }
        if (checkpointInvocation !== null) {
          const responseError =
            error instanceof MissionBridgeError
              ? error.processDiagnostic.responseError ?? error.name
              : error instanceof Error && 'code' in error
                ? String((error as NodeJS.ErrnoException).code)
                : error instanceof Error
                  ? error.name
                  : 'unknown_response_error'
          throw unverifiedCheckpointDisposition(
            'RH Mission checkpoint process settled without a verifiable owner disposition',
            responseError,
          )
        }
        if (signal.aborted) {
          throw new MissionBridgeCancelledError(signal.reason)
        }
        if (error instanceof MissionBridgeError) throw error
        throw new MissionBridgeError(
          'RH Mission bridge response file was not one JSON object',
          result.exitCode,
          [],
          result.terminationSignal,
          processDiagnostic(
            result,
            error instanceof Error && 'code' in error
              ? String((error as NodeJS.ErrnoException).code)
              : error instanceof Error
                ? error.name
                : 'unknown_response_error',
          ),
        )
      }
      if (committedCheckpoint !== null) {
        preserveCheckpointOutcome = true
        throw new CheckpointCommittedSourceHandoffBridgeError(
          result.exitCode,
          parsed.errors as readonly unknown[],
          result.terminationSignal,
          processDiagnostic(result),
          committedCheckpoint.checkpoint,
          committedCheckpoint.continuationSafety,
          committedCheckpoint.sourcePublicationState,
          committedCheckpoint.sourceHandoffFailureCode,
        )
      }
      if (completedCheckpoint !== null) {
        preserveCheckpointOutcome = true
      } else if (forceSignal.aborted) {
        if (checkpointInvocation !== null) preserveCheckpointOutcome = true
        throw new MissionBridgeCancelledError(forceSignal.reason)
      } else if (
        checkpointInvocation !== null &&
        rejectedCheckpoint === null
      ) {
        throw unverifiedCheckpointDisposition(
          'RH Mission checkpoint process settled without the exact completed or precommit owner disposition',
          'checkpoint_disposition_unverified',
          parsed.errors as readonly unknown[],
        )
      } else if (signal.aborted) {
        if (checkpointInvocation !== null) preserveCheckpointOutcome = true
        throw new MissionBridgeCancelledError(signal.reason)
      } else if (rejectedCheckpoint !== null) {
        // This is the one exact owner-proven precommit result; it may remain a
        // normal model-facing rejection/unavailability when not cancelled.
      }
      if (result.exitCode !== 0 || parsed.status !== 'ok') {
        throw new MissionBridgeError(
          `RH Mission owner rejected host action ${action}`,
          result.exitCode,
          parsed.errors as readonly unknown[],
          result.terminationSignal,
          processDiagnostic(result),
        )
      }
      if (warning !== null && warningObserver !== undefined) {
        try {
          const observation = warningObserver(warning)
          if (observation && typeof (observation as Promise<unknown>).then === 'function') {
            void Promise.resolve(observation).catch(() => undefined)
          }
        } catch { /* operational visibility cannot reject a completed checkpoint */ }
      }
      return parsed.data as JsonObject
    } finally {
      const cleanupPaths = [requestPath, responsePath]
      const cleanup = await Promise.allSettled(
        cleanupPaths.map(async (filePath) => {
          try {
            await fs.promises.unlink(filePath)
          } catch (error) {
            if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
              throw error
            }
          }
        }),
      )
      const cleanupFailure = cleanup.find(
        (result): result is PromiseRejectedResult => result.status === 'rejected',
      )
      if (cleanupFailure !== undefined) {
        this.staleFileReconciliation = null
        if (preserveCheckpointOutcome) {
          // Re-arm the existing exact-pattern startup reconciler. Calls already
          // active never rescan; the next call that begins while idle owns it.
        } else {
          throw cleanupFailure.reason
        }
      }
    }
  }

  reconstruct(signal?: AbortSignal): Promise<JsonObject> {
    return this.invoke('reconstruct', {}, signal)
  }

  hostSnapshot(signal?: AbortSignal): Promise<JsonObject> {
    return this.invoke('host_snapshot', {}, signal)
  }

  authorizeExecutiveEpoch(expectedCut: MissionAuthorizationCut, signal?: AbortSignal): Promise<JsonObject> {
    return this.invoke('authorize_executive_epoch', { expected_cut: expectedCut }, signal)
  }

  validateSuspendedEpochCut(input: SuspendedEpochCutInput, signal?: AbortSignal): Promise<JsonObject> {
    return this.invoke('validate_suspended_epoch_cut', {
      expected_cut: input.expectedCut,
      executiveEpochId: input.executiveEpochId,
      rootThreadId: input.rootThreadId,
      workspaceRoot: input.workspaceRoot,
    }, signal)
  }

  bindExecutiveEpoch(input: BindExecutiveEpochInput, signal?: AbortSignal): Promise<JsonObject> {
    return this.invoke('bind_executive_epoch', {
      executiveEpochId: input.executiveEpochId,
      rootThreadId: input.rootThreadId,
      workspaceRoot: input.workspaceRoot,
    }, signal)
  }

  executeSemanticOperation(
    request: unknown,
    binding: SemanticOperationBinding,
    signal?: AbortSignal,
    rootQueryContext?: Readonly<{ cursor_mac_key: string }>,
    warningObserver?: CheckpointSourceFailureWarningObserver,
  ): Promise<JsonObject> {
    if (rootQueryContext !== undefined && !/^[0-9a-f]{64}$/.test(rootQueryContext.cursor_mac_key)) {
      throw new Error('Private root query signing context is invalid')
    }
    return this.invoke('execute_semantic_operation', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
      },
      ...(rootQueryContext === undefined ? {} : { root_query_context: rootQueryContext }),
    }, signal, warningObserver)
  }

  issueHistoricalReadGrant(
    request: unknown,
    binding: HistoricalReadGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('issue_historical_read_grant', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        childThreadId: binding.childThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
      },
    }, signal)
  }

  issueCandidateA1ReviewGrant(
    request: unknown,
    binding: CandidateA1ReviewGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('issue_candidate_a1_review_grant', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        childThreadId: binding.childThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
      },
    }, signal)
  }

  issueResearchReadGrant(
    request: unknown,
    binding: ResearchReadGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('issue_research_read_grant', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        childThreadId: binding.childThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
      },
    }, signal)
  }

  executeResearchRead(
    request: unknown,
    grant: JsonObject,
    binding: ResearchReadBinding,
    signal: AbortSignal | undefined,
    researchQueryContext: Readonly<{ cursor_mac_key: string }>,
  ): Promise<JsonObject> {
    return this.invoke('execute_research_read', {
      request,
      grant,
      research_query_context: { cursor_mac_key: researchQueryContext.cursor_mac_key },
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        callerThreadId: binding.callerThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
        turnId: binding.turnId,
        grantId: binding.grantId,
        assignmentId: binding.assignmentId,
      },
    }, signal)
  }

  executeDelegatedRead(
    request: unknown,
    grant: JsonObject,
    binding: DelegatedReadBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('execute_delegated_read', {
      request,
      grant,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        callerThreadId: binding.callerThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
        turnId: binding.turnId,
        grantId: binding.grantId,
      },
    }, signal)
  }

  executeCandidateA1Review(
    request: unknown,
    grant: JsonObject,
    binding: CandidateA1ReviewBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('execute_candidate_a1_review', {
      request,
      grant,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        callerThreadId: binding.callerThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
        turnId: binding.turnId,
      },
    }, signal)
  }

  openCompleteClaimAdmissionCase(
    request: unknown,
    binding: SemanticOperationBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('open_complete_claim_admission_case', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
      },
    }, signal)
  }

  issueAdmissionReviewGrant(
    request: unknown,
    binding: AdmissionGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('issue_admission_review_grant', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        childThreadId: binding.childThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
      },
    }, signal)
  }

  issueAdmissionDecisionGrant(
    request: unknown,
    binding: AdmissionGrantBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('issue_admission_decision_grant', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        childThreadId: binding.childThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
      },
    }, signal)
  }

  executeAdmissionReview(
    request: unknown,
    grant: JsonObject,
    binding: AdmissionBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('execute_admission_review', {
      request,
      grant,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        callerThreadId: binding.callerThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
        turnId: binding.turnId,
      },
    }, signal)
  }

  executeAdmissionDecision(
    request: unknown,
    grant: JsonObject,
    binding: AdmissionBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('execute_admission_decision', {
      request,
      grant,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
        callerThreadId: binding.callerThreadId,
        parentThreadId: binding.parentThreadId,
        depth: binding.depth,
        turnId: binding.turnId,
      },
    }, signal)
  }

  executeFormalAttempt(
    request: FormalAttemptRequest,
    binding: ExecutiveEpochBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('execute_formal_attempt', {
      request,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
      },
      runtime: {
        release_root: this.config.repoRoot,
        release_commit: this.config.releaseSha,
        state_root: path.join(this.config.runtimeDir, 'formal-attempt'),
        codex_executable: this.config.codexCliPath,
        codex_home: this.config.codexHome,
        outer_containment_id: this.config.permissionProfileId,
        trusted_mcp_server_ids: [...this.config.trustedMcpServerIds],
        trusted_app_ids: [...this.config.trustedAppIds],
        polling_cadence_seconds: this.config.pollIntervalMs / 1000,
      },
    }, signal)
  }

  captureNativeMaterialObservation(
    observation: unknown,
    binding: ExecutiveEpochBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('capture_native_material_observation', {
      observation,
      binding: {
        rootThreadId: binding.rootThreadId,
        executiveEpochId: binding.executiveEpochId,
      },
    }, signal)
  }

  fenceMission(
    context: GoalStopContext,
    binding: ExecutiveEpochBinding,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('fence_mission', {
      context,
      binding: {
        executiveEpochId: binding.executiveEpochId,
      },
    }, signal)
  }

  recordDirectFailedExecutiveEpoch(
    input: DirectFailedExecutiveEpochInput,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    return this.invoke('record_direct_failed_executive_epoch', {
      executiveEpochId: input.executiveEpochId,
      reconciliation: input.reconciliation,
    }, signal)
  }
}
