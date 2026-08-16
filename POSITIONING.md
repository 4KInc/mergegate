# Positioning

## The mechanism, first

A buyer agent signs a conditional payment mandate (*pay exactly X USDC to
provider Y if and only if contract C evaluates PASS before deadline T*) and
funds escrow against a task contract whose every term is fixed and hashed before
the provider is allowed to submit. The provider submits a commit. The verifier
checks out the buyer's pinned base SHA, applies the provider's diff to allowed
source paths only, **overwrites the test tree with the buyer's grader bundle**,
and runs only the buyer's pinned commands. Escrow releases or refunds
on the result. One receipt binds the whole chain and can be re-verified offline
by anyone holding it.

That is the claim. Everything below is context for it.

## What actually ran

Both flows executed on **Base mainnet** with real USDC. Each settlement was
confirmed through a public Base RPC, not only through the payment provider's own
response.

| Flow | Settlement | Verifier fee |
| --- | --- | --- |
| PASS → release, 0.25 USDC to provider | [`0xa1303e97…`](https://basescan.org/tx/0xa1303e97235b39357d73ff82d90c6f6d757dafc2490abb18aa37098cf06dfbae), block 50060061 | [`0x6f94ef37…`](https://basescan.org/tx/0x6f94ef377c10f961a5252eadd8832ade991c47d22a76788e73ea81fe65507d5f) |
| FAIL → refund, 0.25 USDC to buyer | [`0xc9a5e865…`](https://basescan.org/tx/0xc9a5e865dc66000fcc2478bf71ca42fe5359163c0928ff380022942178d27d25), block 50060179 | [`0x177a46af…`](https://basescan.org/tx/0x177a46af7eb120206264c63f588dff0142eb75102239497b151c6e43966a9b96) |

Separately, the verifier is sold as an x402 service and Circle's own CLI pays
it: `circle services pay` verifies the buyer's EIP-3009 authorization, including
the ERC-1271 signature of a Circle Agent Wallet, and settles 0.05 USDC on Base
([`0xb40552f2…`](https://basescan.org/tx/0xb40552f201885ff233a35c66c39114f651dc84b062aa7484ec2c974db59a86d7),
block 50018597).

The FAIL flow is the one that carries the argument. Its code was *correct*: it
would have passed the buyer's pinned tests, but it also edited a protected
path, so the pinned commands never ran and escrow returned to the buyer. The
receipt names the term:

> contract evaluated FAIL: `.github/workflows/deploy.yml` modifies a
> contract-protected path (pattern: `.github/**`)

That is the difference between a control layer and "CI plus a transfer".

Both were graded **inside the sealed Cloud Run job** — executions
`mergegate-verifier-5rbrl` and `mergegate-verifier-mc5bj` — and the receipts
carry the network posture measured from inside it.

Both re-verify offline against the published signing key. The
[one-page case](https://mergegate-api-1031148889398.us-central1.run.app/judge)
assembles all of this from the receipts the deployment actually holds, and the
[dashboard](https://mergegate-api-1031148889398.us-central1.run.app)
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
   decision, settlement tx, and verifier-fee tx into one object. Fifteen of
   those fields are cross-checked against the manifest and mandate the receipt
   carries, so editing any of them fails verification **even for an attacker
   holding the signing key**, proven by re-signing each tampered variant.

Measured unit economics, including what a single evaluation costs in gas and
Gemini tokens and why the demo fee rate is indefensible, are in
[ECONOMICS.md](ECONOMICS.md).

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

  The limitation is now a **contract term** rather than only this paragraph.
  `terms_visibility` is hashed into the contract and defaults to `HASH_ONLY`,
  so a provider agent can branch on what it is actually able to know before
  accepting work. `PUBLISHED_GRADER` records a buyer assertion that MergeGate
  does not verify, and says so. `THIRD_PARTY_ESCROWED_GRADER` is in the
  vocabulary and **rejected at construction**: no such escrow exists here, and
  a contract may not commit to a protection the deployment cannot deliver,
  because a provider reading the term would take it as a guarantee.

- **A retry costs the buyer a second verifier fee.** The loop is closed — a
  refused submission is remediated and resubmitted automatically — but a retry
  is a *new contract*, funded again, because the buyer's mandate authorized
  exactly one payment decision. Nothing about this is free to the buyer, which
  is why `RetryBudget` bounds attempts and respects the deadline. A loop with no
  per-attempt cost would have no reason to terminate.

- **Grading is sealed now, and the posture has one deliberate exception.** This
  is the boundary that was wrong twice before it was right, and the third
  chapter is not a clean win either.

  First: the code asserted `default-deny` egress while a probe inside a real
  Cloud Run Job showed the default configuration reaching the open internet. A
  custom VPC with no Cloud NAT and a deny-all rule fixed that, leaving
  `deny-tcp-egress; dns-resolution-available`, since Cloud Run resolves DNS
  outside the VPC and **DNS remains a residual outbound signalling channel**.

  Second, and only found later: that sealed job was not what graded. Nothing
  dispatched to it. `build_job_request` constructed a Cloud Run job no caller
  submitted, and evaluation ran in the calling process. The manifest was
  defaulting its `egress_policy` to the sealed posture regardless, so every
  receipt asserted an isolation the run did not have. The default became
  `unrestricted; graded in-process, not in the sealed sandbox`, and a caller now
  has to *state* the sealed posture to claim it.

  The whole test suite passed while that was true, which is the useful part: the
  tests covered what the verifier computed and not whether its signed
  description of where it ran was accurate. Network-dependent tests remain out
  of scope regardless, because they are not deterministic and a release
  condition has to be reproducible.

  Third: dispatch now exists, and the first live sealed run failed to start.
  Inputs arrive on a Cloud Storage volume, gcsfuse dials
  `storage.googleapis.com` from inside the graded namespace, and a flat deny
  therefore fails the mount. **"Deny all egress" and "receive inputs" are not
  simultaneously satisfiable on this platform.** One destination is now allowed,
  Google's restricted API VIP, and the posture string names it rather than
  rounding it off:
  `deny-tcp-egress-except-google-restricted-vip-199.36.153.4/30; dns-resolution-available`.

  So the honest statement is two residual channels, not one. Graded code can
  open a socket to Google's API front-end — with no credentials to use there,
  but "unauthenticated" is weaker than "unreachable" — and DNS still resolves.
  The probe that establishes this is a module rather than a one-off, precisely
  because the claim has now moved three times.

- **Five receipt fields rest on the signature alone.** `settlement_tx`,
  `verifier_fee_tx`, `reason`, `settlement_asset`, and `settlement_chain` have
  nothing inside the receipt to check them against. Confirming those means
  comparing the receipt to the chain, which no offline verifier can do. A
  receipt proves the decision was the deterministic result of the mandate and
  the verdict; confirming the money moved requires looking at Base.

- **There is now an LLM, and it decides nothing.** Earlier versions of this
  document said there was no LLM anywhere, then that there were two advisory
  roles. There are now four: Gemini drafts contract terms from a plain request,
  assesses a contract before a provider accepts it, screens the provider's diff
  for malicious code and test gaming, and explains a FAIL into a policy-checked
  retry plan. Each of the four is bounded by deterministic code that decides
  what may be *acted on* — a draft that fails policy cannot be funded, a plan or
  an assessment naming a protected path is refused, and an assessment made
  without sight of the acceptance criteria has its confidence capped whatever
  the model claimed. It is still absent from
  contract creation, evaluation, settlement and receipt issuance, and that
  boundary is enforced by tests rather than convention. The settlement modules
  are parsed and asserted not to import it, settlement is byte-identical for
  hostile model output, and a diff that successfully steers the screening still
  refunds correctly. Prompt injection is not prevented; it is made worthless.

  The screening also produced a false positive on the very first honest
  submission it saw, flagging a comment the provider *deleted* as evidence the
  provider had read the grader, and scored that same wrong finding 40/100, then
  10/100, then 25/100 across three runs on equivalent input. That is the
  argument for the architecture rather than an embarrassment to it: had the screening carried gating power, correct
  work would have been refused, and whether it was refused would depend on which
  run you got.

- **x402 carries the verifier fee, not the task reward.** The endpoint now
  verifies and settles a real payment from a Circle Agent Wallet. What it does
  not carry is the reward: the 0.25 release and refund are ordinary USDC
  transfers through Circle agent wallets, and those are what the receipt binds.
  Circle Gateway nanopayments are still not used.

- **Tamper detection is partial.** Quarantined provider hooks and purged grader
  files are recorded in the manifest as tamper signals and surface in the
  receipt. There is no dedicated probe for a submission that detects the
  verifier environment and behaves differently because of it.

- **The dashboard reads settled receipts, not live evaluations.** It renders
  from Firestore, so a settlement appears without a redeploy, and it re-verifies
  each receipt per request. It does not stream an evaluation in progress.

- **"MergeGate" is a working name.** Commercial use would require trademark and
  domain diligence. It is not presented here as a finalized brand.
