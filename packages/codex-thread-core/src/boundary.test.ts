import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test, { after } from 'node:test'

import {
  CodexAppServerBoundary,
  GoalEpochCancelledError,
  GoalEpochMissionConsistencyError,
  GoalEpochSharedAuthorityLossError,
  GoalEpochStartEffectUnknownError,
  goalConfigArgs,
  type BoundedGoalEpochRequest,
  type CodexAppServerBoundaryOptions,
  type CodexExecutionObservation,
  type DynamicToolCallResult,
  type GoalEpochCapabilitySelections,
  type GoalEpochDiagnosticEvent,
  type GoalEpochDescendantToolGrantRevocation,
  type GoalEpochNativeMaterialObservation,
  type GoalEpochOperationContext,
  type GoalEpochStopContext,
  type GoalProcessConfig,
  type ThreadGoal,
  type ThreadGoalStatus,
} from './boundary.js'

const FIXTURE_ROOT = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'codex-boundary-')))
const WORKSPACE = path.join(FIXTURE_ROOT, 'workspace')
const READ_ROOT = path.join(FIXTURE_ROOT, 'read-only')
const APP_SERVER_CWD = path.join(FIXTURE_ROOT, 'app-server')
const PROTECTED_ROOT = path.join(FIXTURE_ROOT, 'codex-home')
const OUTSIDE_READ_ROOT = path.join(FIXTURE_ROOT, 'outside-read-only')
const SELECTED_CAPABILITY_ROOT = path.join(READ_ROOT, 'src')
for (const directory of [WORKSPACE, SELECTED_CAPABILITY_ROOT, APP_SERVER_CWD, PROTECTED_ROOT, OUTSIDE_READ_ROOT]) {
  fs.mkdirSync(directory, { recursive: true })
}
after(() => fs.rmSync(FIXTURE_ROOT, { recursive: true, force: true }))
const MODEL_CATALOG_PATH = path.join(READ_ROOT, 'codex-model-catalog.0.144.1.json')
const ROOT_THREAD_ID = 'root-thread'
const ROOT_TURN_ID = 'root-turn'

function observationRecorder(events: CodexExecutionObservation[]) {
  return (event: CodexExecutionObservation) => {
    events.push(event)
    return { incarnation: 'test-source', sequence: events.length }
  }
}

const GOAL_POLICY: GoalProcessConfig = {
  permissionProfileId: 'rh-mission-test',
  expectedCliVersion: '0.144.1',
  expectedModelProvider: 'openai',
  expectedModel: 'gpt-5.6-sol',
  reasoningEffort: 'ultra',
  modelCatalogPath: MODEL_CATALOG_PATH,
  appServerCwd: APP_SERVER_CWD,
  protectedRoot: PROTECTED_ROOT,
  readOnlyRoots: [READ_ROOT],
}

const DYNAMIC_TOOL = {
  type: 'function' as const,
  name: 'preserve_effect',
  description: 'Preserve one bounded effect.',
  inputSchema: {
    type: 'object',
    properties: { value: { type: 'string' } },
    required: ['value'],
    additionalProperties: false,
  },
}

const HISTORY_TOOL_NAME = 'rh_mission_history'
const HISTORY_PAGE_TOOL_NAME = 'rh_mission_history_page'
const HISTORY_GRANT_TOOL_NAME = 'rh_mission_history_grant'
const A1_REVIEW_TOOL_NAME = 'rh_mission_a1_review'
const A1_REVIEW_PAGE_TOOL_NAME = 'rh_mission_a1_review_page'
const A1_REVIEW_GRANT_TOOL_NAME = 'rh_mission_a1_review_grant'
const RESEARCH_READ_TOOL_NAME = 'rh_mission_research_read'
const RESEARCH_READ_PAGE_TOOL_NAME = 'rh_mission_research_read_page'
const RESEARCH_READ_GRANT_TOOL_NAME = 'rh_mission_research_read_grant'
const ADMISSION_TOOL_NAME = 'rh_mission_admission'
const ADMISSION_PAGE_TOOL_NAME = 'rh_mission_admission_page'
const ADMISSION_GRANT_TOOL_NAME = 'rh_mission_admission_grant'
const ADMISSION_OPEN_TOOL_NAME = 'rh_mission_admission_open'
const ROOT_MISSION_TOOL_NAME = 'rh_mission'
const FORMAL_TOOL_NAME = 'rh_formal_attempt'

function stringValueTool(name: string) {
  return {
    type: 'function' as const,
    name,
    description: `Exercise ${name}.`,
    inputSchema: {
      type: 'object',
      properties: { value: { type: 'string' } },
      required: ['value'],
      additionalProperties: false,
    },
  }
}

const HISTORY_DYNAMIC_TOOLS = [
  stringValueTool(HISTORY_TOOL_NAME),
  stringValueTool(HISTORY_PAGE_TOOL_NAME),
  stringValueTool(HISTORY_GRANT_TOOL_NAME),
  stringValueTool(ROOT_MISSION_TOOL_NAME),
  stringValueTool(FORMAL_TOOL_NAME),
  DYNAMIC_TOOL,
]

const DESCENDANT_DYNAMIC_TOOLS = [
  ...HISTORY_DYNAMIC_TOOLS,
  stringValueTool(A1_REVIEW_TOOL_NAME),
  stringValueTool(A1_REVIEW_PAGE_TOOL_NAME),
  stringValueTool(A1_REVIEW_GRANT_TOOL_NAME),
  stringValueTool(RESEARCH_READ_TOOL_NAME),
  stringValueTool(RESEARCH_READ_PAGE_TOOL_NAME),
  stringValueTool(RESEARCH_READ_GRANT_TOOL_NAME),
  stringValueTool(ADMISSION_TOOL_NAME),
  stringValueTool(ADMISSION_PAGE_TOOL_NAME),
  stringValueTool(ADMISSION_GRANT_TOOL_NAME),
  stringValueTool(ADMISSION_OPEN_TOOL_NAME),
]

type RpcRecord = Readonly<{
  method: string
  params: Record<string, unknown>
}>

type ServerResponse = Readonly<{
  id: string | number
  result: DynamicToolCallResult
}>

type RpcBlocker = Readonly<{
  promise: Promise<void>
  resolve: () => void
  reject: (error: Error) => void
}>

function goal(
  threadId: string,
  status: ThreadGoalStatus = 'active',
  overrides: Partial<ThreadGoal> = {},
): ThreadGoal {
  return {
    threadId,
    objective: 'Advance one bounded mathematical research epoch.',
    status,
    tokensUsed: 0,
    timeUsedSeconds: 0,
    createdAt: 1,
    updatedAt: 1,
    ...overrides,
  }
}

function request(overrides: Partial<BoundedGoalEpochRequest> = {}): BoundedGoalEpochRequest {
  return {
    objective: 'Advance one bounded mathematical research epoch.',
    developerInstructions: 'Do the bounded work and preserve useful output.',
    dynamicTools: [],
    ...overrides,
  }
}

function selectedCapabilities(
  overrides: Partial<GoalEpochCapabilitySelections> = {},
): GoalEpochCapabilitySelections {
  return {
    localRoots: [],
    mcpServers: [],
    apps: [],
    browser: null,
    ...overrides,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function paramsOf(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {}
}

async function waitUntil(predicate: () => boolean, description: string): Promise<void> {
  const deadline = Date.now() + 2_000
  while (!predicate()) {
    if (Date.now() >= deadline) {
      throw new Error('timed out waiting for ' + description)
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 5))
  }
}

function createVersionCanaryCodexCli(
  root: string,
  userAgent: string,
  stderrText = '',
): string {
  const serverPath = path.join(root, 'version-canary-app-server.mjs')
  const source = [
    "import readline from 'node:readline'",
    `const userAgent = ${JSON.stringify(userAgent)}`,
    ...(stderrText ? [`process.stderr.write(${JSON.stringify(stderrText)})`] : []),
    "const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })",
    "lines.on('line', (line) => {",
    '  const request = JSON.parse(line)',
    "  if (request.method !== 'initialize' && request.method !== 'config/read') return",
    '  const response = {',
    '    jsonrpc: \'2.0\',',
    '    id: request.id,',
    "    result: request.method === 'initialize'",
    '      ? {',
    '          userAgent,',
    '          codexHome: process.cwd(),',
    `          platformFamily: ${JSON.stringify(process.platform)},`,
    `          platformOs: ${JSON.stringify(process.platform)},`,
    '        }',
    '      : { config: {} },',
    '  }',
    "  process.stdout.write(JSON.stringify(response) + '\\n')",
    '})',
    '',
  ].join('\n')
  fs.writeFileSync(serverPath, source)

  if (process.platform === 'win32') {
    const launcherPath = path.join(root, 'version-canary-codex.bat')
    fs.writeFileSync(
      launcherPath,
      `@echo off\r\n"${process.execPath}" "${serverPath}" %*\r\n`,
    )
    return launcherPath
  }

  const launcherPath = path.join(root, 'version-canary-codex')
  const shellQuote = (value: string): string => `'${value.replaceAll("'", "'\\''")}'`
  fs.writeFileSync(
    launcherPath,
    `#!/bin/sh\nexec ${shellQuote(process.execPath)} ${shellQuote(serverPath)} "$@"\n`,
    { mode: 0o755 },
  )
  return launcherPath
}

async function captureProcessOutput(
  stream: NodeJS.WriteStream,
  action: () => Promise<void>,
): Promise<string> {
  const output: string[] = []
  const originalWrite = stream.write
  stream.write = ((chunk: unknown, ...args: unknown[]) => {
    const text = Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk)
    if (text.includes('"system":"workstation_control"')) {
      output.push(text)
      const callback = args.find((item) => typeof item === 'function')
      if (typeof callback === 'function') {
        callback()
      }
      return true
    }
    return (originalWrite as (...values: unknown[]) => boolean).call(stream, chunk, ...args)
  }) as typeof stream.write
  try {
    await action()
    await new Promise<void>((resolve) => setImmediate(resolve))
  } finally {
    stream.write = originalWrite
  }
  return output.join('')
}

class FakeGoalBoundary extends CodexAppServerBoundary {
  readonly requests: RpcRecord[] = []
  readonly responses: ServerResponse[] = []
  readonly goals = new Map<string, ThreadGoal>()
  readonly childTree = new Map<string, string[]>()
  readonly activeTurns = new Map<string, string>()
  readonly failedMethods = new Set<string>()
  private readonly blockedMethods = new Map<string, RpcBlocker>()
  startResultOverrides: Record<string, unknown> = {}
  threadListPageOverride: ((params: Record<string, unknown>) => unknown) | null = null
  suppressAutomaticTurnStart = false
  afterServerResponse:
    ((requestId: string | number, result: DynamicToolCallResult) => void) | null = null

  constructor(options: CodexAppServerBoundaryOptions = {}) {
    super('codex', WORKSPACE, {
      restartOnUnexpectedExit: false,
      goalPolicy: GOAL_POLICY,
      onGoalTermination: async () => undefined,
      onMissionFence: async () => undefined,
      ...options,
    })
  }

  protected override async ensureReady(): Promise<void> {
    return
  }

  blockMethod(method: string): Pick<RpcBlocker, 'resolve' | 'reject'> {
    if (this.blockedMethods.has(method)) {
      throw new Error(method + ' is already blocked')
    }
    let resolvePromise!: () => void
    let rejectPromise!: (error: Error) => void
    const promise = new Promise<void>((resolve, reject) => {
      resolvePromise = resolve
      rejectPromise = reject
    })
    const blocker: RpcBlocker = {
      promise,
      resolve: () => {
        this.blockedMethods.delete(method)
        resolvePromise()
      },
      reject: (error) => {
        this.blockedMethods.delete(method)
        rejectPromise(error)
      },
    }
    this.blockedMethods.set(method, blocker)
    return blocker
  }

  private async waitForMethod(method: string, signal?: AbortSignal): Promise<void> {
    const blocker = this.blockedMethods.get(method)
    if (!blocker) {
      return
    }
    if (!signal) {
      await blocker.promise
      return
    }
    if (signal.aborted) {
      throw new GoalEpochCancelledError('test App Server request', signal.reason)
    }
    await new Promise<void>((resolve, reject) => {
      const abort = (): void => reject(
        new GoalEpochCancelledError('test App Server request', signal.reason),
      )
      signal.addEventListener('abort', abort, { once: true })
      void blocker.promise.then(resolve, reject).finally(() => {
        signal.removeEventListener('abort', abort)
      })
    })
  }

  protected override respondToServerRequest(
    requestId: string | number,
    result: DynamicToolCallResult,
  ): void {
    this.responses.push({ id: requestId, result })
    this.afterServerResponse?.(requestId, result)
  }

  protected override async request(
    method: string,
    input: unknown,
    signal?: AbortSignal,
  ): Promise<any> {
    const params = paramsOf(input)
    this.requests.push({ method, params })
    if (this.failedMethods.has(method)) {
      throw new Error(method + ' failed by test')
    }
    await this.waitForMethod(method, signal)
    if (method === 'thread/start') {
      return {
        result: {
          thread: { id: ROOT_THREAD_ID, status: { type: 'idle' } },
          modelProvider: 'openai',
          model: 'gpt-5.6-sol',
          reasoningEffort: 'ultra',
          cwd: WORKSPACE,
          activePermissionProfile: { id: 'rh-mission-test' },
          sandbox: { networkAccess: true },
          ...this.startResultOverrides,
        },
      }
    }
    if (method === 'thread/goal/set') {
      const threadId = String(params.threadId)
      const existing = this.goals.get(threadId)
      const next =
        typeof params.objective === 'string'
          ? goal(threadId, 'active', {
              objective: params.objective,
            })
          : {
              ...(existing ?? goal(threadId)),
              status: params.status as ThreadGoalStatus,
              updatedAt: (existing?.updatedAt ?? 1) + 1,
            }
      this.goals.set(threadId, next)
      if (
        params.status === 'active' &&
        !this.suppressAutomaticTurnStart
      ) {
        queueMicrotask(() => {
          void this.deliver({
            method: 'turn/started',
            params: { threadId, turn: { id: ROOT_TURN_ID } },
          })
        })
      }
      return { result: { goal: next } }
    }
    if (method === 'thread/goal/get') {
      return { result: { goal: this.goals.get(String(params.threadId)) ?? null } }
    }
    if (method === 'thread/goal/clear') {
      this.goals.delete(String(params.threadId))
      return { result: { cleared: true } }
    }
    if (method === 'thread/list') {
      if (this.threadListPageOverride) {
        return { result: this.threadListPageOverride(params) }
      }
      const parentThreadId = String(params.parentThreadId ?? '')
      const rows = this.childTree.get(parentThreadId) ?? []
      let offset = 0
      if (params.cursor !== undefined) {
        const match = typeof params.cursor === 'string' ? /^offset:(\d+)$/.exec(params.cursor) : null
        if (!match) {
          throw new Error('invalid fake thread/list cursor')
        }
        offset = Number(match[1])
      }
      const limit = Number(params.limit ?? 100)
      const end = Math.min(offset + limit, rows.length)
      return {
        result: {
          data: rows.slice(offset, end).map((id) => ({
            id,
            parentThreadId,
          })),
          nextCursor: end < rows.length ? 'offset:' + end : null,
        },
      }
    }
    if (method === 'thread/read') {
      const threadId = String(params.threadId)
      const activeTurnId = this.activeTurns.get(threadId)
      return {
        result: {
          thread: {
            id: threadId,
            status: { type: activeTurnId ? 'active' : 'idle' },
            turns: activeTurnId ? [{ id: activeTurnId, status: 'active' }] : [],
          },
        },
      }
    }
    if (method === 'thread/resume') {
      return {
        result: {
          thread: {
            id: String(params.threadId),
            parentThreadId: null,
            ephemeral: false,
            status: { type: 'idle' },
          },
          modelProvider: 'openai',
          model: 'gpt-5.6-sol',
          reasoningEffort: 'ultra',
          cwd: WORKSPACE,
          runtimeWorkspaceRoots: [WORKSPACE],
          approvalPolicy: 'never',
          approvalsReviewer: 'user',
          activePermissionProfile: { id: 'rh-mission-test' },
          sandbox: { networkAccess: true },
          ...this.startResultOverrides,
        },
      }
    }
    if (
      method === 'turn/interrupt' ||
      method === 'thread/inject_items' ||
      method === 'thread/archive'
    ) {
      return { result: {} }
    }
    if (method === 'config/read') {
      return {
        result: {
          config: {
            model: 'gpt-5.6-sol',
            model_provider: 'openai',
            model_reasoning_effort: 'ultra',
          },
        },
      }
    }
    throw new Error('unexpected RPC method: ' + method)
  }

  async deliver(message: unknown): Promise<void> {
    await this.enqueueRuntimeMessage(message)
  }

  setGoalStatus(
    threadId: string,
    status: ThreadGoalStatus,
    overrides: Partial<ThreadGoal> = {},
  ): ThreadGoal {
    const next = {
      ...(this.goals.get(threadId) ?? goal(threadId)),
      status,
      updatedAt: (this.goals.get(threadId)?.updatedAt ?? 1) + 1,
      ...overrides,
    }
    this.goals.set(threadId, next)
    return next
  }
}

class FakeAppServerProcess extends EventEmitter {
  readonly writes: string[] = []
  readonly killSignals: (NodeJS.Signals | number | undefined)[] = []
  exitCode: number | null = null
  signalCode: NodeJS.Signals | null = null
  killed = false
  killResult = true
  readonly stdin = {
    write: (chunk: string): boolean => {
      this.writes.push(String(chunk))
      return true
    },
  }

  kill(signal?: NodeJS.Signals | number): boolean {
    this.killed = this.killResult
    this.killSignals.push(signal)
    return this.killResult
  }

  exit(code: number | null = 0): void {
    this.exitCode = code
    this.emit('exit', code)
  }

  close(code: number | null = this.exitCode): void {
    this.emit('close', code)
  }
}

class RpcLifecycleBoundary extends CodexAppServerBoundary {
  constructor(readonly fakeProcess: FakeAppServerProcess, options: CodexAppServerBoundaryOptions = {}) {
    super('codex', WORKSPACE, options)
    const internals = this as unknown as {
      child: FakeAppServerProcess
      childClosed: boolean
      markProcessExited: (code: number | null) => void
      onProcessClose: (code: number | null) => Promise<void>
    }
    internals.child = fakeProcess
    internals.childClosed = false
    fakeProcess.once('exit', (code: number | null) => internals.markProcessExited(code))
    fakeProcess.once('close', (code: number | null) => {
      void internals.onProcessClose(code)
    })
  }

  markReadyForGoalRequest(): void {
    const internals = this as unknown as {
      ready: boolean
      readyReason: string | null
    }
    internals.ready = true
    internals.readyReason = null
  }

  rpc(method: string, params: unknown, signal?: AbortSignal): Promise<any> {
    return this.request(method, params, signal, 'test lifecycle RPC')
  }

  deliverResponse(id: string, result: unknown): void {
    this.dispatchDecodedMessage({ jsonrpc: '2.0', id, result })
  }

  deliverError(id: string, error: unknown): void {
    this.dispatchDecodedMessage({ jsonrpc: '2.0', id, error })
  }
}

function spawnItem(
  childThreadId: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    method: 'item/completed',
    params: {
      threadId: ROOT_THREAD_ID,
      turnId: ROOT_TURN_ID,
      completedAtMs: 1_800_000_000_000,
      item: {
        type: 'collabAgentToolCall',
        id: 'spawn-call-' + childThreadId,
        tool: 'spawnAgent',
        status: 'completed',
        agentsStates: {},
        model: null,
        reasoningEffort: null,
        senderThreadId: ROOT_THREAD_ID,
        receiverThreadIds: [childThreadId],
        prompt: 'Investigate the assigned mathematical route.',
        ...overrides,
      },
    },
  }
}

function nativeChildStarted(
  childThreadId: string,
  agentRole: string | null,
  parentThreadId = ROOT_THREAD_ID,
): Record<string, unknown> {
  return {
    method: 'thread/started',
    params: {
      thread: {
        id: childThreadId,
        agentRole,
        source: { subAgent: { thread_spawn: { parent_thread_id: parentThreadId, agent_role: agentRole } } },
      },
    },
  }
}

function nestedSpawnItem(parentThreadId: string, childThreadId: string): Record<string, unknown> {
  return {
    method: 'item/completed',
    params: {
      threadId: parentThreadId,
      turnId: `turn.${parentThreadId}`,
      item: {
        type: 'collabAgentToolCall',
        id: 'nested-spawn-call-' + childThreadId,
        tool: 'spawnAgent',
        status: 'completed',
        agentsStates: {},
        model: null,
        reasoningEffort: null,
        senderThreadId: parentThreadId,
        receiverThreadIds: [childThreadId],
        prompt: 'Investigate one helper route for the parent research branch.',
      },
    },
  }
}

function waitItem(
  childThreadId: string,
  message: string,
  parentThreadId = ROOT_THREAD_ID,
): Record<string, unknown> {
  return {
    method: 'item/completed',
    params: {
      threadId: parentThreadId,
      turnId: parentThreadId === ROOT_THREAD_ID ? ROOT_TURN_ID : `turn.${parentThreadId}`,
      completedAtMs: 1_800_000_000_100,
      item: {
        type: 'collabAgentToolCall',
        id: 'wait-call-' + childThreadId,
        tool: 'wait',
        status: 'completed',
        prompt: null,
        model: null,
        reasoningEffort: null,
        senderThreadId: parentThreadId,
        receiverThreadIds: [childThreadId],
        agentsStates: {
          [childThreadId]: { status: 'completed', message },
        },
      },
    },
  }
}

function sendInputItem(
  childThreadId: string,
  prompt: string,
  parentThreadId = ROOT_THREAD_ID,
): Record<string, unknown> {
  return {
    method: 'item/completed',
    params: {
      threadId: parentThreadId,
      turnId: parentThreadId === ROOT_THREAD_ID ? ROOT_TURN_ID : `turn.${parentThreadId}`,
      completedAtMs: 1_800_000_000_100,
      item: {
        type: 'collabAgentToolCall',
        id: 'send-input-call-' + childThreadId,
        tool: 'sendInput',
        status: 'completed',
        agentsStates: {},
        model: null,
        reasoningEffort: null,
        senderThreadId: parentThreadId,
        receiverThreadIds: [childThreadId],
        prompt,
      },
    },
  }
}

function subAgentActivity(
  childThreadId: string,
  agentPath: string,
  parentThreadId = ROOT_THREAD_ID,
): Record<string, unknown> {
  return {
    method: 'item/completed',
    params: {
      threadId: parentThreadId,
      turnId: ROOT_TURN_ID,
      item: {
        type: 'subAgentActivity',
        id: 'activity-' + childThreadId,
        kind: 'started',
        agentThreadId: childThreadId,
        agentPath,
      },
    },
  }
}

function rawAgentMessage(
  author: string,
  recipient: string,
  content: readonly Record<string, unknown>[],
  threadId = ROOT_THREAD_ID,
): Record<string, unknown> {
  return {
    method: 'rawResponseItem/completed',
    params: {
      threadId,
      turnId: ROOT_TURN_ID,
      item: {
        type: 'agent_message',
        id: 'agent-message-' + author + '-' + recipient,
        author,
        recipient,
        content,
      },
    },
  }
}

function dynamicToolRequest(
  requestId: string,
  threadId: string,
  callId: string,
  value: string,
  tool = DYNAMIC_TOOL.name,
  turnId = threadId === ROOT_THREAD_ID ? ROOT_TURN_ID : 'child-turn',
): Record<string, unknown> {
  return {
    id: requestId,
    method: 'item/tool/call',
    params: {
      threadId,
      turnId,
      callId,
      tool,
      namespace: null,
      arguments: { value },
    },
  }
}

test('goalConfigArgs binds Astra root and native child defaults to the same ultra policy', () => {
  const args = goalConfigArgs({ ...GOAL_POLICY, expectedModel: 'gpt-6-astra' }, WORKSPACE)
  assert.ok(args.includes('model="gpt-6-astra"'))
  assert.ok(args.includes('model_reasoning_effort="ultra"'))
  assert.ok(args.includes('agents.default_subagent_model="gpt-6-astra"'))
  assert.ok(args.includes('agents.default_subagent_reasoning_effort="ultra"'))
  assert.equal(args.some((value) => value.includes('gpt-5.6-sol')), false)
})

test('goalConfigArgs selects launch preferences and enforces process containment', () => {
  const args = goalConfigArgs(GOAL_POLICY, WORKSPACE)
  const joined = args.join('\n')

  assert.deepEqual(args, goalConfigArgs({ ...GOAL_POLICY, networkAccess: true }, WORKSPACE))
  assert.equal(args[0], '--strict-config')
  assert.match(joined, /default_permissions="rh-mission-test"/)
  assert.match(joined, /model="gpt-5\.6-sol"/)
  assert.match(joined, /model_provider="openai"/)
  assert.match(joined, /model_reasoning_effort="ultra"/)
  assert.equal(
    joined.includes('model_catalog_json=' + JSON.stringify(MODEL_CATALOG_PATH)),
    true,
  )
  assert.match(joined, /network = \{ enabled = true, mode = "limited"/)
  assert.match(joined, /allow_local_binding = false/)
  assert.match(joined, /allow_upstream_proxy = false/)
  assert.match(joined, /dangerously_allow_non_loopback_proxy = false/)
  assert.match(joined, /dangerously_allow_all_unix_sockets = false/)
  assert.match(joined, /domains = \{ "\*" = "allow" \}/)
  const protectedDeny = JSON.stringify(PROTECTED_ROOT.replaceAll('\\', '/')) + ' = "deny"'
  const workspaceRegistration = JSON.stringify(WORKSPACE.replaceAll('\\', '/')) + ' = true'
  const readRootRegistration = JSON.stringify(READ_ROOT.replaceAll('\\', '/')) + ' = true'
  const workspaceParentRead =
    JSON.stringify(path.dirname(WORKSPACE).replaceAll('\\', '/')) + ' = "read"'
  const authorizedRead = JSON.stringify(READ_ROOT.replaceAll('\\', '/')) + ' = "read"'
  assert.equal(joined.includes(protectedDeny), true)
  assert.equal(joined.includes('workspace_roots = { ' + workspaceRegistration + ' }'), true)
  assert.equal(joined.includes(readRootRegistration), false)
  assert.match(joined, /":workspace_roots" = \{ "\." = "write" \}/)
  assert.equal(joined.includes(workspaceParentRead), false)
  assert.equal(joined.includes(authorizedRead), true)
  assert.match(joined, /extends = ":workspace"/)
  assert.doesNotMatch(joined, /":(?:tmpdir|slash_tmp)" = "deny"/)
  assert.equal(joined.includes(path.join(WORKSPACE, '.codex').replaceAll('\\', '/')), false)
  assert.equal(joined.includes(path.join(WORKSPACE, '.agents').replaceAll('\\', '/')), false)
  assert.match(joined, /skills\.bundled\.enabled=false/)
  assert.match(joined, /features\.network_proxy\.enabled=true/)
  assert.match(joined, /shell_environment_policy\.inherit="none"/)
  assert.match(joined, /shell_environment_policy\.set\.PATH="\/usr\/bin:\/bin"/)
  assert.match(joined, /shell_environment_policy\.ignore_default_excludes=false/)
  assert.match(joined, /project_root_markers=\[\]/)
  assert.match(joined, /web_search="live"/)
  assert.match(joined, /orchestrator\.skills\.enabled=true/)
  assert.match(joined, /features\.multi_agent=true/)
  assert.match(joined, /features\.multi_agent_v2\.enabled=false/)
  assert.match(joined, /agents\.max_depth=2/)
  assert.match(joined, /agents\.max_threads=9223372036854775807/)
  assert.doesNotMatch(joined, /features\.multi_agent_v2\.hide_spawn_agent_metadata/)
  assert.doesNotMatch(joined, /features\.multi_agent_v2\.max_concurrent_threads_per_session/)
  assert.doesNotMatch(joined, /features\.multi_agent_v2\.(?:root|subagent)_agent_usage_hint_text/)
  assert.doesNotMatch(joined, /features\.multi_agent_v2=true/)
  assert.doesNotMatch(joined, /available concurrency slots/)
  assert.doesNotMatch(
    joined,
    /features\.(?:code_mode|code_mode_only|shell_tool|unified_exec)=false/,
  )
  assert.match(joined, /features\.shell_tool=true/)
  assert.doesNotMatch(joined, /features\.apps=/)
  assert.doesNotMatch(joined, /features\.browser_use=/)
  assert.doesNotMatch(joined, /features\.browser_use_external=/)
  assert.match(joined, /features\.browser_use_full_cdp_access=false/)
  assert.doesNotMatch(joined, /features\.computer_use=/)
  assert.doesNotMatch(joined, /features\.enable_mcp_apps=/)
  assert.match(joined, /features\.in_app_browser=false/)
  assert.match(joined, /features\.plugin_sharing=false/)
  assert.match(joined, /features\.plugins=false/)
  assert.match(joined, /features\.remote_plugin=false/)
  assert.match(joined, /features\.remote_models=false/)
  assert.match(joined, /features\.rollout_budget=false/)
  assert.match(joined, /features\.skill_mcp_dependency_install=false/)
  assert.match(joined, /features\.token_budget=false/)
  assert.doesNotMatch(joined, /features\.(?:code_mode|code_mode_only|unified_exec)=/)
  assert.doesNotMatch(joined, /mcp_servers\./)
  assert.doesNotMatch(joined, /orchestrator\.mcp\.enabled=/)
  assert.match(joined, /features\.goals=true/)
  assert.doesNotMatch(joined, /windows\.sandbox/)
  assert.doesNotMatch(joined, /attestation|sessionId|nickname/)
})

test('offline goalConfigArgs disables network and provider-hosted or ambient capabilities', () => {
  const joined = goalConfigArgs({ ...GOAL_POLICY, networkAccess: false }, WORKSPACE).join('\n')
  assert.match(joined, /network = \{ enabled = false \}/)
  assert.doesNotMatch(joined, /domains =|network = \{ enabled = true/)
  assert.match(joined, /web_search="disabled"/)
  assert.match(joined, /features\.network_proxy\.enabled=false/)
  assert.match(joined, /orchestrator\.skills\.enabled=false/)
  assert.match(joined, /orchestrator\.mcp\.enabled=false/)
  for (const name of [
    'apps', 'browser_use', 'browser_use_external', 'computer_use', 'enable_mcp_apps',
    'hooks', 'image_generation', 'memories', 'multi_agent', 'plugins', 'remote_plugin',
    'standalone_web_search',
  ]) {
    assert.ok(joined.includes('features.' + name + '=false'), name)
  }
  assert.match(joined, /features\.goals=true/)
  assert.match(joined, /features\.shell_tool=true/)
  assert.ok(joined.includes(JSON.stringify(READ_ROOT.replaceAll('\\', '/')) + ' = "read"'))
  assert.ok(joined.includes(JSON.stringify(PROTECTED_ROOT.replaceAll('\\', '/')) + ' = "deny"'))
  assert.match(joined, /":workspace_roots" = \{ "\." = "write" \}/)
  assert.throws(
    () => goalConfigArgs({ ...GOAL_POLICY, networkAccess: 'false' as unknown as boolean }, WORKSPACE),
    /goalPolicy.networkAccess must be a boolean/,
  )
})

test('offline Goal start and resume retain local tools and reject network readback drift', async () => {
  const boundary = new FakeGoalBoundary({ goalPolicy: { ...GOAL_POLICY, networkAccess: false } })
  boundary.startResultOverrides = { sandbox: { networkAccess: false } }
  await boundary.startBoundedGoalEpoch(request())
  const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
  })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  await boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request())

  for (const method of ['thread/start', 'thread/resume']) {
    const call = boundary.requests.find((entry) => entry.method === method)!
    const config = call.params.config as Record<string, unknown>
    assert.equal(config.web_search, 'disabled')
    assert.equal(config['features.shell_tool'], true)
    assert.equal(config['features.goals'], true)
    for (const name of [
      'apps', 'browser_use', 'browser_use_external', 'computer_use', 'enable_mcp_apps',
      'hooks', 'image_generation', 'memories', 'multi_agent', 'plugins', 'remote_plugin',
      'standalone_web_search',
    ]) {
      assert.equal(config['features.' + name], false, method + ': ' + name)
    }
    assert.equal(config['orchestrator.skills.enabled'], false)
    assert.equal(config['orchestrator.mcp.enabled'], false)
    assert.deepEqual(call.params.runtimeWorkspaceRoots, [WORKSPACE])
    assert.equal(call.params.permissions, GOAL_POLICY.permissionProfileId)
  }
  assert.deepEqual(boundary.requests.find(({ method }) => method === 'thread/start')!.params.dynamicTools, [])
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'active')
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('Goal start requires exact requested network access before creating its Goal', async () => {
  for (const networkAccess of [true, false]) {
    for (const reported of [!networkAccess, undefined, String(networkAccess)]) {
      const boundary = new FakeGoalBoundary({ goalPolicy: { ...GOAL_POLICY, networkAccess } })
      boundary.startResultOverrides = { sandbox: { networkAccess: reported } }
      await assert.rejects(boundary.startBoundedGoalEpoch(request()), /thread\/start did not preserve root Goal containment/)
      assert.equal(boundary.requests.some(({ method }) => method === 'thread/goal/set'), false)
      assert.equal(boundary.requests.some(({ method }) => method === 'thread/archive'), true)
    }
  }
})

test('Goal resume requires exact requested network access before reactivating its Goal', async () => {
  for (const networkAccess of [true, false]) {
    const boundary = new FakeGoalBoundary({ goalPolicy: { ...GOAL_POLICY, networkAccess } })
    boundary.startResultOverrides = { sandbox: { networkAccess } }
    await boundary.startBoundedGoalEpoch(request())
    const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
    await boundary.deliver({
      method: 'thread/goal/updated',
      params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
    })
    await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
    const setsBeforeResume = boundary.requests.filter(({ method }) => method === 'thread/goal/set').length
    boundary.startResultOverrides = { sandbox: { networkAccess: !networkAccess } }
    await assert.rejects(boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request()), /thread\/resume did not preserve root Goal containment/)
    assert.equal(boundary.requests.filter(({ method }) => method === 'thread/goal/set').length, setsBeforeResume)
    assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'usageLimited')
    await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  }
})

test('offline Goal rejects every selected capability family before model work', async () => {
  const selections = [
    ...(['skill', 'plugin'] as const).map((kind) => selectedCapabilities({
      localRoots: [{
        kind,
        id: 'selected-local-root',
        location: { type: 'environment', environmentId: 'local', path: SELECTED_CAPABILITY_ROOT },
      }],
    })),
    selectedCapabilities({ mcpServers: [{ id: 'trusted-mcp', enabledTools: ['read'] }] }),
    selectedCapabilities({ apps: [{ id: 'trusted-app', enabledTools: ['read'] }] }),
    selectedCapabilities({ browser: { mode: 'isolated_ephemeral_unauthenticated' } }),
  ]
  for (const selection of selections) {
    const boundary = new FakeGoalBoundary({
      goalPolicy: { ...GOAL_POLICY, networkAccess: false },
      trustedMcpServerIds: ['trusted-mcp'],
      trustedAppIds: ['trusted-app'],
    })
    await assert.rejects(boundary.startBoundedGoalEpoch(request({ selectedCapabilities: selection })), /offline bounded Goal requires empty selectedCapabilities/)
    await assert.rejects(boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request({ selectedCapabilities: selection })), /offline bounded Goal requires empty selectedCapabilities/)
    assert.equal(boundary.requests.length, 0)
  }
})

test('goalConfigArgs rejects a protected root that overlaps any readable or writable root', () => {
  const cases: GoalProcessConfig[] = [
    { ...GOAL_POLICY, protectedRoot: path.join(WORKSPACE, 'auth') },
    { ...GOAL_POLICY, protectedRoot: path.dirname(WORKSPACE) },
    { ...GOAL_POLICY, protectedRoot: path.join(READ_ROOT, 'auth') },
    {
      ...GOAL_POLICY,
      protectedRoot: PROTECTED_ROOT,
      readOnlyRoots: [path.join(PROTECTED_ROOT, 'public')],
    },
  ]

  for (const policy of cases) {
    assert.throws(
      () => goalConfigArgs(policy, WORKSPACE),
      /protectedRoot must not overlap any readable or writable Goal root/,
    )
  }
  assert.throws(
    () => goalConfigArgs({ ...GOAL_POLICY, protectedRoot: 'relative/codex-home' }, WORKSPACE),
    /protectedRoot must be an absolute path/,
  )
  assert.throws(
    () => goalConfigArgs({ ...GOAL_POLICY, expectedCliVersion: 'latest' }, WORKSPACE),
    /expectedCliVersion must be one exact stable Codex version/,
  )
  assert.throws(
    () => goalConfigArgs({ ...GOAL_POLICY, appServerCwd: WORKSPACE }, WORKSPACE),
    /appServerCwd must be separate/,
  )
  assert.throws(
    () => goalConfigArgs({ ...GOAL_POLICY, modelCatalogPath: 'relative/catalog.json' }, WORKSPACE),
    /modelCatalogPath must be an absolute path/,
  )
  assert.throws(
    () =>
      goalConfigArgs(
        { ...GOAL_POLICY, modelCatalogPath: path.join(path.dirname(READ_ROOT), 'catalog.json') },
        WORKSPACE,
      ),
    /modelCatalogPath must stay inside one read-only Goal root/,
  )
})

test('boundary rejects ambient Goal metadata before App Server spawn', async (t) => {
  for (const metadataEntry of ['.agents', '.codex']) {
    for (const kind of ['populated', 'file', 'symlink', 'dangling symlink', 'unreadable']) {
      await t.test(metadataEntry + ' ' + kind, async (t) => {
        const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-goal-metadata-boundary-'))
        t.after(() => fs.rmSync(root, { recursive: true, force: true }))
        const workspaceRoot = path.join(root, 'workspace')
        const readOnlyRoot = path.join(root, 'release')
        const appServerCwd = path.join(root, 'runtime')
        const protectedRoot = path.join(root, 'codex-home')
        for (const directory of [workspaceRoot, readOnlyRoot, appServerCwd, protectedRoot]) {
          fs.mkdirSync(directory)
        }
        const metadataPath = path.join(workspaceRoot, metadataEntry)
        if (kind === 'file') {
          fs.writeFileSync(metadataPath, 'ambient material')
        } else if (kind === 'symlink' || kind === 'dangling symlink') {
          const target = path.join(root, 'metadata-link-target')
          if (kind === 'symlink') fs.mkdirSync(target)
          fs.symlinkSync(target, metadataPath, process.platform === 'win32' ? 'junction' : 'dir')
        } else {
          fs.mkdirSync(metadataPath)
          if (kind === 'populated') {
            fs.writeFileSync(path.join(metadataPath, 'config.toml'), 'model = "ambient-model"\n')
          } else {
            // A deterministic filesystem denial also covers Windows, where
            // chmod cannot faithfully make this directory unreadable.
            const readdirSync = fs.readdirSync
            t.mock.method(fs, 'readdirSync', (entry: fs.PathLike, ...options: unknown[]) => {
              if (entry === metadataPath) {
                throw Object.assign(new Error('fixture permission denied'), { code: 'EACCES' })
              }
              return Reflect.apply(readdirSync, fs, [entry, ...options])
            })
          }
        }

        const boundary = new CodexAppServerBoundary(
          path.join(root, 'must-not-spawn-codex'),
          workspaceRoot,
          {
            restartOnUnexpectedExit: false,
            goalPolicy: {
              ...GOAL_POLICY,
              modelCatalogPath: path.join(readOnlyRoot, 'catalog.json'),
              appServerCwd,
              protectedRoot,
              readOnlyRoots: [readOnlyRoot],
            },
          },
        )

        await assert.rejects(
          () => boundary.start(),
          /Goal workspace root must not contain ambient Codex configuration, plugin, or skill material/,
        )
      })
    }
  }
})

test('boundary initializes with empty reserved metadata directories and preserves them', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-goal-metadata-boundary-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const workspaceRoot = path.join(root, 'workspace')
  const readOnlyRoot = path.join(root, 'release')
  const appServerCwd = path.join(root, 'runtime')
  const protectedRoot = path.join(root, 'codex-home')
  for (const directory of [workspaceRoot, readOnlyRoot, appServerCwd, protectedRoot]) {
    fs.mkdirSync(directory)
  }
  const metadataPaths = [workspaceRoot, appServerCwd].flatMap((directory) =>
    ['.codex', '.agents'].map((name) => path.join(directory, name)),
  )
  for (const directory of metadataPaths) fs.mkdirSync(directory)
  const originalMetadata = metadataPaths.map((directory) => fs.lstatSync(directory))
  const cliPath = createVersionCanaryCodexCli(
    root,
    'workstation-control-bridge/0.144.1 (test; x86_64)',
  )
  const boundary = new CodexAppServerBoundary(cliPath, workspaceRoot, {
    restartOnUnexpectedExit: false,
    goalPolicy: {
      ...GOAL_POLICY,
      modelCatalogPath: path.join(readOnlyRoot, 'catalog.json'),
      appServerCwd,
      protectedRoot,
      readOnlyRoots: [readOnlyRoot],
    },
  })

  try {
    await boundary.start()
    assert.equal(boundary.isReady(), true)
    for (const [index, directory] of metadataPaths.entries()) {
      const stat = fs.lstatSync(directory)
      assert.equal(stat.isDirectory(), true)
      assert.equal(stat.isSymbolicLink(), false)
      assert.equal(stat.ino, originalMetadata[index].ino)
      assert.deepEqual(fs.readdirSync(directory), [])
    }
  } finally {
    await boundary.stop()
  }
})

test('boundary rejects the actual App Server when initialize reports a different version', async (t) => {
  for (const userAgent of [
    'workstation-control-bridge/0.143.0 (test; x86_64) dumb (workstation-control-bridge; 0.0.0)',
    'workstation-control-bridge/not-a-version (test; x86_64)',
  ]) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-version-boundary-'))
    t.after(() => fs.rmSync(root, { recursive: true, force: true }))
    const workspaceRoot = path.join(root, 'workspace')
    const readOnlyRoot = path.join(root, 'release')
    const appServerCwd = path.join(root, 'runtime')
    const protectedRoot = path.join(root, 'codex-home')
    for (const directory of [workspaceRoot, readOnlyRoot, appServerCwd, protectedRoot]) {
      fs.mkdirSync(directory)
    }
    for (const name of ['.codex', '.agents']) fs.mkdirSync(path.join(workspaceRoot, name))
    const cliPath = createVersionCanaryCodexCli(root, userAgent)
    const boundary = new CodexAppServerBoundary(cliPath, workspaceRoot, {
      restartOnUnexpectedExit: false,
      goalPolicy: {
        ...GOAL_POLICY,
        modelCatalogPath: path.join(readOnlyRoot, 'catalog.json'),
        appServerCwd,
        protectedRoot,
        readOnlyRoots: [readOnlyRoot],
      },
    })
    await assert.rejects(
      () => boundary.start(),
      /Codex App Server must be the pinned 0\.144\.1 runtime/,
    )
  }
})

test('boundary accepts the pinned version from CLI and Desktop App Server user agents', async (t) => {
  for (const userAgent of [
    'workstation-control-bridge/0.144.1 (test; x86_64) dumb (workstation-control-bridge; 0.0.0)',
    'Codex Desktop/0.144.1 (test; x86_64) dumb (workstation-control-bridge; 0.0.0)',
  ]) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-version-boundary-'))
    t.after(() => fs.rmSync(root, { recursive: true, force: true }))
    const workspaceRoot = path.join(root, 'workspace')
    const readOnlyRoot = path.join(root, 'release')
    const appServerCwd = path.join(root, 'runtime')
    const protectedRoot = path.join(root, 'codex-home')
    for (const directory of [workspaceRoot, readOnlyRoot, appServerCwd, protectedRoot]) {
      fs.mkdirSync(directory)
    }
    const cliPath = createVersionCanaryCodexCli(root, userAgent)
    const observations: CodexExecutionObservation[] = []
    const boundary = new CodexAppServerBoundary(cliPath, workspaceRoot, {
      onExecutionObservation: observationRecorder(observations),
      restartOnUnexpectedExit: false,
      goalPolicy: {
        ...GOAL_POLICY,
        modelCatalogPath: path.join(readOnlyRoot, 'catalog.json'),
        appServerCwd,
        protectedRoot,
        readOnlyRoots: [readOnlyRoot],
      },
    })

    await boundary.start()
    assert.equal(boundary.isReady(), true)
    assert.deepEqual(observations.filter((event) => event.source_facts).map((event) => event.source_facts), [
      { codex_version: '0.144.1' },
    ])
    await boundary.stop()
  }
})

test('App Server stderr logs retain content-neutral diagnostics without stderr content', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-stderr-log-boundary-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const workspaceRoot = path.join(root, 'workspace')
  const readOnlyRoot = path.join(root, 'release')
  const appServerCwd = path.join(root, 'runtime')
  const protectedRoot = path.join(root, 'codex-home')
  for (const directory of [workspaceRoot, readOnlyRoot, appServerCwd, protectedRoot]) {
    fs.mkdirSync(directory)
  }
  const secretStderr = 'SECRET_TOKEN proof: zeta_zero_claim'
  const cliPath = createVersionCanaryCodexCli(
    root,
    'workstation-control-bridge/0.144.1 (test; x86_64)',
    secretStderr,
  )
  const boundary = new CodexAppServerBoundary(cliPath, workspaceRoot, {
    restartOnUnexpectedExit: false,
    goalPolicy: {
      ...GOAL_POLICY,
      modelCatalogPath: path.join(readOnlyRoot, 'catalog.json'),
      appServerCwd,
      protectedRoot,
      readOnlyRoots: [readOnlyRoot],
    },
  })

  const output = await captureProcessOutput(process.stdout, async () => {
    await boundary.start()
    await boundary.stop()
  })
  const event = output
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line) as Record<string, unknown>)
    .find((item) => item.event === 'bridge.boundary.stderr')

  assert.ok(event)
  assert.doesNotMatch(output, /SECRET_TOKEN|zeta_zero_claim/)
  assert.equal(event.stderr_present, true)
  assert.equal(event.stderr_character_count, Array.from(secretStderr).length)
  assert.equal(event.stderr_byte_count, Buffer.byteLength(secretStderr, 'utf8'))
  assert.equal(
    event.stderr_sha256,
    createHash('sha256').update(secretStderr, 'utf8').digest('hex'),
  )
  assert.equal('message' in event, false)
})

test('runtime material message failures stay local and later material still delivers', async () => {
  const secretMessage = 'SECRET_TOKEN proof: hidden_counterexample'
  const observedChildren: string[] = []
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (observation) => {
      if (observation.childThreadId === 'failing-observation') {
        throw new Error(secretMessage)
      }
      observedChildren.push(observation.childThreadId)
    },
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  const output = await captureProcessOutput(process.stderr, async () => {
    await assert.rejects(
      () => boundary.deliver(spawnItem('failing-observation')),
      { message: secretMessage },
    )
    await boundary.deliver(spawnItem('delivered-after-failure'))
  })
  const event = output
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line) as Record<string, unknown>)
    .find((item) => item.event === 'bridge.boundary.message_failed')

  assert.ok(event)
  assert.doesNotMatch(output, /SECRET_TOKEN|hidden_counterexample/)
  assert.equal(event.error_class, 'Error')
  assert.equal('message' in event, false)
  await boundary.waitForFatalBoundaryFence()
  assert.deepEqual(observedChildren, ['delivered-after-failure'])
  assert.deepEqual(stopContexts, [])
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'active')

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('confirmed shared material custody loss retains its Mission-wide fence', async () => {
  const fenceContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async () => {
      throw new GoalEpochSharedAuthorityLossError('shared Store/CAS integrity loss')
    },
    onMissionFence: async (context) => {
      fenceContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  await assert.rejects(
    boundary.deliver(spawnItem('shared-integrity-failure')),
    { name: 'GoalEpochSharedAuthorityLossError' },
  )
  await boundary.waitForFatalBoundaryFence()

  assert.equal(fenceContexts[0]?.reason, 'shared_authority_loss')
})

test('start launches one bounded Goal with pinned root policy and simple state', async () => {
  const identities: unknown[] = []
  const boundary = new FakeGoalBoundary({
    onGoalEpochIdentityMaterialized: async (identity) => {
      identities.push(identity)
    },
  })

  const handle = await boundary.startBoundedGoalEpoch(request())
  const startCall = boundary.requests.find((entry) => entry.method === 'thread/start')
  assert.ok(startCall)
  assert.deepEqual(handle, { threadId: ROOT_THREAD_ID })
  assert.deepEqual(identities, [
    { threadId: ROOT_THREAD_ID, workspaceRoot: WORKSPACE },
  ])

  assert.equal(startCall.params.model, 'gpt-5.6-sol')
  assert.equal(startCall.params.modelProvider, 'openai')
  assert.equal(startCall.params.permissions, 'rh-mission-test')
  assert.equal(startCall.params.allowProviderModelFallback, false)
  assert.deepEqual(startCall.params.runtimeWorkspaceRoots, [WORKSPACE])
  const startConfig = startCall.params.config as Record<string, unknown>
  assert.equal(startConfig['features.multi_agent'], true)
  assert.equal(startConfig['features.multi_agent_v2.enabled'], false)
  assert.equal(startConfig['agents.max_depth'], 2)
  assert.equal('agents.max_threads' in startConfig, false)
  assert.equal('features.multi_agent_v2.max_concurrent_threads_per_session' in startConfig, false)
  assert.equal('features.multi_agent_v2.hide_spawn_agent_metadata' in startConfig, false)
  assert.equal('features.multi_agent_v2.root_agent_usage_hint_text' in startConfig, false)
  assert.equal('features.multi_agent_v2.subagent_usage_hint_text' in startConfig, false)
  assert.equal('features.code_mode' in startConfig, false)
  assert.equal('features.code_mode_only' in startConfig, false)
  assert.equal(startConfig['features.shell_tool'], true)
  assert.equal(startConfig['features.apps'], false)
  assert.equal(startConfig['features.browser_use'], false)
  assert.equal(startConfig['features.browser_use_external'], false)
  assert.equal(startConfig['features.browser_use_full_cdp_access'], false)
  assert.equal(startConfig['features.computer_use'], false)
  assert.equal(startConfig['features.enable_mcp_apps'], false)
  assert.equal(startConfig['features.in_app_browser'], false)
  assert.equal(startConfig['features.plugin_sharing'], false)
  assert.equal(startConfig['features.plugins'], false)
  assert.equal(startConfig['features.remote_plugin'], false)
  assert.equal(startConfig['features.remote_models'], false)
  assert.equal(startConfig['features.rollout_budget'], false)
  assert.equal(startConfig['features.skill_mcp_dependency_install'], false)
  assert.equal(startConfig['features.token_budget'], false)
  assert.equal('features.network_proxy' in startConfig, false)
  assert.equal('features.unified_exec' in startConfig, false)
  assert.equal(startConfig.web_search, 'live')
  assert.equal(startConfig['apps._default.enabled'], false)
  assert.equal(startConfig['orchestrator.skills.enabled'], false)
  assert.equal(startConfig['orchestrator.mcp.enabled'], false)
  assert.deepEqual(startCall.params.environments, [
    { environmentId: 'local', cwd: WORKSPACE },
  ])
  assert.deepEqual(startCall.params.selectedCapabilityRoots, [])
  assert.deepEqual(
    Object.fromEntries(
      Object.entries(startConfig).filter(([key, value]) => key.startsWith('features.') && value === false),
    ),
    {
      'features.apps': false,
      'features.browser_use': false,
      'features.browser_use_external': false,
      'features.browser_use_full_cdp_access': false,
      'features.computer_use': false,
      'features.enable_mcp_apps': false,
      'features.in_app_browser': false,
      'features.multi_agent_v2.enabled': false,
      'features.plugin_sharing': false,
      'features.plugins': false,
      'features.remote_plugin': false,
      'features.remote_models': false,
      'features.rollout_budget': false,
      'features.skill_mcp_dependency_install': false,
      'features.token_budget': false,
    },
  )
  assert.equal(
    Object.keys(startConfig).some((key) =>
      key.startsWith('mcp_servers.'),
    ),
    false,
  )
  assert.equal('nickname' in startCall.params, false)
  assert.equal('threadSource' in startCall.params, false)
  assert.equal('sessionId' in startCall.params, false)
  const goalSetCall = boundary.requests.find((entry) => entry.method === 'thread/goal/set')
  assert.ok(goalSetCall)
  assert.equal('tokenBudget' in goalSetCall.params, false)

  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(state.goal?.status, 'active')
  assert.equal(state.activeTurnId, ROOT_TURN_ID)
  assert.deepEqual(state.descendants, [])
  assert.deepEqual(state.nativeMaterialObservations, [])
  assert.equal('modelAttestation' in state, false)
  assert.equal('nativeDelegations' in state, false)

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('recovery discovers the one unarchived root bound to the exact Goal workspace across pages', async () => {
  const boundary = new FakeGoalBoundary()
  boundary.threadListPageOverride = (params) => {
    if (params.cursor === undefined) {
      return {
        data: [
          { id: 'thread.unrelated', cwd: path.dirname(WORKSPACE) },
          { id: 'thread.child', cwd: WORKSPACE, parentThreadId: 'thread.parent' },
        ],
        nextCursor: 'page.2',
      }
    }
    assert.equal(params.cursor, 'page.2')
    return {
      data: [{ id: ROOT_THREAD_ID, cwd: WORKSPACE, parentThreadId: null }],
      nextCursor: null,
    }
  }

  assert.deepEqual(
    await boundary.findMaterializedGoalEpoch(),
    { threadId: ROOT_THREAD_ID },
  )
  assert.equal(
    boundary.requests.filter(({ method }) => method === 'thread/list').length,
    2,
  )
  assert.equal(boundary.requests.some(({ method }) => method === 'thread/start'), false)
})

test('generic thread listing returns every App Server page without a repository count ceiling', async () => {
  const boundary = new FakeGoalBoundary()
  const ids = Array.from({ length: 205 }, (_value, index) => `thread.${index}`)
  boundary.childTree.set('', ids)

  const threads = await boundary.listThreads()

  assert.deepEqual(threads.map((thread) => thread.id), ids)
  assert.equal(
    boundary.requests.filter(({ method }) => method === 'thread/list').length,
    3,
  )
})

test('recovery refuses ambiguous materialized roots in one Goal workspace', async () => {
  const boundary = new FakeGoalBoundary()
  boundary.threadListPageOverride = () => ({
    data: [
      { id: 'thread.first', cwd: WORKSPACE, parentThreadId: null },
      { id: 'thread.second', cwd: WORKSPACE, parentThreadId: null },
    ],
    nextCursor: null,
  })

  await assert.rejects(
    boundary.findMaterializedGoalEpoch(),
    /more than one materialized root thread/,
  )
})

test('cancellation before thread/start creates no root thread', async () => {
  const boundary = new FakeGoalBoundary()
  const controller = new AbortController()
  controller.abort('operator_stop')

  await assert.rejects(
    boundary.startBoundedGoalEpoch(request(), controller.signal),
    GoalEpochCancelledError,
  )
  assert.equal(boundary.requests.length, 0)
})

test('cancellation drains a late thread/start reply and archives its exact root', async () => {
  const boundary = new FakeGoalBoundary()
  const blocker = boundary.blockMethod('thread/start')
  const controller = new AbortController()
  let settled = false
  const starting = boundary.startBoundedGoalEpoch(request(), controller.signal).finally(() => {
    settled = true
  })
  await waitUntil(
    () => boundary.requests.some(({ method }) => method === 'thread/start'),
    'blocked thread/start request',
  )

  controller.abort('operator_stop')
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(settled, false)
  assert.equal(boundary.requests.some(({ method }) => method === 'thread/archive'), false)

  blocker.resolve()
  await assert.rejects(starting, GoalEpochCancelledError)
  assert.deepEqual(
    boundary.requests
      .filter(({ method }) => method === 'thread/archive')
      .map(({ params }) => params.threadId),
    [ROOT_THREAD_ID],
  )
  assert.equal(boundary.requests.some(({ method }) => method === 'thread/goal/set'), false)
})

test('cancellation with no thread/start reply reports an unknown durable effect', async () => {
  const boundary = new FakeGoalBoundary()
  const blocker = boundary.blockMethod('thread/start')
  const controller = new AbortController()
  const starting = boundary.startBoundedGoalEpoch(request(), controller.signal)
  await waitUntil(
    () => boundary.requests.some(({ method }) => method === 'thread/start'),
    'blocked thread/start request',
  )

  controller.abort('operator_stop')
  blocker.reject(new Error('App Server closed before the reply'))

  await assert.rejects(starting, GoalEpochStartEffectUnknownError)
  assert.equal(boundary.requests.some(({ method }) => method === 'thread/archive'), false)
})

test('operator cancellation drains Goal activation before suspending the known root', async () => {
  const boundary = new FakeGoalBoundary()
  const blocker = boundary.blockMethod('thread/goal/set')
  const controller = new AbortController()
  const starting = boundary.startBoundedGoalEpoch(request(), controller.signal)
  await waitUntil(
    () => boundary.requests.some(({ method }) => method === 'thread/goal/set'),
    'blocked Goal activation',
  )

  controller.abort('operator_stop')
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(boundary.requests.some(({ method }) => method === 'thread/archive'), false)

  blocker.resolve()
  await assert.rejects(starting, GoalEpochCancelledError)
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'paused')
})

test('operator cancellation suspends an unstarted first turn after Goal activation', async () => {
  const boundary = new FakeGoalBoundary()
  boundary.suppressAutomaticTurnStart = true
  const controller = new AbortController()
  const starting = boundary.startBoundedGoalEpoch(request(), controller.signal)
  await waitUntil(
    () => boundary.goals.get(ROOT_THREAD_ID)?.status === 'active',
    'Goal activation before first turn',
  )

  controller.abort('operator_stop')

  await assert.rejects(starting, GoalEpochCancelledError)
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'paused')
})

test('Goal state reads propagate explicit cancellation instead of accepting stale fallback state', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const blocker = boundary.blockMethod('thread/read')
  const controller = new AbortController()
  const reading = boundary.readGoalEpochState(ROOT_THREAD_ID, controller.signal)
  await waitUntil(
    () => boundary.requests.filter(({ method }) => method === 'thread/read').length >= 1,
    'blocked Goal state read',
  )

  controller.abort('operator_stop')

  await assert.rejects(reading, GoalEpochCancelledError)
  blocker.resolve()
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('explicit environment capability roots are passed through without enabling ambient MCP', async () => {
  const boundary = new FakeGoalBoundary()
  const environments = [{ environmentId: 'local', cwd: WORKSPACE }]
  const localRoots = [
    {
      kind: 'skill' as const,
      id: 'selected-skill-root',
      location: {
        type: 'environment' as const,
        environmentId: 'local',
        path: SELECTED_CAPABILITY_ROOT,
      },
    },
  ]

  await boundary.startBoundedGoalEpoch(
    request({
      environments,
      selectedCapabilities: selectedCapabilities({ localRoots }),
    }),
  )
  const startCall = boundary.requests.find((entry) => entry.method === 'thread/start')
  assert.ok(startCall)
  assert.deepEqual(startCall.params.environments, environments)
  assert.deepEqual(startCall.params.selectedCapabilityRoots, [
    {
      id: 'selected-skill-root',
      location: localRoots[0]?.location,
    },
  ])
  const startConfig = startCall.params.config as Record<string, unknown>
  assert.equal(startConfig['orchestrator.skills.enabled'], true)
  assert.equal(startConfig['orchestrator.mcp.enabled'], false)

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('deliberate optional families expose only exact roots, ids, and tools', async () => {
  const boundary = new FakeGoalBoundary({
    trustedMcpServerIds: ['trusted.alpha', 'trusted.beta'],
    trustedAppIds: ['app-one', 'app-two'],
  })
  await boundary.startBoundedGoalEpoch(
    request({
      selectedCapabilities: selectedCapabilities({
        localRoots: [
          {
            kind: 'plugin',
            id: 'local-plugin',
            location: {
              type: 'environment',
              environmentId: 'local',
              path: SELECTED_CAPABILITY_ROOT,
            },
          },
        ],
        mcpServers: [
          { id: 'trusted.alpha', enabledTools: ['lookup/exact', 'check'] },
        ],
        apps: [
          { id: 'app-one', enabledTools: ['read/item'] },
        ],
        browser: { mode: 'isolated_ephemeral_unauthenticated' },
      }),
    }),
  )

  const startCall = boundary.requests.find((entry) => entry.method === 'thread/start')
  assert.ok(startCall)
  assert.equal(startCall.params.model, 'gpt-5.6-sol')
  assert.equal(startCall.params.modelProvider, 'openai')
  assert.deepEqual(startCall.params.selectedCapabilityRoots, [
    {
      id: 'local-plugin',
      location: {
        type: 'environment',
        environmentId: 'local',
        path: SELECTED_CAPABILITY_ROOT,
      },
    },
  ])
  const config = startCall.params.config as Record<string, unknown>
  assert.equal(config.web_search, 'live')
  assert.equal(config['features.apps'], true)
  assert.equal(config['features.browser_use'], true)
  assert.equal(config['features.browser_use_external'], true)
  assert.equal(config['features.browser_use_full_cdp_access'], false)
  assert.equal(config['features.computer_use'], true)
  assert.equal(config['features.enable_mcp_apps'], true)
  assert.equal(config['features.in_app_browser'], false)
  assert.equal(config['features.plugins'], true)
  assert.equal(config['features.plugin_sharing'], false)
  assert.equal(config['features.remote_plugin'], false)
  assert.equal(config['features.skill_mcp_dependency_install'], false)
  assert.equal(config['orchestrator.skills.enabled'], true)
  assert.equal(config['orchestrator.mcp.enabled'], true)
  assert.equal(config['apps._default.enabled'], false)
  assert.equal(config['apps."app-one".enabled'], true)
  assert.equal(config['apps."app-one".default_tools_enabled'], false)
  assert.equal(config['apps."app-one".destructive_enabled'], false)
  assert.equal(config['apps."app-one".open_world_enabled'], false)
  assert.equal(config['apps."app-one".tools."read/item".enabled'], true)
  assert.equal(config['apps."app-two".enabled'], false)
  assert.equal(config['mcp_servers."trusted.alpha".enabled'], true)
  assert.deepEqual(
    config['mcp_servers."trusted.alpha".enabled_tools'],
    ['lookup/exact', 'check'],
  )
  assert.equal(config['mcp_servers."trusted.beta".enabled'], false)
  assert.deepEqual(config['mcp_servers."trusted.beta".enabled_tools'], [])
  assert.equal(
    Object.keys(config).some((key) =>
      /(?:oauth|bearer_token|http_headers|env_http_headers|\.command$|\.url$)/i.test(key),
    ),
    false,
  )
  assert.deepEqual(startCall.params.environments, [
    { environmentId: 'local', cwd: WORKSPACE },
  ])
  assert.deepEqual(startCall.params.runtimeWorkspaceRoots, [WORKSPACE])
  for (const forbidden of [
    'browserProfile',
    'browserSession',
    'browserEnvironment',
    'browserMounts',
    'hostBrowser',
    'inAppBrowser',
  ]) {
    assert.equal(forbidden in startCall.params, false)
  }
  assert.equal(
    Object.keys(config).some((key) =>
      key.startsWith('apps.') && key.includes('.tools.') && !key.includes('"read/item"'),
    ),
    false,
  )

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('untrusted MCP selection is rejected before any thread effect', async () => {
  const boundary = new FakeGoalBoundary({ trustedMcpServerIds: ['trusted.alpha'] })

  await assert.rejects(
    boundary.startBoundedGoalEpoch(
      request({
        selectedCapabilities: selectedCapabilities({
          mcpServers: [
            { id: 'not-trusted', enabledTools: ['lookup'] },
          ],
        }),
      }),
    ),
    /untrusted preconfigured server id not-trusted/,
  )
  assert.deepEqual(boundary.requests, [])
})

test('untrusted apps and auth-shaped selection fields are rejected before thread effects', async () => {
  const untrusted = new FakeGoalBoundary({ trustedAppIds: ['trusted-app'] })
  await assert.rejects(
    untrusted.startBoundedGoalEpoch(
      request({
        selectedCapabilities: selectedCapabilities({
          apps: [{ id: 'other-app', enabledTools: ['read'] }],
        }),
      }),
    ),
    /untrusted connector id other-app/,
  )
  assert.deepEqual(untrusted.requests, [])

  const authShaped = new FakeGoalBoundary({ trustedAppIds: ['trusted-app'] })
  await assert.rejects(
    authShaped.startBoundedGoalEpoch(
      request({
        selectedCapabilities: {
          ...selectedCapabilities(),
          apps: [
            {
              id: 'trusted-app',
              enabledTools: ['read'],
              oauthToken: 'must-not-enter-the-selection',
            } as never,
          ],
        },
      }),
    ),
    /wrong closed shape/,
  )
  assert.deepEqual(authShaped.requests, [])

  const personalBrowser = new FakeGoalBoundary()
  await assert.rejects(
    personalBrowser.startBoundedGoalEpoch(
      request({
        selectedCapabilities: {
          ...selectedCapabilities(),
          browser: {
            mode: 'isolated_ephemeral_unauthenticated',
            profileId: 'personal-session',
          } as never,
        },
      }),
    ),
    /wrong closed shape/,
  )
  assert.deepEqual(personalBrowser.requests, [])
})

test('capability selections add no repository count or identifier length ceiling', async () => {
  const appId = `app ${'identity-'.repeat(1_500)}`
  const enabledTools = Array.from(
    { length: 1_000 },
    (_, index) => `tool/${index}/${'detail'.repeat(20)}`,
  )
  const boundary = new FakeGoalBoundary({ trustedAppIds: [appId] })

  await boundary.startBoundedGoalEpoch(
    request({
      selectedCapabilities: selectedCapabilities({
        apps: [{ id: appId, enabledTools }],
      }),
    }),
  )
  const startCall = boundary.requests.find((entry) => entry.method === 'thread/start')
  assert.ok(startCall)
  const config = startCall.params.config as Record<string, unknown>
  const appKey = `apps.${JSON.stringify(appId)}`
  assert.equal(config[`${appKey}.enabled`], true)
  assert.equal(
    enabledTools.every((tool) =>
      config[`${appKey}.tools.${JSON.stringify(tool)}.enabled`] === true,
    ),
    true,
  )

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('a capability root cannot silently bind an ambient environment', async () => {
  const boundary = new FakeGoalBoundary()

  await assert.rejects(
    boundary.startBoundedGoalEpoch(
      request({
        selectedCapabilities: selectedCapabilities({
          localRoots: [
            {
              kind: 'skill',
              id: 'unbound-root',
              location: {
                type: 'environment',
                environmentId: 'ambient-environment',
                path: '/opt/ambient',
              },
            },
          ],
        }),
      }),
    ),
    /must reference a supplied environment/,
  )
  assert.equal(boundary.requests.length, 0)
})

test('Goal environment stays local and selected roots stay absolute, read-only, and separate', async () => {
  await assert.rejects(
    new FakeGoalBoundary().startBoundedGoalEpoch(request({ environments: [] })),
    /must select exactly one local environment/,
  )

  await assert.rejects(
    new FakeGoalBoundary().startBoundedGoalEpoch(
      request({
        environments: [{ environmentId: 'local', cwd: 'relative/path' }],
      }),
    ),
    /must be an absolute local path/,
  )

  await assert.rejects(
    new FakeGoalBoundary().startBoundedGoalEpoch(
      request({
        environments: [{ environmentId: 'remote', cwd: WORKSPACE }],
      }),
    ),
    /must use the local environment/,
  )

  await assert.rejects(
    new FakeGoalBoundary().startBoundedGoalEpoch(
      request({
        environments: [{ environmentId: 'local', cwd: READ_ROOT }],
      }),
    ),
    /cwd must equal its writable Goal workspace/,
  )

  await assert.rejects(
    new FakeGoalBoundary().startBoundedGoalEpoch(
      request({
        selectedCapabilities: selectedCapabilities({
          localRoots: [
            {
              kind: 'skill',
              id: 'outside-read-only-root',
              location: {
                type: 'environment',
                environmentId: 'local',
                path: OUTSIDE_READ_ROOT,
              },
            },
          ],
        }),
      }),
    ),
    /must stay inside one authorized read-only Goal root/,
  )

  await assert.rejects(
    new FakeGoalBoundary().startBoundedGoalEpoch(
      request({
        selectedCapabilities: selectedCapabilities({
          localRoots: [
            {
              kind: 'skill',
              id: 'writable-root',
              location: {
                type: 'environment',
                environmentId: 'local',
                path: WORKSPACE,
              },
            },
          ],
        }),
      }),
    ),
    /must not overlap the writable Goal root/,
  )

  await assert.rejects(
    new FakeGoalBoundary().startBoundedGoalEpoch(
      request({
        selectedCapabilities: selectedCapabilities({
          localRoots: [
            {
              kind: 'skill',
              id: 'protected-root',
              location: {
                type: 'environment',
                environmentId: 'local',
                path: PROTECTED_ROOT,
              },
            },
          ],
        }),
      }),
    ),
    /must not overlap the protected Goal root/,
  )
})

test('optional native role and root data delivery do not change other Goal consumers', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const config = boundary.requests.find(({ method }) => method === 'thread/start')!.params.config as Record<string, unknown>
  assert.equal(Object.keys(config).some((key) => key.endsWith('.config_file')), false)
  assert.equal(boundary.requests.some(({ method }) => method === 'thread/inject_items'), false)
})

test('release-owned instruction-only roles configure both root start and suspended resume', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-native-role-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const configFile = path.join(root, 'fallback.toml')
  fs.writeFileSync(configFile, 'developer_instructions = ' + JSON.stringify('Common foundation.\nSubordinate role; no executive science.') + '\n')
  const names = ['default', 'worker', 'explorer', 'rh_researcher', 'rh_helper', 'rh_historical', 'rh_restricted_review']
  const nativeAgentRoles = names.map((name) => ({ name, description: name + ' bounded role.', configFile }))
  const boundary = new FakeGoalBoundary({ goalPolicy: { ...GOAL_POLICY, readOnlyRoots: [READ_ROOT, root] } })
  await boundary.startBoundedGoalEpoch(request({ nativeAgentRoles }))
  const limited = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({ method: 'thread/goal/updated', params: { threadId: ROOT_THREAD_ID, goal: limited } })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  await boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request({ nativeAgentRoles }))
  for (const method of ['thread/start', 'thread/resume']) {
    const params = boundary.requests.find((entry) => entry.method === method)!.params
    const config = params.config as Record<string, unknown>
    for (const name of names) {
      assert.equal(config['agents.' + JSON.stringify(name) + '.config_file'], fs.realpathSync(configFile))
      assert.equal(config['agents.' + JSON.stringify(name) + '.description'], name + ' bounded role.')
    }
    assert.equal(params.model, GOAL_POLICY.expectedModel)
    assert.equal(params.modelProvider, GOAL_POLICY.expectedModelProvider)
    assert.equal(params.permissions, GOAL_POLICY.permissionProfileId)
    assert.equal(params.approvalPolicy, 'never')
  }
})

test('native role files reject missing, out-of-root, noncanonical and extra configuration before RPC', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-native-role-invalid-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const configFile = path.join(root, 'role.toml')
  const role = { name: 'rh_helper', description: 'Bounded helper.', configFile }
  const policy = { ...GOAL_POLICY, readOnlyRoots: [READ_ROOT, root] }
  const missing = new FakeGoalBoundary({ goalPolicy: policy })
  await assert.rejects(missing.startBoundedGoalEpoch(request({ nativeAgentRoles: [role] })), /ENOENT/)
  assert.equal(missing.requests.length, 0)
  fs.writeFileSync(configFile, 'developer_instructions = "common"\n')
  const outside = new FakeGoalBoundary()
  await assert.rejects(outside.startBoundedGoalEpoch(request({ nativeAgentRoles: [role] })), /authorized read-only root/)
  assert.equal(outside.requests.length, 0)
  for (const content of [
    'developer_instructions = ""\n',
    'developer_instructions = """common"""\n',
    'developer_instructions = "common"\nmodel = "other-model"\n',
    'developer_instructions = "common"\npermissions = "other-policy"\n',
  ]) {
    fs.writeFileSync(configFile, content)
    const invalid = new FakeGoalBoundary({ goalPolicy: policy })
    await assert.rejects(invalid.startBoundedGoalEpoch(request({ nativeAgentRoles: [role] })), /canonical generated developer_instructions assignment/)
    assert.equal(invalid.requests.length, 0)
  }
  fs.writeFileSync(configFile, 'developer_instructions = "common"\n')
  const duplicate = new FakeGoalBoundary({ goalPolicy: policy })
  await assert.rejects(duplicate.startBoundedGoalEpoch(request({ nativeAgentRoles: [role, role] })), /duplicate names/)
  assert.equal(duplicate.requests.length, 0)
})

test('root scientific data is appended as assistant history only before activation and refreshed on resume', async () => {
  const boundary = new FakeGoalBoundary()
  const initialContextText = '<root_data>{"orientation":"first","host_continuity":"first"}</root_data>'
  const refreshedContextText = '<root_data>{"orientation":"fresh","host_continuity":"fresh"}</root_data>'
  const developerInstructions = 'Science-free common instructions. Executive clauses apply only to the root.'
  await boundary.startBoundedGoalEpoch(request({ developerInstructions, initialContextText }))
  const startIndex = boundary.requests.findIndex(({ method }) => method === 'thread/start')
  const firstInjectIndex = boundary.requests.findIndex(({ method }) => method === 'thread/inject_items')
  const firstGoalIndex = boundary.requests.findIndex(({ method, params }) => method === 'thread/goal/set' && params.status === 'active')
  assert.ok(startIndex < firstInjectIndex && firstInjectIndex < firstGoalIndex)
  assert.equal(boundary.requests[startIndex]!.params.developerInstructions, developerInstructions)
  assert.deepEqual(boundary.requests[firstInjectIndex]!.params, {
    threadId: ROOT_THREAD_ID,
    items: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: initialContextText }] }],
  })

  const limited = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({ method: 'thread/goal/updated', params: { threadId: ROOT_THREAD_ID, goal: limited } })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  const resumeStart = boundary.requests.length
  await boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request({ developerInstructions, initialContextText: refreshedContextText }))
  const resumedCalls = boundary.requests.slice(resumeStart)
  const resumeIndex = resumedCalls.findIndex(({ method }) => method === 'thread/resume')
  const injectIndex = resumedCalls.findIndex(({ method }) => method === 'thread/inject_items')
  const activateIndex = resumedCalls.findIndex(({ method, params }) => method === 'thread/goal/set' && params.status === 'active')
  assert.ok(resumeIndex < injectIndex && injectIndex < activateIndex)
  assert.deepEqual(resumedCalls[injectIndex]!.params, {
    threadId: ROOT_THREAD_ID,
    items: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: refreshedContextText }] }],
  })
  assert.equal(resumedCalls[resumeIndex]!.params.developerInstructions, developerInstructions)
  assert.equal(resumedCalls.some(({ method }) => /rollback|turn\/start/.test(method)), false)
  assert.equal(boundary.requests.filter(({ method }) => method === 'thread/inject_items').length, 2)
})

test('cancellation drains initial data append without a retry or Goal activation', async () => {
  const boundary = new FakeGoalBoundary()
  const blocker = boundary.blockMethod('thread/inject_items')
  const controller = new AbortController()
  let settled = false
  const starting = boundary.startBoundedGoalEpoch(request({ initialContextText: 'Bounded root data.' }), controller.signal)
    .finally(() => { settled = true })
  await waitUntil(() => boundary.requests.some(({ method }) => method === 'thread/inject_items'), 'initial root data append')
  controller.abort('operator_stop')
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(settled, false)
  blocker.resolve()
  await assert.rejects(starting, GoalEpochCancelledError)
  assert.equal(boundary.requests.filter(({ method }) => method === 'thread/inject_items').length, 1)
  assert.equal(boundary.requests.some(({ method, params }) => method === 'thread/goal/set' && params.status === 'active'), false)
})

test('uncertain initial append on resume is contained and is not replayed by a failed retry', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const limited = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({ method: 'thread/goal/updated', params: { threadId: ROOT_THREAD_ID, goal: limited } })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  const activeSets = boundary.requests.filter(({ method, params }) => method === 'thread/goal/set' && params.status === 'active').length
  const blocker = boundary.blockMethod('thread/inject_items')
  const resumedRequest = request({ initialContextText: 'Fresh orientation and Host continuity.' })
  const resuming = boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, resumedRequest)
  await waitUntil(() => boundary.requests.some(({ method }) => method === 'thread/inject_items'), 'resumed root data append')
  blocker.reject(new Error('App Server closed before the append reply'))
  await assert.rejects(resuming, /App Server closed before the append reply/)
  assert.equal(boundary.requests.filter(({ method, params }) => method === 'thread/goal/set' && params.status === 'active').length, activeSets)
  await assert.rejects(boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, resumedRequest), /terminal bounded Goal cannot be resumed/)
  assert.equal(boundary.requests.filter(({ method }) => method === 'thread/inject_items').length, 1)
})

test('resume initial context append rejection with concurrent cancellation is terminal and never replayed', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const limited = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({ method: 'thread/goal/updated', params: { threadId: ROOT_THREAD_ID, goal: limited } })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  const activeSets = boundary.requests.filter(({ method, params }) => method === 'thread/goal/set' && params.status === 'active').length
  const blocker = boundary.blockMethod('thread/inject_items')
  const controller = new AbortController()
  const resumedRequest = request({ initialContextText: 'Fresh orientation with possibly persisted append.' })
  const resuming = boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, resumedRequest, controller.signal)
  await waitUntil(() => boundary.requests.some(({ method }) => method === 'thread/inject_items'), 'unacknowledged resumed root data append')
  controller.abort('operator_stop')
  blocker.reject(new Error('Native rollout flush failed after history recording'))
  await assert.rejects(resuming, {
    name: 'GoalEpochInitialContextAppendError',
    message: 'Native rollout flush failed after history recording',
  })
  assert.equal(boundary.requests.filter(({ method, params }) => method === 'thread/goal/set' && params.status === 'active').length, activeSets)
  await assert.rejects(boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, resumedRequest), /terminal bounded Goal cannot be resumed/)
  assert.equal(boundary.requests.filter(({ method }) => method === 'thread/inject_items').length, 1)
})

test('resume initial context append acknowledgment with concurrent cancellation retains safe suspension', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const limited = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({ method: 'thread/goal/updated', params: { threadId: ROOT_THREAD_ID, goal: limited } })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  const activeSets = boundary.requests.filter(({ method, params }) => method === 'thread/goal/set' && params.status === 'active').length
  const blocker = boundary.blockMethod('thread/inject_items')
  const controller = new AbortController()
  const resuming = boundary.resumeBoundedGoalEpoch(
    ROOT_THREAD_ID,
    request({ initialContextText: 'Acknowledged orientation cut.' }),
    controller.signal,
  )
  await waitUntil(() => boundary.requests.some(({ method }) => method === 'thread/inject_items'), 'acknowledged resumed root data append')
  controller.abort('operator_stop')
  blocker.resolve()
  await assert.rejects(resuming, GoalEpochCancelledError)
  assert.equal(boundary.requests.filter(({ method, params }) => method === 'thread/goal/set' && params.status === 'active').length, activeSets)
  assert.equal(boundary.requests.filter(({ method }) => method === 'thread/inject_items').length, 1)
  await boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request({ initialContextText: 'A newly assembled orientation cut.' }))
  const injections = boundary.requests.filter(({ method }) => method === 'thread/inject_items')
  assert.equal(injections.length, 2)
  assert.deepEqual(injections[1]!.params.items, [{
    type: 'message',
    role: 'assistant',
    content: [{ type: 'output_text', text: 'A newly assembled orientation cut.' }],
  }])
})

test('long research objectives are not rejected by a repository length ceiling', async () => {
  const boundary = new FakeGoalBoundary()
  const objective = 'Investigate the mathematical route in full.\n' + 'x'.repeat(20_000)

  await boundary.startBoundedGoalEpoch(request({ objective }))
  const goalSet = boundary.requests.find((entry) => entry.method === 'thread/goal/set')
  assert.equal(goalSet?.params.objective, objective)

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('more than eight explicitly supplied dynamic tools are accepted', async () => {
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({
      success: true,
      contentItems: [{ type: 'inputText', text: 'ok' }],
    }),
  })
  const dynamicTools = Array.from({ length: 12 }, (_, index) => ({
    ...DYNAMIC_TOOL,
    name: 'research_tool_' + index,
  }))

  await boundary.startBoundedGoalEpoch(request({ dynamicTools }))
  const startCall = boundary.requests.find((entry) => entry.method === 'thread/start')
  assert.equal((startCall?.params.dynamicTools as unknown[])?.length, dynamicTools.length)

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('Core tracks platform-created children without imposing a local concurrency ceiling', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())

  await boundary.deliver(spawnItem('child-a'))
  await boundary.deliver(spawnItem('child-b'))
  await boundary.deliver(spawnItem('child-c'))

  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.deepEqual(
    state.descendants.map((entry) => entry.threadId),
    ['child-a', 'child-b', 'child-c'],
  )

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('thread/start rejects direct effective model or effort drift before Goal activation', async () => {
  for (const override of [
    { modelProvider: 'provider-readback-drift' },
    { model: 'model-readback-drift' },
    { reasoningEffort: 'medium' },
  ]) {
    const boundary = new FakeGoalBoundary()
    boundary.startResultOverrides = override

    await assert.rejects(
      () => boundary.startBoundedGoalEpoch(request()),
      /thread\/start did not preserve root Goal containment/,
    )
    assert.equal(
      boundary.requests.some(
        ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
      ),
      true,
    )
  }
})

test('direct descendant settings drift stops only its Goal and preserves captured material', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  let missionFenceCalls = 0
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
    onMissionFence: async () => {
      missionFenceCalls += 1
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('child-before-settings-drift'))

  await boundary.deliver({
    method: 'thread/settings/updated',
    params: {
      threadId: 'child-before-settings-drift',
      threadSettings: {
        model: 'settings-readback-drift',
        modelProvider: 'openai',
        effort: 'ultra',
      },
    },
  })
  await waitUntil(() => stopContexts.length === 1, 'model settings containment')
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.equal(stopContexts[0]?.reason, 'model_contract_violation')
  assert.equal(stopContexts[0]?.containmentScope, 'goal_local')
  assert.equal(missionFenceCalls, 0)
  assert.equal(
    state.nativeMaterialObservations.some(
      (entry) =>
        entry.materialKind === 'assignment' &&
        entry.childThreadId === 'child-before-settings-drift',
    ),
    true,
  )
})

test('model/rerouted immediately stops only the affected Goal', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  let missionFenceCalls = 0
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
    onMissionFence: async () => {
      missionFenceCalls += 1
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  await boundary.deliver({
    method: 'model/rerouted',
    params: {
      threadId: ROOT_THREAD_ID,
      turnId: ROOT_TURN_ID,
      fromModel: 'gpt-5.6-sol',
      toModel: 'rerouted-readback',
      reason: 'highRiskCyberActivity',
    },
  })
  await waitUntil(() => stopContexts.length === 1, 'model reroute containment')

  assert.equal(stopContexts[0]?.reason, 'model_contract_violation')
  assert.equal(stopContexts[0]?.containmentScope, 'goal_local')
  assert.equal(missionFenceCalls, 0)
})

test('matching direct settings do not stop a valid Goal', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  await boundary.deliver({
    method: 'thread/settings/updated',
    params: {
      threadId: ROOT_THREAD_ID,
      threadSettings: {
        model: 'gpt-5.6-sol',
        modelProvider: 'openai',
        effort: 'ultra',
      },
    },
  })
  await new Promise<void>((resolve) => setImmediate(resolve))

  assert.equal(stopContexts.length, 0)
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'active')
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('early unrelated lifecycle notifications do not invalidate valid work', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.deliver({
    method: 'thread/started',
    params: { thread: { id: 'unrelated-child', parentThreadId: 'unrelated-root' } },
  })
  await boundary.deliver({
    method: 'turn/started',
    params: { threadId: 'unrelated-child', turn: { id: 'unrelated-turn' } },
  })

  const handle = await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver({
    method: 'model/rerouted',
    params: {
      threadId: 'unrelated-child',
      turnId: 'unrelated-turn',
      fromModel: 'gpt-5.6-sol',
      toModel: 'gpt-other',
      reason: 'highRiskCyberActivity',
    },
  })
  assert.deepEqual(handle, { threadId: ROOT_THREAD_ID })
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(state.activeTurnId, ROOT_TURN_ID)
  assert.deepEqual(state.descendants, [])

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('native assignments and completed outputs are preserved once as readable material', async () => {
  const observed: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  await boundary.deliver(spawnItem('child-a'))
  await boundary.deliver(spawnItem('child-a'))
  await boundary.deliver({
    method: 'turn/started',
    params: { threadId: 'child-a', turn: { id: 'child-turn-a' } },
  })
  await boundary.deliver(waitItem('child-a', 'A useful candidate lemma with a precise obstruction.'))

  const active = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(observed.length, 2)
  assert.deepEqual(
    observed.map((entry) => [entry.materialKind, entry.content]),
    [
      ['assignment', 'Investigate the assigned mathematical route.'],
      ['output', 'A useful candidate lemma with a precise obstruction.'],
    ],
  )
  assert.equal(active.descendants[0]?.threadId, 'child-a')
  assert.equal(active.descendants[0]?.status, 'active')
  assert.equal(active.descendants[0]?.activeTurnId, 'child-turn-a')
  assert.equal(active.nativeMaterialObservations.length, 2)
  assert.equal('sha256' in active.nativeMaterialObservations[0], false)
  assert.equal('eventOrdinal' in active.nativeMaterialObservations[0], false)
  assert.equal('custodyStatus' in active.nativeMaterialObservations[0], false)

  await boundary.deliver({ method: 'turn/completed', params: {
    threadId: 'child-a', turn: { id: 'child-turn-a', status: 'completed' },
  } })
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).descendants[0]?.status, 'complete')

  const completeGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'complete')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: completeGoal },
  })
  const completed = await boundary.finalizeCompletedGoalEpoch(ROOT_THREAD_ID)
  assert.equal(completed.goal?.status, 'complete')
  assert.equal(completed.nativeMaterialObservations.length, 2)
  assert.deepEqual(
    completed.nativeMaterialObservations.map((entry) => entry.content),
    [
      'Investigate the assigned mathematical route.',
      'A useful candidate lemma with a precise obstruction.',
    ],
  )
})

test('rejected native-material delivery is unmarked so exact replay can redeliver', async () => {
  const attempts: GoalEpochNativeMaterialObservation[] = []
  let rejectDelivery = true
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      attempts.push(material)
      if (rejectDelivery) {
        rejectDelivery = false
        throw new Error('durable custody delivery failed before acceptance')
      }
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const observation = spawnItem('child-custody-replay')

  await assert.rejects(
    boundary.deliver(observation),
    /durable custody delivery failed before acceptance/,
  )
  assert.equal(
    (await boundary.readGoalEpochState(ROOT_THREAD_ID)).nativeMaterialObservations.length,
    0,
  )

  await boundary.deliver(observation)
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(attempts.length, 2)
  assert.equal(attempts[0]?.observationId, attempts[1]?.observationId)
  assert.equal(state.nativeMaterialObservations.length, 1)
  assert.equal(state.nativeMaterialObservations[0]?.observationId, attempts[1]?.observationId)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('capture-recovery outcome completes local native-material custody', async () => {
  const outcomes: string[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      outcomes.push(material.observationId)
      return {
        status: 'capture_recovery_required',
        observationId: material.observationId,
        ownerCode: 'mission_capture_rejected',
        executiveEpochId: 'epoch:capture-recovery',
        nativeLineage: {
          materialKind: material.materialKind,
          rootThreadId: material.rootThreadId,
          parentThreadId: material.parentThreadId,
          childThreadId: material.childThreadId,
        },
        plaintextReference: {
          kind: 'codex_native_material',
          observationId: material.observationId,
          rootThreadId: material.rootThreadId,
          parentThreadId: material.parentThreadId,
          childThreadId: material.childThreadId,
        },
        recovery: {
          operation: 'interpret_material',
          inputRoute: 'capture_scopes[].adopted_root_material',
          channel: material.materialKind === 'assignment' ? 'native_assignment' : 'native_output',
        },
      }
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const observation = spawnItem('child-custody-recovery')

  await boundary.deliver(observation)
  await boundary.deliver(observation)
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.equal(outcomes.length, 1)
  assert.equal(state.goal?.status, 'active')
  assert.equal(state.nativeMaterialObservations.length, 1)
  assert.equal(state.nativeMaterialObservations[0]?.observationId, outcomes[0])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('mismatched native-material custody outcome rejects without consuming the observation', async () => {
  const missionFences: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => ({
      status: 'captured',
      observationId: material.observationId === 'f'.repeat(64)
        ? 'e'.repeat(64)
        : 'f'.repeat(64),
      captureRef: {
        materialId: 'raw-capture:mismatched-custody-outcome',
        revision: 1,
      },
    }),
    onMissionFence: async (context) => {
      missionFences.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  await assert.rejects(
    boundary.deliver(spawnItem('child-custody-identity-mismatch')),
    GoalEpochMissionConsistencyError,
  )
  await boundary.waitForFatalBoundaryFence()
  const terminal = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.deepEqual(
    missionFences.map(({ reason, containmentScope }) => ({ reason, containmentScope })),
    [{ reason: 'mission_consistency_failure', containmentScope: 'mission_fence' }],
  )
  assert.equal(terminal.nativeMaterialObservations.length, 0)
})

test('V1 descendant final output is preserved before root wait and deduplicated on wait', async () => {
  const observed: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('child-v1-final'))
  await boundary.deliver({
    method: 'rawResponseItem/completed',
    params: {
      threadId: 'child-v1-final',
      turnId: 'child-turn-v1-final',
      item: {
        type: 'message',
        role: 'assistant',
        phase: 'commentary',
        content: [{ type: 'output_text', text: 'Interim scratch work.' }],
      },
    },
  })
  const finalOutput = 'A retained candidate lemma even if root never reaches wait.'
  await boundary.deliver({
    method: 'rawResponseItem/completed',
    params: {
      threadId: 'child-v1-final',
      turnId: 'child-turn-v1-final',
      item: {
        type: 'message',
        role: 'assistant',
        phase: 'final_answer',
        content: [{ type: 'output_text', text: finalOutput }],
      },
    },
  })

  assert.deepEqual(
    observed.map((entry) => [entry.materialKind, entry.childThreadId, entry.content]),
    [
      ['assignment', 'child-v1-final', 'Investigate the assigned mathematical route.'],
      ['output', 'child-v1-final', finalOutput],
    ],
  )

  await boundary.deliver(waitItem('child-v1-final', finalOutput))
  assert.equal(observed.length, 2)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('unexpected legacy V2 plaintext envelopes preserve collaboration through raw response items', async () => {
  const observed: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const start = boundary.requests.find((entry) => entry.method === 'thread/start')
  assert.equal(start?.params.experimentalRawEvents, true)

  await boundary.deliver(subAgentActivity('child-v2', '/root/independent_audit'))
  const output = rawAgentMessage(
    '/root/independent_audit',
    '/root',
    [
      {
        type: 'input_text',
        text: (
          'Message Type: FINAL_ANSWER\n'
          + 'Task name: /root\n'
          + 'Sender: /root/independent_audit\n'
          + 'Payload:'
        ),
      },
      { type: 'input_text', text: 'Exact audit finding: the tested implication stops at N=10^6.' },
    ],
  )
  await boundary.deliver(output)
  await boundary.deliver(output)

  assert.deepEqual(
    observed.map((entry) => [entry.materialKind, entry.childThreadId, entry.content]),
    [
      [
        'output',
        'child-v2',
        'Exact audit finding: the tested implication stops at N=10^6.',
      ],
    ],
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('unexpected legacy V2 output arriving before agent-path activity is preserved after mapping', async () => {
  const observed: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver({
    method: 'thread/started',
    params: { thread: { id: 'child-late-map', parentThreadId: ROOT_THREAD_ID } },
  })
  await boundary.deliver(
    rawAgentMessage(
      '/root/late_map',
      '/root',
      [{
        type: 'input_text',
        text: (
          'Message Type: FINAL_ANSWER\n'
          + 'Task name: /root\n'
          + 'Sender: /root/late_map\n'
          + 'Payload:\n'
          + 'Readable output before path correlation.'
        ),
      }],
    ),
  )
  assert.equal(observed.length, 0)

  await boundary.deliver(subAgentActivity('child-late-map', '/root/late_map'))
  assert.deepEqual(
    observed.map((entry) => [entry.materialKind, entry.childThreadId, entry.content]),
    [['output', 'child-late-map', 'Readable output before path correlation.']],
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('unexpected legacy V2 encrypted envelopes are not misrepresented as readable', async () => {
  const observed: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(subAgentActivity('child-encrypted', '/root/encrypted_task'))
  await boundary.deliver(
    rawAgentMessage(
      '/root',
      '/root/encrypted_task',
      [{ type: 'encrypted_content', encrypted_content: 'opaque-provider-payload' }],
    ),
  )
  await boundary.deliver(
    rawAgentMessage(
      '/root/encrypted_task',
      '/root',
      [
        {
          type: 'input_text',
          text: (
            'Message Type: FINAL_ANSWER\n'
            + 'Task name: /root\n'
            + 'Sender: /root/encrypted_task\n'
            + 'Payload:\n'
            + 'incomplete plaintext prefix'
          ),
        },
        { type: 'encrypted_content', encrypted_content: 'opaque-provider-payload' },
      ],
    ),
  )
  await boundary.deliver({
    method: 'rawResponseItem/completed',
    params: {
      threadId: ROOT_THREAD_ID,
      turnId: ROOT_TURN_ID,
      item: {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text: 'ordinary root transcript' }],
      },
    },
  })

  assert.deepEqual(observed, [])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('a researcher may create a leaf helper and receives its output without root relay', async () => {
  const observed: GoalEpochNativeMaterialObservation[] = []
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
    onMissionFence: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('branch-researcher'))
  await boundary.deliver(nestedSpawnItem('branch-researcher', 'leaf-helper'))
  await boundary.deliver(subAgentActivity('branch-researcher', '/root/branch_researcher'))
  await boundary.deliver(
    subAgentActivity(
      'leaf-helper',
      '/root/branch_researcher/hostile_checker',
      'branch-researcher',
    ),
  )
  const helperOutput = 'The proposed continuation fails at the stated uniformity step.'
  await boundary.deliver(
    rawAgentMessage(
      '/root/branch_researcher/hostile_checker',
      '/root/branch_researcher',
      [{
        type: 'input_text',
        text: (
          'Message Type: FINAL_ANSWER\n'
          + 'Task name: /root/branch_researcher\n'
          + 'Sender: /root/branch_researcher/hostile_checker\n'
          + 'Payload:\n'
          + helperOutput
        ),
      }],
      'branch-researcher',
    ),
  )
  await boundary.deliver(waitItem('leaf-helper', helperOutput, 'branch-researcher'))
  const researcherSynthesis = (
    'The branch synthesis rejects the continuation because the helper located '
    + 'a uniformity failure.'
  )
  await boundary.deliver(
    rawAgentMessage('/root/branch_researcher', '/root', [{
      type: 'input_text',
      text: (
        'Message Type: FINAL_ANSWER\n'
        + 'Task name: /root\n'
        + 'Sender: /root/branch_researcher\n'
        + 'Payload:\n'
        + researcherSynthesis
      ),
    }]),
  )
  await boundary.deliver(waitItem('branch-researcher', researcherSynthesis))

  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.deepEqual(
    state.descendants.map((entry) => [entry.threadId, entry.parentThreadId]),
    [
      ['branch-researcher', ROOT_THREAD_ID],
      ['leaf-helper', 'branch-researcher'],
    ],
  )
  assert.deepEqual(
    observed.map((entry) => [
      entry.materialKind,
      entry.parentThreadId,
      entry.childThreadId,
      entry.content,
    ]),
    [
      [
        'assignment',
        ROOT_THREAD_ID,
        'branch-researcher',
        'Investigate the assigned mathematical route.',
      ],
      [
        'assignment',
        'branch-researcher',
        'leaf-helper',
        'Investigate one helper route for the parent research branch.',
      ],
      ['output', 'branch-researcher', 'leaf-helper', helperOutput],
      ['output', ROOT_THREAD_ID, 'branch-researcher', researcherSynthesis],
    ],
  )
  assert.equal(stopContexts.length, 0)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('a leaf helper spawn request triggers exact Mission containment before registering depth three', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onMissionFence: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('branch-researcher'))
  await boundary.deliver(nestedSpawnItem('branch-researcher', 'leaf-helper'))
  await boundary.deliver(nestedSpawnItem('leaf-helper', 'depth-three'))
  await boundary.waitForFatalBoundaryFence()

  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.equal(stopContexts[0]?.reason, 'mission_consistency_failure')
  assert.equal(stopContexts[0]?.containmentScope, 'mission_fence')
  assert.equal(state.descendants.some(({ threadId }) => threadId === 'depth-three'), false)
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === 'leaf-helper',
    ),
    true,
  )
  assert.match(boundary.getReadyReason() ?? '', /maximum depth 2/)
})

test('same-Goal cross-parent communication preserves branch ownership and continued work', async () => {
  const cases = [
    {
      content: 'Root shares a useful observation with the helper.',
      event: (content: string) => sendInputItem('leaf-helper', content),
      capturesOutput: false,
    },
    {
      content: 'The helper returned useful output.',
      event: (content: string) => waitItem('leaf-helper', content),
      capturesOutput: true,
    },
  ]
  for (const item of cases) {
    const observed: GoalEpochNativeMaterialObservation[] = []
    const stopContexts: GoalEpochStopContext[] = []
    const boundary = new FakeGoalBoundary({
      onNativeMaterialObserved: async (material) => {
        observed.push(material)
      },
      onMissionFence: async (context) => {
        stopContexts.push(context)
      },
    })
    await boundary.startBoundedGoalEpoch(request())
    await boundary.deliver(spawnItem('branch-researcher'))
    await boundary.deliver(nestedSpawnItem('branch-researcher', 'leaf-helper'))

    await boundary.deliver(item.event(item.content))
    await boundary.waitForFatalBoundaryFence()

    assert.deepEqual(stopContexts, [])
    assert.deepEqual(observed.filter(({ content }) => content === item.content).map((material) => ({
      kind: material.materialKind, parent: material.parentThreadId, child: material.childThreadId,
    })), item.capturesOutput ? [{ kind: 'output', parent: 'branch-researcher', child: 'leaf-helper' }] : [])
    await boundary.deliver(sendInputItem('leaf-helper', 'The researcher continues its own branch.', 'branch-researcher'))
    assert.equal(observed.at(-1)?.parentThreadId, 'branch-researcher')
    assert.equal(observed.at(-1)?.materialKind, 'assignment')
  }
})

test('incident sibling communication continues and captures one actual-author output across recipients', async () => {
  const sender = '01a06b0e-d64e-7030-b4d7-187b362e60a3'
  const receiver = '01a06b0c-9fe4-7050-b477-eeb04ba9a360'
  const materials: GoalEpochNativeMaterialObservation[] = []
  const events: CodexExecutionObservation[] = []
  const stops: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => { materials.push(material) },
    onExecutionObservation: observationRecorder(events),
    onMissionFence: async (context) => { stops.push(context) },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem(sender))
  await boundary.deliver(spawnItem(receiver))
  await boundary.deliver(subAgentActivity(sender, '/root/sender'))
  await boundary.deliver(subAgentActivity(receiver, '/root/receiver'))
  await boundary.deliver(sendInputItem(receiver, 'Compare this synthetic bound.', sender))
  await boundary.waitForFatalBoundaryFence()
  assert.deepEqual(stops, [])
  assert.equal(materials.some((entry) => entry.content === 'Compare this synthetic bound.'), false)
  assert.equal(events.find((event) => event.contents.some((part) => part.text === 'Compare this synthetic bound.'))?.contents[0]?.field, 'message')
  const output = 'Synthetic completed bound.'
  await boundary.deliver(rawAgentMessage('/root/receiver', '/root/sender', [{
    type: 'input_text', text: `Message Type: FINAL_ANSWER\nTask name: /root/sender\nSender: /root/receiver\nPayload:\n${output}`,
  }], sender))
  await boundary.deliver(waitItem(receiver, output, sender))
  await boundary.deliver(waitItem(receiver, output))
  await boundary.deliver(rawAgentMessage('/root/receiver', '/root', [{
    type: 'input_text', text: `Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/receiver\nPayload:\n${output}`,
  }]))
  assert.deepEqual(materials.filter((entry) => entry.materialKind === 'output').map((entry) => ({
    content: entry.content, parent: entry.parentThreadId, child: entry.childThreadId,
  })), [{ content: output, parent: ROOT_THREAD_ID, child: receiver }])
  assert.equal(events.filter((event) => event.source_method === 'rawResponseItem/completed' && event.contents.some((part) => part.text === output)).length, 2)
  await boundary.deliver(sendInputItem(receiver, 'Continue the assigned route.'))
  assert.equal(materials.at(-1)?.materialKind, 'assignment')
  assert.deepEqual(boundary.resolveDirectChildToolGrantTarget({ rootThreadId: ROOT_THREAD_ID, childThreadId: receiver }), {
    rootThreadId: ROOT_THREAD_ID, childThreadId: receiver, parentThreadId: ROOT_THREAD_ID,
    depth: 1, status: 'pending', activeTurnId: null,
  })
})

test('wait reports cannot retire a newer target turn or revoke its exact live grant', async () => {
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  const materials: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onDescendantToolGrantRevoked: async (event) => { revocations.push(event) },
    onNativeMaterialObserved: async (material) => { materials.push(material) },
    dynamicToolHandler: async () => ({ success: true, contentItems: [{ type: 'inputText', text: 'read' }] }),
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('history-child'))
  await boundary.deliver(nativeChildStarted('history-child', 'rh_historical'))
  await boundary.deliver(spawnItem('sibling'))
  await boundary.deliver({ method: 'turn/started', params: { threadId: 'history-child', turn: { id: 'turn.new' } } })
  boundary.installDescendantToolGrant({ rootThreadId: ROOT_THREAD_ID, childThreadId: 'history-child',
    grantId: 'grant.new', assignmentId: 'assignment.new', allowedToolNames: [HISTORY_TOOL_NAME, HISTORY_PAGE_TOOL_NAME] })
  for (const waiter of [ROOT_THREAD_ID, 'sibling']) {
    for (const state of ['completed', 'pendingInit', 'running', 'interrupted', 'errored', 'shutdown', 'notFound']) {
      const event = waitItem('history-child', `Reported ${state}`, waiter)
      const item = paramsOf(paramsOf(event.params).item)
      item.status = 'completed'
      item.agentsStates = { 'history-child': { status: state, message: `Reported ${state}` } }
      await boundary.deliver(event)
    }
  }
  await boundary.deliver(waitItem('history-child', '', 'sibling'))
  assert.deepEqual(revocations, [])
  assert.equal(boundary.resolveDirectChildToolGrantTarget({ rootThreadId: ROOT_THREAD_ID, childThreadId: 'history-child' }).activeTurnId, 'turn.new')
  await boundary.deliver(dynamicToolRequest('new-turn-read', 'history-child', 'new-read', 'query', HISTORY_TOOL_NAME, 'turn.new'))
  assert.equal(boundary.responses.at(-1)?.result.success, true)
  assert.deepEqual(materials.filter((entry) => entry.materialKind === 'output').map((entry) => entry.content), ['Reported completed'])
  await boundary.deliver({ method: 'turn/completed', params: { threadId: 'history-child', turn: { id: 'turn.new', status: 'completed' } } })
  assert.equal(revocations.length, 1)
  assert.throws(() => boundary.resolveDirectChildToolGrantTarget({ rootThreadId: ROOT_THREAD_ID, childThreadId: 'history-child' }), /not one exact live direct child/)
})

test('validated peer raw messages are visible before owner capture and preserve native receipt identity', async () => {
  const events: CodexExecutionObservation[] = []
  const materials: GoalEpochNativeMaterialObservation[] = []
  let release!: () => void
  const blocked = new Promise<void>((resolve) => { release = resolve })
  let captureStarted = false
  const boundary = new FakeGoalBoundary({
    onExecutionObservation: observationRecorder(events),
    onNativeMaterialObserved: async (material) => {
      materials.push(material)
      if (material.materialKind === 'output') { captureStarted = true; await blocked }
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(subAgentActivity('a', '/root/a'))
  await boundary.deliver(subAgentActivity('b', '/root/b'))
  const envelope = (kind: string, body: string, author = '/root/b', recipient = '/root/a', outer = 'a') => rawAgentMessage(author, recipient, [{
    type: 'input_text', text: `Message Type: ${kind}\nTask name: ${recipient}\nSender: ${author}\nPayload:\n${body}`,
  }], outer)
  await boundary.deliver(envelope('MESSAGE', 'Peer progress.'))
  assert.equal(materials.length, 0)
  const delivery = boundary.deliver(envelope('FINAL_ANSWER', 'Peer final.'))
  await waitUntil(() => captureStarted, 'blocked peer output capture')
  const contentEvent = events.find((event) => event.contents.some((part) => part.text === 'Peer final.'))!
  assert.ok(contentEvent)
  assert.equal(contentEvent.identity.thread_id, 'a')
  assert.equal(contentEvent.identity.parent_thread_id, ROOT_THREAD_ID)
  assert.equal(contentEvent.kind === 'work' && contentEvent.details.sender_thread_id, 'b')
  assert.deepEqual(contentEvent.kind === 'work' && contentEvent.details.receiver_thread_ids, ['a'])
  assert.equal(contentEvent.phase, 'received')
  assert.ok(contentEvent.caused_by)
  assert.equal(events.some((event) => event.phase === 'processed' && event.caused_by?.sequence === contentEvent.caused_by?.sequence), false)
  release()
  await delivery
  await boundary.deliver(envelope('FINAL_ANSWER', 'Peer final.'))
  const duplicateReceipts = events.filter((event) => event.contents.some((part) => part.text === 'Peer final.'))
  assert.equal(duplicateReceipts.length, 2)
  assert.notDeepEqual(duplicateReceipts[0].caused_by, duplicateReceipts[1].caused_by)
  assert.equal(duplicateReceipts[0].contents[0].content_key, duplicateReceipts[1].contents[0].content_key)
  assert.equal(materials.length, 1)
  assert.equal(materials[0].parentThreadId, ROOT_THREAD_ID)
  assert.equal(materials[0].childThreadId, 'b')
  await boundary.deliver(envelope('FINAL_ANSWER', 'Root communication only.', '/root', '/root/a'))
  assert.equal(materials.length, 1)
  assert.ok(events.some((event) => event.contents.some((part) => part.text === 'Root communication only.')))
  await boundary.deliver(envelope('FINAL_ANSWER', 'Wrong outer receiver.', '/root/b', '/root/a', 'b'))
  await boundary.deliver(envelope('FINAL_ANSWER', 'Unknown author.', '/root/unknown', '/root/a'))
  await boundary.deliver(envelope('MESSAGE', 'Unknown ordinary sender.', '/root/unmapped', '/root/a'))
  assert.equal(materials.length, 1)
  assert.equal(events.some((event) => event.contents.some((part) => /Wrong outer|Unknown author|Unknown ordinary/.test(part.text))), false)
  assert.ok(events.some((event) => event.kind === 'coverage' && event.details.reason === 'native_agent_message_unresolved_or_unsupported'))
})

test('late path resolution publishes final content against its original receipt before capture', async () => {
  const events: CodexExecutionObservation[] = []
  const materials: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onExecutionObservation: observationRecorder(events),
    onNativeMaterialObserved: async (material) => {
      assert.ok(events.some((event) => event.contents.some((part) => part.text === material.content)))
      materials.push(material)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  // An unresolved ordinary message must not block the existing pending final-output route.
  await boundary.deliver(rawAgentMessage('/root/unknown', '/root', [{ type: 'input_text',
    text: 'Message Type: MESSAGE\nTask name: /root\nSender: /root/unknown\nPayload:\nUnmapped message.' }]))
  const raw = rawAgentMessage('/root/late', '/root', [{ type: 'input_text',
    text: 'Message Type: FINAL_ANSWER\nTask name: /root\nSender: /root/late\nPayload:\nLate mapped output.' }])
  await boundary.deliver(raw)
  const receiptIndex = events.findIndex((event) => event.kind === 'lifecycle' && event.phase === 'received' && event.identity.item_id === 'agent-message-/root/late-/root')
  assert.ok(receiptIndex >= 0)
  assert.equal(materials.length, 0)
  await boundary.deliver(subAgentActivity('late', '/root/late'))
  const projected = events.find((event) => event.contents.some((part) => part.text === 'Late mapped output.'))!
  assert.deepEqual(projected.caused_by, { incarnation: 'test-source', sequence: receiptIndex + 1 })
  assert.equal(projected.identity.item_id, 'agent-message-/root/late-/root')
  assert.equal(projected.source_method, 'rawResponseItem/completed')
  assert.equal(materials.length, 1)
})

test('peer-addressed leaf final custody is independent of observer availability', async () => {
  for (const onExecutionObservation of [undefined, () => { throw new Error('observer unavailable') }]) {
    const materials: GoalEpochNativeMaterialObservation[] = []
    const boundary = new FakeGoalBoundary({ onExecutionObservation,
      onNativeMaterialObserved: async (material) => { materials.push(material) } })
    await boundary.startBoundedGoalEpoch(request())
    await boundary.deliver(subAgentActivity('a', '/root/a'))
    await boundary.deliver(subAgentActivity('b', '/root/b'))
    await boundary.deliver(subAgentActivity('leaf', '/root/b/leaf', 'b'))
    await boundary.deliver(rawAgentMessage('/root/b/leaf', '/root/a', [{ type: 'input_text',
      text: 'Message Type: FINAL_ANSWER\nTask name: /root/a\nSender: /root/b/leaf\nPayload:\nLeaf final.' }], 'a'))
    await boundary.deliver(waitItem('leaf', 'Leaf final.', 'a'))
    await boundary.deliver(waitItem('leaf', 'Leaf final.', 'b'))
    assert.deepEqual(materials.map((entry) => ({ kind: entry.materialKind, parent: entry.parentThreadId, child: entry.childThreadId })), [
      { kind: 'output', parent: 'b', child: 'leaf' },
    ])
    assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).descendants.find((entry) => entry.threadId === 'leaf')?.parentThreadId, 'b')
  }
})

test('same-Goal mixed and root-target communication cannot manufacture assignments or membership', async () => {
  const events: CodexExecutionObservation[] = []
  const materials: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({ onExecutionObservation: observationRecorder(events),
    onNativeMaterialObserved: async (material) => { materials.push(material) } })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('a'))
  await boundary.deliver(spawnItem('b'))
  await boundary.deliver(nestedSpawnItem('a', 'leaf'))
  const mixed = sendInputItem('leaf', 'Shared branch input.', 'a')
  paramsOf(paramsOf(mixed.params).item).receiverThreadIds = ['leaf', 'b', 'unknown', ROOT_THREAD_ID]
  await boundary.deliver(mixed)
  await boundary.deliver(sendInputItem(ROOT_THREAD_ID, 'Upward update.', 'leaf'))
  await boundary.deliver(waitItem(ROOT_THREAD_ID, 'Root report.', 'b'))
  assert.deepEqual(materials.filter((entry) => ['Shared branch input.', 'Upward update.', 'Root report.'].includes(entry.content)).map((entry) => ({
    kind: entry.materialKind, parent: entry.parentThreadId, child: entry.childThreadId,
  })), [{ kind: 'assignment', parent: 'a', child: 'leaf' }])
  const shared = events.find((event) => event.contents.some((part) => part.text === 'Shared branch input.'))!
  assert.equal(shared.contents[0].field, 'message')
  assert.deepEqual(shared.kind === 'work' && shared.details.receiver_thread_ids, ['leaf', 'b', 'unknown', ROOT_THREAD_ID])
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.deepEqual(state.descendants.map((entry) => [entry.threadId, entry.parentThreadId]).sort(), [['a', ROOT_THREAD_ID], ['b', ROOT_THREAD_ID], ['leaf', 'a']])
})

test('communication sender mismatch still fences without false custody', async () => {
  for (const operation of [sendInputItem('b', 'Spoofed input.', 'a'), waitItem('b', 'Spoofed return.', 'a')]) {
    const stops: GoalEpochStopContext[] = []
    const materials: GoalEpochNativeMaterialObservation[] = []
    const boundary = new FakeGoalBoundary({ onMissionFence: async (context) => { stops.push(context) },
      onNativeMaterialObserved: async (material) => { materials.push(material) } })
    await boundary.startBoundedGoalEpoch(request())
    await boundary.deliver(spawnItem('a'))
    await boundary.deliver(spawnItem('b'))
    paramsOf(operation.params).threadId = ROOT_THREAD_ID
    await boundary.deliver(operation)
    await boundary.waitForFatalBoundaryFence()
    assert.equal(stops[0]?.reason, 'mission_consistency_failure')
    assert.equal(materials.some((entry) => entry.content.startsWith('Spoofed')), false)
  }
})

test('a depth-three thread-start event fails closed even without a collaboration completion item', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onMissionFence: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('branch-researcher'))
  await boundary.deliver(nestedSpawnItem('branch-researcher', 'leaf-helper'))

  await assert.rejects(
    boundary.deliver({
      method: 'thread/started',
      params: {
        thread: { id: 'depth-three', parentThreadId: 'leaf-helper' },
      },
    }),
    /maximum depth 2/,
  )
  await boundary.waitForFatalBoundaryFence()

  assert.equal(stopContexts[0]?.reason, 'mission_consistency_failure')
  assert.match(boundary.getReadyReason() ?? '', /maximum depth 2/)
})

test('a child thread changing its registered parent remains a Mission consistency failure', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onMissionFence: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('registered-child'))

  await assert.rejects(
    boundary.deliver({
      method: 'thread/started',
      params: {
        thread: { id: 'registered-child', parentThreadId: 'different-parent' },
      },
    }),
    /changed its Goal parent/,
  )
  await boundary.waitForFatalBoundaryFence()

  assert.equal(stopContexts[0]?.reason, 'mission_consistency_failure')
  assert.match(boundary.getReadyReason() ?? '', /changed its Goal parent/)
})

test('unexpected legacy V2 malformed envelopes do not create custody or reparent children', async () => {
  const observed: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(subAgentActivity('child-target', '/root/target'))
  await boundary.deliver(subAgentActivity('child-sibling', '/root/sibling'))
  await boundary.deliver({
    method: 'item/completed',
    params: {
      threadId: 'child-sibling',
      turnId: ROOT_TURN_ID,
      item: {
        type: 'subAgentActivity',
        id: 'lateral-interaction',
        kind: 'interacted',
        agentThreadId: 'child-target',
        agentPath: '/root/target',
      },
    },
  })
  await boundary.deliver(
    rawAgentMessage(
      '/root/target',
      '/root',
      [{
        type: 'input_text',
        text: (
          'Message Type: MESSAGE\n'
          + 'Task name: /root\n'
          + 'Sender: /root/target\n'
          + 'Payload:\nnot a completed response'
        ),
      }],
    ),
  )
  await boundary.deliver(
    rawAgentMessage(
      '/root/target',
      '/root',
      [{
        type: 'input_text',
        text: (
          'Message Type: FINAL_ANSWER\n'
          + 'Task name: /root\n'
          + 'Sender: /root/someone_else\n'
          + 'Payload:\nwrong sender'
        ),
      }],
    ),
  )

  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(
    state.descendants.find((entry) => entry.threadId === 'child-target')?.parentThreadId,
    ROOT_THREAD_ID,
  )
  assert.deepEqual(observed, [])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('completion keeps available child output despite irrelevant thread projection failure', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('child-b'))
  await boundary.deliver(waitItem('child-b', 'Retained completed output.'))
  boundary.failedMethods.add('thread/read')

  const completeGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'complete')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: completeGoal },
  })
  const completed = await boundary.finalizeCompletedGoalEpoch(ROOT_THREAD_ID)

  assert.equal(completed.goal?.status, 'complete')
  assert.equal(
    completed.nativeMaterialObservations.some(
      (entry) => entry.materialKind === 'output' && entry.content === 'Retained completed output.',
    ),
    true,
  )
})

test('Goal diagnostics capture active to blocked while the root turn remains active', async () => {
  const diagnostics: GoalEpochDiagnosticEvent[] = []
  const boundary = new FakeGoalBoundary({
    onGoalEpochDiagnostic: (event) => diagnostics.push(event),
  })
  await boundary.startBoundedGoalEpoch(request())
  diagnostics.length = 0

  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })

  assert.deepEqual(diagnostics, [
    {
      sequence: 2,
      kind: 'goal_status_transition',
      rootThreadId: ROOT_THREAD_ID,
      activeTurnId: ROOT_TURN_ID,
      turnId: ROOT_TURN_ID,
      priorGoalStatus: 'active',
      newGoalStatus: 'blocked',
      transitionSource: 'app_server_notification',
      transitionCategory: 'thread/goal/updated',
      appServerEventCategory: 'thread/goal/updated',
      errorCategory: null,
      errorSubtype: null,
      errorClass: null,
      errorCode: null,
      errorWillRetry: null,
      errorAssociatedWithRootTurn: null,
      sameTurnActivityAfterError: null,
      sameTurnActivityAfterBlocked: null,
      compactionEventCategory: null,
      blockedRecoveredWithoutHostIntervention: null,
      rootTurnTerminalStatus: null,
      hostTurnInterruptRequested: false,
    },
  ])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('transient blocked preserves live-root tool authority through activity and recovery', async () => {
  const calls: string[] = []
  const diagnostics: GoalEpochDiagnosticEvent[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      calls.push(String(paramsOf(call.arguments).value))
      return {
        success: true,
        contentItems: [{ type: 'inputText', text: 'preserved' }],
      }
    },
    onGoalEpochDiagnostic: (event) => diagnostics.push(event),
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))
  diagnostics.length = 0

  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })
  await boundary.deliver({
    method: 'thread/tokenUsage/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, tokenUsage: {} },
  })
  await boundary.deliver(
    dynamicToolRequest('blocked-tool-response', ROOT_THREAD_ID, 'blocked-tool-call', 'blocked'),
  )

  const recoveredGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'active')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: recoveredGoal },
  })
  await boundary.deliver(
    dynamicToolRequest('recovered-tool-response', ROOT_THREAD_ID, 'recovered-tool-call', 'active'),
  )

  assert.deepEqual(calls, ['blocked', 'active'])
  assert.deepEqual(
    boundary.responses.slice(-2).map(({ result }) => result.success),
    [true, true],
  )
  assert.equal(
    diagnostics.some(
      (event) =>
        event.kind === 'same_turn_activity_after_transition' &&
        event.turnId === ROOT_TURN_ID &&
        event.sameTurnActivityAfterBlocked,
    ),
    true,
  )
  assert.equal(
    diagnostics.some(
      (event) =>
        event.priorGoalStatus === 'blocked' &&
        event.newGoalStatus === 'active' &&
        event.activeTurnId === ROOT_TURN_ID &&
        event.blockedRecoveredWithoutHostIntervention,
    ),
    true,
  )

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('blocked root loses dynamic tool authority once its root turn is inactive', async () => {
  let toolCalls = 0
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => {
      toolCalls += 1
      return {
        success: true,
        contentItems: [{ type: 'inputText', text: 'must not execute' }],
      }
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))
  await boundary.deliver({
    method: 'turn/completed',
    params: {
      threadId: ROOT_THREAD_ID,
      turn: { id: ROOT_TURN_ID, status: 'completed' },
    },
  })
  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })

  await boundary.deliver(
    dynamicToolRequest('inactive-tool-response', ROOT_THREAD_ID, 'inactive-tool-call', 'inactive'),
  )

  assert.equal(toolCalls, 0)
  assert.equal(boundary.responses.at(-1)?.result.success, false)
  assert.match(
    boundary.responses.at(-1)?.result.contentItems[0]?.text ?? '',
    /outside the active Goal authority/,
  )
  const observed = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(observed.goal?.status, 'blocked')
  assert.equal(observed.activeTurnId, null)

  await boundary.finalizeCompletedGoalEpoch(ROOT_THREAD_ID)
})

test('transient blocked authorizes only the exact live root turn', async () => {
  const calls: string[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      calls.push(String(paramsOf(call.arguments).value))
      return {
        success: true,
        contentItems: [{ type: 'inputText', text: 'preserved' }],
      }
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))
  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })

  await boundary.deliver(
    dynamicToolRequest(
      'late-turn-tool-response',
      ROOT_THREAD_ID,
      'late-turn-tool-call',
      'late',
      DYNAMIC_TOOL.name,
      'prior-root-turn',
    ),
  )
  await boundary.deliver(
    dynamicToolRequest('live-turn-tool-response', ROOT_THREAD_ID, 'live-turn-tool-call', 'live'),
  )

  assert.deepEqual(calls, ['live'])
  assert.equal(boundary.responses.at(-2)?.result.success, false)
  assert.match(
    boundary.responses.at(-2)?.result.contentItems[0]?.text ?? '',
    /outside the active Goal authority/,
  )
  assert.equal(boundary.responses.at(-1)?.result.success, true)

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('Goal diagnostics retain the causal notification when readback observes blocked first', async () => {
  const diagnostics: GoalEpochDiagnosticEvent[] = []
  const boundary = new FakeGoalBoundary({
    onGoalEpochDiagnostic: (event) => diagnostics.push(event),
  })
  await boundary.startBoundedGoalEpoch(request())
  diagnostics.length = 0

  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.readGoalEpochState(ROOT_THREAD_ID)
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })

  assert.deepEqual(
    diagnostics.map((event) => ({
      sequence: event.sequence,
      prior: event.priorGoalStatus,
      next: event.newGoalStatus,
      source: event.transitionSource,
      activeTurnId: event.activeTurnId,
      turnId: event.turnId,
    })),
    [
      {
        sequence: 2,
        prior: 'active',
        next: 'blocked',
        source: 'readback',
        activeTurnId: ROOT_TURN_ID,
        turnId: null,
      },
      {
        sequence: 3,
        prior: 'active',
        next: 'blocked',
        source: 'app_server_notification',
        activeTurnId: ROOT_TURN_ID,
        turnId: ROOT_TURN_ID,
      },
    ],
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('Goal diagnostics capture blocked after the root turn becomes inactive', async () => {
  const diagnostics: GoalEpochDiagnosticEvent[] = []
  const boundary = new FakeGoalBoundary({
    onGoalEpochDiagnostic: (event) => diagnostics.push(event),
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver({
    method: 'turn/completed',
    params: {
      threadId: ROOT_THREAD_ID,
      turn: { id: ROOT_TURN_ID, status: 'failed' },
    },
  })
  diagnostics.length = 0

  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })

  assert.equal(diagnostics.length, 1)
  assert.equal(diagnostics[0]!.kind, 'goal_status_transition')
  assert.equal(diagnostics[0]!.priorGoalStatus, 'active')
  assert.equal(diagnostics[0]!.newGoalStatus, 'blocked')
  assert.equal(diagnostics[0]!.activeTurnId, null)
  assert.equal(diagnostics[0]!.turnId, ROOT_TURN_ID)
  await boundary.finalizeCompletedGoalEpoch(ROOT_THREAD_ID)
})

test('Goal diagnostics preserve blocked, Error, compaction, and continued-activity order safely', async () => {
  const diagnostics: GoalEpochDiagnosticEvent[] = []
  const boundary = new FakeGoalBoundary({
    onGoalEpochDiagnostic: (event) => diagnostics.push(event),
  })
  await boundary.startBoundedGoalEpoch(request())
  diagnostics.length = 0

  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })
  await boundary.deliver({
    method: 'error',
    params: {
      threadId: ROOT_THREAD_ID,
      turnId: ROOT_TURN_ID,
      willRetry: false,
      error: {
        message: 'private provider text must not be retained',
        additionalDetails: { raw: 'private payload' },
        codexErrorInfo: { responseStreamDisconnected: { httpStatusCode: 503 } },
      },
    },
  })
  await boundary.deliver({
    method: 'item/started',
    params: {
      threadId: ROOT_THREAD_ID,
      turnId: ROOT_TURN_ID,
      item: { id: 'private-item-id', type: 'contextCompaction' },
    },
  })
  await boundary.deliver({
    method: 'thread/tokenUsage/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, tokenUsage: {} },
  })

  assert.deepEqual(
    diagnostics.map((event) => [event.sequence, event.kind]),
    [
      [2, 'goal_status_transition'],
      [3, 'app_server_error'],
      [4, 'context_management'],
      [5, 'same_turn_activity_after_transition'],
    ],
  )
  assert.deepEqual(
    diagnostics.find((event) => event.kind === 'app_server_error'),
    {
      sequence: 3,
      kind: 'app_server_error',
      rootThreadId: ROOT_THREAD_ID,
      activeTurnId: ROOT_TURN_ID,
      turnId: ROOT_TURN_ID,
      priorGoalStatus: null,
      newGoalStatus: null,
      transitionSource: null,
      transitionCategory: null,
      appServerEventCategory: 'error',
      errorCategory: 'app_server_turn_error',
      errorSubtype: 'responseStreamDisconnected',
      errorClass: 'TurnError',
      errorCode: 503,
      errorWillRetry: false,
      errorAssociatedWithRootTurn: true,
      sameTurnActivityAfterError: null,
      sameTurnActivityAfterBlocked: null,
      compactionEventCategory: null,
      blockedRecoveredWithoutHostIntervention: null,
      rootTurnTerminalStatus: null,
      hostTurnInterruptRequested: false,
    },
  )
  const activity = diagnostics.at(-1)!
  assert.equal(activity.appServerEventCategory, 'thread/tokenUsage/updated')
  assert.equal(activity.sameTurnActivityAfterError, true)
  assert.equal(activity.sameTurnActivityAfterBlocked, true)
  assert.equal(diagnostics[2]!.compactionEventCategory, 'context_compaction_started')
  const serialized = JSON.stringify(diagnostics)
  assert.doesNotMatch(serialized, /private provider text|private payload|private-item-id/)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('Goal diagnostics distinguish Host mutations from turn-attributed App Server transitions', async () => {
  const diagnostics: GoalEpochDiagnosticEvent[] = []
  const boundary = new FakeGoalBoundary({
    onGoalEpochDiagnostic: (event) => diagnostics.push(event),
  })
  await boundary.startBoundedGoalEpoch(request())

  const hostActivation = diagnostics.find(
    (event) => event.transitionCategory === 'thread/goal/set',
  )!
  assert.equal(hostActivation.transitionSource, 'host_issued_mutation')
  assert.equal(hostActivation.turnId, null)

  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })
  const recoveredGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'active')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: null, goal: recoveredGoal },
  })

  const appServerBlocked = diagnostics.find((event) => event.newGoalStatus === 'blocked')!
  assert.equal(appServerBlocked.transitionSource, 'app_server_notification')
  assert.equal(appServerBlocked.turnId, ROOT_TURN_ID)
  const externallyOriginatedRecovery = diagnostics.find(
    (event) => event.priorGoalStatus === 'blocked' && event.newGoalStatus === 'active',
  )!
  assert.equal(externallyOriginatedRecovery.transitionSource, 'app_server_notification')
  assert.equal(externallyOriginatedRecovery.turnId, null)
  assert.equal(externallyOriginatedRecovery.blockedRecoveredWithoutHostIntervention, true)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('Goal diagnostics do not call recovery unassisted after a Host interrupt request', async () => {
  const diagnostics: GoalEpochDiagnosticEvent[] = []
  const boundary = new FakeGoalBoundary({
    onGoalEpochDiagnostic: (event) => diagnostics.push(event),
  })
  await boundary.startBoundedGoalEpoch(request())
  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: ROOT_TURN_ID, goal: blockedGoal },
  })

  const interrupt = boundary.blockMethod('turn/interrupt')
  const stopping = boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  await waitUntil(
    () => boundary.requests.some((entry) => entry.method === 'turn/interrupt'),
    'Host root turn interrupt request',
  )
  const recoveredGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'active')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, turnId: null, goal: recoveredGoal },
  })

  const recovery = diagnostics.find(
    (event) => event.priorGoalStatus === 'blocked' && event.newGoalStatus === 'active',
  )!
  assert.equal(recovery.blockedRecoveredWithoutHostIntervention, false)
  assert.equal(
    diagnostics.some(
      (event) =>
        event.kind === 'host_turn_interrupt_requested' &&
        event.turnId === ROOT_TURN_ID &&
        event.hostTurnInterruptRequested,
    ),
    true,
  )

  interrupt.resolve()
  await stopping
})

test('successful finalization accepts a blocked Goal after its root turn is inactive', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const blockedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'blocked')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: blockedGoal },
  })
  await boundary.deliver({
    method: 'turn/completed',
    params: {
      threadId: ROOT_THREAD_ID,
      turn: { id: ROOT_TURN_ID, status: 'completed' },
    },
  })

  const observed = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(observed.goal?.status, 'blocked')
  assert.equal(observed.activeTurnId, null)

  const finalized = await boundary.finalizeCompletedGoalEpoch(ROOT_THREAD_ID)
  assert.equal(finalized.goal?.status, 'blocked')
  assert.equal(finalized.activeTurnId, null)
})

test('dynamic root effects are executed once and replayed from the internal dedup cache', async () => {
  const calls: string[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      calls.push(String(paramsOf(call.arguments).value))
      return {
        success: true,
        contentItems: [{ type: 'inputText', text: 'preserved' }],
      }
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))

  await boundary.deliver(dynamicToolRequest('server-1', ROOT_THREAD_ID, 'effect-1', 'alpha'))
  await boundary.deliver(dynamicToolRequest('server-2', ROOT_THREAD_ID, 'effect-1', 'alpha'))

  assert.deepEqual(calls, ['alpha'])
  assert.equal(boundary.responses.length, 2)
  assert.deepEqual(boundary.responses[0]?.result, boundary.responses[1]?.result)

  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('owner checkpoint response closes admission and contains the Goal before fresh collaboration enters custody', async () => {
  const calls: string[] = []
  const observations: GoalEpochNativeMaterialObservation[] = []
  const stopContexts: GoalEpochStopContext[] = []
  const lifecycleEvents: string[] = []
  let missionFenceCalls = 0
  const existingChildThreadId = 'existing-before-checkpoint'
  const freshChildThreadId = 'fresh-after-checkpoint'
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      calls.push(String(paramsOf(call.arguments).value))
      return {
        success: true,
        contentItems: [{ type: 'inputText', text: 'checkpointed' }],
        terminalHandoff: 'owner_checkpoint',
      }
    },
    onNativeMaterialObserved: async (observation) => {
      observations.push(observation)
    },
    onGoalTermination: async (context) => {
      lifecycleEvents.push('termination')
      stopContexts.push(context)
    },
    onMissionFence: async () => {
      missionFenceCalls += 1
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))
  await boundary.deliver(spawnItem(existingChildThreadId))
  boundary.afterServerResponse = (requestId) => {
    if (requestId !== 'checkpoint-response') {
      return
    }
    lifecycleEvents.push('response')
    boundary.childTree.set(ROOT_THREAD_ID, [existingChildThreadId, freshChildThreadId])
    void boundary.deliver(spawnItem(freshChildThreadId))
    void boundary.deliver({
      method: 'thread/started',
      params: {
        thread: { id: freshChildThreadId, parentThreadId: ROOT_THREAD_ID },
      },
    })
    void boundary.deliver(sendInputItem(existingChildThreadId, 'Do fresh work after the checkpoint.'))
    void boundary.deliver(waitItem(existingChildThreadId, 'Late authentic output.'))
    void boundary.deliver(waitItem(freshChildThreadId, 'Fresh post-checkpoint output.'))
  }

  await boundary.deliver(
    dynamicToolRequest('checkpoint-response', ROOT_THREAD_ID, 'checkpoint-call', 'checkpoint'),
  )
  await new Promise<void>((resolve) => setImmediate(resolve))
  const stopped = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'owner_checkpoint')

  assert.deepEqual(calls, ['checkpoint'])
  assert.deepEqual(Object.keys(boundary.responses[0]?.result ?? {}).sort(), [
    'contentItems',
    'success',
  ])
  assert.equal(boundary.responses[0]?.result.success, true)
  assert.deepEqual(
    observations.map(({ materialKind, childThreadId, content }) => ({
      materialKind,
      childThreadId,
      content,
    })),
    [
      {
        materialKind: 'assignment',
        childThreadId: existingChildThreadId,
        content: 'Investigate the assigned mathematical route.',
      },
      {
        materialKind: 'output',
        childThreadId: existingChildThreadId,
        content: 'Late authentic output.',
      },
    ],
  )
  assert.deepEqual(lifecycleEvents, ['response', 'termination'])
  assert.deepEqual(stopContexts.map(({ reason }) => reason), ['owner_checkpoint'])
  assert.equal(stopContexts[0]?.containmentScope, 'goal_local')
  assert.equal(missionFenceCalls, 0)
  assert.equal(stopped.goal?.status, 'complete')
  assert.equal(stopped.descendantsContained, 2)
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === freshChildThreadId,
    ),
    true,
  )
  assert.equal(
    boundary.requests.filter(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ).length,
    1,
  )
  await boundary.deliver(
    dynamicToolRequest('checkpoint-replay', ROOT_THREAD_ID, 'checkpoint-call', 'checkpoint'),
  )
  assert.equal(boundary.responses.at(-1)?.result.success, false)
  assert.deepEqual(calls, ['checkpoint'])
  assert.deepEqual(stopContexts.map(({ reason }) => reason), ['owner_checkpoint'])
})

test('fresh recovery treats the exact archived checkpoint root as already contained', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  boundary.threadListPageOverride = (params) => {
    assert.equal(params.archived, true)
    if (params.cursor === undefined) {
      return {
        data: [{ id: 'unrelated-archived-root', parentThreadId: null, cwd: WORKSPACE }],
        nextCursor: 'archived-page-2',
      }
    }
    assert.equal(params.cursor, 'archived-page-2')
    return {
      data: [{ id: ROOT_THREAD_ID, parentThreadId: null, cwd: WORKSPACE }],
      nextCursor: null,
    }
  }

  const stopped = await boundary.stopBoundedGoalEpoch(
    ROOT_THREAD_ID,
    'owner_checkpoint',
    { phase: 'registered', expectedObjective: request().objective },
  )

  assert.deepEqual(stopContexts.map(({ threadId, reason }) => ({ threadId, reason })), [
    { threadId: ROOT_THREAD_ID, reason: 'owner_checkpoint' },
  ])
  assert.equal(stopped.threadId, ROOT_THREAD_ID)
  assert.equal(stopped.appServerTerminated, false)
  assert.equal(
    boundary.requests.some(({ method }) => method === 'thread/resume'),
    false,
  )
  assert.equal(
    boundary.requests.some(({ method }) => method === 'thread/unarchive'),
    false,
  )
  assert.equal(
    boundary.requests.some(({ method }) => method === 'thread/archive'),
    false,
  )
})

test('fresh dead-runner recovery treats the exact archived root as already contained', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  boundary.threadListPageOverride = (params) => {
    assert.equal(params.archived, true)
    return {
      data: [{ id: ROOT_THREAD_ID, parentThreadId: null, cwd: WORKSPACE }],
      nextCursor: null,
    }
  }

  const stopped = await boundary.stopBoundedGoalEpoch(
    ROOT_THREAD_ID,
    'dead_runner_recovery',
    { phase: 'registered', expectedObjective: request().objective },
  )

  assert.deepEqual(stopContexts.map(({ threadId, reason }) => ({ threadId, reason })), [
    { threadId: ROOT_THREAD_ID, reason: 'dead_runner_recovery' },
  ])
  assert.equal(stopped.threadId, ROOT_THREAD_ID)
  assert.equal(stopped.appServerTerminated, false)
  assert.equal(
    boundary.requests.some(({ method }) => method === 'thread/resume'),
    false,
  )
  assert.equal(
    boundary.requests.some(({ method }) => method === 'thread/unarchive'),
    false,
  )
  assert.equal(
    boundary.requests.some(({ method }) => method === 'thread/archive'),
    false,
  )
})

test('archived recovery rejects a root from the wrong workspace without resuming it', async () => {
  for (const reason of ['owner_checkpoint', 'dead_runner_recovery'] as const) {
    const boundary = new FakeGoalBoundary()
    boundary.threadListPageOverride = () => ({
      data: [{ id: ROOT_THREAD_ID, parentThreadId: null, cwd: path.join(WORKSPACE, 'other') }],
      nextCursor: null,
    })

    await assert.rejects(
      boundary.stopBoundedGoalEpoch(
        ROOT_THREAD_ID,
        reason,
        { phase: 'registered', expectedObjective: request().objective },
      ),
      /archived Goal differs from its durable workspace/,
    )
    assert.equal(
      boundary.requests.some(({ method }) => method === 'thread/resume'),
      false,
    )
  }
})

test('dynamic tool results larger than four MiB are returned without a repository byte ceiling', async () => {
  const largeResult = 'x'.repeat(4 * 1024 * 1024 + 1)
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({
      success: true,
      contentItems: [{ type: 'inputText', text: largeResult }],
    }),
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))

  await boundary.deliver(dynamicToolRequest('large-result-request', ROOT_THREAD_ID, 'large-result', 'x'))

  assert.equal(boundary.responses[0]?.result.success, true)
  assert.equal(boundary.responses[0]?.result.contentItems[0]?.text.length, largeResult.length)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('operator stop aborts an active dynamic owner operation before waiting for the message queue', async () => {
  let operationSignal: AbortSignal | null = null
  let missionFenceCalls = 0
  let resolveStarted!: () => void
  const started = new Promise<void>((resolve) => {
    resolveStarted = resolve
  })
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (_call, context) => {
      operationSignal = context.signal
      resolveStarted()
      return new Promise<DynamicToolCallResult>((_resolve, reject) => {
        const abort = (): void => {
          reject(
            context.signal.reason instanceof Error
              ? context.signal.reason
              : new Error('dynamic owner operation aborted'),
          )
        }
        if (context.signal.aborted) {
          abort()
        } else {
          context.signal.addEventListener('abort', abort, { once: true })
        }
      })
    },
    onMissionFence: async () => {
      missionFenceCalls += 1
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))

  const delivery = boundary.deliver(
    dynamicToolRequest('cancelled-owner-request', ROOT_THREAD_ID, 'cancelled-owner-call', 'x'),
  )
  await started
  const stopped = boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')

  await Promise.all([delivery, stopped])
  assert.equal((operationSignal as AbortSignal | null)?.aborted, true)
  assert.equal(boundary.responses[0]?.result.success, false)
  assert.equal(missionFenceCalls, 0)
})

test('operator stop aborts active native-material custody before waiting for the message queue', async () => {
  let operationSignal: AbortSignal | null = null
  let missionFenceCalls = 0
  let resolveStarted!: () => void
  const started = new Promise<void>((resolve) => {
    resolveStarted = resolve
  })
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (_observation, context) => {
      operationSignal = context.signal
      resolveStarted()
      await new Promise<void>((_resolve, reject) => {
        const abort = (): void => {
          reject(
            context.signal.reason instanceof Error
              ? context.signal.reason
              : new Error('native-material custody aborted'),
          )
        }
        if (context.signal.aborted) {
          abort()
        } else {
          context.signal.addEventListener('abort', abort, { once: true })
        }
      })
    },
    onMissionFence: async () => {
      missionFenceCalls += 1
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  const delivery = boundary.deliver(spawnItem('cancelled-material-child'))
  await started
  const stopped = boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')

  await Promise.all([delivery, stopped])
  assert.equal((operationSignal as AbortSignal | null)?.aborted, true)
  assert.equal(missionFenceCalls, 0)
})

test('changed replay identity contains the unknown effect without executing it twice', async () => {
  const calls: string[] = []
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      calls.push(String(paramsOf(call.arguments).value))
      return {
        success: true,
        contentItems: [{ type: 'inputText', text: 'preserved' }],
      }
    },
    onMissionFence: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))

  await boundary.deliver(dynamicToolRequest('server-1', ROOT_THREAD_ID, 'effect-1', 'alpha'))
  await boundary.deliver(dynamicToolRequest('server-2', ROOT_THREAD_ID, 'effect-1', 'changed'))
  await boundary.waitForFatalBoundaryFence()

  assert.deepEqual(calls, ['alpha'])
  assert.equal(boundary.responses[1]?.result.success, false)
  assert.match(boundary.responses[1]?.result.contentItems[0]?.text ?? '', /changed on replay/)
  assert.equal(stopContexts[0]?.reason, 'unknown_effect')
  assert.equal(stopContexts[0]?.containmentScope, 'mission_fence')
  assert.match(boundary.getReadyReason() ?? '', /contained a fatal condition/)
})

test('root calls to every descendant-only tool family are denied locally', async () => {
  let handlerCalls = 0
  let missionFenceCalls = 0
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => {
      handlerCalls += 1
      return { success: true, contentItems: [{ type: 'inputText', text: 'unexpected' }] }
    },
    onMissionFence: async () => {
      missionFenceCalls += 1
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))

  await boundary.deliver(
    dynamicToolRequest('root-history-alias', ROOT_THREAD_ID, 'root-history-call', 'x', HISTORY_TOOL_NAME),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'root-history-page-alias',
      ROOT_THREAD_ID,
      'root-history-page-call',
      'x',
      HISTORY_PAGE_TOOL_NAME,
    ),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'root-a1-review-alias',
      ROOT_THREAD_ID,
      'root-a1-review-call',
      'x',
      A1_REVIEW_TOOL_NAME,
    ),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'root-a1-review-page-alias',
      ROOT_THREAD_ID,
      'root-a1-review-page-call',
      'x',
      A1_REVIEW_PAGE_TOOL_NAME,
    ),
  )

  for (const tool of [
    RESEARCH_READ_TOOL_NAME,
    RESEARCH_READ_PAGE_TOOL_NAME,
    ADMISSION_TOOL_NAME,
    ADMISSION_PAGE_TOOL_NAME,
  ]) {
    await boundary.deliver(
      dynamicToolRequest(`root-${tool}`, ROOT_THREAD_ID, `call-${tool}`, 'x', tool),
    )
  }

  assert.equal(handlerCalls, 0)
  assert.equal(missionFenceCalls, 0)
  assert.equal(boundary.responses.length, 8)
  assert.equal(boundary.responses.every(({ result }) => result.success === false), true)
  assert.equal(
    boundary.responses.every(({ result }) =>
      /outside the active Goal authority/.test(result.contentItems[0]?.text ?? '')
    ),
    true,
  )
  assert.doesNotMatch(boundary.getReadyReason() ?? '', /contained a fatal condition/)
})

test('an exact A1 review grant admits its closed tool family and preserves review output', async () => {
  const contexts: GoalEpochOperationContext[] = []
  const observed: GoalEpochNativeMaterialObservation[] = []
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  const handlerCalls: string[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call, context) => {
      contexts.push(context)
      handlerCalls.push(`${call.tool}:${String(paramsOf(call.arguments).value)}`)
      return { success: true, contentItems: [{ type: 'inputText', text: 'reviewed' }] }
    },
    onNativeMaterialObserved: async (material) => {
      observed.push(material)
    },
    onDescendantToolGrantRevoked: async (event) => {
      revocations.push(event)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('a1-review-child'))
  await boundary.deliver(nativeChildStarted('a1-review-child', 'rh_restricted_review'))

  const installed = boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'a1-review-child',
    grantId: 'grant.a1-review-child.1',
    assignmentId: 'assignment.a1-review-child.1',
    allowedToolNames: [A1_REVIEW_PAGE_TOOL_NAME, A1_REVIEW_TOOL_NAME],
  })
  assert.deepEqual(installed.allowedToolNames, [A1_REVIEW_TOOL_NAME, A1_REVIEW_PAGE_TOOL_NAME])
  await boundary.deliver({
    method: 'turn/started',
    params: { threadId: 'a1-review-child', turn: { id: 'turn.a1-review-child' } },
  })

  await boundary.deliver(
    dynamicToolRequest(
      'a1-review',
      'a1-review-child',
      'a1-review-call',
      'candidate',
      A1_REVIEW_TOOL_NAME,
      'turn.a1-review-child',
    ),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'a1-review-replay',
      'a1-review-child',
      'a1-review-call',
      'candidate',
      A1_REVIEW_TOOL_NAME,
      'turn.a1-review-child',
    ),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'a1-review-changed-replay',
      'a1-review-child',
      'a1-review-call',
      'changed',
      A1_REVIEW_TOOL_NAME,
      'turn.a1-review-child',
    ),
  )
  for (const [requestId, callId, tool] of [
    ['a1-review-arbitrary', 'a1-review-arbitrary-call', DYNAMIC_TOOL.name],
    ['a1-review-grant-alias', 'a1-review-grant-call', A1_REVIEW_GRANT_TOOL_NAME],
    ['a1-review-history-alias', 'a1-review-history-call', HISTORY_TOOL_NAME],
  ] as const) {
    await boundary.deliver(
      dynamicToolRequest(
        requestId,
        'a1-review-child',
        callId,
        'x',
        tool,
        'turn.a1-review-child',
      ),
    )
  }

  const finalOutput = 'Concrete preserved defect for the exact frozen A1 Candidate.'
  await boundary.deliver({
    method: 'rawResponseItem/completed',
    params: {
      threadId: 'a1-review-child',
      turnId: 'turn.a1-review-child',
      item: {
        type: 'message',
        role: 'assistant',
        phase: 'final_answer',
        content: [{ type: 'output_text', text: finalOutput }],
      },
    },
  })
  await boundary.deliver({
    method: 'turn/completed',
    params: {
      threadId: 'a1-review-child',
      turn: { id: 'turn.a1-review-child', status: 'completed' },
    },
  })
  await boundary.deliver(
    dynamicToolRequest(
      'a1-review-after-completion',
      'a1-review-child',
      'a1-review-after-completion-call',
      'candidate',
      A1_REVIEW_TOOL_NAME,
      'turn.a1-review-child',
    ),
  )

  assert.deepEqual(handlerCalls, [`${A1_REVIEW_TOOL_NAME}:candidate`])
  assert.deepEqual(
    boundary.responses.map(({ result }) => result.success),
    [true, true, false, false, false, false, false],
  )
  assert.match(boundary.responses[2]?.result.contentItems[0]?.text ?? '', /changed on replay/)
  assert.equal(contexts[0]?.rootThreadId, ROOT_THREAD_ID)
  assert.equal(contexts[0]?.callerThreadId, 'a1-review-child')
  assert.equal(contexts[0]?.parentThreadId, ROOT_THREAD_ID)
  assert.equal(contexts[0]?.depth, 1)
  assert.equal(contexts[0]?.grantId, 'grant.a1-review-child.1')
  assert.equal(contexts[0]?.assignmentId, 'assignment.a1-review-child.1')
  assert.deepEqual(
    observed.filter(({ materialKind }) => materialKind === 'output').map(({ content }) => content),
    [finalOutput],
  )
  assert.deepEqual(
    revocations.map(({ grantId, reason }) => ({ grantId, reason })),
    [{ grantId: 'grant.a1-review-child.1', reason: 'child_turn_completed' }],
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

const GRANT_FAMILY_CASES = [
  { name: 'history', role: 'rh_historical', tools: [HISTORY_TOOL_NAME, HISTORY_PAGE_TOOL_NAME] },
  { name: 'a1', role: 'rh_restricted_review', tools: [A1_REVIEW_TOOL_NAME, A1_REVIEW_PAGE_TOOL_NAME] },
  { name: 'research', role: 'rh_researcher', tools: [RESEARCH_READ_TOOL_NAME, RESEARCH_READ_PAGE_TOOL_NAME] },
  { name: 'admission', role: 'rh_restricted_review', tools: [ADMISSION_TOOL_NAME, ADMISSION_PAGE_TOOL_NAME] },
] as const

test('research and Admission aliases require an exact active direct child and its own grant', async () => {
  const calls: string[] = []
  const contexts: GoalEpochOperationContext[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call, context) => {
      calls.push(call.tool)
      contexts.push(context)
      return { success: true, contentItems: [{ type: 'inputText', text: 'bounded result' }] }
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
  for (const entry of GRANT_FAMILY_CASES.slice(2)) {
    const child = entry.name + '-reader'
    await boundary.deliver(spawnItem(child))
    await boundary.deliver(nativeChildStarted(child, entry.role))
    await boundary.deliver(spawnItem(child + '-sibling'))
    await boundary.deliver(nestedSpawnItem(child, child + '-leaf'))
    assert.throws(() => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID, childThreadId: child + '-leaf',
      grantId: 'grant.leaf', assignmentId: 'assignment.leaf', allowedToolNames: entry.tools,
    }), /not one exact live direct child/)
    assert.throws(() => boundary.installDescendantToolGrant({
      rootThreadId: 'another-root', childThreadId: child,
      grantId: 'grant.wrong-root', assignmentId: 'assignment.wrong-root', allowedToolNames: entry.tools,
    }), /not one exact live direct child/)

    const installed = boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID, childThreadId: child,
      grantId: 'grant.' + child, assignmentId: 'assignment.' + child,
      allowedToolNames: [...entry.tools].reverse(),
    })
    assert.deepEqual(installed.allowedToolNames, entry.tools)
    await boundary.deliver(dynamicToolRequest('pending.' + child, child, 'pending', 'x', entry.tools[0], 'turn.' + child))
    assert.equal(boundary.responses.at(-1)?.result.success, false)
    for (const target of [child, child + '-sibling', child + '-leaf']) {
      await boundary.deliver({
        method: 'turn/started', params: { threadId: target, turn: { id: 'turn.' + target } },
      })
    }

    for (const [target, turn, tool] of [
      [child, 'stale-turn', entry.tools[0]],
      [child + '-sibling', 'turn.' + child + '-sibling', entry.tools[0]],
      [child + '-leaf', 'turn.' + child + '-leaf', entry.tools[0]],
      [child, 'turn.' + child, HISTORY_TOOL_NAME],
      [child, 'turn.' + child, A1_REVIEW_TOOL_NAME],
      [child, 'turn.' + child, entry.name === 'research' ? ADMISSION_TOOL_NAME : RESEARCH_READ_TOOL_NAME],
      [child, 'turn.' + child, ROOT_MISSION_TOOL_NAME],
      [child, 'turn.' + child, FORMAL_TOOL_NAME],
      [child, 'turn.' + child, RESEARCH_READ_GRANT_TOOL_NAME],
      [child, 'turn.' + child, ADMISSION_GRANT_TOOL_NAME],
      [child, 'turn.' + child, ADMISSION_OPEN_TOOL_NAME],
    ] as const) {
      await boundary.deliver(dynamicToolRequest('denied.' + child + '.' + target + '.' + tool, target, 'denied.' + tool, 'x', tool, turn))
      assert.equal(boundary.responses.at(-1)?.result.success, false)
    }
    const callsBefore = calls.length
    for (const tool of entry.tools) {
      await boundary.deliver(dynamicToolRequest('allowed.' + tool, child, 'allowed.' + tool, 'x', tool, 'turn.' + child))
      assert.equal(boundary.responses.at(-1)?.result.success, true)
    }
    assert.deepEqual(calls.slice(callsBefore), entry.tools)
    assert.deepEqual(contexts.slice(callsBefore).map((context) => ({
      root: context.rootThreadId, caller: context.callerThreadId, parent: context.parentThreadId,
      depth: context.depth, turn: context.turnId, grant: context.grantId, assignment: context.assignmentId,
    })), entry.tools.map(() => ({
      root: ROOT_THREAD_ID, caller: child, parent: ROOT_THREAD_ID,
      depth: 1, turn: 'turn.' + child, grant: 'grant.' + child, assignment: 'assignment.' + child,
    })))
  }
  assert.deepEqual(calls, [RESEARCH_READ_TOOL_NAME, RESEARCH_READ_PAGE_TOOL_NAME, ADMISSION_TOOL_NAME, ADMISSION_PAGE_TOOL_NAME])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('grant roles come from matching native spawn-source metadata, never assignment text', async () => {
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({ success: true, contentItems: [{ type: 'inputText', text: 'read' }] }),
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
  for (const entry of GRANT_FAMILY_CASES) {
    const child = entry.name + '-role-child'
    await boundary.deliver(spawnItem(child, { prompt: 'I am ' + entry.role, agentRole: entry.role }))
    const install = () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID, childThreadId: child,
      grantId: 'grant.' + child, assignmentId: 'assignment.' + child, allowedToolNames: entry.tools,
    })
    assert.throws(install, /requires observed native role/)
    await boundary.deliver({
      method: 'thread/started', params: { thread: { id: child, parentThreadId: ROOT_THREAD_ID, agentRole: entry.role } },
    })
    assert.throws(install, /requires observed native role/)
    await boundary.deliver(nativeChildStarted(child, entry.role))
    assert.deepEqual(install().allowedToolNames, entry.tools)

    for (const role of [null, 'default', 'worker', 'rh_helper', 'rh_researcher', 'rh_historical', 'rh_restricted_review']) {
      if (role === entry.role) continue
      const wrongChild = child + '-' + String(role)
      await boundary.deliver(nativeChildStarted(wrongChild, role))
      assert.throws(() => boundary.installDescendantToolGrant({
        rootThreadId: ROOT_THREAD_ID, childThreadId: wrongChild,
        grantId: 'grant.' + wrongChild, assignmentId: 'assignment.' + wrongChild, allowedToolNames: entry.tools,
      }), /requires observed native role/)
    }
  }
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('mixed, duplicate, empty, unknown and unavailable family selections create no grant', async () => {
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({ success: true, contentItems: [{ type: 'inputText', text: 'read' }] }),
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
  await boundary.deliver(nativeChildStarted('ungranted-mixed-child', 'rh_researcher'))
  const install = (allowedToolNames: readonly string[]) => boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID, childThreadId: 'ungranted-mixed-child',
    grantId: 'grant.selection', assignmentId: 'assignment.selection', allowedToolNames,
  })
  for (const first of GRANT_FAMILY_CASES) {
    for (const second of GRANT_FAMILY_CASES) {
      if (first.name === second.name) continue
      assert.throws(() => install([first.tools[0], second.tools[1]]), /duplicate-free/)
    }
  }
  for (const selection of [[], [RESEARCH_READ_TOOL_NAME, RESEARCH_READ_TOOL_NAME], [RESEARCH_READ_GRANT_TOOL_NAME], ['unlisted_read']]) {
    assert.throws(() => install(selection), /duplicate-free/)
  }
  assert.deepEqual(install([RESEARCH_READ_PAGE_TOOL_NAME]).allowedToolNames, [RESEARCH_READ_PAGE_TOOL_NAME])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')

  const limited = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({ success: true, contentItems: [{ type: 'inputText', text: 'read' }] }),
  })
  await limited.startBoundedGoalEpoch(request({ dynamicTools: [stringValueTool(RESEARCH_READ_TOOL_NAME)] }))
  await limited.deliver(nativeChildStarted('limited-child', 'rh_researcher'))
  assert.throws(() => limited.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID, childThreadId: 'limited-child',
    grantId: 'grant.unavailable', assignmentId: 'assignment.unavailable',
    allowedToolNames: [RESEARCH_READ_TOOL_NAME, RESEARCH_READ_PAGE_TOOL_NAME],
  }), /outside the root Goal catalog/)
  assert.deepEqual(limited.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID, childThreadId: 'limited-child',
    grantId: 'grant.available', assignmentId: 'assignment.available', allowedToolNames: [RESEARCH_READ_TOOL_NAME],
  }).allowedToolNames, [RESEARCH_READ_TOOL_NAME])
  await limited.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('revocation preserves every child family while a later active turn permits a fresh same-family grant', async () => {
  const calls: string[] = []
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      calls.push(call.tool)
      return { success: true, contentItems: [{ type: 'inputText', text: 'same family' }] }
    },
    onDescendantToolGrantRevoked: async (event) => { revocations.push(event) },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
  for (const entry of GRANT_FAMILY_CASES) {
    const child = entry.name + '-retained-family'
    await boundary.deliver(nativeChildStarted(child, entry.role))
    const install = (suffix: string, allowedToolNames: readonly string[]) => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID, childThreadId: child,
      grantId: 'grant.' + child + suffix, assignmentId: 'assignment.' + child + suffix, allowedToolNames,
    })
    install('.first', entry.tools)
    for (const other of GRANT_FAMILY_CASES) {
      if (other.name === entry.name) continue
      assert.throws(() => install('.live-cross', other.tools), /already has a different/)
    }
    await boundary.deliver({ method: 'turn/started', params: { threadId: child, turn: { id: 'turn.first.' + child } } })
    await boundary.deliver({
      method: 'turn/completed', params: { threadId: child, turn: { id: 'turn.first.' + child, status: 'completed' } },
    })
    assert.throws(() => install('.terminal', entry.tools), /not one exact live direct child/)
    await boundary.deliver({ method: 'turn/started', params: { threadId: child, turn: { id: 'turn.next.' + child } } })
    await boundary.deliver(dynamicToolRequest('ungranted.' + child, child, 'old-grant-call', 'x', entry.tools[0], 'turn.next.' + child))
    assert.equal(boundary.responses.at(-1)?.result.success, false)
    for (const other of GRANT_FAMILY_CASES) {
      if (other.name === entry.name) continue
      assert.throws(() => install('.revoked-cross', other.tools), /remains bound to its .* family after grant revocation/)
    }
    install('.next', entry.tools)
    await boundary.deliver(dynamicToolRequest('regranted.' + child, child, 'new-grant-call', 'x', entry.tools[0], 'turn.next.' + child))
    assert.equal(boundary.responses.at(-1)?.result.success, true)
  }
  assert.deepEqual(calls, GRANT_FAMILY_CASES.map((entry) => entry.tools[0]))
  assert.deepEqual(revocations.map(({ reason }) => reason), GRANT_FAMILY_CASES.map(() => 'child_turn_completed'))
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('native child role and parent metadata cannot be changed to obtain another family', async () => {
  for (const conflict of ['role-change', 'absent-role-change', 'role-projection', 'parent-projection']) {
    const boundary = new FakeGoalBoundary({
      dynamicToolHandler: async () => ({ success: true, contentItems: [{ type: 'inputText', text: 'read' }] }),
    })
    await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
    await boundary.deliver(nativeChildStarted('identity-child', conflict === 'absent-role-change' ? null : 'rh_researcher'))
    const event = nativeChildStarted('identity-child', conflict === 'role-change' ? 'rh_restricted_review' : 'rh_researcher')
    const thread = paramsOf(paramsOf(event.params).thread)
    if (conflict === 'role-projection') thread.agentRole = 'rh_restricted_review'
    if (conflict === 'parent-projection') thread.parentThreadId = 'another-parent'
    await assert.rejects(boundary.deliver(event), /native child (?:identity changed its agent role|source disagrees with its (?:agent role|Goal parent))/)
    await boundary.waitForFatalBoundaryFence()
    assert.throws(() => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID, childThreadId: 'identity-child',
      grantId: 'grant.changed', assignmentId: 'assignment.changed', allowedToolNames: [A1_REVIEW_TOOL_NAME],
    }), /not one exact live direct child/)
  }
})

test('unrelated native role metadata does not register or contain an owned Goal', async () => {
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({ success: true, contentItems: [{ type: 'inputText', text: 'read' }] }),
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
  const unrelated = nativeChildStarted('unrelated-role-child', 'rh_restricted_review', 'unrelated-parent')
  paramsOf(paramsOf(unrelated.params).thread).parentThreadId = 'another-unrelated-parent'
  await boundary.deliver(unrelated)
  assert.deepEqual((await boundary.readGoalEpochState(ROOT_THREAD_ID)).descendants, [])
  assert.doesNotMatch(boundary.getReadyReason() ?? '', /contained a fatal condition/)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('closed descendant grant families coexist only on distinct exact direct children', async () => {
  const calls: string[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      calls.push(`${call.threadId}:${call.tool}`)
      return { success: true, contentItems: [{ type: 'inputText', text: 'ok' }] }
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: DESCENDANT_DYNAMIC_TOOLS }))
  for (const childThreadId of ['history-family-child', 'a1-family-child', 'mixed-family-child']) {
    await boundary.deliver(spawnItem(childThreadId))
  }
  await boundary.deliver(nestedSpawnItem('a1-family-child', 'a1-review-leaf'))
  await boundary.deliver(nativeChildStarted('a1-family-child', 'rh_restricted_review'))
  await boundary.deliver(nativeChildStarted('history-family-child', 'rh_historical'))

  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'history-family-child',
    grantId: 'grant.history-family-child',
    assignmentId: 'assignment.history-family-child',
    allowedToolNames: [HISTORY_TOOL_NAME],
  })
  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'a1-family-child',
    grantId: 'grant.a1-family-child',
    assignmentId: 'assignment.a1-family-child',
    allowedToolNames: [A1_REVIEW_TOOL_NAME],
  })
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'history-family-child',
      grantId: 'grant.cross-family',
      assignmentId: 'assignment.cross-family',
      allowedToolNames: [A1_REVIEW_TOOL_NAME],
    }),
    /already has a different historical read grant/,
  )
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'mixed-family-child',
      grantId: 'grant.mixed-family',
      assignmentId: 'assignment.mixed-family',
      allowedToolNames: [HISTORY_TOOL_NAME, A1_REVIEW_TOOL_NAME],
    }),
    /historical read-tool subset or Candidate A1 review-tool subset/,
  )
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'a1-review-leaf',
      grantId: 'grant.a1-review-leaf',
      assignmentId: 'assignment.a1-review-leaf',
      allowedToolNames: [A1_REVIEW_TOOL_NAME],
    }),
    /not one exact live direct child/,
  )
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: 'different-root',
      childThreadId: 'a1-family-child',
      grantId: 'grant.wrong-root',
      assignmentId: 'assignment.wrong-root',
      allowedToolNames: [A1_REVIEW_TOOL_NAME],
    }),
    /not one exact live direct child/,
  )

  for (const childThreadId of ['history-family-child', 'a1-family-child']) {
    await boundary.deliver({
      method: 'turn/started',
      params: { threadId: childThreadId, turn: { id: `turn.${childThreadId}` } },
    })
  }
  await boundary.deliver(
    dynamicToolRequest(
      'history-family-call',
      'history-family-child',
      'history-family-call-id',
      'x',
      HISTORY_TOOL_NAME,
      'turn.history-family-child',
    ),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'a1-family-call',
      'a1-family-child',
      'a1-family-call-id',
      'x',
      A1_REVIEW_TOOL_NAME,
      'turn.a1-family-child',
    ),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'a1-family-wrong-turn',
      'a1-family-child',
      'a1-family-wrong-turn-call',
      'x',
      A1_REVIEW_TOOL_NAME,
      'turn.not-current',
    ),
  )
  await boundary.deliver(
    dynamicToolRequest(
      'a1-family-cross-call',
      'a1-family-child',
      'a1-family-cross-call-id',
      'x',
      HISTORY_TOOL_NAME,
      'turn.a1-family-child',
    ),
  )

  assert.deepEqual(calls, [
    `history-family-child:${HISTORY_TOOL_NAME}`,
    `a1-family-child:${A1_REVIEW_TOOL_NAME}`,
  ])
  assert.deepEqual(
    boundary.responses.map(({ result }) => result.success),
    [true, true, false, false],
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('an exact direct child grant admits only its authenticated active caller context', async () => {
  const contexts: GoalEpochOperationContext[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (_call, context) => {
      contexts.push(context)
      return { success: true, contentItems: [{ type: 'inputText', text: 'historical-read' }] }
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('history-child'))
  await boundary.deliver(nativeChildStarted('history-child', 'rh_historical'))

  assert.deepEqual(
    boundary.resolveDirectChildToolGrantTarget({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'history-child',
    }),
    {
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'history-child',
      parentThreadId: ROOT_THREAD_ID,
      depth: 1,
      status: 'pending',
      activeTurnId: null,
    },
  )
  const installed = boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'history-child',
    grantId: 'grant.history-child.1',
    assignmentId: 'assignment.history-child.1',
    allowedToolNames: [HISTORY_PAGE_TOOL_NAME, HISTORY_TOOL_NAME],
  })
  assert.deepEqual(installed, {
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'history-child',
    parentThreadId: ROOT_THREAD_ID,
    depth: 1,
    status: 'pending',
    activeTurnId: null,
    grantId: 'grant.history-child.1',
    assignmentId: 'assignment.history-child.1',
    allowedToolNames: [HISTORY_TOOL_NAME, HISTORY_PAGE_TOOL_NAME],
  })
  assert.deepEqual(
    boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'history-child',
      grantId: 'grant.history-child.1',
      assignmentId: 'assignment.history-child.1',
      allowedToolNames: [HISTORY_TOOL_NAME, HISTORY_PAGE_TOOL_NAME],
    }),
    installed,
  )
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'history-child',
      grantId: 'grant.history-child.changed',
      assignmentId: 'assignment.history-child.1',
      allowedToolNames: [HISTORY_TOOL_NAME],
    }),
    /already has a different historical read grant/,
  )

  await boundary.deliver({
    method: 'turn/started',
    params: { threadId: 'history-child', turn: { id: 'turn.history-child' } },
  })
  await boundary.deliver(
    dynamicToolRequest(
      'history-read',
      'history-child',
      'history-call',
      'query',
      HISTORY_TOOL_NAME,
      'turn.history-child',
    ),
  )

  assert.equal(boundary.responses[0]?.result.success, true)
  assert.equal(contexts.length, 1)
  assert.deepEqual(
    {
      rootThreadId: contexts[0]?.rootThreadId,
      callerThreadId: contexts[0]?.callerThreadId,
      parentThreadId: contexts[0]?.parentThreadId,
      depth: contexts[0]?.depth,
      turnId: contexts[0]?.turnId,
      status: contexts[0]?.status,
      grantId: contexts[0]?.grantId,
      assignmentId: contexts[0]?.assignmentId,
      signalAborted: contexts[0]?.signal.aborted,
    },
    {
      rootThreadId: ROOT_THREAD_ID,
      callerThreadId: 'history-child',
      parentThreadId: ROOT_THREAD_ID,
      depth: 1,
      turnId: 'turn.history-child',
      status: 'active',
      grantId: 'grant.history-child.1',
      assignmentId: 'assignment.history-child.1',
      signalAborted: false,
    },
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('ungranted siblings, leaves, and root-authority tools fail locally before the handler', async () => {
  let handlerCalls = 0
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => {
      handlerCalls += 1
      return { success: true, contentItems: [{ type: 'inputText', text: 'unexpected' }] }
    },
    onMissionFence: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('granted-child'))
  await boundary.deliver(nativeChildStarted('granted-child', 'rh_historical'))
  await boundary.deliver(spawnItem('ungranted-sibling'))
  await boundary.deliver(nestedSpawnItem('granted-child', 'leaf-helper'))
  for (const childThreadId of ['granted-child', 'ungranted-sibling', 'leaf-helper']) {
    await boundary.deliver({
      method: 'turn/started',
      params: { threadId: childThreadId, turn: { id: `turn.${childThreadId}` } },
    })
  }
  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'granted-child',
    grantId: 'grant.granted-child',
    assignmentId: 'assignment.granted-child',
    allowedToolNames: [HISTORY_TOOL_NAME, HISTORY_PAGE_TOOL_NAME],
  })
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'leaf-helper',
      grantId: 'grant.leaf',
      assignmentId: 'assignment.leaf',
      allowedToolNames: [HISTORY_TOOL_NAME],
    }),
    /not one exact live direct child/,
  )
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'ungranted-sibling',
      grantId: 'grant.invalid-tool',
      assignmentId: 'assignment.invalid-tool',
      allowedToolNames: [HISTORY_GRANT_TOOL_NAME],
    }),
    /historical read-tool subset/,
  )

  const deniedCalls = [
    ['ungranted-sibling', HISTORY_TOOL_NAME],
    ['leaf-helper', HISTORY_TOOL_NAME],
    ['granted-child', DYNAMIC_TOOL.name],
    ['granted-child', ROOT_MISSION_TOOL_NAME],
    ['granted-child', FORMAL_TOOL_NAME],
    ['granted-child', HISTORY_GRANT_TOOL_NAME],
  ] as const
  for (const [childThreadId, tool] of deniedCalls) {
    await boundary.deliver(
      dynamicToolRequest(
        `denied.${childThreadId}.${tool}`,
        childThreadId,
        `call.${childThreadId}.${tool}`,
        'x',
        tool,
        `turn.${childThreadId}`,
      ),
    )
  }

  assert.equal(handlerCalls, 0)
  assert.equal(boundary.responses.length, deniedCalls.length)
  assert.equal(boundary.responses.every(({ result }) => result.success === false), true)
  assert.deepEqual(stopContexts, [])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('delegated replay is caller-scoped and delegated replay or handler failures remain local', async () => {
  const calls: string[] = []
  const missionFences: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (call) => {
      const value = String(paramsOf(call.arguments).value)
      calls.push(`${call.threadId}:${value}`)
      if (value === 'explode') {
        throw new Error('delegated read failed')
      }
      return { success: true, contentItems: [{ type: 'inputText', text: value }] }
    },
    onMissionFence: async (context) => {
      missionFences.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  for (const childThreadId of ['history-a', 'history-b']) {
    await boundary.deliver(spawnItem(childThreadId))
    await boundary.deliver(nativeChildStarted(childThreadId, 'rh_historical'))
    boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId,
      grantId: `grant.${childThreadId}`,
      assignmentId: `assignment.${childThreadId}`,
      allowedToolNames: [HISTORY_TOOL_NAME],
    })
    await boundary.deliver({
      method: 'turn/started',
      params: { threadId: childThreadId, turn: { id: `turn.${childThreadId}` } },
    })
  }

  await boundary.deliver(
    dynamicToolRequest('read-a', 'history-a', 'shared-call-id', 'a', HISTORY_TOOL_NAME, 'turn.history-a'),
  )
  await boundary.deliver(
    dynamicToolRequest('read-b', 'history-b', 'shared-call-id', 'b', HISTORY_TOOL_NAME, 'turn.history-b'),
  )
  await boundary.deliver(
    dynamicToolRequest('changed-a', 'history-a', 'shared-call-id', 'changed', HISTORY_TOOL_NAME, 'turn.history-a'),
  )
  await boundary.deliver(
    dynamicToolRequest('failed-b', 'history-b', 'failed-call', 'explode', HISTORY_TOOL_NAME, 'turn.history-b'),
  )

  assert.deepEqual(calls, ['history-a:a', 'history-b:b', 'history-b:explode'])
  assert.deepEqual(
    boundary.responses.map(({ result }) => result.success),
    [true, true, false, false],
  )
  assert.match(boundary.responses[2]?.result.contentItems[0]?.text ?? '', /changed on replay/)
  assert.match(boundary.responses[3]?.result.contentItems[0]?.text ?? '', /delegated read failed/)
  assert.deepEqual(missionFences, [])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('root handler failure retains unknown-effect Mission containment', async () => {
  const missionFences: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => {
      throw new Error('root owner operation failed after dispatch')
    },
    onMissionFence: async (context) => {
      missionFences.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))
  await boundary.deliver(
    dynamicToolRequest('root-handler-failure', ROOT_THREAD_ID, 'root-handler-call', 'x'),
  )
  await boundary.waitForFatalBoundaryFence()

  assert.equal(boundary.responses[0]?.result.success, false)
  assert.equal(missionFences[0]?.reason, 'unknown_effect')
  assert.equal(missionFences[0]?.containmentScope, 'mission_fence')
})

test('child completion revokes its grant before any later tool admission', async () => {
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  let handlerCalls = 0
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => {
      handlerCalls += 1
      return { success: true, contentItems: [{ type: 'inputText', text: 'unexpected' }] }
    },
    onDescendantToolGrantRevoked: async (event) => {
      revocations.push(event)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('completed-history-child'))
  await boundary.deliver(nativeChildStarted('completed-history-child', 'rh_historical'))
  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'completed-history-child',
    grantId: 'grant.completed-child',
    assignmentId: 'assignment.completed-child',
    allowedToolNames: [HISTORY_TOOL_NAME],
  })
  await boundary.deliver({
    method: 'turn/started',
    params: { threadId: 'completed-history-child', turn: { id: 'turn.completed-child' } },
  })
  await boundary.deliver({
    method: 'turn/completed',
    params: {
      threadId: 'completed-history-child',
      turn: { id: 'turn.completed-child', status: 'completed' },
    },
  })
  await boundary.deliver(
    dynamicToolRequest(
      'after-child-completion',
      'completed-history-child',
      'after-completion-call',
      'x',
      HISTORY_TOOL_NAME,
      'turn.completed-child',
    ),
  )

  assert.equal(handlerCalls, 0)
  assert.equal(boundary.responses[0]?.result.success, false)
  assert.deepEqual(revocations, [
    {
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'completed-history-child',
      parentThreadId: ROOT_THREAD_ID,
      depth: 1,
      grantId: 'grant.completed-child',
      assignmentId: 'assignment.completed-child',
      reason: 'child_turn_completed',
    },
  ])
  assert.throws(
    () => boundary.installDescendantToolGrant({
      rootThreadId: ROOT_THREAD_ID,
      childThreadId: 'completed-history-child',
      grantId: 'grant.after-completion',
      assignmentId: 'assignment.after-completion',
      allowedToolNames: [HISTORY_TOOL_NAME],
    }),
    /not one exact live direct child/,
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('root checkpoint revokes every live child grant before its success response', async () => {
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({
      success: true,
      contentItems: [{ type: 'inputText', text: 'checkpointed' }],
      terminalHandoff: 'owner_checkpoint',
    }),
    onDescendantToolGrantRevoked: async (event) => {
      revocations.push(event)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('checkpoint-history-child'))
  await boundary.deliver(nativeChildStarted('checkpoint-history-child', 'rh_historical'))
  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'checkpoint-history-child',
    grantId: 'grant.checkpoint-child',
    assignmentId: 'assignment.checkpoint-child',
    allowedToolNames: [HISTORY_TOOL_NAME],
  })
  boundary.afterServerResponse = (requestId) => {
    if (requestId === 'checkpoint-with-child-grant') {
      assert.deepEqual(revocations.map(({ reason }) => reason), ['root_checkpoint'])
    }
  }

  await boundary.deliver(
    dynamicToolRequest(
      'checkpoint-with-child-grant',
      ROOT_THREAD_ID,
      'checkpoint-with-grant-call',
      'checkpoint',
    ),
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'owner_checkpoint')

  assert.deepEqual(revocations.map(({ reason }) => reason), ['root_checkpoint'])
})

test('suspension revokes grants and resume reconstructs no descendant authority', async () => {
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  let handlerCalls = 0
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => {
      handlerCalls += 1
      return { success: true, contentItems: [{ type: 'inputText', text: 'unexpected' }] }
    },
    onDescendantToolGrantRevoked: async (event) => {
      revocations.push(event)
    },
  })
  const goalRequest = request({ dynamicTools: HISTORY_DYNAMIC_TOOLS })
  await boundary.startBoundedGoalEpoch(goalRequest)
  await boundary.deliver(spawnItem('resumed-history-child'))
  await boundary.deliver(nativeChildStarted('resumed-history-child', 'rh_historical'))
  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'resumed-history-child',
    grantId: 'grant.before-suspension',
    assignmentId: 'assignment.before-suspension',
    allowedToolNames: [HISTORY_TOOL_NAME],
  })
  const liveProjection = JSON.stringify(await boundary.readGoalEpochState(ROOT_THREAD_ID))
  assert.equal(liveProjection.includes('grant.before-suspension'), false)
  assert.equal(liveProjection.includes('assignment.before-suspension'), false)
  boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')

  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  assert.deepEqual(revocations.map(({ reason }) => reason), ['goal_suspended'])
  await boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, goalRequest)
  await boundary.deliver({
    method: 'thread/started',
    params: {
      thread: { id: 'resumed-history-child', parentThreadId: ROOT_THREAD_ID },
    },
  })
  await boundary.deliver({
    method: 'turn/started',
    params: { threadId: 'resumed-history-child', turn: { id: 'turn.resumed-child' } },
  })
  await boundary.deliver(
    dynamicToolRequest(
      'resumed-without-grant',
      'resumed-history-child',
      'resumed-without-grant-call',
      'x',
      HISTORY_TOOL_NAME,
      'turn.resumed-child',
    ),
  )

  assert.equal(handlerCalls, 0)
  assert.equal(boundary.responses.at(-1)?.result.success, false)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('terminal containment revokes and aborts an in-flight delegated read without a Mission fence', async () => {
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  const missionFences: GoalEpochStopContext[] = []
  let delegatedSignal: AbortSignal | null = null
  let resolveStarted!: () => void
  const started = new Promise<void>((resolve) => {
    resolveStarted = resolve
  })
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (_call, context) => {
      delegatedSignal = context.signal
      resolveStarted()
      return new Promise<DynamicToolCallResult>((_resolve, reject) => {
        const abort = (): void => reject(
          context.signal.reason instanceof Error
            ? context.signal.reason
            : new Error('delegated operation aborted'),
        )
        if (context.signal.aborted) {
          abort()
        } else {
          context.signal.addEventListener('abort', abort, { once: true })
        }
      })
    },
    onDescendantToolGrantRevoked: async (event) => {
      revocations.push(event)
    },
    onMissionFence: async (context) => {
      missionFences.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('contained-history-child'))
  await boundary.deliver(nativeChildStarted('contained-history-child', 'rh_historical'))
  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'contained-history-child',
    grantId: 'grant.contained-child',
    assignmentId: 'assignment.contained-child',
    allowedToolNames: [HISTORY_TOOL_NAME],
  })
  await boundary.deliver({
    method: 'turn/started',
    params: { threadId: 'contained-history-child', turn: { id: 'turn.contained-child' } },
  })
  const delivery = boundary.deliver(
    dynamicToolRequest(
      'contained-read',
      'contained-history-child',
      'contained-read-call',
      'x',
      HISTORY_TOOL_NAME,
      'turn.contained-child',
    ),
  )
  await started
  const stopped = boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  await Promise.all([delivery, stopped])

  assert.equal((delegatedSignal as AbortSignal | null)?.aborted, true)
  assert.equal(boundary.responses[0]?.result.success, false)
  assert.deepEqual(revocations.map(({ reason }) => reason), ['goal_stopped'])
  assert.deepEqual(missionFences, [])
})

test('successful finalization revokes a remaining direct-child grant', async () => {
  const revocations: GoalEpochDescendantToolGrantRevocation[] = []
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => ({
      success: true,
      contentItems: [{ type: 'inputText', text: 'unused' }],
    }),
    onDescendantToolGrantRevoked: async (event) => {
      revocations.push(event)
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: HISTORY_DYNAMIC_TOOLS }))
  await boundary.deliver(spawnItem('finalized-history-child'))
  await boundary.deliver(nativeChildStarted('finalized-history-child', 'rh_historical'))
  boundary.installDescendantToolGrant({
    rootThreadId: ROOT_THREAD_ID,
    childThreadId: 'finalized-history-child',
    grantId: 'grant.finalized-child',
    assignmentId: 'assignment.finalized-child',
    allowedToolNames: [HISTORY_TOOL_NAME],
  })
  const completeGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'complete')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: completeGoal },
  })
  await boundary.deliver({
    method: 'turn/completed',
    params: {
      threadId: ROOT_THREAD_ID,
      turn: { id: ROOT_TURN_ID, status: 'completed' },
    },
  })

  await boundary.finalizeCompletedGoalEpoch(ROOT_THREAD_ID)
  assert.deepEqual(revocations.map(({ reason }) => reason), ['goal_finalized'])
})

test('child spawn model metadata does not become a fatal containment gate', async () => {
  const observations: GoalEpochNativeMaterialObservation[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (observation) => {
      observations.push(observation)
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  await boundary.deliver(
    spawnItem('child-model-metadata', {
      model: 'gpt-other',
      reasoningEffort: 'medium',
    }),
  )

  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(state.descendants[0]?.threadId, 'child-model-metadata')
  assert.equal(observations[0]?.materialKind, 'assignment')
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('usage suspension cancels an active owner operation while retaining the Goal', async () => {
  let operationSignal: AbortSignal | null = null
  let resolveStarted!: () => void
  const started = new Promise<void>((resolve) => {
    resolveStarted = resolve
  })
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async (_call, context) => {
      operationSignal = context.signal
      resolveStarted()
      return new Promise<DynamicToolCallResult>((_resolve, reject) => {
        const abort = (): void => reject(
          context.signal.reason instanceof Error
            ? context.signal.reason
            : new Error('dynamic owner operation aborted'),
        )
        if (context.signal.aborted) {
          abort()
        } else {
          context.signal.addEventListener('abort', abort, { once: true })
        }
      })
    },
  })
  await boundary.startBoundedGoalEpoch(request({ dynamicTools: [DYNAMIC_TOOL] }))
  const delivery = boundary.deliver(
    dynamicToolRequest('usage-owner-request', ROOT_THREAD_ID, 'usage-owner-call', 'x'),
  )
  await started
  boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')

  const [result] = await Promise.all([
    boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID),
    delivery,
  ])

  assert.equal((operationSignal as AbortSignal | null)?.aborted, true)
  assert.equal(boundary.responses.at(-1)?.result.success, false)
  assert.equal(result.goal?.status, 'usageLimited')
})

test('usage suspension finishes in-flight native-material custody before its stop callback', async () => {
  let materialSignal: AbortSignal | null = null
  let resolveCaptureStarted!: () => void
  const captureStarted = new Promise<void>((resolve) => {
    resolveCaptureStarted = resolve
  })
  let resolveCapture!: () => void
  const captureRelease = new Promise<void>((resolve) => {
    resolveCapture = resolve
  })
  const events: string[] = []
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async (_material, context) => {
      materialSignal = context.signal
      events.push('capture_started')
      resolveCaptureStarted()
      await captureRelease
      events.push('capture_finished')
    },
    onGoalTermination: async (context) => {
      events.push('termination_' + context.reason)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const delivery = boundary.deliver(spawnItem('child-custody'))
  await captureStarted
  boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  let settled = false
  const suspending = boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID).finally(() => {
    settled = true
  })
  await new Promise<void>((resolve) => setImmediate(resolve))

  assert.equal((materialSignal as AbortSignal | null)?.aborted, false)
  assert.equal(settled, false)
  assert.deepEqual(events, ['capture_started'])

  resolveCapture()
  const [suspended] = await Promise.all([suspending, delivery])
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.equal(suspended.goal?.status, 'usageLimited')
  assert.deepEqual(events, ['capture_started', 'capture_finished', 'termination_usage_limited'])
  assert.equal(
    state.nativeMaterialObservations.some(
      (entry) => entry.materialKind === 'assignment' && entry.childThreadId === 'child-custody',
    ),
    true,
  )
})

test('provider usage limits suspend without clearing or archiving the root Goal', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited', {
    tokensUsed: 500,
  })

  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
  })
  const result = await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  await boundary.waitForFatalBoundaryFence()
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.equal(stopContexts[0]?.reason, 'usage_limited')
  assert.equal(stopContexts[0]?.containmentScope, 'goal_local')
  assert.equal(result.goal?.status, 'usageLimited')
  assert.equal(result.goalCleared, false)
  assert.equal(state.goal?.status, 'usageLimited')
  assert.equal(
    boundary.requests.some(
      ({ method, params }) =>
        (method === 'thread/goal/clear' || method === 'thread/archive') &&
        params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )
  await boundary.stop()
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'usageLimited')
})

test('operator suspension preserves and resumes the exact root Goal without a successor', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  const goalRequest = request()
  await boundary.startBoundedGoalEpoch(goalRequest)
  const startsBeforeSuspension = boundary.requests.filter(
    ({ method }) => method === 'thread/start',
  ).length

  const suspended = await boundary.pauseAndInterruptGoalEpoch(ROOT_THREAD_ID)

  assert.equal(suspended.goal?.status, 'paused')
  assert.equal(suspended.goalCleared, false)
  assert.deepEqual(stopContexts.map(({ reason }) => reason), ['operator_stop'])
  assert.equal(
    boundary.requests.some(
      ({ method, params }) =>
        (method === 'thread/goal/clear' || method === 'thread/archive') &&
        params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )

  const resumed = await boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, goalRequest)

  assert.equal(resumed.threadId, ROOT_THREAD_ID)
  assert.equal(
    boundary.requests.filter(({ method }) => method === 'thread/start').length,
    startsBeforeSuspension,
  )
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'active')
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'boundary_failure')
})

test('ordinary operator stop arriving during usage suspension preserves the resumable root', async () => {
  let enteredUsageCallback!: () => void
  const usageCallbackEntered = new Promise<void>((resolve) => {
    enteredUsageCallback = resolve
  })
  let releaseUsageCallback!: () => void
  const usageCallbackRelease = new Promise<void>((resolve) => {
    releaseUsageCallback = resolve
  })
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
      if (context.reason === 'usage_limited') {
        enteredUsageCallback()
        await usageCallbackRelease
      }
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
  })
  await usageCallbackEntered

  const stopping = boundary.suspendBoundedGoalEpoch(
    ROOT_THREAD_ID,
    undefined,
    'operator_stop',
  )
  releaseUsageCallback()
  const stopped = await stopping

  assert.equal(stopped.goalCleared, false)
  assert.deepEqual(stopContexts.map(({ reason }) => reason), ['usage_limited'])
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )
})

test('explicit force-stop terminalizes an already operator-suspended root', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.pauseAndInterruptGoalEpoch(ROOT_THREAD_ID)

  const forced = await boundary.stopBoundedGoalEpoch(
    ROOT_THREAD_ID,
    'explicit_force_stop',
  )

  assert.equal(forced.goalCleared, false)
  assert.deepEqual(stopContexts.map(({ reason }) => reason), [
    'operator_stop',
    'explicit_force_stop',
  ])
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )
})

test('direct explicit force-stop kills the active App Server before control RPC containment', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  const fakeProcess = new FakeAppServerProcess()
  const internals = boundary as unknown as {
    child: FakeAppServerProcess
    childClosed: boolean
    markProcessExited: (code: number | null) => void
    onProcessClose: (code: number | null) => Promise<void>
  }
  internals.child = fakeProcess
  internals.childClosed = false
  fakeProcess.once('exit', (code: number | null) => internals.markProcessExited(code))
  fakeProcess.once('close', (code: number | null) => {
    void internals.onProcessClose(code)
  })
  await boundary.startBoundedGoalEpoch(request())
  const requestsBeforeForce = boundary.requests.length
  boundary.blockMethod('thread/list')

  const forcing = boundary.stopBoundedGoalEpoch(
    ROOT_THREAD_ID,
    'explicit_force_stop',
  )
  await new Promise<void>((resolve) => setImmediate(resolve))

  assert.deepEqual(fakeProcess.killSignals, ['SIGKILL'])
  assert.deepEqual(boundary.requests.slice(requestsBeforeForce), [])

  fakeProcess.exit(null)
  fakeProcess.close(null)
  const forced = await forcing

  assert.equal(forced.goalCleared, false)
  assert.equal(forced.appServerTerminated, true)
  assert.deepEqual(stopContexts.map(({ reason }) => reason), ['explicit_force_stop'])
  assert.deepEqual(boundary.requests.slice(requestsBeforeForce), [])
  await boundary.stop()
})

test('explicit force-stop terminalizes an in-flight operator suspension', async () => {
  let enteredOperatorCallback!: () => void
  const operatorCallbackEntered = new Promise<void>((resolve) => {
    enteredOperatorCallback = resolve
  })
  let releaseOperatorCallback!: () => void
  const operatorCallbackRelease = new Promise<void>((resolve) => {
    releaseOperatorCallback = resolve
  })
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
      if (context.reason === 'operator_stop') {
        enteredOperatorCallback()
        await operatorCallbackRelease
      }
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const suspending = boundary.pauseAndInterruptGoalEpoch(ROOT_THREAD_ID)
  await operatorCallbackEntered

  const forcing = boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'explicit_force_stop')
  releaseOperatorCallback()
  await suspending
  const forced = await forcing

  assert.equal(forced.goalCleared, false)
  assert.deepEqual(stopContexts.map(({ reason }) => reason), [
    'operator_stop',
    'explicit_force_stop',
  ])
  assert.equal(
    boundary.requests.filter(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ).length,
    0,
  )
})

test('failed native-material drain releases usage suspension for terminal containment', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const runtime = (
    boundary as unknown as {
      activeGoals: Map<string, { pendingMaterials: Map<string, Promise<void>> }>
    }
  ).activeGoals.get(ROOT_THREAD_ID)
  assert.ok(runtime)
  let rejectDrain!: (error: Error) => void
  const pendingMaterial = new Promise<void>((_resolve, reject) => {
    rejectDrain = reject
  })
  runtime.pendingMaterials.set('externally-cancelled-material', pendingMaterial)
  void pendingMaterial.then(
    () => runtime.pendingMaterials.delete('externally-cancelled-material'),
    () => runtime.pendingMaterials.delete('externally-cancelled-material'),
  )
  boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  const suspending = boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  await new Promise<void>((resolve) => setImmediate(resolve))

  rejectDrain(new Error('Host-side owner capture was cancelled before Core cancellation'))
  const suspensionOutcome = await suspending.then(
    () => 'fulfilled' as const,
    () => 'rejected' as const,
  )
  const stopOutcome = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop').then(
    () => 'fulfilled' as const,
    () => 'rejected' as const,
  )

  assert.equal(suspensionOutcome, 'rejected')
  assert.equal(stopOutcome, 'fulfilled')
  assert.deepEqual(stopContexts.map(({ reason }) => reason), ['operator_stop'])
  assert.equal(
    boundary.requests.filter(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ).length,
    1,
  )
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  assert.equal(
    boundary.requests.filter(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ).length,
    1,
  )
})

test('explicit resume reuses the exact usage-limited thread under current containment', async () => {
  let toolCalls = 0
  const boundary = new FakeGoalBoundary({
    dynamicToolHandler: async () => {
      toolCalls += 1
      return { success: true, contentItems: [{ type: 'inputText', text: 'preserved' }] }
    },
  })
  const goalRequest = request({ dynamicTools: [DYNAMIC_TOOL] })
  await boundary.startBoundedGoalEpoch(goalRequest)
  const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
  })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  const startsBeforeResume = boundary.requests.filter(({ method }) => method === 'thread/start').length

  const resumed = await boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, goalRequest)

  assert.equal(resumed.threadId, ROOT_THREAD_ID)
  assert.equal(
    boundary.requests.filter(({ method }) => method === 'thread/start').length,
    startsBeforeResume,
  )
  const resume = [...boundary.requests].reverse().find(({ method }) => method === 'thread/resume')!
  assert.equal(resume.params.threadId, ROOT_THREAD_ID)
  assert.equal(resume.params.model, 'gpt-5.6-sol')
  assert.equal(resume.params.modelProvider, 'openai')
  assert.equal(resume.params.approvalPolicy, 'never')
  assert.equal(resume.params.approvalsReviewer, 'user')
  assert.equal(resume.params.permissions, 'rh-mission-test')
  assert.deepEqual(resume.params.runtimeWorkspaceRoots, [WORKSPACE])
  assert.equal('dynamicTools' in resume.params, false)
  assert.equal('experimentalRawEvents' in resume.params, false)
  assert.equal('environments' in resume.params, false)
  assert.equal('selectedCapabilityRoots' in resume.params, false)
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'active')

  await boundary.deliver(dynamicToolRequest('server-resumed', ROOT_THREAD_ID, 'effect-resumed', 'x'))
  assert.equal(toolCalls, 1)
  assert.equal(boundary.responses.at(-1)?.result.success, true)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('cancellation drains a late thread/resume reply before containing the exact suspended root', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
  })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  const goalSetsBeforeResume = boundary.requests.filter(
    ({ method }) => method === 'thread/goal/set',
  ).length
  const blocker = boundary.blockMethod('thread/resume')
  const controller = new AbortController()
  let settled = false
  const resuming = boundary.resumeBoundedGoalEpoch(
    ROOT_THREAD_ID,
    request(),
    controller.signal,
  ).finally(() => {
    settled = true
  })
  await waitUntil(
    () => boundary.requests.some(({ method }) => method === 'thread/resume'),
    'blocked thread/resume request',
  )

  controller.abort('operator requested stop')
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(settled, false)
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )

  blocker.resolve()
  await assert.rejects(resuming, GoalEpochCancelledError)
  assert.equal(
    boundary.requests.filter(({ method }) => method === 'thread/resume').length,
    1,
  )
  assert.equal(
    boundary.requests.filter(({ method }) => method === 'thread/goal/set').length,
    goalSetsBeforeResume,
  )
  assert.deepEqual(
    boundary.requests
      .filter(({ method }) => method === 'thread/archive')
      .map(({ params }) => params.threadId),
    [],
  )
  assert.equal(boundary.goals.has(ROOT_THREAD_ID), true)
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'usageLimited')
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(
    boundary.requests.filter(({ method }) => method === 'thread/resume').length,
    1,
  )
})

test('resume contract drift leaves the usage suspension managed until explicit terminal containment', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
  })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  boundary.startResultOverrides = { runtimeWorkspaceRoots: [path.resolve('wrong-workspace')] }

  await assert.rejects(
    boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request()),
    /thread\/resume did not preserve root Goal containment/,
  )
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'usageLimited')
  const stopped = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'boundary_failure')
  assert.equal(stopped.goalCleared, true)
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ),
    true,
  )
})

test('operator abort during resume returns the exact root to nonterminal suspension', async () => {
  const boundary = new FakeGoalBoundary()
  await boundary.startBoundedGoalEpoch(request())
  const limitedGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'usageLimited')
  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: limitedGoal },
  })
  await boundary.suspendBoundedGoalEpoch(ROOT_THREAD_ID)
  const blocker = boundary.blockMethod('thread/goal/set')
  const controller = new AbortController()
  const resuming = boundary.resumeBoundedGoalEpoch(ROOT_THREAD_ID, request(), controller.signal)
  await waitUntil(
    () => boundary.requests.filter(({ method }) => method === 'thread/goal/set').length >= 2,
    'blocked resume activation',
  )
  controller.abort('operator requested stop')
  blocker.resolve()

  await assert.rejects(resuming, GoalEpochCancelledError)
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(state.goal?.status, 'paused')
  assert.equal(
    boundary.requests.some(
      ({ method, params }) => method === 'thread/archive' && params.threadId === ROOT_THREAD_ID,
    ),
    false,
  )
})

test('token usage is telemetry and does not stop an active Goal', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const activeGoal = boundary.setGoalStatus(ROOT_THREAD_ID, 'active', {
    tokensUsed: Number.MAX_SAFE_INTEGER,
  })

  await boundary.deliver({
    method: 'thread/goal/updated',
    params: { threadId: ROOT_THREAD_ID, goal: activeGoal },
  })
  await new Promise<void>((resolve) => setImmediate(resolve))

  assert.equal(stopContexts.length, 0)
  assert.equal((await boundary.readGoalEpochState(ROOT_THREAD_ID)).goal?.status, 'active')
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('stop contains an allowed researcher and leaf-helper subtree deepest first', async () => {
  const boundary = new FakeGoalBoundary()
  boundary.childTree.set(ROOT_THREAD_ID, ['branch-researcher'])
  boundary.childTree.set('branch-researcher', ['leaf-helper'])
  boundary.childTree.set('leaf-helper', [])
  await boundary.startBoundedGoalEpoch(request())

  const result = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  const archives = boundary.requests
    .filter((entry) => entry.method === 'thread/archive')
    .map((entry) => String(entry.params.threadId))
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)

  assert.equal(result.descendantsContained, 2)
  assert.equal(result.appServerTerminated, false)
  assert.deepEqual(archives.slice(0, 3), ['leaf-helper', 'branch-researcher', ROOT_THREAD_ID])
  assert.deepEqual(
    state.descendants.map((entry) => [entry.threadId, entry.parentThreadId, entry.status]),
    [
      ['branch-researcher', ROOT_THREAD_ID, 'archived'],
      ['leaf-helper', 'branch-researcher', 'archived'],
    ],
  )
  assert.equal('subtreeVerifiedAbsent' in result, false)
  assert.equal('recoveryDisposition' in result, false)
  assert.equal('receipt' in result, false)
})

test('stop fails closed through process containment when discovery finds depth three', async () => {
  const boundary = new FakeGoalBoundary()
  boundary.childTree.set(ROOT_THREAD_ID, ['branch-researcher'])
  boundary.childTree.set('branch-researcher', ['leaf-helper'])
  boundary.childTree.set('leaf-helper', ['depth-three'])
  await boundary.startBoundedGoalEpoch(request())

  const result = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  const archives = boundary.requests
    .filter((entry) => entry.method === 'thread/archive')
    .map((entry) => String(entry.params.threadId))

  assert.equal(result.descendantsContained, 2)
  assert.equal(result.appServerTerminated, true)
  assert.deepEqual(archives.slice(0, 3), ['leaf-helper', 'branch-researcher', ROOT_THREAD_ID])
})

test('dead-runner recovery paginates and contains more than 100 direct descendants', async () => {
  const boundary = new FakeGoalBoundary()
  const descendants = Array.from(
    { length: 205 },
    (_, index) => 'child-' + String(index).padStart(3, '0'),
  )
  boundary.childTree.set(ROOT_THREAD_ID, descendants)

  const result = await boundary.stopBoundedGoalEpoch(
    ROOT_THREAD_ID,
    'dead_runner_recovery',
    {
      phase: 'registered',
      expectedObjective: 'Advance one bounded mathematical research epoch.',
    },
  )
  const rootPages = boundary.requests.filter(
    (entry) =>
      entry.method === 'thread/list' && entry.params.parentThreadId === ROOT_THREAD_ID,
  )
  const archives = new Set(
    boundary.requests
      .filter((entry) => entry.method === 'thread/archive')
      .map((entry) => String(entry.params.threadId)),
  )

  assert.equal(result.descendantsContained, descendants.length)
  assert.deepEqual(
    rootPages.map((entry) => entry.params.cursor ?? null),
    [null, 'offset:100', 'offset:200'],
  )
  assert.deepEqual(rootPages[0]?.params.sourceKinds, [
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
  ])
  assert.equal(descendants.every((threadId) => archives.has(threadId)), true)
  assert.equal(archives.has(ROOT_THREAD_ID), true)
})

test('malformed descendant pagination fails closed through process containment', async () => {
  const boundary = new FakeGoalBoundary()
  boundary.threadListPageOverride = () => ({ data: [], nextCursor: 42 })
  await boundary.startBoundedGoalEpoch(request())

  const result = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')

  assert.equal(result.appServerTerminated, true)
  assert.equal(boundary.getReadyReason(), 'Goal containment required process termination')
})

test('non-advancing descendant pagination fails closed through process containment', async () => {
  const boundary = new FakeGoalBoundary()
  boundary.threadListPageOverride = () => ({ data: [], nextCursor: 'stuck' })
  await boundary.startBoundedGoalEpoch(request())

  const result = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  const listCalls = boundary.requests.filter((entry) => entry.method === 'thread/list')

  assert.equal(listCalls.length, 2)
  assert.equal(result.appServerTerminated, true)
  assert.equal(boundary.getReadyReason(), 'Goal containment required process termination')
})

test('repeated stop is idempotent and does not require matching stop choreography', async () => {
  const stopContexts: GoalEpochStopContext[] = []
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async (context) => {
      stopContexts.push(context)
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  const first = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
  const second = await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'boundary_shutdown')

  assert.equal(stopContexts.length, 1)
  assert.equal(first.goal?.status, 'paused')
  assert.equal(second.goal?.status, 'paused')
  assert.equal(second.threadId, first.threadId)
})

test('local containment completes even when the owner stop callback fails', async () => {
  const boundary = new FakeGoalBoundary({
    onGoalTermination: async () => {
      throw new Error('owner unavailable')
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  await assert.rejects(
    boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop'),
    /locally contained after its owner stop callback failed/,
  )
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(state.goal?.status, 'paused')
  assert.equal(
    boundary.requests.some(
      (entry) =>
        entry.method === 'thread/archive' && entry.params.threadId === ROOT_THREAD_ID,
    ),
    true,
  )
})

test('asynchronous fatal owner rejection remains observable without an unattended rejection', async () => {
  const originalError = new GoalEpochMissionConsistencyError('SECRET_ORIGINAL_FATAL_DETAIL')
  const ownerError = new Error('SECRET_OWNER_CALLBACK_DETAIL')
  const contexts: GoalEpochStopContext[] = []
  let releaseOwner!: () => void
  const ownerReleased = new Promise<void>((resolve) => {
    releaseOwner = resolve
  })
  const boundary = new FakeGoalBoundary({
    onNativeMaterialObserved: async () => {
      throw originalError
    },
    onMissionFence: async (context) => {
      contexts.push(context)
      await ownerReleased
      throw ownerError
    },
  })
  await boundary.startBoundedGoalEpoch(request())

  const output = await captureProcessOutput(process.stderr, async () => {
    await assert.rejects(
      boundary.deliver(spawnItem('fatal-owner-rejection-child')),
      (error) => error === originalError,
    )
    releaseOwner()
    // Do not attach a fence waiter until Node has had an opportunity to emit
    // unhandledRejection. node:test fails this test if the background rejects.
    await new Promise<void>((resolve) => setImmediate(resolve))
    await new Promise<void>((resolve) => setImmediate(resolve))
  })

  assert.deepEqual(contexts, [{
    threadId: ROOT_THREAD_ID,
    turnId: ROOT_TURN_ID,
    reason: 'mission_consistency_failure',
    containmentScope: 'mission_fence',
  }])
  const state = await boundary.readGoalEpochState(ROOT_THREAD_ID)
  assert.equal(state.activeTurnId, null)
  assert.equal(state.goal?.status, 'paused')
  assert.equal(boundary.isReady(), false)
  assert.match(boundary.getReadyReason() ?? '', /fatal containment failed/)
  assert.equal(boundary.requests.some((entry) =>
    entry.method === 'thread/archive' && entry.params.threadId === ROOT_THREAD_ID), true)
  assert.doesNotMatch(output, /SECRET_ORIGINAL_FATAL_DETAIL|SECRET_OWNER_CALLBACK_DETAIL/)
  const fatalEvent = output.trim().split('\n')
    .map((line) => JSON.parse(line) as Record<string, unknown>)
    .find((event) => event.event === 'bridge.boundary.fatal_condition')
  assert.ok(fatalEvent)
  assert.equal(fatalEvent.reason, 'mission_consistency_failure')
  assert.equal(fatalEvent.error_class, 'GoalEpochMissionConsistencyError')
  assert.equal('message' in fatalEvent, false)

  let retainedError: unknown
  await assert.rejects(boundary.waitForFatalBoundaryFence(), (error) => {
    assert.ok(error instanceof AggregateError)
    assert.equal(error.errors[0], originalError)
    const containmentError = error.errors[1]
    assert.ok(containmentError instanceof AggregateError)
    assert.deepEqual(containmentError.errors, [ownerError])
    retainedError = error
    return true
  })
  await boundary.stop()
  assert.equal(contexts.length, 1)
  await assert.rejects(boundary.waitForFatalBoundaryFence(), (error) => error === retainedError)
})

test('process-exit owner rejection remains observable without an unattended rejection', async () => {
  const ownerError = new Error('SECRET_PROCESS_EXIT_OWNER_DETAIL')
  const contexts: GoalEpochStopContext[] = []
  const observations: CodexExecutionObservation[] = []
  const boundary = new FakeGoalBoundary({
    onExecutionObservation: observationRecorder(observations),
    onGoalTermination: async (context) => {
      contexts.push(context)
      await new Promise<void>((resolve) => setImmediate(resolve))
      throw ownerError
    },
  })
  await boundary.startBoundedGoalEpoch(request())
  const internals = boundary as unknown as {
    onProcessClose: (code: number | null) => Promise<void>
  }
  await internals.onProcessClose(1)
  // The callback is deliberately asynchronous and there is no API fence waiter
  // while the background failure settles, matching the child close event path.
  await new Promise<void>((resolve) => setImmediate(resolve))
  await new Promise<void>((resolve) => setImmediate(resolve))
  await new Promise<void>((resolve) => setImmediate(resolve))

  assert.deepEqual(contexts, [{
    threadId: ROOT_THREAD_ID,
    turnId: ROOT_TURN_ID,
    reason: 'boundary_failure',
    containmentScope: 'goal_local',
  }])
  assert.equal(boundary.isReady(), false)
  assert.equal(boundary.getReadyReason(), 'boundary exited with code 1')
  const processExit = observations.find((event) => event.kind === 'error' && event.details.code === 'native_process_exit')!
  const containmentFailure = observations.find((event) => event.kind === 'error' && event.details.code === 'containment_failed')!
  assert.equal(processExit.kind === 'error' && processExit.details.actual, 1)
  assert.deepEqual(containmentFailure.caused_by, processExit.caused_by)
  assert.ok(observations.indexOf(processExit) < observations.indexOf(containmentFailure))
  let retainedError: unknown
  await assert.rejects(boundary.waitForFatalBoundaryFence(), (error) => {
    assert.ok(error instanceof AggregateError)
    assert.deepEqual(error.errors, [ownerError])
    retainedError = error
    return true
  })
  await boundary.stop()
  assert.equal(contexts.length, 1)
  await assert.rejects(boundary.waitForFatalBoundaryFence(), (error) => error === retainedError)
})

test('generic thread mutation APIs stay disabled inside the dedicated Goal boundary', async () => {
  const boundary = new FakeGoalBoundary()
  await assert.rejects(boundary.resumeThread('any'), /disabled inside a bounded Goal boundary/)
  await assert.rejects(boundary.createThread('prompt'), /disabled inside a bounded Goal boundary/)
  await assert.rejects(
    boundary.submitPrompt('any', 'prompt'),
    /disabled inside a bounded Goal boundary/,
  )
  assert.equal(boundary.requests.length, 0)
})

test('lifecycle cancellation drains a late mutation reply before rejecting requests left at close', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  const lateMutation = boundary.rpc('thread/archive', { threadId: 'late-thread' })
  const cancelled = boundary.rpc('thread/read', { threadId: 'cancelled-thread' })
  const cancelledAssertion = assert.rejects(cancelled, /stopped/)
  const stopPromise = boundary.stop()

  fakeProcess.exit(0)
  boundary.deliverResponse('req-1', { archived: true })
  assert.deepEqual(await lateMutation, {
    jsonrpc: '2.0',
    id: 'req-1',
    result: { archived: true },
  })

  fakeProcess.close(0)
  await cancelledAssertion
  await stopPromise
})

test('explicit RPC cancellation is typed and a late reply cannot satisfy another request', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  const controller = new AbortController()
  const cancelled = boundary.rpc('thread/read', { threadId: 'cancelled-thread' }, controller.signal)

  controller.abort('operator_stop')
  await assert.rejects(cancelled, (error: unknown) => {
    assert.ok(error instanceof GoalEpochCancelledError)
    assert.equal(error.phase, 'test lifecycle RPC')
    assert.equal(error.reason, 'operator_stop')
    return true
  })

  boundary.deliverResponse('req-1', { stale: true })
  const next = boundary.rpc('thread/read', { threadId: 'next-thread' })
  boundary.deliverResponse('req-2', { thread: { id: 'next-thread' } })
  assert.deepEqual((await next).result, { thread: { id: 'next-thread' } })

  const stopPromise = boundary.stop()
  fakeProcess.exit(0)
  fakeProcess.close(0)
  await stopPromise
})

test('JSON-RPC response errors carry only the exact outgoing method and safe error fields', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  const failed = boundary.rpc('thread/read', {
    threadId: 'private-thread-id',
    content: 'private-request-content',
  })

  boundary.deliverError('req-1', {
    code: -32602,
    message: 'request rejected',
    data: { content: 'private-response-content' },
  })

  await assert.rejects(failed, (error: unknown) => {
    assert.ok(error instanceof Error)
    const rpcError = error as Error & {
      jsonRpcCode?: unknown
      jsonRpcMethod?: unknown
    }
    assert.equal(rpcError.name, 'JsonRpcResponseError')
    assert.equal(rpcError.jsonRpcCode, -32602)
    assert.equal(rpcError.jsonRpcMethod, 'thread/read')
    assert.equal('params' in rpcError, false)
    assert.equal('data' in rpcError, false)
    assert.equal(JSON.stringify(rpcError).includes('private-'), false)
    return true
  })

  const stopPromise = boundary.stop()
  fakeProcess.exit(0)
  fakeProcess.close(0)
  await stopPromise
})

test('thread/goal/get -32600 returns null for the exact workspace-bound archived root', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  boundary.markReadyForGoalRequest()

  const pending = boundary.getThreadGoal(ROOT_THREAD_ID)
  await waitUntil(() => fakeProcess.writes.length === 1, 'thread/goal/get request')
  const goalRequest = JSON.parse(fakeProcess.writes[0]!)
  assert.deepEqual(
    { method: goalRequest.method, params: goalRequest.params },
    { method: 'thread/goal/get', params: { threadId: ROOT_THREAD_ID } },
  )
  boundary.deliverError(goalRequest.id, { code: -32600, message: 'Invalid Request' })

  await waitUntil(() => fakeProcess.writes.length === 2, 'archived thread/list request')
  const listRequest = JSON.parse(fakeProcess.writes[1]!)
  assert.equal(listRequest.method, 'thread/list')
  boundary.deliverResponse(listRequest.id, {
    data: [{ id: ROOT_THREAD_ID, parentThreadId: null, cwd: WORKSPACE }],
    nextCursor: null,
  })

  assert.equal(await pending, null)

  const stopPromise = boundary.stop()
  fakeProcess.exit(0)
  fakeProcess.close(0)
  await stopPromise
})

test('thread/goal/get -32600 propagates when no exact archived root exists', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  boundary.markReadyForGoalRequest()

  const pending = boundary.getThreadGoal(ROOT_THREAD_ID)
  const rejected = assert.rejects(pending, (error: unknown) => {
    assert.ok(error instanceof Error)
    const rpcError = error as Error & {
      jsonRpcCode?: unknown
      jsonRpcMethod?: unknown
    }
    assert.equal(rpcError.name, 'JsonRpcResponseError')
    assert.equal(rpcError.jsonRpcCode, -32600)
    assert.equal(rpcError.jsonRpcMethod, 'thread/goal/get')
    return true
  })
  await waitUntil(() => fakeProcess.writes.length === 1, 'thread/goal/get request')
  boundary.deliverError('req-1', { code: -32600, message: 'Invalid Request' })
  await waitUntil(() => fakeProcess.writes.length === 2, 'archived thread/list request')
  boundary.deliverResponse('req-2', { data: [], nextCursor: null })

  await rejected

  const stopPromise = boundary.stop()
  fakeProcess.exit(0)
  fakeProcess.close(0)
  await stopPromise
})

test('thread/goal/get errors other than -32600 propagate without archived lookup', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  boundary.markReadyForGoalRequest()

  const pending = boundary.getThreadGoal(ROOT_THREAD_ID)
  const rejected = assert.rejects(pending, (error: unknown) => {
    assert.ok(error instanceof Error)
    const rpcError = error as Error & {
      jsonRpcCode?: unknown
      jsonRpcMethod?: unknown
    }
    assert.equal(rpcError.name, 'JsonRpcResponseError')
    assert.equal(rpcError.jsonRpcCode, -32601)
    assert.equal(rpcError.jsonRpcMethod, 'thread/goal/get')
    return true
  })
  await waitUntil(() => fakeProcess.writes.length === 1, 'thread/goal/get request')
  boundary.deliverError('req-1', { code: -32601, message: 'Method not found' })

  await rejected
  assert.equal(fakeProcess.writes.length, 1)

  const stopPromise = boundary.stop()
  fakeProcess.exit(0)
  fakeProcess.close(0)
  await stopPromise
})

test('graceful stop sends TERM and waits for process close without a hidden deadline', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  let settled = false
  const stopPromise = boundary.stop().then(() => {
    settled = true
  })

  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.deepEqual(fakeProcess.killSignals, ['SIGTERM'])
  assert.equal(settled, false)

  fakeProcess.exit(0)
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.equal(settled, false)

  fakeProcess.close(0)
  await stopPromise
  assert.equal(settled, true)
})

test('explicit force-stop escalates TERM to KILL and releases a hung control RPC', async () => {
  const fakeProcess = new FakeAppServerProcess()
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  const pending = boundary.rpc('thread/goal/get', { threadId: ROOT_THREAD_ID })
  const rejected = assert.rejects(pending, /explicit force-stop test|stopped/)
  const gracefulStop = boundary.stop()
  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.deepEqual(fakeProcess.killSignals, ['SIGTERM'])

  const forceTerminate = (
    boundary as unknown as {
      forceTerminateDedicatedAppServer: (reason: string) => Promise<void>
    }
  ).forceTerminateDedicatedAppServer('explicit force-stop test')

  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.deepEqual(fakeProcess.killSignals, ['SIGTERM', 'SIGKILL'])

  fakeProcess.exit(null)
  fakeProcess.close(null)
  await forceTerminate
  await gracefulStop
  await rejected
})

test('execution observation publishes source-supplied output while native material handling is pending', async () => {
  const events: CodexExecutionObservation[] = []
  let release!: () => void
  const blocked = new Promise<void>((resolve) => { release = resolve })
  const boundary = new FakeGoalBoundary({
    onExecutionObservation: observationRecorder(events),
    onNativeMaterialObserved: async () => blocked,
  })
  await boundary.startBoundedGoalEpoch(request())
  const assignment = boundary.deliver(spawnItem('observed-child'))
  await waitUntil(() => events.some((event) => event.kind === 'work' && event.details.child_thread_id === 'observed-child' && event.phase === 'processed'), 'child registration')
  let outputHandled = false
  const output = boundary.deliver({ method: 'item/commandExecution/outputDelta', params: {
    threadId: 'observed-child', turnId: 'child-turn', itemId: 'command-running', delta: 'first line\n',
  } }).then(() => { outputHandled = true })
  assert.equal(outputHandled, false)
  assert.equal(events.some((event) => event.kind === 'command' && event.contents.some((part) => part.text === 'first line\n')), true)
  release()
  await assignment
  await output
  assert.equal(outputHandled, true)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('execution observation preserves rejected spawn receipt and original error before failed containment', async () => {
  const events: CodexExecutionObservation[] = []
  const boundary = new FakeGoalBoundary({ onExecutionObservation: observationRecorder(events),
    onMissionFence: async () => { throw new Error('fixture containment failure') },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('observed-parent'))
  await boundary.deliver(nestedSpawnItem('observed-parent', 'observed-leaf'))
  await boundary.deliver(nestedSpawnItem('observed-leaf', 'rejected-child'))
  await waitUntil(() => events.some((event) => event.kind === 'error' && event.details.code === 'containment_failed'), 'failed containment observation')
  const rejected = events.find((event) => event.kind === 'error' && event.details.code === 'delegation_spawn_depth')!
  assert.equal(rejected.phase, 'rejected')
  assert.ok(rejected.caused_by)
  assert.equal(events[rejected.caused_by!.sequence - 1]?.phase, 'received')
  assert.equal(events.some((event) => event.kind === 'work' && event.phase === 'processed' && event.details.child_thread_id === 'rejected-child'), false)
  const failed = events.find((event) => event.kind === 'error' && event.details.code === 'containment_failed')!
  assert.ok(events.indexOf(rejected) < events.indexOf(failed))
  assert.deepEqual(failed.caused_by, rejected.caused_by)
  await assert.rejects(boundary.waitForFatalBoundaryFence(), /containment|fixture/)
  await boundary.stop()
})

test('execution observation callback failure cannot reject native work or turn into custody failure', async () => {
  const captured: string[] = []
  const boundary = new FakeGoalBoundary({
    onExecutionObservation: () => { throw new Error('observer sink failure') },
    onNativeMaterialObserved: async (material) => { captured.push(material.childThreadId) },
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('unaffected-child'))
  assert.deepEqual(captured, ['unaffected-child'])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('execution observation does not project another thread outside the owned lineage', async () => {
  const events: CodexExecutionObservation[] = []
  const boundary = new FakeGoalBoundary({ onExecutionObservation: observationRecorder(events) })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver({ method: 'item/agentMessage/delta', params: {
    threadId: 'foreign-thread', turnId: 'foreign-turn', itemId: 'foreign-item', delta: 'foreign body',
  } })
  assert.equal(JSON.stringify(events).includes('foreign body'), false)
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('execution observation links request errors and owner capture references to their receipts', async () => {
  const requests: CodexExecutionObservation[] = []
  const processFixture = new FakeAppServerProcess()
  const rpc = new RpcLifecycleBoundary(processFixture, { onExecutionObservation: observationRecorder(requests) })
  const pending = rpc.rpc('thread/goal/get', { threadId: ROOT_THREAD_ID })
  const rejected = assert.rejects(pending, /fixture request failure/)
  rpc.deliverError('req-1', { code: -32601, message: 'fixture request failure' })
  await rejected
  const outcome = requests.find((event) => event.kind === 'error' && event.details.code === 'native_request_failed')!
  assert.equal(outcome.kind === 'error' && outcome.details.actual, -32601)
  assert.equal(outcome.identity.operation_id, 'req-1')
  assert.deepEqual(outcome.caused_by, { incarnation: 'test-source', sequence: 1 })
  const stop = rpc.stop()
  processFixture.exit(0)
  processFixture.close(0)
  await stop

  const events: CodexExecutionObservation[] = []
  const boundary = new FakeGoalBoundary({ onExecutionObservation: observationRecorder(events),
    onNativeMaterialObserved: async (material) => ({ status: 'captured', observationId: material.observationId,
      captureRef: { materialId: 'capture:fixture-owned', revision: 3 } }),
  })
  await boundary.startBoundedGoalEpoch(request())
  await boundary.deliver(spawnItem('captured-observed-child'))
  const registration = events.find((event) => event.kind === 'work' && event.details.action === 'registered' && event.identity.thread_id === 'captured-observed-child')!
  assert.equal(registration.identity.turn_id, null)
  const reference = events.find((event) => event.kind === 'artifact' && event.details.owner_handle === 'capture:fixture-owned')!
  assert.equal(reference.phase, 'processed')
  assert.deepEqual(JSON.parse(reference.contents[0]!.text), { materialId: 'capture:fixture-owned', revision: 3 })
  assert.equal(events[reference.caused_by!.sequence - 1]!.phase, 'received')
  await boundary.deliver({ method: 'turn/started', params: { threadId: ROOT_THREAD_ID, turn: { id: 'observed-turn', status: 'inProgress' } } })
  await boundary.deliver({ method: 'turn/completed', params: { threadId: ROOT_THREAD_ID, turn: { id: 'observed-turn', status: 'completed' } } })
  const states = events.filter((event) => event.kind === 'work' && event.details.action === 'status' && event.identity.thread_id === ROOT_THREAD_ID)
  assert.deepEqual(states.map((event) => event.kind === 'work' ? event.details.status : null).slice(-2), ['active', 'complete'])
  await boundary.stopBoundedGoalEpoch(ROOT_THREAD_ID, 'operator_stop')
})

test('graceful stop tolerates a false TERM return followed by authoritative process close', async () => {
  const fakeProcess = new FakeAppServerProcess()
  fakeProcess.killResult = false
  const boundary = new RpcLifecycleBoundary(fakeProcess)
  let settled = false
  const stopPromise = boundary.stop().then(() => {
    settled = true
  })

  await new Promise<void>((resolve) => setImmediate(resolve))
  assert.deepEqual(fakeProcess.killSignals, ['SIGTERM'])
  assert.equal(settled, false)

  fakeProcess.exit(0)
  fakeProcess.close(0)
  await stopPromise
  assert.equal(settled, true)
})
