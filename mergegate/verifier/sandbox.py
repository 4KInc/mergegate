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
showed all outbound TCP blocked and DNS still resolving.

**That posture then had to be widened by one destination, on purpose.** A
totally sealed network cannot mount the Cloud Storage volume the job receives
its inputs on, because gcsfuse dials ``storage.googleapis.com`` from inside the
graded namespace; the first live run failed at mount with an i/o timeout. One
egress rule to Google's restricted API VIP restores the input path, and
:data:`EGRESS_DENY_TCP` names that exception rather than rounding it off. The
probe is :mod:`mergegate.verifier.egress_probe`, kept re-runnable precisely
because this claim has now changed twice.
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

EGRESS_UNRESTRICTED = "unrestricted; graded in-process, not in the sealed sandbox"
"""What is true when the grader runs wherever the caller runs.

This exists because the alternative was worse. ``VerificationManifest`` used to
default its ``egress_policy`` to :data:`EGRESS_DENY_TCP`, and that field is
written into a signed receipt. Nothing in this package dispatches to Cloud Run:
:func:`build_job_request` builds a job request that no caller submits, and
grading actually happens through ``runner.run_pinned_commands``, in the calling
process. So every receipt issued so far asserted a sandbox posture that was not
a property of the environment that produced it.

That is precisely the mistake this module documents having made once already,
one level up: the first version asserted ``default-deny`` and a probe disproved
it. Asserting a posture for a sandbox that never ran is the same error wearing
the fix as a costume.

A manifest now has to be *told* it was sealed. The default states the weaker
truth, so a receipt understates its isolation rather than overstating it.
"""

EGRESS_DENY_TCP = (
    "deny-tcp-egress-except-google-restricted-vip-199.36.153.4/30; dns-resolution-available"
)
"""The network posture that was **measured**, not the one we wanted to claim.

Verified by :mod:`mergegate.verifier.egress_probe` executing inside the real
sealed Cloud Run Job. Outbound TCP to the public internet fails — both to an
unrelated address and to a Google *public* address — while loopback succeeds,
so a graded run cannot fetch from the open network.

**Why this is not a flat deny, and what that costs.** An earlier version of this
string was ``deny-tcp-egress``, and it was briefly true. It stopped being true
the moment the job started receiving its inputs on a Cloud Storage volume:
gcsfuse dials ``storage.googleapis.com`` from inside the same network namespace
as the graded code, so a flat deny fails the mount and the job never starts.
"Deny all egress" and "mount a bucket" cannot both hold. The mount was restored
by allowing exactly one destination, Google's restricted API VIP, and this
string names that destination rather than rounding it away.

So the honest statement of the residual surface is two channels, not one:

* **The restricted VIP.** Graded code can open a socket to Google's API
  front-end. It has no cloud credentials with which to do anything there, but
  "unauthenticated" is a weaker claim than "unreachable" and is stated as such.
* **DNS.** Cloud Run resolves through the platform rather than through the VPC,
  so the firewall does not reach it. A submission cannot retrieve data over it,
  but it could signal outward through crafted lookups.

Both are disclosed here and carried into the signed receipt, because this field
is signed and a posture that flatters itself is the one failure this module
exists to prevent.
"""


EGRESS_PROBE = {
    "job": "mergegate-verifier",
    "method": (
        "mergegate.verifier.egress_probe, executed inside the same sealed Cloud Run "
        "Job that grades submissions, on the same pinned image. Each result is a bit "
        "of the exit code as well as structured stdout: an early configuration "
        "surfaced no output at all, so the exit status is kept as the channel that "
        "survives logging breaking. A loopback control bit is included so a broken "
        "probe reports itself instead of masquerading as a perfect seal."
    ),
    # Destination -> (reachable before the sealed VPC, reachable now)
    "results": [
        ("loopback (control)", True, True),
        ("1.1.1.1:443", True, False),
        ("142.250.72.46:443", False, False),
        ("199.36.153.4:443 (restricted Google API VIP)", True, True),
        ("DNS resolution", True, True),
    ],
    "before_exit_code": 21,
    "after_exit_code": 25,
}
"""What the egress probe actually returned, before and after the sealed VPC.

Recorded here rather than retyped into a template so the page cannot drift from
what was measured. Two measurements are worth reading in order.

The first configuration reached the public internet, because Cloud Run grants
egress by default — which is why an earlier version of this module asserting
"default-deny" would have signed a false claim.

The second is the row that is *deliberately* still reachable. Sealing the VPC
completely broke the Cloud Storage volume the job receives its inputs on, so
one destination is allowed: Google's restricted API VIP. The exit code moved
from 17 to 25 when that rule was added, and that difference is the honest cost
of the input path, left visible here instead of being smoothed over.
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
