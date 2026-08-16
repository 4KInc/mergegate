"""Measures what the sealed job can actually reach, from inside the sealed job.

This exists because :data:`~mergegate.verifier.sandbox.EGRESS_DENY_TCP` is
written into a *signed receipt*. A network posture that was reasoned about
rather than measured is a false statement under a signature, and this module is
how that claim stays honest across configuration changes.

It has already caught the claim drifting twice.

1. The first configuration asserted ``default-deny`` while the job could reach
   Cloudflare, because Cloud Run grants egress by default.
2. Sealing the VPC broke the Cloud Storage volume the job receives its inputs
   on. gcsfuse dials ``storage.googleapis.com`` from inside the same network
   namespace as the graded code, so "deny all egress" and "mount a bucket" are
   not simultaneously satisfiable. The mount was restored by allowing exactly
   one destination — Google's restricted API VIP, ``199.36.153.4/30`` — which
   widened the posture the receipt was still describing as a flat deny.

The second is the reason this module is a module and not a shell one-liner. The
opened path is narrow and deliberate, but it is *real*, and a probe that only
ever ran once would have left the receipt asserting the pre-change posture.

**Results are reported two ways.** Exit-code bits came first, because an early
Cloud Run configuration surfaced no output at all and the exit status was the
only channel. Structured stdout was added once logs proved reachable. The bits
are kept: they are what survives if logging breaks, and a probe whose only
channel can fail silently is a probe that can report success by not running.

Bit 0 is a loopback control. A probe that cannot reach *itself* is broken, and
a broken probe must say so rather than report every destination unreachable —
which is indistinguishable from a perfect seal and would be read as one.
"""

from __future__ import annotations

import json
import socket
import sys
from dataclasses import dataclass

__all__ = ["Destination", "DESTINATIONS", "ProbeReport", "probe", "main"]

TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Destination:
    """One thing to try to reach, and what reaching it would mean."""

    label: str
    host: str
    port: int
    expected: bool
    """What the current configuration is supposed to allow.

    Recorded per destination so the probe reports a *disagreement* rather than
    a table a reader has to interpret. The interesting output is not "TCP to
    1.1.1.1 failed", it is "something we expected to be blocked was not".
    """

    meaning: str


DESTINATIONS: tuple[Destination, ...] = (
    Destination(
        "loopback (control)",
        "127.0.0.1",
        0,  # filled in at runtime by the listener this probe opens
        True,
        "the probe itself works; without this a dead probe looks like a seal",
    ),
    Destination(
        "1.1.1.1:443",
        "1.1.1.1",
        443,
        False,
        "public internet by address, bypassing DNS",
    ),
    Destination(
        "142.250.72.46:443",
        "142.250.72.46",
        443,
        False,
        "a Google public address that is NOT the restricted VIP",
    ),
    Destination(
        "199.36.153.4:443",
        "199.36.153.4",
        443,
        True,
        "the restricted Google API VIP the input mount depends on",
    ),
)


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _dns_resolves(name: str = "storage.googleapis.com") -> tuple[bool, str]:
    """Whether DNS answers, and what it answers with.

    The address matters as much as the success. A private zone maps
    ``*.googleapis.com`` onto the restricted VIP; if that zone were removed the
    name would resolve to a public address, the mount would fail, and the cause
    would be invisible from a boolean.
    """
    try:
        return True, socket.gethostbyname(name)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """What the probe found, in a shape the caller cannot misread.

    Typed rather than a bare dict because :meth:`exit_code` is what the
    orchestrator reads. A report that has to be indexed by string is one typo
    away from returning ``None`` where a posture was expected.
    """

    results: tuple[dict[str, object], ...]

    @property
    def exit_code(self) -> int:
        """Each reachable destination sets one bit, lowest bit first."""
        return sum(1 << i for i, r in enumerate(self.results) if r["reachable"])

    @property
    def matches_expected_posture(self) -> bool:
        return all(bool(r["agrees"]) for r in self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "results": list(self.results),
            "exit_code": self.exit_code,
            "matches_expected_posture": self.matches_expected_posture,
        }


def probe() -> ProbeReport:
    """Try every destination. Never raises; a probe that dies reports nothing."""
    results: list[dict[str, object]] = []

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        control_port = listener.getsockname()[1]

        for destination in DESTINATIONS:
            port = control_port if destination.port == 0 else destination.port
            reached = _reachable(destination.host, port)
            results.append(
                {
                    "label": destination.label,
                    "reachable": reached,
                    "expected": destination.expected,
                    "agrees": reached == destination.expected,
                    "meaning": destination.meaning,
                }
            )

    dns_ok, dns_answer = _dns_resolves()
    results.append(
        {
            "label": "DNS resolution",
            "reachable": dns_ok,
            "expected": True,
            "agrees": dns_ok,
            "meaning": f"resolves to {dns_answer}; a residual outbound signalling channel",
        }
    )

    return ProbeReport(tuple(results))


def main() -> int:
    report = probe()
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if not report.matches_expected_posture:
        print(
            "mergegate-probe: MEASURED POSTURE DISAGREES WITH THE DECLARED ONE",
            file=sys.stderr,
        )
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - container entry
    raise SystemExit(main())
