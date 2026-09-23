#!/usr/bin/env python3
"""Explicit installation and foreground lifecycle for the real research Host.

This owner wrapper does not authenticate, select mathematical strategy, create a
service, or alter other installations. See docs/setup.md before starting a Mission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RECORD = ".mathematical-research-release.json"
CONFIG = "installation.json"
PROJECT = "project.riemann_hypothesis"
CATALOG = "services/rh-mission-host/assets/codex-model-catalog.0.153.4.json"
ENTRY = "services/rh-mission-host/dist/main.js"


def absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"an absolute path is required: {value}")
    return Path(os.path.abspath(path))


def ordinary(path: Path, *, directory: bool = False) -> os.stat_result:
    details = path.lstat()
    if path.is_symlink() or not (stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode)):
        raise ValueError(f"expected an ordinary {'directory' if directory else 'file'}: {path}")
    if path.resolve() != path:
        raise ValueError(f"path must not pass through a symlink: {path}")
    return details


def exclusive_json(path: Path, body: object, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(body, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def run(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(argv, cwd=ROOT, env=env, check=True, text=True, stdout=subprocess.PIPE)
    return completed.stdout.strip()


def installed_release() -> dict:
    for path, directory in ((ROOT, True), (ROOT / RECORD, False), (ROOT / ENTRY, False),
                            (ROOT / "scripts/rh_mission.py", False),
                            (ROOT / ".mathematical-research-model-projection.json", False),
                            (ROOT / CATALOG, False)):
        details = ordinary(path, directory=directory)
        if details.st_uid != 0 or details.st_mode & 0o022:
            raise ValueError(f"installed source must be root-owned and not group/world writable: {path}")
    sys.path[:0] = [str(ROOT), str(ROOT / "packages/research-core"), str(ROOT / "packages/research-attempt-adapter")]
    from research_core.mission_attempt_runtime import installed_release_source_digest
    record = json.loads((ROOT / RECORD).read_text(encoding="utf-8"))
    installed_release_source_digest(ROOT, record["release_sha"])
    return record


def source_bundle_identity(bundle: Path, release_sha: str) -> tuple[str, int]:
    """Bind the actual Git archive and verify its source files before enrichment."""
    ordinary(bundle)
    with tarfile.open(bundle, "r:") as archive:
        if archive.pax_headers.get("comment") != release_sha:
            raise ValueError("source bundle must be an uncompressed Git archive for the exact release SHA")
        seen: set[str] = set()
        for member in archive:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("source bundle contains an unsafe path")
            if member.isdir():
                continue
            if not member.isfile() or member.name in seen:
                raise ValueError("source bundle must contain unique ordinary source files")
            seen.add(member.name)
            target = ROOT / relative
            ordinary(target)
            source = archive.extractfile(member)
            assert source is not None
            with source, target.open("rb") as actual:
                if hashlib.file_digest(source, "sha256").digest() != hashlib.file_digest(actual, "sha256").digest():
                    raise ValueError(f"installed source differs from its Git archive: {member.name}")
    with bundle.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest, bundle.stat().st_size


def prepare(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", args.release_sha) or ROOT.name != args.release_sha:
        raise ValueError("the release directory basename must equal the exact 40-character public source commit")
    if (ROOT / RECORD).exists():
        raise ValueError("release is already prepared; do not mutate an installed release")
    digest, size = source_bundle_identity(absolute(args.source_bundle), args.release_sha)
    node, pnpm, codex = (str(absolute(value)) for value in (args.node, args.pnpm, args.codex))
    versions = {"node_version": run([node, "--version"]), "pnpm_version": run([pnpm, "--version"]),
                "codex_version": run([codex, "--version"])}
    if versions["codex_version"] != "codex-cli 0.153.4":
        raise ValueError("this release requires codex-cli 0.153.4")
    if not (ROOT / ENTRY).is_file():
        raise ValueError("build the Host before preparing the release")
    run([node, str(ROOT / "services/rh-mission-host/scripts/import-model-catalog.mjs"),
         "--cache", str(absolute(args.model_cache)), "--output", str(ROOT / CATALOG)])
    sys.path[:0] = [str(ROOT), str(ROOT / "packages/research-core"), str(ROOT / "packages/research-attempt-adapter")]
    from research_core.mission_operation_contract import project_mission_host_model_projection
    from research_core.research_model import deep_thaw
    exclusive_json(ROOT / ".mathematical-research-model-projection.json",
                   deep_thaw(project_mission_host_model_projection()), 0o644)
    exclusive_json(ROOT / RECORD, {
        "schema_version": "wc.rh_mission_host_release.v1", "release_sha": args.release_sha,
        "bundle_sha256": digest, "bundle_size": size,
        "service_package": "@workstation-control/rh-mission-host", **versions,
    }, 0o644)
    print(json.dumps({"status": "prepared", "release_sha": args.release_sha,
                      "bundle_sha256": digest, "bundle_size": size}))


def require_runtime_user() -> None:
    if sys.platform != "linux" or os.getuid() == 0:
        raise ValueError("runtime commands require Linux and a dedicated unprivileged account")
    os.umask(0o077)


def disjoint(paths: list[Path]) -> None:
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError(f"installation paths must not overlap: {first}, {second}")


def owned_directory(path: Path) -> None:
    details = ordinary(path, directory=True)
    if details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError(f"runtime directory must belong to this account with mode 0700: {path}")


def environment(config: dict) -> dict[str, str]:
    # No arbitrary provider keys, PYTHONPATH, plugins or another Codex home enter
    # the Host through the caller's ambient environment.
    import pwd
    home = pwd.getpwuid(os.getuid()).pw_dir
    bins = [str(Path(config[name]).parent) for name in ("node", "python", "codex")]
    env = {"HOME": home, "PATH": os.pathsep.join(dict.fromkeys([*bins, "/usr/local/bin", "/usr/bin", "/bin"])),
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "CODEX_HOME": config["codex_home"],
           "RH_MISSION_HOST_MISSION_ID": config["mission_id"],
           "RH_MISSION_HOST_RELEASE_COMMIT": config["release_sha"],
           "RH_MISSION_HOST_REPO_ROOT": str(ROOT),
           "RH_MISSION_HOST_WORKSPACE_ROOT": config["workspace_root"],
           "RH_MISSION_HOST_GOALS_ROOT": config["goals_root"],
           "RH_MISSION_HOST_RUNTIME_DIR": config["runtime_dir"],
           "RH_MISSION_RUNTIME_LOCK": config["lock_path"],
           "RH_MISSION_HOST_PYTHON_PATH": config["python"],
           "RH_MISSION_HOST_CODEX_CLI_PATH": config["codex"]}
    return env


def initialize(args: argparse.Namespace) -> None:
    require_runtime_user()
    record = installed_release()
    state = absolute(args.state_root)
    codex_home = absolute(args.codex_home)
    owned_directory(codex_home)
    paths = [ROOT, state / "workspace", state / "goals", state / "runtime", codex_home]
    disjoint(paths)
    if state.exists() and any(state.iterdir()):
        raise ValueError("init requires an absent or empty state root; existing state is never replaced")
    state.mkdir(mode=0o700, parents=False, exist_ok=True)
    owned_directory(state)
    seed = json.loads((ROOT / "contracts/rh_autonomous_mission_seed.v1.json").read_text(encoding="utf-8"))
    config = {"schema_version": "mathematical_research.installation.v1", "owner_uid": os.getuid(),
              "release_root": str(ROOT), "release_sha": record["release_sha"],
              "project_id": seed["project_id"], "mission_id": seed["mission"]["mission_id"],
              "workspace_root": str(state / "workspace"), "goals_root": str(state / "goals"),
              "runtime_dir": str(state / "runtime"), "lock_path": str(state / "runtime.lock"),
              "codex_home": str(codex_home), "python": str(absolute(args.python)),
              "node": str(absolute(args.node)), "codex": str(absolute(args.codex))}
    for name in ("python", "node", "codex"):
        if not Path(config[name]).is_file() or not os.access(config[name], os.X_OK):
            raise ValueError(f"{name} must select an existing executable")
    for directory in ("goals", "runtime"):
        (state / directory).mkdir(mode=0o700)
    # Retain this non-secret binding even if genesis fails, so a partial setup is
    # visible and never silently replaced on a retry.
    exclusive_json(state / CONFIG, config)
    request = state / "genesis-request.json"
    exclusive_json(request, {"schema_version": "mathematical_research.mission_owner_genesis_request.v1",
                            "mission_id": config["mission_id"], "workspace_root": config["workspace_root"],
                            "source_commit": record["release_sha"]})
    output = run([config["python"], str(ROOT / "scripts/rh_mission.py"), "owner-genesis", "--request", str(request),
                  "--workspace-root", config["workspace_root"], "--project-id", config["project_id"],
                  "--mission-id", config["mission_id"], "--format", "json"], env=environment(config))
    print(output)


def load(args: argparse.Namespace) -> dict:
    require_runtime_user()
    record = installed_release()
    state = absolute(args.state_root)
    owned_directory(state)
    ordinary(state / CONFIG)
    config = json.loads((state / CONFIG).read_text(encoding="utf-8"))
    if (config.get("schema_version") != "mathematical_research.installation.v1"
            or config.get("owner_uid") != os.getuid() or config.get("release_root") != str(ROOT)
            or config.get("release_sha") != record["release_sha"] or config.get("project_id") != PROJECT):
        raise ValueError("installation binding differs from this account or exact release")
    for key, suffix in (("workspace_root", "workspace"), ("goals_root", "goals"), ("runtime_dir", "runtime"),
                        ("lock_path", "runtime.lock")):
        if config.get(key) != str(state / suffix):
            raise ValueError(f"installation {key} differs from its selected state root")
    for key in ("workspace_root", "goals_root", "runtime_dir", "codex_home"):
        owned_directory(absolute(config[key]))
    disjoint([ROOT, *(absolute(config[key]) for key in ("workspace_root", "goals_root", "runtime_dir", "codex_home"))])
    return config


def inspect(config: dict) -> int:
    # Use the existing owner snapshot, not an invented direct SQL view.
    with tempfile.TemporaryDirectory(prefix="inspect-", dir=config["runtime_dir"]) as temporary:
        request, response = Path(temporary) / "request.json", Path(temporary) / "response.json"
        exclusive_json(request, {"schema_version": "mathematical_research.mission_host_bridge_request.v1",
                                "action": "host_snapshot",
                                "binding": {"project_id": config["project_id"], "mission_id": config["mission_id"]},
                                "payload": {"canonical_source": {"repo_root": str(ROOT), "release_sha": config["release_sha"]}}})
        result = subprocess.run([config["python"], str(ROOT / "scripts/rh_mission.py"), "host-bridge",
                                 "--request", str(request), "--response", str(response),
                                 "--workspace-root", config["workspace_root"], "--project-id", config["project_id"],
                                 "--mission-id", config["mission_id"], "--format", "json"],
                                cwd=ROOT, env=environment(config), check=False)
        if response.is_file():
            print(response.read_text(encoding="utf-8"), end="")
        return result.returncode


def stop(config: dict, *, at_checkpoint: bool) -> None:
    lock = Path(config["lock_path"])
    ordinary(lock)
    original = lock.read_bytes()
    binding = json.loads(original)
    pid = binding.get("pid")
    if type(pid) is not int or pid <= 1 or binding.get("releaseSha") != config["release_sha"]:
        raise ValueError("runtime lock does not identify this release's Host")
    descriptor = os.pidfd_open(pid)
    try:
        process = Path("/proc") / str(pid)
        if process.stat().st_uid != os.getuid():
            raise ValueError("the selected process belongs to another account")
        command = (process / "cmdline").read_bytes().split(b"\0")
        if len(command) < 3 or os.fsencode(ROOT / ENTRY) != command[1]:
            raise ValueError("runtime lock does not identify this release's foreground Host")
        if (process / "cwd").resolve() != ROOT or lock.read_bytes() != original:
            raise ValueError("runtime ownership changed; no stop sent")
        if at_checkpoint:
            marker = Path(config["runtime_dir"]) / "checkpoint-stop.pending"
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        else:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)
    print(json.dumps({"status": "stop_requested", "pid": pid, "at_checkpoint": at_checkpoint,
                      "note": "Wait for Host exit and inspect durable state; this request alone is not completion."}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare-release", help="enrich a built Git archive before root-owned installation")
    for name in ("release-sha", "source-bundle", "model-cache", "node", "pnpm", "codex"):
        prepare_parser.add_argument(f"--{name}", required=True)
    init_parser = commands.add_parser("init", help="initialize a new local Mission without launching a model")
    for name in ("state-root", "codex-home", "python", "node", "codex"):
        init_parser.add_argument(f"--{name}", required=True)
    for name in ("start", "inspect", "stop", "env"):
        command = commands.add_parser(name)
        command.add_argument("--state-root", required=True)
        if name == "start":
            command.add_argument("--single-epoch", action="store_true", help="explicit operator canary: stop after one terminal epoch")
        if name == "stop":
            command.add_argument("--at-checkpoint", action="store_true", help="request stop after the next durable checkpoint")
    args = parser.parse_args()
    if args.command == "prepare-release":
        if sys.platform != "linux":
            raise ValueError("prepare the supported release on Linux")
        prepare(args)
        return 0
    if args.command == "init":
        initialize(args)
        return 0
    config = load(args)
    if args.command == "inspect":
        return inspect(config)
    if args.command == "stop":
        stop(config, at_checkpoint=args.at_checkpoint)
    elif args.command == "env":
        print(json.dumps(environment(config), indent=2, sort_keys=True))
    elif args.command == "start":
        lock = Path(config["lock_path"])
        if lock.exists():
            binding = json.loads(lock.read_text(encoding="utf-8"))
            pid = binding.get("pid")
            if type(pid) is not int or pid <= 1:
                raise ValueError("existing runtime lock is invalid; inspect it before restarting")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass  # The Host owns stale-lock reconciliation.
            else:
                raise ValueError("this installation already has a live Host; no start or marker was issued")
        if args.single_epoch:
            marker = Path(config["runtime_dir"]) / "single-epoch-canary.pending"
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        os.chdir(ROOT)
        os.execve(config["node"], [config["node"], str(ROOT / ENTRY)], environment(config))
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"research: {error}", file=sys.stderr)
        raise SystemExit(1)
