import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ObservationTextSanitizer,
  scrubObservationText,
  scrubObservationValue,
  type ObservationRedactionReason,
} from './observation-content.js'

function stream(chunks: string[]) {
  const sanitizer = new ObservationTextSanitizer()
  let text = ''
  let count = 0
  const reasons = new Set<ObservationRedactionReason>()
  for (const chunk of chunks) {
    const result = sanitizer.push(chunk)
    text += result.text
    count += result.redactionCount
    for (const reason of result.reasons) reasons.add(reason)
    assert.ok(result.pendingChars < 128, `prefix buffer grew to ${result.pendingChars}`)
  }
  const result = sanitizer.finish()
  text += result.text
  count += result.redactionCount
  for (const reason of result.reasons) reasons.add(reason)
  return { text, count, reasons: [...reasons] }
}

const assignment = '[REDACTED:credential_assignment]'
const auth = '[REDACTED:authorization]'
const provider = '[REDACTED:provider_token]'
const privateKey = '[REDACTED:private_key]'
const secretValue = 'fixture_' + 'a9'.repeat(16)
const fixtures: [string, string][] = [
  [`export RH_API_KEY=${secretValue}\nworker ready\n`, `export RH_API_KEY=${assignment}\nworker ready\n`],
  [`curl -H 'Authorization: Bearer ${secretValue}' /health`, `curl -H 'Authorization: ${auth}' /health`],
  [`Authorization: Basic ${secretValue}\r\nready`, `Authorization: ${auth}\r\nready`],
  [`Bearer ${secretValue} done`, `Bearer ${auth} done`],
  [`Cookie: a=${secretValue}; b=${secretValue}\nnext`, 'Cookie: [REDACTED:cookie]\nnext'],
  [`Cookie: sid="${secretValue}"; other=${secretValue}\nnext`, 'Cookie: [REDACTED:cookie]\nnext'],
  [`curl -H 'Cookie: sid="${secretValue}"; other=${secretValue}' /health`, "curl -H 'Cookie: [REDACTED:cookie]' /health"],
  [`curl -H "Cookie: sid=\\"${secretValue}\\"; other=${secretValue}" /health`, 'curl -H "Cookie: [REDACTED:cookie]" /health'],
  [`curl -H "Authorization: Bearer ${secretValue}\\\ncontinued" /health`, `curl -H "Authorization: ${auth}" /health`],
  [`Set-Cookie: sid=${secretValue}; Secure; HttpOnly\r\nnext`, 'Set-Cookie: [REDACTED:cookie]\r\nnext'],
  [`{"clientSecret":"${secretValue}","text":"still here"}`, `{"clientSecret":"${assignment}","text":"still here"}`],
  [`{"refresh_token":\n  "${secretValue}", "output":"ok"}`, `{"refresh_token":\n  "${assignment}", "output":"ok"}`],
  [`{"api_key"\r\n :\n "${secretValue}", "output":"ok"}`, `{"api_key"\r\n :\n "${assignment}", "output":"ok"}`],
  [`password="${secretValue}\\"more\\\\stuff" done`, `password="${assignment}" done`],
  [`PASSWORD=${secretValue}\\ escaped\\\ncontinuation next`, `PASSWORD=${assignment} next`],
  [`PASSWORD='${secretValue}'"more"tail next`, `PASSWORD='${assignment}'"" next`],
  [`?access_token=${secretValue}&page=2`, `?access_token=${assignment}&page=2`],
  [`--api-key=${secretValue} --verbose`, `--api-key=${assignment} --verbose`],
  [`OPENAI_API_KEY\t=\t'${secretValue}'\ncomplete`, `OPENAI_API_KEY\t=\t'${assignment}'\ncomplete`],
  [`result: sk-${'T'.repeat(32)}. done`, `result: ${provider}. done`],
  [`result: ghp_${'U'.repeat(32)}; done`, `result: ${provider}; done`],
  [`result: github_pat_${'V'.repeat(32)}! done`, `result: ${provider}! done`],
  [`result: AKIA${'A'.repeat(16)}. done`, `result: ${provider}. done`],
  [`result: AIza${'B'.repeat(35)}. done`, `result: ${provider}. done`],
  [`result: xoxb-${'7'.repeat(20)}. done`, `result: ${provider}. done`],
  [`before\n-----BEGIN RSA PRIVATE KEY-----\n${secretValue}\n-----END RSA PRIVATE KEY-----\nafter`, `before\n${privateKey}\nafter`],
  [`-----BEGIN ENCRYPTED PRIVATE KEY-----${secretValue}-----END ENCRYPTED PRIVATE KEY-----tail`, `${privateKey}tail`],
]

test('one scanner protects credential spans at every native delta boundary and preserves surrounding output', () => {
  for (const [index, [source, expected]] of fixtures.entries()) {
    const whole = scrubObservationText(source)
    assert.equal(whole.text, expected)
    assert.equal(whole.redactionCount, 1)
    for (let cut = 0; cut <= source.length; cut += 1) {
      const split = stream([source.slice(0, cut), source.slice(cut)])
      assert.equal(split.text, expected, `split ${cut} in fixture ${index}`)
      assert.equal(split.count, whole.redactionCount)
      assert.deepEqual(split.reasons, whole.reasons)
    }
    assert.equal(stream([...source]).text, expected)
  }
})

test('ordinary project content, count/id fields and short signature-like words remain readable', () => {
  const ordinary = [
    'RH worker: checked zeta(s), 40 iterations; exit 0. Δ=0.25 🧮\n',
    'token_count=4096 token_budget: 9000 access_token_count=3 session_id=abc\n',
    'sk-variable is a short name. ghp_example is documentation. Bearer of news.\n',
    'password is a field name; private_key_count=0; tokenization=ok\n',
    'diff --git a/file.ts b/file.ts\n+ const text = "ordinary result"\n',
    'digest=' + 'ab'.repeat(1024),
  ].join('')
  assert.equal(scrubObservationText(ordinary).text, ordinary)
  assert.equal(stream(ordinary.split('')).text, ordinary)
  assert.equal(scrubObservationText(ordinary).redactionCount, 0)
})

test('an unfinished credential never leaks through a reconnect boundary and does not wait for turn completion', () => {
  const sanitizer = new ObservationTextSanitizer()
  assert.equal(sanitizer.push('build phase finished\nAuthorization: Bea').text, `build phase finished\nAuthorization: ${auth}`)
  assert.equal(sanitizer.push('rer ' + secretValue.slice(0, 5)).text, '')
  // A consumer disconnect is not a finish(): later native deltas continue the same state.
  assert.equal(sanitizer.push(secretValue.slice(5) + '\nprogress 1\n').text, '\nprogress 1\n')
  assert.equal(sanitizer.finish().text, '')
})

test('huge newline-free ordinary output streams while huge secret values use bounded retained state', () => {
  const ordinary = new ObservationTextSanitizer()
  const secret = new ObservationTextSanitizer()
  const pem = new ObservationTextSanitizer()
  assert.equal(secret.push('API_KEY="').text, 'API_KEY="')
  assert.equal(pem.push('-----BEGIN PRIVATE KEY-----').text, privateKey)
  const chunk = '0123456789'.repeat(1024)
  let ordinaryLength = 0
  let secretText = ''
  for (let index = 0; index < 128; index += 1) {
    const result = ordinary.push(chunk)
    ordinaryLength += result.text.length
    secretText += secret.push(chunk).text
    assert.equal(pem.push(chunk).text, '')
    assert.ok(ordinary.pendingChars < 128)
    assert.ok(secret.pendingChars < 128)
    assert.ok(pem.pendingChars < 128)
    assert.ok(ordinaryLength >= (index + 1) * chunk.length - 128)
  }
  ordinaryLength += ordinary.finish().text.length
  assert.equal(ordinaryLength, chunk.length * 128)
  assert.equal(secretText, assignment)
  assert.equal(secret.push('"; printf done\n').text, '"; printf done\n')
  assert.equal(pem.push('-----END PRIVATE KEY-----done\n').text, 'done\n')
})

test('empty env values and scalar delimiters restore output without swallowing the next line', () => {
  const value = 'API_KEY=\nnext line\nTOKEN=\r\nnext line\nPASSWORD=\'\' done\n'
  assert.equal(scrubObservationText(value).text, value)
  assert.equal(stream(value.split('')).text, value)
  assert.equal(scrubObservationText(`token=${secretValue}; echo next`).text, `token=${assignment}; echo next`)
  const spaced = `PREFIX_API_KEY${' '.repeat(1024)}=${' '.repeat(1024)}${secretValue}\nnext`
  assert.equal(scrubObservationText(spaced).text, `PREFIX_API_KEY${' '.repeat(1024)}=${' '.repeat(1024)}${assignment}\nnext`)
})

test('surrogate halves split by native input are emitted together for UTF-8 encoding', () => {
  const sanitizer = new ObservationTextSanitizer()
  const first = sanitizer.push('ready \ud83e')
  assert.equal(first.text, 'ready ')
  const second = sanitizer.push('\uddee\n')
  assert.equal(Buffer.from(second.text).toString('utf8'), '🧮\n')
  assert.equal(sanitizer.finish().text, '')
})

test('source completion handles incomplete recognized credentials and finished streams cannot be reused', () => {
  assert.equal(scrubObservationText(`password="${secretValue}`).text, `password="${assignment}`)
  assert.equal(scrubObservationText(`-----BEGIN PRIVATE KEY-----${secretValue}`).text, privateKey)
  assert.equal(scrubObservationText('sk-short').text, 'sk-short')
  assert.equal(scrubObservationText('Authorization:').text, 'Authorization:')
  const sanitizer = new ObservationTextSanitizer()
  sanitizer.finish()
  assert.equal(sanitizer.finish().text, '')
  assert.throws(() => sanitizer.push('another item'), /observation_sanitizer_finished/)
})

test('existing markers survive snapshot/structured projection scrubbing without corrupting content', () => {
  for (const [, expected] of fixtures) {
    assert.equal(scrubObservationText(expected).text, expected)
    assert.equal(scrubObservationText(expected).redactionCount, 0)
  }
  const first = scrubObservationValue({ password: secretValue, output: `API_KEY=${secretValue}` })
  const second = scrubObservationValue(first.value)
  assert.deepEqual(second.value, first.value)
  assert.equal(second.redactionCount, 0)
  // An existing display marker never causes a following value to bypass suppression.
  assert.equal(scrubObservationText(`password=${assignment}${secretValue}\n`).text, `password=${assignment}\n`)
})

test('structured tool data scrubs credential fields while keeping actual project content', () => {
  const input = {
    command: 'pnpm test', content: 'project content', prompt: 'investigate RH',
    text: 'the output', transcript: ['worker commentary'], token_count: 30,
    nested: {
      apiKey: secretValue,
      AWS_SECRET_ACCESS_KEY: secretValue,
      authorization: { scheme: 'Bearer', credential: secretValue },
      session_id: 'research-session', access_token_count: 3,
      result: `command output sk-${'Z'.repeat(30)}\nexit 0`,
    },
  }
  const result = scrubObservationValue(input)
  assert.deepEqual(result.value, {
    ...input,
    nested: {
      ...input.nested,
      apiKey: '[REDACTED:credential_field]',
      AWS_SECRET_ACCESS_KEY: '[REDACTED:credential_field]',
      authorization: '[REDACTED:credential_field]',
      result: `command output ${provider}\nexit 0`,
    },
  })
  assert.equal(result.redactionCount, 4)
  assert.deepEqual(result.reasons, ['credential_field', 'provider_token'])
  assert.equal(input.nested.apiKey, secretValue)
})
