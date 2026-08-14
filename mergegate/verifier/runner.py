"""Execution of the buyer's pinned commands.

Only the commands named in the contract run, in the order the contract names
them, as argv vectors through :mod:`subprocess` with ``shell=False``. There is
no shell to quote for and no string for a provider to inject into.

The environment is rebuilt from an allow-list rather than inherited. This serves
two purposes at once: the run cannot pick up a secret from the verifier's own
environment (P1.4), and there is very little for a submission to sniff in order
to detect that it is being graded (P1.5).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .manifest import MAX_RETAINED_OUTPUT, CommandResult, digest_stream

__all__ = ["run_pinned_commands", "build_sealed_env", "ENV_ALLOWLIST"]

# Everything the interpreter genuinely needs, and nothing else. Notably absent:
# cloud credentials, tokens, the verifier's own project/service metadata, and
# any CI variable that would announce the harness to a submission.
ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TZ")

DEFAULT_TIMEOUT_SECONDS = 600


def build_sealed_env(*, workspace: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Construct the minimal environment the pinned commands run under."""
    env: dict[str, str] = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("TZ", "UTC")
    # Determinism: hash randomization and stray bytecode would both make an
    # otherwise identical run produce a different tree and different output.
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Point the run at the workspace explicitly rather than relying on cwd
    # inheritance, and keep the user's site-packages out of it.
    env["PYTHONNOUSERSITE"] = "1"
    # PYTHONPATH is deliberately settable by the caller: it is how the runtime
    # guard's sitecustomize module gets imported at interpreter startup. It is
    # not inherited from the ambient environment.
    env["HOME"] = str(workspace)
    if extra:
        env.update(extra)
    return env


def run_pinned_commands(
    *,
    workspace: Path,
    commands: tuple[tuple[str, ...], ...],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    env_extra: dict[str, str] | None = None,
) -> tuple[CommandResult, ...]:
    """Run each pinned command in ``workspace``; stop at the first failure.

    Stopping early is intentional. Once a required command has failed the
    verdict is already FAIL, and continuing would only spend sandbox time
    running further attacker-influenced code.
    """
    env = build_sealed_env(workspace=workspace, extra=env_extra)
    results: list[CommandResult] = []

    for argv in commands:
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(  # noqa: S603 - argv vector, shell=False, pinned by contract
                list(argv),
                cwd=workspace,
                env=env,
                capture_output=True,
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
            stdout, stderr = completed.stdout, completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            # A timeout is a failure with a distinct cause, not exit code 0.
            exit_code = 124
        except (OSError, ValueError) as exc:
            # An unrunnable pinned command is a contract problem, not a pass.
            stdout, stderr = b"", str(exc).encode()
            exit_code = 127

        duration_ms = int((time.monotonic() - started) * 1000)
        result = CommandResult(
            argv=tuple(argv),
            exit_code=exit_code,
            stdout_digest=digest_stream(stdout),
            stderr_digest=digest_stream(stderr),
            duration_ms=duration_ms,
            stdout_excerpt=_excerpt(stdout),
            stderr_excerpt=_excerpt(stderr),
            timed_out=timed_out,
        )
        results.append(result)
        if not result.ok:
            break

    return tuple(results)


def _excerpt(data: bytes) -> str:
    """Retain a bounded, human-readable tail of a stream for the dashboard.

    The digest above covers the full stream; this is display only, so it is
    truncated and decoded leniently rather than trusted.
    """
    if len(data) > MAX_RETAINED_OUTPUT:
        data = data[-MAX_RETAINED_OUTPUT:]
    return data.decode("utf-8", errors="replace")
