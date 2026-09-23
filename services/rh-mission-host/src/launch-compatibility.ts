import { createHash } from 'node:crypto'

import { resolveGoalEpochInstructions, type BoundedGoalEpochRequest } from '../../../packages/codex-thread-core/dist/index.js'
import { loadPinnedCodexModelCatalog, type MissionHostConfig } from './config.js'

function measure(text: string) {
  return {
    utf8_bytes: Buffer.byteLength(text, 'utf8'),
    characters: Array.from(text).length,
    sha256: createHash('sha256').update(text, 'utf8').digest('hex'),
  }
}

/** One deterministic, local, pre-effect accounting path for launch and inspection. */
export function measureMissionLaunchCompatibility(
  config: MissionHostConfig,
  request: BoundedGoalEpochRequest,
  launchMode: string,
) {
  const policy = loadPinnedCodexModelCatalog(config.modelCatalogPath)
  const requestJson = JSON.stringify(request)
  const instructions = resolveGoalEpochInstructions(request, [config.repoRoot])
  const resolvedRequestJson = JSON.stringify({ ...request, developerInstructions: instructions.developerInstructions })
  const components = {
    objective: measure(request.objective),
    developer_instructions: measure(request.developerInstructions),
    initial_context_data: measure(request.initialContextText ?? ''),
    dynamic_tools: measure(JSON.stringify(request.dynamicTools)),
    environments: measure(JSON.stringify(request.environments)),
    selected_capabilities: measure(JSON.stringify(request.selectedCapabilities)),
    request_json: measure(requestJson),
    resolved_developer_instructions: measure(instructions.developerInstructions),
    resolved_request_json: measure(resolvedRequestJson),
    catalog_instruction_template: measure(policy.catalogInstructionTemplate),
  }
  // Byte-level BPE represents each accounted UTF-8 byte with at most one token.
  // Count the whole request JSON (including its quoting/keys) conservatively,
  // plus the catalog template once. Native-selected instruction blocks and
  // variable rendering remain unmeasured; component metrics are not summed.
  const upperBound = components.resolved_request_json.utf8_bytes + components.catalog_instruction_template.utf8_bytes
  const established = upperBound <= policy.effectiveContextAllowanceTokens
  return {
    schema_version: 'workstation_control.rh_mission_launch_compatibility.v2',
    status: established ? 'compatible' as const : 'inconclusive' as const,
    scope: 'accounted_host_controlled_launch_payload' as const,
    model: config.expectedModel,
    catalog: { path: policy.catalogPath, sha256: policy.catalogSha256 },
    context_allowance: {
      model_context_window_tokens: policy.contextWindowTokens,
      effective_context_window_percent: policy.effectiveContextWindowPercent,
      tokens: policy.effectiveContextAllowanceTokens,
      source: 'pinned_model_catalog' as const,
    },
    compaction_threshold: {
      configured_tokens: policy.configuredCompactionThresholdTokens,
      source: policy.configuredCompactionThresholdTokens === null
        ? 'not_configured_in_pinned_catalog' as const : 'pinned_model_catalog' as const,
      runtime_default_established: false,
      effective_tokens: policy.configuredCompactionThresholdTokens,
    },
    measurement: {
      method: 'utf8_byte_level_bpe_conservative_upper_bound' as const,
      accounted_token_upper_bound: upperBound,
      tokenizer_estimate: null,
      tokenizer_estimate_is_certification: false,
      catalog_instruction_template_source: policy.catalogInstructionTemplateSource,
      catalog_instruction_template_count: 1,
      components,
    },
    unmeasured: [
      'provider_message_framing', 'runtime_builtin_tools', 'output_and_reasoning_reservation',
      'runtime_selected_provider_instruction_blocks', 'runtime_instruction_template_variable_rendering',
      ...(launchMode === 'resume_suspended_goal' ? ['retained_native_thread_history'] : []),
    ],
    provider_wire_equivalence_claimed: false,
    history_erasure_claimed: false,
    // An upper bound above the allowance does not establish actual overflow.
    // The ordinary native request boundary still enforces supported model limits.
    admission_effect: 'informational_only' as const,
  }
}
