import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { importModelCatalog } from './import-model-catalog.mjs'

const fixture = JSON.parse(readFileSync(new URL('../test-fixtures/codex-model-catalog.0.153.4.json', import.meta.url), 'utf8'))

test('catalog import preserves caller metadata while selecting the explicit native policy', () => {
  const cache = structuredClone(fixture)
  cache.models[0].supported_reasoning_levels.push({ effort: 'low', description: 'Synthetic alternate effort' })
  cache.models[0].default_reasoning_level = 'low'
  cache.models[0].multi_agent_version = 'v2'
  cache.models.push({ slug: 'unselected-synthetic-model', base_instructions: 'Not selected.' })
  const before = structuredClone(cache)
  const encoded = importModelCatalog(cache)
  const selected = JSON.parse(encoded).models
  assert.equal(selected.length, 1)
  assert.equal(selected[0].base_instructions, cache.models[0].base_instructions)
  assert.equal(selected[0].model_messages.instructions_template, cache.models[0].base_instructions)
  assert.equal(selected[0].default_reasoning_level, 'ultra')
  assert.equal(selected[0].multi_agent_version, 'v1')
  assert.deepEqual(selected[0].supported_reasoning_levels.map(({ effort }) => effort), ['ultra'])
  assert.match(encoded, /"limit": 9223372036854775807/)
  assert.deepEqual(cache, before)
})

test('catalog import refuses unsupported model access or runtime metadata rather than inventing a fallback', () => {
  for (const mutate of [
    (cache) => { cache.models = [] },
    (cache) => { cache.models.push(structuredClone(cache.models[0])) },
    (cache) => { cache.models[0].supported_reasoning_levels = [{ effort: 'high' }] },
    (cache) => { cache.models[0].minimal_client_version = '0.154.0' },
    (cache) => { cache.models[0].base_instructions = '' },
  ]) {
    const cache = structuredClone(fixture)
    mutate(cache)
    assert.throws(() => importModelCatalog(cache))
  }
})
