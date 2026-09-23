import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import {
  RH_OBSERVATION_SCHEMA_VERSION,
  type RhObservation,
  type RhObservationContentHandle,
  type RhObservationCoverage,
  type RhObservationSelection,
  type RhObservationSource,
  type RhObservationWatermark,
  type RhObservationWorker,
} from '../../../packages/codex-remote-core/dist/rh-observation.js'

/** These bounds limit diagnostic custody, never research or native output. */
export const RH_OBSERVATION_STORE_DEFAULTS = Object.freeze({
  pageBytes: 512 * 1024,
  segmentBytes: 1024 * 1024,
  flushMilliseconds: 250,
  queueBytes: 32 * 1024 * 1024,
  retainedBytes: 1024 * 1024 * 1024,
  retentionMilliseconds: 7 * 24 * 60 * 60 * 1000,
  diskReserveBytes: 1024 * 1024 * 1024,
  maxSegments: 8192,
  maxIncarnations: 128,
  maxWorkers: 4096,
})

type WithoutContent<T> = T extends unknown ? Omit<T, 'content'> : never
export type RhObservationStoreInput = WithoutContent<RhObservation>

/** Offsets are assigned by the Host after content sanitization. */
export interface RhObservationStoreContent {
  content_id: string
  field: RhObservationContentHandle['field']
  text: string
  update_mode: 'append' | 'snapshot'
  start_byte: number
  complete: boolean
}

export interface RhObservationStoreHealth {
  state: 'initializing' | 'healthy' | 'degraded' | 'closed'
  incarnation: string
  queued_bytes: number
  observed_sequence: number
  committed_sequence: number
  dropped_observations: number
  error_code: 'invalid_admission' | 'queue_full' | 'disk_reserve' | 'unsafe_path' | 'write_failed' | null
}

export interface RhObservationStoreOptions {
  runtimeDir: string
  incarnation: string
  selection: RhObservationSelection
  source: RhObservationSource
  limits?: Partial<RhObservationStoreLimits>
  onHealth?: (health: RhObservationStoreHealth) => void
}

export type RhObservationStoreLimits = { [K in keyof typeof RH_OBSERVATION_STORE_DEFAULTS]: number }

export interface RhObservationStoreSegment {
  file: string
  first_sequence: number
  last_sequence: number
  bytes: number
  sha256: string
  created_at: string
  /** Includes allocation for the segment and its content files. */
  retained_bytes: number
  /** Size-tier compaction only; not an observation or causal identity. */
  level: number
}

/** Internal artifact index. Wire rows/handles retain the shared contract. */
export interface RhObservationStoreManifest {
  schema_version: typeof RH_OBSERVATION_SCHEMA_VERSION
  selection: RhObservationSelection
  source: RhObservationSource
  watermark: RhObservationWatermark
  coverage: RhObservationCoverage[]
  first_available_sequence: number
  clean_shutdown: boolean
  updated_at: string
  segments: RhObservationStoreSegment[]
  workers: RhObservationWorker[]
}

interface QueuedObservation {
  observation: RhObservationStoreInput
  contents: RhObservationStoreContent[]
  reservedBytes: number
}

const INCARNATION = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/
const CONTENT_ID = /^[a-f0-9]{64}$/
const SEGMENT_FILE = /^segment\.[1-9][0-9]*\.[1-9][0-9]*\.jsonl$/
const CONTENT_FILE = /^content\.[a-f0-9]{64}\.[1-9][0-9]*\.[0-9]+-[0-9]+\.txt$/
const PRIVATE_FILE = /^(?:manifest\.json|current\.json|segment\.[1-9][0-9]*\.[1-9][0-9]*\.jsonl|content\.[a-f0-9]{64}\.[1-9][0-9]*\.[0-9]+-[0-9]+\.txt)$/
const TEMP_FILE = /^\.(?:manifest\.json|segment\.[1-9][0-9]*\.[1-9][0-9]*\.jsonl|content\.[a-f0-9]{64}\.[1-9][0-9]*\.[0-9]+-[0-9]+\.txt)\.[a-f0-9-]{36}\.tmp$/
const COMPACTION_FAN_IN = 32
const MAX_COVERAGE = 128
const MAX_METADATA_BYTES = 64 * 1024
const MAX_WORKER_SNAPSHOT_BYTES = 1024 * 1024
const ALLOCATION_BYTES = 4096

class ObservationStoreError extends Error {
  constructor(readonly code: 'unsafe_path' | 'disk_reserve' | 'write_failed') {
    super(`RH observation storage ${code}`)
  }
}

function sha256(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex')
}

function allocatedBytes(bytes: number): number {
  return Math.ceil(Math.max(bytes, 1) / ALLOCATION_BYTES) * ALLOCATION_BYTES
}

function sameIdentity(left: fs.Stats, right: fs.Stats): boolean {
  return left.dev === right.dev && left.ino === right.ino && left.size === right.size && left.mtimeMs === right.mtimeMs
}

function isAbsent(error: unknown): boolean {
  return (error as NodeJS.ErrnoException).code === 'ENOENT'
}

function validateDirectory(stat: fs.Stats, privateDirectory: boolean): void {
  if (!stat.isDirectory() || stat.isSymbolicLink() ||
    (process.platform !== 'win32' && (stat.mode & (privateDirectory ? 0o077 : 0o022)) !== 0)) {
    throw new ObservationStoreError('unsafe_path')
  }
}

/** Follow the Host's no-symlink/private-parent custody, including the extra child. */
async function requireDirectory(directory: string, privateDirectory = true): Promise<void> {
  validateDirectory(await fs.promises.lstat(directory), privateDirectory)
  // Windows realpath expands 8.3 names (including os.tmpdir), not just links.
  if (process.platform !== 'win32' && path.resolve(await fs.promises.realpath(directory)) !== path.resolve(directory)) {
    throw new ObservationStoreError('unsafe_path')
  }
}

async function requireStoreDirectory(runtimeDir: string, incarnation?: string): Promise<string> {
  await requireDirectory(runtimeDir, false)
  const root = path.join(runtimeDir, 'observations')
  await requireDirectory(root)
  if (incarnation === undefined) return root
  if (!INCARNATION.test(incarnation)) throw new ObservationStoreError('unsafe_path')
  const directory = path.join(root, incarnation)
  await requireDirectory(directory)
  return directory
}

async function syncDirectory(directory: string): Promise<void> {
  // Windows does not support the directory fsync used by the Linux Host.
  if (process.platform === 'win32') return
  const file = await fs.promises.open(directory, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)
  try { await file.sync() } finally { await file.close() }
}

async function readPrivateFile(directory: string, name: string, maximumBytes: number): Promise<Buffer> {
  if (!PRIVATE_FILE.test(name)) throw new ObservationStoreError('unsafe_path')
  await requireDirectory(directory)
  const filePath = path.join(directory, name)
  const before = await fs.promises.lstat(filePath)
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > maximumBytes ||
    (process.platform !== 'win32' && (before.mode & 0o077) !== 0)) {
    throw new ObservationStoreError('unsafe_path')
  }
  const file = await fs.promises.open(filePath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)
  try {
    if (!sameIdentity(before, await file.stat())) throw new ObservationStoreError('unsafe_path')
    const bytes = await file.readFile()
    if (bytes.length !== before.size || !sameIdentity(before, await file.stat())) throw new ObservationStoreError('unsafe_path')
    await requireDirectory(directory)
    if (!sameIdentity(before, await fs.promises.lstat(filePath))) throw new ObservationStoreError('unsafe_path')
    return bytes
  } finally { await file.close() }
}

async function publishPrivateFile(directory: string, name: string, bytes: string | Buffer, replace = false, onPublished?: () => void): Promise<void> {
  if (!PRIVATE_FILE.test(name)) throw new ObservationStoreError('unsafe_path')
  await requireDirectory(directory)
  const destination = path.join(directory, name)
  try {
    const existing = await fs.promises.lstat(destination)
    if (!replace || !existing.isFile() || existing.isSymbolicLink() || existing.nlink !== 1) {
      throw new ObservationStoreError('unsafe_path')
    }
  } catch (error) { if (!isAbsent(error)) throw error }
  const temporary = path.join(directory, `.${name}.${randomUUID()}.tmp`)
  try {
    const file = await fs.promises.open(temporary,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600)
    try {
      await file.writeFile(bytes)
      await file.sync()
    } finally { await file.close() }
    await requireDirectory(directory)
    await fs.promises.rename(temporary, destination)
    onPublished?.()
    await syncDirectory(directory)
  } catch (error) {
    try { await fs.promises.unlink(temporary) } catch { /* It may already be published. */ }
    throw error
  }
}

async function unlinkPrivateFile(directory: string, name: string): Promise<void> {
  if (!PRIVATE_FILE.test(name)) throw new ObservationStoreError('unsafe_path')
  await requireDirectory(directory)
  try {
    const stat = await fs.promises.lstat(path.join(directory, name))
    if (!stat.isFile() || stat.isSymbolicLink()) throw new ObservationStoreError('unsafe_path')
    await fs.promises.unlink(path.join(directory, name))
  } catch (error) { if (!isAbsent(error)) throw error }
}

export function rhObservationContentFile(handle: RhObservationContentHandle): string {
  if (!CONTENT_ID.test(handle.content_id) || !Number.isSafeInteger(handle.revision) || handle.revision < 1 ||
    !Number.isSafeInteger(handle.start_byte) || handle.start_byte < 0 || !Number.isSafeInteger(handle.end_byte) || handle.end_byte < handle.start_byte) {
    throw new ObservationStoreError('unsafe_path')
  }
  return `content.${handle.content_id}.${handle.revision}.${handle.start_byte}-${handle.end_byte}.txt`
}

async function readSegment(directory: string, segment: RhObservationStoreSegment): Promise<RhObservation[]> {
  if (!SEGMENT_FILE.test(segment.file)) throw new ObservationStoreError('unsafe_path')
  const bytes = await readPrivateFile(directory, segment.file, segment.bytes)
  if (bytes.length !== segment.bytes || sha256(bytes) !== segment.sha256) throw new ObservationStoreError('write_failed')
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  if (!text.endsWith('\n')) throw new ObservationStoreError('write_failed')
  const rows = text.trimEnd().split('\n').map((row) => JSON.parse(row) as RhObservation)
  let previous = segment.first_sequence - 1
  for (const row of rows) {
    if (!Number.isSafeInteger(row.sequence) || row.sequence <= previous || row.sequence > segment.last_sequence || !Array.isArray(row.content)) {
      throw new ObservationStoreError('write_failed')
    }
    previous = row.sequence
  }
  if (rows[0]?.sequence !== segment.first_sequence || previous !== segment.last_sequence) throw new ObservationStoreError('write_failed')
  return rows
}

/** Reads only the declared committed cut. It does not initialize or repair storage. */
export async function readRhObservationManifest(runtimeDir: string, incarnation: string): Promise<RhObservationStoreManifest> {
  const directory = await requireStoreDirectory(path.resolve(runtimeDir), incarnation)
  const value = JSON.parse((await readPrivateFile(directory, 'manifest.json', 8 * 1024 * 1024)).toString('utf8')) as RhObservationStoreManifest
  if (value.schema_version !== RH_OBSERVATION_SCHEMA_VERSION || value.selection?.incarnation !== incarnation ||
    value.watermark?.incarnation !== incarnation || !Array.isArray(value.segments) || value.segments.length > 8192 ||
    !Number.isSafeInteger(value.first_available_sequence) || !Number.isSafeInteger(value.watermark.sequence) ||
    !Array.isArray(value.coverage)) throw new ObservationStoreError('write_failed')
  return value
}

async function withCommittedManifest<T>(runtimeDir: string, incarnation: string, read: (manifest: RhObservationStoreManifest) => Promise<T>): Promise<T> {
  const manifest = await readRhObservationManifest(runtimeDir, incarnation)
  try { return await read(manifest) } catch (error) {
    if (!isAbsent(error)) throw error
    const current = await readRhObservationManifest(runtimeDir, incarnation)
    if (JSON.stringify(current.segments) === JSON.stringify(manifest.segments)) throw error
    // A changed cut explains the missing old compacted/retired segment. Re-read
    // that new cut, without pretending a repeatedly changing read was complete.
    return read(current)
  }
}

/** Local focused reader also used to verify producer artifacts. Controller owns remote paging. */
export async function readRhObservationRows(runtimeDir: string, incarnation: string, afterSequence = 0, limit = 100): Promise<{ manifest: RhObservationStoreManifest; observations: RhObservation[]; expired: boolean }> {
  if (!Number.isSafeInteger(afterSequence) || afterSequence < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 1000) {
    throw new ObservationStoreError('unsafe_path')
  }
  const directory = await requireStoreDirectory(path.resolve(runtimeDir), incarnation)
  return withCommittedManifest(runtimeDir, incarnation, async (manifest) => {
    const observations: RhObservation[] = []
    for (const segment of manifest.segments) {
      if (segment.last_sequence <= afterSequence) continue
      for (const observation of await readSegment(directory, segment)) {
        if (observation.incarnation !== incarnation) throw new ObservationStoreError('write_failed')
        if (observation.sequence > afterSequence) observations.push(observation)
        if (observations.length === limit) break
      }
      if (observations.length === limit) break
    }
    return { manifest, observations, expired: afterSequence > 0 && afterSequence < manifest.first_available_sequence - 1 }
  })
}

export async function readRhObservationContent(runtimeDir: string, handle: RhObservationContentHandle, offset = handle.start_byte, pageBytes = RH_OBSERVATION_STORE_DEFAULTS.pageBytes): Promise<{ text: string; next_offset: number; has_more: boolean }> {
  if (!Number.isSafeInteger(pageBytes) || pageBytes < 4 || pageBytes > RH_OBSERVATION_STORE_DEFAULTS.pageBytes ||
    !Number.isSafeInteger(offset) || offset < handle.start_byte || offset > handle.end_byte) throw new ObservationStoreError('unsafe_path')
  const directory = await requireStoreDirectory(path.resolve(runtimeDir), handle.incarnation)
  const bytes = await withCommittedManifest(runtimeDir, handle.incarnation, async (manifest) => {
    const segment = manifest.segments.find((candidate) => candidate.first_sequence <= handle.revision && candidate.last_sequence >= handle.revision)
    if (!segment) throw new ObservationStoreError('write_failed')
    const row = (await readSegment(directory, segment)).find((candidate) => candidate.sequence === handle.revision)
    if (!row?.content.some((candidate) => Object.keys(candidate).length === Object.keys(handle).length &&
      Object.entries(candidate).every(([key, value]) => (handle as unknown as Record<string, unknown>)[key] === value))) throw new ObservationStoreError('unsafe_path')
    return readPrivateFile(directory, rhObservationContentFile(handle), handle.end_byte - handle.start_byte)
  })
  if (bytes.length !== handle.end_byte - handle.start_byte || sha256(bytes) !== handle.sha256) throw new ObservationStoreError('write_failed')
  const localOffset = offset - handle.start_byte
  if (localOffset < bytes.length && (bytes[localOffset] & 0xc0) === 0x80) throw new ObservationStoreError('unsafe_path')
  let end = Math.min(bytes.length, localOffset + pageBytes)
  while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end -= 1
  if (end === localOffset && end !== bytes.length) throw new ObservationStoreError('write_failed')
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(localOffset, end))
  return { text, next_offset: handle.start_byte + end, has_more: end < bytes.length }
}

/** A detached, bounded writer. admit never performs filesystem I/O or awaits the sink. */
export class RhObservationStore {
  readonly limits: RhObservationStoreLimits
  private readonly runtimeDir: string
  private readonly directory: string
  private queue: QueuedObservation[] = []
  private queueBytes = 0
  private seen = 0
  private dropped = 0
  private coverage: RhObservationCoverage[] = []
  private manifest: RhObservationStoreManifest
  private timer: NodeJS.Timeout | null = null
  private writer: Promise<void> | null = null
  private initialized = false
  private createdDirectory = false
  private closed = false
  private dirty = true
  private state: RhObservationStoreHealth['state'] = 'initializing'
  private errorCode: RhObservationStoreHealth['error_code'] = null
  private readonly uncommittedFiles = new Set<string>()
  private pendingPublication: { through: number; sequences: number[] } | null = null
  private readonly inspectedPriorIncarnations = new Set<string>()
  private observedCodexVersion: string | null

  constructor(private readonly options: RhObservationStoreOptions) {
    if (!INCARNATION.test(options.incarnation) || options.selection.incarnation !== options.incarnation || !options.selection.mission_id) {
      throw new ObservationStoreError('unsafe_path')
    }
    this.limits = { ...RH_OBSERVATION_STORE_DEFAULTS, ...options.limits }
    for (const [name, value] of Object.entries(this.limits)) {
      if (!Number.isSafeInteger(value) || value < (name === 'diskReserveBytes' ? 0 : 1)) throw new ObservationStoreError('unsafe_path')
    }
    if (this.limits.maxSegments > 8192 || this.limits.maxIncarnations > 128 || this.limits.maxWorkers > 4096) throw new ObservationStoreError('unsafe_path')
    this.runtimeDir = path.resolve(options.runtimeDir)
    this.observedCodexVersion = options.source.codex_version
    this.directory = path.join(this.runtimeDir, 'observations', options.incarnation)
    this.manifest = {
      schema_version: RH_OBSERVATION_SCHEMA_VERSION,
      selection: { ...options.selection }, source: { ...options.source },
      watermark: { incarnation: options.incarnation, sequence: 0, content_sequence: 0, journal_cursor: null },
      coverage: [], first_available_sequence: 1, clean_shutdown: false,
      updated_at: new Date().toISOString(), segments: [], workers: [],
    }
    if (this.limits.pageBytes < 4 || this.limits.pageBytes > RH_OBSERVATION_STORE_DEFAULTS.pageBytes || this.limits.segmentBytes > 1024 * 1024) throw new ObservationStoreError('unsafe_path')
    this.schedule()
  }

  health(): RhObservationStoreHealth {
    return { state: this.state, incarnation: this.options.incarnation, queued_bytes: this.queueBytes,
      observed_sequence: this.seen, committed_sequence: this.manifest.watermark.sequence,
      dropped_observations: this.dropped, error_code: this.errorCode }
  }

  /** Enrich only the native version observed by this process's own handshake. */
  recordCodexVersion(version: string): boolean {
    if (this.closed || !version || version.length > 256 || /[\r\n]/.test(version)) return false
    const observed = this.observedCodexVersion
    if (observed !== null) return observed === version
    this.observedCodexVersion = version
    this.dirty = true
    return true
  }

  admit(observation: RhObservationStoreInput, contents: readonly RhObservationStoreContent[] = []): boolean {
    try {
      if (this.closed || observation.incarnation !== this.options.incarnation || !Number.isSafeInteger(observation.sequence) ||
        observation.sequence <= this.seen || observation.identity.mission_id !== this.options.selection.mission_id ||
        (this.options.selection.epoch_id !== null && observation.identity.epoch_id !== this.options.selection.epoch_id) ||
        (this.options.selection.root_thread_id !== null && observation.identity.root_thread_id !== this.options.selection.root_thread_id)) {
        this.notify('degraded', 'invalid_admission')
        return false
      }
      if (observation.sequence > this.seen + 1) this.addLoss(this.seen + 1, observation.sequence - 1, 'producer_sequence_gap')
      this.seen = observation.sequence
      let reservedBytes = Buffer.byteLength(JSON.stringify(observation), 'utf8') * 2 + 1024
      let metadataBytes = reservedBytes
      const ids = new Set<string>()
      if (reservedBytes > MAX_METADATA_BYTES || contents.length > 16) throw new Error('invalid admission')
      for (const content of contents) {
        if (!CONTENT_ID.test(content.content_id) || ids.has(content.content_id) || !Number.isSafeInteger(content.start_byte) || content.start_byte < 0 ||
          (content.update_mode === 'snapshot' && content.start_byte !== 0) || typeof content.text !== 'string') throw new Error('invalid admission')
        ids.add(content.content_id)
        // Account strings, encoded buffers, handle/JSON expansion, and the in-flight batch.
        const contentBytes = Buffer.byteLength(content.text, 'utf8')
        if (!Number.isSafeInteger(content.start_byte + contentBytes)) throw new Error('invalid admission')
        const chunks = Math.max(1, Math.ceil(contentBytes / Math.max(1, this.limits.pageBytes - 3)))
        metadataBytes += chunks * 1024
        if (metadataBytes > MAX_METADATA_BYTES) throw new Error('invalid admission')
        reservedBytes += content.text.length * 2 + contentBytes + chunks * 2048
      }
      if (reservedBytes > this.limits.queueBytes - this.queueBytes) {
        this.addLoss(observation.sequence, observation.sequence, 'queue_full')
        this.notify('degraded', 'queue_full')
        this.schedule()
        return false
      }
      this.queue.push({ observation: structuredClone(observation), contents: contents.map((content) => ({ ...content })), reservedBytes })
      this.queueBytes += reservedBytes
      this.dirty = true
      this.schedule()
      return true
    } catch {
      if (Number.isSafeInteger(observation?.sequence) && observation.sequence > 0) this.addLoss(observation.sequence, observation.sequence, 'invalid_admission')
      this.notify('degraded', 'invalid_admission')
      this.schedule()
      return false
    }
  }

  /** Explicit flush is never part of Core business processing or containment. */
  async flush(): Promise<void> {
    if (this.timer) { clearTimeout(this.timer); this.timer = null }
    if (this.writer) { await this.writer; if (!this.dirty && this.queue.length === 0) return }
    const writer = this.writePending().catch((error: unknown) => {
      this.notify('degraded', error instanceof ObservationStoreError ? error.code : 'write_failed')
    })
    this.writer = writer
    try { await writer } finally { if (this.writer === writer) this.writer = null }
    if (!this.closed && (this.dirty || this.queue.length > 0)) this.schedule()
  }

  /** Caller may choose a bounded shutdown wait; research never waits on this method. */
  async close(): Promise<void> {
    this.closed = true
    await this.flush()
    if (this.initialized && this.queue.length === 0 && !this.dirty) {
      try {
        const next = { ...this.manifest, clean_shutdown: true, updated_at: new Date().toISOString() }
        await this.publishManifest(next)
        this.manifest = next
        this.notify('closed', this.errorCode)
      } catch (error) { this.notify('degraded', error instanceof ObservationStoreError ? error.code : 'write_failed') }
    }
  }

  private schedule(): void {
    if (this.closed || this.timer || this.writer) return
    this.timer = setTimeout(() => { this.timer = null; void this.flush() }, this.limits.flushMilliseconds)
    this.timer.unref()
  }

  private notify(state: RhObservationStoreHealth['state'], code: RhObservationStoreHealth['error_code']): void {
    this.state = state
    this.errorCode = code
    try {
      // A logger callback must not acquire the writer's or the Mission's liveness.
      const result: unknown = this.options.onHealth?.(this.health())
      if (result && typeof (result as PromiseLike<unknown>).then === 'function') void Promise.resolve(result).catch(() => {})
    } catch { /* independent health failure never affects research or the writer */ }
  }

  private addLoss(from: number, to: number, reason: string): void {
    this.dropped += to - from + 1
    const last = this.coverage.at(-1)
    if (last?.kind === 'known_loss' && last.reason === reason && last.to_sequence === from - 1) last.to_sequence = to
    else this.coverage.push({ kind: 'known_loss', reason, from_sequence: from, to_sequence: to })
    if (this.coverage.length > MAX_COVERAGE) {
      this.coverage.splice(0, this.coverage.length - MAX_COVERAGE + 1)
      this.coverage.unshift({ kind: 'unavailable', reason: 'older_loss_ledger_exceeded_bound', from_sequence: null, to_sequence: null })
    }
    this.dirty = true
  }

  private async initialize(): Promise<void> {
    if (this.initialized) return
    await requireDirectory(this.runtimeDir, false)
    const root = path.join(this.runtimeDir, 'observations')
    try { await fs.promises.mkdir(root, { mode: 0o700 }) } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
    }
    await requireDirectory(root)
    if (!this.createdDirectory) {
      await fs.promises.mkdir(this.directory, { mode: 0o700 })
      this.createdDirectory = true
    }
    await requireStoreDirectory(this.runtimeDir, this.options.incarnation)
    await this.publishManifest(this.manifest)
    await publishPrivateFile(root, 'current.json', `${JSON.stringify({ schema_version: RH_OBSERVATION_SCHEMA_VERSION, incarnation: this.options.incarnation })}\n`, true)
    this.initialized = true
  }

  private async publishManifest(manifest: RhObservationStoreManifest, onPublished?: () => void): Promise<void> {
    await requireStoreDirectory(this.runtimeDir, this.options.incarnation)
    await publishPrivateFile(this.directory, 'manifest.json', `${JSON.stringify(manifest)}\n`, true, onPublished)
  }

  private async requireDiskReserve(bytes: number): Promise<void> {
    const stat = await fs.promises.statfs(this.runtimeDir)
    if (stat.bavail * stat.bsize < this.limits.diskReserveBytes + bytes) throw new ObservationStoreError('disk_reserve')
  }

  private async writePending(): Promise<void> {
    // An indeterminate rename never authorizes deleting possibly committed bytes.
    // Resolve its actual cut before replacing the manifest or admitting a new batch.
    if (this.pendingPublication) {
      const visible = await readRhObservationManifest(this.runtimeDir, this.options.incarnation)
      if (visible.watermark.sequence >= this.pendingPublication.through) {
        this.manifest = visible
      } else {
        for (const sequence of this.pendingPublication.sequences) this.addLoss(sequence, sequence, 'write_failed')
        for (const file of this.uncommittedFiles) await unlinkPrivateFile(this.directory, file)
      }
      this.uncommittedFiles.clear()
      this.pendingPublication = null
    }
    const batch = this.queue.splice(0)
    const reserved = batch.reduce((sum, entry) => sum + entry.reservedBytes, 0)
    const through = this.seen
    this.dirty = false
    let committed = false
    let publicationAttempted = false
    try {
      await this.initialize()
      await this.requireDiskReserve(reserved)
      const segments = [...this.manifest.segments]
      let contentSequence = this.manifest.watermark.content_sequence
      let rows: RhObservation[] = []
      let rowBytes = 0
      let contentAllocation = 0
      const publishRows = async (): Promise<void> => {
        if (!rows.length) return
        segments.push(await this.publishSegment(rows, 0, contentAllocation))
        rows = []; rowBytes = 0; contentAllocation = 0
      }
      for (const entry of batch) {
        const handles: RhObservationContentHandle[] = []
        for (const content of entry.contents) {
          const bytes = Buffer.from(content.text, 'utf8')
          let start = 0
          do {
            let end = Math.min(bytes.length, start + this.limits.pageBytes)
            while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end -= 1
            if (end === start && bytes.length > start) throw new ObservationStoreError('write_failed')
            const chunk = bytes.subarray(start, end)
            const handle: RhObservationContentHandle = {
              incarnation: this.options.incarnation, content_id: content.content_id, field: content.field,
              revision: entry.observation.sequence, update_mode: content.update_mode,
              start_byte: content.start_byte + start, end_byte: content.start_byte + end,
              total_bytes: content.start_byte + bytes.length, complete: content.complete,
              encoding: 'utf-8', sha256: sha256(chunk),
            }
            const file = rhObservationContentFile(handle)
            this.uncommittedFiles.add(file)
            await publishPrivateFile(this.directory, file, chunk)
            handles.push(handle)
            contentAllocation += allocatedBytes(chunk.length)
            start = end
          } while (start < bytes.length)
          contentSequence = entry.observation.sequence
        }
        const row = { ...entry.observation, content: handles } as RhObservation
        rows.push(row)
        rowBytes += Buffer.byteLength(JSON.stringify(row), 'utf8') + 1
        if (rowBytes >= this.limits.segmentBytes) await publishRows()
      }
      await publishRows()
      const oldMetadata = await this.compactSegments(segments)
      const workers = this.projectWorkers(batch)
      const next: RhObservationStoreManifest = {
        ...this.manifest, segments, clean_shutdown: false, updated_at: new Date().toISOString(),
        source: { ...this.manifest.source, codex_version: this.observedCodexVersion },
        watermark: { ...this.manifest.watermark, sequence: through, content_sequence: contentSequence },
        coverage: [...this.manifest.coverage.filter((gap) => gap.kind === 'cursor_expired'),
          ...this.coverage.filter((gap) => (gap.from_sequence === null || gap.from_sequence <= through) && (gap.to_sequence === null || gap.to_sequence >= this.manifest.first_available_sequence))
            .map((gap) => ({ ...gap, to_sequence: gap.to_sequence === null ? null : Math.min(gap.to_sequence, through) }))],
        workers,
      }
      const retired = this.trimManifest(next, Math.max(0, this.limits.retainedBytes - this.manifestOverhead(next) - 2 * ALLOCATION_BYTES))
      publicationAttempted = true
      await this.publishManifest(next, () => {
        this.manifest = next
        committed = true
        this.uncommittedFiles.clear()
      })
      for (const file of oldMetadata) await unlinkPrivateFile(this.directory, file)
      for (const segment of retired) await this.removeSegment(this.directory, segment)
      await this.rotateOlderIncarnations()
      this.notify(this.coverage.length ? 'degraded' : 'healthy', null)
    } catch (error) {
      if (!committed && this.initialized) {
        // rename may have committed the manifest before a directory-sync failure.
        // Never delete bytes referenced by that now-visible committed cut.
        try {
          const visible = await readRhObservationManifest(this.runtimeDir, this.options.incarnation)
          if (visible.watermark.sequence === through && through > this.manifest.watermark.sequence) {
            this.manifest = visible
            committed = true
            this.uncommittedFiles.clear()
          }
        } catch {
          if (publicationAttempted) {
            this.pendingPublication = { through, sequences: batch.map((entry) => entry.observation.sequence) }
            this.dirty = true
          }
        }
      }
      if (!committed && !this.pendingPublication) {
        for (const entry of batch) this.addLoss(entry.observation.sequence, entry.observation.sequence,
          error instanceof ObservationStoreError ? error.code : 'write_failed')
        for (const file of this.uncommittedFiles) {
          try { await unlinkPrivateFile(this.directory, file) } catch { /* Leave uncertainty, never touch an unsafe path. */ }
        }
        this.uncommittedFiles.clear()
      }
      throw error
    } finally { this.queueBytes -= reserved }
  }

  private projectWorkers(batch: QueuedObservation[]): RhObservationWorker[] {
    const workers = new Map(this.manifest.workers.map((worker) => [worker.thread_id, { ...worker }]))
    let snapshotBytes = Buffer.byteLength(JSON.stringify([...workers.values()]), 'utf8')
    for (const { observation } of batch) {
      if (observation.kind !== 'work' || observation.phase !== 'processed' ||
        !['registered', 'status'].includes(observation.details.action ?? '')) continue
      const threadId = observation.details.child_thread_id
      if (!threadId) continue
      const existing = workers.get(threadId)
      if (!existing && observation.details.action !== 'registered') continue
      if (!existing && workers.size >= this.limits.maxWorkers) {
        if (!this.coverage.some((gap) => gap.reason === 'worker_snapshot_limit')) {
          this.coverage.push({ kind: 'unavailable', reason: 'worker_snapshot_limit', from_sequence: observation.sequence, to_sequence: null })
        }
        continue
      }
      const worker = {
        thread_id: threadId,
        parent_thread_id: observation.identity.parent_thread_id,
        root_thread_id: observation.identity.root_thread_id,
        status: observation.details.status ?? existing?.status ?? null,
        active_operation_id: observation.identity.operation_id,
      }
      const nextBytes = snapshotBytes + Buffer.byteLength(JSON.stringify(worker), 'utf8') + 1 - (existing ? Buffer.byteLength(JSON.stringify(existing), 'utf8') + 1 : 0)
      if (nextBytes > MAX_WORKER_SNAPSHOT_BYTES) {
        if (!this.coverage.some((gap) => gap.reason === 'worker_snapshot_byte_limit')) {
          this.coverage.push({ kind: 'unavailable', reason: 'worker_snapshot_byte_limit', from_sequence: observation.sequence, to_sequence: null })
        }
        continue
      }
      workers.set(threadId, worker)
      snapshotBytes = nextBytes
    }
    return [...workers.values()]
  }

  private async publishSegment(rows: RhObservation[], level: number, contentAllocation: number): Promise<RhObservationStoreSegment> {
    const bytes = rows.map((row) => JSON.stringify(row)).join('\n') + '\n'
    const first = rows[0].sequence
    const last = rows.at(-1)!.sequence
    const file = `segment.${first}.${last}.jsonl`
    this.uncommittedFiles.add(file)
    await publishPrivateFile(this.directory, file, bytes)
    const length = Buffer.byteLength(bytes, 'utf8')
    return { file, first_sequence: first, last_sequence: last, bytes: length, sha256: sha256(bytes),
      created_at: new Date().toISOString(), retained_bytes: allocatedBytes(length) + contentAllocation, level }
  }

  private async compactSegments(segments: RhObservationStoreSegment[]): Promise<string[]> {
    const retired: string[] = []
    for (;;) {
      let start = -1
      for (let index = 0; index + COMPACTION_FAN_IN <= segments.length; index += 1) {
        const group = segments.slice(index, index + COMPACTION_FAN_IN)
        if (group.every((segment) => segment.level === group[0].level && segment.bytes < this.limits.segmentBytes) &&
          group.reduce((sum, segment) => sum + segment.bytes, 0) <= 4 * 1024 * 1024) { start = index; break }
      }
      if (start < 0) break
      const group = segments.slice(start, start + COMPACTION_FAN_IN)
      const rows: RhObservation[] = []
      for (const segment of group) rows.push(...await readSegment(this.directory, segment))
      const contentBytes = group.reduce((sum, segment) => sum + segment.retained_bytes - allocatedBytes(segment.bytes), 0)
      const replacement = await this.publishSegment(rows, group[0].level + 1, contentBytes)
      replacement.created_at = group[0].created_at
      segments.splice(start, COMPACTION_FAN_IN, replacement)
      retired.push(...group.map((segment) => segment.file))
    }
    return retired
  }

  private trimManifest(manifest: RhObservationStoreManifest, byteBudget = this.limits.retainedBytes): RhObservationStoreSegment[] {
    let bytes = manifest.segments.reduce((sum, segment) => sum + segment.retained_bytes, 0)
    const retired: RhObservationStoreSegment[] = []
    const before = Date.now() - this.limits.retentionMilliseconds
    while (manifest.segments.length && (bytes > byteBudget || manifest.segments.length > this.limits.maxSegments || Date.parse(manifest.segments[0].created_at) < before)) {
      const segment = manifest.segments.shift()!
      retired.push(segment)
      bytes -= segment.retained_bytes
    }
    if (retired.length) {
      manifest.first_available_sequence = manifest.segments[0]?.first_sequence ?? manifest.watermark.sequence + 1
      manifest.coverage = manifest.coverage.filter((gap) => gap.kind !== 'cursor_expired' && (gap.to_sequence === null || gap.to_sequence >= manifest.first_available_sequence))
      manifest.coverage.unshift({ kind: 'cursor_expired', reason: 'diagnostic_retention', from_sequence: 1, to_sequence: manifest.first_available_sequence - 1 })
    }
    return retired
  }

  private manifestOverhead(manifest: RhObservationStoreManifest): number {
    return allocatedBytes(Buffer.byteLength(JSON.stringify(manifest), 'utf8') + 1) + ALLOCATION_BYTES
  }

  private async reclaimPriorResidue(directory: string, manifest: RhObservationStoreManifest): Promise<void> {
    const incarnation = manifest.watermark.incarnation
    if (this.inspectedPriorIncarnations.has(incarnation)) return
    const committed = new Set(['manifest.json'])
    for (const segment of manifest.segments) {
      committed.add(segment.file)
      for (const row of await readSegment(directory, segment)) {
        for (const handle of row.content) committed.add(rhObservationContentFile(handle))
      }
    }
    const residue: string[] = []
    for (const entry of await fs.promises.readdir(directory, { withFileTypes: true })) {
      if (committed.has(entry.name)) continue
      if (!SEGMENT_FILE.test(entry.name) && !CONTENT_FILE.test(entry.name) && !TEMP_FILE.test(entry.name)) continue
      if (!entry.isFile() || entry.isSymbolicLink()) throw new ObservationStoreError('unsafe_path')
      residue.push(entry.name)
    }
    if (!manifest.clean_shutdown && !manifest.coverage.some((gap) => gap.reason === 'prior_process_uncommitted_tail')) {
      manifest.coverage.push({ kind: 'uncertain_tail', reason: 'prior_process_uncommitted_tail',
        from_sequence: manifest.watermark.sequence + 1, to_sequence: null })
      await publishPrivateFile(directory, 'manifest.json', `${JSON.stringify(manifest)}\n`, true)
    }
    for (const name of residue) {
      if (TEMP_FILE.test(name)) {
        await requireDirectory(directory)
        const file = path.join(directory, name)
        const stat = await fs.promises.lstat(file)
        if (!stat.isFile() || stat.isSymbolicLink()) throw new ObservationStoreError('unsafe_path')
        await fs.promises.unlink(file)
      } else await unlinkPrivateFile(directory, name)
    }
    this.inspectedPriorIncarnations.add(incarnation)
  }

  private async removeSegment(directory: string, segment: RhObservationStoreSegment): Promise<void> {
    for (const row of await readSegment(directory, segment)) {
      for (const handle of row.content) await unlinkPrivateFile(directory, rhObservationContentFile(handle))
    }
    await unlinkPrivateFile(directory, segment.file)
  }

  private async rotateOlderIncarnations(): Promise<void> {
    const root = await requireStoreDirectory(this.runtimeDir)
    const retained: { directory: string; manifest: RhObservationStoreManifest }[] = []
    for (const entry of await fs.promises.readdir(root, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.isSymbolicLink() || !INCARNATION.test(entry.name) || entry.name === this.options.incarnation) continue
      try {
        const manifest = await readRhObservationManifest(this.runtimeDir, entry.name)
        if (manifest.selection.mission_id !== this.options.selection.mission_id) continue
        retained.push({ directory: path.join(root, entry.name), manifest })
      } catch { /* Unknown/uncommitted directories are never promoted or removed. */ }
    }
    retained.sort((left, right) => Date.parse(right.manifest.updated_at) - Date.parse(left.manifest.updated_at))
    let budget = Math.max(0, this.limits.retainedBytes - 2 * ALLOCATION_BYTES - this.manifestOverhead(this.manifest) -
      this.manifest.segments.reduce((sum, segment) => sum + segment.retained_bytes, 0))
    for (let index = 0; index < retained.length; index += 1) {
      const entry = retained[index]
      await this.reclaimPriorResidue(entry.directory, entry.manifest)
      const next = structuredClone(entry.manifest)
      const keepIndex = index < this.limits.maxIncarnations - 1 && budget >= this.manifestOverhead(next)
      const retired = this.trimManifest(next, keepIndex ? Math.max(0, budget - this.manifestOverhead(next)) : 0)
      budget = Math.max(0, budget - this.manifestOverhead(next) - next.segments.reduce((sum, segment) => sum + segment.retained_bytes, 0))
      if (retired.length) {
        await publishPrivateFile(entry.directory, 'manifest.json', `${JSON.stringify(next)}\n`, true)
        for (const segment of retired) await this.removeSegment(entry.directory, segment)
      }
      if (!keepIndex && !next.segments.length) {
        const names = await fs.promises.readdir(entry.directory)
        if (names.length === 1 && names[0] === 'manifest.json') {
          await unlinkPrivateFile(entry.directory, 'manifest.json')
          await fs.promises.rmdir(entry.directory)
          this.inspectedPriorIncarnations.delete(next.watermark.incarnation)
        }
      }
    }
  }
}
