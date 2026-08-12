"""P1.4 — the sandbox execution spec for Cloud Run Jobs.

The isolation properties are expressed as a data structure rather than as
scattered API arguments, for two reasons: they can be asserted in tests without
a GCP project, and the spec that gets asserted is the same object that gets
submitted. A test that checks a constant which the deploy path then ignores
proves nothing.

Cloud Run's second-generation execution environment runs workloads under gVisor,
which is the isolation boundary this relies on. Everything else here narrows
what the graded run can reach inside that boundary.

**Deployment status:** the spec and the rendered request body below are
exercised by tests. Nothing here has yet been submitted to a live Cloud Run
API — there is no GCP project wired up. The README status table says so rather
than describing this as working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["SandboxSpec", "SandboxPolicyError", "build_job_request"]


class SandboxPolicyError(ValueError):
    """The requested sandbox configuration would weaken a stated guarantee."""


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """Everything that constrains one evaluation run.

    Defaults are the strict posture. Each field that could weaken a guarantee is
    validated rather than trusted, so a future caller cannot quietly relax
    isolation by passing a different argument.
    """

    image_digest: str
    """Pinned by digest, taken from the contract. Never a tag."""

    argv: tuple[str, ...]
    cpu: str = "2"
    memory: str = "4Gi"
    timeout_seconds: int = 600
    max_processes: int = 512
    egress: str = "default-deny"
    execution_environment: str = "gen2"
    """gen2 is the gVisor-sandboxed environment. gen1 is not accepted."""

    service_account: str = ""
    """Intentionally empty by default. The graded run needs no cloud identity,
    and an attached one is a credential inside the sandbox."""

    writable_paths: tuple[str, ...] = ("/workspace",)
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if "@sha256:" not in self.image_digest:
            raise SandboxPolicyError(
                f"verifier image must be pinned by digest, got {self.image_digest!r} — "
                "a tag would let the graded environment drift after funding"
            )
        if not self.argv:
            raise SandboxPolicyError("a sandbox run needs a pinned argv vector")
        if self.egress != "default-deny":
            raise SandboxPolicyError(
                f"egress policy {self.egress!r} is not permitted. Network-dependent "
                "tests are out of scope because they are not deterministic, and a "
                "release condition has to be reproducible."
            )
        if self.execution_environment != "gen2":
            raise SandboxPolicyError(
                f"execution environment {self.execution_environment!r} is not "
                "permitted — gen2 is the gVisor-sandboxed environment"
            )
        if self.service_account:
            raise SandboxPolicyError(
                "the graded run must not carry a service account: an attached "
                "identity is a credential reachable from inside the sandbox"
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3600:
            raise SandboxPolicyError(
                f"timeout must be between 1 and 3600 seconds, got {self.timeout_seconds}"
            )
        for key, _value in self.env:
            if _looks_like_a_secret(key):
                raise SandboxPolicyError(
                    f"refusing to pass {key!r} into the sandbox — no secrets in the "
                    "graded environment"
                )


def _looks_like_a_secret(key: str) -> bool:
    upper = key.upper()
    markers = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "API", "AUTH")
    return any(marker in upper for marker in markers)


def build_job_request(
    spec: SandboxSpec, *, project: str, region: str, job_name: str
) -> dict[str, Any]:
    """Render the spec as a Cloud Run Jobs request body.

    Kept as a plain dict so the exact submitted shape is inspectable in a test
    and reviewable in a diff.
    """
    return {
        "parent": f"projects/{project}/locations/{region}",
        "jobId": job_name,
        "job": {
            "template": {
                "taskCount": 1,
                "template": {
                    "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
                    "maxRetries": 0,  # a retried evaluation is a second evaluation
                    "timeout": f"{spec.timeout_seconds}s",
                    "containers": [
                        {
                            "image": spec.image_digest,
                            "command": list(spec.argv[:1]),
                            "args": list(spec.argv[1:]),
                            "resources": {"limits": {"cpu": spec.cpu, "memory": spec.memory}},
                            "env": [{"name": k, "value": v} for k, v in spec.env],
                        }
                    ],
                },
            }
        },
    }
