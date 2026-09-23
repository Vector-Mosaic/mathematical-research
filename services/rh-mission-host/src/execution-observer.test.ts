import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import type { CodexExecutionObservation } from '../../../packages/codex-thread-core/dist/index.js'
import { projectNativeObservation } from '../../../packages/codex-thread-core/dist/native-observation.js'
import type { RhObservation, RhObservationContentHandle } from '../../../packages/codex-remote-core/dist/rh-observation.js'
import { MissionExecutionObserver, type MissionExecutionObserverOptions } from './execution-observer.js'
import { readRhObservationContent, readRhObservationRows } from './observation-store.js'

async function fixture(t: test.TestContext, options: MissionExecutionObserverOptions = {}) {
  const runtimeDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-execution-observer-'))
  await fs.promises.chmod(runtimeDir, 0o700)
  const journal: string[] = []
  t.mock.method(process.stderr, 'write', (chunk: string | Uint8Array) => {
    journal.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    return true
  })
  t.mock.method(process.stdout, 'write', (chunk: string | Uint8Array) => {
    journal.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    return true
  })
  const observer = new MissionExecutionObserver({ runtimeDir, missionId: 'fixture-mission', releaseSha: 'a'.repeat(40) }, {
    incarnation: 'fixture-incarnation', ...options,
    limits: { diskReserveBytes: 0, flushMilliseconds: 60_000, ...options.limits },
  })
  t.after(async () => {
    await observer.close()
    await fs.promises.rm(runtimeDir, { recursive: true, force: true })
  })
  let nativeReceipt = 0
  const draft = (method: string, params: Record<string, unknown>): CodexExecutionObservation[] =>
    projectNativeObservation({ method, params }, {
      rootThreadId: 'root', parentThreadId: 'root', depth: 1,
      receiptId: `native-${++nativeReceipt}`, receivedAt: '2026-09-05T14:00:00.000Z',
    }).map((event) => ({ ...event, phase: 'received', caused_by: null }))
  const send = (method: string, params: Record<string, unknown>) =>
    draft(method, params).map((event) => observer.observe(event, 'epoch-one'))
  const output = (delta: string, itemId = 'command-one') => send('item/commandExecution/outputDelta', {
    threadId: 'worker', turnId: 'turn-one', itemId, delta,
  })
  const rows = async () => (await readRhObservationRows(runtimeDir, observer.incarnation, 0, 1000)).observations
  const text = async (handle: RhObservationContentHandle) => (await readRhObservationContent(runtimeDir, handle)).text
  const field = async (observations: RhObservation[], name: RhObservationContentHandle['field']) => {
    const pages: { handle: RhObservationContentHandle; text: string }[] = []
    for (const row of observations) for (const handle of row.content.filter((entry) => entry.field === name)) pages.push({ handle, text: await text(handle) })
    return pages
  }
  return { runtimeDir, observer, journal, draft, send, output, rows, text, field }
}

test('native output is committed while still active and sanitized UTF-8 offsets join across receipts', async (t) => {
  const context = await fixture(t)
  assert.deepEqual(context.output('stage € '), [{ incarnation: context.observer.incarnation, sequence: 1 }])
  await context.observer.flush()
  const first = await context.rows()
  assert.equal(first[0].kind, 'command')
  assert.deepEqual(first[0].details, { output_mode: 'streaming' })
  assert.equal(first[0].identity.mission_id, 'fixture-mission')
  assert.equal(first[0].identity.epoch_id, 'epoch-one')
  assert.equal(first[0].identity.thread_id, 'worker')
  assert.equal((await context.field(first, 'output'))[0].text, 'stage € ')
  assert.equal(first[0].content[0].complete, false)

  // A decoded transport may split a UTF-16 surrogate pair between notifications.
  context.output('\uD83D')
  await context.observer.flush()
  assert.equal((await context.rows())[1].content.length, 0)
  context.output('\uDE42 continues\n')
  await context.observer.flush()
  const pages = await context.field(await context.rows(), 'output')
  assert.equal(pages.map((page) => page.text).join(''), 'stage € 🙂 continues\n')
  assert.equal(pages[1].handle.start_byte, Buffer.byteLength('stage € '))
  assert.equal(pages[1].handle.end_byte, Buffer.byteLength('stage € 🙂 continues\n'))
  assert.equal(pages[1].handle.content_id, pages[0].handle.content_id)
  assert.ok(pages.every((page) => page.handle.complete === false))
})

test('checkpoint-source failure is one root and Executive-Epoch-bound processed error observation', async (t) => {
  const context = await fixture(t)
  const warning = {
    code: 'checkpoint_source_capture_failed_writer_reacquired',
    reason_code: 'checkpoint_source_copy_failed',
    database_bytes: 8192,
    handoff_elapsed_ms: 23,
  } as const

  assert.deepEqual(
    context.observer.observeCheckpointSourceFailure(warning, {
      rootThreadId: 'root.checkpoint',
      executiveEpochId: 'epoch.checkpoint',
    }),
    { incarnation: context.observer.incarnation, sequence: 1 },
  )
  await context.observer.flush()

  const rows = await context.rows()
  assert.equal(rows.length, 1)
  const row = rows[0]!
  assert.equal(row.kind, 'error')
  assert.equal(row.phase, 'processed')
  assert.equal(row.source_method, 'host/checkpoint-source-handoff')
  assert.equal(row.summary, 'Checkpoint completed; no usable new backup source was reported')
  assert.deepEqual(row.identity, {
    mission_id: 'fixture-mission',
    epoch_id: 'epoch.checkpoint',
    root_thread_id: 'root.checkpoint',
    thread_id: 'root.checkpoint',
    parent_thread_id: null,
    turn_id: null,
    item_id: null,
    operation_id: null,
  })
  assert.deepEqual(row.details, {
    code: warning.code,
    actual: warning.reason_code,
    expected: 'checkpoint_source_published',
    source_location: 'WorkspaceStore.publish_checkpoint_source_handoff',
    containment: false,
  })
  const errorContent = await context.field(rows, 'error')
  assert.equal(errorContent.length, 1)
  assert.deepEqual(JSON.parse(errorContent[0]!.text), warning)
  assert.equal(errorContent[0]!.handle.complete, true)
})

test('skipped checkpoint snapshot records preserved-writer truth without claiming reacquisition', async (t) => {
  const context = await fixture(t)
  const warning = {
    code: 'checkpoint_source_snapshot_skipped_writer_preserved',
    reason_code: 'checkpoint_source_busy',
    database_bytes: null,
    handoff_elapsed_ms: 3,
  } as const

  context.observer.observeCheckpointSourceFailure(warning, {
    rootThreadId: 'root.checkpoint.busy',
    executiveEpochId: 'epoch.checkpoint.busy',
  })
  await context.observer.flush()

  const rows = await context.rows()
  assert.equal(rows.length, 1)
  const row = rows[0]!
  assert.equal(row.kind, 'error')
  assert.equal(
    row.summary,
    'Checkpoint completed; this snapshot was skipped and the existing writer was preserved',
  )
  assert.equal(row.details.code, warning.code)
  assert.equal(row.details.actual, warning.reason_code)
  const errorContent = await context.field(rows, 'error')
  assert.deepEqual(JSON.parse(errorContent[0]!.text), warning)
  assert.equal(errorContent[0]!.handle.complete, true)
})

test('source-operation teardown warning reports writer revalidation without claiming lock release', async (t) => {
  const context = await fixture(t)
  const warning = {
    code: 'checkpoint_source_operation_teardown_failed_writer_revalidated',
    reason_code: 'checkpoint_source_published',
    database_bytes: 4096,
    handoff_elapsed_ms: 9,
  } as const

  context.observer.observeCheckpointSourceFailure(warning, {
    rootThreadId: 'root.checkpoint.teardown',
    executiveEpochId: 'epoch.checkpoint.teardown',
  })
  await context.observer.flush()

  const rows = await context.rows()
  assert.equal(rows.length, 1)
  const row = rows[0]!
  assert.equal(row.kind, 'error')
  assert.equal(
    row.summary,
    'Checkpoint completed; source-operation teardown failed after the active writer was revalidated',
  )
  assert.equal(row.details.code, warning.code)
  assert.equal(row.details.actual, warning.reason_code)
  const errorContent = await context.field(rows, 'error')
  assert.deepEqual(JSON.parse(errorContent[0]!.text), warning)
  assert.equal(errorContent[0]!.handle.complete, true)
})

test('split credential spans never reach artifacts and source completion releases an ordinary held tail', async (t) => {
  const context = await fixture(t)
  const credential = 'fixture_' + 'ab79'.repeat(8)
  const chunks = ['ready\nAuthoriza', 'tion: Bea', `rer ${credential.slice(0, 13)}`, `${credential.slice(13)}\nstage sk-var`]
  for (const chunk of chunks) {
    context.output(chunk)
    await context.observer.flush()
    const pages = await context.field(await context.rows(), 'output')
    assert.ok(pages.every((page) => !page.text.includes(credential.slice(0, 13)) && !page.text.includes(credential.slice(13))))
  }
  const beforeCompletion = await context.field(await context.rows(), 'output')
  assert.equal(beforeCompletion.map((page) => page.text).join(''), 'ready\nAuthorization: [REDACTED:authorization]\nstage ')
  assert.ok(beforeCompletion.every((page) => !page.handle.complete))

  // A completion whose output snapshot is absent still closes the existing
  // source stream. A viewer disconnect cannot supply this native marker.
  context.send('item/completed', { threadId: 'worker', turnId: 'turn-one', item: {
    id: 'command-one', type: 'commandExecution', command: 'fixture command', cwd: '/fixture',
    status: 'completed', aggregatedOutput: null, exitCode: 0, durationMs: 10,
  } })
  await context.observer.flush()
  const rows = await context.rows()
  const completed = await context.field(rows, 'output')
  assert.equal(completed.map((page) => page.text).join(''), 'ready\nAuthorization: [REDACTED:authorization]\nstage sk-var')
  assert.equal(completed.at(-1)!.handle.complete, true)
  assert.equal(completed.at(-1)!.handle.start_byte, completed.at(-2)!.handle.end_byte)
  assert.ok(rows.some((row) => row.kind === 'coverage' && row.details.reason === 'credential_signatures_redacted' && row.caused_by !== null))
  const directory = path.join(context.runtimeDir, 'observations', context.observer.incarnation)
  for (const name of await fs.promises.readdir(directory)) {
    const bytes = await fs.promises.readFile(path.join(directory, name), 'utf8')
    assert.ok(!bytes.includes(credential))
    assert.ok(!bytes.includes(credential.slice(0, 13)))
    assert.ok(!bytes.includes(credential.slice(13)))
  }
  assert.ok(!context.journal.join('').includes(credential))
})

test('final native snapshots retain item identity and empty completion remains visible', async (t) => {
  const context = await fixture(t)
  context.output('first €\n')
  await context.observer.flush()
  const first = (await context.field(await context.rows(), 'output'))[0]
  context.send('item/completed', {
    threadId: 'worker', turnId: 'turn-one', completedAtMs: 1_788_617_000_000,
    item: { id: 'command-one', type: 'commandExecution', command: 'printf fixture', cwd: '/fixture',
      status: 'completed', aggregatedOutput: 'first €\nfinal 🙂\n', exitCode: 0, durationMs: 25, processId: 'process-one' },
  })
  await context.observer.flush()
  const rows = await context.rows()
  const final = (await context.field(rows.filter((row) => row.source_method === 'item/completed'), 'output'))[0]
  assert.equal(final.handle.content_id, first.handle.content_id)
  assert.ok(final.handle.revision > first.handle.revision)
  assert.equal(final.handle.update_mode, 'snapshot')
  assert.equal(final.handle.start_byte, 0)
  assert.equal(final.handle.complete, true)
  assert.equal(final.text, 'first €\nfinal 🙂\n')
  assert.equal(await context.text(first.handle), 'first €\n')
  const command = rows.find((row) => row.kind === 'command' && row.source_method === 'item/completed')
  assert.ok(command?.kind === 'command')
  assert.equal(command.details.exit_code, 0)
  assert.equal(command.details.output_mode, 'unknown')

  context.send('item/completed', { threadId: 'worker', turnId: 'turn-one',
    item: { id: 'empty-message', type: 'agentMessage', text: '', phase: 'final_answer' } })
  await context.observer.flush()
  const empty = (await context.rows()).find((row) => row.identity.item_id === 'empty-message')!
  assert.equal(empty.content.length, 1)
  assert.equal(empty.content[0].complete, true)
  assert.equal(empty.content[0].start_byte, 0)
  assert.equal(empty.content[0].end_byte, 0)
  assert.equal(await context.text(empty.content[0]), '')
})

test('native details and structured tool bodies use shared scrubbing without losing ordinary results', async (t) => {
  const context = await fixture(t)
  const credential = 'fixture_' + '91de'.repeat(8)
  context.send('item/completed', { threadId: 'worker', turnId: 'turn-one', item: {
    id: 'file-one', type: 'fileChange', status: 'completed',
    changes: [{ path: `/fixture/report?access_token=${credential}&revision=2`, kind: { type: 'update', move_path: null }, diff: '+ measured 17 zeros\n' }],
  } })
  context.send('item/completed', { threadId: 'worker', turnId: 'turn-one', item: {
    id: 'tool-one', type: 'mcpToolCall', server: 'fixture-server', tool: 'inspect', status: 'completed',
    arguments: { nested: { access_token: credential }, token_count: 37, target: 'project-output' },
    result: { content: [{ type: 'text', text: 'tool returned the requested table' }], structuredContent: { password: credential, rows: 4 } },
    error: null, durationMs: 10,
  } })
  await context.observer.flush()
  const rows = await context.rows()
  const file = rows.find((row) => row.kind === 'file')
  assert.ok(file?.kind === 'file')
  assert.equal(file.details.path, '/fixture/report?access_token=[REDACTED:credential_assignment]&revision=2')
  assert.equal((await context.field(rows, 'diff'))[0].text, '+ measured 17 zeros\n')
  const args = JSON.parse((await context.field(rows, 'arguments'))[0].text)
  assert.deepEqual(args, { nested: { access_token: '[REDACTED:credential_field]' }, token_count: 37, target: 'project-output' })
  const results = await context.field(rows, 'result')
  assert.ok(results.some((page) => page.text === 'tool returned the requested table'))
  assert.ok(results.some((page) => page.text === '{"password":"[REDACTED:credential_field]","rows":4}'))
  assert.ok(!JSON.stringify(rows).includes(credential))
  assert.ok(results.every((page) => !page.text.includes(credential)))
})

test('health callback failures do not reject native observations or their eventual publication', async (t) => {
  let failures = 0
  const context = await fixture(t, { onHealth: () => {
    failures += 1
    if (failures === 1) throw new Error('fixture callback failure')
    return Promise.reject(new Error('fixture asynchronous callback failure'))
  } })
  assert.doesNotThrow(() => context.output('still executing\n'))
  await assert.doesNotReject(context.observer.flush())
  assert.ok(failures > 0)
  context.output('another source receipt\n')
  await assert.doesNotReject(context.observer.flush())
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.ok(failures > 1)
  assert.equal((await context.field(await context.rows(), 'output'))[0].text, 'still executing\n')
  assert.ok(!context.journal.join('').includes('fixture callback failure'))
})

test('independent full snapshots expose their held tail without claiming source completion', async (t) => {
  const context = await fixture(t)
  context.send('turn/diff/updated', { threadId: 'worker', turnId: 'turn-one', diff: '+ prefix sk-var' })
  context.send('turn/plan/updated', { threadId: 'worker', turnId: 'turn-one',
    plan: [{ step: 'inspect output', status: 'inProgress' }], explanation: 'explanation sk-var' })
  context.send('item/fileChange/patchUpdated', { threadId: 'worker', turnId: 'turn-one', itemId: 'patch-one',
    changes: [{ path: '/fixture/change.ts', kind: { type: 'update', move_path: null }, diff: '+ another sk-var' }] })
  await context.observer.flush()
  const rows = await context.rows()
  const diffs = await context.field(rows, 'diff')
  assert.deepEqual(diffs.map((page) => page.text), ['+ prefix sk-var', '+ another sk-var'])
  const messages = await context.field(rows, 'message')
  assert.ok(messages.some((page) => page.text === 'explanation sk-var'))
  assert.ok([...diffs, ...messages].every((page) => page.handle.complete === false))
})

test('item-start snapshot and later native deltas share credential scanner state', async (t) => {
  const context = await fixture(t)
  const credential = 'fixture_' + '6c29'.repeat(8)
  context.send('item/started', { threadId: 'worker', turnId: 'turn-one', item: {
    id: 'command-one', type: 'commandExecution', command: 'fixture command', cwd: '/fixture',
    status: 'inProgress', aggregatedOutput: 'Authorization: Bea', exitCode: null, durationMs: null,
  } })
  await context.observer.flush()
  const initial = (await context.field(await context.rows(), 'output'))[0]
  context.output(`rer ${credential}\nworker finished sk-var`)
  await context.observer.flush()
  const activePages = await context.field(await context.rows(), 'output')
  assert.equal(activePages.map((page) => page.text).join(''), 'Authorization: [REDACTED:authorization]\nworker finished ')
  assert.ok(activePages.every((page) => !page.text.includes(credential) && page.handle.content_id === initial.handle.content_id))
  context.send('item/completed', { threadId: 'worker', turnId: 'turn-one', item: {
    id: 'command-one', type: 'commandExecution', command: 'fixture command', cwd: '/fixture',
    status: 'completed', aggregatedOutput: null, exitCode: 0, durationMs: 20,
  } })
  await context.observer.flush()
  const completePages = await context.field(await context.rows(), 'output')
  assert.equal(completePages.map((page) => page.text).join(''), 'Authorization: [REDACTED:authorization]\nworker finished sk-var')
  assert.equal(completePages.at(-1)!.handle.complete, true)
  assert.equal(completePages.at(-1)!.handle.content_id, initial.handle.content_id)
})

test('queue overflow preserves source receipt gaps and sanitized offsets for subsequent live output', async (t) => {
  const context = await fixture(t, { limits: { queueBytes: 10_000 } })
  const firstText = 'first €\n'
  const lostText = 'unretained '.repeat(2048)
  const lastText = 'third 🙂\n'
  const first = context.output(firstText)[0]
  await context.observer.flush()
  const lost = context.output(lostText)[0]
  const last = context.output(lastText)[0]
  assert.deepEqual([first.sequence, lost.sequence, last.sequence], [1, 2, 3])
  await context.observer.flush()
  const observed = await readRhObservationRows(context.runtimeDir, context.observer.incarnation)
  assert.deepEqual(observed.observations.map((row) => row.sequence), [1, 3])
  assert.ok(observed.manifest.coverage.some((gap) => gap.kind === 'known_loss' && gap.reason === 'queue_full' && gap.from_sequence === 2 && gap.to_sequence === 2))
  const pages = await context.field(observed.observations, 'output')
  assert.equal(pages[1].text, lastText)
  assert.equal(pages[1].handle.start_byte, Buffer.byteLength(firstText + lostText))
  assert.ok(pages[1].handle.start_byte > pages[0].handle.end_byte)
})

test('existing handshake facts enrich source provenance without entering wire rows or overwriting a conflict', async (t) => {
  const context = await fixture(t)
  const handshake: CodexExecutionObservation = {
    identity: { root_thread_id: null, thread_id: null, parent_thread_id: null, turn_id: null, item_id: null, operation_id: null },
    received_at: '2026-09-05T14:00:00.000Z', source_time: null, source_method: 'initialize', phase: 'processed', caused_by: null,
    kind: 'lifecycle', summary: 'Native source identity observed', details: { stage: 'native_handshake' }, contents: [],
    source_facts: { codex_version: '0.149.1' },
  }
  context.observer.observe(handshake, null)
  await context.observer.flush()
  const first = await readRhObservationRows(context.runtimeDir, context.observer.incarnation)
  assert.equal(first.manifest.source.codex_version, '0.149.1')
  assert.equal(Object.hasOwn(first.observations[0], 'source_facts'), false)
  context.observer.observe({ ...handshake, source_facts: { codex_version: '0.150.0' } }, null)
  await context.observer.flush()
  const next = await readRhObservationRows(context.runtimeDir, context.observer.incarnation)
  assert.equal(next.manifest.source.codex_version, '0.149.1')
  assert.ok(next.observations.some((row) => row.kind === 'coverage' && row.details.reason === 'native_source_version_changed'))
})

test('field-state capacity preserves an unfinished credential scanner and suppresses unknown continuation bytes', async (t) => {
  const context = await fixture(t)
  const retainedCredential = 'fixture_' + 'ad63'.repeat(8)
  const unknownCredential = 'fixture_' + '27fb'.repeat(8)
  context.output('Authorization: Bea', 'tracked-credential')
  // Each incomplete provider prefix requires its own scanner even while it has
  // no publishable bytes. The real default is 4096 tracked fields.
  for (let index = 0; index < 4096; index += 1) {
    context.output('sk-', `other-unfinished-${index}`)
  }
  const retained = context.output(`rer ${retainedCredential}\ntracked safe continuation\n`, 'tracked-credential')[0]
  const unknownStart = context.output('Authorization: Bea', 'untracked-credential')[0]
  context.output(`rer ${unknownCredential}\n`, 'untracked-credential')
  await context.observer.flush()

  const tail = (await readRhObservationRows(context.runtimeDir, context.observer.incarnation, retained.sequence - 1, 100)).observations
  const retainedRows = tail.filter((row) => row.identity.item_id === 'tracked-credential')
  const retainedPages = await context.field(retainedRows, 'output')
  assert.ok(retainedPages.some((page) => page.text.includes('tracked safe continuation\n')))
  assert.ok(retainedPages.every((page) => !page.text.includes(retainedCredential)))
  const unknownRows = tail.filter((row) => row.sequence >= unknownStart.sequence && row.identity.item_id === 'untracked-credential')
  assert.ok(unknownRows.length > 0)
  assert.ok(unknownRows.every((row) => row.content.length === 0))
  assert.ok(tail.some((row) => row.kind === 'coverage' && row.caused_by !== null))
  assert.ok(!context.journal.join('').includes(retainedCredential))
  assert.ok(!context.journal.join('').includes(unknownCredential))
})
