"""Docker-sandboxed Python execution for the ``research.run_code`` tool.

``DockerCodeRunner`` shells out to the ``docker`` CLI with a hardened flag set —
``--cap-drop ALL --network none`` per the plan, plus a read-only rootfs, a
non-root user, no-new-privileges, and memory/pid/cpu limits.  The code is fed to
the container over **stdin** (``python -I -``) so there is no shell-quoting or
injection surface.

The subprocess call is injectable (``runner=``) so command construction and the
failure paths are unit-testable without a running Docker daemon.  When Docker is
absent or the daemon is down, ``run`` fails soft — it returns a result dict with
an ``error`` marker rather than raising, matching the tool contract.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any, Callable, Optional

DEFAULT_IMAGE = "python:3.11-slim"
DEFAULT_TIMEOUT = 30
# Container startup (image already pulled) is near-instant on Linux but can take
# tens of seconds on Docker Desktop's WSL2 backend (Windows/macOS). This is
# added to the code timeout to form the *subprocess* deadline, so slow container
# startup never eats into the user's code-execution budget. The code timeout
# itself is enforced inside the container by coreutils ``timeout`` (below).
DEFAULT_STARTUP_OVERHEAD = 90
# coreutils ``timeout`` exit codes: 124 = deadline hit (SIGTERM), 137 = SIGKILL
# after ``-k`` grace. Both mean the code ran too long.
_TIMEOUT_EXIT_CODES = frozenset({124, 137})


class DockerCodeRunner:
    """Run untrusted Python in a locked-down disposable container."""

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        default_timeout: int = DEFAULT_TIMEOUT,
        memory: str = "256m",
        pids_limit: int = 128,
        cpus: str = "1.0",
        startup_overhead: int = DEFAULT_STARTUP_OVERHEAD,
        runner: Callable[..., Any] = subprocess.run,
        docker_path: Optional[str] = None,
    ) -> None:
        self._image = image
        self._default_timeout = int(default_timeout)
        self._memory = memory
        self._pids_limit = int(pids_limit)
        self._cpus = cpus
        self._startup_overhead = int(startup_overhead)
        self._run = runner
        self._docker = docker_path or shutil.which("docker") or "docker"

    def _build_cmd(self, code_timeout: int) -> list[str]:
        return [
            self._docker, "run", "--rm", "-i",
            "--cap-drop", "ALL",
            "--network", "none",
            "--security-opt", "no-new-privileges",
            "--read-only",                       # immutable rootfs
            "--tmpfs", "/tmp:rw,size=64m,noexec",  # scratch space for the interpreter
            "--memory", self._memory,
            "--memory-swap", self._memory,        # == memory ⇒ no swap
            "--pids-limit", str(self._pids_limit),
            "--cpus", self._cpus,
            "--user", "65534:65534",             # nobody:nogroup
            self._image,
            # coreutils `timeout` bounds the code itself, in-container, so slow
            # container startup doesn't count against the caller's budget. -k 5
            # escalates to SIGKILL 5s after the initial SIGTERM.
            "timeout", "-k", "5", str(code_timeout),
            "python", "-I", "-",                  # isolated mode; read program from stdin
        ]

    def run(self, code: str, timeout_seconds: Optional[int] = None) -> dict:
        code_timeout = int(timeout_seconds) if timeout_seconds else self._default_timeout
        cmd = self._build_cmd(code_timeout)
        try:
            proc = self._run(
                cmd,
                input=code,
                capture_output=True,
                text=True,
                # Subprocess deadline = code budget + startup allowance. The code
                # budget is enforced inside the container; this only guards against
                # a hung/stuck container that never returns.
                timeout=code_timeout + self._startup_overhead,
            )
        except FileNotFoundError:
            return {
                "stdout": "", "stderr": f"docker executable not found ({self._docker})",
                "exit_code": -1, "error": "docker_unavailable",
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "", "stderr": f"container did not return within {code_timeout + self._startup_overhead}s",
                "exit_code": -1, "error": "timeout",
            }

        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        exit_code = getattr(proc, "returncode", -1)

        # Code exceeded its in-container `timeout` budget.
        if exit_code in _TIMEOUT_EXIT_CODES:
            return {
                "stdout": stdout,
                "stderr": stderr or f"execution exceeded {code_timeout}s timeout",
                "exit_code": exit_code, "error": "timeout",
            }

        # Distinguish "the daemon isn't running" (infra) from "the code failed"
        # (a legitimate non-zero exit the caller wants to see).
        if exit_code != 0 and (
            "Cannot connect to the Docker daemon" in stderr
            or "error during connect" in stderr
            or "is not running" in stderr
        ):
            return {
                "stdout": "", "stderr": stderr.strip(),
                "exit_code": exit_code, "error": "docker_unavailable",
            }

        return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}


class DisabledCodeRunner:
    """No-op runner: code execution is turned off.  Deterministic; for tests."""

    def run(self, code: str, timeout_seconds: Optional[int] = None) -> dict:
        return {"stdout": "", "stderr": "code execution disabled", "exit_code": 0, "error": "disabled"}


def build_code_runner() -> DockerCodeRunner:
    """Build the default real Docker-backed runner."""
    return DockerCodeRunner()
