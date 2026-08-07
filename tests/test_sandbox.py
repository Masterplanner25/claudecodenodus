"""Unit tests for the Docker code sandbox (``src/sandbox.py``).

The subprocess call is faked (``runner=``) so these verify command construction,
result mapping, and every failure path without a running Docker daemon.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from src.sandbox import DockerCodeRunner, DisabledCodeRunner
from src.runtime import _ext_run_code


class FakeRunner:
    """Stand-in for ``subprocess.run`` — records the call, returns a canned result
    or raises a preset exception."""

    def __init__(self, *, stdout="", stderr="", returncode=0, raise_exc=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raise_exc = raise_exc
        self.cmd = None
        self.input = None
        self.timeout = None

    def __call__(self, cmd, input=None, capture_output=None, text=None, timeout=None):
        self.cmd = cmd
        self.input = input
        self.timeout = timeout
        if self.raise_exc is not None:
            raise self.raise_exc
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr, returncode=self.returncode)


# ── command construction ─────────────────────────────────────────────────────

def test_command_has_hardening_flags():
    fake = FakeRunner(stdout="ok\n")
    runner = DockerCodeRunner(runner=fake, docker_path="docker")
    runner.run("print('ok')")
    cmd = fake.cmd
    joined = " ".join(cmd)
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--cap-drop" in cmd and "ALL" in cmd
    assert "--network" in cmd and "none" in cmd
    assert "no-new-privileges" in joined
    assert "--read-only" in cmd
    assert "--pids-limit" in cmd
    assert "--memory" in cmd
    # program is read from stdin in isolated mode
    assert cmd[-3:] == ["python", "-I", "-"]
    # code timeout is enforced in-container by coreutils `timeout`
    ti = cmd.index("timeout")
    assert cmd[ti:ti + 4] == ["timeout", "-k", "5", "30"]  # default_timeout=30


def test_code_is_passed_over_stdin():
    fake = FakeRunner(stdout="")
    runner = DockerCodeRunner(runner=fake)
    runner.run("print(2 + 2)")
    assert fake.input == "print(2 + 2)"


def test_custom_limits_applied():
    fake = FakeRunner()
    runner = DockerCodeRunner(runner=fake, memory="512m", pids_limit=64, cpus="2.0")
    runner.run("pass")
    cmd = fake.cmd
    assert cmd[cmd.index("--memory") + 1] == "512m"
    assert cmd[cmd.index("--pids-limit") + 1] == "64"
    assert cmd[cmd.index("--cpus") + 1] == "2.0"


# ── result mapping ───────────────────────────────────────────────────────────

def test_success_result():
    fake = FakeRunner(stdout="hello\n", stderr="", returncode=0)
    out = DockerCodeRunner(runner=fake).run("print('hello')")
    assert out == {"stdout": "hello\n", "stderr": "", "exit_code": 0}


def test_nonzero_exit_is_passed_through():
    # A traceback in the container is a legitimate result, not an infra error.
    fake = FakeRunner(stdout="", stderr="Traceback...\nValueError\n", returncode=1)
    out = DockerCodeRunner(runner=fake).run("raise ValueError")
    assert out["exit_code"] == 1
    assert "ValueError" in out["stderr"]
    assert "error" not in out


# ── failure paths ────────────────────────────────────────────────────────────

def test_docker_missing_fails_soft():
    fake = FakeRunner(raise_exc=FileNotFoundError())
    out = DockerCodeRunner(runner=fake, docker_path="docker").run("print(1)")
    assert out["exit_code"] == -1
    assert out["error"] == "docker_unavailable"


def test_hung_container_fails_soft():
    # subprocess-level timeout = the container never returned (hung), distinct
    # from the code exceeding its own in-container budget (exit 124, below).
    fake = FakeRunner(raise_exc=subprocess.TimeoutExpired(cmd="docker", timeout=95))
    out = DockerCodeRunner(runner=fake).run("while True: pass", timeout_seconds=5)
    assert out["exit_code"] == -1
    assert out["error"] == "timeout"


def test_code_timeout_exit_124_is_timeout():
    # coreutils `timeout` inside the container returns 124 when it kills the code.
    fake = FakeRunner(returncode=124, stdout="partial\n")
    out = DockerCodeRunner(runner=fake).run("while True: pass", timeout_seconds=5)
    assert out["exit_code"] == 124
    assert out["error"] == "timeout"
    assert "5s" in out["stderr"]


def test_daemon_down_detected_from_stderr():
    fake = FakeRunner(
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        returncode=1,
    )
    out = DockerCodeRunner(runner=fake).run("print(1)")
    assert out["error"] == "docker_unavailable"


def test_subprocess_timeout_adds_startup_overhead():
    fake = FakeRunner()
    DockerCodeRunner(runner=fake, default_timeout=30, startup_overhead=90).run("pass")
    # subprocess deadline = code budget + startup allowance, so slow container
    # startup never eats into the caller's code-execution budget.
    assert fake.timeout == 120


# ── disabled runner + dispatch wrapper ───────────────────────────────────────

def test_disabled_runner():
    out = DisabledCodeRunner().run("print(1)")
    assert out["exit_code"] == 0
    assert out["error"] == "disabled"


def test_ext_run_code_defaults_to_disabled():
    out = _ext_run_code({"code": "print(1)"}, code_runner=None)
    assert out["error"] == "disabled"


def test_ext_run_code_uses_injected_runner():
    fake = FakeRunner(stdout="42\n", returncode=0)
    out = _ext_run_code({"code": "print(42)", "timeout_seconds": 10}, code_runner=DockerCodeRunner(runner=fake))
    assert out["stdout"] == "42\n"
    assert fake.input == "print(42)"
