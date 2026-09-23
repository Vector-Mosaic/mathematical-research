import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  CheckpointCommittedSourceHandoffBridgeError,
  CheckpointDispositionUnverifiedBridgeError,
  MissionBridgeCancelledError,
  MissionBridgeError,
  PythonMissionBridge,
  type BridgeCommandRunner,
  type JsonObject,
  type MissionAuthorizationCut,
  type MissionBridgeConfig,
} from './mission-bridge.js'

const TEST_AUTHORIZATION_CUT: MissionAuthorizationCut = {
  project_commit: 7,
  current_root_digest: '1'.repeat(64),
  transition_head_digest: '2'.repeat(64),
  canonical_authority_digest: '3'.repeat(64),
}

const SOURCE_REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../..',
)

function resolvePythonExecutable(): string {
  const completed = spawnSync(
    process.platform === 'linux' ? '/usr/bin/python3' : 'python',
    ['-c', 'import sys; print(sys.executable)'],
    { cwd: SOURCE_REPO_ROOT, encoding: 'utf8', windowsHide: true },
  )
  assert.equal(completed.status, 0, completed.stderr)
  const executable = completed.stdout.trim()
  assert.equal(path.isAbsolute(executable), true)
  return executable
}

function initializeRealMissionWorkspace(
  pythonPath: string,
  workspaceRoot: string,
  projectId: string,
  missionId: string,
  releaseSha: string,
): void {
  const code = [
    'import sys',
    'from pathlib import Path',
    'repo=Path(sys.argv[1])',
    'workspace=Path(sys.argv[2])',
    'project_id=sys.argv[3]',
    'mission_id=sys.argv[4]',
    'release_sha=sys.argv[5]',
    "research_root=repo/'packages'/'research-core'",
    "test_root=research_root/'tests'",
    "wc_packages=repo/'packages'/'research-attempt-adapter'",
    'sys.path[:0]=[str(research_root),str(test_root),str(wc_packages)]',
    'from research_core.canonical_snapshot import load_canonical_snapshot',
    'from research_core.mission_interface import MissionInterface',
    'from test_mission_interface_direct import _direct_genesis_fixture',
    'loaded=load_canonical_snapshot(repo, source_commit=release_sha)',
    "assert loaded.ok and loaded.value is not None, loaded.failure",
    'MissionInterface.initialize_from_owner(',
    ' workspace, project_id=project_id, mission_id=mission_id,',
    ' canonical_snapshot=loaded.value,',
    ' genesis_seed=_direct_genesis_fixture(project_id=project_id, mission_id=mission_id),',
    " owner_command_id='mission-bridge-cross-language.bootstrap',",
    ')',
  ].join('\n')
  const completed = spawnSync(
    pythonPath,
    ['-c', code, SOURCE_REPO_ROOT, workspaceRoot, projectId, missionId, releaseSha],
    {
      cwd: SOURCE_REPO_ROOT,
      encoding: 'utf8',
      windowsHide: true,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' },
    },
  )
  assert.equal(completed.status, 0, completed.stderr)
}

function ownerEnvelope(
  data: Record<string, unknown>,
  warnings: readonly unknown[] = [],
): Record<string, unknown> {
  return {
    schema_version: 'tool_result.v1',
    tool_id: 'tool.mathematical_research.rh_mission',
    status: 'ok',
    verb: 'host-bridge',
    summary: 'ok',
    data,
    warnings: [...warnings],
    errors: [],
  }
}

function writeResponse(args: readonly string[], value: unknown): void {
  const responsePath = args[args.indexOf('--response') + 1]
  assert.equal(typeof responsePath, 'string')
  fs.writeFileSync(responsePath!, JSON.stringify(value), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
}

function bridgeFixture(prefix: string): { root: string; config: MissionBridgeConfig } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  const runtimeDir = path.join(root, 'runtime')
  const repoRoot = path.join(root, 'release')
  const missionWorkspaceRoot = path.join(root, 'workspace')
  fs.mkdirSync(runtimeDir)
  fs.mkdirSync(repoRoot)
  fs.mkdirSync(missionWorkspaceRoot)
  return {
    root,
    config: {
      repoRoot,
      releaseSha: 'a'.repeat(40),
      missionWorkspaceRoot,
      runtimeDir,
      codexHome: path.join(root, 'codex-home'),
      pythonPath: path.join(root, 'python'),
      codexCliPath: path.join(root, 'codex'),
      missionScriptPath: path.join(repoRoot, 'scripts', 'rh_mission.py'),
      projectId: 'project.rh',
      missionId: 'mission.rh',
      pollIntervalMs: 2000,
      permissionProfileId: 'rh-host-boundary.1',
      trustedMcpServerIds: [],
      trustedAppIds: [],
    },
  }
}

test('Python bridge reconciles only exact stale bridge files before its first invocation', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-stale-files-')
  const staleRequestPath = path.join(
    fixture.config.runtimeDir,
    'mission-bridge-request.41.11111111-1111-4111-8111-111111111111.json',
  )
  const staleResponsePath = path.join(
    fixture.config.runtimeDir,
    'mission-bridge-response.42.22222222-2222-4222-8222-222222222222.json',
  )
  const unrelatedPaths = [
    path.join(fixture.config.runtimeDir, 'mission-host-state.json'),
    path.join(
      fixture.config.runtimeDir,
      'mission-bridge-request.43.33333333-3333-4333-8333-333333333333.json.backup',
    ),
    path.join(
      fixture.config.runtimeDir,
      'mission-bridge-input.44.44444444-4444-4444-8444-444444444444.json',
    ),
  ]
  fs.writeFileSync(staleRequestPath, 'full-server-grant')
  fs.writeFileSync(staleResponsePath, 'raw-untrusted-history')
  for (const unrelatedPath of unrelatedPaths) {
    fs.writeFileSync(unrelatedPath, 'preserve')
  }
  const linkedTarget = path.join(fixture.root, 'linked-runtime-target')
  const linkedBridgePath = path.join(
    fixture.config.runtimeDir,
    'mission-bridge-response.45.55555555-5555-4555-8555-555555555555.json',
  )
  fs.mkdirSync(linkedTarget)
  fs.writeFileSync(path.join(linkedTarget, 'sentinel.txt'), 'preserve')
  fs.symlinkSync(
    linkedTarget,
    linkedBridgePath,
    process.platform === 'win32' ? 'junction' : 'dir',
  )

  let invocationCount = 0
  let firstRequestPath: string | null = null
  let markFirstStarted!: () => void
  const firstStarted = new Promise<void>((resolve) => {
    markFirstStarted = resolve
  })
  let markSecondStarted!: () => void
  const secondStarted = new Promise<void>((resolve) => {
    markSecondStarted = resolve
  })
  let releaseFirst!: () => void
  const firstMayFinish = new Promise<void>((resolve) => {
    releaseFirst = resolve
  })
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    invocationCount += 1
    const requestPath = args[args.indexOf('--request') + 1]
    assert.equal(typeof requestPath, 'string')
    assert.equal(fs.existsSync(staleRequestPath), false)
    assert.equal(fs.existsSync(staleResponsePath), false)
    for (const unrelatedPath of unrelatedPaths) {
      assert.equal(fs.readFileSync(unrelatedPath, 'utf8'), 'preserve')
    }
    assert.equal(fs.lstatSync(linkedBridgePath).isSymbolicLink(), true)
    assert.equal(fs.readFileSync(path.join(linkedTarget, 'sentinel.txt'), 'utf8'), 'preserve')
    if (invocationCount === 1) {
      firstRequestPath = requestPath!
      markFirstStarted()
      await firstMayFinish
    } else {
      markSecondStarted()
      assert.equal(firstRequestPath === null ? false : fs.existsSync(firstRequestPath), true)
    }
    writeResponse(args, ownerEnvelope({ accepted: true }))
    return {
      exitCode: 0,
      terminationSignal: null,
      stdout: '',
      stderr: '',
      stderrTruncated: false,
    }
  })

  const first = bridge.reconstruct()
  await firstStarted
  const second = bridge.authorizeExecutiveEpoch(TEST_AUTHORIZATION_CUT)
  await secondStarted
  releaseFirst()
  await Promise.all([first, second])

  assert.equal(invocationCount, 2)
  assert.equal(firstRequestPath === null ? false : fs.existsSync(firstRequestPath), false)
  assert.equal(fs.existsSync(staleRequestPath), false)
  assert.equal(fs.existsSync(staleResponsePath), false)
  for (const unrelatedPath of unrelatedPaths) {
    assert.equal(fs.readFileSync(unrelatedPath, 'utf8'), 'preserve')
  }
  assert.equal(fs.lstatSync(linkedBridgePath).isSymbolicLink(), true)
  assert.equal(fs.readFileSync(path.join(linkedTarget, 'sentinel.txt'), 'utf8'), 'preserve')
})

test('Python bridge cleans a partially created request when its request write rejects', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-partial-request-')
  let failAfterCreate = true
  let createdRequestPath: string | null = null
  let commandRuns = 0
  const bridge = new PythonMissionBridge(
    fixture.config,
    async (_executable, args) => {
      commandRuns += 1
      writeResponse(args, ownerEnvelope({ accepted: true }))
      return {
        exitCode: 0,
        terminationSignal: null,
        stdout: '',
        stderr: '',
        stderrTruncated: false,
      }
    },
    async (filePath, encoded) => {
      await fs.promises.writeFile(filePath, encoded, {
        encoding: 'utf8',
        flag: 'wx',
        mode: 0o600,
      })
      if (failAfterCreate) {
        failAfterCreate = false
        createdRequestPath = filePath
        assert.equal(fs.existsSync(filePath), true)
        throw Object.assign(new Error('simulated partial request write failure'), { code: 'EIO' })
      }
    },
  )

  await assert.rejects(
    () => bridge.reconstruct(),
    /simulated partial request write failure/,
  )
  assert.notEqual(createdRequestPath, null)
  assert.equal(createdRequestPath === null ? true : fs.existsSync(createdRequestPath), false)
  assert.deepEqual(
    fs.readdirSync(fixture.config.runtimeDir).filter((name) => name.startsWith('mission-bridge-')),
    [],
  )
  assert.equal(commandRuns, 0)

  assert.deepEqual(await bridge.reconstruct(), { accepted: true })
  assert.equal(commandRuns, 1)
  assert.deepEqual(
    fs.readdirSync(fixture.config.runtimeDir).filter((name) => name.startsWith('mission-bridge-')),
    [],
  )
})

test('Python bridge invokes only host-bridge through an exact shell-free JSON-file command', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-mission-bridge-'))
  const runtimeDir = path.join(root, 'runtime')
  fs.mkdirSync(runtimeDir)
  const repoRoot = path.join(root, 'release')
  const workspaceRoot = path.join(root, 'workspace')
  fs.mkdirSync(repoRoot)
  fs.mkdirSync(workspaceRoot)
  const pythonPath = path.join(root, 'python')
  const codexCliPath = path.join(root, 'codex')
  const codexHome = path.join(root, 'codex-home')
  const missionScriptPath = path.join(repoRoot, 'scripts', 'rh_mission.py')
  let capturedRequest: unknown

  const runner: BridgeCommandRunner = async (executable, args, options) => {
    assert.equal(executable, pythonPath)
    assert.equal(options.shell, false)
    assert.equal(options.cwd, repoRoot)
    assert.deepEqual(options.env, {
      PATH: '/usr/bin:/bin',
      PYTHONUTF8: '1',
      PYTHONDONTWRITEBYTECODE: '1',
    })
    assert.equal('CODEX_HOME' in options.env, false)
    assert.equal('HOME' in options.env, false)
    assert.equal('timeout' in options, false)
    assert.equal('maxBuffer' in options, false)
    assert.deepEqual(args.slice(0, 2), [missionScriptPath, 'host-bridge'])
    assert.equal(args.includes('usage'), false)
    assert.equal(args.includes('capabilities'), false)
    const requestPath = args[args.indexOf('--request') + 1]
    capturedRequest = JSON.parse(fs.readFileSync(requestPath, 'utf8'))
    writeResponse(args, ownerEnvelope({ accepted: true }))
    return {
      exitCode: 0,
      terminationSignal: null,
      stdout: '',
      stderr: '',
      stderrTruncated: false,
    }
  }

  const bridge = new PythonMissionBridge(
    {
      repoRoot,
      releaseSha: 'a'.repeat(40),
      missionWorkspaceRoot: workspaceRoot,
      runtimeDir,
      codexHome,
      pythonPath,
      codexCliPath,
      missionScriptPath,
      projectId: 'project.rh',
      missionId: 'mission.rh',
      pollIntervalMs: 2000,
      permissionProfileId: 'rh-host-boundary.1',
      trustedMcpServerIds: ['mcp.readonly'],
      trustedAppIds: ['app.readonly'],
    },
    runner,
  )
  const request = {
    schema_version: 'mathematical_research.mission_semantic_request.v1',
    operation: 'orient',
    input: {},
  }
  const result = await bridge.executeSemanticOperation(request, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
  })

  assert.deepEqual(result, { accepted: true })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'execute_semantic_operation',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
      },
    },
  })

  const cursorKey = 'c'.repeat(64)
  await bridge.executeSemanticOperation(request, { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    undefined, { cursor_mac_key: cursorKey })
  assert.equal(((capturedRequest as unknown as { payload: { root_query_context: { cursor_mac_key: string } } }).payload.root_query_context).cursor_mac_key, cursorKey)
  assert.equal('root_query_context' in request, false)
  assert.throws(() => bridge.executeSemanticOperation(request, { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    undefined, { cursor_mac_key: 'SECRET_INVALID_KEY' }), /^Error: Private root query signing context is invalid$/)

  const grantRequest = {
    child_thread_id: 'thread.history',
    assignment_mode: 'historical_opportunity_scout',
    assignment: 'Search retained history for one overlooked composable route.',
    context: { id: 'context:history-scout', revision: 1 },
    source_families: ['branches', 'evidence'],
    raw_body_policy: 'metadata_only',
  }
  capturedRequest = undefined
  await bridge.issueHistoricalReadGrant(grantRequest, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    childThreadId: 'thread.history',
    parentThreadId: 'thread.root',
    depth: 1,
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'issue_historical_read_grant',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: grantRequest,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        childThreadId: 'thread.history',
        parentThreadId: 'thread.root',
        depth: 1,
      },
    },
  })

  const candidateA1GrantRequest = {
    child_thread_id: 'thread.a1-review',
    assignment: 'Independently reconstruct the exact frozen complete-target claim.',
    context: { id: 'context:a1-review', revision: 1 },
    candidate_ref: {
      id: 'candidate:complete-rh',
      revision: 2,
      payload_sha256: 'a'.repeat(64),
    },
  }
  capturedRequest = undefined
  await bridge.issueCandidateA1ReviewGrant(candidateA1GrantRequest, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    childThreadId: 'thread.a1-review',
    parentThreadId: 'thread.root',
    depth: 1,
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'issue_candidate_a1_review_grant',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: candidateA1GrantRequest,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        childThreadId: 'thread.a1-review',
        parentThreadId: 'thread.root',
        depth: 1,
      },
    },
  })

  const grant = {
    grant_id: 'grant.history.1',
    assignment_id: 'assignment.history.1',
  }
  capturedRequest = undefined
  await bridge.executeDelegatedRead(request, grant, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    callerThreadId: 'thread.history',
    parentThreadId: 'thread.root',
    depth: 1,
    turnId: 'turn.history.1',
    grantId: 'grant.history.1',
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'execute_delegated_read',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request,
      grant,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        callerThreadId: 'thread.history',
        parentThreadId: 'thread.root',
        depth: 1,
        turnId: 'turn.history.1',
        grantId: 'grant.history.1',
      },
    },
  })

  const candidateA1Grant = {
    grant_id: 'grant.a1-review.1',
    assignment_id: 'assignment.a1-review.1',
  }
  const candidateA1ReviewRequest = { mode: 'retrieve' }
  capturedRequest = undefined
  await bridge.executeCandidateA1Review(candidateA1ReviewRequest, candidateA1Grant, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    callerThreadId: 'thread.a1-review',
    parentThreadId: 'thread.root',
    depth: 1,
    turnId: 'turn.a1-review.1',
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'execute_candidate_a1_review',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: candidateA1ReviewRequest,
      grant: candidateA1Grant,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        callerThreadId: 'thread.a1-review',
        parentThreadId: 'thread.root',
        depth: 1,
        turnId: 'turn.a1-review.1',
      },
    },
  })

  const admissionCaseRequest = {
    candidate_ref: {
      id: 'candidate:complete-rh',
      revision: 2,
      payload_sha256: 'a'.repeat(64),
    },
  }
  capturedRequest = undefined
  await bridge.openCompleteClaimAdmissionCase(admissionCaseRequest, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'open_complete_claim_admission_case',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: admissionCaseRequest,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
      },
    },
  })

  const admissionGrantRequest = {
    child_thread_id: 'thread.admission-reviewer',
    assignment: 'Independently reconstruct the exact frozen Admission Case.',
    context: { id: 'context:admission', revision: 1 },
    case_ref: {
      id: 'evidence:admission-case',
      revision: 1,
      payload_sha256: 'b'.repeat(64),
    },
  }
  capturedRequest = undefined
  await bridge.issueAdmissionReviewGrant(admissionGrantRequest, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    childThreadId: 'thread.admission-reviewer',
    parentThreadId: 'thread.root',
    depth: 1,
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'issue_admission_review_grant',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: admissionGrantRequest,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        childThreadId: 'thread.admission-reviewer',
        parentThreadId: 'thread.root',
        depth: 1,
      },
    },
  })

  const admissionDecisionGrantRequest = {
    ...admissionGrantRequest,
    child_thread_id: 'thread.admission-admitter',
    assignment: 'Make the role-disjoint exact Admission decision.',
  }
  capturedRequest = undefined
  await bridge.issueAdmissionDecisionGrant(admissionDecisionGrantRequest, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    childThreadId: 'thread.admission-admitter',
    parentThreadId: 'thread.root',
    depth: 1,
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'issue_admission_decision_grant',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: admissionDecisionGrantRequest,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        childThreadId: 'thread.admission-admitter',
        parentThreadId: 'thread.root',
        depth: 1,
      },
    },
  })

  const admissionGrant = {
    grant_id: 'grant.admission.1',
    assignment_id: 'assignment.admission.1',
  }
  const admissionReviewRequest = { mode: 'retrieve' }
  capturedRequest = undefined
  await bridge.executeAdmissionReview(admissionReviewRequest, admissionGrant, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    callerThreadId: 'thread.admission-reviewer',
    parentThreadId: 'thread.root',
    depth: 1,
    turnId: 'turn.admission-reviewer.1',
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'execute_admission_review',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: admissionReviewRequest,
      grant: admissionGrant,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        callerThreadId: 'thread.admission-reviewer',
        parentThreadId: 'thread.root',
        depth: 1,
        turnId: 'turn.admission-reviewer.1',
      },
    },
  })

  const admissionDecisionRequest = {
    mode: 'submit',
    disposition: 'authorize_exact_delta',
    decision_basis: 'The exact frozen claim and review qualify.',
  }
  capturedRequest = undefined
  await bridge.executeAdmissionDecision(admissionDecisionRequest, admissionGrant, {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    callerThreadId: 'thread.admission-admitter',
    parentThreadId: 'thread.root',
    depth: 1,
    turnId: 'turn.admission-admitter.1',
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'execute_admission_decision',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: admissionDecisionRequest,
      grant: admissionGrant,
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
        callerThreadId: 'thread.admission-admitter',
        parentThreadId: 'thread.root',
        depth: 1,
        turnId: 'turn.admission-admitter.1',
      },
    },
  })

  capturedRequest = undefined
  await bridge.executeFormalAttempt(
    {
      selected_bet_sha256: 'b'.repeat(64),
      correction_basis: null,
    },
    {
      rootThreadId: 'thread.root',
      executiveEpochId: 'epoch.root',
    },
  )
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'execute_formal_attempt',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      request: {
        selected_bet_sha256: 'b'.repeat(64),
        correction_basis: null,
      },
      binding: {
        rootThreadId: 'thread.root',
        executiveEpochId: 'epoch.root',
      },
      runtime: {
        release_root: repoRoot,
        release_commit: 'a'.repeat(40),
        state_root: path.join(runtimeDir, 'formal-attempt'),
        codex_executable: codexCliPath,
        codex_home: codexHome,
        outer_containment_id: 'rh-host-boundary.1',
        trusted_mcp_server_ids: ['mcp.readonly'],
        trusted_app_ids: ['app.readonly'],
        polling_cadence_seconds: 2,
      },
    },
  })

  capturedRequest = undefined
  await bridge.reconstruct()
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'reconstruct',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {},
  })

  capturedRequest = undefined
  await bridge.hostSnapshot()
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'host_snapshot',
    binding: { project_id: 'project.rh', mission_id: 'mission.rh' },
    payload: {},
  })

  capturedRequest = undefined
  await bridge.authorizeExecutiveEpoch(TEST_AUTHORIZATION_CUT)
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'authorize_executive_epoch',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: { expected_cut: TEST_AUTHORIZATION_CUT },
  })

  capturedRequest = undefined
  await bridge.bindExecutiveEpoch({
    executiveEpochId: 'epoch.successor',
    rootThreadId: 'thread.successor',
    workspaceRoot,
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'bind_executive_epoch',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      executiveEpochId: 'epoch.successor',
      rootThreadId: 'thread.successor',
      workspaceRoot,
    },
  })

  capturedRequest = undefined
  await bridge.recordDirectFailedExecutiveEpoch({
    executiveEpochId: 'epoch.successor',
    reconciliation: {
      stage: 'goal_runtime',
      failure_reason: 'operator_stop',
    },
  })
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'record_direct_failed_executive_epoch',
    binding: {
      project_id: 'project.rh',
      mission_id: 'mission.rh',
    },
    payload: {
      executiveEpochId: 'epoch.successor',
      reconciliation: {
        stage: 'goal_runtime',
        failure_reason: 'operator_stop',
      },
    },
  })
  assert.deepEqual(fs.readdirSync(runtimeDir), [])
})

test('Python bridge issues research reads through the exact direct-child grant action', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-research-grant-')
  const request = {
    child_thread_id: 'thread.research',
    assignment: 'Investigate one ordinary mathematical branch using its declared sources.',
    source_families: ['branches', 'evidence', 'missions', 'sessions'],
    raw_body_policy: 'metadata_only',
  }
  const binding = {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    childThreadId: 'thread.research',
    parentThreadId: 'thread.root',
    depth: 1,
  }
  const grant = {
    schema_version: 'mathematical_research.research_read_grant.v1',
    project_id: fixture.config.projectId,
    mission_id: fixture.config.missionId,
    executive_epoch_id: binding.executiveEpochId,
    root_thread_id: binding.rootThreadId,
    child_thread_id: binding.childThreadId,
    parent_thread_id: binding.parentThreadId,
    direct_depth: 1,
    grant_id: 'd'.repeat(64),
    assignment_id: 'assignment.research.1',
    assignment: request.assignment,
    source_families: request.source_families,
    raw_body_policy: request.raw_body_policy,
    allowed_modes: ['usage', 'retrieve'],
  }
  let capturedRequest: unknown
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    capturedRequest = JSON.parse(fs.readFileSync(args[args.indexOf('--request') + 1], 'utf8'))
    writeResponse(args, ownerEnvelope(grant))
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  assert.deepEqual(await bridge.issueResearchReadGrant(request, binding), grant)
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'issue_research_read_grant',
    binding: { project_id: fixture.config.projectId, mission_id: fixture.config.missionId },
    payload: { request, binding },
  })
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge keeps research query custody separate from the exact assignment and read request', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-research-read-')
  const request = {
    mode: 'retrieve',
    selection: {
      mode: 'search',
      purpose: 'Resolve the current branch obstruction.',
      query: 'saddle normalization',
      kinds: ['evidence'],
      page_size: 2,
      cursor: 'opaque.research.cursor',
    },
  }
  const grant = {
    schema_version: 'mathematical_research.research_read_grant.v1',
    project_id: fixture.config.projectId,
    mission_id: fixture.config.missionId,
    executive_epoch_id: 'epoch.root',
    root_thread_id: 'thread.root',
    child_thread_id: 'thread.research',
    parent_thread_id: 'thread.root',
    direct_depth: 1,
    grant_id: 'd'.repeat(64),
    assignment_id: 'assignment.research.1',
    assignment: 'Investigate the current branch obstruction.',
    source_families: ['evidence'],
    raw_body_policy: 'metadata_only',
    allowed_modes: ['usage', 'retrieve'],
  }
  const binding = {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    callerThreadId: 'thread.research',
    parentThreadId: 'thread.root',
    depth: 1,
    turnId: 'turn.research.2',
    grantId: grant.grant_id,
    assignmentId: grant.assignment_id,
  }
  const context = { cursor_mac_key: 'c'.repeat(64) }
  const result = {
    schema_version: 'mathematical_research.research_read_result.v1',
    grant_id: grant.grant_id,
    assignment_id: grant.assignment_id,
    mode: request.mode,
    project_commit_cut: 7,
    result: { items: [], next_cursor: null },
  }
  const originalRequest = structuredClone(request)
  const originalGrant = structuredClone(grant)
  let capturedRequest: unknown
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    capturedRequest = JSON.parse(fs.readFileSync(args[args.indexOf('--request') + 1], 'utf8'))
    writeResponse(args, ownerEnvelope(result))
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  assert.deepEqual(await bridge.executeResearchRead(request, grant, binding, undefined, context), result)
  assert.deepEqual(capturedRequest, {
    schema_version: 'mathematical_research.mission_host_bridge_request.v1',
    action: 'execute_research_read',
    binding: { project_id: fixture.config.projectId, mission_id: fixture.config.missionId },
    payload: { request, grant, binding, research_query_context: context },
  })
  assert.deepEqual(request, originalRequest)
  assert.deepEqual(grant, originalGrant)
  assert.equal(JSON.stringify(request).includes(context.cursor_mac_key), false)
  assert.equal(JSON.stringify(grant).includes(context.cursor_mac_key), false)
  assert.equal(JSON.stringify(result).includes(context.cursor_mac_key), false)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge preserves an owner rejection of the exact research assignment binding', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-research-assignment-')
  const ownerError = {
    code: 'research_read_grant_invalid',
    message: 'Research grant differs from its exact live caller/assignment binding.',
  }
  let capturedBinding: unknown
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    const input = JSON.parse(fs.readFileSync(args[args.indexOf('--request') + 1], 'utf8'))
    capturedBinding = input.payload.binding
    writeResponse(args, {
      ...ownerEnvelope({}),
      status: 'error',
      errors: [ownerError],
    })
    return { exitCode: 2, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })
  const binding = {
    rootThreadId: 'thread.root',
    executiveEpochId: 'epoch.root',
    callerThreadId: 'thread.research',
    parentThreadId: 'thread.root',
    depth: 1,
    turnId: 'turn.research.2',
    grantId: 'd'.repeat(64),
    assignmentId: 'assignment.research.other',
  }

  await assert.rejects(
    () => bridge.executeResearchRead(
      { mode: 'usage' },
      { grant_id: binding.grantId, assignment_id: 'assignment.research.1' },
      binding,
      undefined,
      { cursor_mac_key: 'c'.repeat(64) },
    ),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeError)
      assert.equal(error.exitCode, 2)
      assert.deepEqual(error.ownerErrors, [ownerError])
      return true
    },
  )
  assert.deepEqual(capturedBinding, binding)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge rejects a successful process with a non-owner result envelope', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-mission-bridge-invalid-'))
  fs.mkdirSync(path.join(root, 'runtime'))
  const bridge = new PythonMissionBridge(
    {
      repoRoot: root,
      releaseSha: 'a'.repeat(40),
      missionWorkspaceRoot: root,
      runtimeDir: path.join(root, 'runtime'),
      codexHome: path.join(root, 'codex-home'),
      pythonPath: path.join(root, 'python'),
      codexCliPath: path.join(root, 'codex'),
      missionScriptPath: path.join(root, 'rh_mission.py'),
      projectId: 'project.rh',
      missionId: 'mission.rh',
      pollIntervalMs: 2000,
      permissionProfileId: 'rh-host-boundary.1',
      trustedMcpServerIds: [],
      trustedAppIds: [],
    },
    async (_executable, args) => {
      writeResponse(args, { status: 'ok' })
      return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
    },
  )

  await assert.rejects(
    () => bridge.reconstruct(),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeError)
      assert.match(error.message, /wrong closed result envelope/)
      assert.equal(error.exitCode, 0)
      assert.equal(error.terminationSignal, null)
      return true
    },
  )
})

test('Python bridge delivers the exact bounded checkpoint-source warning without changing semantic data', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-source-warning-')
  const warning = {
    code: 'checkpoint_source_capture_failed_writer_reacquired',
    reason_code: 'checkpoint_source_capture_failed',
    database_bytes: 4096,
    handoff_elapsed_ms: 17,
  } as const
  const data = {
    schema_version: 'mathematical_research.mission_semantic_result.v1',
    operation: 'checkpoint',
    status: 'completed',
    result: {
      checkpoint_id: 'checkpoint.fixture',
      executive_epoch_id: 'epoch.root',
      state: 'checkpointed',
    },
    error: null,
  }
  let observed: unknown = null
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    writeResponse(args, ownerEnvelope(data, [warning]))
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  const result = await bridge.executeSemanticOperation(
    { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
    { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    undefined,
    undefined,
    (value) => {
      observed = structuredClone(value)
      return Promise.reject(new Error('injected non-authoritative observer failure'))
    },
  )

  assert.deepEqual(result, data)
  assert.deepEqual(observed, warning)
  assert.equal(Object.hasOwn(result, 'warnings'), false)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge delivers a writer-preserved skipped-snapshot checkpoint warning', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-source-skipped-')
  const warning = {
    code: 'checkpoint_source_snapshot_skipped_writer_preserved',
    reason_code: 'checkpoint_source_busy',
    database_bytes: null,
    handoff_elapsed_ms: 3,
  } as const
  const data = {
    schema_version: 'mathematical_research.mission_semantic_result.v1',
    operation: 'checkpoint',
    status: 'completed',
    result: {
      checkpoint_id: 'checkpoint.fixture.busy',
      executive_epoch_id: 'epoch.root',
      state: 'checkpointed',
    },
    error: null,
  }
  let observed: unknown = null
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    writeResponse(args, ownerEnvelope(data, [warning]))
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  const result = await bridge.executeSemanticOperation(
    { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
    { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    undefined,
    undefined,
    (value) => { observed = structuredClone(value) },
  )

  assert.deepEqual(result, data)
  assert.deepEqual(observed, warning)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge delivers a source-operation teardown warning after writer revalidation', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-source-teardown-')
  const warning = {
    code: 'checkpoint_source_operation_teardown_failed_writer_revalidated',
    reason_code: 'checkpoint_source_published',
    database_bytes: 4096,
    handoff_elapsed_ms: 9,
  } as const
  const data = {
    schema_version: 'mathematical_research.mission_semantic_result.v1',
    operation: 'checkpoint',
    status: 'completed',
    result: {
      checkpoint_id: 'checkpoint.fixture.teardown',
      executive_epoch_id: 'epoch.root',
      state: 'checkpointed',
    },
    error: null,
  }
  let observed: unknown = null
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    writeResponse(args, ownerEnvelope(data, [warning]))
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  const result = await bridge.executeSemanticOperation(
    { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
    { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    undefined,
    undefined,
    (value) => { observed = structuredClone(value) },
  )

  assert.deepEqual(result, data)
  assert.deepEqual(observed, warning)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge preserves a committed checkpoint while surfacing unsafe source handoff', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-source-unsafe-')
  const checkpoint = {
    checkpoint_id: 'checkpoint.fixture.unsafe',
    executive_epoch_id: 'epoch.root',
    state: 'checkpointed',
  }
  const ownerError = {
    code: 'checkpoint_committed_source_handoff_failed',
    reason_code: 'checkpoint_source_reacquisition_failed',
    message: 'research checkpoint committed, but its operational source handoff failed',
    checkpoint_completed: true,
    checkpoint_id: checkpoint.checkpoint_id,
    executive_epoch_id: checkpoint.executive_epoch_id,
    checkpoint_state: checkpoint.state,
    source_publication_state: 'published',
    continuation_safety: 'unsafe',
  }
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    writeResponse(args, {
      schema_version: 'tool_result.v1',
      tool_id: 'tool.mathematical_research.rh_mission',
      status: 'error',
      verb: 'host-bridge',
      summary: 'Research checkpoint committed, but its operational source handoff failed.',
      data: {
        canonical_effect: 'none',
        mathematical_effect: 'none',
        checkpoint_completed: true,
        checkpoint,
        source_publication_state: 'published',
        source_handoff_failure_code: 'checkpoint_source_reacquisition_failed',
        continuation_safety: 'unsafe',
      },
      warnings: [],
      errors: [ownerError],
    })
    return { exitCode: 1, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  await assert.rejects(
    () => bridge.executeSemanticOperation(
      { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
      { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    ),
    (error: unknown) => {
      assert.ok(error instanceof CheckpointCommittedSourceHandoffBridgeError)
      assert.deepEqual(error.checkpoint, checkpoint)
      assert.equal(error.continuationSafety, 'unsafe')
      assert.equal(error.sourcePublicationState, 'published')
      assert.equal(
        error.sourceHandoffFailureCode,
        'checkpoint_source_reacquisition_failed',
      )
      assert.deepEqual(error.ownerErrors, [ownerError])
      assert.match(error.message, /checkpoint committed/i)
      assert.match(error.message, /do not replay/i)
      return true
    },
  )
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge preserves a committed checkpoint while surfacing unverified writer state', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-source-unverified-')
  const checkpoint = {
    checkpoint_id: 'checkpoint.fixture.unverified',
    executive_epoch_id: 'epoch.root',
    state: 'checkpointed',
  }
  const ownerError = {
    code: 'checkpoint_committed_source_handoff_failed',
    reason_code: 'checkpoint_source_writer_state_unverified',
    message: 'research checkpoint committed, but writer state is unverified',
    checkpoint_completed: true,
    checkpoint_id: checkpoint.checkpoint_id,
    executive_epoch_id: checkpoint.executive_epoch_id,
    checkpoint_state: checkpoint.state,
    source_publication_state: 'unknown',
    continuation_safety: 'unverified',
  }
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    writeResponse(args, {
      schema_version: 'tool_result.v1',
      tool_id: 'tool.mathematical_research.rh_mission',
      status: 'error',
      verb: 'host-bridge',
      summary: 'Research checkpoint committed, but its operational source handoff failed.',
      data: {
        canonical_effect: 'none',
        mathematical_effect: 'none',
        checkpoint_completed: true,
        checkpoint,
        source_publication_state: 'unknown',
        source_handoff_failure_code: 'checkpoint_source_writer_state_unverified',
        continuation_safety: 'unverified',
      },
      warnings: [],
      errors: [ownerError],
    })
    return { exitCode: 1, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  await assert.rejects(
    () => bridge.executeSemanticOperation(
      { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
      { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    ),
    (error: unknown) => {
      assert.ok(error instanceof CheckpointCommittedSourceHandoffBridgeError)
      assert.deepEqual(error.checkpoint, checkpoint)
      assert.equal(error.continuationSafety, 'unverified')
      assert.equal(error.sourcePublicationState, 'unknown')
      assert.equal(
        error.sourceHandoffFailureCode,
        'checkpoint_source_writer_state_unverified',
      )
      return true
    },
  )
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge preserves a committed checkpoint with safe writer and source integrity failure', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-source-safe-')
  const checkpoint = {
    checkpoint_id: 'checkpoint.fixture.safe',
    executive_epoch_id: 'epoch.root',
    state: 'checkpointed',
  }
  const ownerError = {
    code: 'checkpoint_committed_source_handoff_failed',
    reason_code: 'checkpoint_source_path_unsafe',
    message: 'research checkpoint committed, but its source path is unsafe',
    checkpoint_completed: true,
    checkpoint_id: checkpoint.checkpoint_id,
    executive_epoch_id: checkpoint.executive_epoch_id,
    checkpoint_state: checkpoint.state,
    source_publication_state: 'published',
    continuation_safety: 'safe',
  }
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    writeResponse(args, {
      schema_version: 'tool_result.v1',
      tool_id: 'tool.mathematical_research.rh_mission',
      status: 'error',
      verb: 'host-bridge',
      summary: 'Research checkpoint committed, but its operational source handoff failed.',
      data: {
        canonical_effect: 'none',
        mathematical_effect: 'none',
        checkpoint_completed: true,
        checkpoint,
        source_publication_state: 'published',
        source_handoff_failure_code: 'checkpoint_source_path_unsafe',
        continuation_safety: 'safe',
      },
      warnings: [],
      errors: [ownerError],
    })
    return { exitCode: 1, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  await assert.rejects(
    () => bridge.executeSemanticOperation(
      { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
      { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    ),
    (error: unknown) => {
      assert.ok(error instanceof CheckpointCommittedSourceHandoffBridgeError)
      assert.deepEqual(error.checkpoint, checkpoint)
      assert.equal(error.continuationSafety, 'safe')
      assert.equal(error.sourcePublicationState, 'published')
      assert.equal(error.sourceHandoffFailureCode, 'checkpoint_source_path_unsafe')
      return true
    },
  )
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge lets an exact checkpoint finish across ordinary cancellation', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-cancel-')
  const controller = new AbortController()
  let started!: () => void
  const didStart = new Promise<void>((resolve) => { started = resolve })
  let finish!: () => void
  const mayFinish = new Promise<void>((resolve) => { finish = resolve })
  const data = {
    schema_version: 'mathematical_research.mission_semantic_result.v1',
    operation: 'checkpoint',
    status: 'completed',
    result: {
      checkpoint_id: 'checkpoint.fixture.cancel',
      executive_epoch_id: 'epoch.root',
      state: 'checkpointed',
    },
    error: null,
  }
  let commandSignalAborted = false
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args, options) => {
    options.signal.addEventListener('abort', () => { commandSignalAborted = true }, { once: true })
    started()
    await mayFinish
    writeResponse(args, ownerEnvelope(data))
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  const pending = bridge.executeSemanticOperation(
    { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
    { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
    controller.signal,
  )
  await didStart
  controller.abort('operator_stop')
  assert.equal(commandSignalAborted, false)
  finish()

  assert.deepEqual(await pending, data)
  assert.equal(commandSignalAborted, false)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge marks a settled malformed checkpoint result as disposition-unverified', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-unverified-output-')
  const controller = new AbortController()
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    writeResponse(args, ownerEnvelope({
      operation: 'checkpoint',
      status: 'completed',
      result: {
        checkpoint_id: 'checkpoint.fixture.unverified-output',
        executive_epoch_id: 'epoch.root',
        state: 'checkpointed',
      },
    }))
    controller.abort('operator_stop')
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  await assert.rejects(
    () => bridge.executeSemanticOperation(
      { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
      { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
      controller.signal,
    ),
    (error: unknown) => {
      assert.ok(error instanceof CheckpointDispositionUnverifiedBridgeError)
      assert.equal(error.rootThreadId, 'thread.root')
      assert.equal(error.executiveEpochId, 'epoch.root')
      assert.equal(error.processDiagnostic.responseError, 'checkpoint_disposition_unverified')
      return true
    },
  )
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge distinguishes an exact precommit rejection from a racing ordinary cancellation', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-precommit-')
  const controller = new AbortController()
  let invocation = 0
  const rejected = {
    schema_version: 'mathematical_research.mission_semantic_result.v1',
    operation: 'checkpoint',
    status: 'rejected',
    result: null,
    error: {
      code: 'mission_checkpoint_rejected',
      message: 'current work is not ready for a truthful checkpoint',
      property: 'checkpoint_domain_invariants',
      failure_scope: 'call',
      correction: 'continue_epoch_before_checkpoint',
    },
  }
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    invocation += 1
    writeResponse(args, ownerEnvelope(rejected))
    if (invocation === 2) controller.abort('operator_stop')
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })
  const request = {
    schema_version: 'mathematical_research.mission_semantic_request.v1',
    operation: 'checkpoint',
    input: {},
  }
  const binding = { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' }

  assert.deepEqual(await bridge.executeSemanticOperation(request, binding), rejected)
  await assert.rejects(
    () => bridge.executeSemanticOperation(request, binding, controller.signal),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeCancelledError)
      assert.equal(error.reason, 'operator_stop')
      return true
    },
  )
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge reconciles inconsistent or absent exact checkpoint process results', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-process-disposition-')
  let invocation = 0
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    invocation += 1
    if (invocation === 1) {
      writeResponse(args, ownerEnvelope({
        schema_version: 'mathematical_research.mission_semantic_result.v1',
        operation: 'checkpoint',
        status: 'completed',
        result: {
          checkpoint_id: 'checkpoint.fixture.inconsistent-process',
          executive_epoch_id: 'epoch.root',
          state: 'checkpointed',
        },
        error: null,
      }))
      return { exitCode: 1, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
    }
    throw new Error('simulated runner disposition loss')
  })
  const invoke = () => bridge.executeSemanticOperation(
    { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
    { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
  )

  for (const responseError of ['checkpoint_disposition_unverified', 'Error']) {
    await assert.rejects(invoke, (error: unknown) => {
      assert.ok(error instanceof CheckpointDispositionUnverifiedBridgeError)
      assert.equal(error.rootThreadId, 'thread.root')
      assert.equal(error.executiveEpochId, 'epoch.root')
      assert.equal(error.processDiagnostic.responseError, responseError)
      return true
    })
  }
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge keeps explicit force connected after an exact checkpoint starts', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-checkpoint-force-')
  let started!: () => void
  const didStart = new Promise<void>((resolve) => { started = resolve })
  let ordinarySignalAborted = false
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, _args, options) => {
    options.signal.addEventListener('abort', () => { ordinarySignalAborted = true }, { once: true })
    started()
    await new Promise<void>((resolve) => {
      options.forceSignal.addEventListener('abort', () => resolve(), { once: true })
    })
    return { exitCode: null, terminationSignal: 'SIGUSR2', stdout: '', stderr: '', stderrTruncated: false }
  })
  const pending = bridge.executeSemanticOperation(
    { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
    { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' },
  )

  await didStart
  const forcing = bridge.cancelOutstanding('explicit_force_stop')
  await assert.rejects(pending, (error: unknown) => {
    assert.ok(error instanceof MissionBridgeCancelledError)
    assert.equal(error.reason, 'explicit_force_stop')
    return true
  })
  await forcing
  assert.equal(ordinarySignalAborted, false)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge rejects warning drift and warnings outside a successful checkpoint', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-invalid-checkpoint-source-warning-')
  const valid = {
    code: 'checkpoint_source_capture_failed_writer_reacquired',
    reason_code: 'checkpoint_source_capture_failed',
    database_bytes: 4096,
    handoff_elapsed_ms: 17,
  }
  const validPreserved = {
    code: 'checkpoint_source_snapshot_skipped_writer_preserved',
    reason_code: 'checkpoint_source_busy',
    database_bytes: null,
    handoff_elapsed_ms: 3,
  }
  const checkpointData = {
    schema_version: 'mathematical_research.mission_semantic_result.v1',
    operation: 'checkpoint',
    status: 'completed',
    result: {
      checkpoint_id: 'checkpoint.fixture',
      executive_epoch_id: 'epoch.root',
      state: 'checkpointed',
    },
    error: null,
  }
  const responses = [
    ownerEnvelope({ reconstructed: true }, [valid]),
    ownerEnvelope(checkpointData, [{ ...valid, extra: true }]),
    ownerEnvelope(checkpointData, [{ ...valid, reason_code: 'not-bounded!' }]),
    ownerEnvelope(checkpointData, [{ ...valid, database_bytes: 0 }]),
    ownerEnvelope(checkpointData, [{ ...valid, database_bytes: 1.5 }]),
    ownerEnvelope(checkpointData, [{ ...valid, handoff_elapsed_ms: -1 }]),
    ownerEnvelope(checkpointData, [{ ...valid, handoff_elapsed_ms: Number.MAX_SAFE_INTEGER + 1 }]),
    ownerEnvelope(checkpointData, [valid, valid]),
    ownerEnvelope(checkpointData, [{ ...valid, database_bytes: null }]),
    ownerEnvelope(checkpointData, [{ ...validPreserved, reason_code: 'checkpoint_source_copy_failed' }]),
    ownerEnvelope({ operation: 'orient', status: 'completed', result: {} }, [valid]),
  ]
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args) => {
    const response = responses.shift()
    assert.ok(response)
    writeResponse(args, response)
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })
  const binding = { rootThreadId: 'thread.root', executiveEpochId: 'epoch.root' }
  await assert.rejects(() => bridge.reconstruct(), /wrong closed result envelope/)
  for (let index = 0; index < 9; index += 1) {
    await assert.rejects(
      () => bridge.executeSemanticOperation(
        { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'checkpoint', input: {} },
        binding,
      ),
      (error: unknown) => {
        assert.ok(error instanceof CheckpointDispositionUnverifiedBridgeError)
        assert.equal(error.processDiagnostic.responseError, 'invalid_owner_envelope')
        return true
      },
    )
  }
  await assert.rejects(
    () => bridge.executeSemanticOperation(
      { schema_version: 'mathematical_research.mission_semantic_request.v1', operation: 'orient', input: {} },
      binding,
    ),
    /wrong closed result envelope/,
  )
  assert.equal(responses.length, 0)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('real TypeScript-to-Python formal bridge accepts an install-shaped release and returns owner failures', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-cross-language-')
  const releaseSha = 'a'.repeat(40)
  const releaseRoot = path.join(fixture.root, releaseSha)
  const runtimeDir = path.join(fixture.root, 'real-runtime')
  const codexHome = path.join(fixture.root, 'real-codex-home')
  const missionWorkspaceRoot = path.join(fixture.root, 'real-workspace')
  const goalWorkspaceRoot = path.join(fixture.root, 'goal-workspace')
  const projectId = 'project.rh.bridge'
  const missionId = 'mission.rh.bridge'
  const pythonPath = resolvePythonExecutable()
  fs.mkdirSync(releaseRoot)
  fs.mkdirSync(runtimeDir)
  fs.mkdirSync(codexHome)
  fs.mkdirSync(goalWorkspaceRoot)
  const resolvedGoalWorkspaceRoot = fs.realpathSync.native(goalWorkspaceRoot)
  const modelCatalogRelative = path.join(
    'services',
    'rh-mission-host',
    'assets',
    'codex-model-catalog.0.153.4.json',
  )
  const modelCatalogPath = path.join(releaseRoot, modelCatalogRelative)
  fs.mkdirSync(path.dirname(modelCatalogPath), { recursive: true })
  fs.copyFileSync(path.join(SOURCE_REPO_ROOT, 'services/rh-mission-host/test-fixtures/codex-model-catalog.0.153.4.json'), modelCatalogPath)
  fs.writeFileSync(
    path.join(releaseRoot, '.mathematical-research-release.json'),
    JSON.stringify({
      schema_version: 'wc.rh_mission_host_release.v1',
      release_sha: releaseSha,
      bundle_sha256: '7'.repeat(64),
      bundle_size: 4096,
      node_version: process.version,
      pnpm_version: '10.16.1',
      codex_version: 'codex-cli 0.153.4',
      service_package: '@workstation-control/rh-mission-host',
    }),
  )
  const packageDirectory = path.join(releaseRoot, 'node_modules', 'package')
  fs.mkdirSync(packageDirectory, { recursive: true })
  fs.writeFileSync(path.join(packageDirectory, 'index.js'), 'export default true\n')
  fs.symlinkSync(
    packageDirectory,
    path.join(releaseRoot, 'node_modules', '.linked-package'),
    process.platform === 'win32' ? 'junction' : 'dir',
  )
  initializeRealMissionWorkspace(
    pythonPath,
    missionWorkspaceRoot,
    projectId,
    missionId,
    releaseSha,
  )
  const config: MissionBridgeConfig = {
    ...fixture.config,
    repoRoot: releaseRoot,
    releaseSha,
    missionWorkspaceRoot,
    runtimeDir,
    codexHome,
    pythonPath,
    codexCliPath: process.execPath,
    missionScriptPath: path.join(SOURCE_REPO_ROOT, 'scripts', 'rh_mission.py'),
    projectId,
    missionId,
  }
  const bridge = new PythonMissionBridge(config)
  const staleSnapshot = await bridge.hostSnapshot()
  assert.equal(staleSnapshot.schema_version, 'mathematical_research.mission_host_snapshot.v4')
  const initialState = staleSnapshot.current_state as JsonObject
  const initialOrientation = staleSnapshot.executive_orientation as JsonObject
  assert.equal(initialState.schema_version, 'mathematical_research.mission_host_current_state.v2')
  assert.equal(initialOrientation.schema_version, 'mathematical_research.executive_orientation.v3')
  assert.equal(Object.hasOwn(initialOrientation, 'strategy_ground'), false)
  assert.equal(((initialState.mission as JsonObject).summary as JsonObject).scientific_context_id, null)
  assert.equal((initialOrientation.mission as JsonObject).scientific_context_id, null)
  assert.deepEqual(initialOrientation.scientific_context, {
    state: 'unbound', binding: null, root_reference: null, purpose: null, question: null,
    known_omissions: [], restricted_uses: [], restrictions: [], independence_treatment: null,
    treatments: [], source_changes: [], unavailable: [],
  })
  const predecessor = await bridge.authorizeExecutiveEpoch(
    staleSnapshot.authorization_cut as MissionAuthorizationCut,
  )
  const predecessorEpochId = String(predecessor.executive_epoch_id)
  await bridge.bindExecutiveEpoch({
    executiveEpochId: predecessorEpochId,
    rootThreadId: 'thread.rh.bridge.predecessor',
    workspaceRoot: resolvedGoalWorkspaceRoot,
  })
  const semanticRequest = (operation: string, input: JsonObject = {}): JsonObject => ({
    schema_version: 'mathematical_research.mission_semantic_request.v1',
    operation,
    input,
  })
  const predecessorBinding = {
    executiveEpochId: predecessorEpochId,
    rootThreadId: 'thread.rh.bridge.predecessor',
  }
  const strategy = await bridge.executeSemanticOperation(
    semanticRequest('record_strategy', {
      mission_continuation: 'continue',
      integrated_comparison: (
        'Advance one legitimate current Strategy after the stale launch snapshot.'
      ),
      reconsideration_conditions: [
        { condition: 'Reconsider only if exact current owner facts change.' },
      ],
    }),
    predecessorBinding,
  )
  assert.equal(strategy.status, 'completed')
  const checkpoint = await bridge.executeSemanticOperation(
    semanticRequest('checkpoint'),
    predecessorBinding,
  )
  assert.equal(checkpoint.status, 'completed')
  await assert.rejects(
    () => bridge.authorizeExecutiveEpoch(
      staleSnapshot.authorization_cut as MissionAuthorizationCut,
    ),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeError)
      assert.deepEqual(
        error.ownerErrors.map((item) => (item as Record<string, unknown>).code),
        ['mission_host_store_cut_stale'],
      )
      return true
    },
  )
  const afterStale = await bridge.hostSnapshot()
  assert.deepEqual((afterStale.executive_orientation as JsonObject).scientific_context,
    initialOrientation.scientific_context)
  assert.equal((((afterStale.current_state as JsonObject).mission as JsonObject).summary as JsonObject)
    .scientific_context_id, null)
  assert.equal(
    ((afterStale.current_state as JsonObject).latest_executive_epoch as JsonObject).executive_epoch_id,
    predecessorEpochId,
  )
  assert.equal(
    ((afterStale.current_state as JsonObject).latest_executive_epoch as JsonObject).state,
    'checkpointed',
  )
  const snapshot = afterStale
  const authorized = await bridge.authorizeExecutiveEpoch(
    snapshot.authorization_cut as MissionAuthorizationCut,
  )
  const executiveEpochId = String(authorized.executive_epoch_id)
  const rootThreadId = 'thread.rh.bridge'
  await bridge.bindExecutiveEpoch({
    executiveEpochId,
    rootThreadId,
    workspaceRoot: resolvedGoalWorkspaceRoot,
  })
  const boundSnapshot = await bridge.hostSnapshot()
  assert.deepEqual(
    await bridge.validateSuspendedEpochCut({
      expectedCut: boundSnapshot.authorization_cut as MissionAuthorizationCut,
      executiveEpochId,
      rootThreadId,
      workspaceRoot: resolvedGoalWorkspaceRoot,
    }),
    {
      executive_epoch_id: executiveEpochId,
      root_thread_id: rootThreadId,
      state: 'bound',
      cut_validated: true,
    },
  )
  const resumedStrategy = await bridge.executeSemanticOperation(
    semanticRequest('record_strategy', {
      mission_continuation: 'continue',
      integrated_comparison: (
        'Move one legitimate current Strategy after the suspended launch cut.'
      ),
      reconsideration_conditions: [
        { condition: 'Reconsider only if the exact current owner facts change again.' },
      ],
    }),
    { executiveEpochId, rootThreadId },
  )
  assert.equal(resumedStrategy.status, 'completed')
  const afterStrategySnapshot = await bridge.hostSnapshot()
  await assert.rejects(
    () => bridge.validateSuspendedEpochCut({
      expectedCut: boundSnapshot.authorization_cut as MissionAuthorizationCut,
      executiveEpochId,
      rootThreadId,
      workspaceRoot: resolvedGoalWorkspaceRoot,
    }),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeError)
      assert.deepEqual(
        error.ownerErrors.map((item) => (item as Record<string, unknown>).code),
        ['mission_host_store_cut_stale'],
      )
      return true
    },
  )
  const resumedSnapshot = await bridge.hostSnapshot()
  assert.deepEqual(
    resumedSnapshot.current_state,
    afterStrategySnapshot.current_state,
  )
  assert.deepEqual(
    await bridge.validateSuspendedEpochCut({
      expectedCut: resumedSnapshot.authorization_cut as MissionAuthorizationCut,
      executiveEpochId,
      rootThreadId,
      workspaceRoot: resolvedGoalWorkspaceRoot,
    }),
    {
      executive_epoch_id: executiveEpochId,
      root_thread_id: rootThreadId,
      state: 'bound',
      cut_validated: true,
    },
  )

  await assert.rejects(
    () => bridge.executeFormalAttempt(
      { selected_bet_sha256: 'c'.repeat(64), correction_basis: null },
      { executiveEpochId, rootThreadId },
    ),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeError)
      assert.equal(
        error.exitCode,
        2,
        JSON.stringify({
          ownerErrors: error.ownerErrors,
          processDiagnostic: error.processDiagnostic,
        }),
      )
      assert.equal(error.terminationSignal, null)
      assert.deepEqual(
        error.ownerErrors.map((item) => (item as Record<string, unknown>).code),
        ['invalid_invocation'],
      )
      return true
    },
  )

  assert.equal(
    fs.existsSync(path.join(runtimeDir, 'formal-attempt', 'attempt-journal.sqlite3')),
    true,
  )
  assert.deepEqual(
    fs.readdirSync(runtimeDir).filter((name) => name.startsWith('mission-bridge-')),
    [],
  )

  const unsafeRuntimeDir = path.join(fixture.root, 'unsafe-runtime')
  const unsafeTarget = path.join(fixture.root, 'unsafe-input-target')
  const unsafeInputStages = path.join(
    unsafeRuntimeDir,
    'formal-attempt',
    'input-stages',
  )
  fs.mkdirSync(path.dirname(unsafeInputStages), { recursive: true })
  fs.mkdirSync(unsafeTarget)
  fs.symlinkSync(
    unsafeTarget,
    unsafeInputStages,
    process.platform === 'win32' ? 'junction' : 'dir',
  )
  const unsafeBridge = new PythonMissionBridge({ ...config, runtimeDir: unsafeRuntimeDir })
  await assert.rejects(
    () => unsafeBridge.executeFormalAttempt(
      { selected_bet_sha256: 'c'.repeat(64), correction_basis: null },
      { executiveEpochId, rootThreadId },
    ),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeError)
      assert.deepEqual(
        error.ownerErrors.map((item) => (item as Record<string, unknown>).code),
        ['unsafe_path_component'],
      )
      assert.equal(error.processDiagnostic.responseError, null)
      return true
    },
  )
  assert.deepEqual(
    fs.readdirSync(unsafeRuntimeDir).filter((name) => name.startsWith('mission-bridge-')),
    [],
  )
})

test('Python bridge has no hidden deadline beyond the former 120 second boundary', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-slow-')
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let simulatedElapsedMs = 0
  let started!: () => void
  const didStart = new Promise<void>((resolve) => {
    started = resolve
  })
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, args, options) => {
    started()
    assert.equal('timeout' in options, false)
    await gate
    assert.equal(simulatedElapsedMs, 120_001)
    writeResponse(args, ownerEnvelope({ reconstructed: true }))
    return { exitCode: 0, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  const pending = bridge.reconstruct()
  await didStart
  simulatedElapsedMs = 120_001
  release()

  assert.deepEqual(await pending, { reconstructed: true })
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge transports request, response, and streamed stdout beyond four MiB', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-large-')
  const large = 'semantic-content:' + 'x'.repeat(5 * 1024 * 1024)
  const helperPath = path.join(fixture.config.repoRoot, 'bridge-helper.cjs')
  fs.writeFileSync(
    helperPath,
    [
      "const fs = require('node:fs')",
      'const args = process.argv.slice(2)',
      "const requestPath = args[args.indexOf('--request') + 1]",
      "const responsePath = args[args.indexOf('--response') + 1]",
      'if (fs.statSync(requestPath).size <= 4 * 1024 * 1024) process.exit(2)',
      "const large = 'semantic-content:' + 'x'.repeat(5 * 1024 * 1024)",
      `const envelope = ${JSON.stringify(ownerEnvelope({}))}`,
      "envelope.data = { status: 'completed', result: { content: large } }",
      "fs.writeFileSync(responsePath, JSON.stringify(envelope), { encoding: 'utf8', flag: 'wx', mode: 0o600 })",
      "process.stdout.write('o'.repeat(5 * 1024 * 1024))",
      "process.stderr.write('e'.repeat(5 * 1024 * 1024))",
    ].join('\n'),
  )
  const bridge = new PythonMissionBridge({
    ...fixture.config,
    pythonPath: process.execPath,
    missionScriptPath: helperPath,
  })

  const result = await bridge.executeSemanticOperation(
    { operation: 'retrieve', input: { query: large } },
    {
      rootThreadId: 'thread.root',
      executiveEpochId: 'epoch.root',
    },
  )

  assert.equal((result.result as Record<string, unknown>).content, large)
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge preserves a bounded stderr tail and exact nonzero exit', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-stderr-')
  const helperPath = path.join(fixture.config.repoRoot, 'bridge-helper.cjs')
  fs.writeFileSync(
    helperPath,
    "process.stderr.write('x'.repeat(40 * 1024) + ':stderr-tail-sentinel'); process.exit(23)\n",
  )
  const bridge = new PythonMissionBridge({
    ...fixture.config,
    pythonPath: process.execPath,
    missionScriptPath: helperPath,
  })

  await assert.rejects(
    () => bridge.reconstruct(),
    (error: unknown) => {
      assert.ok(error instanceof MissionBridgeError)
      assert.equal(error.exitCode, 23)
      assert.equal(error.terminationSignal, null)
      assert.equal(error.processDiagnostic.stderrTruncated, true)
      assert.ok(error.processDiagnostic.stderrTail.length <= 32 * 1024)
      assert.match(error.processDiagnostic.stderrTail, /:stderr-tail-sentinel$/)
      assert.deepEqual(error.ownerErrors, [])
      return true
    },
  )
})

test(
  'Python bridge preserves the exact process termination signal',
  { skip: process.platform === 'win32' },
  async () => {
    const fixture = bridgeFixture('rh-mission-bridge-signal-')
    const helperPath = path.join(fixture.config.repoRoot, 'bridge-helper.cjs')
    fs.writeFileSync(
      helperPath,
      "process.stderr.write('signal-sentinel'); process.kill(process.pid, 'SIGTERM')\n",
    )
    const bridge = new PythonMissionBridge({
      ...fixture.config,
      pythonPath: process.execPath,
      missionScriptPath: helperPath,
    })

    await assert.rejects(
      () => bridge.reconstruct(),
      (error: unknown) => {
        assert.ok(error instanceof MissionBridgeError)
        assert.equal(error.exitCode, null)
        assert.equal(error.terminationSignal, 'SIGTERM')
        assert.equal(error.processDiagnostic.stderrTail, 'signal-sentinel')
        return true
      },
    )
  },
)

test('Python bridge explicit cancellation terminates and drains every outstanding owner process', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-cancel-')
  let started!: () => void
  const didStart = new Promise<void>((resolve) => {
    started = resolve
  })
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, _args, options) => {
    started()
    await new Promise<void>((resolve) => {
      options.signal.addEventListener('abort', () => resolve(), { once: true })
    })
    return { exitCode: 1, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  const pending = bridge.reconstruct()
  const rejected = assert.rejects(pending, MissionBridgeCancelledError)
  await didStart
  await bridge.cancelOutstanding('operator_stop')
  await rejected
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})

test('Python bridge force cancellation escalates an outstanding graceful cancellation', async () => {
  const fixture = bridgeFixture('rh-mission-bridge-force-escalation-')
  let started!: () => void
  const didStart = new Promise<void>((resolve) => {
    started = resolve
  })
  let cancelled!: () => void
  const didCancel = new Promise<void>((resolve) => {
    cancelled = resolve
  })
  let forced!: () => void
  const didForce = new Promise<void>((resolve) => {
    forced = resolve
  })
  const bridge = new PythonMissionBridge(fixture.config, async (_executable, _args, options) => {
    started()
    options.signal.addEventListener('abort', cancelled, { once: true })
    await new Promise<void>((resolve) => {
      options.forceSignal.addEventListener('abort', () => {
        forced()
        resolve()
      }, { once: true })
    })
    return { exitCode: 1, terminationSignal: null, stdout: '', stderr: '', stderrTruncated: false }
  })

  const pending = bridge.reconstruct()
  const rejected = assert.rejects(pending, (error: unknown) => {
    assert.ok(error instanceof MissionBridgeCancelledError)
    assert.equal(error.reason, 'explicit_force_stop')
    return true
  })
  await didStart
  const graceful = bridge.cancelOutstanding('operator_stop')
  await didCancel
  const force = bridge.cancelOutstanding('explicit_force_stop')
  await didForce
  await Promise.all([graceful, force, rejected])
  assert.deepEqual(fs.readdirSync(fixture.config.runtimeDir), [])
})
