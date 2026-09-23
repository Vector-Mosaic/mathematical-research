import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test, { afterEach, beforeEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  PINNED_CODEX_CLI_VERSION,
  PINNED_MODEL_CATALOG_RELATIVE_PATH,
  acquireRuntimeLock,
  loadPinnedCodexModelCatalog,
  readMissionHostConfig,
  readMissionHostConfigFromEnvironment,
  type MissionHostPathOverrides,
} from './config.js'

const RELEASE_SHA = 'a'.repeat(40)
const PINNED_MODEL_CATALOG_SOURCE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  'test-fixtures',
  `codex-model-catalog.${PINNED_CODEX_CLI_VERSION}.json`,
)
const fixtureOwnerUids = new Map<string, number>()
const nativeLstatSync = fs.lstatSync

// These fixtures represent installed release assets while compute runs as its
// unprivileged account. No prefix-wide or platform bypass: only declared exact
// fixture paths receive a modeled UID, with all other metadata/content intact.
beforeEach(() => {
  mock.method(fs, 'lstatSync', ((...args: Parameters<typeof fs.lstatSync>) => {
    const stat = Reflect.apply(nativeLstatSync, fs, args) as fs.Stats | fs.BigIntStats | undefined
    const uid = typeof args[0] === 'string' ? fixtureOwnerUids.get(args[0]) : undefined
    if (!stat || uid === undefined) return stat
    return Object.assign(Object.create(Object.getPrototypeOf(stat)), stat, {
      uid: typeof stat.uid === 'bigint' ? BigInt(uid) : uid,
    })
  }) as typeof fs.lstatSync)
})
afterEach(() => {
  mock.restoreAll()
  fixtureOwnerUids.clear()
})

function oneToolProjection(
  tool: string,
  operations: string[],
  strategyContinuation: 'continue' | 'closeout' | null = null,
): Record<string, unknown> {
  const inputSchema = {
    type: 'object',
    properties: {
      operation: {
        type: 'string',
        enum: [
          'usage',
          ...operations,
        ],
      },
      input: { type: 'object' },
    },
    required: ['operation', 'input'],
    additionalProperties: false,
  }
  return {
    schema_version: 'mathematical_research.mission_model_projection.v2',
    allowed_operations: operations,
    semantic_request_schema_version: 'mathematical_research.mission_semantic_request.v1',
    input_schema: inputSchema,
    usage: {
      index: {
        schema_version: 'mathematical_research.mission_model_usage.v1',
        tool,
        status: 'ok',
        semantic_operation_count: operations.length,
        operations: operations.map((operation) => ({ operation })),
      },
      operation_guides: Object.fromEntries(
        operations.map((operation) => [
          operation,
          {
            schema_version: 'mathematical_research.mission_model_usage.v1',
            tool,
            status: 'ok',
            operation,
            input_schema: operation === 'record_strategy'
              ? {
                  type: 'object',
                  properties: {
                    mission_continuation: {
                      type: 'string',
                      enum: [strategyContinuation],
                    },
                  },
                }
              : { type: 'object' },
            examples: [{
              call: {
                operation,
                input: operation === 'record_strategy'
                  ? { mission_continuation: strategyContinuation }
                  : {},
              },
            }],
          },
        ]),
      ),
    },
  }
}

function closedSchema(
  properties: Record<string, unknown>,
  required: string[] = Object.keys(properties),
): Record<string, unknown> {
  return {
    type: 'object',
    additionalProperties: false,
    properties,
    required,
  }
}

function exactRefSchema(prefix: 'candidate' | 'evidence'): Record<string, unknown> {
  return closedSchema({
    id: { type: 'string', pattern: `^(?:${prefix}):` },
    revision: { type: 'integer', minimum: 1 },
    payload_sha256: { type: 'string', pattern: '^[0-9a-f]{64}(?![\\s\\S])' },
  })
}

function ownerArraySchema(items: Record<string, unknown>, minItems = 0): Record<string, unknown> {
  return {
    type: 'array',
    items,
    minItems,
    uniqueItems: true,
  }
}

function candidateA1ReviewProjection(): Record<string, unknown> {
  const text = { type: 'string', minLength: 1 }
  const evidenceRef = exactRefSchema('evidence')
  return {
    name: 'rh_mission_a1_review',
    grant_tool_name: 'rh_mission_a1_review_grant',
    page_tool_name: 'rh_mission_a1_review_page',
    grant_input_schema: closedSchema({
      child_thread_id: text,
      assignment: text,
      context: closedSchema({
        id: { type: 'string', pattern: '^(?:context):' },
        revision: { type: 'integer', minimum: 1 },
      }),
      candidate_ref: exactRefSchema('candidate'),
    }),
    request_schema: {
      oneOf: [
        closedSchema({ mode: { const: 'usage' } }),
        closedSchema(
          {
            mode: { const: 'retrieve' },
            page_size: { type: 'integer', minimum: 1, maximum: 50 },
            cursor: text,
          },
          ['mode'],
        ),
        closedSchema(
          {
            mode: { const: 'submit' },
            disposition: { type: 'string', enum: ['invalidated', 'admission_ready'] },
            review_finding: text,
            cited_basis: ownerArraySchema(evidenceRef),
            concrete_defects: ownerArraySchema(
              closedSchema({
                exact_defect: text,
                affected_scope: text,
                sufficiency_basis: text,
              }),
            ),
            no_remaining_material_objection: { type: 'boolean' },
            limitations: ownerArraySchema(text),
            non_inferences: ownerArraySchema(text),
          },
          ['mode', 'disposition', 'review_finding', 'no_remaining_material_objection'],
        ),
      ],
    },
  }
}

function admissionProjection(): Record<string, unknown> {
  const text = { type: 'string', minLength: 1 }
  const textArray = ownerArraySchema(text)
  const ownerRef = closedSchema({
    kind: { type: 'string', enum: ['evidence', 'context', 'branch', 'candidate'] },
    identity: text,
    revision: { type: 'integer', minimum: 1 },
    payload_sha256: { type: 'string', pattern: '^[0-9a-f]{64}(?![\\s\\S])' },
  })
  const context = closedSchema({
    id: { type: 'string', pattern: '^(?:context):' },
    revision: { type: 'integer', minimum: 1 },
  })
  return {
    name: 'rh_mission_admission',
    open_tool_name: 'rh_mission_admission_open',
    grant_tool_name: 'rh_mission_admission_grant',
    page_tool_name: 'rh_mission_admission_page',
    case_input_schema: closedSchema({ candidate_ref: exactRefSchema('candidate') }),
    grant_input_schema: closedSchema({
      role: { type: 'string', enum: ['reviewer', 'admitter'] },
      child_thread_id: text,
      assignment: text,
      context,
      case_ref: exactRefSchema('evidence'),
    }),
    request_schema: {
      oneOf: [
        closedSchema({ mode: { const: 'usage' } }),
        closedSchema(
          {
            mode: { const: 'retrieve' },
            page_size: { type: 'integer', minimum: 1, maximum: 50 },
            cursor: text,
          },
          ['mode'],
        ),
        closedSchema(
          {
            mode: { const: 'submit' },
            disposition: { type: 'string', enum: ['no_material_objection', 'material_objection'] },
            review_finding: text,
            objections: ownerArraySchema(closedSchema({
              exact_objection: text,
              affected_scope: text,
              materiality_basis: text,
            })),
            cited_basis: ownerArraySchema(ownerRef),
            limitations: textArray,
            non_inferences: textArray,
          },
          ['mode', 'disposition', 'review_finding'],
        ),
        closedSchema(
          {
            mode: { const: 'submit' },
            disposition: { const: 'authorize_exact_delta' },
            decision_basis: text,
            limitations: textArray,
            non_inferences: textArray,
          },
          ['mode', 'disposition', 'decision_basis'],
        ),
        closedSchema(
          {
            mode: { const: 'submit' },
            disposition: { const: 'reject' },
            decision_basis: text,
            objections: ownerArraySchema(closedSchema({
              exact_objection: text,
              affected_scope: text,
              materiality_basis: text,
            }), 1),
            cited_basis: ownerArraySchema(ownerRef, 1),
            limitations: textArray,
            non_inferences: textArray,
          },
          ['mode', 'disposition', 'decision_basis', 'objections', 'cited_basis'],
        ),
      ],
    },
  }
}

function researchReadProjection(): Record<string, unknown> {
  const text = { type: 'string', minLength: 1 }
  const historicalFamilies = [
    'branches', 'candidates', 'strategies', 'contexts', 'evidence',
    'capture_annotations', 'captures', 'capture_artifacts',
  ]
  const sourceFamilies = ownerArraySchema({ type: 'string', enum: historicalFamilies }, 1)
  const page = {
    page_size: { type: 'integer', minimum: 1, maximum: 100 },
    cursor: text,
  }
  const fields = ownerArraySchema({ type: 'string', enum: ['id', 'title', 'content', 'relationships'] }, 1)
  const historicalHandle = {
    type: 'string',
    pattern: '^(?:(?:candidate|branch|strategy|context|evidence|capture-annotation):' +
      '[A-Za-z0-9][A-Za-z0-9._:-]{0,191}(?:@[1-9][0-9]*)?' +
      '|capture:raw-capture:[0-9a-f]{48}' +
      '|capture-artifact:raw-capture:[0-9a-f]{48}#(?:0|[1-9][0-9]*))(?![\\s\\S])',
  }
  const currentHandle = {
    ...historicalHandle,
    pattern: historicalHandle.pattern.replace(
      'candidate|branch|strategy|context|evidence|capture-annotation',
      'mission|session|candidate|branch|strategy|context|evidence|capture-annotation',
    ),
  }
  const selection = {
    oneOf: [
      closedSchema({ mode: { const: 'read' }, purpose: text, ids: ownerArraySchema(currentHandle, 1) }),
      closedSchema({
        mode: { const: 'search' },
        purpose: text,
        query: text,
        kinds: ownerArraySchema({
          type: 'string',
          enum: ['mission', 'strategy', 'branch', 'candidate', 'context', 'evidence', 'session', 'capture', 'capture-annotation'],
        }, 1),
        fields,
        ...page,
      }, ['mode', 'purpose', 'query']),
      closedSchema({
        mode: { const: 'history_inventory' },
        purpose: text,
        revision_scope: { type: 'string', enum: ['retained_history', 'current_at_cut'] },
        source_families: sourceFamilies,
        ...page,
      }, ['mode', 'purpose']),
      closedSchema({
        mode: { const: 'history_search' },
        purpose: text,
        revision_scope: { type: 'string', enum: ['retained_history', 'current_at_cut'] },
        query: text,
        fields,
        source_families: sourceFamilies,
        ...page,
      }, ['mode', 'purpose', 'query', 'fields']),
      closedSchema({
        mode: { const: 'history_read' },
        purpose: text,
        ids: ownerArraySchema(historicalHandle, 1),
        include_raw_bodies: { type: 'boolean' },
      }),
      closedSchema({
        mode: { const: 'history_traverse' },
        purpose: text,
        ids: ownerArraySchema(historicalHandle, 1),
        relationship_kinds: ownerArraySchema({
          type: 'string',
          enum: [
            'revision_predecessor', 'owner_reference', 'branch_genealogy', 'candidate_genealogy',
            'strategy_hook', 'capture_annotation', 'capture_artifact', 'capture_lineage',
          ],
        }, 1),
        ...page,
      }, ['mode', 'purpose', 'ids', 'relationship_kinds']),
      closedSchema({
        mode: { const: 'selected_context' },
        purpose: text,
        id: {
          type: 'string',
          pattern: '^context:[A-Za-z0-9][A-Za-z0-9._:-]{0,191}@[1-9][0-9]*(?![\\s\\S])',
        },
        selection: {
          oneOf: [
            closedSchema({ mode: { const: 'treatments' }, treatment_ids: ownerArraySchema(text, 1) }),
            closedSchema({ mode: { const: 'whole_context' } }),
          ],
        },
      }),
    ],
  }
  return {
    name: 'rh_mission_research_read',
    grant_tool_name: 'rh_mission_research_read_grant',
    page_tool_name: 'rh_mission_research_read_page',
    grant_input_schema: closedSchema({
      child_thread_id: text,
      assignment: text,
      source_families: ownerArraySchema({ type: 'string', enum: [...historicalFamilies, 'missions', 'sessions'] }, 1),
      raw_body_policy: { type: 'string', enum: ['metadata_only', 'allow_untrusted_material'] },
    }),
    request_schema: {
      oneOf: [
        closedSchema({
          mode: { const: 'usage' },
          topics: ownerArraySchema({ type: 'string', enum: ['reads', 'sources', 'continuations', 'authority'] }, 1),
        }, ['mode']),
        closedSchema({ mode: { const: 'retrieve' }, selection }),
      ],
    },
  }
}

function modelProjection(): Record<string, unknown> {
  const rootOperations = [
    'orient',
    'retrieve',
    'record_context',
    'interpret_material',
    'record_candidate',
    'record_branch',
    'synthesize',
    'record_strategy',
    'checkpoint',
  ]
  return {
    schema_version: 'mathematical_research.mission_host_model_projection.v1',
    root_tool: {
      name: 'rh_mission',
      model_projection: oneToolProjection('rh_mission', rootOperations, 'continue'),
    },
    closeout_root_tool: {
      name: 'rh_mission',
      model_projection: oneToolProjection(
        'rh_mission',
        ['record_strategy', 'checkpoint'],
        'closeout',
      ),
    },
    historical_read_tool: {
      name: 'rh_mission_history',
      model_projection: oneToolProjection('rh_mission_history', ['orient', 'retrieve']),
      grant_input_schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          child_thread_id: { type: 'string', minLength: 1 },
          assignment_mode: {
            type: 'string',
            enum: ['historical_opportunity_scout', 'lifecycle_historian'],
          },
          assignment: { type: 'string', minLength: 1 },
          context: {
            type: 'object',
            additionalProperties: false,
            properties: {
              id: { type: 'string', pattern: '^context:' },
              revision: { type: 'integer', minimum: 1 },
            },
            required: ['id', 'revision'],
          },
          source_families: {
            type: 'array',
            items: {
              type: 'string',
              enum: [
                'branches',
                'candidates',
                'strategies',
                'contexts',
                'evidence',
                'capture_annotations',
                'captures',
                'capture_artifacts',
              ],
            },
            minItems: 1,
            uniqueItems: true,
          },
          raw_body_policy: {
            type: 'string',
            enum: ['metadata_only', 'allow_untrusted_material'],
          },
        },
        required: [
          'child_thread_id',
          'assignment_mode',
          'assignment',
          'context',
          'source_families',
          'raw_body_policy',
        ],
      },
    },
    research_read_tool: researchReadProjection(),
    candidate_a1_review_tool: candidateA1ReviewProjection(),
    admission_tool: admissionProjection(),
  }
}

type Fixture = {
  root: string
  repoRoot: string
  overrides: MissionHostPathOverrides
  projectionPath: string
  modelCatalogPath: string
}

function fixture(): Fixture {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-mission-config-'))
  const repoRoot = path.join(root, RELEASE_SHA)
  const workspaceRoot = path.join(root, 'projects', 'mission.rh')
  const goalsRoot = path.join(root, 'goals')
  const runtimeDir = path.join(root, 'runtime', 'mission.rh')
  const codexHome = path.join(root, 'codex-home')
  const lockPath = path.join(root, 'run', 'runtime.lock')
  const missionScriptPath = path.join(repoRoot, 'scripts', 'rh_mission.py')
  const entrypointPath = path.join(
    repoRoot,
    'services',
    'rh-mission-host',
    'dist',
    'main.js',
  )
  const missionSeedPath = path.join(
    repoRoot,
    'contracts',
    'rh_autonomous_mission_seed.v1.json',
  )
  const projectionPath = path.join(repoRoot, '.mathematical-research-model-projection.json')
  const modelCatalogPath = path.join(repoRoot, ...PINNED_MODEL_CATALOG_RELATIVE_PATH.split('/'))
  const pythonPath = path.join(root, 'python3')
  const codexCliPath = path.join(root, 'codex')
  for (const filePath of [
    missionScriptPath,
    entrypointPath,
    missionSeedPath,
    projectionPath,
    modelCatalogPath,
    pythonPath,
    codexCliPath,
  ]) {
    fs.mkdirSync(path.dirname(filePath), { recursive: true })
  }
  fs.mkdirSync(workspaceRoot, { recursive: true })
  fs.mkdirSync(codexHome, { recursive: true })
  fs.writeFileSync(missionScriptPath, '# owner bridge\n')
  fs.writeFileSync(entrypointPath, '// built host\n')
  fs.writeFileSync(missionSeedPath, '{}\n')
  fs.writeFileSync(projectionPath, `${JSON.stringify(modelProjection())}\n`)
  fs.writeFileSync(modelCatalogPath, fs.readFileSync(PINNED_MODEL_CATALOG_SOURCE))
  fs.writeFileSync(pythonPath, '')
  fs.writeFileSync(codexCliPath, '')
  for (const immutablePath of [repoRoot, missionScriptPath, entrypointPath, missionSeedPath, projectionPath, modelCatalogPath]) {
    fixtureOwnerUids.set(immutablePath, 0)
  }
  return {
    root,
    repoRoot,
    projectionPath,
    modelCatalogPath,
    overrides: {
      repoRoot,
      missionWorkspaceRoot: workspaceRoot,
      goalsRoot,
      runtimeDir,
      codexHome,
      lockPath,
      pythonPath,
      codexCliPath,
    },
  }
}

test('config owns only fixed transport identity and model launch facts', () => {
  const built = fixture()
  const config = readMissionHostConfig('mission.rh', built.overrides)

  assert.equal(config.releaseSha, RELEASE_SHA)
  assert.equal(config.projectId, 'project.riemann_hypothesis')
  assert.equal(config.pollIntervalMs, 2000)
  assert.equal('objective' in config, false)
  assert.equal('runtimeContractPath' in config, false)
  assert.equal('bridgeTimeoutMs' in config, false)
  assert.equal('wallClockMs' in config, false)
  assert.equal('maxConcurrentSubagents' in config, false)
  assert.equal(config.expectedCliVersion, '0.153.4')
  assert.equal(config.expectedModelProvider, 'openai')
  assert.equal(config.expectedModel, 'gpt-6-astra')
  assert.equal(config.reasoningEffort, 'ultra')
  assert.equal(config.modelCatalogPath, built.modelCatalogPath)
  assert.equal(config.codexHome, built.overrides.codexHome)
  assert.equal(config.missionModelProjection.inputSchema.type, 'object')
  assert.equal('oneOf' in config.missionModelProjection.inputSchema, false)
  assert.deepEqual(
    (config.missionModelProjection.inputSchema.properties as Record<string, Record<string, unknown>>)
      .operation?.enum,
    ['usage', ...Object.keys(config.missionModelProjection.operationGuides)],
  )
  assert.equal(config.missionModelProjection.semanticRequestSchemaVersion, 'mathematical_research.mission_semantic_request.v1')
  assert.equal(config.missionModelProjection.usageIndex.semantic_operation_count, 9)
  assert.deepEqual(Object.keys(config.closeoutMissionModelProjection.operationGuides), [
    'record_strategy',
    'checkpoint',
  ])
  assert.equal(config.closeoutMissionModelProjection.usageIndex.semantic_operation_count, 2)
  const ordinaryStrategyInput = config.missionModelProjection.operationGuides
    .record_strategy.input_schema as Record<string, unknown>
  const ordinaryStrategyProperties = ordinaryStrategyInput.properties as Record<string, Record<string, unknown>>
  assert.deepEqual(ordinaryStrategyProperties.mission_continuation.enum, ['continue'])
  const closeoutStrategyInput = config.closeoutMissionModelProjection.operationGuides
    .record_strategy.input_schema as Record<string, unknown>
  const closeoutStrategyProperties = closeoutStrategyInput.properties as Record<string, Record<string, unknown>>
  assert.deepEqual(closeoutStrategyProperties.mission_continuation.enum, ['closeout'])
  assert.deepEqual(Object.keys(config.historicalReadModelProjection.operationGuides), ['orient', 'retrieve'])
  assert.equal(config.historicalReadModelProjection.usageIndex.tool, 'rh_mission_history')
  assert.equal(config.historicalReadModelProjection.usageIndex.semantic_operation_count, 2)
  assert.equal(config.historicalReadGrantInputSchema.additionalProperties, false)
  assert.deepEqual(config.researchReadGrantInputSchema, researchReadProjection().grant_input_schema)
  assert.deepEqual(config.researchReadRequestSchema, researchReadProjection().request_schema)
  assert.equal(config.candidateA1ReviewGrantInputSchema.additionalProperties, false)
  assert.deepEqual(
    ((config.candidateA1ReviewRequestSchema.oneOf as Array<Record<string, unknown>>).map(
      (branch) =>
        ((branch.properties as Record<string, Record<string, unknown>>).mode).const,
    )),
    ['usage', 'retrieve', 'submit'],
  )
  assert.equal(config.admissionCaseInputSchema.additionalProperties, false)
  assert.equal(config.admissionGrantInputSchema.additionalProperties, false)
  assert.deepEqual(
    ((config.admissionRequestSchema.oneOf as Array<Record<string, unknown>>).map(
      (branch) =>
        ((branch.properties as Record<string, Record<string, unknown>>).mode).const,
    )),
    ['usage', 'retrieve', 'submit', 'submit', 'submit'],
  )
  const admissionBranches = config.admissionRequestSchema.oneOf as Array<Record<string, unknown>>
  assert.equal(
    ((admissionBranches[3].properties as Record<string, Record<string, unknown>>).disposition).const,
    'authorize_exact_delta',
  )
  assert.equal(
    ((admissionBranches[4].properties as Record<string, Record<string, unknown>>).disposition).const,
    'reject',
  )
  assert.deepEqual(config.trustedMcpServerIds, [])
  assert.deepEqual(config.trustedAppIds, [])
})

test('config retains foreign-owner and writable-mode rejection with exact fixture ownership doubles', {
  skip: process.platform === 'win32',
}, () => {
  const built = fixture()
  // Deliberately model the wrong owner: the same production guard must reject it.
  fixtureOwnerUids.set(built.modelCatalogPath, 12345)
  assert.throws(() => readMissionHostConfig('mission.rh', built.overrides), /pinned Codex model catalog must be root-owned/)
  fixtureOwnerUids.set(built.modelCatalogPath, 0)
  const originalMode = fs.statSync(built.modelCatalogPath).mode & 0o777
  try {
    fs.chmodSync(built.modelCatalogPath, originalMode | 0o020)
    assert.throws(() => readMissionHostConfig('mission.rh', built.overrides), /pinned Codex model catalog must be root-owned and not group\/world writable/)
  } finally {
    fs.chmodSync(built.modelCatalogPath, originalMode)
  }
  assert.equal(readMissionHostConfig('mission.rh', built.overrides).modelCatalogPath, built.modelCatalogPath)
})

test('config applies no repository length ceiling to a path-safe Mission identity', () => {
  const built = fixture()
  const missionId = `mission.${'research-lineage.'.repeat(20)}rh`

  const config = readMissionHostConfig(missionId, built.overrides)

  assert.equal(config.missionId, missionId)
})

test('synthetic model fixture represents Astra/Ultra and the native result-capacity policy', () => {
  const bytes = fs.readFileSync(PINNED_MODEL_CATALOG_SOURCE)
  const source = bytes.toString('utf8')
  assert.equal(PINNED_CODEX_CLI_VERSION, '0.153.4')
  assert.ok(source.includes('"limit": 9223372036854775807'))
  const catalog = JSON.parse(source) as {
    models: Array<Record<string, unknown>>
  }
  assert.deepEqual(Object.keys(catalog), ['models'])
  assert.equal(catalog.models.length, 1)
  const model = catalog.models[0]!
  assert.equal(model.context_window, 272000)
  assert.equal(model.effective_context_window_percent, 95)
  assert.equal(model.tool_mode, 'code_mode_only')
  assert.deepEqual(model.experimental_supported_tools, [])
  assert.equal(model.slug, 'gpt-6-astra')
  assert.equal(model.minimal_client_version, '0.153.0')
  assert.equal(model.default_reasoning_level, 'ultra')
  assert.equal(model.multi_agent_version, 'v1')
  assert.deepEqual(model.supported_reasoning_levels, [
    { effort: 'ultra', description: 'Maximum reasoning with automatic task delegation' },
  ])
  assert.equal(typeof model.base_instructions, 'string')
  assert.equal(typeof model.model_messages, 'object')
  const truncationPolicy = model.truncation_policy as Record<string, unknown>
  assert.equal(truncationPolicy.mode, 'tokens')
  assert.equal(typeof truncationPolicy.limit, 'number')
  assert.ok(Number(truncationPolicy.limit) > 1e18)
})

test('public launch binds explicit paths and derives contracts from the immutable release root', () => {
  const built = fixture()
  const env: NodeJS.ProcessEnv = {
    RH_MISSION_HOST_MISSION_ID: 'mission.rh',
    RH_MISSION_HOST_RELEASE_COMMIT: RELEASE_SHA,
    RH_MISSION_HOST_REPO_ROOT: built.repoRoot,
    RH_MISSION_HOST_WORKSPACE_ROOT: built.overrides.missionWorkspaceRoot,
    RH_MISSION_HOST_GOALS_ROOT: built.overrides.goalsRoot,
    RH_MISSION_HOST_RUNTIME_DIR: built.overrides.runtimeDir,
    CODEX_HOME: built.overrides.codexHome,
    RH_MISSION_RUNTIME_LOCK: built.overrides.lockPath,
    RH_MISSION_HOST_PYTHON_PATH: built.overrides.pythonPath,
    RH_MISSION_HOST_CODEX_CLI_PATH: built.overrides.codexCliPath,
  }
  const config = readMissionHostConfigFromEnvironment(env, {
    pythonPath: built.overrides.pythonPath,
    codexCliPath: built.overrides.codexCliPath,
    codexHome: built.overrides.codexHome,
  })
  assert.equal(config.missionId, 'mission.rh')
  assert.equal(config.releaseSha, RELEASE_SHA)
  assert.equal(config.codexHome, built.overrides.codexHome)
  assert.equal(config.goalsRoot, built.overrides.goalsRoot)
  // The installation name is a location, never the source authority.
  assert.equal(readMissionHostConfig('mission.rh', {
    ...built.overrides, releaseSha: 'b'.repeat(40),
  }).releaseSha, 'b'.repeat(40))
})

test('production launch rejects a CODEX_HOME outside the configured protected root', () => {
  const built = fixture()
  const env: NodeJS.ProcessEnv = {
    RH_MISSION_HOST_MISSION_ID: 'mission.rh',
    RH_MISSION_HOST_RELEASE_COMMIT: RELEASE_SHA,
    RH_MISSION_HOST_REPO_ROOT: built.repoRoot,
    RH_MISSION_HOST_WORKSPACE_ROOT: built.overrides.missionWorkspaceRoot,
    RH_MISSION_HOST_GOALS_ROOT: built.overrides.goalsRoot,
    RH_MISSION_HOST_RUNTIME_DIR: built.overrides.runtimeDir,
    RH_MISSION_RUNTIME_LOCK: built.overrides.lockPath,
    RH_MISSION_HOST_PYTHON_PATH: built.overrides.pythonPath,
    RH_MISSION_HOST_CODEX_CLI_PATH: built.overrides.codexCliPath,
    CODEX_HOME: path.join(built.root, 'different-codex-home'),
  }
  assert.throws(
    () =>
      readMissionHostConfigFromEnvironment(env, {
        pythonPath: built.overrides.pythonPath,
        codexCliPath: built.overrides.codexCliPath,
        codexHome: built.overrides.codexHome,
      }),
    /CODEX_HOME differs from the path derived and verified/,
  )
})

test('config rejects a Codex home that overlaps a Goal-readable or writable root', () => {
  const built = fixture()
  assert.throws(
    () =>
      readMissionHostConfig('mission.rh', {
        ...built.overrides,
        codexHome: built.overrides.repoRoot,
      }),
    /paths must not overlap/,
  )
})

test('config rejects model-catalog policy drift before starting Codex', () => {
  const built = fixture()
  const catalog = JSON.parse(fs.readFileSync(built.modelCatalogPath, 'utf8')) as {
    models: Array<Record<string, unknown>>
  }
  catalog.models[0]!.default_reasoning_level = 'low'
  fs.writeFileSync(built.modelCatalogPath, JSON.stringify(catalog))
  assert.throws(
    () => readMissionHostConfig('mission.rh', built.overrides),
    /exact Astra\/Ultra policy/,
  )
})

test('config rejects a catalog that requires a newer Codex client before launch', () => {
  for (const version of ['0.153.5', '0.154.0', '1.0.0']) {
    const built = fixture()
    const catalog = JSON.parse(fs.readFileSync(built.modelCatalogPath, 'utf8'))
    catalog.models[0].minimal_client_version = version
    fs.writeFileSync(built.modelCatalogPath, JSON.stringify(catalog))
    assert.throws(
      () => readMissionHostConfig('mission.rh', built.overrides),
      /below the model catalog minimal_client_version/,
    )
  }
})

test('config requires an exact stable model minimum client version when supplied', () => {
  for (const version of [false, 153, '0.153', '0.153.0-alpha.1', '00.153.0', '0.153.0 ', '9007199254740992.0.0']) {
    const built = fixture()
    const catalog = JSON.parse(fs.readFileSync(built.modelCatalogPath, 'utf8'))
    catalog.models[0].minimal_client_version = version
    fs.writeFileSync(built.modelCatalogPath, JSON.stringify(catalog))
    assert.throws(
      () => readMissionHostConfig('mission.rh', built.overrides),
      /must declare a valid minimal_client_version/,
    )
  }
})

test('config accepts unspecified provider limits without inventing a minimum or compaction threshold', () => {
  for (const optional of [null, undefined]) {
    const built = fixture()
    const catalog = JSON.parse(fs.readFileSync(built.modelCatalogPath, 'utf8'))
    catalog.models[0].minimal_client_version = optional
    catalog.models[0].auto_compact_token_limit = optional
    const bytes = JSON.stringify(catalog)
    fs.writeFileSync(built.modelCatalogPath, bytes)
    const config = readMissionHostConfig('mission.rh', built.overrides)
    const policy = loadPinnedCodexModelCatalog(built.modelCatalogPath)
    assert.equal(config.expectedCliVersion, PINNED_CODEX_CLI_VERSION)
    assert.equal(config.expectedModel, 'gpt-6-astra')
    assert.equal(config.reasoningEffort, 'ultra')
    assert.equal(policy.configuredCompactionThresholdTokens, null)
    assert.equal(policy.effectiveContextAllowanceTokens, 258400)
    assert.equal(fs.readFileSync(built.modelCatalogPath, 'utf8'), bytes)
  }
})

test('config rejects a model catalog that switches static V1 to Multi-Agent V2', () => {
  const built = fixture()
  const catalog = JSON.parse(fs.readFileSync(built.modelCatalogPath, 'utf8')) as {
    models: Array<Record<string, unknown>>
  }
  catalog.models[0]!.multi_agent_version = 'v2'
  fs.writeFileSync(built.modelCatalogPath, JSON.stringify(catalog))
  assert.throws(
    () => readMissionHostConfig('mission.rh', built.overrides),
    /exact Astra\/Ultra policy/,
  )
})

test('config accounts for the modern literal template without replacing provider instruction blocks', () => {
  for (const optional of [null, undefined]) {
    const built = fixture()
    const catalog = JSON.parse(fs.readFileSync(built.modelCatalogPath, 'utf8'))
    catalog.models[0].base_instructions = optional
    catalog.models[0].model_messages = {
      instructions_template: 'Authored modern template: {{ personality }} remains literal with no variables.',
      instructions_variables: optional,
      persistent_instructions: 'Authored persistent-mode fixture; not part of the base template.',
      approvals: { never: 'Authored provider approval-policy fixture.' },
      collaboration_modes: { default: 'Authored provider collaboration fixture.' },
      multi_agent: { role: { root: 'Authored provider root-role fixture.' } },
    }
    const bytes = JSON.stringify(catalog)
    fs.writeFileSync(built.modelCatalogPath, bytes)
    const config = readMissionHostConfig('mission.rh', built.overrides)
    const policy = loadPinnedCodexModelCatalog(built.modelCatalogPath)
    assert.equal(config.expectedCliVersion, PINNED_CODEX_CLI_VERSION)
    assert.equal(config.reasoningEffort, 'ultra')
    assert.equal(policy.catalogInstructionTemplate, catalog.models[0].model_messages.instructions_template)
    assert.equal(policy.catalogInstructionTemplateSource, 'model_messages.instructions_template')
    assert.equal(fs.readFileSync(built.modelCatalogPath, 'utf8'), bytes)
  }
})

test('config validates model-facing owner structure directly', () => {
  const built = fixture()
  const projection = modelProjection()
  delete projection.root_tool
  fs.writeFileSync(built.projectionPath, JSON.stringify(projection))
  assert.throws(() => readMissionHostConfig('mission.rh', built.overrides), /wrong closed shape/)
})

test('config rejects a top-level oneOf or model-authored schema version at the front door', () => {
  const built = fixture()
  const projection = modelProjection()
  const rootTool = projection.root_tool as Record<string, unknown>
  const rootProjection = rootTool.model_projection as Record<string, unknown>
  const inputSchema = rootProjection.input_schema as Record<string, unknown>
  inputSchema.oneOf = []
  fs.writeFileSync(built.projectionPath, JSON.stringify(projection))
  assert.throws(() => readMissionHostConfig('mission.rh', built.overrides), /compact command grammar/)

  const second = fixture()
  const secondProjection = modelProjection()
  const secondRootTool = secondProjection.root_tool as Record<string, unknown>
  const secondRootProjection = secondRootTool.model_projection as Record<string, unknown>
  const secondInputSchema = secondRootProjection.input_schema as Record<string, unknown>
  const properties = secondInputSchema.properties as Record<string, unknown>
  properties.schema_version = { type: 'string' }
  fs.writeFileSync(second.projectionPath, JSON.stringify(secondProjection))
  assert.throws(() => readMissionHostConfig('mission.rh', second.overrides), /front-door properties/)
})

test('config rejects the obsolete six-operation model projection', () => {
  const built = fixture()
  const projection = modelProjection()
  const rootTool = projection.root_tool as Record<string, unknown>
  const rootProjection = rootTool.model_projection as Record<string, unknown>
  rootProjection.allowed_operations = [
    'orient',
    'retrieve',
    'record_strategy',
    'record_context',
    'interpret_material',
    'checkpoint',
  ]
  fs.writeFileSync(built.projectionPath, JSON.stringify(projection))
  assert.throws(() => readMissionHostConfig('mission.rh', built.overrides), /pinned owner contract/)
})

test('config rejects historical alias or grant-contract drift', () => {
  const aliasFixture = fixture()
  const aliasProjection = modelProjection()
  const historicalTool = aliasProjection.historical_read_tool as Record<string, unknown>
  historicalTool.name = 'rh_mission'
  fs.writeFileSync(aliasFixture.projectionPath, JSON.stringify(aliasProjection))
  assert.throws(() => readMissionHostConfig('mission.rh', aliasFixture.overrides), /tool names differ/)

  const grantFixture = fixture()
  const grantProjection = modelProjection()
  const grantTool = grantProjection.historical_read_tool as Record<string, unknown>
  const grantSchema = grantTool.grant_input_schema as Record<string, unknown>
  const properties = grantSchema.properties as Record<string, unknown>
  delete properties.raw_body_policy
  fs.writeFileSync(grantFixture.projectionPath, JSON.stringify(grantProjection))
  assert.throws(() => readMissionHostConfig('mission.rh', grantFixture.overrides), /grant input properties/)
})

test('config requires the research-read projection and exact tool aliases', () => {
  const missingFixture = fixture()
  const missingProjection = modelProjection()
  delete missingProjection.research_read_tool
  fs.writeFileSync(missingFixture.projectionPath, JSON.stringify(missingProjection))
  assert.throws(
    () => readMissionHostConfig('mission.rh', missingFixture.overrides),
    /wrong closed shape/,
  )

  for (const [field, alias] of [
    ['name', 'rh_mission_history'],
    ['grant_tool_name', 'rh_mission_a1_review_grant'],
    ['page_tool_name', 'rh_mission_page'],
  ]) {
    const built = fixture()
    const projection = modelProjection()
    const tool = projection.research_read_tool as Record<string, unknown>
    tool[field] = alias
    fs.writeFileSync(built.projectionPath, JSON.stringify(projection))
    assert.throws(
      () => readMissionHostConfig('mission.rh', built.overrides),
      /tool names differ/,
      field,
    )
  }
})

test('config rejects research-read grant authority and source-scope drift', () => {
  const cases: Array<{
    label: string
    alter: (schema: Record<string, unknown>, properties: Record<string, Record<string, unknown>>) => void
    error: RegExp
  }> = [
    {
      label: 'owner assignment identity cannot be model authored',
      alter: (_schema, properties) => { properties.assignment_id = { type: 'string' } },
      error: /research-read grant input schema properties.*wrong closed shape/,
    },
    {
      label: 'ordinary research does not reuse a historical Context grant',
      alter: (_schema, properties) => { properties.context = { type: 'object' } },
      error: /research-read grant input schema properties.*wrong closed shape/,
    },
    {
      label: 'assignment remains required',
      alter: (schema) => { schema.required = ['child_thread_id', 'source_families', 'raw_body_policy'] },
      error: /research-read grant input schema differs/,
    },
    {
      label: 'source family scope is nonempty',
      alter: (_schema, properties) => { properties.source_families.minItems = 0 },
      error: /research source-family array/,
    },
    {
      label: 'source family scope is duplicate free',
      alter: (_schema, properties) => { properties.source_families.uniqueItems = false },
      error: /research source-family array/,
    },
    {
      label: 'current Mission and Session sources remain available',
      alter: (_schema, properties) => {
        const items = properties.source_families.items as Record<string, unknown>
        items.enum = (items.enum as string[]).filter((family) => family !== 'sessions')
      },
      error: /research source families/,
    },
    {
      label: 'raw material exposure requires its exact declared policy',
      alter: (_schema, properties) => { properties.raw_body_policy.enum = ['metadata_only', 'all'] },
      error: /research raw-body policy/,
    },
  ]
  for (const item of cases) {
    const built = fixture()
    const projection = modelProjection()
    const tool = projection.research_read_tool as Record<string, unknown>
    const schema = tool.grant_input_schema as Record<string, unknown>
    item.alter(schema, schema.properties as Record<string, Record<string, unknown>>)
    fs.writeFileSync(built.projectionPath, JSON.stringify(projection))
    assert.throws(() => readMissionHostConfig('mission.rh', built.overrides), item.error, item.label)
  }
})

test('config rejects research-read request and selection authority drift', () => {
  const cases: Array<{
    label: string
    alter: (branches: Array<Record<string, unknown>>) => void
    error: RegExp
  }> = [
    {
      label: 'write mode cannot join the research-read interface',
      alter: (branches) => { branches.push(closedSchema({ mode: { const: 'submit' } })) },
      error: /research-read modes differ/,
    },
    {
      label: 'private cursor custody cannot be model authored',
      alter: (branches) => {
        const properties = branches[1].properties as Record<string, unknown>
        properties.research_query_context = { type: 'object' }
      },
      error: /research-read retrieval request properties.*wrong closed shape/,
    },
    {
      label: 'retrieval selection remains required',
      alter: (branches) => { branches[1].required = ['mode'] },
      error: /research-read retrieval request differs/,
    },
    {
      label: 'usage topics preserve the declared discovery surface',
      alter: (branches) => {
        const properties = branches[0].properties as Record<string, Record<string, unknown>>
        const items = properties.topics.items as Record<string, unknown>
        items.enum = ['reads', 'sources']
      },
      error: /research-read usage topics/,
    },
    {
      label: 'selected Context reading cannot disappear from ordinary research',
      alter: (branches) => {
        const properties = branches[1].properties as Record<string, Record<string, unknown>>
        const selections = properties.selection.oneOf as Array<Record<string, unknown>>
        selections.pop()
      },
      error: /research-read selection modes differ/,
    },
    {
      label: 'root-only checkpoint discovery cannot enter a research selection',
      alter: (branches) => {
        const properties = branches[1].properties as Record<string, Record<string, unknown>>
        const selections = properties.selection.oneOf as Array<Record<string, unknown>>
        const readProperties = selections[0].properties as Record<string, Record<string, unknown>>
        readProperties.mode.const = 'checkpoint'
      },
      error: /research-read selection modes differ/,
    },
    {
      label: 'owner selection branches remain closed',
      alter: (branches) => {
        const properties = branches[1].properties as Record<string, Record<string, unknown>>
        const selections = properties.selection.oneOf as Array<Record<string, unknown>>
        selections[0].additionalProperties = true
      },
      error: /research-read selection must retain closed/,
    },
  ]
  for (const item of cases) {
    const built = fixture()
    const projection = modelProjection()
    const tool = projection.research_read_tool as Record<string, unknown>
    const schema = tool.request_schema as Record<string, unknown>
    item.alter(schema.oneOf as Array<Record<string, unknown>>)
    fs.writeFileSync(built.projectionPath, JSON.stringify(projection))
    assert.throws(() => readMissionHostConfig('mission.rh', built.overrides), item.error, item.label)
  }
})

test('config rejects Candidate A1 review tool or owner schema drift', () => {
  const toolFixture = fixture()
  const toolProjection = modelProjection()
  const reviewTool = toolProjection.candidate_a1_review_tool as Record<string, unknown>
  reviewTool.page_tool_name = 'rh_mission_page'
  fs.writeFileSync(toolFixture.projectionPath, JSON.stringify(toolProjection))
  assert.throws(() => readMissionHostConfig('mission.rh', toolFixture.overrides), /tool names differ/)

  const grantFixture = fixture()
  const grantProjection = modelProjection()
  const grantTool = grantProjection.candidate_a1_review_tool as Record<string, unknown>
  const grantSchema = grantTool.grant_input_schema as Record<string, unknown>
  const grantProperties = grantSchema.properties as Record<string, unknown>
  delete grantProperties.candidate_ref
  fs.writeFileSync(grantFixture.projectionPath, JSON.stringify(grantProjection))
  assert.throws(
    () => readMissionHostConfig('mission.rh', grantFixture.overrides),
    /grant input schema properties has the wrong closed shape/,
  )

  const requestFixture = fixture()
  const requestProjection = modelProjection()
  const requestTool = requestProjection.candidate_a1_review_tool as Record<string, unknown>
  const requestSchema = requestTool.request_schema as Record<string, unknown>
  const branches = requestSchema.oneOf as Array<Record<string, unknown>>
  const submit = branches[2]!
  const submitProperties = submit.properties as Record<string, unknown>
  const disposition = submitProperties.disposition as Record<string, unknown>
  disposition.enum = ['invalidated']
  fs.writeFileSync(requestFixture.projectionPath, JSON.stringify(requestProjection))
  assert.throws(
    () => readMissionHostConfig('mission.rh', requestFixture.overrides),
    /disposition schema differs from the pinned owner contract/,
  )
})

test('config rejects closeout or Admission projection drift', () => {
  const ordinaryAuthorityFixture = fixture()
  const ordinaryAuthorityProjection = modelProjection()
  const ordinaryRoot = ordinaryAuthorityProjection.root_tool as Record<string, unknown>
  const ordinaryModel = ordinaryRoot.model_projection as Record<string, unknown>
  const ordinaryUsage = ordinaryModel.usage as Record<string, unknown>
  const ordinaryGuides = ordinaryUsage.operation_guides as Record<string, Record<string, unknown>>
  const ordinaryStrategy = ordinaryGuides.record_strategy
  const ordinaryInput = ordinaryStrategy.input_schema as Record<string, unknown>
  const ordinaryProperties = ordinaryInput.properties as Record<string, Record<string, unknown>>
  ordinaryProperties.mission_continuation.enum = ['continue', 'closeout']
  fs.writeFileSync(
    ordinaryAuthorityFixture.projectionPath,
    JSON.stringify(ordinaryAuthorityProjection),
  )
  assert.throws(
    () => readMissionHostConfig('mission.rh', ordinaryAuthorityFixture.overrides),
    /Strategy continuation schema differs from the pinned owner contract/,
  )

  const closeoutAuthorityFixture = fixture()
  const closeoutAuthorityProjection = modelProjection()
  const closeoutAuthorityRoot = closeoutAuthorityProjection.closeout_root_tool as Record<string, unknown>
  const closeoutAuthorityModel = closeoutAuthorityRoot.model_projection as Record<string, unknown>
  const closeoutAuthorityUsage = closeoutAuthorityModel.usage as Record<string, unknown>
  const closeoutAuthorityGuides = closeoutAuthorityUsage.operation_guides as Record<string, Record<string, unknown>>
  const closeoutAuthorityStrategy = closeoutAuthorityGuides.record_strategy
  const closeoutAuthorityExamples = closeoutAuthorityStrategy.examples as Array<Record<string, unknown>>
  const closeoutAuthorityCall = closeoutAuthorityExamples[0]!.call as Record<string, unknown>
  const closeoutAuthorityInput = closeoutAuthorityCall.input as Record<string, unknown>
  closeoutAuthorityInput.mission_continuation = 'continue'
  fs.writeFileSync(
    closeoutAuthorityFixture.projectionPath,
    JSON.stringify(closeoutAuthorityProjection),
  )
  assert.throws(
    () => readMissionHostConfig('mission.rh', closeoutAuthorityFixture.overrides),
    /Strategy examples grant the wrong continuation authority/,
  )

  const closeoutFixture = fixture()
  const closeoutProjection = modelProjection()
  const closeoutTool = closeoutProjection.closeout_root_tool as Record<string, unknown>
  const closeoutModel = closeoutTool.model_projection as Record<string, unknown>
  closeoutModel.allowed_operations = ['record_strategy']
  fs.writeFileSync(closeoutFixture.projectionPath, JSON.stringify(closeoutProjection))
  assert.throws(
    () => readMissionHostConfig('mission.rh', closeoutFixture.overrides),
    /pinned owner contract/,
  )

  const admissionFixture = fixture()
  const admissionProjectionValue = modelProjection()
  const admissionTool = admissionProjectionValue.admission_tool as Record<string, unknown>
  admissionTool.grant_tool_name = 'rh_mission_a1_review_grant'
  fs.writeFileSync(admissionFixture.projectionPath, JSON.stringify(admissionProjectionValue))
  assert.throws(
    () => readMissionHostConfig('mission.rh', admissionFixture.overrides),
    /tool names differ/,
  )

  const requestFixture = fixture()
  const requestProjection = modelProjection()
  const requestTool = requestProjection.admission_tool as Record<string, unknown>
  const requestSchema = requestTool.request_schema as Record<string, unknown>
  const decision = (requestSchema.oneOf as Array<Record<string, unknown>>)[3]!
  const properties = decision.properties as Record<string, unknown>
  const disposition = properties.disposition as Record<string, unknown>
  disposition.const = 'reject'
  fs.writeFileSync(requestFixture.projectionPath, JSON.stringify(requestProjection))
  assert.throws(
    () => readMissionHostConfig('mission.rh', requestFixture.overrides),
    /authorize submit request differs from the pinned owner contract/,
  )

  const rejectFixture = fixture()
  const rejectProjection = modelProjection()
  const rejectTool = rejectProjection.admission_tool as Record<string, unknown>
  const rejectSchema = rejectTool.request_schema as Record<string, unknown>
  const rejectDecision = (rejectSchema.oneOf as Array<Record<string, unknown>>)[4]!
  const rejectProperties = rejectDecision.properties as Record<string, unknown>
  const objections = rejectProperties.objections as Record<string, unknown>
  objections.minItems = 0
  fs.writeFileSync(rejectFixture.projectionPath, JSON.stringify(rejectProjection))
  assert.throws(
    () => readMissionHostConfig('mission.rh', rejectFixture.overrides),
    /reject objections schema/,
  )
})

test('runtime lock rejects a second live owner and releases by token', async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-mission-lock-'))
  const lockPath = path.join(runtimeDir, 'mission-host.lock.json')
  const lock = await acquireRuntimeLock(lockPath, RELEASE_SHA)
  await assert.rejects(() => acquireRuntimeLock(lockPath, RELEASE_SHA), /another RH Mission Host process/)
  await lock.release()
  assert.equal(fs.existsSync(lockPath), false)
})
