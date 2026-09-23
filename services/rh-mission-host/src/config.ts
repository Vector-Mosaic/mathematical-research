import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { RhObservationStoreLimits } from './observation-store.js'

export const PINNED_MODEL_PROVIDER = 'openai' as const
export const PINNED_MODEL = 'gpt-6-astra' as const
export const PINNED_REASONING_EFFORT = 'ultra' as const
export const PINNED_CODEX_CLI_VERSION = '0.153.4' as const
export const RH_PROJECT_ID = 'project.riemann_hypothesis' as const
export const RH_MISSION_POLL_INTERVAL_MS = 2000 as const
export const RH_MISSION_TRUSTED_MCP_SERVER_IDS: readonly string[] = []
export const RH_MISSION_TRUSTED_APP_IDS: readonly string[] = []

const HOST_MODEL_PROJECTION_SCHEMA_VERSION = 'mathematical_research.mission_host_model_projection.v1'
const MODEL_PROJECTION_SCHEMA_VERSION = 'mathematical_research.mission_model_projection.v2'
const MODEL_USAGE_SCHEMA_VERSION = 'mathematical_research.mission_model_usage.v1'
const SEMANTIC_REQUEST_SCHEMA_VERSION = 'mathematical_research.mission_semantic_request.v1'
const MODEL_USAGE_OPERATION = 'usage'
const MODEL_PROJECTION_NAME = '.mathematical-research-model-projection.json'
const MISSION_SEED_RELATIVE_PATH =
  'contracts/rh_autonomous_mission_seed.v1.json'
const SERVICE_ENTRYPOINT_RELATIVE_PATH =
  'services/rh-mission-host/dist/main.js'
export const PINNED_MODEL_CATALOG_RELATIVE_PATH =
  `services/rh-mission-host/assets/codex-model-catalog.${PINNED_CODEX_CLI_VERSION}.json`
const REQUIRED_OPERATIONS = [
  'orient',
  'retrieve',
  'record_context',
  'interpret_material',
  'record_candidate',
  'record_branch',
  'synthesize',
  'record_strategy',
  'checkpoint',
] as const
const HISTORICAL_READ_OPERATIONS = ['orient', 'retrieve'] as const
const CLOSEOUT_OPERATIONS = ['record_strategy', 'checkpoint'] as const
const HISTORICAL_ASSIGNMENT_MODES = [
  'historical_opportunity_scout',
  'lifecycle_historian',
] as const
const HISTORICAL_SOURCE_FAMILIES = [
  'branches',
  'candidates',
  'strategies',
  'contexts',
  'evidence',
  'capture_annotations',
  'captures',
  'capture_artifacts',
] as const
const HISTORICAL_RAW_BODY_POLICIES = [
  'metadata_only',
  'allow_untrusted_material',
] as const
const RESEARCH_SOURCE_FAMILIES = [...HISTORICAL_SOURCE_FAMILIES, 'missions', 'sessions'] as const
const RESEARCH_READ_MODES = ['usage', 'retrieve'] as const
const RESEARCH_READ_USAGE_TOPICS = ['reads', 'sources', 'continuations', 'authority'] as const
const RESEARCH_READ_SELECTION_MODES = [
  'read', 'search', 'history_inventory', 'history_search', 'history_read', 'history_traverse',
  'selected_context',
] as const
const CANDIDATE_A1_REVIEW_MODES = ['usage', 'retrieve', 'submit'] as const
const CANDIDATE_A1_REVIEW_DISPOSITIONS = ['invalidated', 'admission_ready'] as const
const ADMISSION_GRANT_ROLES = ['reviewer', 'admitter'] as const
const ADMISSION_REVIEW_DISPOSITIONS = ['no_material_objection', 'material_objection'] as const
const RELEASE_SHA = /^[0-9a-f]{40}$/
const MISSION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/
const DEFAULT_WORKSPACE_ROOT = '/var/lib/mathematical-research/projects'
const DEFAULT_GOALS_ROOT = '/var/lib/mathematical-research/goals'
const DEFAULT_RUNTIME_ROOT = '/var/lib/mathematical-research/runtime'
const DEFAULT_CODEX_HOME = '/var/lib/mathematical-research/codex-home'
const DEFAULT_RUNTIME_LOCK_PATH = '/var/lib/mathematical-research/runtime.lock'
const DEFAULT_PYTHON_PATH = '/usr/bin/python3'
const DEFAULT_CODEX_CLI_PATH = '/usr/bin/codex'
const DEFAULT_REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  '..',
)

export interface MissionModelProjection {
  inputSchema: Record<string, unknown>
  semanticRequestSchemaVersion: typeof SEMANTIC_REQUEST_SCHEMA_VERSION
  usageIndex: Record<string, unknown>
  operationGuides: Readonly<Record<string, Record<string, unknown>>>
}

export interface MissionHostConfig {
  /** Optional source/test override; production uses the bounded observation-store defaults. */
  observationLimits?: Partial<RhObservationStoreLimits>
  repoRoot: string
  releaseSha: string
  serviceEntrypointPath: string
  missionWorkspaceRoot: string
  goalsRoot: string
  runtimeDir: string
  codexHome: string
  statePath: string
  lockPath: string
  pythonPath: string
  codexCliPath: string
  modelCatalogPath: string
  missionScriptPath: string
  missionModelProjectionPath: string
  missionModelProjection: MissionModelProjection
  closeoutMissionModelProjection: MissionModelProjection
  historicalReadModelProjection: MissionModelProjection
  historicalReadGrantInputSchema: Record<string, unknown>
  researchReadGrantInputSchema: Record<string, unknown>
  researchReadRequestSchema: Record<string, unknown>
  candidateA1ReviewGrantInputSchema: Record<string, unknown>
  candidateA1ReviewRequestSchema: Record<string, unknown>
  admissionCaseInputSchema: Record<string, unknown>
  admissionGrantInputSchema: Record<string, unknown>
  admissionRequestSchema: Record<string, unknown>
  projectId: string
  missionId: string
  pollIntervalMs: number
  permissionProfileId: string
  trustedMcpServerIds: readonly string[]
  trustedAppIds: readonly string[]
  expectedCliVersion: typeof PINNED_CODEX_CLI_VERSION
  expectedModelProvider: typeof PINNED_MODEL_PROVIDER
  expectedModel: typeof PINNED_MODEL
  reasoningEffort: typeof PINNED_REASONING_EFFORT
}

/** Explicit operator-owned paths; no connection to another installation is inferred. */
export interface MissionHostPathOverrides {
  repoRoot?: string
  releaseSha?: string
  missionWorkspaceRoot?: string
  goalsRoot?: string
  runtimeDir?: string
  codexHome?: string
  lockPath?: string
  pythonPath?: string
  codexCliPath?: string
}

export interface RuntimeLock {
  readonly path: string
  release(): Promise<void>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    return JSON.stringify(value)
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return JSON.stringify(value)
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`
  }
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(',')}}`
  }
  throw new Error('owner JSON contains a non-JSON value')
}

function requiredText(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value || value.trim() !== value) {
    throw new Error(`${label} must be one non-empty trimmed string`)
  }
  return value
}

function requireExactKeys(value: Record<string, unknown>, expected: readonly string[], label: string): void {
  const actual = Object.keys(value).sort()
  const sortedExpected = [...expected].sort()
  if (actual.length !== sortedExpected.length || actual.some((key, index) => key !== sortedExpected[index])) {
    throw new Error(`${label} has the wrong closed shape`)
  }
}

function requireExisting(pathValue: string, kind: 'file' | 'directory', label: string): void {
  let stat: fs.Stats
  try {
    stat = fs.statSync(pathValue)
  } catch (error) {
    throw new Error(`${label} does not exist: ${pathValue}`, { cause: error })
  }
  if ((kind === 'file' && !stat.isFile()) || (kind === 'directory' && !stat.isDirectory())) {
    throw new Error(`${label} is not a ${kind}: ${pathValue}`)
  }
}

function requireRootOwnedImmutablePath(pathValue: string, kind: 'file' | 'directory', label: string): fs.Stats {
  let stat: fs.Stats
  try {
    stat = fs.lstatSync(pathValue)
  } catch (error) {
    throw new Error(`${label} does not exist: ${pathValue}`, { cause: error })
  }
  if (stat.isSymbolicLink()) {
    throw new Error(`${label} must not be a symbolic link`)
  }
  if ((kind === 'file' && !stat.isFile()) || (kind === 'directory' && !stat.isDirectory())) {
    throw new Error(`${label} is not a ${kind}: ${pathValue}`)
  }
  if (process.platform !== 'win32' && (stat.uid !== 0 || (stat.mode & 0o022) !== 0)) {
    throw new Error(`${label} must be root-owned and not group/world writable`)
  }
  return stat
}

function requireAbsolutePath(value: string, label: string): string {
  if (!path.isAbsolute(value)) {
    throw new Error(`${label} must be an absolute path`)
  }
  return path.resolve(value)
}

function pathKey(value: string): string {
  const resolved = path.resolve(value)
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved
}

function pathsOverlap(left: string, right: string): boolean {
  const leftKey = pathKey(left)
  const rightKey = pathKey(right)
  const relativeLeft = path.relative(leftKey, rightKey)
  const relativeRight = path.relative(rightKey, leftKey)
  return (
    leftKey === rightKey ||
    (!relativeLeft.startsWith('..') && !path.isAbsolute(relativeLeft)) ||
    (!relativeRight.startsWith('..') && !path.isAbsolute(relativeRight))
  )
}

function readJsonFile(filePath: string, label: string): unknown {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (error) {
    throw new Error(`${label} is not one readable JSON document`, { cause: error })
  }
}

function parseMissionModelProjection(
  value: unknown,
  requiredOperations: readonly string[],
  toolName: string,
  label: string,
  expectedStrategyContinuation: 'continue' | 'closeout' | null,
): MissionModelProjection {
  if (!isRecord(value)) {
    throw new Error(`${label} must be an object`)
  }
  const allowedOperations = value.allowed_operations
  const inputSchema = value.input_schema
  const usage = value.usage
  requireExactKeys(
    value,
    ['allowed_operations', 'input_schema', 'schema_version', 'semantic_request_schema_version', 'usage'],
    label,
  )
  if (
    value.schema_version !== MODEL_PROJECTION_SCHEMA_VERSION ||
    value.semantic_request_schema_version !== SEMANTIC_REQUEST_SCHEMA_VERSION ||
    !Array.isArray(allowedOperations) ||
    canonicalJson(allowedOperations) !== canonicalJson(requiredOperations) ||
    !isRecord(inputSchema) ||
    !isRecord(usage)
  ) {
    throw new Error(`${label} differs from the pinned owner contract`)
  }
  requireExactKeys(usage, ['index', 'operation_guides'], 'RH Mission model usage projection')
  const usageIndex = usage.index
  const operationGuides = usage.operation_guides
  if (!isRecord(usageIndex) || !isRecord(operationGuides)) {
    throw new Error('RH Mission model usage projection differs from the pinned owner contract')
  }
  requireExactKeys(operationGuides, requiredOperations, `${label} operation guides`)
  if (
    usageIndex.schema_version !== MODEL_USAGE_SCHEMA_VERSION ||
    usageIndex.tool !== toolName ||
    usageIndex.status !== 'ok' ||
    usageIndex.semantic_operation_count !== requiredOperations.length ||
    !Array.isArray(usageIndex.operations)
  ) {
    throw new Error('RH Mission model usage index differs from the pinned owner contract')
  }
  const properties = inputSchema.properties
  if (!isRecord(properties)) {
    throw new Error('RH Mission model front door differs from the compact command grammar')
  }
  requireExactKeys(properties, ['operation', 'input'], 'RH Mission model front-door properties')
  if (
    inputSchema.type !== 'object' ||
    inputSchema.additionalProperties !== false ||
    'oneOf' in inputSchema ||
    !isRecord(properties.operation) ||
    !isRecord(properties.input) ||
    canonicalJson(properties.operation.enum) !== canonicalJson([MODEL_USAGE_OPERATION, ...requiredOperations]) ||
    properties.input.type !== 'object' ||
    canonicalJson(inputSchema.required) !== canonicalJson(['operation', 'input'])
  ) {
    throw new Error('RH Mission model front door differs from the compact command grammar')
  }
  for (const operation of requiredOperations) {
    const guide = operationGuides[operation]
    if (
      !isRecord(guide) ||
      guide.schema_version !== MODEL_USAGE_SCHEMA_VERSION ||
      guide.tool !== toolName ||
      guide.status !== 'ok' ||
      guide.operation !== operation ||
      !isRecord(guide.input_schema) ||
      !Array.isArray(guide.examples)
    ) {
      throw new Error(`RH Mission model guide for ${operation} differs from its owner projection`)
    }
  }
  if (expectedStrategyContinuation !== null) {
    const strategyGuide = operationGuides.record_strategy
    const strategyInput = isRecord(strategyGuide)
      ? strategyGuide.input_schema
      : null
    const strategyProperties = isRecord(strategyInput)
      ? strategyInput.properties
      : null
    const continuationSchema = isRecord(strategyProperties)
      ? strategyProperties.mission_continuation
      : null
    requireEnumSchema(
      continuationSchema,
      [expectedStrategyContinuation],
      `${label} Strategy continuation schema`,
    )
    const examples = isRecord(strategyGuide) ? strategyGuide.examples : null
    if (
      !Array.isArray(examples) ||
      examples.length === 0 ||
      examples.some((example) => {
        const call = isRecord(example) ? example.call : null
        const input = isRecord(call) ? call.input : null
        return !isRecord(call) || call.operation !== 'record_strategy' ||
          !isRecord(input) || input.mission_continuation !== expectedStrategyContinuation
      })
    ) {
      throw new Error(`${label} Strategy examples grant the wrong continuation authority`)
    }
  }
  return {
    inputSchema,
    semanticRequestSchemaVersion: SEMANTIC_REQUEST_SCHEMA_VERSION,
    usageIndex,
    operationGuides: operationGuides as Record<string, Record<string, unknown>>,
  }
}

function requireEnumSchema(
  value: unknown,
  expected: readonly string[],
  label: string,
): void {
  if (
    !isRecord(value) ||
    value.type !== 'string' ||
    canonicalJson(value.enum) !== canonicalJson(expected)
  ) {
    throw new Error(`${label} differs from the pinned owner contract`)
  }
}

function validateHistoricalReadGrantInputSchema(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error('RH Mission historical-read grant input schema must be an object')
  }
  requireExactKeys(
    value,
    ['additionalProperties', 'properties', 'required', 'type'],
    'RH Mission historical-read grant input schema',
  )
  const properties = value.properties
  if (
    value.type !== 'object' ||
    value.additionalProperties !== false ||
    !isRecord(properties) ||
    canonicalJson(value.required) !== canonicalJson([
      'child_thread_id',
      'assignment_mode',
      'assignment',
      'context',
      'source_families',
      'raw_body_policy',
    ])
  ) {
    throw new Error('RH Mission historical-read grant input schema differs from the pinned owner contract')
  }
  requireExactKeys(
    properties,
    ['assignment', 'assignment_mode', 'child_thread_id', 'context', 'raw_body_policy', 'source_families'],
    'RH Mission historical-read grant input properties',
  )
  for (const key of ['child_thread_id', 'assignment'] as const) {
    const schema = properties[key]
    if (!isRecord(schema) || schema.type !== 'string' || schema.minLength !== 1) {
      throw new Error(`RH Mission historical-read grant ${key} schema differs from the pinned owner contract`)
    }
  }
  requireEnumSchema(properties.assignment_mode, HISTORICAL_ASSIGNMENT_MODES, 'historical assignment mode')
  requireEnumSchema(properties.raw_body_policy, HISTORICAL_RAW_BODY_POLICIES, 'historical raw-body policy')
  const context = properties.context
  if (!isRecord(context) || context.type !== 'object' || context.additionalProperties !== false) {
    throw new Error('RH Mission historical-read grant Context schema differs from the pinned owner contract')
  }
  const contextProperties = context.properties
  if (
    !isRecord(contextProperties) ||
    canonicalJson(context.required) !== canonicalJson(['id', 'revision'])
  ) {
    throw new Error('RH Mission historical-read grant Context schema differs from the pinned owner contract')
  }
  requireExactKeys(contextProperties, ['id', 'revision'], 'RH Mission historical-read grant Context properties')
  const contextId = contextProperties.id
  const contextRevision = contextProperties.revision
  if (
    !isRecord(contextId) ||
    contextId.type !== 'string' ||
    typeof contextId.pattern !== 'string' ||
    !isRecord(contextRevision) ||
    contextRevision.type !== 'integer' ||
    contextRevision.minimum !== 1
  ) {
    throw new Error('RH Mission historical-read grant Context schema differs from the pinned owner contract')
  }
  const sourceFamilies = properties.source_families
  if (
    !isRecord(sourceFamilies) ||
    sourceFamilies.type !== 'array' ||
    sourceFamilies.minItems !== 1 ||
    sourceFamilies.uniqueItems !== true ||
    !isRecord(sourceFamilies.items)
  ) {
    throw new Error('RH Mission historical-read source-family schema differs from the pinned owner contract')
  }
  requireEnumSchema(sourceFamilies.items, HISTORICAL_SOURCE_FAMILIES, 'historical source families')
  return value
}

function requireTextSchema(value: unknown, label: string): void {
  if (!isRecord(value) || value.type !== 'string' || value.minLength !== 1) {
    throw new Error(`${label} differs from the pinned owner contract`)
  }
}

function validateResearchReadGrantInputSchema(value: unknown): Record<string, unknown> {
  const properties = requireClosedObjectSchema(
    value,
    ['assignment', 'child_thread_id', 'raw_body_policy', 'source_families'],
    ['child_thread_id', 'assignment', 'source_families', 'raw_body_policy'],
    'RH Mission research-read grant input schema',
  )
  requireTextSchema(properties.child_thread_id, 'research-read child_thread_id schema')
  requireTextSchema(properties.assignment, 'research-read assignment schema')
  requireEnumSchema(properties.raw_body_policy, HISTORICAL_RAW_BODY_POLICIES, 'research raw-body policy')
  requireEnumSchema(
    requireOwnerArraySchema(properties.source_families, 1, 'research source-family array'),
    RESEARCH_SOURCE_FAMILIES,
    'research source families',
  )
  return value as Record<string, unknown>
}

function validateResearchReadRequestSchema(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error('RH Mission research-read request schema must be an object')
  }
  requireExactKeys(value, ['oneOf'], 'RH Mission research-read request schema')
  if (!Array.isArray(value.oneOf) || value.oneOf.length !== RESEARCH_READ_MODES.length) {
    throw new Error('RH Mission research-read modes differ from the pinned owner contract')
  }
  const [usage, retrieve] = value.oneOf
  const usageProperties = requireClosedObjectSchema(
    usage, ['mode', 'topics'], ['mode'], 'research-read usage request',
  )
  const retrieveProperties = requireClosedObjectSchema(
    retrieve, ['mode', 'selection'], ['mode', 'selection'], 'research-read retrieval request',
  )
  if (
    !isRecord(usageProperties.mode) || canonicalJson(usageProperties.mode) !== '{"const":"usage"}' ||
    !isRecord(retrieveProperties.mode) || canonicalJson(retrieveProperties.mode) !== '{"const":"retrieve"}'
  ) {
    throw new Error('RH Mission research-read modes differ from the pinned owner contract')
  }
  requireEnumSchema(
    requireOwnerArraySchema(usageProperties.topics, 1, 'research-read usage topics'),
    RESEARCH_READ_USAGE_TOPICS,
    'research-read usage topics',
  )
  const selection = retrieveProperties.selection
  if (!isRecord(selection)) {
    throw new Error('research-read selection schema must be an object')
  }
  requireExactKeys(selection, ['oneOf'], 'research-read selection schema')
  if (!Array.isArray(selection.oneOf)) {
    throw new Error('research-read selection modes are absent')
  }
  const modes = selection.oneOf.map((branch) => {
    if (
      !isRecord(branch) || branch.type !== 'object' || branch.additionalProperties !== false ||
      !isRecord(branch.properties) || !isRecord(branch.properties.mode) ||
      !Array.isArray(branch.required) || !branch.required.includes('mode') ||
      typeof branch.properties.mode.const !== 'string'
    ) {
      throw new Error('research-read selection must retain closed owner-projected modes')
    }
    return branch.properties.mode.const
  })
  if (canonicalJson([...modes].sort()) !== canonicalJson([...RESEARCH_READ_SELECTION_MODES].sort())) {
    throw new Error('research-read selection modes differ from the pinned owner contract')
  }
  return value
}

function requireClosedObjectSchema(
  value: unknown,
  expectedProperties: readonly string[],
  expectedRequired: readonly string[],
  label: string,
): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error(`${label} must be an object`)
  }
  requireExactKeys(
    value,
    ['additionalProperties', 'properties', 'required', 'type'],
    label,
  )
  if (
    value.type !== 'object' ||
    value.additionalProperties !== false ||
    !isRecord(value.properties) ||
    canonicalJson(value.required) !== canonicalJson(expectedRequired)
  ) {
    throw new Error(`${label} differs from the pinned owner contract`)
  }
  requireExactKeys(value.properties, expectedProperties, `${label} properties`)
  return value.properties
}

function requireRecordReferenceSchema(value: unknown, prefix: string, label: string): void {
  const properties = requireClosedObjectSchema(
    value,
    ['id', 'payload_sha256', 'revision'],
    ['id', 'revision', 'payload_sha256'],
    label,
  )
  const id = properties.id
  const revision = properties.revision
  const payloadSha256 = properties.payload_sha256
  if (
    !isRecord(id) ||
    id.type !== 'string' ||
    typeof id.pattern !== 'string' ||
    !id.pattern.startsWith(`^(?:${prefix}):`) ||
    !isRecord(revision) ||
    revision.type !== 'integer' ||
    revision.minimum !== 1 ||
    !isRecord(payloadSha256) ||
    payloadSha256.type !== 'string' ||
    payloadSha256.pattern !== '^[0-9a-f]{64}(?![\\s\\S])'
  ) {
    throw new Error(`${label} differs from the pinned owner contract`)
  }
}

function requireOwnerArraySchema(
  value: unknown,
  minimumItems: number,
  label: string,
): Record<string, unknown> {
  if (
    !isRecord(value) ||
    value.type !== 'array' ||
    value.minItems !== minimumItems ||
    value.uniqueItems !== true ||
    !isRecord(value.items)
  ) {
    throw new Error(`${label} differs from the pinned owner contract`)
  }
  return value.items
}

function validateCandidateA1ReviewGrantInputSchema(value: unknown): Record<string, unknown> {
  const properties = requireClosedObjectSchema(
    value,
    ['assignment', 'candidate_ref', 'child_thread_id', 'context'],
    ['child_thread_id', 'assignment', 'context', 'candidate_ref'],
    'RH Mission Candidate A1 review grant input schema',
  )
  requireTextSchema(properties.child_thread_id, 'RH Mission Candidate A1 review child_thread_id schema')
  requireTextSchema(properties.assignment, 'RH Mission Candidate A1 review assignment schema')
  const contextProperties = requireClosedObjectSchema(
    properties.context,
    ['id', 'revision'],
    ['id', 'revision'],
    'RH Mission Candidate A1 review Context schema',
  )
  const contextId = contextProperties.id
  const contextRevision = contextProperties.revision
  if (
    !isRecord(contextId) ||
    contextId.type !== 'string' ||
    typeof contextId.pattern !== 'string' ||
    !contextId.pattern.startsWith('^(?:context):') ||
    !isRecord(contextRevision) ||
    contextRevision.type !== 'integer' ||
    contextRevision.minimum !== 1
  ) {
    throw new Error('RH Mission Candidate A1 review Context schema differs from the pinned owner contract')
  }
  requireRecordReferenceSchema(
    properties.candidate_ref,
    'candidate',
    'RH Mission Candidate A1 review Candidate reference schema',
  )
  return value as Record<string, unknown>
}

function validateCandidateA1ReviewRequestSchema(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error('RH Mission Candidate A1 review request schema must be an object')
  }
  requireExactKeys(value, ['oneOf'], 'RH Mission Candidate A1 review request schema')
  const branches = value.oneOf
  if (!Array.isArray(branches) || branches.length !== CANDIDATE_A1_REVIEW_MODES.length) {
    throw new Error('RH Mission Candidate A1 review request schema differs from the pinned owner contract')
  }

  const byMode = new Map<string, Record<string, unknown>>()
  for (const branch of branches) {
    if (!isRecord(branch) || !isRecord(branch.properties) || !isRecord(branch.properties.mode)) {
      throw new Error('RH Mission Candidate A1 review request branch differs from the pinned owner contract')
    }
    const mode = branch.properties.mode.const
    if (typeof mode !== 'string' || byMode.has(mode)) {
      throw new Error('RH Mission Candidate A1 review request modes differ from the pinned owner contract')
    }
    byMode.set(mode, branch)
  }
  if (canonicalJson([...byMode.keys()]) !== canonicalJson(CANDIDATE_A1_REVIEW_MODES)) {
    throw new Error('RH Mission Candidate A1 review request modes differ from the pinned owner contract')
  }

  const usageProperties = requireClosedObjectSchema(
    byMode.get('usage'),
    ['mode'],
    ['mode'],
    'RH Mission Candidate A1 review usage request',
  )
  if (!isRecord(usageProperties.mode) || canonicalJson(usageProperties.mode) !== '{"const":"usage"}') {
    throw new Error('RH Mission Candidate A1 review usage request differs from the pinned owner contract')
  }

  const retrieveProperties = requireClosedObjectSchema(
    byMode.get('retrieve'),
    ['cursor', 'mode', 'page_size'],
    ['mode'],
    'RH Mission Candidate A1 review retrieve request',
  )
  const retrieveMode = retrieveProperties.mode
  const pageSize = retrieveProperties.page_size
  if (
    !isRecord(retrieveMode) ||
    retrieveMode.const !== 'retrieve' ||
    !isRecord(pageSize) ||
    pageSize.type !== 'integer' ||
    pageSize.minimum !== 1 ||
    pageSize.maximum !== 50
  ) {
    throw new Error('RH Mission Candidate A1 review retrieve request differs from the pinned owner contract')
  }
  requireTextSchema(retrieveProperties.cursor, 'RH Mission Candidate A1 review cursor schema')

  const submitProperties = requireClosedObjectSchema(
    byMode.get('submit'),
    [
      'cited_basis',
      'concrete_defects',
      'disposition',
      'limitations',
      'mode',
      'no_remaining_material_objection',
      'non_inferences',
      'review_finding',
    ],
    ['mode', 'disposition', 'review_finding', 'no_remaining_material_objection'],
    'RH Mission Candidate A1 review submit request',
  )
  if (
    !isRecord(submitProperties.mode) ||
    submitProperties.mode.const !== 'submit' ||
    !isRecord(submitProperties.no_remaining_material_objection) ||
    submitProperties.no_remaining_material_objection.type !== 'boolean'
  ) {
    throw new Error('RH Mission Candidate A1 review submit request differs from the pinned owner contract')
  }
  requireEnumSchema(
    submitProperties.disposition,
    CANDIDATE_A1_REVIEW_DISPOSITIONS,
    'RH Mission Candidate A1 review disposition schema',
  )
  requireTextSchema(submitProperties.review_finding, 'RH Mission Candidate A1 review finding schema')
  requireRecordReferenceSchema(
    requireOwnerArraySchema(submitProperties.cited_basis, 0, 'RH Mission Candidate A1 cited basis schema'),
    'evidence',
    'RH Mission Candidate A1 cited basis reference schema',
  )
  const defectProperties = requireClosedObjectSchema(
    requireOwnerArraySchema(
      submitProperties.concrete_defects,
      0,
      'RH Mission Candidate A1 concrete defects schema',
    ),
    ['affected_scope', 'exact_defect', 'sufficiency_basis'],
    ['exact_defect', 'affected_scope', 'sufficiency_basis'],
    'RH Mission Candidate A1 concrete defect schema',
  )
  requireTextSchema(defectProperties.exact_defect, 'RH Mission Candidate A1 exact defect schema')
  requireTextSchema(defectProperties.affected_scope, 'RH Mission Candidate A1 affected scope schema')
  requireTextSchema(defectProperties.sufficiency_basis, 'RH Mission Candidate A1 sufficiency basis schema')
  for (const key of ['limitations', 'non_inferences'] as const) {
    const item = requireOwnerArraySchema(
      submitProperties[key],
      0,
      `RH Mission Candidate A1 ${key} schema`,
    )
    requireTextSchema(item, `RH Mission Candidate A1 ${key} item schema`)
  }
  return value
}

function requireContextSelectorSchema(value: unknown, label: string): void {
  const properties = requireClosedObjectSchema(
    value,
    ['id', 'revision'],
    ['id', 'revision'],
    label,
  )
  const id = properties.id
  const revision = properties.revision
  if (
    !isRecord(id) ||
    id.type !== 'string' ||
    typeof id.pattern !== 'string' ||
    !id.pattern.startsWith('^(?:context):') ||
    !isRecord(revision) ||
    revision.type !== 'integer' ||
    revision.minimum !== 1
  ) {
    throw new Error(`${label} differs from the pinned owner contract`)
  }
}

function validateAdmissionCaseInputSchema(value: unknown): Record<string, unknown> {
  const properties = requireClosedObjectSchema(
    value,
    ['candidate_ref'],
    ['candidate_ref'],
    'RH Mission Admission Case input schema',
  )
  requireRecordReferenceSchema(
    properties.candidate_ref,
    'candidate',
    'RH Mission Admission Case Candidate reference schema',
  )
  return value as Record<string, unknown>
}

function validateAdmissionGrantInputSchema(value: unknown): Record<string, unknown> {
  const properties = requireClosedObjectSchema(
    value,
    ['assignment', 'case_ref', 'child_thread_id', 'context', 'role'],
    ['role', 'child_thread_id', 'assignment', 'context', 'case_ref'],
    'RH Mission Admission grant input schema',
  )
  requireEnumSchema(properties.role, ADMISSION_GRANT_ROLES, 'RH Mission Admission grant role schema')
  requireTextSchema(properties.child_thread_id, 'RH Mission Admission child_thread_id schema')
  requireTextSchema(properties.assignment, 'RH Mission Admission assignment schema')
  requireContextSelectorSchema(properties.context, 'RH Mission Admission Context schema')
  requireRecordReferenceSchema(
    properties.case_ref,
    'evidence',
    'RH Mission Admission Case reference schema',
  )
  return value as Record<string, unknown>
}

function requireTextArraySchema(value: unknown, label: string): void {
  const item = requireOwnerArraySchema(value, 0, label)
  requireTextSchema(item, `${label} item`)
}

function validateAdmissionRequestSchema(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error('RH Mission Admission request schema must be an object')
  }
  requireExactKeys(value, ['oneOf'], 'RH Mission Admission request schema')
  const branches = value.oneOf
  if (!Array.isArray(branches) || branches.length !== 5) {
    throw new Error('RH Mission Admission request schema differs from the pinned owner contract')
  }
  const usageProperties = requireClosedObjectSchema(
    branches[0], ['mode'], ['mode'], 'RH Mission Admission usage request',
  )
  if (!isRecord(usageProperties.mode) || usageProperties.mode.const !== 'usage') {
    throw new Error('RH Mission Admission usage request differs from the pinned owner contract')
  }
  const retrieveProperties = requireClosedObjectSchema(
    branches[1],
    ['cursor', 'mode', 'page_size'],
    ['mode'],
    'RH Mission Admission retrieve request',
  )
  if (
    !isRecord(retrieveProperties.mode) ||
    retrieveProperties.mode.const !== 'retrieve' ||
    !isRecord(retrieveProperties.page_size) ||
    retrieveProperties.page_size.type !== 'integer' ||
    retrieveProperties.page_size.minimum !== 1 ||
    retrieveProperties.page_size.maximum !== 50
  ) {
    throw new Error('RH Mission Admission retrieve request differs from the pinned owner contract')
  }
  requireTextSchema(retrieveProperties.cursor, 'RH Mission Admission cursor schema')

  const reviewProperties = requireClosedObjectSchema(
    branches[2],
    ['cited_basis', 'disposition', 'limitations', 'mode', 'non_inferences', 'objections', 'review_finding'],
    ['mode', 'disposition', 'review_finding'],
    'RH Mission Admission Review submit request',
  )
  if (!isRecord(reviewProperties.mode) || reviewProperties.mode.const !== 'submit') {
    throw new Error('RH Mission Admission Review submit request differs from the pinned owner contract')
  }
  requireEnumSchema(
    reviewProperties.disposition,
    ADMISSION_REVIEW_DISPOSITIONS,
    'RH Mission Admission Review disposition schema',
  )
  requireTextSchema(reviewProperties.review_finding, 'RH Mission Admission Review finding schema')
  const objectionProperties = requireClosedObjectSchema(
    requireOwnerArraySchema(reviewProperties.objections, 0, 'RH Mission Admission objections schema'),
    ['affected_scope', 'exact_objection', 'materiality_basis'],
    ['exact_objection', 'affected_scope', 'materiality_basis'],
    'RH Mission Admission objection schema',
  )
  for (const key of ['affected_scope', 'exact_objection', 'materiality_basis'] as const) {
    requireTextSchema(objectionProperties[key], `RH Mission Admission objection ${key} schema`)
  }
  const citedBasis = requireClosedObjectSchema(
    requireOwnerArraySchema(reviewProperties.cited_basis, 0, 'RH Mission Admission cited basis schema'),
    ['identity', 'kind', 'payload_sha256', 'revision'],
    ['kind', 'identity', 'revision', 'payload_sha256'],
    'RH Mission Admission cited basis reference schema',
  )
  requireEnumSchema(citedBasis.kind, ['evidence', 'context', 'branch', 'candidate'], 'RH Mission Admission cited basis kind schema')
  requireTextSchema(citedBasis.identity, 'RH Mission Admission cited basis identity schema')
  if (
    !isRecord(citedBasis.revision) || citedBasis.revision.type !== 'integer' || citedBasis.revision.minimum !== 1 ||
    !isRecord(citedBasis.payload_sha256) || citedBasis.payload_sha256.pattern !== '^[0-9a-f]{64}(?![\\s\\S])'
  ) {
    throw new Error('RH Mission Admission cited basis reference schema differs from the pinned owner contract')
  }
  requireTextArraySchema(reviewProperties.limitations, 'RH Mission Admission Review limitations schema')
  requireTextArraySchema(reviewProperties.non_inferences, 'RH Mission Admission Review non-inferences schema')

  const authorizeProperties = requireClosedObjectSchema(
    branches[3],
    ['decision_basis', 'disposition', 'limitations', 'mode', 'non_inferences'],
    ['mode', 'disposition', 'decision_basis'],
    'RH Mission Admission authorize submit request',
  )
  if (
    !isRecord(authorizeProperties.mode) || authorizeProperties.mode.const !== 'submit' ||
    !isRecord(authorizeProperties.disposition) ||
    authorizeProperties.disposition.const !== 'authorize_exact_delta'
  ) {
    throw new Error('RH Mission Admission authorize submit request differs from the pinned owner contract')
  }
  requireTextSchema(authorizeProperties.decision_basis, 'RH Mission Admission authorize basis schema')
  requireTextArraySchema(authorizeProperties.limitations, 'RH Mission Admission authorize limitations schema')
  requireTextArraySchema(authorizeProperties.non_inferences, 'RH Mission Admission authorize non-inferences schema')

  const rejectProperties = requireClosedObjectSchema(
    branches[4],
    ['cited_basis', 'decision_basis', 'disposition', 'limitations', 'mode', 'non_inferences', 'objections'],
    ['mode', 'disposition', 'decision_basis', 'objections', 'cited_basis'],
    'RH Mission Admission reject submit request',
  )
  if (
    !isRecord(rejectProperties.mode) || rejectProperties.mode.const !== 'submit' ||
    !isRecord(rejectProperties.disposition) || rejectProperties.disposition.const !== 'reject'
  ) {
    throw new Error('RH Mission Admission reject submit request differs from the pinned owner contract')
  }
  requireTextSchema(rejectProperties.decision_basis, 'RH Mission Admission reject basis schema')
  const rejectObjectionProperties = requireClosedObjectSchema(
    requireOwnerArraySchema(rejectProperties.objections, 1, 'RH Mission Admission reject objections schema'),
    ['affected_scope', 'exact_objection', 'materiality_basis'],
    ['exact_objection', 'affected_scope', 'materiality_basis'],
    'RH Mission Admission reject objection schema',
  )
  for (const key of ['affected_scope', 'exact_objection', 'materiality_basis'] as const) {
    requireTextSchema(rejectObjectionProperties[key], `RH Mission Admission reject objection ${key} schema`)
  }
  const rejectCitedBasis = requireClosedObjectSchema(
    requireOwnerArraySchema(rejectProperties.cited_basis, 1, 'RH Mission Admission reject cited basis schema'),
    ['identity', 'kind', 'payload_sha256', 'revision'],
    ['kind', 'identity', 'revision', 'payload_sha256'],
    'RH Mission Admission reject cited basis reference schema',
  )
  requireEnumSchema(rejectCitedBasis.kind, ['evidence', 'context', 'branch', 'candidate'], 'RH Mission Admission reject cited basis kind schema')
  requireTextSchema(rejectCitedBasis.identity, 'RH Mission Admission reject cited basis identity schema')
  if (
    !isRecord(rejectCitedBasis.revision) || rejectCitedBasis.revision.type !== 'integer' || rejectCitedBasis.revision.minimum !== 1 ||
    !isRecord(rejectCitedBasis.payload_sha256) || rejectCitedBasis.payload_sha256.pattern !== '^[0-9a-f]{64}(?![\\s\\S])'
  ) {
    throw new Error('RH Mission Admission reject cited basis reference schema differs from the pinned owner contract')
  }
  requireTextArraySchema(rejectProperties.limitations, 'RH Mission Admission reject limitations schema')
  requireTextArraySchema(rejectProperties.non_inferences, 'RH Mission Admission reject non-inferences schema')
  return value
}

function loadMissionModelProjection(projectionPath: string): Readonly<{
  root: MissionModelProjection
  closeoutRoot: MissionModelProjection
  historicalRead: MissionModelProjection
  historicalReadGrantInputSchema: Record<string, unknown>
  researchReadGrantInputSchema: Record<string, unknown>
  researchReadRequestSchema: Record<string, unknown>
  candidateA1ReviewGrantInputSchema: Record<string, unknown>
  candidateA1ReviewRequestSchema: Record<string, unknown>
  admissionCaseInputSchema: Record<string, unknown>
  admissionGrantInputSchema: Record<string, unknown>
  admissionRequestSchema: Record<string, unknown>
}> {
  requireRootOwnedImmutablePath(projectionPath, 'file', 'RH Mission model projection')
  const value = readJsonFile(projectionPath, 'RH Mission model projection')
  if (!isRecord(value)) {
    throw new Error('RH Mission model projection must be an object')
  }
  requireExactKeys(
    value,
    [
      'admission_tool',
      'candidate_a1_review_tool',
      'closeout_root_tool',
      'historical_read_tool',
      'research_read_tool',
      'root_tool',
      'schema_version',
    ],
    'RH Mission Host model projection',
  )
  const rootTool = value.root_tool
  const closeoutRootTool = value.closeout_root_tool
  const historicalReadTool = value.historical_read_tool
  const researchReadTool = value.research_read_tool
  const candidateA1ReviewTool = value.candidate_a1_review_tool
  const admissionTool = value.admission_tool
  if (
    value.schema_version !== HOST_MODEL_PROJECTION_SCHEMA_VERSION ||
    !isRecord(rootTool) ||
    !isRecord(closeoutRootTool) ||
    !isRecord(historicalReadTool) ||
    !isRecord(researchReadTool) ||
    !isRecord(candidateA1ReviewTool) ||
    !isRecord(admissionTool)
  ) {
    throw new Error('RH Mission Host model projection differs from the pinned owner contract')
  }
  requireExactKeys(rootTool, ['model_projection', 'name'], 'RH Mission root-tool projection')
  requireExactKeys(
    closeoutRootTool,
    ['model_projection', 'name'],
    'RH Mission closeout-root-tool projection',
  )
  requireExactKeys(
    historicalReadTool,
    ['grant_input_schema', 'model_projection', 'name'],
    'RH Mission historical-read-tool projection',
  )
  requireExactKeys(
    candidateA1ReviewTool,
    ['grant_input_schema', 'grant_tool_name', 'name', 'page_tool_name', 'request_schema'],
    'RH Mission Candidate A1 review-tool projection',
  )
  requireExactKeys(
    researchReadTool,
    ['grant_input_schema', 'grant_tool_name', 'name', 'page_tool_name', 'request_schema'],
    'RH Mission research-read-tool projection',
  )
  requireExactKeys(
    admissionTool,
    [
      'case_input_schema',
      'grant_input_schema',
      'grant_tool_name',
      'name',
      'open_tool_name',
      'page_tool_name',
      'request_schema',
    ],
    'RH Mission Admission-tool projection',
  )
  if (
    rootTool.name !== 'rh_mission' ||
    closeoutRootTool.name !== 'rh_mission' ||
    historicalReadTool.name !== 'rh_mission_history' ||
    researchReadTool.name !== 'rh_mission_research_read' ||
    researchReadTool.grant_tool_name !== 'rh_mission_research_read_grant' ||
    researchReadTool.page_tool_name !== 'rh_mission_research_read_page' ||
    candidateA1ReviewTool.name !== 'rh_mission_a1_review' ||
    candidateA1ReviewTool.grant_tool_name !== 'rh_mission_a1_review_grant' ||
    candidateA1ReviewTool.page_tool_name !== 'rh_mission_a1_review_page' ||
    admissionTool.name !== 'rh_mission_admission' ||
    admissionTool.open_tool_name !== 'rh_mission_admission_open' ||
    admissionTool.grant_tool_name !== 'rh_mission_admission_grant' ||
    admissionTool.page_tool_name !== 'rh_mission_admission_page'
  ) {
    throw new Error('RH Mission Host tool names differ from the pinned owner contract')
  }
  return {
    root: parseMissionModelProjection(
      rootTool.model_projection,
      REQUIRED_OPERATIONS,
      'rh_mission',
      'RH Mission root model projection',
      'continue',
    ),
    closeoutRoot: parseMissionModelProjection(
      closeoutRootTool.model_projection,
      CLOSEOUT_OPERATIONS,
      'rh_mission',
      'RH Mission closeout root model projection',
      'closeout',
    ),
    historicalRead: parseMissionModelProjection(
      historicalReadTool.model_projection,
      HISTORICAL_READ_OPERATIONS,
      'rh_mission_history',
      'RH Mission historical-read model projection',
      null,
    ),
    historicalReadGrantInputSchema: validateHistoricalReadGrantInputSchema(
      historicalReadTool.grant_input_schema,
    ),
    researchReadGrantInputSchema: validateResearchReadGrantInputSchema(researchReadTool.grant_input_schema),
    researchReadRequestSchema: validateResearchReadRequestSchema(researchReadTool.request_schema),
    candidateA1ReviewGrantInputSchema: validateCandidateA1ReviewGrantInputSchema(
      candidateA1ReviewTool.grant_input_schema,
    ),
    candidateA1ReviewRequestSchema: validateCandidateA1ReviewRequestSchema(
      candidateA1ReviewTool.request_schema,
    ),
    admissionCaseInputSchema: validateAdmissionCaseInputSchema(
      admissionTool.case_input_schema,
    ),
    admissionGrantInputSchema: validateAdmissionGrantInputSchema(
      admissionTool.grant_input_schema,
    ),
    admissionRequestSchema: validateAdmissionRequestSchema(
      admissionTool.request_schema,
    ),
  }
}

export interface PinnedLaunchContextPolicy {
  catalogPath: string
  catalogSha256: string
  contextWindowTokens: number
  effectiveContextWindowPercent: number
  effectiveContextAllowanceTokens: number
  baseInstructions: string
  baseInstructionsSource: 'model_messages.instructions_template_equals_base_instructions'
  configuredCompactionThresholdTokens: number | null
}

export function loadPinnedCodexModelCatalog(catalogPath: string): PinnedLaunchContextPolicy {
  requireRootOwnedImmutablePath(catalogPath, 'file', 'pinned Codex model catalog')
  const value = readJsonFile(catalogPath, 'pinned Codex model catalog')
  if (!isRecord(value)) {
    throw new Error('pinned Codex model catalog must be one object')
  }
  requireExactKeys(value, ['models'], 'pinned Codex model catalog')
  const models = value.models
  if (!Array.isArray(models) || models.length !== 1 || !isRecord(models[0])) {
    throw new Error('pinned Codex model catalog must contain exactly one complete model record')
  }
  const minimumClientVersion = models[0].minimal_client_version
  const minimumVersionParts = typeof minimumClientVersion === 'string'
    ? /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(minimumClientVersion)?.slice(1).map(Number)
    : undefined
  if (!minimumVersionParts || !minimumVersionParts.every(Number.isSafeInteger)) {
    throw new Error('pinned Codex model catalog must declare a valid minimal_client_version')
  }
  const pinnedVersionParts = PINNED_CODEX_CLI_VERSION.split('.').map(Number)
  const differingPart = minimumVersionParts.findIndex((part, index) => part !== pinnedVersionParts[index])
  if (differingPart !== -1 && minimumVersionParts[differingPart]! > pinnedVersionParts[differingPart]!) {
    throw new Error('pinned Codex version is below the model catalog minimal_client_version')
  }
  const supported = models[0].supported_reasoning_levels
  if (
    models[0].slug !== PINNED_MODEL ||
    models[0].default_reasoning_level !== PINNED_REASONING_EFFORT ||
    models[0].multi_agent_version !== 'v1' ||
    !Array.isArray(supported) ||
    supported.length !== 1 ||
    !isRecord(supported[0]) ||
    supported[0].effort !== PINNED_REASONING_EFFORT ||
    typeof supported[0].description !== 'string'
  ) {
    throw new Error('pinned Codex model catalog differs from the exact Astra/Ultra policy')
  }
  const model = models[0]
  const messages = model.model_messages
  if (
    !Number.isSafeInteger(model.context_window) || (model.context_window as number) <= 0 ||
    !Number.isSafeInteger(model.effective_context_window_percent) ||
    (model.effective_context_window_percent as number) <= 0 ||
    (model.effective_context_window_percent as number) > 100 ||
    typeof model.base_instructions !== 'string' || model.base_instructions.length === 0 ||
    !isRecord(messages) || messages.instructions_template !== model.base_instructions ||
    !isRecord(messages.instructions_variables) ||
    Object.values(messages.instructions_variables).some((value) => value !== '') ||
    (model.auto_compact_token_limit !== undefined &&
      (!Number.isSafeInteger(model.auto_compact_token_limit) || (model.auto_compact_token_limit as number) <= 0))
  ) {
    throw new Error('pinned Codex launch context accounting is not established')
  }
  return {
    catalogPath,
    catalogSha256: createHash('sha256').update(fs.readFileSync(catalogPath)).digest('hex'),
    contextWindowTokens: model.context_window as number,
    effectiveContextWindowPercent: model.effective_context_window_percent as number,
    effectiveContextAllowanceTokens: Math.floor(
      (model.context_window as number) * (model.effective_context_window_percent as number) / 100,
    ),
    baseInstructions: model.base_instructions,
    baseInstructionsSource: 'model_messages.instructions_template_equals_base_instructions',
    configuredCompactionThresholdTokens: model.auto_compact_token_limit as number | undefined ?? null,
  }
}

function deterministicIdentity(missionId: string, releaseSha: string): string {
  return createHash('sha256').update(`${missionId}\0${releaseSha}`, 'utf8').digest('hex')
}

export function readMissionHostConfig(
  missionIdInput: string,
  overrides: MissionHostPathOverrides = {},
): MissionHostConfig {
  const missionId = missionIdInput.trim()
  if (!MISSION_ID.test(missionId) || missionId === '.' || missionId === '..') {
    throw new Error('mission_id contains unsupported characters')
  }
  const repoRoot = requireAbsolutePath(overrides.repoRoot ?? DEFAULT_REPO_ROOT, 'RH Mission repo root')
  const missionWorkspaceRoot = requireAbsolutePath(
    overrides.missionWorkspaceRoot ?? path.join(DEFAULT_WORKSPACE_ROOT, missionId),
    'RH Mission workspace root',
  )
  const goalsRoot = requireAbsolutePath(overrides.goalsRoot ?? DEFAULT_GOALS_ROOT, 'RH Goal workspaces root')
  const runtimeDir = requireAbsolutePath(
    overrides.runtimeDir ?? path.join(DEFAULT_RUNTIME_ROOT, missionId),
    'RH Mission runtime directory',
  )
  const codexHome = requireAbsolutePath(
    overrides.codexHome ?? DEFAULT_CODEX_HOME,
    'RH Mission Codex home',
  )
  const lockPath = requireAbsolutePath(
    overrides.lockPath ?? DEFAULT_RUNTIME_LOCK_PATH,
    'RH Mission global runtime lock',
  )
  const pythonPath = requireAbsolutePath(overrides.pythonPath ?? DEFAULT_PYTHON_PATH, 'Python executable')
  const codexCliPath = requireAbsolutePath(overrides.codexCliPath ?? DEFAULT_CODEX_CLI_PATH, 'Codex executable')
  const releaseSha = overrides.releaseSha ?? path.basename(repoRoot)
  if (!RELEASE_SHA.test(releaseSha)) {
    throw new Error('RH Mission release identity must be one exact commit')
  }
  const missionScriptPath = path.join(repoRoot, 'scripts', 'rh_mission.py')
  const serviceEntrypointPath = path.join(repoRoot, ...SERVICE_ENTRYPOINT_RELATIVE_PATH.split('/'))
  const missionSeedPath = path.join(repoRoot, ...MISSION_SEED_RELATIVE_PATH.split('/'))
  const missionModelProjectionPath = path.join(repoRoot, MODEL_PROJECTION_NAME)
  const modelCatalogPath = path.join(repoRoot, ...PINNED_MODEL_CATALOG_RELATIVE_PATH.split('/'))

  requireRootOwnedImmutablePath(repoRoot, 'directory', 'RH Mission release root')
  if (process.platform !== 'win32' && fs.realpathSync.native(repoRoot) !== repoRoot) {
    throw new Error('RH Mission release root must be its exact non-symlinked release path')
  }
  requireExisting(missionWorkspaceRoot, 'directory', 'RH Mission workspace root')
  requireExisting(codexHome, 'directory', 'RH Mission Codex home')
  requireExisting(pythonPath, 'file', 'Python executable')
  requireExisting(codexCliPath, 'file', 'Codex executable')
  requireRootOwnedImmutablePath(missionScriptPath, 'file', 'RH Mission bridge')
  requireRootOwnedImmutablePath(serviceEntrypointPath, 'file', 'RH Mission Host entrypoint')
  requireRootOwnedImmutablePath(missionSeedPath, 'file', 'RH autonomous Mission genesis seed')
  requireRootOwnedImmutablePath(missionModelProjectionPath, 'file', 'RH Mission model projection')
  loadPinnedCodexModelCatalog(modelCatalogPath)
  const modelProjections = loadMissionModelProjection(missionModelProjectionPath)
  if (
    pathsOverlap(goalsRoot, missionWorkspaceRoot) ||
    pathsOverlap(goalsRoot, repoRoot) ||
    pathsOverlap(goalsRoot, runtimeDir) ||
    pathsOverlap(goalsRoot, codexHome) ||
    pathsOverlap(missionWorkspaceRoot, repoRoot) ||
    pathsOverlap(missionWorkspaceRoot, runtimeDir) ||
    pathsOverlap(missionWorkspaceRoot, codexHome) ||
    pathsOverlap(runtimeDir, repoRoot) ||
    pathsOverlap(runtimeDir, codexHome) ||
    pathsOverlap(repoRoot, codexHome)
  ) {
    throw new Error('RH Mission release, owner workspace, Goal workspaces, runtime, and Codex home paths must not overlap')
  }

  const identityDigest = deterministicIdentity(missionId, releaseSha)
  return {
    repoRoot,
    releaseSha,
    serviceEntrypointPath,
    missionWorkspaceRoot,
    goalsRoot,
    runtimeDir,
    codexHome,
    statePath: path.join(runtimeDir, 'mission-host-state.json'),
    lockPath,
    pythonPath,
    codexCliPath,
    modelCatalogPath,
    missionScriptPath,
    missionModelProjectionPath,
    missionModelProjection: modelProjections.root,
    closeoutMissionModelProjection: modelProjections.closeoutRoot,
    historicalReadModelProjection: modelProjections.historicalRead,
    historicalReadGrantInputSchema: modelProjections.historicalReadGrantInputSchema,
    researchReadGrantInputSchema: modelProjections.researchReadGrantInputSchema,
    researchReadRequestSchema: modelProjections.researchReadRequestSchema,
    candidateA1ReviewGrantInputSchema: modelProjections.candidateA1ReviewGrantInputSchema,
    candidateA1ReviewRequestSchema: modelProjections.candidateA1ReviewRequestSchema,
    admissionCaseInputSchema: modelProjections.admissionCaseInputSchema,
    admissionGrantInputSchema: modelProjections.admissionGrantInputSchema,
    admissionRequestSchema: modelProjections.admissionRequestSchema,
    projectId: RH_PROJECT_ID,
    missionId,
    pollIntervalMs: RH_MISSION_POLL_INTERVAL_MS,
    permissionProfileId: `rh-mission-linux-${releaseSha}-${identityDigest.slice(0, 16)}`,
    trustedMcpServerIds: RH_MISSION_TRUSTED_MCP_SERVER_IDS,
    trustedAppIds: RH_MISSION_TRUSTED_APP_IDS,
    expectedCliVersion: PINNED_CODEX_CLI_VERSION,
    expectedModelProvider: PINNED_MODEL_PROVIDER,
    expectedModel: PINNED_MODEL,
    reasoningEffort: PINNED_REASONING_EFFORT,
  }
}

function requiredEnvironmentValue(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key]
  if (value === undefined) {
    throw new Error(`Missing required RH Mission Host launch binding: ${key}`)
  }
  return requiredText(value, key)
}

function assertSamePath(actual: string, expected: string, label: string): void {
  if (requireAbsolutePath(actual, label) !== path.resolve(expected)) {
    throw new Error(`${label} differs from the path derived and verified by the Mission Host`)
  }
}

/** Consume explicit installation paths; all semantic values come from the owner contract. */
export function readMissionHostConfigFromEnvironment(
  env: NodeJS.ProcessEnv = process.env,
  launchPathOverrides: Pick<
    MissionHostPathOverrides,
    'pythonPath' | 'codexCliPath' | 'codexHome'
  > = {},
): MissionHostConfig {
  const missionId = requiredEnvironmentValue(env, 'RH_MISSION_HOST_MISSION_ID')
  const suppliedPythonPath = requiredEnvironmentValue(env, 'RH_MISSION_HOST_PYTHON_PATH')
  const suppliedCodexCliPath = requiredEnvironmentValue(env, 'RH_MISSION_HOST_CODEX_CLI_PATH')
  const suppliedCodexHome = requiredEnvironmentValue(env, 'CODEX_HOME')
  if (launchPathOverrides.pythonPath !== undefined) {
    assertSamePath(suppliedPythonPath, launchPathOverrides.pythonPath, 'RH_MISSION_HOST_PYTHON_PATH')
  }
  if (launchPathOverrides.codexCliPath !== undefined) {
    assertSamePath(suppliedCodexCliPath, launchPathOverrides.codexCliPath, 'RH_MISSION_HOST_CODEX_CLI_PATH')
  }
  if (launchPathOverrides.codexHome !== undefined) {
    assertSamePath(suppliedCodexHome, launchPathOverrides.codexHome, 'CODEX_HOME')
  }
  const config = readMissionHostConfig(missionId, {
    repoRoot: requiredEnvironmentValue(env, 'RH_MISSION_HOST_REPO_ROOT'),
    releaseSha: requiredEnvironmentValue(env, 'RH_MISSION_HOST_RELEASE_COMMIT'),
    missionWorkspaceRoot: requiredEnvironmentValue(env, 'RH_MISSION_HOST_WORKSPACE_ROOT'),
    goalsRoot: requiredEnvironmentValue(env, 'RH_MISSION_HOST_GOALS_ROOT'),
    runtimeDir: requiredEnvironmentValue(env, 'RH_MISSION_HOST_RUNTIME_DIR'),
    lockPath: requiredEnvironmentValue(env, 'RH_MISSION_RUNTIME_LOCK'),
    codexHome: suppliedCodexHome,
    pythonPath: suppliedPythonPath,
    codexCliPath: suppliedCodexCliPath,
  })
  return config
}

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM'
  }
}

export async function acquireRuntimeLock(lockPath: string, releaseSha: string): Promise<RuntimeLock> {
  await fs.promises.mkdir(path.dirname(lockPath), { recursive: true, mode: 0o700 })
  const token = randomUUID()
  const payload = `${JSON.stringify({ pid: process.pid, token, releaseSha, startedAt: new Date().toISOString() })}\n`

  const create = async (): Promise<void> => {
    const handle = await fs.promises.open(lockPath, 'wx', 0o600)
    try {
      await handle.writeFile(payload, 'utf8')
      await handle.sync()
    } finally {
      await handle.close()
    }
  }

  try {
    await create()
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {
      throw error
    }
    let existing: unknown
    try {
      existing = JSON.parse(await fs.promises.readFile(lockPath, 'utf8'))
    } catch (readError) {
      throw new Error('RH Mission runtime lock exists but is unreadable', { cause: readError })
    }
    const pid = isRecord(existing) ? existing.pid : null
    if (!Number.isSafeInteger(pid) || (pid as number) <= 0 || pidAlive(pid as number)) {
      throw new Error('another RH Mission Host process owns the runtime lock')
    }
    const stalePath = `${lockPath}.stale.${pid}.${Date.now()}`
    await fs.promises.rename(lockPath, stalePath)
    await create()
  }

  let released = false
  return {
    path: lockPath,
    async release(): Promise<void> {
      if (released) {
        return
      }
      const current = JSON.parse(await fs.promises.readFile(lockPath, 'utf8')) as unknown
      if (!isRecord(current) || current.token !== token || current.pid !== process.pid) {
        throw new Error('RH Mission runtime lock ownership changed before release')
      }
      await fs.promises.unlink(lockPath)
      released = true
    },
  }
}
