import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'

import type { CodexExecutionObservation } from '../../../packages/codex-thread-core/dist/index.js'
import { createLogger } from '../../../packages/codex-thread-core/dist/logger.js'
import {
  ObservationTextSanitizer,
  scrubObservationValue,
} from '../../../packages/codex-remote-core/dist/observation-content.js'
import type { RhObservationPosition, RhObservationSource } from '../../../packages/codex-remote-core/dist/rh-observation.js'
import type { MissionHostConfig } from './config.js'
import type {
  CheckpointSourceFailureWarning,
  SemanticOperationBinding,
} from './mission-bridge.js'
import {
  RhObservationStore,
  type RhObservationStoreContent,
  type RhObservationStoreHealth,
  type RhObservationStoreInput,
  type RhObservationStoreLimits,
} from './observation-store.js'

type FieldState = {
  scanner: ObservationTextSanitizer
  offset: number
  complete: boolean
}

export interface MissionExecutionObserverOptions {
  incarnation?: string
  source?: RhObservationSource
  limits?: Partial<RhObservationStoreLimits>
  onHealth?: (health: RhObservationStoreHealth) => void
}

/** Fixed process provenance, read once at startup without another process. */
export async function observeMissionProcessSource(config: Pick<MissionHostConfig, 'releaseSha'>): Promise<RhObservationSource> {
  let bootId: string | null = null
  if (process.platform === 'linux') {
    try {
      const value = (await fs.promises.readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim()
      if (/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)) bootId = value
    } catch { /* source unavailable is not a research startup failure */ }
  }
  return {
    host_id: os.hostname(), release_sha: config.releaseSha, selected_release_sha: null, bound_release_sha: null,
    codex_version: null, boot_id: bootId, invocation_id: process.env.INVOCATION_ID ?? null, origin: 'live',
  }
}

/** A process-lifetime adapter, independent of model-result grants and owner custody. */
export class MissionExecutionObserver {
  readonly incarnation: string
  readonly store: RhObservationStore
  private sequence = 0
  private readonly fields = new Map<string, FieldState>()
  private readonly maxFieldStates = 4096
  private suppressUnknownAppends = false
  private lastHealth = ''
  private readonly logger = createLogger('rh_mission_host')

  constructor(
    private readonly config: Pick<MissionHostConfig, 'runtimeDir' | 'missionId' | 'releaseSha'>,
    options: MissionExecutionObserverOptions = {},
  ) {
    this.incarnation = options.incarnation ?? randomUUID()
    this.store = new RhObservationStore({
      runtimeDir: config.runtimeDir,
      incarnation: this.incarnation,
      selection: { mission_id: config.missionId, epoch_id: null, root_thread_id: null, incarnation: this.incarnation },
      source: options.source ?? {
        host_id: os.hostname(), release_sha: config.releaseSha,
        selected_release_sha: null, bound_release_sha: null,
        // Configured expected version is not an observed native handshake.
        codex_version: null, boot_id: null, invocation_id: process.env.INVOCATION_ID ?? null, origin: 'live',
      },
      limits: options.limits,
      onHealth: (health) => {
        try {
          const result = options.onHealth?.(health) as unknown
          if (result && typeof (result as Promise<unknown>).then === 'function') void Promise.resolve(result).catch(() => undefined)
        } catch { /* telemetry cannot reject a native call */ }
        // Admission happens per source item; journal work follows committed cuts
        // and health transitions instead of logging every intermediate queue size.
        const line = JSON.stringify([health.state, health.committed_sequence, health.error_code, health.dropped_observations])
        if (line === this.lastHealth) return
        this.lastHealth = line
        this.logger.info('rh_mission.observation_health', { mission_id: config.missionId, ...health })
      },
    })
  }

  /** Returns receipt identity immediately. Filesystem work belongs to the detached store. */
  observe(event: CodexExecutionObservation, epochId: string | null): RhObservationPosition {
    const position = { incarnation: this.incarnation, sequence: ++this.sequence }
    try {
      const contents: RhObservationStoreContent[] = []
      let redactions = 0
      let stateUnavailable = 0
      let lateAppends = 0
      for (const input of event.contents) {
        const contentId = createHash('sha256').update(input.content_key).digest('hex')
        let state = this.fields.get(contentId)
        let selectedContentId = contentId
        if (state?.complete && input.update_mode === 'append') {
          // The API supplied content after completion. Retain it separately rather
          // than silently appending it to a claimed complete native item.
          lateAppends += 1
          selectedContentId = createHash('sha256').update(`${input.content_key}:${position.sequence}`).digest('hex')
          state = undefined
        }
        if (!state && input.update_mode === 'append' && this.suppressUnknownAppends) {
          // After a stream-state capacity loss, a new key might be continuation
          // of an omitted credential prefix. A full native snapshot can reestablish
          // context; guessing that these bytes start a new field is unsafe.
          stateUnavailable += 1
          continue
        }
        let ephemeralSnapshot = false
        if (!state || input.update_mode === 'snapshot') {
          if (!this.fields.has(selectedContentId) && this.fields.size >= this.maxFieldStates) {
            const old = [...this.fields.entries()].find(([, value]) => value.complete)?.[0]
            if (old !== undefined) {
              this.fields.delete(old)
            } else {
              this.suppressUnknownAppends = true
              stateUnavailable += 1
              if (input.update_mode === 'append') continue
              // Full snapshots remain readable without discarding any active
              // scanner. Their later unknown deltas are explicitly omitted.
              ephemeralSnapshot = true
            }
          }
          state = { scanner: new ObservationTextSanitizer(), offset: 0, complete: false }
          if (!ephemeralSnapshot) this.fields.set(selectedContentId, state)
        }
        const first = state.scanner.push(input.text)
        // These native notifications carry independent full-value snapshots and
        // may never receive an item/completed event. Item-start snapshots retain
        // their scanner because a credential can span that snapshot and a delta.
        const independentSnapshot = input.update_mode === 'snapshot' &&
          ['turn/diff/updated', 'turn/plan/updated', 'item/fileChange/patchUpdated'].includes(event.source_method)
        const final = input.complete || independentSnapshot || ephemeralSnapshot ? state.scanner.finish() : null
        const text = first.text + (final?.text ?? '')
        redactions += first.redactionCount + (final?.redactionCount ?? 0)
        const startByte = input.update_mode === 'snapshot' ? 0 : state.offset
        state.offset = startByte + Buffer.byteLength(text, 'utf8')
        state.complete = input.complete
        // Empty snapshots clear display state; empty final appends mark source completion.
        if (text.length > 0 || input.update_mode === 'snapshot' || input.complete) {
          contents.push({ content_id: selectedContentId, field: input.field, text,
            update_mode: input.update_mode, start_byte: startByte, complete: input.complete })
        }
      }
      const details = scrubObservationValue(event.details)
      redactions += details.redactionCount
      const { contents: _privateContent, source_facts: sourceFacts, ...metadata } = event
      const sourceConflict = sourceFacts && !this.store.recordCodexVersion(sourceFacts.codex_version)
      const row = { ...metadata, ...position,
        identity: { ...event.identity, mission_id: this.config.missionId, epoch_id: epochId },
        details: details.value,
      } as RhObservationStoreInput
      this.store.admit(row, contents)
      if (sourceConflict) this.coverage(event, epochId, position, 'native_source_version_changed', 1)
      if (redactions > 0) this.coverage(event, epochId, position, 'credential_signatures_redacted', redactions)
      if (stateUnavailable > 0) this.coverage(event, epochId, position, 'live_content_stream_state_unavailable', stateUnavailable)
      if (lateAppends > 0) this.coverage(event, epochId, position, 'content_after_source_completion', lateAppends)
    } catch {
      this.coverage(event, epochId, position, 'observation_projection_failed', 1)
    }
    return position
  }

  observeCheckpointSourceFailure(
    warning: CheckpointSourceFailureWarning,
    binding: SemanticOperationBinding,
  ): RhObservationPosition {
    const writerPreserved =
      warning.code === 'checkpoint_source_snapshot_skipped_writer_preserved'
    const operationTeardownFailed =
      warning.code === 'checkpoint_source_operation_teardown_failed_writer_revalidated'
    return this.observe({
      identity: {
        root_thread_id: binding.rootThreadId,
        thread_id: binding.rootThreadId,
        parent_thread_id: null,
        turn_id: null,
        item_id: null,
        operation_id: null,
      },
      received_at: new Date().toISOString(),
      source_time: null,
      source_method: 'host/checkpoint-source-handoff',
      phase: 'processed',
      caused_by: null,
      kind: 'error',
      summary: operationTeardownFailed
        ? 'Checkpoint completed; source-operation teardown failed after the active writer was revalidated'
        : writerPreserved
          ? 'Checkpoint completed; this snapshot was skipped and the existing writer was preserved'
          : 'Checkpoint completed; no usable new backup source was reported',
      details: {
        code: warning.code,
        actual: warning.reason_code,
        expected: 'checkpoint_source_published',
        source_location: 'WorkspaceStore.publish_checkpoint_source_handoff',
        containment: false,
      },
      contents: [{
        content_key: `checkpoint-source-handoff:${binding.rootThreadId}:${binding.executiveEpochId}`,
        field: 'error',
        text: JSON.stringify(warning),
        update_mode: 'snapshot',
        complete: true,
      }],
    }, binding.executiveEpochId)
  }

  private coverage(event: CodexExecutionObservation, epochId: string | null, causedBy: RhObservationPosition, reason: string, count: number): void {
    try {
      this.store.admit({ incarnation: this.incarnation, sequence: ++this.sequence,
        identity: { ...event.identity, mission_id: this.config.missionId, epoch_id: epochId },
        received_at: new Date().toISOString(), source_time: null, phase: 'processed', kind: 'coverage',
        source_method: 'host/observation', summary: 'Observation coverage limitation',
        details: { reason, count }, caused_by: causedBy,
      })
    } catch { /* a failed observation sink must remain independent of research */ }
  }

  flush(): Promise<void> { return this.store.flush() }
  close(): Promise<void> { return this.store.close() }
}
