import {
  scrubObservationValue,
  type RhObservationContentHandle,
  type RhObservationDetails,
  type RhObservationIdentity,
  type RhObservationKind,
} from '../../codex-remote-core/dist/index.js'

export type CoreObservationIdentity = Omit<RhObservationIdentity, 'mission_id' | 'epoch_id'>

export interface CoreObservationScope {
  rootThreadId: string | null
  parentThreadId: string | null
  depth: number | null
  /** Opaque, unique receipt identity supplied by the owning Core. */
  receiptId: string
  receivedAt: string
  /** Core-authenticated ownership facts; communication never establishes these. */
  collaborationPromptField?: 'assignment' | 'message'
  nativeAgentMessage?: {
    message: DecodedNativeAgentMessage
    senderThreadId: string
    receiverThreadId: string
  }
}

export function classifyCollaborationPrompt(
  tool: unknown,
  senderId: string | null | undefined,
  receiverParentIds: readonly (string | null | undefined)[],
): 'assignment' | 'message' {
  if (tool === 'spawnAgent') return 'assignment'
  return tool === 'sendInput' && Boolean(senderId) && receiverParentIds.length > 0 &&
    receiverParentIds.every((parentId) => parentId === senderId) ? 'assignment' : 'message'
}

export interface DecodedNativeAgentMessage {
  kind: 'MESSAGE' | 'FINAL_ANSWER'
  author: string
  recipient: string
  content: string
}

/** Decode only the established plaintext envelope; Core must resolve its participants. */
export function decodeNativeAgentMessage(value: unknown): DecodedNativeAgentMessage | null {
  const item = object(value)
  if (item.type !== 'agent_message' || !identifier(item.author) || !identifier(item.recipient) ||
    !Array.isArray(item.content) || item.content.length === 0 ||
    item.content.some((part) => object(part).type !== 'input_text' || typeof object(part).text !== 'string')) return null
  const envelope = item.content.map((part) => object(part).text).join('\n')
  const decoded = /^Message Type: (MESSAGE|FINAL_ANSWER)\nTask name: ([^\n]+)\nSender: ([^\n]+)\nPayload:\n([\s\S]*)$/.exec(envelope)
  if (!decoded || decoded[2] !== item.recipient || decoded[3] !== item.author || !decoded[4]) return null
  return { kind: decoded[1] as DecodedNativeAgentMessage['kind'], author: item.author as string,
    recipient: item.recipient as string, content: decoded[4] }
}

/** Internal content only. The Host must sanitize streaming text before publication. */
export interface CoreObservationContent {
  content_key: string
  field: RhObservationContentHandle['field']
  text: string
  update_mode: 'append' | 'snapshot'
  complete: boolean
  /** Documented structured data already passed the shared value sanitizer. */
  sanitized?: true
}

export type CoreObservationDraft = {
  [K in RhObservationKind]: {
    kind: K
    details: RhObservationDetails[K]
    summary: string
    identity: CoreObservationIdentity
    source_method: string
    source_time: string | null
    received_at: string
    contents: CoreObservationContent[]
  }
}[RhObservationKind]

type NativeObject = Record<string, unknown>
const object = (value: unknown): NativeObject => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as NativeObject : {}
const string = (value: unknown): string | null => typeof value === 'string' ? value : null
const identifier = (value: unknown): string | null => typeof value === 'string' && value.length > 0 ? value : null
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : []
const number = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null
const status = (value: unknown): string => string(value) ?? 'unknown'

interface NativeTextContent {
  parts: { part: string; text: string }[]
  unavailable: number
}

/** Exact 0.153.4 function-output text variants, shared by live and historical readers. */
export function nativeFunctionOutputContent(value: unknown): NativeTextContent {
  if (typeof value === 'string') return { parts: [{ part: '', text: value }], unavailable: 0 }
  const result: NativeTextContent = { parts: [], unavailable: 0 }
  if (!Array.isArray(value)) return { ...result, unavailable: 1 }
  for (let index = 0; index < value.length; index++) {
    const entry = object(value[index])
    if (entry.type === 'input_text' && typeof entry.text === 'string') {
      result.parts.push({ part: `content:${index}`, text: entry.text })
    } else {
      // Media and encrypted bytes remain outside the text projection.
      result.unavailable++
    }
  }
  return result
}

/** Assistant question titles/options are visible content; unrelated item fields are never read. */
export function nativeAgentQuestionContent(value: unknown): NativeTextContent {
  const result: NativeTextContent = { parts: [], unavailable: 0 }
  if (value == null) return result
  if (!Array.isArray(value)) return { ...result, unavailable: 1 }
  for (let index = 0; index < value.length; index++) {
    const question = object(value[index])
    if (typeof question.title === 'string') result.parts.push({ part: `question:${index}:title`, text: question.title })
    else result.unavailable++
    if (question.options == null) continue
    if (!Array.isArray(question.options)) { result.unavailable++; continue }
    for (let option = 0; option < question.options.length; option++) {
      const text = question.options[option]
      if (typeof text === 'string') result.parts.push({ part: `question:${index}:option:${option}`, text })
      else result.unavailable++
    }
  }
  return result
}

/** Exact supported envelope fields only; an item sender is not its outer thread. */
export function nativeObservationThreadId(message: unknown): string | null {
  const envelope = object(message)
  const params = object(envelope.params)
  return identifier(params.threadId)
    ?? (envelope.method === 'thread/started' ? identifier(object(params.thread).id) : null)
}

function timestamp(value: unknown, seconds = false): string | null {
  const numeric = number(value)
  if (numeric === null) return null
  const time = new Date(seconds ? numeric * 1000 : numeric)
  return Number.isFinite(time.getTime()) ? time.toISOString() : null
}

/**
 * Projects supported public notifications at the existing owner seam.
 * It has no I/O, subscription, state, native-history read or owner effect.
 * Scope membership is established by the caller before any content is published.
 */
export function projectNativeObservation(message: unknown, scope: CoreObservationScope): CoreObservationDraft[] {
  const envelope = object(message)
  const method = string(envelope.method)
  if (method === null) return []
  const params = object(envelope.params)
  const item = object(params.item)
  const turn = object(params.turn)
  const thread = object(params.thread)
  const nativeItemId = identifier(params.itemId) ?? identifier(item.id)
  const identity: CoreObservationIdentity = {
    root_thread_id: scope.rootThreadId,
    thread_id: nativeObservationThreadId(message),
    turn_id: identifier(params.turnId) ?? identifier(turn.id),
    item_id: nativeItemId,
    // callId belongs to the request; the schema does not assert it equals item.id.
    operation_id: method === 'item/tool/call' ? identifier(params.callId) : nativeItemId,
    parent_thread_id: method === 'thread/started' && Object.hasOwn(thread, 'parentThreadId')
      ? identifier(thread.parentThreadId) : scope.parentThreadId,
  }
  const sourceTime = method === 'item/started' ? timestamp(params.startedAtMs)
    : method === 'item/completed' ? timestamp(params.completedAtMs)
      : method === 'turn/started' ? timestamp(turn.startedAt, true)
        : method === 'turn/completed' ? timestamp(turn.completedAt, true) : null
  const observations: CoreObservationDraft[] = []
  function emit<K extends RhObservationKind>(kind: K, summary: string, details: RhObservationDetails[K], contents: CoreObservationContent[] = [], override: Partial<CoreObservationIdentity> = {}): void {
    observations.push({ kind, details, summary, identity: { ...identity, ...override }, source_method: method!,
      source_time: sourceTime, received_at: scope.receivedAt, contents } as CoreObservationDraft)
  }
  function coverage(reason: string, count = 1): void {
    emit('coverage', 'Native observation coverage limitation', { reason, count })
  }
  function contentKey(field: CoreObservationContent['field'], part: string, turnLevel = false): string {
    const stable = identity.thread_id !== null && identity.turn_id !== null && (identity.item_id !== null || turnLevel)
    return JSON.stringify([stable ? 'native' : 'receipt', stable ? identity.thread_id : scope.receiptId,
      stable ? identity.turn_id : null, stable ? identity.item_id : null, field, part])
  }
  function content(field: CoreObservationContent['field'], value: unknown, complete: boolean, part = '', updateMode: 'append' | 'snapshot' = 'snapshot', turnLevel = false): CoreObservationContent[] {
    const text = string(value)
    if (text === null) {
      coverage('native_content_field_unavailable')
      return []
    }
    return [{ content_key: contentKey(field, part, turnLevel), field, text, update_mode: updateMode, complete }]
  }
  function structured(field: 'arguments' | 'result', value: unknown, complete: boolean, part = ''): CoreObservationContent[] {
    if (value === undefined) {
      coverage('native_content_field_unavailable')
      return []
    }
    // Only explicitly selected, documented tool arguments/results reach this path.
    const scrubbed = scrubObservationValue(value)
    if (scrubbed.redactionCount > 0) coverage('structured_content_redacted', scrubbed.redactionCount)
    return content(field, JSON.stringify(scrubbed.value), complete, part).map((entry) => ({ ...entry, sanitized: true }))
  }
  function visibleError(value: unknown, code = 'native_error', part = 'error', turnError = true): void {
    const error = object(value)
    const text = string(error.message)
    const detail = turnError ? string(error.additionalDetails) : null
    const nativeInfo = turnError ? error.codexErrorInfo : null
    const nativeCodes = ['contextWindowExceeded', 'sessionBudgetExceeded', 'usageLimitExceeded', 'serverOverloaded',
      'cyberPolicy', 'misalignmentPolicyViolation', 'internalServerError', 'unauthorized', 'badRequest',
      'threadRollbackFailed', 'sandboxError', 'other']
    const detailedCodes = ['httpConnectionFailed', 'responseStreamConnectionFailed', 'responseStreamDisconnected',
      'responseTooManyFailedAttempts', 'activeTurnNotSteerable']
    const detailedCode = detailedCodes.find((candidate) => Object.hasOwn(object(nativeInfo), candidate))
    const nativeCode = typeof nativeInfo === 'string' && nativeCodes.includes(nativeInfo) ? nativeInfo : detailedCode
    const httpStatus = detailedCode ? number(object(object(nativeInfo)[detailedCode]).httpStatusCode) : null
    emit('error', 'Native execution error', { code: nativeCode ?? code, ...(httpStatus !== null ? { actual: httpStatus } : {}) }, [
      ...(text !== null ? content('error', text, true, part) : []),
      ...(detail !== null ? content('error', detail, true, `${part}:details`) : []),
    ])
    if (text === null) coverage('native_error_message_unavailable')
  }
  function fileChanges(changes: unknown, complete: boolean, state: string): void {
    if (!Array.isArray(changes)) { coverage('native_file_changes_unavailable'); return }
    if (changes.length === 0) emit('file', 'File patch observation', { status: state })
    const occurrences = new Map<string, number>()
    for (let index = 0; index < changes.length; index++) {
      const change = object(changes[index])
      const path = string(change.path)
      const kind = object(change.kind)
      const occurrence = path === null ? index : occurrences.get(path) ?? 0
      if (path !== null) occurrences.set(path, occurrence + 1)
      const changeKind = ['add', 'delete', 'update'].includes(status(kind.type)) ? status(kind.type) : 'unknown'
      emit('file', 'File patch observation', { ...(path !== null ? { path } : {}), change_kind: changeKind, status: state },
        content('diff', change.diff, complete, JSON.stringify([path, occurrence])))
      if (path === null || changeKind === 'unknown') coverage('native_file_change_metadata_unavailable')
      if (typeof kind.move_path === 'string') emit('artifact', 'File move destination', { path: kind.move_path })
    }
  }
  function toolContentEntries(value: unknown, complete: boolean, dynamic: boolean): CoreObservationContent[] {
    if (!Array.isArray(value)) { coverage('native_tool_result_unavailable'); return [] }
    const output: CoreObservationContent[] = []
    for (let index = 0; index < value.length; index++) {
      const entry = object(value[index])
      if (entry.type === (dynamic ? 'inputText' : 'text')) {
        output.push(...content('result', entry.text, complete, `content:${index}`))
      } else if (!dynamic && entry.type === 'resource' && typeof object(entry.resource).text === 'string') {
        output.push(...content('result', object(entry.resource).text, complete, `resource:${index}`))
      } else {
        // Opaque MCP content and encoded media are not a generic JSON transcript.
        coverage('native_tool_content_variant_unavailable')
      }
    }
    return output
  }

  if (method === 'item/agentMessage/delta' || method === 'item/plan/delta') {
    emit('message', method === 'item/plan/delta' ? 'Visible plan update' : 'Assistant message update',
      { role: 'assistant', phase: null }, content('message', params.delta, false, '', 'append'))
  } else if (method === 'item/commandExecution/outputDelta') {
    emit('command', 'Command output update', { output_mode: 'streaming' }, content('output', params.delta, false, '', 'append'))
  } else if (method === 'item/commandExecution/terminalInteraction') {
    emit('command', 'Command terminal input', { process_id: identifier(params.processId) }, content('arguments', params.stdin, true, `stdin:${scope.receiptId}`))
  } else if (method === 'item/fileChange/patchUpdated') {
    fileChanges(params.changes, false, 'patchUpdated')
  } else if (method === 'item/fileChange/outputDelta') {
    coverage('native_file_change_output_delta_not_emitted_in_pinned_api')
  } else if (method === 'item/mcpToolCall/progress') {
    // Progress is not tool result output and does not imply result streaming.
    emit('tool', 'MCP tool progress', { output_mode: 'completion_only' }, content('message', params.message, true, `progress:${scope.receiptId}`))
  } else if (method === 'item/tool/call') {
    emit('tool', 'Dynamic tool request received', { tool_name: status(params.tool), server: string(params.namespace), status: 'requested', output_mode: 'completion_only' },
      structured('arguments', params.arguments, true))
  } else if (method === 'item/started' || method === 'item/completed') {
    const complete = method === 'item/completed'
    switch (item.type) {
      case 'userMessage': {
        const texts: string[] = []
        if (!Array.isArray(item.content)) coverage('native_message_content_unavailable')
        for (const entryValue of list(item.content)) {
          const entry = object(entryValue)
          if (entry.type === 'text' && typeof entry.text === 'string') texts.push(entry.text)
          else if (['skill', 'mention', 'localImage', 'localAudio'].includes(status(entry.type)) && typeof entry.path === 'string') {
            emit('artifact', 'Message resource reference', { path: entry.path })
          } else coverage('native_message_content_variant_unavailable')
        }
        emit('message', 'User message', { role: 'user', phase: null }, texts.length > 0 ? content('message', texts.join('\n'), complete) : [])
        break
      }
      case 'agentMessage': {
        const phase = item.phase === 'commentary' ? 'commentary' : item.phase === 'final_answer' ? 'final' : null
        const text = string(item.text)
        const messageContents = complete && text === null
          ? content('message', '', true, '', 'append') : content('message', item.text, complete)
        const questions = nativeAgentQuestionContent(item.questions)
        for (const part of questions.parts) messageContents.push(...content('message', part.text, complete, part.part))
        if (questions.unavailable > 0) coverage('native_agent_question_content_unavailable', questions.unavailable)
        emit('message', 'Assistant message', { role: 'assistant', phase }, messageContents)
        if (complete && text === null) coverage('native_agent_message_completion_text_unavailable')
        break
      }
      case 'plan': {
        const text = string(item.text)
        emit('message', 'Visible plan', { role: 'assistant', phase: null }, complete && text === null
          ? content('message', '', true, '', 'append') : content('message', item.text, complete))
        if (complete && text === null) coverage('native_plan_completion_text_unavailable')
        break
      }
      case 'hookPrompt':
        emit('message', 'Hook prompt', { role: 'system', phase: null }, list(item.fragments).flatMap((fragment, index) => content('message', object(fragment).text, complete, `hook:${index}`)))
        break
      case 'reasoning':
        coverage('native_reasoning_content_not_projected')
        break
      case 'commandExecution': {
        const output = string(item.aggregatedOutput)
        emit('command', 'Command execution', {
          ...(typeof item.cwd === 'string' ? { cwd: item.cwd } : {}), status: status(item.status),
          exit_code: number(item.exitCode), signal: null, duration_ms: number(item.durationMs),
          process_id: identifier(item.processId),
          // A snapshot cannot establish whether this operation also emitted deltas.
          // In particular, completion must not downgrade previously observed streaming.
          output_mode: 'unknown',
        }, [...content('command', item.command, true), ...(output !== null ? content('output', output, complete)
          // Flush a held sanitizer tail without replacing earlier partial output.
          : complete ? content('output', '', true, '', 'append') : [])])
        if (complete && output === null) coverage('native_command_completion_output_unavailable')
        break
      }
      case 'fileChange':
        fileChanges(item.changes, complete, status(item.status))
        break
      case 'dynamicToolCall': {
        const contents = structured('arguments', item.arguments, true)
        if (Array.isArray(item.contentItems)) contents.push(...toolContentEntries(item.contentItems, complete, true))
        else if (complete) coverage('native_tool_result_unavailable')
        emit('tool', 'Dynamic tool execution', { tool_name: status(item.tool), server: string(item.namespace), status: status(item.status), success: typeof item.success === 'boolean' ? item.success : null, duration_ms: number(item.durationMs), output_mode: 'completion_only' }, contents)
        break
      }
      case 'functionCallOutput': {
        const output = nativeFunctionOutputContent(item.output)
        emit('tool', 'Function call output', { tool_name: status(item.name), server: string(item.namespace),
          output_mode: 'completion_only' },
        output.parts.flatMap((part) => content('result', part.text, complete, part.part)))
        if (output.unavailable > 0) coverage('native_function_output_content_unavailable', output.unavailable)
        break
      }
      case 'mcpToolCall': {
        const contents = structured('arguments', item.arguments, true)
        const result = object(item.result)
        if (Array.isArray(result.content)) contents.push(...toolContentEntries(result.content, complete, false))
        else if (complete && item.error == null) coverage('native_tool_result_unavailable')
        if (Object.hasOwn(result, 'structuredContent')) contents.push(...structured('result', result.structuredContent, complete, 'structuredContent'))
        emit('tool', 'MCP tool execution', { tool_name: status(item.tool), server: string(item.server), status: status(item.status), duration_ms: number(item.durationMs), output_mode: 'completion_only' }, contents)
        if (item.error != null) visibleError(item.error, 'native_mcp_tool_error', 'error', false)
        break
      }
      case 'collabAgentToolCall': {
        const receivers = list(item.receiverThreadIds).filter((value): value is string => typeof value === 'string')
        const field = item.tool === 'spawnAgent' ? 'assignment' : scope.collaborationPromptField ?? 'message'
        const contents = typeof item.prompt === 'string' ? content(field, item.prompt, true) : []
        emit('work', 'Worker collaboration operation', { action: status(item.tool), status: status(item.status),
          sender_thread_id: identifier(item.senderThreadId), receiver_thread_ids: receivers,
          child_thread_id: null, requested_model: string(item.model), requested_reasoning_effort: string(item.reasoningEffort),
          ...(scope.depth !== null ? { depth: scope.depth } : {}) }, contents)
        // Target status is a report in the sender's operation, never registration proof.
        for (const [receiver, stateValue] of Object.entries(object(item.agentsStates))) {
          const state = object(stateValue)
          emit('work', 'Reported worker state', { action: 'reported_state', status: status(state.status),
            sender_thread_id: identifier(item.senderThreadId), receiver_thread_ids: [receiver] },
          typeof state.message === 'string' ? content('message', state.message, complete, `agentState:${receiver}`) : [])
        }
        break
      }
      case 'subAgentActivity':
        emit('work', 'Subagent activity', { action: status(item.kind), receiver_thread_ids: identifier(item.agentThreadId) === null ? [] : [item.agentThreadId as string],
          ...(typeof item.agentPath === 'string' ? { agent_path: item.agentPath } : {}) })
        break
      case 'webSearch':
        emit('tool', 'Web search', { tool_name: 'webSearch', output_mode: 'completion_only' }, [
          ...content('arguments', item.query, true),
          ...(item.results != null ? structured('result', item.results, complete) : []),
        ])
        break
      case 'imageView':
        emit('artifact', 'Image reference', typeof item.path === 'string' ? { path: item.path } : {})
        if (typeof item.path !== 'string') coverage('native_artifact_path_unavailable')
        break
      case 'imageGeneration':
        emit('tool', 'Image generation', { tool_name: 'imageGeneration', status: status(item.status), output_mode: 'completion_only' }, typeof item.revisedPrompt === 'string' ? content('arguments', item.revisedPrompt, true) : [])
        if (typeof item.savedPath === 'string') emit('artifact', 'Generated image reference', { path: item.savedPath })
        else coverage('native_generated_image_artifact_unavailable')
        break
      case 'sleep':
        emit('lifecycle', 'Worker sleep activity', { stage: complete ? 'sleep_completed' : 'sleep_started' })
        break
      case 'enteredReviewMode':
      case 'exitedReviewMode':
        emit('message', 'Review message', { role: 'assistant', phase: null }, content('message', item.review, complete))
        break
      case 'contextCompaction':
        emit('lifecycle', 'Context compaction', { stage: complete ? 'compaction_completed' : 'compaction_started' })
        break
      default:
        coverage('unsupported_native_item_type')
    }
  } else if (method === 'rawResponseItem/completed') {
    const resolved = scope.nativeAgentMessage
    if (resolved) {
      emit('work', 'Native worker message', { action: resolved.message.kind === 'FINAL_ANSWER' ? 'final_answer' : 'message',
        sender_thread_id: resolved.senderThreadId, receiver_thread_ids: [resolved.receiverThreadId] },
      content('message', resolved.message.content, true))
    } else {
      coverage(item.type === 'agent_message' ? 'native_agent_message_unresolved_or_unsupported' : 'unsupported_native_raw_item')
    }
  } else if (method === 'turn/started' || method === 'turn/completed') {
    emit('lifecycle', 'Turn lifecycle', { state: status(turn.status), stage: method === 'turn/started' ? 'turn_started' : 'turn_completed' })
    if (turn.error != null) visibleError(turn.error, 'native_turn_error')
    // Items are projected at their own receipt boundary, avoiding a second transcript here.
  } else if (method === 'thread/started') {
    const subAgent = object(object(thread.source).subAgent)
    const spawn = object(subAgent.thread_spawn)
    emit('work', 'Native thread observed', { action: 'thread_started', status: status(object(thread.status).type),
      child_thread_id: identity.parent_thread_id !== null ? identity.thread_id : null,
      ...(Object.hasOwn(subAgent, 'thread_spawn') ? {
        source_parent_thread_id: identifier(spawn.parent_thread_id), source_depth: number(spawn.depth),
        ...(typeof spawn.agent_path === 'string' ? { agent_path: spawn.agent_path } : {}),
      } : {}),
      ...(scope.depth !== null ? { depth: scope.depth } : {}) })
  } else if (method === 'thread/status/changed') {
    const state = object(params.status)
    emit('lifecycle', 'Thread status', { state: status(state.type), stage: 'thread_status' })
    for (const flag of list(state.activeFlags)) {
      if (flag === 'waitingOnApproval' || flag === 'waitingOnUserInput') emit('lifecycle', 'Thread waiting state', { state: flag, stage: 'thread_active_flag' })
    }
  } else if (method === 'thread/goal/updated') {
    const goal = object(params.goal)
    emit('lifecycle', 'Goal status', { state: status(goal.status), stage: 'goal_updated' })
  } else if (['thread/closed', 'thread/archived', 'thread/unarchived', 'thread/deleted', 'thread/goal/cleared', 'thread/compacted'].includes(method)) {
    emit('lifecycle', 'Thread lifecycle', { stage: method })
  } else if (method === 'turn/diff/updated') {
    emit('file', 'Turn aggregate diff', { status: 'updated' }, content('diff', params.diff, false, 'turn', 'snapshot', true))
  } else if (method === 'turn/plan/updated') {
    const steps = list(params.plan).map((value) => {
      const step = object(value)
      return { step: string(step.step), status: string(step.status) }
    })
    emit('message', 'Turn plan update', { role: 'assistant', phase: null }, [
      ...content('message', JSON.stringify(steps), false, 'turnPlan', 'snapshot', true),
      ...(typeof params.explanation === 'string' ? content('message', params.explanation, false, 'turnPlanExplanation', 'snapshot', true) : []),
    ])
  } else if (method === 'error') {
    visibleError(params.error, 'native_turn_error')
    if (typeof params.willRetry === 'boolean') emit('lifecycle', 'Native error retry disposition', { stage: 'native_error', state: params.willRetry ? 'retry_pending' : 'no_retry' })
  } else if (method === 'warning') {
    emit('error', 'Native warning', { code: 'native_warning' }, content('error', params.message, true))
  } else if (method.startsWith('item/reasoning/')) {
    coverage('native_reasoning_content_not_projected')
  } else {
    coverage('unsupported_native_notification')
  }
  if (identity.thread_id === null) coverage('native_thread_identity_unavailable')
  if (observations.some((entry) => entry.contents.length > 0) && identity.item_id === null && !['turn/diff/updated', 'turn/plan/updated'].includes(method)) {
    coverage('native_item_identity_unavailable_receipt_scoped_content')
  }
  return observations
}
