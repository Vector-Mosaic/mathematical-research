/** Credential scrubbing for readable project observations, separate from the
 * deliberately content-free normal-log projection in redaction.ts.
 *
 * This recognizes explicit credential fields/assignments, authorization and
 * cookie headers, PEM private-key blocks, and the token signatures below. It
 * is not a detector for arbitrary/encoded secrets. Existing tracked-secret
 * scanning supplies the assignment, sk-/GitHub/AWS and PEM signature precedent;
 * that Python repository scanner is not an observation/runtime dependency.
 */
export type ObservationRedactionReason =
  | 'credential_field' | 'credential_assignment' | 'authorization'
  | 'cookie' | 'private_key' | 'provider_token'

export interface ObservationTextScrubResult {
  text: string
  /** Counts/reasons describe newly redacted spans in this call, not lifetime totals. */
  redactionCount: number
  reasons: ObservationRedactionReason[]
  /** Unpublished UTF-16 code units retained for prefix/terminator lookahead. */
  pendingChars: number
}

export interface ObservationValueScrubResult {
  value: unknown
  redactionCount: number
  reasons: ObservationRedactionReason[]
}

const FIELD_NAMES = [
  'api_key', 'api-key', 'apikey', 'access_key', 'access-key', 'accesskey',
  'access_token', 'access-token', 'accesstoken',
  'refresh_token', 'refresh-token', 'refreshtoken',
  'client_secret', 'client-secret', 'clientsecret',
  'private_key', 'private-key', 'privatekey',
  'secret', 'token', 'password', 'passwd',
]
const HEADER_NAMES = ['authorization', 'proxy-authorization', 'cookie', 'set-cookie']
const FIELD_PREFIXES = [...FIELD_NAMES, ...HEADER_NAMES]
const STRUCTURED_FIELDS = new Set(FIELD_PREFIXES.map(normalizeField))
const PRIVATE_KEY_TYPES = ['', 'RSA ', 'EC ', 'OPENSSH ', 'DSA ', 'ENCRYPTED ']
const PRIVATE_MARKERS = PRIVATE_KEY_TYPES.map((type) => ({
  begin: `-----BEGIN ${type}PRIVATE KEY-----`,
  end: `-----END ${type}PRIVATE KEY-----`,
}))
const PROVIDERS = [
  { prefix: 'sk-', minimum: 20, alphabet: /^[A-Za-z0-9_-]$/ },
  ...['ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_'].map((prefix) => ({
    prefix, minimum: 20, alphabet: /^[A-Za-z0-9_]$/,
  })),
  { prefix: 'AKIA', minimum: 16, alphabet: /^[A-Z0-9]$/ },
  { prefix: 'ASIA', minimum: 16, alphabet: /^[A-Z0-9]$/ },
  { prefix: 'AIza', minimum: 35, alphabet: /^[A-Za-z0-9_-]$/ },
  ...['xoxb-', 'xoxp-', 'xoxa-', 'xoxr-', 'xoxs-'].map((prefix) => ({
    prefix, minimum: 10, alphabet: /^[A-Za-z0-9-]$/,
  })),
]
const BEARER_ALPHABET = /^[A-Za-z0-9._~+/=-]$/
const HORIZONTAL_SPACE = /^[ \t]$/
const SCALAR_END = /^[\s,;&|}\])"']$/

function normalizeField(value: string): string {
  return value.toLowerCase().replace(/[-_]/g, '')
}

function reasonForField(value: string): ObservationRedactionReason {
  if (value.includes('authorization')) return 'authorization'
  if (value.includes('cookie')) return 'cookie'
  return 'credential_assignment'
}

function isCredentialField(value: string): boolean {
  if (STRUCTURED_FIELDS.has(normalizeField(value))) return true
  // Environment prefixes are permitted, but token_count/session_id and similar
  // operational fields are not credential values.
  return FIELD_NAMES.some((name) => value.toLowerCase().endsWith(`_${name}`))
}

function marker(reason: ObservationRedactionReason): string {
  return `[REDACTED:${reason}]`
}

const REDACTION_MARKERS = ([
  'credential_field', 'credential_assignment', 'authorization', 'cookie', 'private_key', 'provider_token',
] satisfies ObservationRedactionReason[]).map(marker)

type Mode =
  | { kind: 'normal' }
  | { kind: 'separator'; reason: ObservationRedactionReason; keyQuote: string | null; closedQuote: boolean }
  | { kind: 'value'; reason: ObservationRedactionReason; multiline: boolean; rawHeader: boolean; outerQuote: string | null }
  | { kind: 'quoted'; reason: ObservationRedactionReason; quote: string; escaped: boolean; started: boolean }
  | { kind: 'scalar'; reason: ObservationRedactionReason; started: boolean; escaped: boolean }
  | { kind: 'header'; reason: ObservationRedactionReason; started: boolean; outerQuote: string | null; escaped: boolean }
  | { kind: 'token'; alphabet: RegExp }
  | { kind: 'private'; end: string; suffix: string }

/** One instance belongs to one native content field's append stream. Never
 * reuse it across items, or finish on a transport disconnect. finish() means
 * that this source field has ended; snapshots use scrubObservationText instead.
 *
 * Prefix lookahead is < 128 UTF-16 units, independent of line/value length.
 * Recognized arbitrarily long values are discarded incrementally. Ordinary
 * newline-free output is emitted without waiting for a command/turn to end.
 * An unterminated quoted credential/private-key block remains suppressed until
 * source completion. Inputs are decoded text; the transport owns UTF-8 decoding.
 */
export class ObservationTextSanitizer {
  private pending = ''
  private previous = ''
  private mode: Mode = { kind: 'normal' }
  private finished = false

  get pendingChars(): number {
    return this.pending.length + (this.mode.kind === 'private' ? this.mode.suffix.length : 0)
  }

  push(chunk: string): ObservationTextScrubResult {
    if (this.finished) throw new Error('observation_sanitizer_finished')
    const result = this.result()
    // Feed bounded slices internally even if the source supplied a huge chunk.
    for (const character of chunk) {
      this.pending += character
      this.drain(false, result)
    }
    result.pendingChars = this.pendingChars
    return result
  }

  finish(): ObservationTextScrubResult {
    const result = this.result()
    if (this.finished) return result
    this.drain(true, result)
    this.pending = ''
    this.mode = { kind: 'normal' }
    this.finished = true
    return result
  }

  private result(): ObservationTextScrubResult {
    return { text: '', redactionCount: 0, reasons: [], pendingChars: 0 }
  }

  private redact(reason: ObservationRedactionReason, result: ObservationTextScrubResult): void {
    result.text += marker(reason)
    result.redactionCount += 1
    if (!result.reasons.includes(reason)) result.reasons.push(reason)
  }

  private take(count: number): string {
    const consumed = this.pending.slice(0, count)
    this.pending = this.pending.slice(count)
    if (consumed) this.previous = consumed.at(-1)!
    return consumed
  }

  private drain(final: boolean, result: ObservationTextScrubResult): void {
    while (this.pending) {
      const character = this.pending[0]
      const mode = this.mode
      if ((mode.kind === 'quoted' || mode.kind === 'scalar' || mode.kind === 'header') && !mode.started) {
        const existing = REDACTION_MARKERS.find((value) => this.pending.startsWith(value))
        if (existing) {
          result.text += this.take(existing.length)
          mode.started = true
          continue
        }
        if (!final && REDACTION_MARKERS.some((value) => value.startsWith(this.pending))) return
      }
      if (mode.kind === 'normal') {
        if (this.start(final, result)) continue
        if (!final && this.couldStart()) return
        // Do not publish half of a surrogate pair for a caller to UTF-8 encode.
        if (!final && this.pending.length === 1 && /[\uD800-\uDBFF]/.test(character)) return
        const length = /^[\uD800-\uDBFF][\uDC00-\uDFFF]/.test(this.pending) ? 2 : 1
        result.text += this.take(length)
      } else if (mode.kind === 'separator') {
        if (HORIZONTAL_SPACE.test(character) || (mode.keyQuote && mode.closedQuote && /\s/.test(character))) result.text += this.take(1)
        else if (!mode.closedQuote && /["']/.test(character)) {
          mode.closedQuote = true
          result.text += this.take(1)
        } else if (character === ':' || character === '=') {
          result.text += this.take(1)
          this.mode = {
            kind: 'value', reason: mode.reason,
            multiline: Boolean(mode.keyQuote && mode.closedQuote && character === ':'),
            rawHeader: !mode.closedQuote && (mode.reason === 'authorization' || mode.reason === 'cookie'),
            outerQuote: mode.closedQuote ? null : mode.keyQuote,
          }
        } else this.mode = { kind: 'normal' }
      } else if (mode.kind === 'value') {
        if (HORIZONTAL_SPACE.test(character) || (mode.multiline && /\s/.test(character))) {
          result.text += this.take(1)
        } else if (mode.rawHeader && !/[\r\n]/.test(character)) {
          this.mode = { kind: 'header', reason: mode.reason, started: false, outerQuote: mode.outerQuote, escaped: false }
        } else if (character === '"' || character === "'") {
          result.text += this.take(1)
          this.mode = { kind: 'quoted', reason: mode.reason, quote: character, escaped: false, started: false }
        } else if (SCALAR_END.test(character)) this.mode = { kind: 'normal' }
        else this.mode = { kind: 'scalar', reason: mode.reason, started: false, escaped: false }
      } else if (mode.kind === 'quoted') {
        if (!mode.escaped && character === mode.quote) {
          result.text += this.take(1)
          this.mode = { kind: 'scalar', reason: mode.reason, started: mode.started, escaped: false }
        } else {
          if (!mode.started) { this.redact(mode.reason, result); mode.started = true }
          mode.escaped = !mode.escaped && character === '\\'
          this.take(1)
        }
      } else if (mode.kind === 'header') {
        if (!mode.outerQuote && /[\r\n]/.test(character)) this.mode = { kind: 'normal' }
        else if (!mode.escaped && character === mode.outerQuote) {
          result.text += this.take(1)
          this.mode = { kind: 'scalar', reason: mode.reason, started: mode.started, escaped: false }
        }
        else {
          if (!mode.started) { this.redact(mode.reason, result); mode.started = true }
          mode.escaped = mode.outerQuote === '"' && !mode.escaped && (character === '\\' || character === '`')
          this.take(1)
        }
      } else if (mode.kind === 'scalar') {
        if (!mode.escaped && (character === '"' || character === "'")) {
          result.text += this.take(1)
          this.mode = { kind: 'quoted', reason: mode.reason, quote: character, escaped: false, started: mode.started }
        } else if (!mode.escaped && SCALAR_END.test(character)) this.mode = { kind: 'normal' }
        else {
          if (!mode.started) { this.redact(mode.reason, result); mode.started = true }
          mode.escaped = !mode.escaped && character === '\\'
          this.take(1)
        }
      } else if (mode.kind === 'token') {
        if (mode.alphabet.test(character)) this.take(1)
        else this.mode = { kind: 'normal' }
      } else if (mode.kind === 'private') {
        mode.suffix += this.take(1)
        if (mode.suffix.endsWith(mode.end)) this.mode = { kind: 'normal' }
        else {
          while (mode.suffix && !mode.end.startsWith(mode.suffix)) mode.suffix = mode.suffix.slice(1)
        }
      }
    }
  }

  private atBoundary(): boolean {
    return !/[A-Za-z0-9]/.test(this.previous)
  }

  private start(final: boolean, result: ObservationTextScrubResult): boolean {
    for (const key of PRIVATE_MARKERS) {
      if (this.pending.startsWith(key.begin)) {
        this.take(key.begin.length)
        this.redact('private_key', result)
        this.mode = { kind: 'private', end: key.end, suffix: '' }
        return true
      }
    }
    if (!this.atBoundary()) return false
    for (const provider of PROVIDERS) {
      const required = provider.prefix.length + provider.minimum
      if (this.pending.startsWith(provider.prefix) && this.pending.length >= required
        && [...this.pending.slice(provider.prefix.length, required)].every((char) => provider.alphabet.test(char))) {
        this.take(required)
        this.redact('provider_token', result)
        this.mode = { kind: 'token', alphabet: provider.alphabet }
        return true
      }
    }
    const lower = this.pending.toLowerCase()
    // A bare Bearer value needs a token-shaped value of at least eight chars;
    // Authorization headers/fields above also cover short credentials.
    const bearer = /^bearer[ \t]+([A-Za-z0-9._~+/=-]{8})/i.exec(this.pending)
    if (bearer) {
      result.text += this.take(bearer[0].length - 8)
      this.take(8)
      this.redact('authorization', result)
      this.mode = { kind: 'token', alphabet: BEARER_ALPHABET }
      return true
    }
    for (const field of FIELD_PREFIXES) {
      if (!lower.startsWith(field)) continue
      const next = this.pending[field.length]
      if ((!next && final) || (next && /^[ \t:="']$/.test(next))) {
        const keyQuote = this.previous === '"' || this.previous === "'" ? this.previous : null
        result.text += this.take(field.length)
        this.mode = { kind: 'separator', reason: reasonForField(field), keyQuote, closedQuote: false }
        return true
      }
    }
    return false
  }

  private couldStart(): boolean {
    if (PRIVATE_MARKERS.some(({ begin }) => begin.startsWith(this.pending))) return true
    if (!this.atBoundary()) return false
    const lower = this.pending.toLowerCase()
    if (FIELD_PREFIXES.some((field) => field.startsWith(lower))) return true
    for (const { prefix, minimum, alphabet } of PROVIDERS) {
      if (prefix.startsWith(this.pending)) return true
      if (this.pending.startsWith(prefix) && this.pending.length < prefix.length + minimum
        && [...this.pending.slice(prefix.length)].every((char) => alphabet.test(char))) return true
    }
    if ('bearer'.startsWith(lower)) return true
    // Horizontal spacing is bounded for the standalone signature; actual
    // Authorization fields handle arbitrary spacing in their value state.
    return /^bearer[ \t]{1,16}[A-Za-z0-9._~+/=-]{0,7}$/i.test(this.pending)
  }
}

export function scrubObservationText(text: string): ObservationTextScrubResult {
  const sanitizer = new ObservationTextSanitizer()
  const first = sanitizer.push(text)
  const last = sanitizer.finish()
  return {
    text: first.text + last.text,
    redactionCount: first.redactionCount + last.redactionCount,
    reasons: [...new Set([...first.reasons, ...last.reasons])],
    pendingChars: 0,
  }
}

/** For parsed JSON-shaped tool arguments/results. Ordinary project-content keys
 * are retained; only credential-valued keys are replaced in their entirety. */
export function scrubObservationValue(value: unknown): ObservationValueScrubResult {
  const reasons = new Set<ObservationRedactionReason>()
  let redactionCount = 0
  function visit(entry: unknown): unknown {
    if (typeof entry === 'string') {
      const scrubbed = scrubObservationText(entry)
      redactionCount += scrubbed.redactionCount
      for (const reason of scrubbed.reasons) reasons.add(reason)
      return scrubbed.text
    }
    if (Array.isArray(entry)) return entry.map(visit)
    if (entry && typeof entry === 'object') {
      return Object.fromEntries(Object.entries(entry).map(([key, nested]) => {
        if (isCredentialField(key) && nested !== null && nested !== undefined && nested !== ''
          && !(typeof nested === 'string' && REDACTION_MARKERS.includes(nested))) {
          redactionCount += 1
          reasons.add('credential_field')
          return [key, marker('credential_field')]
        }
        return [key, visit(nested)]
      }))
    }
    return entry
  }
  const result = visit(value)
  return { value: result, redactionCount, reasons: [...reasons] }
}
