import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

// Install at services/rh-mission-host/scripts/generate-agent-roles.mjs.
// Fixed inputs belong to the admitted source tree, never the active Mission,
// the invoking cwd, repository AGENTS.md, or an installed Codex home.
const repositoryRoot = new URL('../../../', import.meta.url)
const outputRoot = new URL('../dist/agent-roles/', import.meta.url)
const instructionRoot = 'docs/instructions/'
const commonSource = `${instructionRoot}AGENTS.md`

// Keep these five paths in the built-product generated asset hash inventory.
// default, worker and explorer share fallback.toml.
// Role names, descriptions, capabilities and caller grants are bound by Host/Core.
const roles = [
  ['rh_researcher', [commonSource, `${instructionRoot}Researcher.md`]],
  ['rh_helper', [commonSource, `${instructionRoot}Helper.md`]],
  ['rh_historical', [
    commonSource,
    `${instructionRoot}Researcher.md`,
    `${instructionRoot}Grounding_Discovery_And_Capabilities.md`,
  ]],
  ['rh_restricted_review', [commonSource, `${instructionRoot}Restricted_Review.md`]],
  ['fallback', [commonSource]],
]

if (process.argv.length !== 2) {
  throw new Error('generate-agent-roles takes no arguments; its source and output paths are fixed')
}

const decoder = new TextDecoder('utf-8', { fatal: true })
const sourcePaths = [...new Set(roles.flatMap(([, paths]) => paths))]
const sources = new Map(await Promise.all(sourcePaths.map(async (relativePath) => {
  const bytes = await readFile(new URL(relativePath, repositoryRoot))
  const content = decoder.decode(bytes).replace(/\r\n?/g, '\n')
  if (content.trim().length === 0) {
    throw new Error(`Canonical RH instruction source is empty: ${relativePath}`)
  }
  return [relativePath, content]
})))

const sourceLocationNote = [
  'The complete canonical instruction texts below identify their source locations',
  'relative to the immutable release root supplied by the Host. Resolve each',
  "text's relative Markdown links from that source file, not the scratch working",
  'directory or this generated carrier.',
].join(' ')

// Assemble every complete body before writing any output. Do not select sections,
// summarize roles, inline linked guides, or introduce current scientific content.
const outputs = roles.map(([name, paths]) => {
  const completeMarkdown = [sourceLocationNote, ...paths.map((relativePath) => (
    `Canonical instruction source: \`${relativePath}\`\n\n${sources.get(relativePath)}`
  ))].join('\n\n')

  // Core validates this exact closed carrier before the pinned native loader.
  // No other TOML assignments, comments, model selection or grants belong here.
  return [new URL(`${name}.toml`, outputRoot),
    `developer_instructions = ${JSON.stringify(completeMarkdown)}\n`]
})

await mkdir(outputRoot, { recursive: true })
for (const [destination, content] of outputs) {
  await writeFile(destination, content, { encoding: 'utf8', mode: 0o644 })
}

process.stdout.write(`Generated ${outputs.length} RH role carriers in ${fileURLToPath(outputRoot)}\n`)
