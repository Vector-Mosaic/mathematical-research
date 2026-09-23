import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  RhObservationStore,
  readRhObservationContent,
  readRhObservationManifest,
  readRhObservationRows,
  rhObservationContentFile,
  type RhObservationStoreContent,
  type RhObservationStoreInput,
  type RhObservationStoreLimits,
} from './observation-store.js'

const contentId = createHash('sha256').update('native-thread/native-item/output').digest('hex')

function observation(incarnation: string, sequence: number): RhObservationStoreInput {
  return {
    incarnation, sequence, received_at: new Date().toISOString(), source_time: null,
    kind: 'command', phase: 'received', source_method: 'item/commandExecution/outputDelta',
    identity: { mission_id: 'mission-test', epoch_id: 'epoch-test', root_thread_id: 'root',
      thread_id: 'worker', turn_id: 'turn', item_id: 'item', operation_id: 'item', parent_thread_id: 'root' },
    summary: 'Command output received', details: { status: 'running', output_mode: 'streaming' },
    caused_by: null,
  }
}

function content(text: string, start = 0, complete = false): RhObservationStoreContent {
  return { content_id: contentId, field: 'output', text, update_mode: 'append', start_byte: start, complete }
}

async function runtime(t: test.TestContext): Promise<string> {
  const directory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-observation-store-'))
  await fs.promises.chmod(directory, 0o700)
  t.after(async () => { await fs.promises.rm(directory, { recursive: true, force: true }) })
  return directory
}

function store(runtimeDir: string, incarnation = 'incarnation-one', limits: Partial<RhObservationStoreLimits> = {}): RhObservationStore {
  return new RhObservationStore({ runtimeDir, incarnation,
    selection: { mission_id: 'mission-test', epoch_id: null, root_thread_id: null, incarnation },
    source: { host_id: 'fixture', release_sha: null, selected_release_sha: null, bound_release_sha: null,
      codex_version: 'fixture', boot_id: 'fixture-boot', invocation_id: incarnation, origin: 'live' },
    limits: { diskReserveBytes: 0, flushMilliseconds: 60_000, ...limits },
  })
}

test('committed growing UTF-8 content is readable during execution and revisions retain exact ranges', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'live', { pageBytes: 12 })
  t.after(() => writer.close())
  assert.equal(writer.admit(observation('live', 1), [content('first €🙂 output')]), true)
  await writer.flush()
  const first = await readRhObservationRows(directory, 'live')
  assert.equal(first.manifest.clean_shutdown, false)
  assert.equal(first.manifest.watermark.sequence, 1)
  assert.equal(first.observations.length, 1)
  assert.ok(first.observations[0].content.length > 1)
  let reconstructed = ''
  for (const handle of first.observations[0].content) {
    assert.equal(handle.complete, false)
    let offset = handle.start_byte
    do {
      const page = await readRhObservationContent(directory, handle, offset, 4)
      reconstructed += page.text
      assert.ok(page.next_offset > offset || page.next_offset === handle.end_byte)
      offset = page.next_offset
    } while (offset < handle.end_byte)
  }
  assert.equal(reconstructed, 'first €🙂 output')
  const size = Buffer.byteLength(reconstructed)
  assert.equal(writer.admit(observation('live', 2), [content(' / still running', size)]), true)
  await writer.flush()
  const second = await readRhObservationRows(directory, 'live', 1)
  assert.equal(second.observations[0].content[0].start_byte, size)
  assert.equal(second.observations[0].content[0].revision, 2)
  assert.deepEqual((await readRhObservationRows(directory, 'live', 0, 1)).observations, first.observations)
  assert.deepEqual(JSON.parse(await fs.promises.readFile(path.join(directory, 'observations/current.json'), 'utf8')),
    { schema_version: 'rh_observation.v1', incarnation: 'live' })
  await writer.close()
  assert.equal((await readRhObservationManifest(directory, 'live')).clean_shutdown, true)
})

test('large source output is fully available through finite immutable content chunks', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'large')
  t.after(() => writer.close())
  const text = 'output €🙂\n'.repeat(350_000)
  assert.ok(Buffer.byteLength(text) > 4 * 1024 * 1024)
  assert.equal(writer.admit(observation('large', 1), [content(text)]), true)
  await writer.flush()
  const { observations } = await readRhObservationRows(directory, 'large')
  let restored = ''
  for (const handle of observations[0].content) {
    assert.ok(handle.end_byte - handle.start_byte <= 512 * 1024)
    restored += (await readRhObservationContent(directory, handle)).text
  }
  assert.equal(restored, text)
  assert.equal(writer.health().queued_bytes, 0)
})

test('slow sink retains in-flight reservation, drops bounded admissions and allows later work', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'slow', { queueBytes: 10_000 })
  t.after(() => writer.close())
  await writer.flush()
  const original = fs.promises.rename.bind(fs.promises)
  let release!: () => void
  let entered!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  const blocked = new Promise<void>((resolve) => { entered = resolve })
  let held = false
  const mock = t.mock.method(fs.promises, 'rename', async (source: fs.PathLike, destination: fs.PathLike) => {
    if (!held && String(destination).includes('segment.')) { held = true; entered(); await gate }
    return original(source, destination)
  })
  assert.equal(writer.admit(observation('slow', 1), [content('x'.repeat(1000))]), true)
  const flushing = writer.flush()
  await blocked
  assert.ok(writer.health().queued_bytes > 0)
  assert.equal(writer.admit(observation('slow', 2), [content('x'.repeat(3000))]), false)
  assert.ok(writer.health().queued_bytes <= 10_000)
  release()
  await flushing
  mock.mock.restore()
  assert.equal(writer.admit(observation('slow', 3), [content('ok', 1000)]), true)
  await writer.flush()
  const { manifest, observations } = await readRhObservationRows(directory, 'slow')
  assert.deepEqual(observations.map((entry) => entry.sequence), [1, 3])
  assert.ok(manifest.coverage.some((gap) => gap.reason === 'queue_full' && gap.from_sequence === 2 && gap.to_sequence === 2))
})

test('failed publication preserves old cut and reports exact loss, without admitting orphan content', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'failure')
  t.after(() => writer.close())
  assert.equal(writer.admit(observation('failure', 1), [content('retained')]), true)
  await writer.flush()
  const original = fs.promises.rename.bind(fs.promises)
  const mock = t.mock.method(fs.promises, 'rename', async (source: fs.PathLike, destination: fs.PathLike) => {
    if (String(destination).endsWith('manifest.json')) throw Object.assign(new Error('fixture'), { code: 'ENOSPC' })
    return original(source, destination)
  })
  writer.admit(observation('failure', 2), [content('not committed', 8)])
  await writer.flush()
  assert.equal(writer.health().state, 'degraded')
  assert.deepEqual((await readRhObservationRows(directory, 'failure')).observations.map((entry) => entry.sequence), [1])
  mock.mock.restore()
  await writer.flush()
  const manifest = await readRhObservationManifest(directory, 'failure')
  assert.equal(manifest.watermark.sequence, 2)
  assert.ok(manifest.coverage.some((gap) => gap.from_sequence === 2 && gap.to_sequence === 2 && gap.kind === 'known_loss'))
  assert.equal((await fs.promises.readdir(path.join(directory, 'observations/failure'))).some((file) => file.includes(`.${2}.`)), false)
})

test('failure after manifest rename never removes now-committed bytes or invents data loss', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'renamed')
  t.after(() => writer.close())
  await writer.flush()
  const original = fs.promises.rename.bind(fs.promises)
  const mock = t.mock.method(fs.promises, 'rename', async (source: fs.PathLike, destination: fs.PathLike) => {
    await original(source, destination)
    if (String(destination).endsWith('manifest.json')) throw new Error('fixture after rename')
  })
  writer.admit(observation('renamed', 1), [content('committed')])
  await writer.flush()
  mock.mock.restore()
  const { manifest, observations } = await readRhObservationRows(directory, 'renamed')
  assert.equal(observations.length, 1)
  assert.equal((await readRhObservationContent(directory, observations[0].content[0])).text, 'committed')
  assert.equal(manifest.coverage.some((gap) => gap.kind === 'known_loss'), false)
  assert.equal(writer.health().dropped_observations, 0)
})

test('indeterminate manifest readback preserves possibly committed files until recovery', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'uncertain')
  t.after(() => writer.close())
  await writer.flush()
  const rename = fs.promises.rename.bind(fs.promises)
  const lstat = fs.promises.lstat.bind(fs.promises)
  let unavailable = false
  const renameMock = t.mock.method(fs.promises, 'rename', async (source: fs.PathLike, destination: fs.PathLike) => {
    await rename(source, destination)
    if (String(destination).endsWith('manifest.json')) { unavailable = true; throw new Error('fixture after rename') }
  })
  const statMock = t.mock.method(fs.promises, 'lstat', async (...args: Parameters<typeof fs.promises.lstat>) => {
    if (unavailable && String(args[0]).endsWith('manifest.json')) throw Object.assign(new Error('fixture unavailable'), { code: 'EIO' })
    return lstat(...args)
  })
  writer.admit(observation('uncertain', 1), [content('possibly committed')])
  await writer.flush()
  renameMock.mock.restore()
  statMock.mock.restore()
  const before = await readRhObservationRows(directory, 'uncertain')
  assert.equal((await readRhObservationContent(directory, before.observations[0].content[0])).text, 'possibly committed')
  await writer.flush()
  assert.equal(writer.health().dropped_observations, 0)
  assert.equal((await readRhObservationRows(directory, 'uncertain')).manifest.watermark.sequence, 1)
})

test('size-tier compaction preserves every receipt and exact content handle', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'compacted')
  t.after(() => writer.close())
  let firstHandle
  for (let sequence = 1; sequence <= 65; sequence += 1) {
    writer.admit(observation('compacted', sequence), [content('a', sequence - 1)])
    await writer.flush()
    if (sequence === 1) firstHandle = (await readRhObservationRows(directory, 'compacted')).observations[0].content[0]
  }
  const { manifest, observations } = await readRhObservationRows(directory, 'compacted')
  assert.equal(observations.length, 65)
  assert.deepEqual(observations.map((entry) => entry.sequence), Array.from({ length: 65 }, (_, index) => index + 1))
  assert.equal(manifest.segments.length, 3)
  assert.ok(manifest.segments.some((entry) => entry.level === 1))
  assert.equal((await readRhObservationContent(directory, firstHandle!)).text, 'a')
  assert.equal((await fs.promises.readdir(path.join(directory, 'observations/compacted'))).filter((name) => name.startsWith('segment.')).length, 3)
})

test('readers continue at the changed committed cut when compaction removes their old segment', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'reader-race')
  t.after(() => writer.close())
  for (let sequence = 1; sequence <= 31; sequence += 1) {
    writer.admit(observation('reader-race', sequence), [content('x', sequence - 1)])
    await writer.flush()
  }
  const first = (await readRhObservationRows(directory, 'reader-race')).observations[0].content[0]
  const lstat = fs.promises.lstat.bind(fs.promises)
  let release!: () => void
  let entered!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  const blocked = new Promise<void>((resolve) => { entered = resolve })
  let pendingReaders = 0
  let hold = true
  const mock = t.mock.method(fs.promises, 'lstat', async (...args: Parameters<typeof fs.promises.lstat>) => {
    if (hold && String(args[0]).endsWith('segment.1.1.jsonl')) {
      pendingReaders += 1
      if (pendingReaders === 2) entered()
      await gate
    }
    return lstat(...args)
  })
  const rowRead = readRhObservationRows(directory, 'reader-race')
  const contentRead = readRhObservationContent(directory, first)
  await blocked
  hold = false
  writer.admit(observation('reader-race', 32), [content('x', 31)])
  await writer.flush()
  release()
  assert.equal((await rowRead).observations.length, 32)
  assert.equal((await contentRead).text, 'x')
  mock.mock.restore()
})

test('retention spans restarts and expires cursors without touching model results or unrelated files', async (t) => {
  const directory = await runtime(t)
  const modelResults = path.join(directory, '.rh-mission-owner-results')
  await fs.promises.mkdir(modelResults)
  await fs.promises.writeFile(path.join(modelResults, 'user-owned'), 'unchanged')
  const first = store(directory, 'first', { retainedBytes: 49_152 })
  first.admit(observation('first', 1), [content('one')])
  await first.close()
  const second = store(directory, 'second', { retainedBytes: 49_152 })
  second.admit(observation('second', 1), [content('two')])
  await second.close()
  assert.equal((await readRhObservationRows(directory, 'first')).observations.length, 1)
  const third = store(directory, 'third', { retainedBytes: 49_152 })
  third.admit(observation('third', 1), [content('three')])
  await third.close()
  const expired = await readRhObservationManifest(directory, 'first')
  assert.equal(expired.segments.length, 0)
  assert.ok(expired.coverage.some((gap) => gap.kind === 'cursor_expired'))
  assert.equal((await readRhObservationRows(directory, 'second')).observations.length, 1)
  assert.equal(await fs.promises.readFile(path.join(modelResults, 'user-owned'), 'utf8'), 'unchanged')
})

test('disk reserve failure is isolated and recovery can commit subsequent source observations', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'disk')
  t.after(() => writer.close())
  await writer.flush()
  const statfs = fs.promises.statfs.bind(fs.promises)
  const mock = t.mock.method(fs.promises, 'statfs', async (...args: Parameters<typeof fs.promises.statfs>) => ({ ...(await statfs(...args)), bavail: 0 }))
  writer.admit(observation('disk', 1), [content('not stored')])
  await writer.flush()
  assert.equal(writer.health().error_code, 'disk_reserve')
  mock.mock.restore()
  writer.admit(observation('disk', 2), [content('new source', 10)])
  await writer.flush()
  assert.deepEqual((await readRhObservationRows(directory, 'disk')).observations.map((entry) => entry.sequence), [2])
})

test('committed corruption, invalid UTF-8 offsets, forged handles and symlinks fail closed', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'safe')
  writer.admit(observation('safe', 1), [content('€safe')])
  await writer.close()
  const { observations } = await readRhObservationRows(directory, 'safe')
  const handle = observations[0].content[0]
  const reordered = Object.fromEntries(Object.entries(handle).reverse()) as typeof handle
  assert.equal((await readRhObservationContent(directory, reordered)).text, '€safe')
  await assert.rejects(readRhObservationContent(directory, handle, 1))
  await assert.rejects(readRhObservationContent(directory, { ...handle, content_id: '../outside' }))
  const contentPath = path.join(directory, 'observations/safe', rhObservationContentFile(handle))
  await fs.promises.writeFile(contentPath, 'tampered')
  await assert.rejects(readRhObservationContent(directory, handle))
  const outside = path.join(directory, 'outside')
  await fs.promises.writeFile(outside, 'outside bytes')
  await fs.promises.unlink(contentPath)
  try { await fs.promises.symlink(outside, contentPath) } catch (error) {
    if (process.platform !== 'win32' || !['EPERM', 'EACCES'].includes(String((error as NodeJS.ErrnoException).code))) throw error
    t.diagnostic('Windows principal cannot create symlink; Linux no-follow fixture remains required')
    return
  }
  await assert.rejects(readRhObservationContent(directory, handle))
  assert.equal(await fs.promises.readFile(outside, 'utf8'), 'outside bytes')
})

test('chunk-handle expansion is bounded before queue admission', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'small-pages', { pageBytes: 4 })
  t.after(() => writer.close())
  assert.equal(writer.admit(observation('small-pages', 1), [content('x'.repeat(1024 * 1024))]), false)
  assert.equal(writer.health().queued_bytes, 0)
  assert.equal(writer.admit(observation('small-pages', 2), [content('ok')]), true)
  await writer.flush()
  assert.deepEqual((await readRhObservationRows(directory, 'small-pages')).observations.map((row) => row.sequence), [2])
})

test('private observation directory cannot redirect writes to an outside directory', async (t) => {
  const directory = await runtime(t)
  const outside = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-observation-outside-'))
  t.after(() => fs.promises.rm(outside, { recursive: true, force: true }))
  await fs.promises.writeFile(path.join(outside, 'sentinel'), 'unchanged')
  try { await fs.promises.symlink(outside, path.join(directory, 'observations'), process.platform === 'win32' ? 'junction' : 'dir') } catch (error) {
    if (process.platform !== 'win32' || !['EPERM', 'EACCES'].includes(String((error as NodeJS.ErrnoException).code))) throw error
    t.diagnostic('Windows principal cannot create directory link; Linux no-follow fixture remains required')
    return
  }
  const writer = store(directory, 'outside-blocked')
  writer.admit(observation('outside-blocked', 1), [content('must not escape')])
  await writer.close()
  assert.equal(writer.health().error_code, 'unsafe_path')
  assert.deepEqual(await fs.promises.readdir(outside), ['sentinel'])
})

test('restart discards uncommitted residue truthfully and bounds expired incarnation manifests', async (t) => {
  const directory = await runtime(t)
  const first = store(directory, 'old', { maxIncarnations: 2 })
  first.admit(observation('old', 1), [content('committed before crash')])
  await first.flush()
  const oldDirectory = path.join(directory, 'observations/old')
  const orphan = `content.${contentId}.2.22-28.txt`
  await fs.promises.writeFile(path.join(oldDirectory, orphan), 'orphan', { mode: 0o600 })
  const second = store(directory, 'middle', { maxIncarnations: 2 })
  await second.close()
  const recovered = await readRhObservationManifest(directory, 'old')
  assert.equal(recovered.watermark.sequence, 1)
  assert.ok(recovered.coverage.some((gap) => gap.kind === 'uncertain_tail' && gap.from_sequence === 2))
  await assert.rejects(fs.promises.lstat(path.join(oldDirectory, orphan)), { code: 'ENOENT' })
  assert.equal((await readRhObservationRows(directory, 'old')).observations.length, 1)
  const third = store(directory, 'new', { maxIncarnations: 2 })
  await third.close()
  const names = await fs.promises.readdir(path.join(directory, 'observations'))
  assert.equal(names.filter((name) => name !== 'current.json').length, 2)
  // No old-process finalizer runs: the fixture intentionally models a crash.
})

test('worker snapshot requires successful registration and preserves status through retention', async (t) => {
  const directory = await runtime(t)
  const writer = store(directory, 'workers', { maxSegments: 1 })
  t.after(() => writer.close())
  const registration = { ...observation('workers', 1), kind: 'work' as const, phase: 'rejected' as const,
    details: { action: 'registered', child_thread_id: 'worker', status: 'active' } }
  writer.admit(registration)
  await writer.flush()
  assert.equal((await readRhObservationManifest(directory, 'workers')).workers.length, 0)
  writer.admit({ ...registration, sequence: 2, phase: 'processed' })
  await writer.flush()
  writer.admit(observation('workers', 3))
  await writer.flush()
  const manifest = await readRhObservationManifest(directory, 'workers')
  assert.equal(manifest.workers[0].thread_id, 'worker')
  assert.equal(manifest.segments[0].first_sequence, 3)
  assert.ok((await readRhObservationRows(directory, 'workers', 1)).expired)
})

test('native handshake version enriches the committed source once without overwriting observed facts', async (t) => {
  const directory = await runtime(t)
  const writer = new RhObservationStore({ runtimeDir: directory, incarnation: 'handshake',
    selection: { mission_id: 'mission-test', epoch_id: null, root_thread_id: null, incarnation: 'handshake' },
    source: { host_id: 'fixture', release_sha: null, selected_release_sha: null, bound_release_sha: null,
      codex_version: null, boot_id: null, invocation_id: null, origin: 'live' },
    limits: { diskReserveBytes: 0, flushMilliseconds: 60_000 },
  })
  t.after(() => writer.close())
  await writer.flush()
  assert.equal((await readRhObservationManifest(directory, 'handshake')).source.codex_version, null)
  const rename = fs.promises.rename.bind(fs.promises)
  let release!: () => void
  let entered!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  const blocked = new Promise<void>((resolve) => { entered = resolve })
  let held = false
  const mock = t.mock.method(fs.promises, 'rename', async (source: fs.PathLike, destination: fs.PathLike) => {
    if (!held && String(destination).endsWith('manifest.json')) { held = true; entered(); await gate }
    return rename(source, destination)
  })
  writer.admit(observation('handshake', 1))
  const precedingCut = writer.flush()
  await blocked
  assert.equal(writer.recordCodexVersion('0.149.1'), true)
  assert.equal(writer.recordCodexVersion('0.149.1'), true)
  assert.equal(writer.recordCodexVersion('0.150.0'), false)
  writer.admit(observation('handshake', 2))
  release()
  await precedingCut
  mock.mock.restore()
  await writer.flush()
  assert.equal((await readRhObservationManifest(directory, 'handshake')).source.codex_version, '0.149.1')
})
