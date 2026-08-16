"""The entry point that runs *inside* the sealed Cloud Run job.

Until this existed, grading happened in whatever process called ``evaluate``,
while the receipt described a sandbox that never ran. The job request was built
and its egress probed; nothing submitted it. This module is the thing that gets
submitted.

**How inputs cross a boundary with no network.** The job has no TCP egress, so
it cannot fetch anything itself. Inputs arrive on a Cloud Storage volume that
the *platform* mounts: the fetch happens outside the graded process entirely,
which is what lets the seal stay closed while still handing the run a base tree,
a grader bundle and a diff. Results leave the same way.

**What this deliberately does not trust.** The request names expected hashes,
and this module recomputes them rather than believing them. A caller that
tampered with the mounted inputs after signing the request would produce a
manifest whose digests disagree, and the orchestrator rejects that. The job is
not the last line of defence, but it is not a rubber stamp either.

**Exit codes**, because the orchestrator reads them before reading any file:

* ``0`` a manifest was written; it may be a PASS or a FAIL, both are results
* ``2`` the request could not be honoured at all: unreadable, malformed, or the
  mounted inputs do not match the hashes the request pinned

Never ``0`` without a manifest, and never a manifest without a verdict the
contract's own terms produced.
"""

from __future__ import annotations

import base64
import json
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["JobRequest", "run_job", "main", "REQUEST_NAME", "MANIFEST_NAME", "STATUS_NAME"]

REQUEST_NAME = "request.json"
MANIFEST_NAME = "manifest.json"
STATUS_NAME = "status.json"

EXIT_OK = 0
EXIT_UNUSABLE = 2


@dataclass(frozen=True, slots=True)
class JobRequest:
    """What the orchestrator asked this job to grade.

    ``correlation_id`` is echoed into the result so an orchestrator cannot be
    handed a manifest from some other run. That matters more than it sounds:
    the whole point of moving grading out of process is that the process asking
    for a verdict is no longer the process producing one.
    """

    correlation_id: str
    contract: dict[str, Any]
    contract_hash: str
    funding_tx: str
    mandate_hash: str
    submission_sha: str
    changes: tuple[dict[str, Any], ...]
    grader_hash: str
    timeout_seconds: int = 600

    @classmethod
    def load(cls, path: Path) -> JobRequest:
        data = json.loads(path.read_text())
        return cls(
            correlation_id=str(data["correlation_id"]),
            contract=dict(data["contract"]),
            contract_hash=str(data["contract_hash"]),
            funding_tx=str(data["funding_tx"]),
            mandate_hash=str(data["mandate_hash"]),
            submission_sha=str(data["submission_sha"]),
            changes=tuple(dict(c) for c in data["changes"]),
            grader_hash=str(data["grader_hash"]),
            timeout_seconds=int(data.get("timeout_seconds", 600)),
        )


def _extract(archive: Path, destination: Path) -> Path:
    """Unpack a tar of one of the graded inputs.

    ``filter="data"`` refuses absolute paths, parent traversal, symlinks and
    device nodes. The archives are built by the orchestrator, but an extractor
    that trusts its input is exactly the shape of bug that turns a graded run
    into a container escape, and the cost of refusing is nil.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")
    return destination


def _submission(request: JobRequest) -> Any:
    from ..submission import ChangeKind, FileChange, Submission

    changes = tuple(
        FileChange(
            path=str(c["path"]),
            kind=ChangeKind(c["kind"]),
            content=base64.b64decode(c["content_b64"]) if c.get("content_b64") else None,
            executable=bool(c.get("executable", False)),
        )
        for c in request.changes
    )
    return Submission(request.submission_sha, changes)


def _sealed(request: JobRequest) -> Any:
    """Rebuild the sealed contract and let it validate itself.

    ``SealedContract.assert_intact`` recomputes ``contract_hash`` from the
    terms. If the mounted request was altered, this raises here rather than
    producing a verdict against terms nobody funded.
    """
    from ..contract import SealedContract, TaskContract

    contract = TaskContract.from_canonical_dict(request.contract)
    sealed = SealedContract(
        contract=contract,
        contract_hash=request.contract_hash,
        funding_tx=request.funding_tx,
        mandate_hash=request.mandate_hash,
    )
    sealed.assert_intact()
    return sealed


def run_job(mount: Path) -> int:
    """Grade one submission from a mounted input directory.

    ``mount`` holds ``in/`` and ``out/``. Everything this function needs is
    under it, and nothing it needs comes off the network.
    """
    inputs, outputs = mount / "in", mount / "out"
    outputs.mkdir(parents=True, exist_ok=True)

    def fail(reason: str) -> int:
        (outputs / STATUS_NAME).write_text(
            json.dumps({"ok": False, "reason": reason}, indent=2) + "\n"
        )
        print(f"mergegate-job: {reason}", file=sys.stderr)
        return EXIT_UNUSABLE

    try:
        request = JobRequest.load(inputs / REQUEST_NAME)
    except Exception as exc:  # noqa: BLE001 - any unreadable request is one answer
        return fail(f"unreadable request: {type(exc).__name__}: {exc}")

    try:
        from ..hashing import hash_directory
        from .evaluate import evaluate
        from .sandbox import EGRESS_DENY_TCP

        # The graded tree is built on local disk, never on the mounted bucket.
        # The mount is a Cloud Storage volume, and object storage has no file
        # permissions to copy: the first live run died in ``shutil.copytree``
        # with "[Errno 1] Operation not permitted" on every file, because
        # ``copystat`` cannot set a mode or an mtime on an object. The bucket
        # carries the request in and the manifest out. Grading happens here.
        with tempfile.TemporaryDirectory(prefix="mergegate-") as tmp:
            scratch = Path(tmp)
            base_tree = _extract(inputs / "base_tree.tar", scratch / "base")
            grader = _extract(inputs / "grader.tar", scratch / "grader")

            # Recomputed, never trusted from the request. A grader swapped on
            # the volume after the request was written would otherwise grade a
            # submission against tests nobody committed to.
            actual = hash_directory(grader)
            if actual != request.grader_hash:
                return fail(
                    f"grader bundle on the volume hashes to {actual}, "
                    f"request pinned {request.grader_hash}"
                )

            sealed = _sealed(request)
            manifest = evaluate(
                sealed=sealed,
                submission=_submission(request),
                base_tree=base_tree,
                grader_bundle=grader,
                destination=scratch / "workspace",
                timeout_seconds=request.timeout_seconds,
                # Stated because it is true here: this code is running inside
                # the sealed job whose posture was probed. That is the whole
                # reason this module exists.
                egress_policy=EGRESS_DENY_TCP,
            )
    except Exception as exc:  # noqa: BLE001 - reported, never a silent PASS
        return fail(f"evaluation failed: {type(exc).__name__}: {exc}")

    (outputs / MANIFEST_NAME).write_text(
        json.dumps(manifest.to_canonical_dict(), indent=2, sort_keys=True) + "\n"
    )
    (outputs / STATUS_NAME).write_text(
        json.dumps(
            {
                "ok": True,
                "correlation_id": request.correlation_id,
                "verdict": manifest.verdict.value,
                "contract_hash": request.contract_hash,
                "submission_sha": request.submission_sha,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"mergegate-job: {manifest.verdict.value} for {request.submission_sha[:12]}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    mount = Path(args[0]) if args else Path("/mnt/eval")
    return run_job(mount)


if __name__ == "__main__":  # pragma: no cover - container entry
    raise SystemExit(main())
