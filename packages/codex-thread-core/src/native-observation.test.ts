import assert from 'node:assert/strict'
import test from 'node:test'

import { ObservationTextSanitizer } from '../../codex-remote-core/dist/index.js'
import { classifyCollaborationPrompt, decodeNativeAgentMessage, nativeObservationThreadId, projectNativeObservation, type CoreObservationDraft, type CoreObservationScope } from './native-observation.js'

const scope: CoreObservationScope = {
  rootThreadId: 'root', parentThreadId: 'root', depth: 1,
  receiptId: 'incarnation:1', receivedAt: '2026-09-05T12:00:00.000Z',
}

function itemNotification(item: Record<string, unknown>, complete = false) {
  return {
    method: complete ? 'item/completed' : 'item/started',
    params: { threadId: 'worker', turnId: 'turn', item,
      ...(complete ? { completedAtMs: 1788609601000 } : { startedAtMs: 1788609600000 }) },
  }
}

function ofKind<K extends CoreObservationDraft['kind']>(observations: CoreObservationDraft[], kind: K): Extract<CoreObservationDraft, { kind: K }> {
  const result = observations.find((observation) => observation.kind === kind)
  assert.ok(result, `Expected a ${kind} observation`)
  return result as Extract<CoreObservationDraft, { kind: K }>
}

const contents = (observations: CoreObservationDraft[]) => observations.flatMap((observation) => observation.contents)
const reasons = (observations: CoreObservationDraft[]) => observations.filter((observation) => observation.kind === 'coverage').map((observation) => observation.details.reason)

test('0.153.4 function outputs preserve exact text and native identity without inferred execution status', () => {
  const item = { type: 'functionCallOutput', id: 'function-result', name: 'inspect_result', namespace: 'project', output: '  Exact ∑ result\n' }
  const first = ofKind(projectNativeObservation(itemNotification(item, true), scope), 'tool')
  assert.equal(first.details.tool_name, 'inspect_result')
  assert.equal(first.details.server, 'project')
  assert.equal(first.details.output_mode, 'completion_only')
  assert.equal(first.details.status, undefined)
  assert.equal(first.details.success, undefined)
  assert.equal(first.identity.item_id, 'function-result')
  assert.equal(first.identity.operation_id, 'function-result')
  assert.equal(first.contents[0].field, 'result')
  assert.equal(first.contents[0].text, item.output)
  assert.equal(first.contents[0].complete, true)
  const replay = ofKind(projectNativeObservation(itemNotification(item, true), { ...scope, receiptId: 'incarnation:2' }), 'tool')
  assert.equal(replay.contents[0].content_key, first.contents[0].content_key)
  const another = ofKind(projectNativeObservation(itemNotification({ ...item, id: 'other-result' }, true), scope), 'tool')
  assert.notEqual(another.contents[0].content_key, first.contents[0].content_key)
  const partial = ofKind(projectNativeObservation(itemNotification({ ...item, namespace: undefined, output: '' }), scope), 'tool')
  assert.equal(partial.contents[0].text, '')
  assert.equal(partial.contents[0].complete, false)
})

test('0.153.4 function output arrays preserve text order and explicitly exclude unsupported payloads', () => {
  const forbidden = () => { throw new Error('opaque payload must not be read') }
  const observations = projectNativeObservation(itemNotification({ type: 'functionCallOutput', id: 'parts', name: 'function', output: [
    { type: 'input_text', text: 'First\n', get privateMetadata() { return forbidden() } },
    { type: 'input_image', get image_url() { return forbidden() } },
    { type: 'input_audio', get audio_url() { return forbidden() } },
    { type: 'encrypted_content', get encrypted_content() { return forbidden() } },
    { type: 'input_text', text: 'Second' },
  ] }, true), scope)
  const result = ofKind(observations, 'tool')
  assert.deepEqual(result.contents.map((part) => part.text), ['First\n', 'Second'])
  assert.notEqual(result.contents[0].content_key, result.contents[1].content_key)
  assert.equal(ofKind(observations, 'coverage').details.count, 3)
  assert.ok(reasons(observations).includes('native_function_output_content_unavailable'))
  const malformed = projectNativeObservation(itemNotification({ type: 'functionCallOutput', id: 'bad', name: 'function', output: { text: 'Not a native output body' } }, true), scope)
  assert.deepEqual(contents(malformed), [])
  assert.ok(reasons(malformed).includes('native_function_output_content_unavailable'))
})

test('0.153.4 assistant questions preserve titles and options separately from message text and deltas', () => {
  const item = { type: 'agentMessage', id: 'question-message', phase: 'commentary', text: 'Choose the route.',
    questions: [{ title: 'Which bound?\n', options: ['First', 'Second'] }, { title: 'Why?', options: null }],
    get memoryCitation() { throw new Error('private metadata must not be read') } }
  const message = ofKind(projectNativeObservation(itemNotification(item, true), scope), 'message')
  assert.deepEqual(message.contents.map((part) => part.text), ['Choose the route.', 'Which bound?\n', 'First', 'Second', 'Why?'])
  assert.equal(new Set(message.contents.map((part) => part.content_key)).size, 5)
  assert.ok(message.contents.every((part) => part.field === 'message' && part.complete))
  const delta = ofKind(projectNativeObservation({ method: 'item/agentMessage/delta', params: { threadId: 'worker', turnId: 'turn', itemId: item.id, delta: 'Choose' } }, scope), 'message')
  assert.equal(delta.contents[0].content_key, message.contents[0].content_key)
  assert.notEqual(delta.contents[0].content_key, message.contents[1].content_key)
  for (const questions of [null, undefined, []]) {
    const oldMessage = projectNativeObservation(itemNotification({ type: 'agentMessage', id: 'old', text: 'Existing message', questions }, true), scope)
    assert.deepEqual(contents(oldMessage).map((part) => part.text), ['Existing message'])
    assert.equal(reasons(oldMessage).length, 0)
  }
  const malformed = projectNativeObservation(itemNotification({ type: 'agentMessage', id: 'bad-question', text: 'Still readable',
    questions: [{ title: 'Valid title', options: [5, 'Valid option'] }, { title: false }] }, true), scope)
  assert.deepEqual(contents(malformed).map((part) => part.text), ['Still readable', 'Valid title', 'Valid option'])
  assert.ok(reasons(malformed).includes('native_agent_question_content_unavailable'))
})

test('collaboration prompt classification requires proven direct-parent facts for every target', () => {
  assert.equal(classifyCollaborationPrompt('spawnAgent', null, []), 'assignment')
  assert.equal(classifyCollaborationPrompt('sendInput', 'parent', ['parent', 'parent']), 'assignment')
  for (const parents of [[], ['sibling'], ['parent', 'sibling'], ['parent', null], [undefined]]) {
    assert.equal(classifyCollaborationPrompt('sendInput', 'parent', parents), 'message')
  }
  assert.equal(classifyCollaborationPrompt('sendInput', null, [null]), 'message')
  assert.equal(classifyCollaborationPrompt('wait', 'parent', ['parent']), 'message')
  const item = { type: 'collabAgentToolCall', id: 'send', tool: 'sendInput', senderThreadId: 'worker',
    receiverThreadIds: ['child', 'peer'], status: 'completed', prompt: 'Useful shared input.', agentsStates: {} }
  const peer = ofKind(projectNativeObservation(itemNotification(item, true), scope), 'work')
  const direct = ofKind(projectNativeObservation(itemNotification(item, true), { ...scope, collaborationPromptField: 'assignment' }), 'work')
  assert.equal(peer.contents[0].field, 'message')
  assert.equal(direct.contents[0].field, 'assignment')
  assert.deepEqual(peer.details.receiver_thread_ids, ['child', 'peer'])
})

test('raw envelope decoder admits only matching plaintext MESSAGE and FINAL_ANSWER envelopes', () => {
  for (const kind of ['MESSAGE', 'FINAL_ANSWER']) {
    const item = { type: 'agent_message', id: 'raw', author: '/root/a', recipient: '/root/b', content: [{
      type: 'input_text', text: `Message Type: ${kind}\nTask name: /root/b\nSender: /root/a\nPayload:\nUseful body.`,
    }] }
    const decoded = decodeNativeAgentMessage(item)
    assert.deepEqual(decoded, { kind, author: '/root/a', recipient: '/root/b', content: 'Useful body.' })
    const message = { method: 'rawResponseItem/completed', params: { threadId: 'b', turnId: 'turn', item } }
    assert.equal(contents(projectNativeObservation(message, scope)).length, 0)
    const work = ofKind(projectNativeObservation(message, { ...scope, nativeAgentMessage: {
      message: decoded!, senderThreadId: 'a', receiverThreadId: 'b',
    } }), 'work')
    assert.equal(work.identity.thread_id, 'b')
    assert.equal(work.identity.item_id, 'raw')
    assert.equal(work.details.sender_thread_id, 'a')
    assert.deepEqual(work.details.receiver_thread_ids, ['b'])
    assert.equal(work.contents[0].text, 'Useful body.')
    assert.equal(work.contents[0].field, 'message')
    assert.equal(decodeNativeAgentMessage({ ...item, author: '/root/spoof' }), null)
    assert.equal(decodeNativeAgentMessage({ ...item, recipient: '/root/spoof' }), null)
    assert.equal(decodeNativeAgentMessage({ ...item, content: [...item.content, { type: 'encrypted_content', encrypted_content: 'opaque' }] }), null)
    assert.equal(decodeNativeAgentMessage({ ...item, content: [{ type: 'output_text', text: item.content[0].text }] }), null)
    assert.equal(decodeNativeAgentMessage({ ...item, content: [{ type: 'input_text', text: item.content[0].text.replace(kind, 'NEW_TASK') }] }), null)
  }
})

test('command output is available while running and final snapshot replaces the same field', () => {
  const command = { type: 'commandExecution', id: 'command-1', command: 'python diagnostic.py', cwd: '/work/rh',
    commandActions: [], status: 'inProgress', processId: 'pty-1', aggregatedOutput: null, exitCode: null, durationMs: null }
  const start = ofKind(projectNativeObservation(itemNotification(command), scope), 'command')
  const deltaEnvelope = { method: 'item/commandExecution/outputDelta', params: { threadId: 'worker', turnId: 'turn', itemId: 'command-1', delta: 'step one\n' } }
  const delta = ofKind(projectNativeObservation(deltaEnvelope, { ...scope, receiptId: 'incarnation:2' }), 'command')
  assert.equal(start.details.status, 'inProgress')
  assert.equal(start.details.exit_code, null)
  assert.equal(start.details.signal, null)
  assert.equal(start.details.process_id, 'pty-1')
  assert.equal(start.contents[0].field, 'command')
  assert.equal(delta.contents[0].text, 'step one\n')
  assert.equal(delta.contents[0].complete, false)
  assert.equal(delta.contents[0].update_mode, 'append')
  assert.equal(delta.details.output_mode, 'streaming')
  assert.equal(delta.source_time, null)
  assert.equal(delta.received_at, scope.receivedAt)
  assert.equal(Object.hasOwn(delta.contents[0], 'offset'), false)
  const finish = ofKind(projectNativeObservation(itemNotification({ ...command, status: 'completed', aggregatedOutput: 'step one\nstep two\n', exitCode: 0, durationMs: 1000 }, true), scope), 'command')
  const finalOutput = finish.contents.find((entry) => entry.field === 'output')!
  assert.equal(finish.details.exit_code, 0)
  assert.equal(finish.details.duration_ms, 1000)
  assert.equal(finish.details.output_mode, 'unknown')
  assert.equal(finalOutput.content_key, delta.contents[0].content_key)
  assert.equal(finalOutput.update_mode, 'snapshot')
  assert.equal(finalOutput.complete, true)
  assert.equal(finish.source_time, '2026-09-05T12:00:01.000Z')
  // Native replay cannot be deduplicated by identical text; local receipts remain the caller's job.
  const repeated = ofKind(projectNativeObservation(deltaEnvelope, { ...scope, receiptId: 'incarnation:3' }), 'command')
  assert.equal(repeated.contents[0].text, delta.contents[0].text)
  assert.equal(repeated.contents[0].content_key, delta.contents[0].content_key)
})

test('agent delta and final snapshot share identity without guessing phase during streaming', () => {
  const delta = ofKind(projectNativeObservation({ method: 'item/agentMessage/delta', params: { threadId: 'worker', turnId: 'turn', itemId: 'answer', delta: 'Partial answer' } }, scope), 'message')
  const final = ofKind(projectNativeObservation(itemNotification({ type: 'agentMessage', id: 'answer', phase: 'final_answer', text: 'Final answer' }, true), scope), 'message')
  assert.equal(delta.details.phase, null)
  assert.equal(final.details.phase, 'final')
  assert.equal(delta.contents[0].content_key, final.contents[0].content_key)
  const another = ofKind(projectNativeObservation(itemNotification({ type: 'agentMessage', id: 'another-answer', phase: null, text: 'Final answer' }, true), scope), 'message')
  assert.notEqual(final.contents[0].content_key, another.contents[0].content_key)
  const unknown = ofKind(projectNativeObservation(itemNotification({ type: 'agentMessage', id: 'unknown-phase', phase: 'future_phase', text: 'Visible text' }), scope), 'message')
  assert.equal(unknown.details.phase, null)
})

test('missing native identities use exact receipt keys and never search nested text or sender identities', () => {
  const missingItem = { method: 'item/agentMessage/delta', params: { threadId: 'worker', turnId: 'turn', delta: 'same' } }
  const first = projectNativeObservation(missingItem, scope)
  const second = projectNativeObservation(missingItem, { ...scope, receiptId: 'incarnation:2' })
  assert.notEqual(contents(first)[0].content_key, contents(second)[0].content_key)
  assert.equal(ofKind(first, 'message').identity.item_id, null)
  assert.ok(reasons(first).includes('native_item_identity_unavailable_receipt_scoped_content'))
  assert.equal(nativeObservationThreadId({ method: 'item/started', params: { item: { senderThreadId: 'foreign', threadId: 'foreign' }, thread: { id: 'foreign' } } }), null)
  assert.equal(nativeObservationThreadId({ method: 'thread/started', params: { thread: { id: 'child' } } }), 'child')
  assert.equal(nativeObservationThreadId({ method: 'item/started', params: { threadId: 'outer', item: { senderThreadId: 'sender' } } }), 'outer')
})

test('collaboration preserves outer thread, sender and targets while an attempted spawn creates no child', () => {
  const attempted = projectNativeObservation(itemNotification({ type: 'collabAgentToolCall', id: 'spawn', tool: 'spawnAgent', status: 'inProgress', senderThreadId: 'different-sender', receiverThreadIds: [], agentsStates: {}, prompt: 'Check the lemma', model: 'gpt-6-astra', reasoningEffort: 'high' }), scope)
  const work = ofKind(attempted, 'work')
  assert.equal(work.identity.thread_id, 'worker')
  assert.equal(work.details.sender_thread_id, 'different-sender')
  assert.deepEqual(work.details.receiver_thread_ids, [])
  assert.equal(work.details.child_thread_id, null)
  assert.equal(work.contents[0].field, 'assignment')
  assert.equal(work.contents[0].text, 'Check the lemma')
  assert.equal(work.details.requested_model, 'gpt-6-astra')
  assert.equal(work.details.requested_reasoning_effort, 'high')
  const completed = projectNativeObservation(itemNotification({ type: 'collabAgentToolCall', id: 'spawn', tool: 'spawnAgent', status: 'completed', senderThreadId: 'worker', receiverThreadIds: ['child'], agentsStates: { child: { status: 'running', message: 'Checking the bound' } }, prompt: 'Check the lemma' }, true), scope)
  assert.equal(ofKind(completed, 'work').details.child_thread_id, null)
  assert.ok(contents(completed).some((entry) => entry.text === 'Checking the bound'))
  const registered = ofKind(projectNativeObservation({ method: 'thread/started', params: { thread: { id: 'child', parentThreadId: 'worker', status: { type: 'active', activeFlags: [] } } } }, scope), 'work')
  assert.equal(registered.identity.parent_thread_id, 'worker')
  assert.equal(registered.details.child_thread_id, 'child')
  const activity = ofKind(projectNativeObservation(itemNotification({ type: 'subAgentActivity', id: 'activity', agentThreadId: 'child', agentPath: '/root/worker/child', kind: 'interacted' }), scope), 'work')
  assert.equal(activity.details.agent_path, '/root/worker/child')
  assert.deepEqual(activity.details.receiver_thread_ids, ['child'])
  const conflicting = ofKind(projectNativeObservation({ method: 'thread/started', params: { thread: {
    id: 'child', parentThreadId: 'declared-parent', status: { type: 'idle' },
    source: { subAgent: { thread_spawn: { parent_thread_id: 'source-parent', depth: 7, agent_path: '/root/source-parent/child' } } },
  } } }, { ...scope, parentThreadId: 'bound-parent', depth: 1 }), 'work')
  assert.equal(conflicting.identity.parent_thread_id, 'declared-parent')
  assert.equal(conflicting.details.source_parent_thread_id, 'source-parent')
  assert.equal(conflicting.details.source_depth, 7)
  assert.equal(conflicting.details.depth, 1)
})

test('dynamic requests keep call identity distinct and documented tool results are completion-only', () => {
  const request = ofKind(projectNativeObservation({ id: 17, method: 'item/tool/call', params: { threadId: 'worker', turnId: 'turn', callId: 'call-1', tool: 'read_file', namespace: 'research', arguments: { path: 'lemma.lean' } } }, scope), 'tool')
  assert.equal(request.identity.operation_id, 'call-1')
  assert.equal(request.identity.item_id, null)
  assert.equal(request.details.status, 'requested')
  const projected = projectNativeObservation(itemNotification({ type: 'dynamicToolCall', id: 'dynamic-item', tool: 'read_file', namespace: 'research', status: 'completed', success: true,
    durationMs: 84, arguments: { path: 'lemma.lean', api_key: 'fixture-credential-value' }, contentItems: [{ type: 'inputText', text: 'lemma result' }, { type: 'inputImage', imageUrl: 'data:PRIVATE_IMAGE_BYTES' }] }, true), scope)
  const tool = ofKind(projected, 'tool')
  assert.equal(tool.details.output_mode, 'completion_only')
  assert.equal(tool.details.success, true)
  assert.equal(tool.details.duration_ms, 84)
  assert.equal(tool.contents.find((entry) => entry.field === 'arguments')!.sanitized, true)
  assert.equal(tool.contents.find((entry) => entry.field === 'result')!.sanitized, undefined)
  assert.ok(tool.contents.some((entry) => entry.text === 'lemma result' && entry.complete))
  assert.equal(JSON.stringify(projected).includes('fixture-credential-value'), false)
  assert.equal(JSON.stringify(projected).includes('PRIVATE_IMAGE_BYTES'), false)
  assert.ok(reasons(projected).includes('structured_content_redacted'))
  assert.ok(reasons(projected).includes('native_tool_content_variant_unavailable'))
})

test('MCP visible text and structured data survive, private metadata and unknown entries do not', () => {
  const projected = projectNativeObservation(itemNotification({ type: 'mcpToolCall', id: 'mcp', tool: 'inspect', server: 'research', status: 'completed', arguments: { query: 'bound' }, result: {
    content: [{ type: 'text', text: 'Useful result' }, { type: 'resource', resource: { uri: 'project://lemma', text: 'Proof outline' } }, { type: 'futureType', internal: 'UNKNOWN_NESTED_BYTES' }],
    structuredContent: { rows: [{ count: 3, password: 'fixture-password' }] }, _meta: { private: 'PRIVATE_METADATA_BYTES' },
  } }, true), scope)
  const text = JSON.stringify(projected)
  assert.ok(text.includes('Useful result'))
  assert.ok(text.includes('Proof outline'))
  assert.ok(contents(projected).some((entry) => entry.text.includes('count')))
  for (const excluded of ['fixture-password', 'PRIVATE_METADATA_BYTES', 'UNKNOWN_NESTED_BYTES']) assert.equal(text.includes(excluded), false)
  const progress = ofKind(projectNativeObservation({ method: 'item/mcpToolCall/progress', params: { threadId: 'worker', turnId: 'turn', itemId: 'mcp', message: 'Reading index' } }, scope), 'tool')
  assert.equal(progress.details.output_mode, 'completion_only')
  assert.equal(progress.contents[0].field, 'message')
})

test('file patch snapshots keep diff identity across updates and expose move destination', () => {
  const change = { path: '/work/rh/lemma.lean', kind: { type: 'update', move_path: '/work/rh/proof.lean' }, diff: '-old\n+new' }
  const update = projectNativeObservation({ method: 'item/fileChange/patchUpdated', params: { threadId: 'worker', turnId: 'turn', itemId: 'patch', changes: [change] } }, scope)
  const finish = projectNativeObservation(itemNotification({ type: 'fileChange', id: 'patch', status: 'completed', changes: [change] }, true), scope)
  assert.equal(ofKind(update, 'file').details.change_kind, 'update')
  assert.equal(ofKind(update, 'file').contents[0].complete, false)
  assert.equal(ofKind(finish, 'file').contents[0].complete, true)
  assert.equal(ofKind(update, 'file').contents[0].content_key, ofKind(finish, 'file').contents[0].content_key)
  assert.equal(ofKind(update, 'artifact').details.path, '/work/rh/proof.lean')
  const deprecated = projectNativeObservation({ method: 'item/fileChange/outputDelta', params: { threadId: 'worker', turnId: 'turn', itemId: 'patch', delta: 'LEGACY_OUTPUT' } }, scope)
  assert.deepEqual(contents(deprecated), [])
  assert.ok(reasons(deprecated).includes('native_file_change_output_delta_not_emitted_in_pinned_api'))
  const started = projectNativeObservation(itemNotification({ type: 'fileChange', id: 'patch', status: 'inProgress', changes: [] }), scope)
  assert.equal(ofKind(started, 'file').details.status, 'inProgress')
  const other = { path: '/work/rh/other.lean', kind: { type: 'add' }, diff: '+other' }
  const twoFiles = projectNativeObservation(itemNotification({ type: 'fileChange', id: 'patch', status: 'inProgress', changes: [change, other] }), scope)
  const reordered = projectNativeObservation(itemNotification({ type: 'fileChange', id: 'patch', status: 'completed', changes: [other, change] }, true), scope)
  const keyByPath = (entries: CoreObservationDraft[]) => Object.fromEntries(entries.filter((entry) => entry.kind === 'file').map((entry) => [entry.details.path, entry.contents[0].content_key]))
  assert.deepEqual(keyByPath(twoFiles), keyByPath(reordered))
})

test('unknown native variants and reasoning never use a JSON transcript fallback', () => {
  for (const input of [
    itemNotification({ type: 'reasoning', id: 'hidden', content: ['HIDDEN_CONTENT'], summary: ['HIDDEN_SUMMARY'] }),
    itemNotification({ type: 'futureType', id: 'future', text: 'UNKNOWN_TEXT', deeply: { credential: 'UNKNOWN_CREDENTIAL' } }),
    { method: 'item/reasoning/textDelta', params: { threadId: 'worker', turnId: 'turn', itemId: 'hidden', delta: 'HIDDEN_DELTA' } },
    { method: 'future/method', params: { threadId: 'worker', payload: 'UNKNOWN_PAYLOAD' } },
  ]) {
    const projected = projectNativeObservation(input, scope)
    assert.deepEqual(contents(projected), [])
    assert.ok(projected.every((entry) => entry.kind === 'coverage'))
    assert.equal(/HIDDEN_|UNKNOWN_/.test(JSON.stringify(projected)), false)
  }
  assert.deepEqual(projectNativeObservation(null, scope), [])
  assert.deepEqual(projectNativeObservation({ result: { text: 'UNSCOPED_RESPONSE' } }, scope), [])
})

test('turn lifecycle separates source time from receipt and preserves native error code and text', () => {
  const input = { method: 'turn/completed', params: { threadId: 'worker', turn: { id: 'turn', status: 'failed', completedAt: 1788609601,
    error: { message: 'Connection ended', additionalDetails: 'Retry exhausted', codexErrorInfo: { responseStreamDisconnected: { httpStatusCode: 502, unrelated: 'DO_NOT_COPY' } } },
    items: [{ type: 'reasoning', id: 'hidden', content: ['DO_NOT_COPY_ITEMS'] }] } } }
  const projected = projectNativeObservation(input, scope)
  assert.equal(ofKind(projected, 'lifecycle').source_time, '2026-09-05T12:00:01.000Z')
  assert.equal(ofKind(projected, 'error').details.code, 'responseStreamDisconnected')
  assert.equal(ofKind(projected, 'error').details.actual, 502)
  assert.ok(contents(projected).some((entry) => entry.text === 'Connection ended'))
  assert.ok(contents(projected).some((entry) => entry.text === 'Retry exhausted'))
  assert.equal(JSON.stringify(projected).includes('DO_NOT_COPY'), false)
  const waiting = projectNativeObservation({ method: 'thread/status/changed', params: { threadId: 'worker', status: { type: 'active', activeFlags: ['waitingOnUserInput'] } } }, scope)
  assert.deepEqual(waiting.map((entry) => entry.kind === 'lifecycle' && entry.details.state), ['active', 'waitingOnUserInput'])
})

test('message inputs expose text and resource paths', () => {
  const message = projectNativeObservation(itemNotification({ type: 'userMessage', id: 'user', content: [{ type: 'text', text: 'Check this lemma' }, { type: 'localImage', path: '/work/rh/plot.png' }] }, true), scope)
  assert.equal(ofKind(message, 'message').contents[0].text, 'Check this lemma')
  assert.equal(ofKind(message, 'artifact').details.path, '/work/rh/plot.png')
})

test('missing final command, message or plan text closes its stream without replacing partial content', () => {
  const cases = [
    { deltaMethod: 'item/commandExecution/outputDelta', kind: 'command' as const, field: 'output',
      item: { type: 'commandExecution', id: 'command', command: 'check', commandActions: [], cwd: '/work/rh', status: 'failed', aggregatedOutput: null },
      reason: 'native_command_completion_output_unavailable' },
    { deltaMethod: 'item/agentMessage/delta', kind: 'message' as const, field: 'message',
      item: { type: 'agentMessage', id: 'answer', phase: 'final_answer' },
      reason: 'native_agent_message_completion_text_unavailable' },
    { deltaMethod: 'item/plan/delta', kind: 'message' as const, field: 'message',
      item: { type: 'plan', id: 'plan' }, reason: 'native_plan_completion_text_unavailable' },
  ]
  for (const fixture of cases) {
    const delta = projectNativeObservation({ method: fixture.deltaMethod, params: {
      threadId: 'worker', turnId: 'turn', itemId: fixture.item.id, delta: 'Partial ordinary output ending in auth',
    } }, scope)
    const prior = contents(delta).find((entry) => entry.field === fixture.field)!
    const missingFinal = projectNativeObservation(itemNotification(fixture.item, true), scope)
    const closing = ofKind(missingFinal, fixture.kind).contents.find((entry) => entry.field === fixture.field)!
    assert.deepEqual(closing, { content_key: prior.content_key, field: fixture.field, text: '', update_mode: 'append', complete: true })
    assert.ok(reasons(missingFinal).includes(fixture.reason))
    assert.equal(prior.complete, false)
    const sanitizer = new ObservationTextSanitizer()
    const streamed = sanitizer.push(prior.text)
    assert.ok(streamed.pendingChars > 0)
    const closingChunk = sanitizer.push(closing.text)
    const tail = sanitizer.finish()
    assert.equal(streamed.text + closingChunk.text + tail.text, prior.text)
    assert.equal(tail.pendingChars, 0)
    const stillRunning = projectNativeObservation(itemNotification(fixture.item, false), scope)
    assert.equal(contents(stillRunning).some((entry) => entry.field === fixture.field && entry.complete), false)
    const disconnected = projectNativeObservation({ method: 'thread/environment/disconnected', params: { threadId: 'worker' } }, scope)
    assert.deepEqual(contents(disconnected), [])
    const emptyFinal = projectNativeObservation(itemNotification({ ...fixture.item,
      ...(fixture.kind === 'command' ? { aggregatedOutput: '' } : { text: '' }),
    }, true), scope)
    assert.equal(ofKind(emptyFinal, fixture.kind).contents.find((entry) => entry.field === fixture.field)!.update_mode, 'snapshot')
    assert.equal(reasons(emptyFinal).includes(fixture.reason), false)
  }
})
