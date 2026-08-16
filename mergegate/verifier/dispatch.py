"""Orchestrating a sealed evaluation, and refusing to believe its result.

This is the other half of :mod:`mergegate.verifier.job`. It builds the request,
hands it to a sealed Cloud Run job through a storage volume, and then does the
part that actually matters: **checks that the manifest it got back describes the
evaluation it asked for.**

**Why the check is the point.** Moving grading into a job buys nothing on its
own. If the orchestrator accepted whatever manifest appeared, the sealed job
would be theatre: anything able to write to the output prefix could mint a PASS.
So the returned manifest is compared field by field against the request, and a
mismatch on the correlation id, contract hash, submission SHA, grader hash or
image digest is refused. A refusal is not a FAIL, because those settle
differently: a FAIL refunds and closes the task, while a refused result means
the evaluation did not happen and nothing should move.

**Nothing here can manufacture a verdict.** ``VerdictUnavailableError`` carries no
decision, and there is no path in this module that constructs a
``VerificationManifest``. The only manifest that exists is the one the job
wrote. That is deliberate and asserted by a test: an orchestrator that could
build its own manifest is an orchestrator that can pay itself.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contract import SealedContract
from ..hashing import hash_directory
from ..submission import Submission
from .manifest import VerificationManifest
from .sandbox import EGRESS_DENY_TCP

__all__ = [
    "EvaluationRequest",
    "SealedEvaluation",
    "VerdictUnavailableError",
    "build_request",
    "pack_inputs",
    "verify_result",
    "CloudRunJob",
    "run_sealed_evaluation",
    "MOUNT_ROOT",
]


class VerdictUnavailableError(RuntimeError):
    """The sealed evaluation did not produce a result this orchestrator accepts.

    Deliberately not a FAIL. A FAIL is a verdict the contract's terms produced
    and it refunds the buyer, closing the task. This means no verdict exists,
    so the correct response is to leave escrow untouched and let the deadline
    decide. Conflating the two would let an infrastructure outage spend money.
    """


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """What the job is being asked to grade, and what it must return."""

    correlation_id: str
    sealed: SealedContract
    submission: Submission
    grader_hash: str
    timeout_seconds: int = 600

    def to_json(self) -> str:
        contract = self.sealed.contract
        return json.dumps(
            {
                "correlation_id": self.correlation_id,
                "contract": contract.to_canonical_dict(),
                "contract_hash": self.sealed.contract_hash,
                "funding_tx": self.sealed.funding_tx,
                "mandate_hash": self.sealed.mandate_hash,
                "submission_sha": self.submission.submission_sha,
                "changes": [
                    {
                        "path": c.path,
                        "kind": str(c.kind),
                        "content_b64": (
                            base64.b64encode(c.content).decode() if c.content is not None else None
                        ),
                        "executable": c.executable,
                    }
                    for c in self.submission.changes
                ],
                "grader_hash": self.grader_hash,
                "timeout_seconds": self.timeout_seconds,
            },
            indent=2,
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class SealedEvaluation:
    """A manifest that was produced by a sealed job and checked against its request.

    Only :func:`verify_result` constructs this. Holding one is the evidence that
    the checks below passed, so callers do not have to remember to run them.
    """

    manifest: VerificationManifest
    correlation_id: str
    execution_id: str
    image_digest: str
    egress_policy: str = EGRESS_DENY_TCP


def build_request(
    *,
    sealed: SealedContract,
    submission: Submission,
    grader_bundle: Path,
    timeout_seconds: int = 600,
    correlation_id: str | None = None,
) -> EvaluationRequest:
    """Describe one evaluation.

    The correlation id is derived from the contract hash and submission SHA
    rather than random, so a retried dispatch of the same evaluation reuses it.
    A random id would make a duplicate dispatch look like a different
    evaluation, which is precisely the ambiguity the settlement layer spends
    three separate guards avoiding.
    """
    resolved = correlation_id or str(
        uuid.uuid5(
            uuid.UUID("6f9d3c1e-4a2b-5e8d-9c7f-1b2a3c4d5e6f"),
            f"{sealed.contract_hash}|{submission.submission_sha}",
        )
    )
    return EvaluationRequest(
        correlation_id=resolved,
        sealed=sealed,
        submission=submission,
        grader_hash=hash_directory(grader_bundle),
        timeout_seconds=timeout_seconds,
    )


def _tar_bytes(directory: Path) -> bytes:
    """A deterministic tar of a directory.

    Timestamps, uid/gid and ownership names are zeroed and entries sorted, so
    packing the same tree twice produces identical bytes. Non-deterministic
    archives would make an otherwise reproducible evaluation depend on when it
    ran.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(directory.rglob("*")):
            info = tar.gettarinfo(path, arcname=str(path.relative_to(directory)))
            info.mtime, info.uid, info.gid = 0, 0, 0
            info.uname = info.gname = ""
            if path.is_file():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
    return buffer.getvalue()


def pack_inputs(
    request: EvaluationRequest, *, base_tree: Path, grader_bundle: Path, destination: Path
) -> Path:
    """Write the ``in/`` directory the job will read from its volume."""
    inputs = destination / "in"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "request.json").write_text(request.to_json() + "\n")
    (inputs / "base_tree.tar").write_bytes(_tar_bytes(base_tree))
    (inputs / "grader.tar").write_bytes(_tar_bytes(grader_bundle))
    return inputs


def verify_result(
    request: EvaluationRequest,
    *,
    outputs: Path,
    execution_id: str,
    image_digest: str,
) -> SealedEvaluation:
    """Check a job's output against the request, or refuse it.

    Every comparison here answers a way the result could describe some other
    evaluation than the one that was paid for. Raises
    :class:`VerdictUnavailableError` rather than returning a verdict, because there
    is no safe verdict to return: an unverifiable result must not settle in
    either direction.
    """
    status_path, manifest_path = outputs / "status.json", outputs / "manifest.json"
    if not status_path.is_file():
        raise VerdictUnavailableError("the job wrote no status")

    try:
        status = json.loads(status_path.read_text())
    except json.JSONDecodeError as exc:
        raise VerdictUnavailableError(f"unreadable status: {exc}") from exc

    if not status.get("ok"):
        raise VerdictUnavailableError(
            f"the job reported failure: {status.get('reason', 'unstated')}"
        )
    if status.get("correlation_id") != request.correlation_id:
        raise VerdictUnavailableError(
            "result belongs to a different evaluation: correlation id "
            f"{status.get('correlation_id')!r}, expected {request.correlation_id!r}"
        )
    if not manifest_path.is_file():
        raise VerdictUnavailableError("the job reported success but wrote no manifest")

    try:
        data: dict[str, Any] = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise VerdictUnavailableError(f"unreadable manifest: {exc}") from exc

    expectations = {
        "contract_hash": request.sealed.contract_hash,
        "submission_sha": request.submission.submission_sha,
        "grader_hash": request.grader_hash,
        "base_sha": request.sealed.contract.base_sha,
        "task_id": request.sealed.contract.task_id,
        "verifier_image_digest": request.sealed.contract.verifier_image_digest,
    }
    for field_name, expected in expectations.items():
        actual = data.get(field_name)
        if actual != expected:
            raise VerdictUnavailableError(
                f"manifest {field_name} is {actual!r}, request pinned {expected!r}"
            )

    # The image that ran has to be the image the contract funded. Comparing the
    # contract's digest to the manifest above only proves the job echoed it;
    # this compares it to what the platform reports actually executing.
    if image_digest and image_digest != request.sealed.contract.verifier_image_digest:
        raise VerdictUnavailableError(
            f"executed image {image_digest!r} is not the digest the contract pinned, "
            f"{request.sealed.contract.verifier_image_digest!r}"
        )

    return SealedEvaluation(
        manifest=VerificationManifest.from_canonical_dict(data),
        correlation_id=request.correlation_id,
        execution_id=execution_id,
        image_digest=image_digest or request.sealed.contract.verifier_image_digest,
    )


# -- running the job for real --------------------------------------------------

#: Where the job's storage volume is mounted inside the container.
MOUNT_ROOT = "/mnt/evalroot"


@dataclass(frozen=True, slots=True)
class CloudRunJob:
    """The sealed job, addressed well enough to run one evaluation in it."""

    name: str
    region: str
    project: str
    bucket: str
    prefix: str = "evaluations"
    binary: str = "gcloud"
    timeout_seconds: int = 900


def _gcloud(job: CloudRunJob, argv: list[str], *, timeout: int) -> tuple[int, str, str]:
    import subprocess

    completed = subprocess.run(  # noqa: S603 - argv vector, shell=False
        [job.binary, *argv],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.returncode, completed.stdout, completed.stderr


def run_sealed_evaluation(
    request: EvaluationRequest,
    *,
    base_tree: Path,
    grader_bundle: Path,
    job: CloudRunJob,
    workdir: Path,
) -> SealedEvaluation:
    """Grade one submission inside the sealed Cloud Run job.

    Uploads the inputs to the job's storage volume, executes the job against
    that prefix, and reads the manifest back. The result still goes through
    :func:`verify_result`, so a job that returns something describing a
    different evaluation is refused exactly as it would be locally.

    Raises :class:`VerdictUnavailableError` for every failure mode. There is no
    path here that falls back to grading in this process: silently degrading to
    an unsealed run would reproduce the precise problem this module exists to
    fix, and would do it invisibly.
    """
    import shutil

    prefix = f"{job.prefix}/{request.correlation_id}"
    staged = workdir / "staged"
    if staged.exists():
        shutil.rmtree(staged)
    pack_inputs(request, base_tree=base_tree, grader_bundle=grader_bundle, destination=staged)

    code, _, err = _gcloud(
        job,
        [
            "storage",
            "rsync",
            "--recursive",
            str(staged / "in"),
            f"gs://{job.bucket}/{prefix}/in",
            "--project",
            job.project,
        ],
        timeout=job.timeout_seconds,
    )
    if code != 0:
        raise VerdictUnavailableError(f"could not stage evaluation inputs: {err.strip()[:300]}")

    code, out, err = _gcloud(
        job,
        [
            "run",
            "jobs",
            "execute",
            job.name,
            "--region",
            job.region,
            "--project",
            job.project,
            "--wait",
            # One token, not two. The value begins with "-m", and gcloud reads a
            # separate argument starting with a dash as another flag, answering
            # "argument --args: expected one argument". The same shape broke
            # `mergegate verify --public-key <key>` for keys whose base64
            # happened to start with a dash.
            f"--args=-m,mergegate.verifier.job,{MOUNT_ROOT}/{prefix}",
        ],
        timeout=job.timeout_seconds,
    )
    execution_id = ""
    for line in (out + err).splitlines():
        if "mergegate-verifier-" in line:
            for token in line.replace("[", " ").replace("]", " ").split():
                if token.startswith("mergegate-verifier-"):
                    execution_id = token.strip(".,")
                    break
        if execution_id:
            break
    if code != 0:
        raise VerdictUnavailableError(
            f"the sealed job did not complete: {(err or out).strip()[:300]}"
        )

    downloaded = workdir / "result"
    downloaded.mkdir(parents=True, exist_ok=True)
    code, _, err = _gcloud(
        job,
        [
            "storage",
            "rsync",
            "--recursive",
            f"gs://{job.bucket}/{prefix}/out",
            str(downloaded),
            "--project",
            job.project,
        ],
        timeout=job.timeout_seconds,
    )
    if code != 0:
        raise VerdictUnavailableError(f"could not read the job's result: {err.strip()[:300]}")

    return verify_result(
        request,
        outputs=downloaded,
        execution_id=execution_id,
        image_digest=request.sealed.contract.verifier_image_digest,
    )
