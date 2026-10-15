# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated subprocess execution of a single Python card (Phase C - Pillar 1).

A card is executed in a fresh, isolated interpreter (``python -I -B -c <code>``)
with a **hard timeout** (all platforms), a cleaned environment, a throw-away
working directory and - on POSIX only - best-effort ``RLIMIT_AS`` / ``RLIMIT_CPU``
caps.

This is **convenience isolation, not an adversarial sandbox**: network egress is
**not** blocked, in-memory rlimits do not apply on Windows, and the child's
filesystem access is **not** confined (it can read and write anywhere the searx
process has OS permission).  Every entry point is fail-safe: it returns an
:py:class:`ExecutionResult` and never raises.
"""

__all__ = ["ExecutionResult", "run"]

import os
import shutil
import subprocess
import sys
import tempfile
import time
import typing as t
from dataclasses import dataclass

from searx import logger

log = logger.getChild("searx.sandbox.runner")

_DEFAULT_MAX_OUTPUT_BYTES: int = 65536
_DEFAULT_TIMEOUT: float = 5.0
_KEEP_ENV: frozenset[str] = frozenset({"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL"})


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of a card execution."""

    ok: bool
    stdout: str
    stderr: str
    returncode: int
    duration_ms: int
    timed_out: bool
    error: str | None


def _clean_env() -> dict[str, str]:
    """Return a minimal environment, dropping proxies and Python overrides."""
    env: dict[str, str] = {}
    try:
        for key, value in os.environ.items():
            upper = key.upper()
            if upper.endswith("_PROXY") or upper.startswith("PYTHON"):
                continue
            if upper in _KEEP_ENV:
                env[key] = value
    except Exception:  # pylint: disable=broad-except
        return {"PATH": os.environ.get("PATH", "")}
    return env


def _preexec(memory_limit_mb: int) -> t.Callable[[], None] | None:
    """Return a POSIX pre-exec hook capping address space / CPU, or ``None``."""
    try:
        import resource  # noqa: PLC0415 - POSIX only, imported lazily
    except ImportError:  # pragma: no cover - Windows
        return None
    mem_bytes = max(16, int(memory_limit_mb)) * 1024 * 1024
    # ``resource`` is Unix-only: resolve via getattr so the win32 typeshed does
    # not statically flag RLIMIT_* / setrlimit as missing, and so a platform
    # that ships ``resource`` without these symbols degrades to no-op.
    rlimit_as = getattr(resource, "RLIMIT_AS", None)
    rlimit_cpu = getattr(resource, "RLIMIT_CPU", None)
    setrlimit = getattr(resource, "setrlimit", None)
    if setrlimit is None or rlimit_as is None:
        return None

    def _limit() -> None:  # pragma: no cover - runs in the child process
        try:
            setrlimit(rlimit_as, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass
        try:
            cpu = int(os.environ.get("SEARX_SANDBOX_CPU_S", "0") or 0)
            if cpu > 0 and rlimit_cpu is not None:
                setrlimit(rlimit_cpu, (cpu, cpu + 1))
        except (ValueError, OSError):
            pass

    return _limit


def _truncate(data: bytes, limit: int) -> str:
    if len(data) > limit:
        data = data[:limit] + b" ...[truncated]"
    return data.decode("utf-8", errors="replace")


def run(
    code: str,
    *,
    language: str = "python",
    timeout: float = _DEFAULT_TIMEOUT,
    memory_limit_mb: int = 128,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> ExecutionResult:
    """Execute ``code`` in an isolated subprocess (never raises)."""
    start = time.monotonic()
    workdir: str | None = None
    try:
        if not str(code or "").strip():
            return ExecutionResult(False, "", "", -1, 0, False, "empty code")
        try:
            timeout = max(0.1, float(timeout))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        try:
            max_output_bytes = max(1024, int(max_output_bytes))
        except (TypeError, ValueError):
            max_output_bytes = _DEFAULT_MAX_OUTPUT_BYTES
        workdir = tempfile.mkdtemp(prefix="searx_sandbox_")
        env = _clean_env()
        env["SEARX_SANDBOX_CPU_S"] = str(int(timeout) + 1)
        preexec = _preexec(memory_limit_mb)
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            preexec_fn=preexec,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            ok=proc.returncode == 0,
            stdout=_truncate(proc.stdout or b"", max_output_bytes),
            stderr=_truncate(proc.stderr or b"", max_output_bytes),
            returncode=int(proc.returncode),
            duration_ms=duration_ms,
            timed_out=False,
            error=None if proc.returncode == 0 else "nonzero exit",
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            ok=False,
            stdout=_truncate(e.stdout or b"", max_output_bytes),
            stderr=_truncate(e.stderr or b"", max_output_bytes),
            returncode=-1,
            duration_ms=duration_ms,
            timed_out=True,
            error="timeout",
        )
    except Exception as e:  # pylint: disable=broad-except
        duration_ms = int((time.monotonic() - start) * 1000)
        log.debug("sandbox runner: execution failed: %s", e)
        return ExecutionResult(False, "", "", -1, duration_ms, False, str(e) or "execution error")
    finally:
        if workdir:
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception as e:  # pylint: disable=broad-except
                log.debug("sandbox runner: workdir cleanup failed: %s", e)
