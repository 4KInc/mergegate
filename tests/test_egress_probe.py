"""The probe's own reporting logic, tested without a network.

:func:`~mergegate.verifier.egress_probe.probe` is deliberately not called here.
It measures the machine it runs on, and on a developer laptop the public
internet *is* reachable, so a test that asserted the sealed posture locally
would fail for the one reason that means nothing. The measurement belongs in
the sealed job; what belongs in CI is the arithmetic that turns a measurement
into an exit code, because a wrong bit order would misreport a seal.
"""

from __future__ import annotations

from mergegate.verifier.egress_probe import DESTINATIONS, ProbeReport


def _row(label: str, *, reachable: bool, expected: bool) -> dict[str, object]:
    return {
        "label": label,
        "reachable": reachable,
        "expected": expected,
        "agrees": reachable == expected,
        "meaning": "",
    }


def test_each_reachable_destination_sets_its_own_bit_lowest_first() -> None:
    report = ProbeReport(
        (
            _row("a", reachable=True, expected=True),
            _row("b", reachable=False, expected=False),
            _row("c", reachable=True, expected=True),
        )
    )
    assert report.exit_code == 0b101


def test_an_unreachable_run_is_exit_zero_not_a_silent_success() -> None:
    """Zero means "reached nothing", which is why the control bit exists.

    Without a loopback control, a probe that crashed before opening a socket
    and a probe that proved a perfect seal would both report 0.
    """
    report = ProbeReport(tuple(_row(x, reachable=False, expected=False) for x in "abc"))
    assert report.exit_code == 0


def test_the_first_destination_is_a_loopback_control() -> None:
    assert "control" in DESTINATIONS[0].label
    assert DESTINATIONS[0].expected is True


def test_a_destination_that_should_be_blocked_but_is_not_fails_the_posture() -> None:
    report = ProbeReport(
        (
            _row("loopback (control)", reachable=True, expected=True),
            _row("1.1.1.1:443", reachable=True, expected=False),
        )
    )
    assert report.matches_expected_posture is False


def test_a_dead_control_fails_the_posture_even_when_everything_is_blocked() -> None:
    """The failure mode this whole design exists to catch.

    Every destination unreachable looks like a perfect seal. It is only
    distinguishable from a broken probe by the control bit, so a dead control
    has to fail loudly rather than report the strongest possible result.
    """
    report = ProbeReport(
        (
            _row("loopback (control)", reachable=False, expected=True),
            _row("1.1.1.1:443", reachable=False, expected=False),
        )
    )
    assert report.exit_code == 0
    assert report.matches_expected_posture is False


def test_the_declared_posture_admits_the_restricted_vip() -> None:
    """The input mount depends on it, so it must not quietly become blocked.

    If someone tightens the firewall back to a flat deny, the job stops being
    able to receive inputs at all. Pinning the expectation here means that
    change shows up as a failing test rather than as a job that dies at mount.
    """
    vip = next(d for d in DESTINATIONS if d.host == "199.36.153.4")
    assert vip.expected is True
    assert not any(d.expected for d in DESTINATIONS if d.host in {"1.1.1.1", "142.250.72.46"})
