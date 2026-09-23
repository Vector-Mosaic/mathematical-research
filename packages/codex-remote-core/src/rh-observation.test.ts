import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import {
  mergeRhContentPage,
  mergeRhObservations,
  rhObservationKey,
  type RhObservation,
  type RhObservationContentHandle,
  type RhObservationContentPage,
} from './rh-observation.js'

function observation(sequence: number, overrides: Partial<RhObservation> = {}): RhObservation {
  return {
    incarnation: 'host-boot-1',
    sequence,
    received_at: '2026-09-05T12:00:00.000Z',
    source_time: null,
    phase: 'received',
    kind: 'message',
    source_method: 'item/agentMessage/delta',
    identity: {
      mission_id: 'rh', epoch_id: 'epoch-1', root_thread_id: 'root-1',
      thread_id: 'worker-1', turn_id: 'turn-1', item_id: 'message-1',
      operation_id: null, parent_thread_id: 'root-1',
    },
    summary: 'same text',
    details: { role: 'assistant', phase: 'commentary' },
    content: [],
    caused_by: null,
    ...overrides,
  } as RhObservation
}

function contentPage(text: string, overrides: Partial<RhObservationContentHandle> = {}): RhObservationContentPage {
  const bytes = new TextEncoder().encode(text)
  const start = overrides.start_byte ?? 0
  return {
    schema_version: 'rh_observation.v1',
    kind: 'content',
    selection: { mission_id: 'rh', epoch_id: 'epoch-1', root_thread_id: 'root-1', incarnation: 'host-boot-1' },
    source: {
      host_id: 'rh-host', release_sha: null, selected_release_sha: null, bound_release_sha: null,
      codex_version: null, boot_id: 'host-boot-1', invocation_id: null, origin: 'live',
    },
    watermark: { incarnation: 'host-boot-1', sequence: 10, content_sequence: 10, journal_cursor: null },
    coverage: [], next_cursor: null, has_more: false,
    availability: 'available',
    handle: {
      incarnation: 'host-boot-1', content_id: 'command-output-1', field: 'output', revision: 1,
      update_mode: 'snapshot', start_byte: start, end_byte: start + bytes.length,
      total_bytes: start + bytes.length, complete: true, encoding: 'utf-8',
      sha256: createHash('sha256').update(bytes).digest('hex'),
      ...overrides,
    },
    text,
  }
}

test('receipt retries preserve the original observation and do not mutate caller arrays', () => {
  const first = observation(1)
  const existing = Object.freeze([first])
  const repeated = { ...first, summary: 'later transport copy' }
  const next = observation(2)
  const incoming = Object.freeze([repeated, next, next])
  const merged = mergeRhObservations(existing, incoming)

  assert.deepEqual(merged, [first, next])
  assert.equal(merged[0], first)
  assert.deepEqual(existing, [first])
  assert.equal(incoming.length, 3)
})

test('receipt identity includes source incarnation without inventing ordering across restarts', () => {
  const beforeRestart = observation(40)
  const afterRestart = observation(1, { incarnation: 'host-boot-2' })
  const oldSequence = observation(1)

  assert.notEqual(rhObservationKey(afterRestart), rhObservationKey(oldSequence))
  assert.deepEqual(
    mergeRhObservations([beforeRestart, oldSequence], [afterRestart, oldSequence]),
    [beforeRestart, oldSequence, afterRestart],
  )
})

test('identical native delta text survives when each delta has its own receipt', () => {
  const first = observation(1)
  const second = observation(2)
  const merged = mergeRhObservations([], [first, second, first, second])

  assert.equal(merged.length, 2)
  assert.equal(merged.map((item) => item.summary).join(''), 'same textsame text')
})

test('journal record identity survives reassigned page ordinals after middle-row vacuum', () => {
  const before = observation(2, { journal_cursor: 'opaque-row-before-vacuum' })
  const after = observation(2, { journal_cursor: 'opaque-different-row' })
  const replay = observation(1, { journal_cursor: 'opaque-row-before-vacuum' })
  assert.deepEqual(mergeRhObservations([before], [after, replay]), [before, after])
  assert.notEqual(rhObservationKey(before), rhObservationKey(observation(2)))
})

test('separate message items in one turn remain separate observations', () => {
  const commentary = observation(1)
  const final = observation(2, {
    identity: { ...commentary.identity, item_id: 'message-2' },
    details: { role: 'assistant', phase: 'final' },
  })

  assert.deepEqual(mergeRhObservations([commentary], [final]), [commentary, final])
})

test('content pages join at UTF-8 byte offsets and complete only at the full committed extent', () => {
  // A=1 byte, é=2 bytes, 🙂=4 bytes, Z=1 byte; JS string length is only 5.
  const first = contentPage('Aé', { total_bytes: 8 })
  const second = contentPage('🙂Z', { start_byte: 3, total_bytes: 8 })
  const partial = mergeRhContentPage(null, first)
  assert.ok(partial)
  assert.equal(partial.text, 'Aé')
  assert.equal(partial.end_byte, 3)
  assert.equal(partial.complete, false)

  const joined = mergeRhContentPage(partial, second)
  assert.ok(joined)
  assert.equal(joined.text, 'Aé🙂Z')
  assert.equal(joined.end_byte, 8)
  assert.equal(joined.complete, true)
})

test('reconnect replay of earlier content pages does not duplicate already assembled text', () => {
  const first = contentPage('hello ', { total_bytes: 11 })
  const second = contentPage('world', { start_byte: 6, total_bytes: 11 })
  const joined = mergeRhContentPage(mergeRhContentPage(null, first), second)
  const replayed = mergeRhContentPage(mergeRhContentPage(joined, first), second)

  assert.equal(replayed, joined)
  assert.equal(replayed?.text, 'hello world')
})

test('same-revision replay with conflicting bytes is rejected instead of silently discarded', () => {
  const assembled = mergeRhContentPage(null, contentPage('hello'))
  assert.throws(() => mergeRhContentPage(assembled, contentPage('HELLO')))
})

test('covered-range retries compare UTF-8 byte slices without character-index corruption', () => {
  const assembled = mergeRhContentPage(null, contentPage('Aé🙂Z'))
  const retry = contentPage('🙂', { start_byte: 3, total_bytes: 8 })

  assert.equal(mergeRhContentPage(assembled, retry), assembled)
  assert.throws(() => mergeRhContentPage(assembled, contentPage('🙃', { start_byte: 3, total_bytes: 8 })))
})

test('append publications retain repeated native text and deduplicate old receipts by exact bytes', () => {
  const first = contentPage('ha', { update_mode: 'append', revision: 1, complete: false })
  const second = contentPage('ha', { update_mode: 'append', revision: 2, start_byte: 2, complete: false })
  const third = contentPage('!', { update_mode: 'append', revision: 3, start_byte: 4 })
  const partial = mergeRhContentPage(mergeRhContentPage(null, first), second)
  assert.equal(partial?.text, 'haha')
  assert.equal(partial?.total_bytes, 4)
  assert.equal(partial?.complete, false)
  const completed = mergeRhContentPage(partial, third)
  assert.equal(completed?.text, 'haha!')
  assert.equal(completed?.revision, 3)
  assert.equal(completed?.complete, true)

  const replayed = mergeRhContentPage(mergeRhContentPage(completed, first), second)
  assert.equal(replayed, completed)
  assert.throws(() => mergeRhContentPage(completed, contentPage('HA', {
    update_mode: 'append', revision: 1, complete: false,
  })))
})

test('append content requires a captured prefix and cannot bridge a missing byte range', () => {
  const second = contentPage('world', { update_mode: 'append', revision: 2, start_byte: 6 })
  assert.throws(() => mergeRhContentPage(null, second), /first page|prefix|gap/i)

  const first = mergeRhContentPage(null, contentPage('hello', { update_mode: 'append', complete: false }))
  assert.throws(() => mergeRhContentPage(first, second), /gap|overlap/i)
})

test('a new empty append revision marks unchanged output complete without duplicating text', () => {
  const running = mergeRhContentPage(null, contentPage('finished output', { update_mode: 'append', complete: false }))
  assert.ok(running)
  assert.equal(running.complete, false)
  const completion = contentPage('', {
    update_mode: 'append', revision: 2, start_byte: running.end_byte, complete: true,
  })
  const completed = mergeRhContentPage(running, completion)

  assert.equal(completed?.text, 'finished output')
  assert.equal(completed?.revision, 2)
  assert.equal(completed?.end_byte, running.end_byte)
  assert.equal(completed?.total_bytes, running.total_bytes)
  assert.equal(completed?.complete, true)
  assert.equal(mergeRhContentPage(completed, completion), completed)
  assert.equal(running.complete, false)
})

test('gaps and partial overlaps cannot silently corrupt assembled content', () => {
  const partial = mergeRhContentPage(null, contentPage('hello ', { total_bytes: 11 }))

  assert.throws(
    () => mergeRhContentPage(partial, contentPage('orld', { start_byte: 7, total_bytes: 11 })),
    /gap|overlap/i,
  )
  assert.throws(
    () => mergeRhContentPage(partial, contentPage(' world', { start_byte: 5, total_bytes: 11 })),
    /gap|overlap/i,
  )
  assert.equal(partial?.text, 'hello ')
})

test('content range validation measures UTF-8 bytes rather than UTF-16 code units', () => {
  assert.throws(
    () => mergeRhContentPage(null, contentPage('🙂', { end_byte: 2, total_bytes: 2 })),
    /byte range|UTF-8/i,
  )
})

test('a newer revision is reread from byte zero and replaces the previous snapshot once', () => {
  const old = mergeRhContentPage(null, contentPage('hello', { complete: false }))
  const first = contentPage('hello ', { revision: 2, total_bytes: 11, complete: false })
  const second = contentPage('world', { revision: 2, start_byte: 6, total_bytes: 11 })

  assert.throws(() => mergeRhContentPage(old, second), /first page/i)
  const replacement = mergeRhContentPage(old, first)
  assert.equal(replacement?.text, 'hello ')
  assert.equal(replacement?.revision, 2)
  assert.equal(replacement?.complete, false)
  const completed = mergeRhContentPage(replacement, second)
  assert.equal(completed?.text, 'hello world')
  assert.equal(completed?.complete, true)
  assert.equal(old?.text, 'hello')
})

test('the same content id and revision from a new incarnation replaces old host content', () => {
  const old = mergeRhContentPage(null, contentPage('old process'))
  const replacement = mergeRhContentPage(old, contentPage('new process', { incarnation: 'host-boot-2' }))

  assert.equal(replacement?.text, 'new process')
  assert.equal(replacement?.incarnation, 'host-boot-2')
})
