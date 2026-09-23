import { createHash } from 'node:crypto'
import { constants, lstatSync, mkdirSync, openSync, readFileSync, closeSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const MODEL = 'gpt-6-astra'
const CLI_VERSION = '0.153.4'
const EFFORT = 'ultra'
const NATIVE_RESULT_CAPACITY = '9223372036854775807'

/** Select provider metadata; the source cache never becomes a public repository asset. */
export function importModelCatalog(cache) {
  if (!cache || !Array.isArray(cache.models)) throw new Error('Expected a Codex model cache with a models array')
  const matches = cache.models.filter((entry) => entry?.slug === MODEL)
  if (matches.length !== 1) throw new Error(`The supplied cache must contain exactly one ${MODEL} entry`)
  const model = structuredClone(matches[0])
  const levels = model.supported_reasoning_levels
  const ultra = Array.isArray(levels) ? levels.find((level) => level?.effort === EFFORT) : undefined
  if (!ultra || typeof ultra.description !== 'string') throw new Error('The supplied model does not advertise ultra reasoning')
  // The provider can leave optional limits unspecified. Keep that distinction;
  // the exact native CLI version remains independently pinned and handshaken.
  if (model.minimal_client_version !== undefined && model.minimal_client_version !== null) {
    const version = typeof model.minimal_client_version === 'string'
      ? /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(model.minimal_client_version)?.slice(1).map(Number)
      : undefined
    const pinned = CLI_VERSION.split('.').map(Number)
    const differing = version?.findIndex((part, index) => part !== pinned[index])
    if (!version || !version.every(Number.isSafeInteger) || (differing !== -1 && version[differing] > pinned[differing])) {
      throw new Error(`The supplied model metadata is incompatible with Codex ${CLI_VERSION}`)
    }
  }
  const messages = model.model_messages
  if (!messages || typeof messages !== 'object' || Array.isArray(messages) ||
      typeof messages.instructions_template !== 'string' || !messages.instructions_template.trim() ||
      (messages.instructions_variables !== undefined && messages.instructions_variables !== null &&
        (typeof messages.instructions_variables !== 'object' || Array.isArray(messages.instructions_variables) ||
          Object.values(messages.instructions_variables).some((value) => value !== null && typeof value !== 'string'))) ||
      !Number.isSafeInteger(model.context_window) || model.context_window <= 0 ||
      !Number.isSafeInteger(model.effective_context_window_percent) ||
      model.effective_context_window_percent <= 0 || model.effective_context_window_percent > 100 ||
      (model.auto_compact_token_limit !== undefined && model.auto_compact_token_limit !== null &&
        (!Number.isSafeInteger(model.auto_compact_token_limit) || model.auto_compact_token_limit <= 0))) {
    throw new Error('The model cache is missing valid provider instructions or context accounting metadata')
  }
  // These are the declared research runtime policy, not altered mathematical claims.
  model.default_reasoning_level = EFFORT
  model.supported_reasoning_levels = [ultra]
  model.multi_agent_version = 'v1'
  model.tool_mode = 'code_mode_only'
  model.experimental_supported_tools = []
  // The pinned native runtime consumes model_messages, including provider policy
  // blocks beyond the template. Preserve the complete caller record; the legacy
  // base_instructions field is neither required nor used to replace those blocks.
  model.truncation_policy = { mode: 'tokens', limit: NATIVE_RESULT_CAPACITY }
  // JSON.parse/JSON.stringify round this signed-64-bit compatibility value in JS.
  // Emit the exact numeric literal consumed by the pinned native runtime.
  return JSON.stringify({ models: [model] }, null, 2)
    .replace(`"limit": "${NATIVE_RESULT_CAPACITY}"`, `"limit": ${NATIVE_RESULT_CAPACITY}`) + '\n'
}

function main(args) {
  if (args.length !== 4 || args[0] !== '--cache' || args[2] !== '--output') {
    throw new Error('Usage: import-model-catalog.mjs --cache <absolute models_cache.json> --output <absolute catalog.json>')
  }
  const source = args[1]
  const destination = args[3]
  if (!path.isAbsolute(source) || !path.isAbsolute(destination) || path.resolve(source) === path.resolve(destination)) {
    throw new Error('Cache and output must be distinct absolute file paths')
  }
  const metadata = lstatSync(source)
  if (!metadata.isFile() || metadata.isSymbolicLink()) throw new Error('Model cache must be a regular non-symlink file')
  const body = importModelCatalog(JSON.parse(readFileSync(source, 'utf8')))
  mkdirSync(path.dirname(destination), { recursive: true, mode: 0o755 })
  const descriptor = openSync(destination, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0), 0o644)
  try { writeFileSync(descriptor, body) } finally { closeSync(descriptor) }
  // No provider prompt content, account information or cache body is logged.
  process.stdout.write(JSON.stringify({ status: 'created', model: MODEL, codex_version: CLI_VERSION,
    sha256: createHash('sha256').update(body).digest('hex') }) + '\n')
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main(process.argv.slice(2))
