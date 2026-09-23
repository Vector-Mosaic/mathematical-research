import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import {
  GoalEpochMissionConsistencyError,
  GoalEpochSharedAuthorityLossError,
} from '../../../packages/codex-thread-core/dist/index.js'

import {
  consumePendingCheckpointStop,
  consumePendingForceStop,
  consumePendingSingleEpochCanary,
  errorClassForLog,
  jsonRpcErrorFieldsForLog,
  parseMissionHostInvocation,
  reconcileStoppedEpochAndReleaseLock,
  serializeErrorForLog,
} from './main.js'
import {
  CheckpointCommittedSourceHandoffBridgeError,
  MissionBridgeError,
} from './mission-bridge.js'
import { measureMissionLaunchCompatibility } from './launch-compatibility.js'
import { type MissionHostConfig } from './config.js'
import { fileURLToPath } from 'node:url'

test('stopped reconciliation invocation admits only exact owner epoch and root identities', () => {
  const executiveEpochId = `epoch:${'a'.repeat(64)}`
  const rootThreadId = '019fbcab-1234-7000-8000-123456789abc'
  assert.equal(parseMissionHostInvocation([]), null)
  assert.deepEqual(parseMissionHostInvocation(['--reconcile-stopped', executiveEpochId, rootThreadId]), {
    executiveEpochId, rootThreadId,
  })
  for (const argv of [
    ['--reconcile-stopped'],
    ['--reconcile-stopped', executiveEpochId],
    ['--reconcile-stopped', executiveEpochId, rootThreadId, '--start'],
    ['--start'],
    ['--reconcile-stopped', 'epoch:wrong', rootThreadId],
    ['--reconcile-stopped', executiveEpochId, 'thread.other'],
    ['--reconcile-stopped', executiveEpochId, '019fbcab-1234-4000-8000-123456789abc'],
  ]) {
    assert.throws(() => parseMissionHostInvocation(argv))
  }
})

for (const operationFails of [false, true]) {
  for (const releaseFails of [false, true]) {
    test(`stopped reconciliation preserves operation failure=${operationFails} and lock-release failure=${releaseFails}`, async () => {
      const expected = { executiveEpochId: `epoch:${'a'.repeat(64)}`, rootThreadId: '019fbcab-1234-7000-8000-123456789abc' }
      const result = { ...expected, state: 'failed_before_checkpoint' as const,
        checkpointId: null, failureReason: 'mission_consistency_failure' }
      const operationError = new GoalEpochMissionConsistencyError('PRIVATE_RECONCILIATION_FAILURE')
      const releaseError = new Error('PRIVATE_LOCK_RELEASE_FAILURE')
      const calls: string[] = []
      const operation = reconcileStoppedEpochAndReleaseLock({
        async reconcileStoppedEpoch(actual) {
          calls.push('reconcile')
          assert.deepEqual(actual, expected)
          if (operationFails) throw operationError
          return result
        },
      }, expected, {
        async release() {
          calls.push('release')
          if (releaseFails) throw releaseError
        },
      })
      if (!operationFails && !releaseFails) {
        assert.equal(await operation, result)
      } else {
        await assert.rejects(operation, (error) => {
          if (operationFails && releaseFails) {
            assert.ok(error instanceof AggregateError)
            assert.deepEqual(error.errors, [operationError, releaseError])
            assert.doesNotMatch(JSON.stringify(serializeErrorForLog(error)), /PRIVATE_/)
          } else {
            assert.equal(error, operationFails ? operationError : releaseError)
          }
          return true
        })
      }
      assert.deepEqual(calls, ['reconcile', 'release'])
    })
  }
}

test('fatal containment aggregate logs retain original typed failure and safe owner code without private content', () => {
  for (const original of [
    new GoalEpochMissionConsistencyError('PRIVATE_ORIGINAL_CONSISTENCY_FACTS'),
    new GoalEpochSharedAuthorityLossError('PRIVATE_ORIGINAL_CUSTODY_FACTS'),
  ]) {
    const callback = new MissionBridgeError('PRIVATE_CALLBACK_MESSAGE', 1, [{
      code: 'workspace_error', message: 'PRIVATE_OWNER_DETAIL',
    }])
    const error = new AggregateError([
      original,
      new AggregateError([callback], 'PRIVATE_CONTAINMENT_MESSAGE'),
    ], 'PRIVATE_OUTER_MESSAGE')
    const serialized = serializeErrorForLog(error)
    const causes = serialized.causes as Record<string, unknown>[]
    assert.equal(serialized.type, 'AggregateError')
    assert.equal(serialized.cause_count, 2)
    assert.deepEqual(causes[0], { type: 'Error', error_class: original.constructor.name })
    assert.equal(causes[1]?.type, 'AggregateError')
    const nested = causes[1]?.causes as Record<string, unknown>[]
    assert.equal(nested[0]?.type, 'MissionBridgeError')
    assert.deepEqual(nested[0]?.owner_error_codes, ['workspace_error'])
    assert.doesNotMatch(JSON.stringify(serialized), /PRIVATE_/)
  }
})

test('committed checkpoint handoff failures retain safe operational facts without private content', () => {
  const checkpoint = {
    checkpoint_id: `checkpoint:${'a'.repeat(64)}`,
    executive_epoch_id: `epoch:${'b'.repeat(64)}`,
    state: 'checkpointed',
  }
  for (const expected of [
    {
      continuationSafety: 'unsafe' as const,
      sourcePublicationState: 'published' as const,
      reasonCode: 'checkpoint_source_reacquisition_failed',
      wrap: (cause: Error) => new GoalEpochSharedAuthorityLossError(
        'PRIVATE_SHARED_AUTHORITY_DETAIL',
        { cause },
      ),
    },
    {
      continuationSafety: 'unverified' as const,
      sourcePublicationState: 'unknown' as const,
      reasonCode: 'checkpoint_source_writer_state_unverified',
      wrap: (cause: Error) => new GoalEpochMissionConsistencyError(
        'PRIVATE_CONSISTENCY_DETAIL',
        { cause },
      ),
    },
    {
      continuationSafety: 'safe' as const,
      sourcePublicationState: 'unknown' as const,
      reasonCode: 'checkpoint_source_path_unsafe',
      wrap: (cause: Error) => new GoalEpochMissionConsistencyError(
        'PRIVATE_SAFE_WRITER_SOURCE_DETAIL',
        { cause },
      ),
    },
  ]) {
    const bridgeError = new CheckpointCommittedSourceHandoffBridgeError(
      1,
      [{ code: 'checkpoint_committed_source_handoff_failed', message: 'PRIVATE_OWNER' }],
      null,
      {
        exitCode: 1,
        terminationSignal: null,
        stderrTail: 'PRIVATE_STDERR',
        stderrTruncated: false,
        responseError: 'PRIVATE_RESPONSE',
      },
      checkpoint,
      expected.continuationSafety,
      expected.sourcePublicationState,
      expected.reasonCode,
    )
    const serialized = serializeErrorForLog(expected.wrap(bridgeError))
    assert.deepEqual(serialized.checkpoint_source_handoff, {
      checkpoint_completed: true,
      checkpoint_id: checkpoint.checkpoint_id,
      executive_epoch_id: checkpoint.executive_epoch_id,
      checkpoint_state: 'checkpointed',
      source_publication_state: expected.sourcePublicationState,
      source_handoff_failure_code: expected.reasonCode,
      continuation_safety: expected.continuationSafety,
    })
    assert.doesNotMatch(JSON.stringify(serialized), /PRIVATE_/)
  }
})

test('inconclusive compatibility reports sizes without prompt content or an artificial refusal', (t) => {
  const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
  const config = { expectedModel: 'gpt-6-astra', modelCatalogPath: path.join(sourceRoot, 'services/rh-mission-host/test-fixtures/codex-model-catalog.0.153.4.json') } as MissionHostConfig
  // This source asset supplies the fixture bytes. Only its installed ownership
  // is modeled; actual mode/type/symlink checks and catalog contents stay real.
  const nativeLstatSync = fs.lstatSync
  t.mock.method(fs, 'lstatSync', ((...args: Parameters<typeof fs.lstatSync>) => {
    const stat = Reflect.apply(nativeLstatSync, fs, args) as fs.Stats | fs.BigIntStats | undefined
    if (!stat || args[0] !== config.modelCatalogPath) return stat
    return Object.assign(Object.create(Object.getPrototypeOf(stat)), stat, {
      uid: typeof stat.uid === 'bigint' ? 0n : 0,
    })
  }) as typeof fs.lstatSync)
  const report = measureMissionLaunchCompatibility(config, {
    objective: 'PRIVATE_OBJECTIVE', developerInstructions: 'PRIVATE_PROOF_🧭'.repeat(40000),
    dynamicTools: [], environments: [{ environmentId: 'local', cwd: '/private/fixture' }],
    selectedCapabilities: { localRoots: [], mcpServers: [], apps: [], browser: null },
  }, 'resume_suspended_goal')
  assert.equal(report.status, 'inconclusive')
  assert.equal(report.admission_effect, 'informational_only')
  assert.ok(report.unmeasured.includes('retained_native_thread_history'))
  assert.equal(report.history_erasure_claimed, false)
  assert.doesNotMatch(JSON.stringify(report), /PRIVATE_OBJECTIVE|PRIVATE_PROOF|private\/fixture|cursor_mac_key/)
})

test('Mission Host error logs remove raw bridge and owner content while retaining safe diagnostics', () => {
  const stderrTail = 'SECRET_TOKEN proof: hidden_zero_claim'
  const responseError = 'SECRET_RESPONSE rejected proof body'
  const error = new MissionBridgeError(
    'SECRET_EXCEPTION arbitrary bridge message',
    23,
    [
      {
        code: 'workspace_error',
        message: 'SECRET_OWNER_BODY unsupported owner kind',
        proof: 'hidden mathematical content',
      },
      {
        code: 'unsafe code containing SECRET_CODE_BODY',
        message: 'another secret owner body',
      },
    ],
    'SIGTERM',
    {
      exitCode: 23,
      terminationSignal: 'SIGTERM',
      stderrTail,
      stderrTruncated: true,
      responseError,
    },
  )

  const serialized = serializeErrorForLog(error)
  const output = JSON.stringify(serialized)

  assert.doesNotMatch(
    output,
    /SECRET_TOKEN|hidden_zero_claim|SECRET_RESPONSE|SECRET_EXCEPTION|SECRET_OWNER_BODY|hidden mathematical content|SECRET_CODE_BODY|another secret owner body/,
  )
  assert.equal(serialized.type, 'MissionBridgeError')
  assert.equal(serialized.owner_error_count, 2)
  assert.deepEqual(serialized.owner_error_codes, ['workspace_error'])
  assert.deepEqual(serialized.process_diagnostic, {
    exit_code: 23,
    termination_signal: 'SIGTERM',
    stderr_tail_present: true,
    stderr_tail_character_count: Array.from(stderrTail).length,
    stderr_tail_byte_count: Buffer.byteLength(stderrTail, 'utf8'),
    stderr_tail_sha256: createHash('sha256').update(stderrTail, 'utf8').digest('hex'),
    stderr_truncated: true,
    response_error_present: true,
  })
})

test('Mission Host error logs do not trust arbitrary exception names or aggregate messages', () => {
  const arbitrary = new Error('SECRET_GENERIC_MESSAGE proof body')
  arbitrary.name = 'SECRET_CONTROLLED_CLASS'
  const aggregate = new AggregateError(
    [arbitrary, 'SECRET_NON_ERROR_VALUE'],
    'SECRET_AGGREGATE_MESSAGE',
  )

  const serialized = serializeErrorForLog(aggregate)
  const output = JSON.stringify(serialized)

  assert.doesNotMatch(
    output,
    /SECRET_GENERIC_MESSAGE|SECRET_CONTROLLED_CLASS|SECRET_NON_ERROR_VALUE|SECRET_AGGREGATE_MESSAGE/,
  )
  assert.deepEqual(serialized, {
    type: 'AggregateError',
    cause_count: 2,
    causes: [{ type: 'Error' }, { type: 'UnknownError' }],
  })
  assert.equal(errorClassForLog(arbitrary), 'Error')
  assert.equal(errorClassForLog(aggregate), 'AggregateError')
  assert.equal(errorClassForLog('SECRET_NON_ERROR_VALUE'), 'UnknownError')
})

test('Mission Host error logs retain only an allowlisted concrete error class', () => {
  class JsonRpcResponseError extends Error {
    readonly jsonRpcCode = -32602

    constructor(readonly jsonRpcMethod: unknown) {
      super('SECRET_RPC_RESPONSE_BODY')
    }
  }
  class SecretControlledClass extends Error {}

  const responseError = new JsonRpcResponseError('thread/resume')
  assert.equal(errorClassForLog(responseError), 'JsonRpcResponseError')
  assert.deepEqual(jsonRpcErrorFieldsForLog(responseError), {
    json_rpc_code: -32602,
    json_rpc_code_present: true,
    json_rpc_method: 'thread/resume',
  })
  assert.deepEqual(jsonRpcErrorFieldsForLog(new Error()), {})
  assert.equal(errorClassForLog(new SecretControlledClass()), 'Error')
})

test('Mission Host RPC error logs omit arbitrary outgoing method strings', () => {
  class JsonRpcResponseError extends Error {
    readonly jsonRpcCode = -32601

    constructor(readonly jsonRpcMethod: unknown) {
      super('SECRET_RPC_RESPONSE_BODY')
    }
  }

  const fields = jsonRpcErrorFieldsForLog(
    new JsonRpcResponseError('SECRET_ARBITRARY_RPC_METHOD'),
  )

  assert.deepEqual(fields, {
    json_rpc_code: -32601,
    json_rpc_code_present: true,
  })
  assert.doesNotMatch(JSON.stringify(fields), /SECRET_ARBITRARY_RPC_METHOD|SECRET_RPC_RESPONSE_BODY/)
  assert.deepEqual(jsonRpcErrorFieldsForLog(new JsonRpcResponseError(null)), {
    json_rpc_code: -32601,
    json_rpc_code_present: true,
  })
})

test('failed pending force reconciliation retains the marker and does not create a normal Host', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-force-intent-'))
  const markerPath = path.join(root, 'force-stop.pending')
  await fs.promises.writeFile(markerPath, '', { flag: 'wx' })
  let hostCount = 0
  try {
    await assert.rejects(
      consumePendingForceStop(root, () => {
        hostCount += 1
        return {
          async reconcilePendingExplicitForceStop(): Promise<void> {
            throw new Error('owner reconciliation failed')
          },
        }
      }),
      /owner reconciliation failed/,
    )

    assert.equal(hostCount, 1)
    assert.equal(fs.existsSync(markerPath), true)
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('checkpoint-stop intent is consumed exactly once from a regular marker', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-checkpoint-stop-intent-'))
  const markerPath = path.join(root, 'checkpoint-stop.pending')
  try {
    assert.equal(await consumePendingCheckpointStop(root), false)
    await fs.promises.writeFile(markerPath, '', { flag: 'wx' })
    assert.equal(await consumePendingCheckpointStop(root), true)
    assert.equal(fs.existsSync(markerPath), false)
    assert.equal(await consumePendingCheckpointStop(root), false)
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('checkpoint-stop intent ignores a non-file marker without consuming it', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-checkpoint-stop-invalid-'))
  const markerPath = path.join(root, 'checkpoint-stop.pending')
  await fs.promises.mkdir(markerPath)
  try {
    assert.equal(await consumePendingCheckpointStop(root), false)
    assert.equal(fs.existsSync(markerPath), true)
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('checkpoint-stop intent ignores nonempty material without consuming it', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-checkpoint-stop-nonempty-'))
  const markerPath = path.join(root, 'checkpoint-stop.pending')
  await fs.promises.writeFile(markerPath, 'not-an-intent')
  try {
    assert.equal(await consumePendingCheckpointStop(root), false)
    assert.equal(await fs.promises.readFile(markerPath, 'utf8'), 'not-an-intent')
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('checkpoint-stop cancellation that wins the consume race remains a benign clear', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-checkpoint-stop-clear-race-'))
  const markerPath = path.join(root, 'checkpoint-stop.pending')
  const originalUnlink = fs.promises.unlink
  await fs.promises.writeFile(markerPath, '', { flag: 'wx' })
  try {
    fs.promises.unlink = async (target: fs.PathLike): Promise<void> => {
      await originalUnlink(target)
      const error = new Error('already cleared') as NodeJS.ErrnoException
      error.code = 'ENOENT'
      throw error
    }
    assert.equal(await consumePendingCheckpointStop(root), false)
    assert.equal(fs.existsSync(markerPath), false)
  } finally {
    fs.promises.unlink = originalUnlink
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('single-epoch canary intent is consumed exactly once from a regular marker', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-single-epoch-canary-intent-'))
  const markerPath = path.join(root, 'single-epoch-canary.pending')
  const hostStatePath = path.join(root, 'mission-host-state.json')
  const hostStateBytes = Buffer.from('{"activeGoal":null,"schemaVersion":"sentinel"}\n')
  try {
    assert.equal(await consumePendingSingleEpochCanary(root), false)
    await fs.promises.writeFile(hostStatePath, hostStateBytes, { flag: 'wx' })
    await fs.promises.writeFile(markerPath, '', { flag: 'wx' })
    assert.equal(await consumePendingSingleEpochCanary(root), true)
    assert.equal(fs.existsSync(markerPath), false)
    assert.deepEqual(await fs.promises.readFile(hostStatePath), hostStateBytes)
    assert.equal(await consumePendingSingleEpochCanary(root), false)
    assert.deepEqual(await fs.promises.readFile(hostStatePath), hostStateBytes)
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('single-epoch canary rejects invalid marker material without consuming it', async () => {
  const directoryRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-single-epoch-invalid-'))
  const directoryMarker = path.join(directoryRoot, 'single-epoch-canary.pending')
  await fs.promises.mkdir(directoryMarker)
  try {
    await assert.rejects(
      consumePendingSingleEpochCanary(directoryRoot),
      /single-epoch canary marker must be an empty regular file/,
    )
    assert.equal(fs.existsSync(directoryMarker), true)
  } finally {
    await fs.promises.rm(directoryRoot, { recursive: true, force: true })
  }

  const nonemptyRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-single-epoch-nonempty-'))
  const nonemptyMarker = path.join(nonemptyRoot, 'single-epoch-canary.pending')
  await fs.promises.writeFile(nonemptyMarker, 'not-an-intent')
  try {
    await assert.rejects(
      consumePendingSingleEpochCanary(nonemptyRoot),
      /single-epoch canary marker must be an empty regular file/,
    )
    assert.equal(await fs.promises.readFile(nonemptyMarker, 'utf8'), 'not-an-intent')
  } finally {
    await fs.promises.rm(nonemptyRoot, { recursive: true, force: true })
  }
})

test('single-epoch canary rejects a symlink marker without consuming its target', async (context) => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-single-epoch-symlink-'))
  const targetPath = path.join(root, 'target')
  const markerPath = path.join(root, 'single-epoch-canary.pending')
  await fs.promises.writeFile(targetPath, '')
  try {
    try {
      await fs.promises.symlink(targetPath, markerPath, 'file')
    } catch (error) {
      if (['EPERM', 'EACCES'].includes(String((error as NodeJS.ErrnoException).code))) {
        context.skip('file symlink creation is unavailable on this platform')
        return
      }
      throw error
    }
    await assert.rejects(
      consumePendingSingleEpochCanary(root),
      /single-epoch canary marker must be an empty regular file/,
    )
    assert.equal((await fs.promises.lstat(markerPath)).isSymbolicLink(), true)
    assert.equal(await fs.promises.readFile(targetPath, 'utf8'), '')
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('single-epoch canary cancellation that wins the consume race remains a benign clear', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-single-epoch-clear-race-'))
  const markerPath = path.join(root, 'single-epoch-canary.pending')
  const originalUnlink = fs.promises.unlink
  await fs.promises.writeFile(markerPath, '', { flag: 'wx' })
  try {
    fs.promises.unlink = async (target: fs.PathLike): Promise<void> => {
      await originalUnlink(target)
      const error = new Error('already cleared') as NodeJS.ErrnoException
      error.code = 'ENOENT'
      throw error
    }
    assert.equal(await consumePendingSingleEpochCanary(root), false)
    assert.equal(fs.existsSync(markerPath), false)
  } finally {
    fs.promises.unlink = originalUnlink
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})
