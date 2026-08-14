"""Runtime guard: provider code may not read the buyer's grader.

The path guard stops a provider *editing* the tests. It does nothing about
reading them, and reading is enough: code that implements nothing can scrape the
expected values out of the test file at import time and answer from a lookup
table. That submission passes every assertion while containing no
implementation, which makes the verdict meaningless.

Demonstrated before this existed, not hypothesised. See
``tests/test_grader_confidentiality.py``.

The defense is a CPython audit hook installed from a ``sitecustomize`` module on
``PYTHONPATH``, placed in a directory **outside** the graded workspace:

* ``sitecustomize`` is imported by ``site`` during interpreter startup, before
  any test or provider module runs, so the hook is in place first.
* Audit hooks cannot be removed once added. Provider code that imports this
  module cannot uninstall it.
* Living outside the workspace means the provider's diff cannot reach it, and
  the grader-path purge cannot delete it.

The hook only blocks reads of grader paths *by frames belonging to provider
source paths*. pytest reading its own test files runs from site-packages and is
unaffected, and the grader reading its own fixtures is likewise fine. The
narrowness matters: a blanket ban on opening grader files would break pytest
itself.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["GUARD_DIR_NAME", "write_guard", "SOURCE_ROOTS_VAR", "GRADER_ROOTS_VAR"]

GUARD_DIR_NAME = "_mergegate_guard"
SOURCE_ROOTS_VAR = "MERGEGATE_SOURCE_ROOTS"
GRADER_ROOTS_VAR = "MERGEGATE_GRADER_ROOTS"

_SITECUSTOMIZE = '''\
"""Installed by MergeGate. Blocks provider code from reading the graded tests.

Not part of the submission and not reachable from it: this file lives outside
the workspace and is loaded at interpreter startup.
"""
import os
import sys


def _roots(name):
    raw = os.environ.get(name, "")
    return tuple(os.path.abspath(p) for p in raw.split(os.pathsep) if p)


_GRADER = _roots("MERGEGATE_GRADER_ROOTS")
_SOURCE = _roots("MERGEGATE_SOURCE_ROOTS")


def _under(path, roots):
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def _hook(event, args):
    # Only file opens matter, and only when the target is a grader path. The
    # stack walk below is comparatively expensive, so it is gated on that.
    if event != "open" or not args or not _GRADER:
        return
    try:
        target = os.path.abspath(os.fspath(args[0]))
    except (TypeError, ValueError):
        return
    if not _under(target, _GRADER):
        return

    # Walk the live stack rather than capturing a traceback: cheaper, and it
    # sees the real caller chain including the module executing at import time.
    frame = sys._getframe()
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename and _under(os.path.abspath(filename), _SOURCE):
            raise PermissionError(
                "MergeGate: provider source may not read the buyer's grader "
                "(%s). Reading the graded tests at run time lets a submission "
                "answer from them instead of implementing anything."
                % os.path.basename(target)
            )
        frame = frame.f_back


sys.addaudithook(_hook)
'''


def write_guard(
    parent: Path, *, source_roots: list[Path], grader_roots: list[Path]
) -> dict[str, str]:
    """Write the guard module and return the environment it needs.

    ``parent`` must be outside the graded workspace. Returns the environment
    additions the runner should apply: ``PYTHONPATH`` plus the two root lists
    the hook reads.
    """
    guard_dir = parent / GUARD_DIR_NAME
    guard_dir.mkdir(parents=True, exist_ok=True)
    (guard_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    return {
        "PYTHONPATH": str(guard_dir),
        SOURCE_ROOTS_VAR: os.pathsep.join(str(p.resolve()) for p in source_roots),
        GRADER_ROOTS_VAR: os.pathsep.join(str(p.resolve()) for p in grader_roots),
    }
