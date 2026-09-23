import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  AGENT_COMMUNICATIONS_NOTIFICATION_OUTBOX_ENV,
  LocalAgentCommunicationsNotificationOutbox,
  clearAgentCommunicationsEnvironmentForChildren,
  createMissionNotification,
  readAgentCommunicationsNotificationConfig,
} from './agent-communications.js'

test('notification integration is dormant or safely disabled without affecting Host config', () => {
  assert.deepEqual(readAgentCommunicationsNotificationConfig({}), {
    config: null,
    warning: null,
  })
  assert.deepEqual(
    readAgentCommunicationsNotificationConfig({
      [AGENT_COMMUNICATIONS_NOTIFICATION_OUTBOX_ENV]: 'relative-outbox',
    }),
    { config: null, warning: 'outbox_path_invalid' },
  )
  const outboxDirectory = path.resolve('agent-communications-outbox')
  assert.deepEqual(
    readAgentCommunicationsNotificationConfig({
      [AGENT_COMMUNICATIONS_NOTIFICATION_OUTBOX_ENV]: outboxDirectory,
    }),
    { config: { outboxDirectory }, warning: null },
  )
})

test('one readable operation identity replaces duplicate hash and incident identities', () => {
  const operationId =
    'rh-mission:candidate-a1:mission.rh.public.1:complete-rh:1'
  const notification = createMissionNotification(
    operationId,
    'critical',
    'RH Candidate A1 requires review',
    'Review candidate complete-rh@1.',
  )
  assert.deepEqual(notification, {
    operationId,
    reason: 'critical',
    title: 'RH Candidate A1 requires review',
    summary: 'Review candidate complete-rh@1.',
  })
  assert.equal('incidentId' in notification, false)
  assert.throws(
    () => createMissionNotification('unsafe/path', 'critical', 'Title', 'Summary'),
    /operation identity is invalid/,
  )
})

test('all communication route and credential names are removed before App Server spawn', () => {
  const env: NodeJS.ProcessEnv = {
    [AGENT_COMMUNICATIONS_NOTIFICATION_OUTBOX_ENV]: '/var/lib/outbox',
    AGENT_COMMUNICATIONS_GATEWAY_TOKEN: 'must-not-reach-child',
    AGENT_COMMUNICATIONS_GATEWAY_TOKEN_FILE: '/run/credentials/full-token',
    AGENT_COMMUNICATIONS_NOTIFICATION_TOKEN: 'must-not-reach-child',
    AGENT_COMMUNICATIONS_NOTIFICATION_TOKEN_FILE: '/run/credentials/notification-token',
    AGENT_COMMUNICATIONS_PUSHOVER_APP_TOKEN_FILE: '/run/credentials/pushover-app-token',
    AGENT_COMMUNICATIONS_PUSHOVER_USER_KEY: 'must-not-reach-child',
    AGENT_COMMUNICATIONS_GRAPH_PRIVATE_KEY_PATH: '/run/credentials/graph-private-key.pem',
    AGENT_COMMUNICATIONS_HEALTHCHECKS_PING_URL_FILE: '/run/credentials/healthchecks-url',
    AGENT_COMMUNICATIONS_CLIENT_CONFIG_FILE: '/protected/client.env',
    AGENT_COMMUNICATIONS_GATEWAY_SSH_IDENTITY_FILE: '/protected/gateway-identity',
    AGENT_COMMUNICATIONS_FUTURE_SECRET_ROUTE: 'must-also-not-reach-child',
    RH_MISSION_HOST_MISSION_ID: 'mission.rh.public.1',
  }
  clearAgentCommunicationsEnvironmentForChildren(env)
  assert.deepEqual(env, {
    RH_MISSION_HOST_MISSION_ID: 'mission.rh.public.1',
  })
})

test('outbox admission is one atomic credentialless event without receipt clutter', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-notification-outbox-'))
  try {
    const sink = new LocalAgentCommunicationsNotificationOutbox({
      outboxDirectory: root,
    })
    const notification = createMissionNotification(
      'rh-mission:usage-suspended:mission.rh.public.1:epoch.7',
      'critical',
      'RH Mission usage suspended',
      'The retained Goal can resume when provider capacity is available.',
    )
    const admission = sink.admit(notification)
    assert.deepEqual(admission, {
      operationId: notification.operationId,
      disposition: 'admitted',
    })

    const files = await fs.promises.readdir(root)
    assert.equal(files.length, 1)
    assert.match(files[0] ?? '', /^[0-9a-f-]+\.json$/)
    const payload = JSON.parse(
      await fs.promises.readFile(path.join(root, files[0] ?? ''), 'utf8'),
    ) as Record<string, unknown>
    assert.deepEqual(payload, {
      schema_version: 'agent_communications.notification_outbox_event.v1',
      operation_id: notification.operationId,
      reason: 'critical',
      title: 'RH Mission usage suspended',
      summary: 'The retained Goal can resume when provider capacity is available.',
    })
    for (const forbidden of [
      'source_ref',
      'incident_id',
      'token',
      'url',
      'hash',
      'receipt',
    ]) {
      assert.equal(forbidden in payload, false)
    }
    assert.equal(files.some((file) => file.endsWith('.tmp')), false)
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('unsafe or absent outbox paths fail only the optional admission', async () => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-notification-path-'))
  const missing = path.join(root, 'missing')
  const notification = createMissionNotification(
    'rh-mission:strategy-pause:mission.rh:strategy.main:4',
    'needs_owner',
    'RH Mission paused for owner',
    'Review the retained checkpoint.',
  )
  try {
    assert.throws(
      () => new LocalAgentCommunicationsNotificationOutbox({
        outboxDirectory: missing,
      }).admit(notification),
      /ENOENT/,
    )
    if (process.platform !== 'win32') {
      const link = path.join(root, 'outbox-link')
      await fs.promises.symlink(root, link)
      assert.throws(
        () => new LocalAgentCommunicationsNotificationOutbox({
          outboxDirectory: link,
        }).admit(notification),
        /one real directory/,
      )
    }
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})

test('admission checks directory read write and traverse before creating a file', async (context) => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'rh-notification-access-'))
  const notification = createMissionNotification(
    'rh-mission:host-failure:mission.rh:epoch.9',
    'critical',
    'RH Mission needs attention',
    'The Host retained its last mathematical checkpoint.',
  )
  try {
    context.mock.method(fs, 'accessSync', (candidate: fs.PathLike, mode?: number) => {
      assert.equal(candidate, root)
      assert.equal(
        mode,
        fs.constants.R_OK | fs.constants.W_OK | fs.constants.X_OK,
      )
      const error = new Error('directory cannot be opened for durable fsync') as NodeJS.ErrnoException
      error.code = 'EACCES'
      throw error
    })

    assert.throws(
      () => new LocalAgentCommunicationsNotificationOutbox({
        outboxDirectory: root,
      }).admit(notification),
      /directory cannot be opened for durable fsync/,
    )
    assert.deepEqual(await fs.promises.readdir(root), [])
  } finally {
    await fs.promises.rm(root, { recursive: true, force: true })
  }
})
