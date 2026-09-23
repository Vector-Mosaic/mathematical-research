"""Detached durable worker which owns one Codex App Server Attempt."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import queue
import signal
import stat
import subprocess
import threading
from pathlib import Path
from typing import Mapping

from .codex_exec_provider import _require_execution_spec_cognitive_policy
from .durable_supervisor import (
    collect_complete_artifacts,
    process_identity,
    read_json_object,
    require_no_link_components,
    write_json_atomic,
    write_json_once,
)
from .models import (
    ProviderExecutionSpec,
    ProviderObservation,
    ProviderState,
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    staged_attachment_path,
)


_PERMISSION_ID = "wc_formal_attempt"


class _RpcFailure(RuntimeError):
    pass


class _TopLevelFacts:
    def __init__(
        self, delegation_max_depth: int, *, model: str, reasoning_effort: str
    ) -> None:
        if (
            not isinstance(delegation_max_depth, int)
            or isinstance(delegation_max_depth, bool)
            or delegation_max_depth < 1
        ):
            raise ValueError("Formal delegation depth must be one positive integer")
        self.delegation_max_depth = delegation_max_depth
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.turn_status: str | None = None
        self.failure_detail: str | None = None
        self.final_messages: list[str] = []
        self.descendant_final_messages: list[tuple[str, str, str]] = []
        self._descendant_final_keys: set[tuple[str, str, str]] = set()
        self._parent_thread_ids: dict[str, str] = {}
        self._pending_parent_thread_ids: dict[str, str] = {}
        self._pending_parent_assertions: dict[str, set[str]] = {}
        self._pending_descendant_finals: list[tuple[str, str | None, str]] = []
        self._pending_descendant_final_keys: set[
            tuple[str, str | None, str]
        ] = set()
        self.multi_agent_events = 0

    def _mark_failure(self, detail: str) -> None:
        if self.failure_detail is None:
            self.failure_detail = detail

    def _thread_depth(self, thread_id: str) -> int | None:
        if self.thread_id is None:
            return None
        if thread_id == self.thread_id:
            return 0
        depth = 0
        current = thread_id
        seen: set[str] = set()
        while current != self.thread_id:
            if current in seen:
                self._mark_failure("codex_delegation_parentage_cycle")
                return None
            seen.add(current)
            parent = self._parent_thread_ids.get(current)
            if parent is None:
                return None
            depth += 1
            current = parent
        return depth

    @staticmethod
    def _valid_thread_id(value: object) -> bool:
        return isinstance(value, str) and bool(value)

    def register_root_thread(self, thread_id: str) -> None:
        if not self._valid_thread_id(thread_id):
            self._mark_failure("codex_delegation_parentage_malformed")
            return
        if self.thread_id is not None:
            if self.thread_id != thread_id:
                self._mark_failure("codex_delegation_parentage_conflict")
            return
        self.thread_id = thread_id
        if thread_id in self._pending_parent_thread_ids:
            self._pending_parent_thread_ids.pop(thread_id, None)
            self._mark_failure("codex_delegation_parentage_conflict")
        retained: list[tuple[str, str | None, str]] = []
        for child_thread_id, expected_parent_thread_id, text in (
            self._pending_descendant_finals
        ):
            if child_thread_id != thread_id:
                retained.append((child_thread_id, expected_parent_thread_id, text))
                continue
            self._pending_descendant_final_keys.discard(
                (child_thread_id, expected_parent_thread_id, text)
            )
            if expected_parent_thread_id is not None:
                self._mark_failure("codex_delegation_parentage_conflict")
            else:
                self.final_messages.append(text)
        self._pending_descendant_finals = retained
        self._resolve_pending_parentage()

    def _pending_parentage_cycles(
        self, child_thread_id: str, parent_thread_id: str
    ) -> bool:
        current = parent_thread_id
        seen = {child_thread_id}
        while True:
            if current in seen:
                return True
            if self.thread_id is not None and current == self.thread_id:
                return False
            seen.add(current)
            parent = self._parent_thread_ids.get(current)
            if parent is None:
                parent = self._pending_parent_thread_ids.get(current)
            if parent is None:
                return False
            current = parent

    def _validate_parent_assertions(
        self, child_thread_id: str, parent_thread_id: str
    ) -> None:
        assertions = self._pending_parent_assertions.pop(child_thread_id, set())
        if any(item != parent_thread_id for item in assertions):
            self._mark_failure("codex_delegation_parentage_conflict")

    def _drain_descendant_finals(
        self, child_thread_id: str, parent_thread_id: str
    ) -> None:
        retained: list[tuple[str, str | None, str]] = []
        for pending_child, expected_parent, text in self._pending_descendant_finals:
            if pending_child != child_thread_id:
                retained.append((pending_child, expected_parent, text))
                continue
            self._pending_descendant_final_keys.discard(
                (pending_child, expected_parent, text)
            )
            if expected_parent is not None and expected_parent != parent_thread_id:
                self._mark_failure("codex_delegation_parentage_conflict")
                continue
            self._record_descendant_final(
                child_thread_id,
                parent_thread_id,
                text,
            )
        self._pending_descendant_finals = retained

    def _resolve_pending_parentage(self) -> None:
        if self.thread_id is None:
            return
        made_progress = True
        while made_progress:
            made_progress = False
            for child_thread_id, parent_thread_id in tuple(
                self._pending_parent_thread_ids.items()
            ):
                if child_thread_id == self.thread_id:
                    self._pending_parent_thread_ids.pop(child_thread_id, None)
                    self._mark_failure("codex_delegation_parentage_conflict")
                    made_progress = True
                    continue
                parent_depth = self._thread_depth(parent_thread_id)
                if parent_depth is None:
                    continue
                self._pending_parent_thread_ids.pop(child_thread_id, None)
                made_progress = True
                if parent_depth >= self.delegation_max_depth:
                    self._mark_failure("codex_delegation_depth_exceeded")
                    continue
                self._parent_thread_ids[child_thread_id] = parent_thread_id
                self._validate_parent_assertions(child_thread_id, parent_thread_id)
                self._drain_descendant_finals(child_thread_id, parent_thread_id)

    def _register_descendant(self, child_thread_id: str, parent_thread_id: str) -> None:
        if (
            not self._valid_thread_id(child_thread_id)
            or not self._valid_thread_id(parent_thread_id)
        ):
            self._mark_failure("codex_delegation_parentage_malformed")
            return
        if child_thread_id == parent_thread_id or child_thread_id == self.thread_id:
            self._mark_failure("codex_delegation_parentage_conflict")
            return
        existing = self._parent_thread_ids.get(child_thread_id)
        if existing is not None:
            if existing != parent_thread_id:
                self._mark_failure("codex_delegation_parentage_conflict")
            else:
                self._validate_parent_assertions(child_thread_id, parent_thread_id)
                self._drain_descendant_finals(child_thread_id, parent_thread_id)
            return
        pending = self._pending_parent_thread_ids.get(child_thread_id)
        if pending is not None and pending != parent_thread_id:
            self._mark_failure("codex_delegation_parentage_conflict")
            return
        self._pending_parent_thread_ids[child_thread_id] = parent_thread_id
        if self._pending_parentage_cycles(child_thread_id, parent_thread_id):
            self._pending_parent_thread_ids.pop(child_thread_id, None)
            self._mark_failure("codex_delegation_parentage_cycle")
            return
        self._resolve_pending_parentage()

    def _assert_direct_parent(
        self, child_thread_id: str, parent_thread_id: str
    ) -> None:
        if (
            not self._valid_thread_id(child_thread_id)
            or not self._valid_thread_id(parent_thread_id)
            or child_thread_id == parent_thread_id
            or child_thread_id == self.thread_id
        ):
            self._mark_failure("codex_delegation_parentage_conflict")
            return
        actual = self._parent_thread_ids.get(child_thread_id)
        if actual is not None:
            if actual != parent_thread_id:
                self._mark_failure("codex_delegation_parentage_conflict")
            return
        pending = self._pending_parent_thread_ids.get(child_thread_id)
        if pending is not None and pending != parent_thread_id:
            self._mark_failure("codex_delegation_parentage_conflict")
            return
        assertions = self._pending_parent_assertions.setdefault(
            child_thread_id,
            set(),
        )
        assertions.add(parent_thread_id)
        if len(assertions) != 1:
            self._mark_failure("codex_delegation_parentage_conflict")

    def _record_descendant_final(
        self, child_thread_id: str, parent_thread_id: str, text: str
    ) -> None:
        if self._parent_thread_ids.get(child_thread_id) != parent_thread_id:
            self._mark_failure("codex_delegation_parentage_conflict")
            return
        key = (child_thread_id, parent_thread_id, text)
        if key not in self._descendant_final_keys:
            self._descendant_final_keys.add(key)
            self.descendant_final_messages.append(key)

    def _queue_or_record_descendant_final(
        self,
        child_thread_id: str,
        text: str,
        *,
        expected_parent_thread_id: str | None = None,
    ) -> None:
        if not self._valid_thread_id(child_thread_id):
            self._mark_failure("codex_delegation_parentage_malformed")
            return
        if self.thread_id is not None and child_thread_id == self.thread_id:
            if expected_parent_thread_id is not None:
                self._mark_failure("codex_delegation_parentage_conflict")
            else:
                self.final_messages.append(text)
            return
        parent_thread_id = self._parent_thread_ids.get(child_thread_id)
        if parent_thread_id is not None:
            if (
                expected_parent_thread_id is not None
                and expected_parent_thread_id != parent_thread_id
            ):
                self._mark_failure("codex_delegation_parentage_conflict")
                return
            self._record_descendant_final(child_thread_id, parent_thread_id, text)
            return
        key = (child_thread_id, expected_parent_thread_id, text)
        if key not in self._pending_descendant_final_keys:
            self._pending_descendant_final_keys.add(key)
            self._pending_descendant_finals.append(key)

    def finalize_parentage(self) -> None:
        """Resolve delayed events and classify only genuinely unresolved custody."""

        self._resolve_pending_parentage()
        for child_thread_id, assertions in tuple(
            self._pending_parent_assertions.items()
        ):
            actual = self._parent_thread_ids.get(child_thread_id)
            if actual is not None:
                self._validate_parent_assertions(child_thread_id, actual)
            elif len(assertions) > 1:
                self._mark_failure("codex_delegation_parentage_conflict")
        if self._pending_parent_thread_ids:
            if any(
                self._pending_parentage_cycles(child_thread_id, parent_thread_id)
                for child_thread_id, parent_thread_id in (
                    self._pending_parent_thread_ids.items()
                )
            ):
                self._mark_failure("codex_delegation_parentage_cycle")
            else:
                self._mark_failure("codex_delegation_parentage_unresolved")
        if self._pending_parent_assertions or self._pending_descendant_finals:
            self._mark_failure("codex_delegation_parentage_unresolved")

    def _observe_collaboration_item(
        self, params: Mapping[str, object], item: Mapping[str, object]
    ) -> None:
        tool = item.get("tool")
        if tool not in {"spawnAgent", "sendInput", "wait"}:
            return
        outer_sender = params.get("threadId")
        sender = item.get("senderThreadId", outer_sender)
        receivers = item.get("receiverThreadIds")
        if (
            not self._valid_thread_id(outer_sender)
            or not self._valid_thread_id(sender)
            or sender != outer_sender
        ):
            self._mark_failure("codex_delegation_parentage_conflict")
            return
        if (
            not isinstance(receivers, list)
            or any(not self._valid_thread_id(value) for value in receivers)
            or len(receivers) != len(set(receivers))
        ):
            self._mark_failure("codex_delegation_parentage_malformed")
            return
        receiver_thread_ids = list(receivers)
        if tool == "spawnAgent":
            if len(receiver_thread_ids) != 1:
                self._mark_failure("codex_delegation_parentage_malformed")
                return
            self._register_descendant(receiver_thread_ids[0], sender)
            return
        for child_thread_id in receiver_thread_ids:
            self._assert_direct_parent(child_thread_id, sender)
            if tool != "wait":
                continue
            states = item.get("agentsStates")
            state = states.get(child_thread_id) if isinstance(states, Mapping) else None
            message = state.get("message") if isinstance(state, Mapping) else None
            if isinstance(message, str):
                self._queue_or_record_descendant_final(
                    child_thread_id,
                    message,
                    expected_parent_thread_id=sender,
                )

    def observe(self, message: Mapping[str, object]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "model/rerouted":
            self._mark_failure("codex_model_reroute_forbidden")
            return
        if method == "thread/settings/updated":
            settings = params.get("threadSettings")
            if isinstance(settings, dict):
                model = settings.get("model")
                provider = settings.get("modelProvider")
                effort = settings.get("reasoningEffort")
                if (
                    (model is not None and model != self.model)
                    or (provider is not None and provider != "openai")
                    or (effort is not None and effort != self.reasoning_effort)
                ):
                    self._mark_failure("codex_model_reroute_forbidden")
            return
        if method == "error":
            self._mark_failure("codex_top_level_error")
            return
        if method == "thread/started":
            thread = params.get("thread")
            if not isinstance(thread, Mapping):
                return
            child_thread_id = thread.get("id")
            parent_thread_id = thread.get("parentThreadId")
            if not self._valid_thread_id(child_thread_id):
                self._mark_failure("codex_delegation_parentage_malformed")
                return
            if parent_thread_id is None:
                self.register_root_thread(child_thread_id)
            elif isinstance(parent_thread_id, str):
                self._register_descendant(child_thread_id, parent_thread_id)
            else:
                self._mark_failure("codex_delegation_parentage_malformed")
            return
        if method == "turn/started":
            thread_id = params.get("threadId")
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else params.get("turnId")
            if self.thread_id is None and isinstance(thread_id, str):
                self.register_root_thread(thread_id)
            if thread_id != self.thread_id:
                return
            if isinstance(turn_id, str):
                self.turn_id = turn_id
            return
        if method == "turn/completed":
            thread_id = params.get("threadId")
            turn = params.get("turn")
            if self.thread_id is None and isinstance(thread_id, str):
                self.register_root_thread(thread_id)
            if thread_id != self.thread_id:
                return
            if isinstance(turn, dict):
                turn_id = turn.get("id")
                status = turn.get("status")
                if isinstance(turn_id, str):
                    self.turn_id = turn_id
                if isinstance(status, str):
                    self.turn_status = status
            return
        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, dict):
                return
            if item.get("type") == "collabAgentToolCall":
                self.multi_agent_events += 1
                self._observe_collaboration_item(params, item)
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                phase = item.get("phase")
                if phase in {None, "final_answer"}:
                    message_thread_id = params.get("threadId")
                    if message_thread_id == self.thread_id:
                        self.final_messages.append(str(item["text"]))
                    elif phase == "final_answer" and isinstance(message_thread_id, str):
                        self._queue_or_record_descendant_final(
                            message_thread_id,
                            str(item["text"]),
                        )


class _AppServer:
    def __init__(
        self,
        *,
        argv: list[str],
        cwd: str,
        scratch_root: Path,
        polling_cadence_seconds: float,
        delegation_max_depth: int,
        model: str,
        reasoning_effort: str,
        state_directory: Path,
        launch_binding_sha256: str,
        attempt_id: str,
    ) -> None:
        self.polling_cadence_seconds = polling_cadence_seconds
        self.state_directory = state_directory
        self.launch_binding_sha256 = launch_binding_sha256
        self.attempt_id = attempt_id
        self.messages: queue.Queue[dict[str, object] | None] = queue.Queue()
        self.stdin_lock = threading.Lock()
        self.facts = _TopLevelFacts(
            delegation_max_depth, model=model, reasoning_effort=reasoning_effort
        )
        self.next_request_id = 1
        self.force_requested = threading.Event()
        self.cancel_requested = threading.Event()
        self.stop_controls = threading.Event()
        self.job_handle = None
        self.events_handle = None
        self.stderr_handle = None
        kwargs: dict[str, object] = {
            "args": argv,
            "cwd": cwd,
            "env": dict(os.environ),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
            )
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(**kwargs)
        try:
            if os.name == "nt":
                self.job_handle = self._assign_windows_job()
            app_identity = process_identity(int(self.process.pid))
            if app_identity is None:
                raise _RpcFailure("App Server process identity is unavailable")
            write_json_once(
                state_directory / "app-server-owner.json",
                {
                    "schema": "wc.codex_attempt_app_server_owner.v1",
                    "attempt_id": attempt_id,
                    "launch_binding_sha256": launch_binding_sha256,
                    "process_identity": app_identity,
                },
            )
            # Protocol traffic and stderr are operational evidence, not declared
            # research output. Keep them in the already-owned scratch channel.
            self.events_handle = (scratch_root / "app-server-events.jsonl").open(
                "xb"
            )
            self.stderr_handle = (scratch_root / "app-server-stderr.log").open(
                "xb"
            )
            self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
            self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
            self.control_thread = threading.Thread(
                target=self._monitor_controls, daemon=True
            )
            self.stdout_thread.start()
            self.stderr_thread.start()
            self.control_thread.start()
        except BaseException:
            self._abort_initialization()
            raise

    def _abort_initialization(self) -> None:
        try:
            if self.process.poll() is None:
                if os.name == "nt" and self.job_handle is None:
                    self.process.kill()
                else:
                    self.force_tree()
            self.process.wait()
        finally:
            for handle_name in ("events_handle", "stderr_handle"):
                handle = getattr(self, handle_name, None)
                if handle is not None:
                    handle.close()
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(self.process, stream_name, None)
                if stream is not None:
                    stream.close()
            if self.job_handle is not None:
                ctypes.windll.kernel32.CloseHandle(self.job_handle)
                self.job_handle = None

    def _assign_windows_job(self):
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        name = "wc-rh-attempt-" + self.launch_binding_sha256
        job = kernel32.CreateJobObjectW(None, name)
        if not job:
            raise _RpcFailure("Could not create the exact App Server Job Object")

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(f"value{index}", ctypes.c_ulonglong) for index in range(6)]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ) or not kernel32.AssignProcessToJobObject(
            job, ctypes.c_void_p(int(self.process._handle))
        ):
            kernel32.CloseHandle(job)
            raise _RpcFailure("Could not bind App Server to its exact Job Object")
        return job

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        assert self.events_handle is not None
        try:
            for line in iter(self.process.stdout.readline, b""):
                self.events_handle.write(line)
                self.events_handle.flush()
                try:
                    decoded = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.messages.put(
                        {
                            "method": "error",
                            "params": {"source": "invalid_app_server_envelope"},
                        }
                    )
                    continue
                if isinstance(decoded, dict):
                    self.messages.put(decoded)
                else:
                    self.messages.put(
                        {
                            "method": "error",
                            "params": {"source": "non_object_app_server_envelope"},
                        }
                    )
        finally:
            self.messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        assert self.stderr_handle is not None
        for chunk in iter(lambda: self.process.stderr.read(1024 * 1024), b""):
            self.stderr_handle.write(chunk)
            self.stderr_handle.flush()

    def _send(self, payload: Mapping[str, object]) -> None:
        encoded = canonical_json_bytes(payload) + b"\n"
        with self.stdin_lock:
            if self.process.stdin is None:
                raise _RpcFailure("App Server stdin is unavailable")
            self.process.stdin.write(encoded)
            self.process.stdin.flush()

    def request(self, method: str, params: Mapping[str, object]) -> object:
        request_id = f"attempt-{self.next_request_id}"
        self.next_request_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = self.messages.get()
            if message is None:
                raise _RpcFailure("App Server exited before replying")
            self._observe(message)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                self.facts.failure_detail = "codex_top_level_error"
                raise _RpcFailure(f"App Server {method} returned a top-level error")
            return message.get("result")

    def _observe(self, message: dict[str, object]) -> None:
        self.facts.observe(message)
        if "id" in message and isinstance(message.get("method"), str):
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Interactive server requests are unavailable for this Attempt.",
                    },
                }
            )

    def wait_for_terminal(self) -> None:
        while self.facts.turn_status is None:
            message = self.messages.get()
            if message is None:
                return
            self._observe(message)

    def _control_payload(self, action: str) -> dict[str, object] | None:
        path = self.state_directory / f"{action}.request.json"
        if not path.exists():
            return None
        payload = read_json_object(path)
        expected = {
            "schema": "wc.codex_attempt_control.v1",
            "attempt_id": self.attempt_id,
            "launch_binding_sha256": self.launch_binding_sha256,
            "action": action,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise _RpcFailure("Attempt control request has an invalid binding")
        return payload

    def _monitor_controls(self) -> None:
        cancel_sent = False
        while not self.stop_controls.wait(self.polling_cadence_seconds):
            try:
                if self._control_payload("force_stop") is not None:
                    self.force_requested.set()
                    self.force_tree()
                    return
                if self._control_payload("cancel") is not None:
                    self.cancel_requested.set()
                    if (
                        not cancel_sent
                        and self.facts.thread_id is not None
                        and self.facts.turn_id is not None
                        and self.process.poll() is None
                    ):
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": "attempt-cancel",
                                "method": "turn/interrupt",
                                "params": {
                                    "threadId": self.facts.thread_id,
                                    "turnId": self.facts.turn_id,
                                },
                            }
                        )
                        cancel_sent = True
            except BaseException:
                self.force_requested.set()
                self.force_tree()
                return

    def force_tree(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name == "nt":
            if self.job_handle is None or not ctypes.windll.kernel32.TerminateJobObject(
                self.job_handle, 0xC000013A
            ):
                raise _RpcFailure("Exact App Server Job Object termination failed")
        else:
            os.killpg(int(self.process.pid), signal.SIGKILL)

    def close_after_terminal(self) -> int:
        self.stop_controls.set()
        if self.process.poll() is None:
            if self.process.stdin is not None:
                with self.stdin_lock:
                    self.process.stdin.close()
            self.process.terminate()
        exit_code = int(self.process.wait())
        self.stdout_thread.join()
        self.stderr_thread.join()
        self.control_thread.join()
        assert self.events_handle is not None
        assert self.stderr_handle is not None
        self.events_handle.close()
        self.stderr_handle.close()
        if self.job_handle is not None:
            ctypes.windll.kernel32.CloseHandle(self.job_handle)
        return exit_code

    def close_after_exit(self) -> int:
        self.stop_controls.set()
        exit_code = int(self.process.wait())
        self.stdout_thread.join()
        self.stderr_thread.join()
        self.control_thread.join()
        assert self.events_handle is not None
        assert self.stderr_handle is not None
        self.events_handle.close()
        self.stderr_handle.close()
        if self.job_handle is not None:
            ctypes.windll.kernel32.CloseHandle(self.job_handle)
        return exit_code


def _result_object(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _RpcFailure(f"App Server {key} response is not an object")
    nested = value.get(key)
    if isinstance(nested, dict):
        return nested
    return value


def _verify_staged_inputs(spec: ProviderExecutionSpec) -> None:
    root = Path(spec.input_staging_root)
    require_no_link_components(root, "worker_input_staging_root")
    details = root.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise _RpcFailure("Worker input staging root is not a directory")
    expected = {str(Path(spec.bootstrap_manifest_path).absolute()).casefold()}
    manifest = Path(spec.bootstrap_manifest_path)
    manifest_details = manifest.lstat()
    if (
        not stat.S_ISREG(manifest_details.st_mode)
        or manifest_details.st_nlink != 1
        or sha256_file(manifest) != spec.bootstrap_manifest_sha256
    ):
        raise _RpcFailure("Worker bootstrap manifest custody drifted")
    for item in spec.staged_attachments:
        path = Path(staged_attachment_path(str(root), item.logical_name))
        require_no_link_components(path, "worker_staged_attachment")
        item_details = path.lstat()
        if (
            path != Path(item.staged_path)
            or not stat.S_ISREG(item_details.st_mode)
            or item_details.st_nlink != 1
            or item_details.st_size != item.byte_length
            or sha256_file(path) != item.sha256
        ):
            raise _RpcFailure("Worker staged attachment custody drifted")
        expected.add(str(path.absolute()).casefold())
    actual: set[str] = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(directory) / name
            require_no_link_components(path, "worker_staged_directory")
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise _RpcFailure("Worker staged directory custody drifted")
        for name in files:
            actual.add(str((Path(directory) / name).absolute()).casefold())
    if actual != expected:
        raise _RpcFailure("Worker staged input inventory drifted")


def _write_text_once(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        try:
            existing = path.read_bytes()
        except OSError as read_exc:
            raise _RpcFailure("Provider message artifact could not be inspected") from read_exc
        if existing != encoded:
            raise _RpcFailure("Provider message artifact path was already occupied") from exc


def _persist_provider_messages(facts: _TopLevelFacts, output_root: Path) -> None:
    if facts.final_messages:
        _write_text_once(output_root / "provider-final-message.txt", facts.final_messages[-1])
    for ordinal, (child_thread_id, parent_thread_id, content) in enumerate(
        facts.descendant_final_messages,
        start=1,
    ):
        body = (
            "child_thread_id="
            + json.dumps(child_thread_id, ensure_ascii=False)
            + "\nparent_thread_id="
            + json.dumps(parent_thread_id, ensure_ascii=False)
            + "\n\n"
            + content
        )
        _write_text_once(
            output_root / f"provider-descendant-final-{ordinal:06d}.txt",
            body,
        )


def _terminal_observation(
    *,
    app_server: _AppServer,
    provider_ref: str,
    exit_code: int,
    output_root: Path,
    scratch_root: Path,
) -> ProviderObservation:
    app_server.facts.finalize_parentage()
    status = app_server.facts.turn_status
    detail = app_server.facts.failure_detail
    if app_server.force_requested.is_set():
        state = ProviderState.FORCE_STOPPED
        detail = "codex_force_stopped"
    elif app_server.cancel_requested.is_set() and status in {None, "interrupted"}:
        state = ProviderState.CANCELLED
        detail = "codex_cancelled"
    elif detail is not None:
        state = ProviderState.FAILED
    elif status == "completed":
        state = ProviderState.SUCCEEDED
        detail = "codex_succeeded"
    elif status == "failed":
        state = ProviderState.FAILED
        detail = "codex_turn_failed"
    elif status == "interrupted":
        state = ProviderState.FAILED
        detail = "codex_unrequested_interruption"
    elif exit_code != 0:
        state = ProviderState.FAILED
        detail = "codex_app_server_nonzero_exit"
    else:
        state = ProviderState.FAILED
        detail = "codex_app_server_exit_without_turn_terminal"
    _persist_provider_messages(app_server.facts, output_root)
    artifacts = collect_complete_artifacts(output_root, scratch_root)
    return ProviderObservation(
        provider_id="codex_exec",
        provider_ref=provider_ref,
        state=state,
        detail_code=str(detail),
        exit_code=exit_code,
        artifacts=artifacts,
        resource_facts={
            "exit_code": exit_code,
            "artifact_count": len(artifacts),
            "output_bytes": sum(item.size_bytes for item in artifacts),
            "multi_agent_events": app_server.facts.multi_agent_events,
        },
    )


def _write_failed_completion(
    *,
    state_directory: Path,
    attempt_id: str,
    launch_sha256: str,
    spec: ProviderExecutionSpec,
    provider_ref: str,
    detail_code: str,
    exit_code: int | None,
    output_root: Path,
    scratch_root: Path,
    facts: _TopLevelFacts | None = None,
) -> None:
    if facts is not None:
        facts.finalize_parentage()
        if (
            detail_code == "codex_app_server_protocol_failure"
            and facts.failure_detail is not None
        ):
            detail_code = facts.failure_detail
        _persist_provider_messages(facts, output_root)
    artifacts = collect_complete_artifacts(output_root, scratch_root)
    observation = ProviderObservation(
        provider_id="codex_exec",
        provider_ref=provider_ref,
        state=ProviderState.FAILED,
        detail_code=detail_code,
        exit_code=exit_code,
        artifacts=artifacts,
        resource_facts={
            "exit_code": exit_code,
            "artifact_count": len(artifacts),
            "output_bytes": sum(item.size_bytes for item in artifacts),
            "multi_agent_events": 0 if facts is None else facts.multi_agent_events,
        },
    )
    write_json_once(
        state_directory / "completion.json",
        {
            "schema": "wc.codex_attempt_completion.v3",
            "attempt_id": attempt_id,
            "execution_spec_sha256": spec.digest,
            "launch_binding_sha256": launch_sha256,
            "observation": observation.as_dict(),
        },
    )


def run_worker(state_directory: Path) -> None:
    require_no_link_components(state_directory, "worker_state_directory")
    request_path = state_directory / "worker-request.json"
    launch_path = state_directory / "launch.json"
    request = read_json_object(request_path)
    launch = read_json_object(launch_path)
    launch_sha256 = sha256_bytes(canonical_json_bytes(launch))
    if (
        request.get("schema") != "wc.codex_attempt_worker_request.v1"
        or launch.get("schema") != "wc.codex_attempt_launch.v3"
        or launch.get("worker_request_sha256") != sha256_file(request_path)
        or request.get("attempt_id") != launch.get("attempt_id")
        or request.get("session_id") != launch.get("session_id")
        or request.get("provider_ref") != launch.get("provider_ref")
        or request.get("execution_spec_sha256")
        != launch.get("execution_spec_sha256")
    ):
        raise _RpcFailure("Worker request does not match its immutable launch binding")
    attempt_id = str(request["attempt_id"])
    identity = process_identity(os.getpid())
    if identity is None:
        raise _RpcFailure("Worker cannot establish its process-birth identity")
    if os.name != "nt" and identity.get("process_group") != os.getpid():
        raise _RpcFailure("Worker does not own its exact POSIX process group")
    write_json_once(
        state_directory / "worker-owner.json",
        {
            "schema": "wc.codex_attempt_worker_owner.v1",
            "attempt_id": attempt_id,
            "launch_binding_sha256": launch_sha256,
            "process_identity": identity,
        },
    )
    spec_raw = request.get("execution_spec")
    if not isinstance(spec_raw, dict):
        raise _RpcFailure("Worker execution specification is absent")
    spec = ProviderExecutionSpec.from_dict(spec_raw)
    if spec.digest != request.get("execution_spec_sha256"):
        raise _RpcFailure("Worker execution specification digest drifted")
    _require_execution_spec_cognitive_policy(spec)
    _verify_staged_inputs(spec)
    try:
        bootstrap = base64.b64decode(str(request["bootstrap_base64"]), validate=True)
    except (ValueError, KeyError) as exc:
        raise _RpcFailure("Worker bootstrap is invalid") from exc
    if sha256_bytes(bootstrap) != request.get("bootstrap_sha256"):
        raise _RpcFailure("Worker bootstrap digest drifted")
    argv = request.get("app_server_argv")
    thread_params = request.get("thread_start_params")
    cadence = request.get("polling_cadence_seconds")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) for item in argv)
        or not isinstance(thread_params, dict)
        or not isinstance(cadence, (int, float))
        or isinstance(cadence, bool)
        or not math.isfinite(float(cadence))
        or cadence <= 0
    ):
        raise _RpcFailure("Worker operational request is invalid")
    if (
        thread_params.get("model") != spec.model_profile
        or thread_params.get("modelProvider") != "openai"
        or thread_params.get("allowProviderModelFallback") is not False
    ):
        raise _RpcFailure(
            "Worker model request differs from its immutable execution specification"
        )
    config = thread_params.get("config")
    delegation_max_depth = (
        config.get("agents.max_depth") if isinstance(config, dict) else None
    )
    if (
        not isinstance(delegation_max_depth, int)
        or isinstance(delegation_max_depth, bool)
        or delegation_max_depth < 1
    ):
        raise _RpcFailure("Worker delegation depth boundary is absent or invalid")
    output_root = Path(spec.output_directory)
    scratch_root = Path(spec.scratch_directory)
    try:
        app_server = _AppServer(
            argv=list(argv),
            cwd=spec.scratch_directory,
            scratch_root=scratch_root,
            polling_cadence_seconds=float(cadence),
            delegation_max_depth=delegation_max_depth,
            model=spec.model_profile,
            reasoning_effort=spec.reasoning_effort,
            state_directory=state_directory,
            launch_binding_sha256=launch_sha256,
            attempt_id=attempt_id,
        )
    except BaseException as exc:
        try:
            _write_failed_completion(
                state_directory=state_directory,
                attempt_id=attempt_id,
                launch_sha256=launch_sha256,
                spec=spec,
                provider_ref=str(request["provider_ref"]),
                detail_code="codex_app_server_start_failed",
                exit_code=None,
                output_root=output_root,
                scratch_root=scratch_root,
            )
        except BaseException:
            write_json_atomic(
                state_directory / "worker-error.json",
                {
                    "schema": "wc.codex_attempt_worker_error.v1",
                    "attempt_id": attempt_id,
                    "launch_binding_sha256": launch_sha256,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        return
    try:
        app_server.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "wc-formal-attempt-worker",
                    "title": "Workstation Formal Attempt Worker",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        started = _result_object(
            app_server.request("thread/start", thread_params), "thread"
        )
        thread_id = started.get("id")
        if not isinstance(thread_id, str):
            raise _RpcFailure("thread/start returned no exact thread identity")
        app_server.facts.register_root_thread(thread_id)
        prompt = bootstrap.decode("utf-8", errors="strict")
        turn = _result_object(
            app_server.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": spec.scratch_directory,
                    "approvalPolicy": "never",
                    "effort": spec.reasoning_effort,
                    "model": spec.model_profile,
                    "permissions": _PERMISSION_ID,
                    "runtimeWorkspaceRoots": list(
                        thread_params["runtimeWorkspaceRoots"]
                    ),
                    "environments": list(thread_params["environments"]),
                    "input": [
                        {"type": "text", "text": prompt, "text_elements": []}
                    ],
                },
            ),
            "turn",
        )
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            raise _RpcFailure("turn/start returned no exact turn identity")
        app_server.facts.turn_id = turn_id
        app_server.wait_for_terminal()
        if app_server.process.poll() is None:
            exit_code = app_server.close_after_terminal()
        else:
            exit_code = app_server.close_after_exit()
        observation = _terminal_observation(
            app_server=app_server,
            provider_ref=str(request["provider_ref"]),
            exit_code=exit_code,
            output_root=output_root,
            scratch_root=scratch_root,
        )
        completion = {
            "schema": "wc.codex_attempt_completion.v3",
            "attempt_id": attempt_id,
            "execution_spec_sha256": spec.digest,
            "launch_binding_sha256": launch_sha256,
            "observation": observation.as_dict(),
        }
        write_json_once(state_directory / "completion.json", completion)
    except BaseException as exc:
        try:
            app_server.force_tree()
            exit_code = app_server.close_after_exit()
        except BaseException:
            write_json_atomic(
                state_directory / "worker-error.json",
                {
                    "schema": "wc.codex_attempt_worker_error.v1",
                    "attempt_id": attempt_id,
                    "launch_binding_sha256": launch_sha256,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        try:
            _write_failed_completion(
                state_directory=state_directory,
                attempt_id=attempt_id,
                launch_sha256=launch_sha256,
                spec=spec,
                provider_ref=str(request["provider_ref"]),
                detail_code=(
                    app_server.facts.failure_detail
                    or "codex_app_server_protocol_failure"
                ),
                exit_code=exit_code,
                output_root=output_root,
                scratch_root=scratch_root,
                facts=app_server.facts,
            )
        except BaseException:
            write_json_atomic(
                state_directory / "worker-error.json",
                {
                    "schema": "wc.codex_attempt_worker_error.v1",
                    "attempt_id": attempt_id,
                    "launch_binding_sha256": launch_sha256,
                    "error_type": type(exc).__name__,
                },
            )
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-directory", required=True)
    arguments = parser.parse_args()
    run_worker(Path(arguments.state_directory).absolute())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
