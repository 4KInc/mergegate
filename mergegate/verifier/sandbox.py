"""P1.4: the sandbox execution spec for Cloud Run Jobs.

The isolation properties are expressed as a data structure rather than as
scattered API arguments, for two reasons: they can be asserted in tests without
a GCP project, and the spec that gets asserted is the same object that gets
submitted. A test that checks a constant which the deploy path then ignores
proves nothing.

Cloud Run's second-generation execution environment runs workloads under gVisor,
which is the isolation boundary this relies on. Everything else here narrows
what the graded run can reach inside that boundary.

**How the egress posture was established.** It was measured, not assumed. A
probe executed inside a real Cloud Run Job showed that the default
configuration reaches the open internet (Cloud Run grants egress by default),
which meant an earlier version of this module asserted ``default-deny`` while
the deployed job could reach Cloudflare and resolve DNS. Since the manifest
writes this field into a *signed receipt*, that would have signed a false
statement.

The fix was a custom VPC with no Cloud NAT plus an explicit deny-all egress
firewall rule, attached with ``--vpc-egress=all-traffic``. Re-probing then
showed all outbound TCP blocked and DNS still resolving, which is exactly what
:data:`EGRESS_DENY_TCP` now claims: no more, no less.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SandboxSpec",
    "SandboxPolicyError",
    "build_job_request",
    "EGRESS_DENY_TCP",
    "EGRESS_PROBE",
]

EGRESS_DENY_TCP = "deny-tcp-egress; dns-resolution-available"
"""The network posture that was **measured**, not the one we wanted to claim.

Verified by executing a probe inside a real Cloud Run Job on the sealed VPC:
outbound TCP to three separate public addresses all failed while loopback
succeeded, so a graded run cannot fetch anything. DNS resolution still
succeeds: Cloud Run resolves through the platform rather than through the VPC,
so the deny-all egress firewall does not reach it.

DNS is therefore a residual side channel: a submission cannot retrieve data over
it, but it could signal outward through crafted lookups. That is a stated limit
of v1, disclosed here and in the receipt, rather than a gap papered over by
calling this "default-deny".
"""


EGRESS_PROBE = {
    "job": "mergegate-egress-probe",
    "method": (
        "A probe executed inside a real Cloud Run Job, encoding each result as a "
        "bit of its exit code. Cloud Run surfaced neither stdout nor stderr, so the "
        "exit code was the only channel; a loopback control bit was included so a "
        "broken probe reports itself instead of masquerading as a passing guarantee."
    ),
    # Destination -> (reachable before the sealed VPC, reachable after)
    "results": [
        ("loopback (control)", True, True),
        ("1.1.1.1:443", True, False),
        ("142.250.72.46:443", False, False),
        ("93.184.216.34:80", False, False),
        ("DNS resolution", True, True),
    ],
    "before_exit_code": 21,
    "after_exit_code": 17,
}
"""What the egress probe actually returned, before and after the sealed VPC.

Recorded here rather than retyped into a template so the page cannot drift from
what was measured. The first configuration reached the public internet: Cloud
Run grants egress by default, which is why an earlier version of this module
asserting "default-deny" would have signed a false claim.
"""


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
    egress: str = EGRESS_DENY_TCP
    """The measured posture, not an aspiration. See :data:`EGRESS_DENY_TCP`."""

    network: str = "mergegate-sealed"
    subnet: str = "mergegate-sealed-uc1"
    """Direct VPC egress into a network with no Cloud NAT and an explicit
    deny-all egress firewall rule. Without these the job reaches the open
    internet, which is Cloud Run's default, and the egress claim would be false."""

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
                f"verifier image must be pinned by digest, got {self.image_digest!r}: "
                "a tag would let the graded environment drift after funding"
            )
        if not self.argv:
            raise SandboxPolicyError("a sandbox run needs a pinned argv vector")
        if self.egress != EGRESS_DENY_TCP:
            raise SandboxPolicyError(
                f"egress policy {self.egress!r} is not permitted. Network-dependent "
                "tests are out of scope because they are not deterministic, and a "
                "release condition has to be reproducible."
            )
        if not self.network or not self.subnet:
            raise SandboxPolicyError(
                "a sealed run requires the VPC network and subnet that carry the "
                "deny-all egress rule. Cloud Run reaches the open internet by "
                "default, so omitting these would make the egress claim false."
            )
        if self.execution_environment != "gen2":
            raise SandboxPolicyError(
                f"execution environment {self.execution_environment!r} is not "
                "permitted: gen2 is the gVisor-sandboxed environment"
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
                    f"refusing to pass {key!r} into the sandbox: no secrets in the "
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
                    # all-traffic forces every packet through the sealed VPC.
                    # private-ranges-only would leave public egress intact.
                    "vpcAccess": {
                        "networkInterfaces": [{"network": spec.network, "subnetwork": spec.subnet}],
                        "egress": "ALL_TRAFFIC",
                    },
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
