import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { CodexAppServerBoundary } from '../../../packages/codex-thread-core/dist/index.js'

import {
  acquireRuntimeLock,
  readMissionHostConfigFromEnvironment,
} from './config.js'
import {
  CheckpointCommittedSourceHandoffBridgeError,
  MissionBridgeError,
  PythonMissionBridge,
} from './mission-bridge.js'
import { MissionExecutionObserver, observeMissionProcessSource } from './execution-observer.js'
import { createLogger } from '../../../packages/codex-thread-core/dist/logger.js'
import {
  MissionHost,
  type CodexEpochBoundaryFactory,
  type MissionOwnerBridgeFactory,
} from './mission-host.js'
import {
  LocalAgentCommunicationsNotificationOutbox,
  clearAgentCommunicationsEnvironmentForChildren,
  readAgentCommunicationsNotificationConfig,
} from './agent-communications.js'

const SAFE_TERMINATION_SIGNALS = new Set<string>(Object.keys(os.constants.signals))
const SAFE_ERROR_CLASSES = new Set([
  'AggregateError',
  'Error',
  'GoalDelegationDepthError',
  'GoalDelegationParentError',
  'GoalEpochCancelledError',
  'GoalEpochStartEffectUnknownError',
  'GoalEpochMissionConsistencyError',
  'GoalEpochSharedAuthorityLossError',
  'JsonRpcResponseError',
  'UnknownError',
])
const SAFE_JSON_RPC_METHODS = new Set([
  'config/read',
  'initialize',
  'thread/archive',
  'thread/goal/clear',
  'thread/goal/get',
  'thread/goal/set',
  'thread/list',
  'thread/read',
  'thread/resume',
  'thread/start',
  'turn/interrupt',
  'turn/start',
])
const FORCE_STOP_PENDING_FILE = 'force-stop.pending'
const CHECKPOINT_STOP_PENDING_FILE = 'checkpoint-stop.pending'
const SINGLE_EPOCH_CANARY_PENDING_FILE = 'single-epoch-canary.pending'

type PendingForceStopReconciler = Pick<MissionHost, 'reconcilePendingExplicitForceStop'>

export async function consumePendingForceStop(
  runtimeDir: string,
  createHost: () => PendingForceStopReconciler,
): Promise<void> {
  const markerPath = path.join(runtimeDir, FORCE_STOP_PENDING_FILE)
  try {
    await fs.promises.lstat(markerPath)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return
    }
    throw error
  }
  await createHost().reconcilePendingExplicitForceStop()
  await fs.promises.unlink(markerPath)
}

async function consumePendingEmptyMarker(
  runtimeDir: string,
  markerName: string,
  invalidMarkerBehavior: 'ignore' | 'reject' = 'ignore',
): Promise<boolean> {
  const markerPath = path.join(runtimeDir, markerName)
  let details: fs.Stats
  try {
    details = await fs.promises.lstat(markerPath)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return false
    }
    throw error
  }
  if (!details.isFile() || details.isSymbolicLink() || details.size !== 0) {
    if (invalidMarkerBehavior === 'reject') {
      throw new Error('single-epoch canary marker must be an empty regular file')
    }
    return false
  }
  try {
    await fs.promises.unlink(markerPath)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
      return false
    }
    throw error
  }
  return true
}

export async function consumePendingCheckpointStop(runtimeDir: string): Promise<boolean> {
  return consumePendingEmptyMarker(runtimeDir, CHECKPOINT_STOP_PENDING_FILE)
}

export async function consumePendingSingleEpochCanary(runtimeDir: string): Promise<boolean> {
  return consumePendingEmptyMarker(runtimeDir, SINGLE_EPOCH_CANARY_PENDING_FILE, 'reject')
}

function safeTerminationSignal(value: unknown): string | null {
  return typeof value === 'string' && SAFE_TERMINATION_SIGNALS.has(value) ? value : null
}

function safeOwnerErrorCodes(ownerErrors: readonly unknown[]): string[] {
  const codes = new Set<string>()
  for (const item of ownerErrors) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      continue
    }
    const code = (item as Record<string, unknown>).code
    if (typeof code === 'string' && /^[a-z][a-z0-9_]{0,95}$/.test(code)) {
      codes.add(code)
    }
  }
  return [...codes]
}

function stderrTailLogFields(value: string): Record<string, unknown> {
  const byteCount = Buffer.byteLength(value, 'utf8')
  return {
    stderr_tail_present: byteCount > 0,
    stderr_tail_character_count: Array.from(value).length,
    stderr_tail_byte_count: byteCount,
    stderr_tail_sha256:
      byteCount === 0
        ? null
        : createHash('sha256').update(value, 'utf8').digest('hex'),
  }
}

function checkpointSourceHandoffLogFields(error: unknown): Record<string, unknown> {
  const failure = error instanceof CheckpointCommittedSourceHandoffBridgeError
    ? error
    : error instanceof Error && error.cause instanceof CheckpointCommittedSourceHandoffBridgeError
      ? error.cause
      : null
  if (failure === null) return {}
  const checkpointId = failure.checkpoint.checkpoint_id
  const executiveEpochId = failure.checkpoint.executive_epoch_id
  const checkpointState = failure.checkpoint.state
  if (
    typeof checkpointId !== 'string' ||
    !/^checkpoint:[0-9a-f]{64}$/.test(checkpointId) ||
    typeof executiveEpochId !== 'string' ||
    !/^epoch:[0-9a-f]{64}$/.test(executiveEpochId) ||
    checkpointState !== 'checkpointed' ||
    !['unknown', 'published'].includes(failure.sourcePublicationState) ||
    !['safe', 'unsafe', 'unverified'].includes(failure.continuationSafety) ||
    !/^[a-z][a-z0-9_]{0,127}$/.test(failure.sourceHandoffFailureCode)
  ) {
    return {}
  }
  return {
    checkpoint_source_handoff: {
      checkpoint_completed: true,
      checkpoint_id: checkpointId,
      executive_epoch_id: executiveEpochId,
      checkpoint_state: checkpointState,
      source_publication_state: failure.sourcePublicationState,
      source_handoff_failure_code: failure.sourceHandoffFailureCode,
      continuation_safety: failure.continuationSafety,
    },
  }
}

export function serializeErrorForLog(error: unknown): Record<string, unknown> {
  if (error instanceof AggregateError) {
    return {
      type: 'AggregateError',
      cause_count: error.errors.length,
      causes: error.errors.map((item) => serializeErrorForLog(item)),
    }
  }
  if (error instanceof MissionBridgeError) {
    const ownerErrorCodes = safeOwnerErrorCodes(error.ownerErrors)
    return {
      type: 'MissionBridgeError',
      process_diagnostic: {
        exit_code: Number.isSafeInteger(error.processDiagnostic.exitCode)
          ? error.processDiagnostic.exitCode
          : null,
        termination_signal: safeTerminationSignal(error.processDiagnostic.terminationSignal),
        ...stderrTailLogFields(error.processDiagnostic.stderrTail),
        stderr_truncated: error.processDiagnostic.stderrTruncated,
        response_error_present:
          typeof error.processDiagnostic.responseError === 'string' &&
          error.processDiagnostic.responseError.length > 0,
      },
      owner_error_count: error.ownerErrors.length,
      owner_error_codes: ownerErrorCodes,
      ...checkpointSourceHandoffLogFields(error),
    }
  }
  return {
    type: error instanceof Error ? 'Error' : 'UnknownError',
    ...(error instanceof Error && errorClassForLog(error) !== 'Error'
      ? { error_class: errorClassForLog(error), ...jsonRpcErrorFieldsForLog(error) }
      : {}),
    ...checkpointSourceHandoffLogFields(error),
  }
}

export function errorClassForLog(error: unknown): string {
  if (!(error instanceof Error)) {
    return 'UnknownError'
  }
  const constructorName = error.constructor?.name
  return typeof constructorName === 'string' && SAFE_ERROR_CLASSES.has(constructorName)
    ? constructorName
    : 'Error'
}

export function jsonRpcErrorFieldsForLog(error: unknown): Record<string, unknown> {
  if (errorClassForLog(error) !== 'JsonRpcResponseError') {
    return {}
  }
  const responseError = error as Error & {
    jsonRpcCode?: unknown
    jsonRpcMethod?: unknown
  }
  const code = responseError.jsonRpcCode
  const method = responseError.jsonRpcMethod
  return {
    json_rpc_code:
      typeof code === 'number' && Number.isSafeInteger(code) ? code : null,
    json_rpc_code_present: code !== null && code !== undefined,
    ...(typeof method === 'string' && SAFE_JSON_RPC_METHODS.has(method)
      ? { json_rpc_method: method }
      : {}),
  }
}

function isDirectEntryPoint(): boolean {
  if (typeof process.argv[1] !== 'string') {
    return false
  }
  try {
    return fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))
  } catch {
    return false
  }
}

export function parseMissionHostInvocation(argv: readonly string[]): null | Readonly<{
  executiveEpochId: string
  rootThreadId: string
}> {
  if (argv.length === 0) return null
  if (argv.length !== 3 || argv[0] !== '--reconcile-stopped' ||
      !/^epoch:[0-9a-f]{64}$/.test(argv[1]!) ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(argv[2]!)) {
    throw new Error('RH Mission Host accepts no free-form argv; configure its explicit installation environment bindings')
  }
  return { executiveEpochId: argv[1]!, rootThreadId: argv[2]! }
}

export async function reconcileStoppedEpochAndReleaseLock(
  host: Pick<MissionHost, 'reconcileStoppedEpoch'>,
  expected: Parameters<MissionHost['reconcileStoppedEpoch']>[0],
  lock: Readonly<{ release(): Promise<void> }>,
): ReturnType<MissionHost['reconcileStoppedEpoch']> {
  let failed = false
  let failure: unknown
  let result!: Awaited<ReturnType<MissionHost['reconcileStoppedEpoch']>>
  try {
    result = await host.reconcileStoppedEpoch(expected)
  } catch (error) {
    failed = true
    failure = error
  }
  try {
    await lock.release()
  } catch (error) {
    failure = failed
      ? new AggregateError([failure, error], 'RH stopped reconciliation failed and its runtime lock did not release')
      : error
    failed = true
  }
  if (failed) throw failure
  return result
}

export async function runMissionHostProcess(env: NodeJS.ProcessEnv = process.env): Promise<void> {
  if (process.platform !== 'linux' || process.getuid?.() === 0) {
    throw new Error('RH Mission Host must run on Linux as a dedicated unprivileged account')
  }
  const recovery = parseMissionHostInvocation(process.argv.slice(2))
  const notificationConfiguration = readAgentCommunicationsNotificationConfig(env)
  const config = readMissionHostConfigFromEnvironment(env)
  clearAgentCommunicationsEnvironmentForChildren(process.env)
  if (notificationConfiguration.warning !== null) {
    process.stderr.write(`${JSON.stringify({
      status: 'warning',
      subsystem: 'agent_communications',
      event: 'notification_outbox_disabled',
      reason: notificationConfiguration.warning,
    })}\n`)
  }
  const lock = await acquireRuntimeLock(config.lockPath, config.releaseSha)
  const bridgeFactory: MissionOwnerBridgeFactory = () => new PythonMissionBridge(config)
  const boundaryFactory: CodexEpochBoundaryFactory = {
    create(goalWorkspaceRoot, callbacks) {
      return new CodexAppServerBoundary(config.codexCliPath, goalWorkspaceRoot, {
        restartOnUnexpectedExit: false,
        goalPolicy: {
          permissionProfileId: config.permissionProfileId,
          expectedCliVersion: config.expectedCliVersion,
          expectedModelProvider: config.expectedModelProvider,
          expectedModel: config.expectedModel,
          reasoningEffort: config.reasoningEffort,
          modelCatalogPath: config.modelCatalogPath,
          appServerCwd: config.runtimeDir,
          protectedRoot: config.codexHome,
          readOnlyRoots: [config.repoRoot],
        },
        trustedMcpServerIds: config.trustedMcpServerIds,
        trustedAppIds: config.trustedAppIds,
        dynamicToolHandler: callbacks.onDynamicToolCall,
        onGoalEpochDiagnostic: callbacks.onGoalEpochDiagnostic,
        onExecutionObservation: callbacks.onExecutionObservation,
        onNativeMaterialObserved: callbacks.onNativeMaterialObserved,
        onGoalTermination: callbacks.onGoalTermination,
        onMissionFence: callbacks.onMissionFence,
        onGoalEpochIdentityMaterialized: callbacks.onEpochIdentityMaterialized,
        onDescendantToolGrantRevoked: callbacks.onDescendantToolGrantRevoked,
      })
    },
  }
  const notificationSink = notificationConfiguration.config === null
    ? null
    : new LocalAgentCommunicationsNotificationOutbox(
        notificationConfiguration.config,
      )
  let executionObserver: MissionExecutionObserver | null = null
  const createHost = () => new MissionHost(
    config,
    bridgeFactory,
    boundaryFactory,
    undefined,
    undefined,
    undefined,
    notificationSink,
    () => consumePendingCheckpointStop(config.runtimeDir),
    () => consumePendingSingleEpochCanary(config.runtimeDir),
    executionObserver,
  )
  if (recovery !== null) {
    // No marker consumption, signal-to-resume handling, or host.run invocation
    // is reachable from this closed operator-only entry.
    const result = await reconcileStoppedEpochAndReleaseLock(createHost(), recovery, lock)
    process.stdout.write(`${JSON.stringify({ status: 'ok', mission_id: config.missionId, result })}\n`)
    return
  }
  try {
    executionObserver = new MissionExecutionObserver(config, {
      limits: config.observationLimits, source: await observeMissionProcessSource(config),
    })
  } catch {
    createLogger('rh_mission_host').error('rh_mission.observation_health', {
      mission_id: config.missionId, state: 'degraded', error_code: 'initialization_failed',
    })
  }
  await consumePendingForceStop(config.runtimeDir, createHost)
  const host = createHost()
  const abortController = new AbortController()
  let forceStopPromise: Promise<void> | null = null
  const requestStop = () => abortController.abort('operator_stop')
  const requestForceStop = () => {
    abortController.abort('explicit_force_stop')
    forceStopPromise ??= host.requestExplicitForceStop()
    void forceStopPromise.catch(() => undefined)
  }
  process.once('SIGINT', requestStop)
  process.once('SIGTERM', requestStop)
  process.on('SIGUSR2', requestForceStop)

  let failure: unknown
  let failed = false
  try {
    const result = await host.run(abortController.signal)
    process.stdout.write(`${JSON.stringify({ status: 'ok', mission_id: config.missionId, result })}\n`)
  } catch (error) {
    failed = true
    failure = error
  }
  if (forceStopPromise !== null) {
    try {
      await forceStopPromise
    } catch (error) {
      failure = failed
        ? new AggregateError([failure, error], 'RH Mission Host failed during explicit force-stop')
        : error
      failed = true
    }
  }
  process.off('SIGINT', requestStop)
  process.off('SIGTERM', requestStop)
  process.off('SIGUSR2', requestForceStop)
  // Research and required containment have finished; diagnostic drain is bounded
  // independently and cannot turn their outcome into a Mission failure.
  let observationDrainTimer: NodeJS.Timeout | undefined
  await Promise.race([
    executionObserver?.close().catch(() => undefined) ?? Promise.resolve(),
    new Promise<void>((resolve) => { observationDrainTimer = setTimeout(resolve, 2000) }),
  ])
  if (observationDrainTimer) clearTimeout(observationDrainTimer)
  try {
    await lock.release()
  } catch (error) {
    failure = failed
      ? new AggregateError([failure, error], 'RH Mission Host failed and its runtime lock did not release')
      : error
    failed = true
  }
  if (failed) {
    throw failure
  }
}

if (isDirectEntryPoint()) {
  runMissionHostProcess().catch((error: unknown) => {
    process.stderr.write(`${JSON.stringify({
      status: 'error',
      error_class: errorClassForLog(error),
      ...jsonRpcErrorFieldsForLog(error),
      error: serializeErrorForLog(error),
    })}\n`)
    process.exitCode = 1
  })
}
