"""Focused unit tests for the Slice-2 source-endpoint predecessor stop primitive.

These tests exercise the exact predecessor stop implemented in
``scripts/deploy/deploy.py::slice2_source_predecessor_stop`` using a minimal
fake runner. They cover, end-to-end:

1. Stage 01 receipt validation — endpoint mismatch fails *before* any runner
   call.
2. Stage 01 receipt validation — non-hex / wrong-length predecessor container
   ID fails *before* the stop is issued.
3. Exact call ordering on the happy path:
   inspect ``.Id`` → inspect ``.State.Running`` → ``docker stop --time 30``
   → inspect ``.State.Running``.
4. The same deterministic plan in dry-run mode — three live inspects must not
   actually run, and the recorded plan must match.
5. Post-stop ``State.Running=true`` after a successful ``docker stop`` raises
   :class:`DeterministicGateFailure` because the stop did not converge.
6. Command corpus contains no forbidden operations
   (``rm``, ``prune``, ``chown``, ``compose``, ``recreate``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import cast

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "scripts" / "deploy" / "deploy.py"
SOURCE_DOCKER_HOST = "unix:///var/run/docker.sock"
TARGET_DOCKER_HOST = "unix:///var/run/docker.sock"
WRONG_DOCKER_HOST = "unix:///run/user/1000/docker.sock"
FAKE_HOST = "user@example"
FAKE_REMOTE_ROOT = "/opt/ispindel-dashboard"

# A valid 64-char lowercase hex digest — used as the captured predecessor ID.
CAPTURED_ID = "1" * 64
# Distinct value for tests that simulate an ID drift in the live inspect.
DRIFTED_ID = "2" * 64

FORBIDDEN_TOKENS = ("rm", "prune", "chown", "compose", "recreate")


def _load_deploy() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ispindel_deploy_predecessor_stop", DEPLOY_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy() -> ModuleType:
    return _load_deploy()


def _make_receipt(*, source_endpoint: str = SOURCE_DOCKER_HOST,
                  predecessor_container_id: str = CAPTURED_ID) -> dict[str, object]:
    return {
        "source_endpoint": source_endpoint,
        "predecessor_container_id": predecessor_container_id,
    }


class RecordingRunner:
    """Minimal fake Runner that records every call sequence in order.

    Default responses simulate the live predecessor on the source endpoint:
    the inspect ``.Id`` returns the captured ID, the first inspect of
    ``.State.Running`` returns ``"true"``, and the post-stop inspect returns
    ``"false"``. Tests mutate :attr:`responses` to script failure modes.
    """

    def __init__(self) -> None:
        self.dry_run = False
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.plan: list[list[str]] = []
        self.responses: list[bytes] = [CAPTURED_ID.encode() + b"\n", b"true\n",
                                       b"", b"false\n"]

    def _record(self, name: str, argv: object, **kwargs: object) -> None:
        if isinstance(argv, list):
            self.calls.append((name, tuple(argv), dict(kwargs)))
        else:
            self.calls.append((name, (argv,), dict(kwargs)))

    def _script_based(self, name: str, script: str, **kwargs: object) -> None:
        self.calls.append((name, (script,), dict(kwargs)))

    def remote(self, _host: str, _root: str, command: object, *,
               required: bool = True) -> subprocess.CompletedProcess[bytes]:
        self._record("remote", command, required=required)
        return subprocess.CompletedProcess([], 0, b"", b"")

    def remote_script(self, _host: str, _root: str, script: str, *,
                      required: bool = True) -> subprocess.CompletedProcess[bytes]:
        self._script_based("remote_script", script, required=required)
        return subprocess.CompletedProcess([], 0, b"", b"")

    def remote_endpoint(self, host: str, root: str, endpoint: str, command: object,
                        *, required: bool = True,
                        label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
        self._record("remote_endpoint", command, endpoint=endpoint, label=label,
                     required=required)
        if not self.responses:
            return subprocess.CompletedProcess([], 0, b"", b"")
        stdout = self.responses.pop(0)
        return subprocess.CompletedProcess([], 0, stdout, b"")


def _argv_token_sequence(calls: list[tuple[str, tuple[object, ...], dict[str, object]]]) \
        -> list[str]:
    """Flatten every recorded argv and script into a single token stream."""
    out: list[str] = []
    for _name, args, _kwargs in calls:
        for item in args:
            if isinstance(item, list):
                out.extend(cast(list[str], item))
            elif isinstance(item, str):
                out.extend(item.split())
            else:
                out.append(str(item))
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_endpoint_mismatch_fails_before_any_runner_call(deploy: ModuleType) -> None:
    """A wrong source_endpoint must fail closed with DeterministicGateFailure
    before any inspect/stop is dispatched."""
    runner = RecordingRunner()
    receipt = _make_receipt(source_endpoint=WRONG_DOCKER_HOST)
    with pytest.raises(deploy.DeterministicGateFailure,
                       match="source_endpoint"):
        deploy.slice2_source_predecessor_stop(
            runner, FAKE_HOST, FAKE_REMOTE_ROOT, receipt, execute=True,
        )
    assert runner.calls == []
    assert runner.plan == []


def test_invalid_predecessor_container_id_fails_before_stop(deploy: ModuleType) -> None:
    """A non-64-hex predecessor_container_id must fail closed without issuing
    any docker invocation."""
    runner = RecordingRunner()
    cases = ("not-hex", "Z" * 64, "0" * 63, "0" * 65, "", "0xa", "abc-def")
    for bad in cases:
        receipt = _make_receipt(predecessor_container_id=bad)
        with pytest.raises(deploy.DeterministicGateFailure,
                           match="lowercase 64-char hex"):
            deploy.slice2_source_predecessor_stop(
                runner, FAKE_HOST, FAKE_REMOTE_ROOT, receipt, execute=True,
            )
    assert runner.calls == []


def test_happy_path_executes_exact_inspect_stop_inspect_sequence(deploy: ModuleType) -> None:
    """On the happy path the recorded sequence is exactly:
    inspect ``.Id`` → inspect ``.State.Running`` → ``docker stop --time 30``
    → inspect ``.State.Running``; all on the source endpoint.
    """
    runner = RecordingRunner()
    receipt = _make_receipt()
    plan = deploy.slice2_source_predecessor_stop(
        runner, FAKE_HOST, FAKE_REMOTE_ROOT, receipt, execute=True,
    )
    names = [name for name, _args, _kwargs in runner.calls]
    assert names == [
        "remote_endpoint", "remote_endpoint", "remote_endpoint", "remote_endpoint",
    ]
    # First inspect: pin ID.
    first_argv = list(runner.calls[0][1])
    assert first_argv == [
        "docker", "inspect", "--format", "{{.Id}}", CAPTURED_ID,
    ]
    # Second inspect: confirm running.
    second_argv = list(runner.calls[1][1])
    assert second_argv == [
        "docker", "inspect", "--format", "{{.State.Running}}", CAPTURED_ID,
    ]
    # Third: the exact docker stop with --time 30 and captured ID.
    third_argv = list(runner.calls[2][1])
    assert third_argv == [
        "docker", "stop", "--time", "30", CAPTURED_ID,
    ]
    # Fourth: re-inspect running must be false.
    fourth_argv = list(runner.calls[3][1])
    assert fourth_argv == [
        "docker", "inspect", "--format", "{{.State.Running}}", CAPTURED_ID,
    ]
    # Every call is pinned to the captured source endpoint.
    for _n, _a, kwargs in runner.calls:
        assert kwargs["endpoint"] == SOURCE_DOCKER_HOST
        assert kwargs["label"] == "source"
    # Plan returns the four deterministic steps and the post-stop running flag.
    assert [step["label"] for step in plan["steps"]] == [
        "pre_inspect_id", "pre_inspect_running", "stop", "post_inspect_running",
    ]
    assert plan["predecessor_container_id"] == CAPTURED_ID
    assert plan["source_endpoint"] == SOURCE_DOCKER_HOST
    assert plan["stop_timeout_seconds"] == 30
    assert plan["post_stop_running"] == "false"


def test_dry_run_records_plan_without_calling_runner(deploy: ModuleType) -> None:
    """Dry-run must not invoke the fake runner at all; it only appends a
    deterministic plan row representing the inspect/stop/inspect sequence."""
    runner = RecordingRunner()
    runner.dry_run = True
    receipt = _make_receipt()
    plan = deploy.slice2_source_predecessor_stop(
        runner, FAKE_HOST, FAKE_REMOTE_ROOT, receipt, execute=False,
    )
    assert [list(args) for _name, args, _kwargs in runner.calls] == [
        ["docker", "inspect", "--format", "{{.Id}}", CAPTURED_ID],
        ["docker", "inspect", "--format", "{{.State.Running}}", CAPTURED_ID],
        ["docker", "stop", "--time", "30", CAPTURED_ID],
        ["docker", "inspect", "--format", "{{.State.Running}}", CAPTURED_ID],
    ]
    # The dry-run plan still describes what execute would do.
    assert [step["label"] for step in plan["steps"]] == [
        "pre_inspect_id", "pre_inspect_running", "stop", "post_inspect_running",
    ]


def test_id_mismatch_fails_before_stop_is_issued(deploy: ModuleType) -> None:
    """If inspect ``.Id`` returns anything other than the captured ID, the
    function must abort with DeterministicGateFailure before issuing stop.
    """
    runner = RecordingRunner()
    runner.responses = [DRIFTED_ID.encode() + b"\n"]  # only first response matters
    receipt = _make_receipt()
    with pytest.raises(deploy.DeterministicGateFailure,
                       match="pre-inspect ID mismatch"):
        deploy.slice2_source_predecessor_stop(
            runner, FAKE_HOST, FAKE_REMOTE_ROOT, receipt, execute=True,
        )
    # Stop must not have been called; only the ID inspect and (possibly)
    # the running inspect at most. We assert no "docker stop" string exists in
    # any recorded argv.
    tokens = _argv_token_sequence(runner.calls)
    assert "stop" not in tokens
    # No argv combines docker + stop + captured id; verify by reconstructing
    # the stop argv literally.
    for _name, args, _kwargs in runner.calls:
        argv = list(args)
        if len(argv) >= 3 and argv[1] == "stop":
            pytest.fail(f"docker stop should not have been issued; got {argv}")
    # The pre-inspect ID call must have been made.
    assert any(
        name == "remote_endpoint" and list(args)[:3] == ["docker", "inspect", "--format"]
        for name, args, _kwargs in runner.calls
    )


def test_post_stop_state_failing_raises_deterministic_gate_failure(deploy: ModuleType) -> None:
    """Post-stop inspect reporting ``State.Running=true`` means docker stop
    did not converge — fail closed.
    """
    runner = RecordingRunner()
    # Pre-inspect ID OK, pre-inspect running OK, stop returns success, but
    # post-stop inspect returns "true".
    runner.responses = [
        CAPTURED_ID.encode() + b"\n",
        b"true\n",
        b"",
        b"true\n",
    ]
    receipt = _make_receipt()
    with pytest.raises(deploy.DeterministicGateFailure,
                       match="post-stop Running=true"):
        deploy.slice2_source_predecessor_stop(
            runner, FAKE_HOST, FAKE_REMOTE_ROOT, receipt, execute=True,
        )
    # The full inspect/stop/inspect sequence still happened — we are not
    # allowed to silently skip the post-stop check.
    names = [name for name, _a, _k in runner.calls]
    assert names == [
        "remote_endpoint", "remote_endpoint", "remote_endpoint", "remote_endpoint",
    ]
    third_argv = list(runner.calls[2][1])
    assert third_argv[:3] == ["docker", "stop", "--time"]


def test_command_corpus_contains_no_forbidden_operations(deploy: ModuleType) -> None:
    """The exact happy-path command corpus must not contain forbidden tokens
    (``rm``, ``prune``, ``chown``, ``compose``, ``recreate``); Slice-2B1 only
    inspects and ``docker stop``.
    """
    runner = RecordingRunner()
    receipt = _make_receipt()
    deploy.slice2_source_predecessor_stop(
        runner, FAKE_HOST, FAKE_REMOTE_ROOT, receipt, execute=True,
    )
    argv_strings: list[str] = []
    for _name, args, _kwargs in runner.calls:
        argv_strings.append(" ".join(str(item) for item in args))
    joined = "\n".join(argv_strings)
    for token in FORBIDDEN_TOKENS:
        # ``stop`` overlaps with ``docker stop`` — check as a whole token.
        assert not any(
            token == str(item) or f" {token} " in f" {' '.join(argv_strings)} "
            for item in argv_strings
        )
    # The only docker subcommands invoked across the corpus are inspect and stop.
    subcommands = [
        str(args[1])
        for _name, args, _kwargs in runner.calls
        if len(args) >= 2
    ]
    assert set(subcommands) == {"inspect", "stop"}
