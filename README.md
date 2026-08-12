# MergeGate

**A deterministic evaluator and conditional USDC settlement layer for autonomous
coding agents.**

A buyer agent pre-authorizes a conditional USDC payment and funds escrow against
a signed, immutable task contract. A provider coding agent submits a commit. A
neutral, sealed sandbox runs the **buyer-pinned** grader against the provider's
diff. Escrow releases to the provider on PASS or refunds the buyer on FAIL.
Every decision emits one receipt that cryptographically binds contract + grader
+ artifact + verifier environment + decision + settlement transaction into a
single independently-verifiable object.

**The release condition is a reproducible test contract — not an LLM opinion, an
optimistic timeout, or a discretionary approval.** MergeGate implements the
deterministic *evaluator* of the ERC-8183 agent-job pattern for GitHub code,
without putting an LLM in the payment-authority path.

---

## What MergeGate does and does not claim

> **Scope of the guarantee: verified contract acceptance — not code quality,
> security, or mergeworthiness.**

MergeGate proves that a submission passed the buyer's pinned tests, unmodified,
in an environment the provider could not influence. It does not assess whether
the delivered code is good, safe, or something a maintainer would merge.
Test-passing and maintainer-mergeable are [documented as different
things](POSITIONING.md#honest-boundaries); we treat that as a stated boundary,
not a gap to paper over.

**Custody:** MergeGate is *programmable USDC escrow with policy-bound
conditional settlement*. MergeGate holds escrow authority. This is **not**
described as non-custodial, and will not be unless and until a contract is
deployed where neither MergeGate nor either party has unilateral release
authority outside the signed conditions.

**Scope:** v1 is **trusted-buyer** escrow — private repos, approved providers.
It is not an open or permissionless labor marketplace, and the "work is visible
before payment" defection risk is deferred by scope rather than solved.

**Naming:** "MergeGate" is a working name. Commercial use would require
trademark and domain diligence; it is not presented here as a finalized brand.

---

## Architecture

MergeGate does not rebuild the proof layer. Receipt signing, the policy engine,
canonical JSON (RFC 8785), and Merkle hashing come from the shared
[agent-authorization-gateway](https://github.com/4KInc/agent-authorization-gateway)
engine, vendored here as the `engine/` submodule and reached through exactly one
adapter module (`mergegate/engine.py`).

```
buyer agent ──signs mandate──> escrow (USDC, Base)
     │                              │
     │  pins contract + grader      │  releases / refunds
     ▼                              ▼
task contract ──> sealed sandbox verifier ──> bound receipt
  (immutable)      (Cloud Run Job, gVisor)     (offline-verifiable)
                            ▲
provider agent ──diff──────┘
```

Hosting is Google Cloud: verifier on Cloud Run Jobs with gVisor sandboxing, API
and dashboard on Cloud Run, evidence in GCS.

---

## Implementation status

Nothing below is asserted from intent — a row is only marked done when a test
or a real run demonstrates it. On-chain rows stay "not yet" until they have run
against real USDC and produced a transaction hash we can cite.

| Gate | What it establishes | Status |
| --- | --- | --- |
| P0.1 agent-funded escrow | Buyer agent funds and signs the mandate; no human checkout | **Not yet** — awaiting Circle credentials |
| P0.2 immutable contract + pinned grader | Terms and grader hash fixed before submission | **Done** — `mergegate/contract.py`, tested |
| P0.3 neutral sandbox verifier | Provider cannot influence the effective grader | **Done (logic)** — `mergegate/verifier/`, attacks tested end to end; container not yet deployed |
| P0.4 artifact binding | Pay only for the exact verified SHA + tree hash | **Partial** — `submission_sha` + `tree_hash` bound into the manifest; invalidation-on-force-push not yet built |
| P0.5 idempotent settlement | One contract → one settlement action | **Not yet** |
| P0.6 conditional-mandate execution | Settlement is deterministic, not discretionary | **Not yet** |
| P0.7 bound receipt | One object binds the whole chain, offline-verifiable | **Not yet** |
| P1.1 conftest / persisted-file gaming | Provider test hooks cannot survive grader injection | **Done** — hostile `conftest.py` and `sitecustomize.py` quarantined, asserted against a real pytest run |
| P1.2 `.git` history leakage | No reading reference solutions from git history | **Done** — `git archive` never creates `.git`; a run that tries to read the gold patch fails |
| P1.3 protected / graded path enforcement | Path violations reject regardless of test results | **Done** — `mergegate/paths.py`, tested |
| P1.4 sandbox isolation | Default-deny egress, no secrets, resource limits | **Partial** — spec refuses weakened configs and is tested; not yet submitted to a live Cloud Run API |
| P1.5 env-sniffing / tamper detection | Harness-tampering attempts recorded in the receipt | **Not yet** |
| P2.1 two mainnet demo flows | PASS→release and protected-path FAIL→refund | **Not yet** |
| P2.2 x402 verifier fee | Verifier-fee tx bound into the receipt | **Not yet** |

---

## How neutrality is demonstrated

The claim is not "it runs in a sandbox" — it is that the provider cannot
influence the grader. Assembly is ordered so the buyer's contribution always
overwrites the provider's:

1. Materialize the pinned base tree (`git archive` — no `.git` is ever created).
2. Guard every touched path. A protected- or grader-path violation is a hard
   reject and **the pinned commands never run**.
3. Apply the provider's changes to allowed source paths only.
4. Quarantine test hooks the provider introduced or modified *anywhere* —
   `src/conftest.py` sits inside an allowed path and pytest would still execute
   it. Allowed to write is not allowed to grade.
5. Purge the grader paths, then inject the buyer's bundle, so the graded bytes
   are the buyer's.
6. Hash the tree.

Steps 2 and 5 are deliberately redundant: a defense that depends on one check
being correct fails when that check is wrong.

`tests/test_verifier_neutrality.py` runs the documented attacks against a real
repository with a real `pytest` process and asserts each one fails — rewriting
the graded tests, a `conftest.py` hook that forces every outcome to pass, a
`sitecustomize.py` that runs before any test is imported, reading the reference
solution out of `.git`, and functionally-correct code that disables the CI gate
on its way past. Mocking the runner would prove nothing; the grade has to
actually be computed.

Tamper signals (quarantined hooks, purged grader files) are recorded in the
manifest rather than silently fixed up, so they can surface in the receipt.

## Development

Requires Python 3.11+.

```bash
git clone --recurse-submodules https://github.com/4KInc/mergegate.git
cd mergegate
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check . && mypy mergegate tests && pytest -q
```

If you cloned without `--recurse-submodules`, the shared engine will be missing
and imports will tell you so:

```bash
git submodule update --init --recursive
```

## Design

Dashboard screens live in `design/screens/` (generated with Google Stitch,
project `14966020786333238841`, design system "MergeGate Proof"). They are
design references for the Next.js dashboard, not shipped assets.

## License

Apache-2.0.
