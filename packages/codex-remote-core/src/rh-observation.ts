/** Browser-safe projections of contracts/rh_observation.v1.schema.json.
 * The JSON schema is the wire authority; the Node client validates that schema.
 * None of these observations grant research/owner authority.
 */
export const RH_OBSERVATION_SCHEMA_VERSION = 'rh_observation.v1' as const

/** Reviewed display codes from the owning Python controller. Never display its raw stderr/message. */
export const RH_CONTROLLER_CODE_MESSAGES = {
  invalid_argument: 'The controller rejected the requested identity or selector.',
  invalid_observation_selector: 'The observation selector does not match the controller contract.',
  invalid_remote_result: 'The remote observation result failed its identity or data contract.',
  remote_command_failed: 'The remote controller failed without a usable structured result.',
  connection_config_missing: 'The dedicated RH controller connection is not configured.',
  invalid_ssh_host: 'The configured RH controller host is invalid.',
  invalid_ssh_user: 'The configured RH controller account is invalid.',
  invalid_ssh_port: 'The configured RH controller port is invalid.',
  ssh_identity_missing: 'The dedicated RH controller credential is unavailable.',
  known_hosts_missing: 'The pinned RH controller host identity is unavailable.',
  ssh_launch_failed: 'The local RH connection process could not start.',
  controller_operation_busy: 'Another protected controller operation is still active.',
  service_not_inactive: 'The selected RH Mission service is still active; historical diagnosis cannot begin.',
  systemd_status_failed: 'The controller could not read the selected service state.',
  systemd_status_invalid: 'The selected service state is incomplete or malformed.',
  mission_binding_missing: 'The RH controller has no retained Mission binding.',
  mission_binding_mismatch: 'The requested Mission differs from the controller-bound Mission.',
  mission_binding_invalid: 'The controller Mission binding failed its data or path contract.',
  mission_state_invalid: 'The retained Mission Host state failed its contract.',
  observation_module_invalid: 'The installed observation component failed its custody check.',
  observation_contract_invalid: 'The observation result failed its canonical contract.',
  observation_source_unsupported: 'The selected source does not supply this kind of observation.',
  observation_source_unavailable: 'The selected retained observation source is unavailable.',
  observation_selection_invalid: 'The selected observation incarnation is invalid.',
  observation_scope_mismatch: 'The observation source does not match the selected Mission.',
  observation_manifest_invalid: 'The retained observation index failed its contract.',
  observation_path_invalid: 'The selected observation file failed its path safety check.',
  observation_file_invalid: 'The selected observation file has an invalid type, identity or size.',
  observation_file_changed: 'The observation file changed during the exact read; retry the same selection.',
  observation_record_invalid: 'A retained observation record is malformed.',
  observation_digest_mismatch: 'Retained observation bytes disagree with their committed digest.',
  observation_cursor_invalid: 'The observation continuation receipt is invalid.',
  observation_cursor_scope_mismatch: 'The continuation receipt belongs to a different source selection.',
  observation_content_unavailable: 'The exact committed content revision or range is unavailable.',
  observation_content_invalid: 'The committed content failed its identity or byte integrity check.',
  observation_content_offset_invalid: 'The content offset is outside the selected segment or splits a UTF-8 character.',
  observation_content_page_too_small: 'The requested page cannot fit the next UTF-8 character.',
  diagnostic_store_invalid: 'The diagnostic receipt store failed its ownership or path check.',
  diagnostic_job_missing: 'The exact diagnostic job receipt is unavailable.',
  diagnostic_receipt_invalid: 'The retained diagnostic receipt failed its contract.',
  diagnostic_job_identity_mismatch: 'The diagnostic job does not match the requested Mission or incarnation.',
  diagnostic_unit_unavailable: 'The selected diagnostic process state could not be established.',
  diagnostic_selected_release_mismatch: 'The selected and bound RH release no longer matches the request.',
  diagnostic_release_invalid: 'The selected diagnostic release is unavailable or invalid.',
  diagnostic_requires_inactive: 'Historical diagnosis requires the selected Mission to be disabled and inactive.',
  diagnostic_job_conflict: 'This diagnostic job ID is already bound to another request.',
  diagnostic_result_identity_mismatch: 'The diagnostic result does not match the selected source.',
  diagnostic_cleanup_unverified: 'The selected diagnostic reader has not been proved quiescent.',
  diagnostic_source_unverified: 'The retained diagnostic source identity could not be verified.',
  diagnostic_source_mismatch: 'The diagnostic root does not match its retained Executive Epoch.',
} as const
export type RhControllerDiagnosticCode = keyof typeof RH_CONTROLLER_CODE_MESSAGES

export function isRhControllerDiagnosticCode(value: unknown): value is RhControllerDiagnosticCode {
  return typeof value === 'string' && Object.hasOwn(RH_CONTROLLER_CODE_MESSAGES, value)
}

export interface RhObservationSelection {
  mission_id: string
  epoch_id: string | null
  root_thread_id: string | null
  incarnation: string | null
}

export interface RhObservationIdentity extends Omit<RhObservationSelection, 'incarnation'> {
  thread_id: string | null
  turn_id: string | null
  item_id: string | null
  operation_id: string | null
  parent_thread_id: string | null
}

export interface RhObservationSource {
  host_id: string | null
  release_sha: string | null
  selected_release_sha: string | null
  bound_release_sha: string | null
  codex_version: string | null
  boot_id: string | null
  invocation_id: string | null
  origin: 'live' | 'history' | 'journal' | 'incident'
}

export interface RhObservationPosition {
  incarnation: string
  sequence: number
  /** Journald's opaque record identity; ordinal delivery positions can change after vacuum. */
  journal_cursor?: string | null
}

export interface RhObservationWatermark extends RhObservationPosition {
  content_sequence: number
  journal_cursor: string | null
}

export interface RhObservationCoverage {
  kind: 'known_loss' | 'uncertain_tail' | 'unsupported' | 'redacted' | 'unavailable' | 'cursor_expired'
  reason: string
  from_sequence: number | null
  to_sequence: number | null
}

export interface RhObservationContentHandle {
  incarnation: string
  content_id: string
  field: 'message' | 'assignment' | 'command' | 'output' | 'arguments' | 'result' | 'diff' | 'artifact' | 'error'
  revision: number
  update_mode: 'append' | 'snapshot'
  start_byte: number
  end_byte: number
  /** Known committed extent of this revision, never predicted eventual size. */
  total_bytes: number
  /** True only when the source has supplied completion. */
  complete: boolean
  encoding: 'utf-8'
  /** SHA-256 of exactly this handle's [start_byte, end_byte) UTF-8 bytes. */
  sha256: string
}

type ComparedValue = string | number | boolean | null | string[]
export interface RhObservationDetails {
  work: { action?: string; status?: string; sender_thread_id?: string | null; receiver_thread_ids?: string[]; child_thread_id?: string | null; depth?: number; agent_path?: string; source_parent_thread_id?: string | null; source_depth?: number | null; requested_model?: string | null; requested_reasoning_effort?: string | null }
  message: { role?: 'user' | 'assistant' | 'system'; phase?: 'commentary' | 'final' | null }
  command: { command_name?: string; cwd?: string; status?: string; exit_code?: number | null; signal?: string | null; duration_ms?: number | null; process_id?: string | null; output_mode?: 'streaming' | 'completion_only' | 'unavailable' | 'unknown' }
  tool: { tool_name?: string; server?: string | null; status?: string; success?: boolean | null; duration_ms?: number | null; output_mode?: 'streaming' | 'completion_only' | 'unavailable' | 'unknown' }
  file: { path?: string; change_kind?: string; status?: string }
  artifact: { owner_handle?: string; path?: string; media_type?: string }
  error: { code?: string; actual?: ComparedValue; expected?: ComparedValue; source_location?: string; containment?: boolean }
  lifecycle: { state?: string; stage?: string; reason?: string }
  coverage: { reason?: string; count?: number }
}

export type RhObservationKind = keyof RhObservationDetails
export type RhObservation = {
  [K in RhObservationKind]: {
    incarnation: string
    sequence: number
    journal_cursor?: string
    received_at: string
    source_time: string | null
    phase: 'received' | 'processed' | 'rejected'
    kind: K
    source_method: string
    identity: RhObservationIdentity
    summary: string
    details: RhObservationDetails[K]
    content: RhObservationContentHandle[]
    caused_by: RhObservationPosition | null
  }
}[RhObservationKind]

export interface RhObservationWorker {
  thread_id: string
  parent_thread_id: string | null
  root_thread_id: string | null
  status: string | null
  active_operation_id: string | null
  native_source?: {
    source_id: string
    relative_path: string
    archived: boolean
    history_mode: string | null
    creator_cli_version: string | null
  }
}

interface RhObservationPageBase {
  schema_version: typeof RH_OBSERVATION_SCHEMA_VERSION
  selection: RhObservationSelection
  source: RhObservationSource
  watermark: RhObservationWatermark
  coverage: RhObservationCoverage[]
  next_cursor: string | null
  has_more: boolean
}

export interface RhObservationSnapshot extends RhObservationPageBase {
  kind: 'snapshot'
  state: 'live' | 'disconnected' | 'history_only' | 'degraded' | 'inactive'
  workers: RhObservationWorker[]
  observations: RhObservation[]
  operational: RhObservationOperational | null
  sources: RhObservationSourceEntry[]
}

export interface RhObservationSourceEntry {
  incarnation: string
  source: RhObservationSource
  selection: RhObservationSelection
  watermark: RhObservationWatermark
}

export interface RhObservationOperational {
  observed_at: string
  service: {
    active_state: string | null
    sub_state: string | null
    main_pid: number | null
    result: string | null
    exec_main_status: number | null
    unit_file_state: string | null
  }
  host: {
    mission_id: string
    updated_at: string | null
    epoch_id: string | null
    root_thread_id: string | null
    goal_phase: string | null
    failure_reason: string | null
    capture_recovery_count: number
    checkpoint_stop_pending: boolean
    single_epoch_canary_pending: boolean
    force_stop_pending: boolean
  } | null
}

export interface RhObservationActivityPage extends RhObservationPageBase {
  kind: 'activity'
  observations: RhObservation[]
}

export interface RhObservationContentPage extends RhObservationPageBase {
  kind: 'content'
  availability: 'available' | 'unavailable' | 'redacted' | 'unsupported'
  handle: RhObservationContentHandle
  text: string | null
}

export interface RhObservationDiagnosticJob {
  schema_version: typeof RH_OBSERVATION_SCHEMA_VERSION
  kind: 'diagnostic_job'
  selection: RhObservationSelection
  job_id: string
  job_incarnation: string
  diagnostic_release_sha: string
  expected_selected_release_sha: string
  unit_name: string
  state: 'starting' | 'running' | 'cancelling' | 'completed' | 'cancelled' | 'failed'
  stage: string
  elapsed_seconds: number
  progress: { completed: number; total: number | null; message: string | null }
  result_handle: string | null
  error: string | null
  quiescent: boolean
  source_protection_held: boolean
}

export interface RhObservationIncident {
  schema_version: typeof RH_OBSERVATION_SCHEMA_VERSION
  kind: 'incident'
  incident_id: string
  created_at: string
  snapshot: RhObservationSnapshot
  activity: RhObservationActivityPage[]
  content: RhObservationContentPage[]
  diagnostic_job: RhObservationDiagnosticJob | null
  before: RhObservationSource | null
  after: RhObservationSource | null
  coverage: RhObservationCoverage[]
}

export type RhObservationResponse = RhObservationSnapshot | RhObservationActivityPage | RhObservationContentPage | RhObservationDiagnosticJob | RhObservationIncident

export interface RhObservationPageRequest {
  cursor?: string | null
  limit?: number
}

export interface RhObservationContentRequest {
  content_id: string
  revision: number
  /** Omission starts at the immutable segment's own start byte. */
  offset_bytes?: number
  max_bytes?: number
}

export interface RhDiagnosticStartRequest {
  mission_id: string
  executive_epoch_id: string
  root_thread_id: string
  job_id: string
  diagnostic_release_sha: string
  expected_selected_release_sha: string
}

export interface RhDiagnosticJobRequest {
  mission_id: string
  job_id: string
  job_incarnation: string
}

/** Receipts are our delivery identities. Identical native delta text is not a retry key. */
export function rhObservationKey(observation: RhObservationPosition): string {
  if (observation.journal_cursor) return JSON.stringify([observation.incarnation, 'journal', observation.journal_cursor])
  return JSON.stringify([observation.incarnation, observation.sequence])
}

/** Preserve first receipt on duplicate reads; never claim ordering across incarnations. */
export function mergeRhObservations(existing: readonly RhObservation[], incoming: readonly RhObservation[]): RhObservation[] {
  const byReceipt = new Map(existing.map((value) => [rhObservationKey(value), value]))
  for (const value of incoming) {
    const key = rhObservationKey(value)
    if (!byReceipt.has(key)) byReceipt.set(key, value)
  }
  return [...byReceipt.values()]
}

export interface RhObservationContentView {
  incarnation: string
  content_id: string
  revision: number
  text: string
  start_byte: number
  end_byte: number
  total_bytes: number
  complete: boolean
}

/** Join immutable append ranges; a new snapshot revision replaces the display. */
export function mergeRhContentPage(previous: RhObservationContentView | null, page: RhObservationContentPage): RhObservationContentView | null {
  if (page.availability !== 'available' || page.text === null) return previous
  const { handle } = page
  if (new TextEncoder().encode(page.text).length !== handle.end_byte - handle.start_byte) {
    throw new Error('RH content byte range does not match UTF-8 text')
  }
  const sameContent = previous !== null && previous.incarnation === handle.incarnation && previous.content_id === handle.content_id
  const replacing = sameContent && handle.update_mode === 'snapshot' && handle.revision > previous.revision
  const continuing = sameContent && !replacing
  if (continuing && handle.revision <= previous.revision && handle.end_byte <= previous.end_byte && handle.start_byte >= previous.start_byte) {
    const held = new TextEncoder().encode(previous.text).slice(handle.start_byte, handle.end_byte)
    if (new TextDecoder('utf-8', { fatal: true }).decode(held) !== page.text) throw new Error('RH content retry disagrees with committed bytes')
    return previous
  }
  if (continuing && handle.revision < previous.revision) throw new Error('RH content revision is stale')
  if (continuing && handle.start_byte !== previous.end_byte) throw new Error('RH content page has a gap or overlap')
  if (!continuing && handle.start_byte !== 0) throw new Error('RH content revision requires its first page')
  return {
    incarnation: handle.incarnation,
    content_id: handle.content_id,
    revision: handle.revision,
    text: continuing ? previous.text + page.text : page.text,
    start_byte: 0,
    end_byte: handle.end_byte,
    total_bytes: handle.total_bytes,
    complete: handle.complete && handle.end_byte === handle.total_bytes,
  }
}
