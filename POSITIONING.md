# Positioning

## The mechanism, first

A buyer agent signs a conditional payment mandate — *pay exactly X USDC to
provider Y if and only if contract C evaluates PASS before deadline T* — and
funds escrow against a task contract whose every term is fixed and hashed before
the provider is allowed to submit. The provider submits a commit. A sealed
container checks out the buyer's pinned base SHA, applies the provider's diff to
allowed source paths only, **overwrites the test tree with the buyer's grader
bundle**, and runs only the buyer's pinned commands. Escrow releases or refunds
on the result. One receipt binds the whole chain and can be re-verified offline
by anyone holding it.

That is the claim. Everything below is context for it.

## Where this sits relative to ERC-8183

ERC-8183 defines **who** may evaluate an agent's work — the roles, the
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
nearby" — things are — but "what is actually load-bearing here".

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
   anything the provider persisted there — so a `conftest.py` that overrides
   outcomes, or a modified test file, has no effect on the grade. There are
   tests that attempt exactly these attacks and assert they fail.
3. **Bound.** A signature over "PASS" proves someone said PASS. The receipt here
   binds `contract_hash`, `grader_hash`, base SHA, `submission_sha`,
   `tree_hash`, verifier image digest, command output digest, result digest,
   decision, settlement tx, and verifier-fee tx into one object. It answers
   *which code, judged by which tests, in which environment, settling which
   payment* — and a third party can re-check all of it from the receipt alone.

The threat model comes from documented reality, not imagination: the SWE-bench
and coding-agent literature records agents passing benchmarks by editing tests,
reading gold patches out of `.git` history, and leaving files that survive repo
reset. Defeating those visibly is the demonstration, not background security
work.

## Honest boundaries

These are stated up front rather than discovered by a judge.

- **Verified contract acceptance is not code quality.** METR's work on AI-generated
  pull requests found that passing the associated tests is a poor proxy for
  what a maintainer will actually merge. MergeGate does not dispute that; it
  scopes to exactly the claim it can prove. A PASS receipt means "this diff
  satisfied these pinned tests in this environment", full stop — not that the
  code is correct, secure, idiomatic, or mergeable.

- **Custody is real.** MergeGate holds escrow authority. Calling this
  non-custodial would be false, so we do not. The accurate description is
  *programmable USDC escrow with policy-bound conditional settlement*. A
  genuinely non-custodial version requires a deployed contract in which neither
  MergeGate nor either party can release outside the signed conditions; that is
  not what v1 does.

- **Trusted-buyer scope.** v1 targets private repos and approved providers. In
  an open marketplace, a buyer can read a submitted diff and then refuse — work
  is visible before payment. MergeGate does not solve that; it operates in a
  scope where it does not arise. Presenting this as a permissionless labor
  market would be overclaiming.

- **Network-dependent tests are out of scope.** The sandbox is default-deny
  egress. Tests that reach the network are not deterministic and therefore
  cannot be a release condition. This is a deliberate limit, not an oversight.

- **No LLM in the payment-authority path.** Gemini may normalize a buyer's task
  prose into a proposed contract, or summarize a failure for a human reader.
  Neither output is consulted when deciding whether funds move.
