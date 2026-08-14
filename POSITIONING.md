# Positioning

## The mechanism, first

A buyer agent signs a conditional payment mandate (*pay exactly X USDC to
provider Y if and only if contract C evaluates PASS before deadline T*) and
funds escrow against a task contract whose every term is fixed and hashed before
the provider is allowed to submit. The provider submits a commit. A sealed
container checks out the buyer's pinned base SHA, applies the provider's diff to
allowed source paths only, **overwrites the test tree with the buyer's grader
bundle**, and runs only the buyer's pinned commands. Escrow releases or refunds
on the result. One receipt binds the whole chain and can be re-verified offline
by anyone holding it.

That is the claim. Everything below is context for it.

## What actually ran

Both flows executed on **Base mainnet** with real USDC. Each settlement was
confirmed through a public Base RPC, not only through the payment provider's own
response.

| Flow | Settlement | Verifier fee |
| --- | --- | --- |
| PASS → release, 0.25 USDC to provider | [`0xb8a45ef2…`](https://basescan.org/tx/0xb8a45ef2bb14bff0ce99f5058b5a40be368424bc1e99f053993b84e9f12fe827), block 49945815 | [`0xeb5c1603…`](https://basescan.org/tx/0xeb5c16037349ad081168f3dcea99f912366013b45a754c39cce74b22714c0723) |
| FAIL → refund, 0.25 USDC to buyer | [`0x4581edf6…`](https://basescan.org/tx/0x4581edf6e7ab61e0f776ce52655ad77e0d7c99e85fcd235f5a53013cce895b1e), block 49946711 | [`0x75ca88ed…`](https://basescan.org/tx/0x75ca88edadcf72225b0baddaf7c036449c9952f667b3242cac6df4c3bb928280) |

The FAIL flow is the one that carries the argument. Its code was *correct*: it
would have passed the buyer's pinned tests, but it also edited a protected
path, so the pinned commands never ran and escrow returned to the buyer. The
receipt names the term:

> contract evaluated FAIL: `.github/workflows/deploy.yml` modifies a
> contract-protected path (pattern: `.github/**`)

That is the difference between a control layer and "CI plus a transfer".

Both receipts re-verify offline against the published signing key. The
dashboard at
[mergegate-api-1031148889398.us-central1.run.app](https://mergegate-api-1031148889398.us-central1.run.app)
re-verifies them on every request rather than trusting a stored flag.

## Where this sits relative to ERC-8183

ERC-8183 defines **who** may evaluate an agent's work: the roles, the
validator, the shape of an agent job. It is deliberately agnostic about how an
evaluator reaches its verdict.

**MergeGate defines *how* an evaluator can safely evaluate a GitHub submission
without an LLM in the payment-authority path.** The two are complementary: an
ERC-8183 deployment still needs something to put in the evaluator slot, and for
code delivery that something has to be resistant to the provider gaming it.

MergeGate is not proposed as a replacement for, or an improvement on, the
standard. It is an evaluator implementation with an opinionated threat model.

## Prior art

Disclosed in full, because the interesting question is not "is anything else
nearby" (things are) but "what is actually load-bearing here".

| Prior art | What it does | Relationship |
| --- | --- | --- |
| **ERC-8183** | Agent-job pattern: roles for requesting, performing, and validating agent work | MergeGate implements the evaluator role; it does not define the pattern |
| **Circle's AI-escrow demo** | Reference implementation of agent-initiated escrowed USDC payment | Establishes the payment primitive MergeGate settles on. MergeGate's contribution is the release condition, not the transfer |
| **TaskBounty** | On-chain bounty posting and payout for tasks | Overlaps on "fund a task, pay on completion". Differs on who determines completion and whether that determination is reproducible |
| **ArcAgent** | Agent task execution with payment rails | Overlaps on agent-to-agent commerce framing |
| **BountyBook** | Bounty coordination for software work | Overlaps on the GitHub-bounty surface; acceptance is human-adjudicated |
| **Cascrow** | Conditional escrow primitives | Overlaps on conditional release; conditions are not code-evaluation-specific |

Several of these solve "hold funds and release on a condition". Some solve
"post a coding task and pay for it". None of them, as far as we can establish,
make the *evaluation itself* the thing that is hardened and provable.

## The defensible wedge

Not the escrow. Not the receipt signature. Both are commodities.

The wedge is the **deterministic, buyer-pinned, provably-neutral evaluator plus
the bound receipt**:

1. **Buyer-pinned.** The grader bundle is hashed and committed to the contract
   before the provider ever sees the task. The provider cannot supply, edit, or
   influence the tests it will be graded against.

2. **Provably neutral.** Neutrality is demonstrated, not asserted. The grader
   bundle is injected *after* the provider's diff is applied, overwriting
   anything the provider persisted there. The documented attacks are executed
   against a real repository with a real `pytest` process and asserted to fail:
   rewriting the graded tests, a `conftest.py` hook that forces every outcome to
   pass, a `sitecustomize.py` that runs before any test is imported, reading the
   reference solution out of `.git`, and functionally-correct code that disables
   the CI gate on its way past. Mocking the runner would prove nothing; the
   grade has to actually be computed.

   One defense is worth naming because the obvious version of it is
   insufficient: a path guard alone permits `src/conftest.py`, which sits inside
   an allowed source path, and pytest will still execute it. **Allowed to write
   is not allowed to grade.** Provider-introduced test hooks are quarantined
   anywhere in the tree, and hooks already present in the buyer's base tree are
   left alone so ordinary projects keep working.

3. **Bound.** A signature over "PASS" proves someone said PASS. The receipt
   binds `contract_hash`, `grader_hash`, base SHA, `submission_sha`,
   `tree_hash`, verifier image digest, command output digest, result digest,
   decision, settlement tx, and verifier-fee tx into one object. Thirteen of
   those fields are cross-checked against the manifest and mandate the receipt
   carries, so editing any of them fails verification **even for an attacker
   holding the signing key**, proven by re-signing each tampered variant.

The threat model comes from documented reality, not imagination: the SWE-bench
and coding-agent literature records agents passing benchmarks by editing tests,
reading gold patches out of `.git` history, and leaving files that survive repo
reset. Defeating those visibly is the demonstration, not background security
work.

## Honest boundaries

These are stated up front rather than discovered by a judge. Several were
sharpened by deploying the system and measuring it, which is why a few read
differently from how they would have been written in advance.

- **Verified contract acceptance is not code quality.** METR's work on
  AI-generated pull requests found that passing the associated tests is a poor
  proxy for what a maintainer will actually merge. MergeGate does not dispute
  that; it scopes to exactly the claim it can prove. A PASS receipt means "this
  diff satisfied these pinned tests in this environment", full stop, not that
  the code is correct, secure, idiomatic, or mergeable.

- **Custody is real.** MergeGate holds escrow authority. Calling this
  non-custodial would be false, so we do not. The accurate description is
  *programmable USDC escrow with policy-bound conditional settlement*. A
  genuinely non-custodial version requires a deployed contract in which neither
  MergeGate nor either party can release outside the signed conditions; that is
  not what v1 does.

- **Trusted-buyer scope.** v1 targets private repos and approved providers. In
  an open marketplace, a buyer can read a submitted diff and then refuse: work
  is visible before payment. MergeGate does not solve that; it operates in a
  scope where it does not arise. Presenting this as a permissionless labor
  market would be overclaiming.

- **The sandbox blocks outbound TCP; DNS still resolves.** This is measured, not
  assumed, and the measurement corrected an earlier false claim. A probe run
  inside a real Cloud Run Job showed that the default configuration reaches the
  open internet (Cloud Run grants egress by default) while the code was
  asserting `default-deny`. Since that field is written into a *signed receipt*,
  it would have signed a false statement. A custom VPC with no Cloud NAT and an
  explicit deny-all egress rule now blocks all outbound TCP, but DNS resolution
  survives because Cloud Run resolves outside the VPC. So a graded run cannot
  fetch anything, and **DNS remains a residual outbound signalling channel**.
  The claim is `deny-tcp-egress; dns-resolution-available`, no more. Network-
  dependent tests remain out of scope regardless, because they are not
  deterministic and a release condition has to be reproducible.

- **Five receipt fields rest on the signature alone.** `settlement_tx`,
  `verifier_fee_tx`, `reason`, `settlement_asset`, and `settlement_chain` have
  nothing inside the receipt to check them against. Confirming those means
  comparing the receipt to the chain, which no offline verifier can do. A
  receipt proves the decision was the deterministic result of the mandate and
  the verdict; confirming the money moved requires looking at Base.

- **There is no LLM anywhere in the system.** Not merely absent from the
  payment-authority path, absent entirely. No model is called at any point in
  contract creation, evaluation, settlement, or receipt issuance. An earlier
  draft of this document said Gemini "may normalize task prose into a proposed
  contract"; that capability was never built, and describing it here would have
  been claiming a feature that does not exist.

- **The verifier fee is a plain USDC transfer, not x402.** Escrow does pay the
  verifier a per-run fee as a distinct on-chain transaction bound into the
  receipt, which is the substance of making the payment rail central in two
  places. But it is an ordinary transfer, not the x402 protocol or Circle
  Gateway. Anyone reading "x402" and expecting the protocol should read this
  instead.

- **Tamper detection is partial.** Quarantined provider hooks and purged grader
  files are recorded in the manifest as tamper signals and surface in the
  receipt. There is no dedicated probe for a submission that detects the
  verifier environment and behaves differently because of it.

- **The dashboard reads settled receipts, not live evaluations.** It renders
  from Firestore, so a settlement appears without a redeploy, and it re-verifies
  each receipt per request. It does not stream an evaluation in progress.

- **"MergeGate" is a working name.** Commercial use would require trademark and
  domain diligence. It is not presented here as a finalized brand.
