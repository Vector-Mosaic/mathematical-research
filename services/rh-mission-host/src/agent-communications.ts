import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

export const AGENT_COMMUNICATIONS_NOTIFICATION_OUTBOX_ENV =
  'RH_MISSION_AGENT_COMMUNICATIONS_OUTBOX_DIRECTORY'

export interface AgentCommunicationsNotificationConfig {
  outboxDirectory: string
}

export interface AgentCommunicationsNotificationConfigResult {
  config: AgentCommunicationsNotificationConfig | null
  warning: 'outbox_path_invalid' | null
}

export type AgentCommunicationsReason = 'completed' | 'needs_owner' | 'critical'

export interface AgentCommunicationsNotification {
  operationId: string
  reason: AgentCommunicationsReason
  title: string
  summary: string
}

export interface AgentCommunicationsNotificationAdmission {
  operationId: string
  disposition: 'admitted'
}

export interface MissionNotificationSink {
  admit(
    notification: AgentCommunicationsNotification,
  ): AgentCommunicationsNotificationAdmission
}

export function readAgentCommunicationsNotificationConfig(
  env: NodeJS.ProcessEnv,
): AgentCommunicationsNotificationConfigResult {
  const raw = env[AGENT_COMMUNICATIONS_NOTIFICATION_OUTBOX_ENV]
  if (raw === undefined || raw.trim() === '') {
    return { config: null, warning: null }
  }
  const candidate = raw.trim()
  if (candidate !== raw || !path.isAbsolute(candidate)) {
    return { config: null, warning: 'outbox_path_invalid' }
  }
  return {
    config: { outboxDirectory: path.resolve(candidate) },
    warning: null,
  }
}

export function clearAgentCommunicationsEnvironmentForChildren(
  env: NodeJS.ProcessEnv,
): void {
  for (const key of Object.keys(env)) {
    if (
      key === AGENT_COMMUNICATIONS_NOTIFICATION_OUTBOX_ENV ||
      key.startsWith('AGENT_COMMUNICATIONS_')
    ) {
      delete env[key]
    }
  }
}

function requireOperationId(value: string): string {
  if (value.trim() === '' || /[\\/\0\r\n]/.test(value)) {
    throw new Error('Agent Communications notification operation identity is invalid')
  }
  return value
}

export function createMissionNotification(
  operationId: string,
  reason: AgentCommunicationsReason,
  title: string,
  summary: string,
): AgentCommunicationsNotification {
  return {
    operationId: requireOperationId(operationId),
    reason,
    title,
    summary,
  }
}

function requireOutboxDirectory(directory: string): void {
  const metadata = fs.lstatSync(directory)
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error('Agent Communications notification outbox must be one real directory')
  }
  // Durable admission requires opening and fsyncing the directory after the
  // atomic rename. Check that full requirement before creating a temporary
  // file so a write+execute-only deployment cannot admit an event and then
  // misleadingly report that admission failed.
  fs.accessSync(
    directory,
    fs.constants.R_OK | fs.constants.W_OK | fs.constants.X_OK,
  )
}

function fsyncDirectory(directory: string): void {
  if (process.platform === 'win32') {
    return
  }
  const descriptor = fs.openSync(directory, fs.constants.O_RDONLY)
  try {
    fs.fsyncSync(descriptor)
  } finally {
    fs.closeSync(descriptor)
  }
}

/**
 * Atomically hands one closed attention event to the separately isolated
 * Agent Communications service. This class owns no bearer or provider call.
 */
export class LocalAgentCommunicationsNotificationOutbox
implements MissionNotificationSink {
  constructor(private readonly config: AgentCommunicationsNotificationConfig) {}

  admit(
    notification: AgentCommunicationsNotification,
  ): AgentCommunicationsNotificationAdmission {
    requireOperationId(notification.operationId)
    requireOutboxDirectory(this.config.outboxDirectory)
    const storageId = randomUUID()
    const temporary = path.join(this.config.outboxDirectory, `.${storageId}.tmp`)
    const target = path.join(this.config.outboxDirectory, `${storageId}.json`)
    const body = Buffer.from(`${JSON.stringify({
      schema_version: 'agent_communications.notification_outbox_event.v1',
      operation_id: notification.operationId,
      reason: notification.reason,
      title: notification.title,
      summary: notification.summary,
    })}\n`, 'utf8')
    let descriptor: number | null = null
    let promoted = false
    try {
      descriptor = fs.openSync(
        temporary,
        fs.constants.O_CREAT |
          fs.constants.O_EXCL |
          fs.constants.O_WRONLY |
          (fs.constants.O_NOFOLLOW ?? 0),
        0o600,
      )
      fs.writeFileSync(descriptor, body)
      // Flush the final access mode with the content; a crash after a later
      // path-based chmod could otherwise retain a 0600 file the gateway
      // cannot read.
      fs.fchmodSync(descriptor, 0o640)
      fs.fsyncSync(descriptor)
      fs.closeSync(descriptor)
      descriptor = null
      // The production directory is setgid to the Agent Communications
      // ingress group. Both identities can open the directory for the fsync
      // required by crash-durable admission; no provider credential crosses
      // this boundary.
      fs.renameSync(temporary, target)
      promoted = true
      fsyncDirectory(this.config.outboxDirectory)
      return {
        operationId: notification.operationId,
        disposition: 'admitted',
      }
    } finally {
      if (descriptor !== null) {
        fs.closeSync(descriptor)
      }
      if (!promoted) {
        try {
          fs.unlinkSync(temporary)
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
            throw error
          }
        }
      }
    }
  }
}
