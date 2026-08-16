# MergeGate Unit Economics

Every figure below was measured on Base mainnet or against the live APIs, not
projected. Where a number is a guess, it says so.

The short version: **MergeGate sells one thing, the evaluation, and charges for
it whichever way the verdict goes.** That is the whole revenue model today. It
is implemented, it settles on mainnet, and it is thin.

---

## One product, one payer

| Item | Amount | Who pays | When |
| --- | --- | --- | --- |
| Verifier fee | 0.05 USDC | escrow, funded by the buyer agent | every evaluation, PASS or FAIL |
| Task reward | 0.25 USDC | escrow, funded by the buyer agent | released to the provider only on PASS |

The reward is not revenue. It passes through escrow to the provider agent, or
returns to the buyer. MergeGate captures the fee and nothing else.

**Charging on FAIL is the deliberate part.** The evaluation is the product, and
a refused submission consumes the same sandbox, the same grader, and the same
settlement path as an accepted one. Charging only on PASS would make MergeGate's
revenue depend on the verdict, which is exactly the incentive a neutral
evaluator must not have.

### Escrow is funded with reward plus fee

Escrow holds 0.30, not 0.25. Funding only the reward leaves nothing for the fee
after a release, and because a failed fee transfer is deliberately non-fatal,
the shortfall would silently drop the fee rather than fail loudly. That was a
real bug once: escrow funded with 0.25, the fee transfer failed, and the receipt
carried an empty `verifier_fee_tx` while reporting success.

---

## What one evaluation actually costs

Measured, on the two mainnet runs of 2026-08-15.

### Settlement gas: zero

This surprised me, and it is the single most favourable number in the model.

The four agent-wallet transfers in a full flow each burn about 155,400 gas at
0.006 gwei, roughly **$0.0018** each. MergeGate pays none of it. The `from`
address on every one is `0xe19635704ae3b77bc993358ff515d10cceae0ce1`, a Circle
relayer. Circle Agent Wallets sponsor gas, so escrow funding, release, refund
and the fee transfer all cost MergeGate nothing on-chain.

The one exception is x402. There MergeGate runs its own relayer, because
EIP-3009 exists so the *payer* never needs gas, which means the recipient side
must submit. That transaction is paid by `0x349eF760…` and costs about
**$0.0018**.

### Gemini: $0.0025 to $0.0049

Measured with `usage_metadata` on the real mainnet diff, not estimated from
character counts:

| | tokens | rate | cost |
| --- | --- | --- | --- |
| input | 505 | $0.30/M | $0.000151 |
| output | 132 | $2.50/M | $0.000330 |
| **thinking** | **786** | $2.50/M | $0.001965 |
| | | | **$0.00245** |

**Thinking tokens are six times the visible output and 80% of the bill.** Any
cost model built from prompt and response length alone would understate this by
roughly 4x. A PASS costs one screening call. A FAIL costs screening plus
forensics, so about **$0.0049**.

Gemini is optional. With no key configured the deterministic core runs
identically and this line is zero.

### Compute

The API service is Cloud Run, 1 vCPU / 1 GiB, `maxScale=4`, no minimum
instances, so it scales to zero and costs nothing while idle. At the volumes
below it stays inside the Cloud Run free tier.

Grading now runs in the sealed Cloud Run job, at 2 vCPU / 4 GiB with a 600s
ceiling. A graded run of about 30 seconds at those resources is roughly $0.0012,
which does not change any conclusion here, being two orders of magnitude below
the gas cost of the settlement it accompanies. Cold start and volume mount add
wall-clock, not meaningful cost.

Firestore holds settlement state, receipts, funded contracts and advisory
reports: a handful of small documents per task, comfortably inside the free
tier at any volume this project will see.

### Per-evaluation total

| | PASS | FAIL |
| --- | --- | --- |
| Revenue | $0.05 | $0.05 |
| Settlement gas | $0 (Circle sponsors) | $0 |
| Gemini | $0.0025 | $0.0049 |
| Compute | ~$0 (free tier) | ~$0 |
| **Gross margin** | **$0.0476** | **$0.0451** |

**95% gross margin on a PASS, 90% on a FAIL**, the difference being the second
Gemini call. Both are dominated by that one line item.

---

## What a retry costs, and who pays it

The loop is closed: a refused submission is remediated and resubmitted
automatically. That is a feature with a price attached, and the price falls on
the buyer.

A retry is a **new contract**, funded again, because the settled task is
terminal: the buyer's mandate authorized exactly one payment decision. So a
task that takes two attempts costs the buyer two verifier fees:

| Attempts | Buyer pays in fees | Provider receives | MergeGate revenue |
| --- | --- | --- | --- |
| 1 (PASS) | 0.05 | 0.25 | 0.05 |
| 2 (FAIL then PASS) | 0.10 | 0.25 | 0.10 |
| 3 (two failures then PASS) | 0.15 | 0.25 | 0.15 |

At the demo's 0.25 reward, a third attempt means the buyer has spent 0.40 to
obtain 0.25 of work. That is unreasonable, and it is unreasonable because the
demo reward is tiny rather than because the fee is: at a $50 task, three
attempts is $0.15 of fees, which is noise.

**The incentive here points the wrong way and is worth naming.** MergeGate earns
per evaluation, so more failed attempts mean more revenue. Nothing in the system
resists that today. What limits it is `RetryBudget`, which bounds attempts and
respects the deadline, and the fact that a plan proposing a prohibited change is
refused *before* an attempt is spent rather than after. Both reduce wasted
attempts; neither removes the incentive. A flat fee charged per *task* rather
than per *evaluation* would, at the cost of making pathological retry loops free
to the provider.

---

## An expired task, and a fee question that is not settled

The state machine has a terminal `EXPIRED` state: the deadline passed and no
verdict ever existed, so escrow returns to the buyer. It exists because a task
the verifier never answered used to stay open forever.

**Whether MergeGate should charge its fee in that case is an open question, and
the code does not currently answer it.** No path wires expiry to the settlement
executor yet, so nothing is charged today. But `SettlementExecutor.execute`
charges the fee for whatever directive it is handed, so wiring expiry naively
would bill the buyer 0.05 for an evaluation that produced no result.

That would be charging for our own outage, and it is worth saying plainly that
it is the *wrong* default rather than discovering it later. The argument for
charging is that the compute was attempted and consumed; the argument against is
that the product is a verdict and no verdict was delivered. The second is
stronger. An expiry is the one outcome where the failure is on MergeGate's side,
and a neutral evaluator that profits from its own unavailability has an
incentive nobody should have to reason about.

Stated here rather than quietly implemented either way, because it changes who
pays for a failure and that is a pricing decision, not an implementation detail.

---

## The fee rate is a demo figure, and 20% is indefensible

0.05 on a 0.25 reward is 20%. Anyone dividing those two numbers should know we
know.

It was chosen so both figures are legible in a block explorer, not because any
buyer would pay it. A 20% take on delivered work is far outside what code
marketplaces sustain: Upwork sits around 10%, and a verification layer providing
strictly less than a full marketplace cannot command more.

The rate is also the wrong shape. Verification cost is roughly **flat** in task
size, because it is one sandbox run and one settlement, while a percentage fee
grows without bound. The honest structure is a flat per-evaluation fee, which is
what the cost table above actually supports.

**A defensible price is $0.05 to $0.25 flat**, independent of reward size. At
$0.05 that is the current demo number applied to any task, which on a $50 task
is 0.1% rather than 20%. None of this is validated against a customer, because
there are no customers.

---

## Break-even

Fixed costs are near zero: Cloud Run scales to zero, Firestore is inside the
free tier, and Circle charges no platform fee. There is no server sitting idle
being paid for.

That makes break-even a strange quantity. The honest statement is that
**MergeGate is gross-margin positive from the first evaluation** and has no
meaningful fixed base to amortise. It also means the model says nothing
interesting about scale, because nothing about it is capital intensive.

At a flat $0.05 with ~$0.0025 marginal cost:

| Evaluations / month | Revenue | Gemini cost | Gross |
| --- | --- | --- | --- |
| 1,000 | $50 | $2.50 | $47.50 |
| 100,000 | $5,000 | $250 | $4,750 |
| 1,000,000 | $50,000 | $2,500 | $47,500 |

The obvious problem is visible in that table: at a million evaluations a month
this is a $600k/year business. **Per-evaluation pricing is not the business**,
it is the metering unit. Whatever MergeGate becomes commercially, it is not that
column.

Two directions are more plausible than volume, and neither is built:

- **Escrow value, not evaluation count.** A small percentage of settled value
  aligns revenue with the risk being removed rather than with compute consumed.
- **The receipt, not the verdict.** The durable artifact is a signed, offline
  verifiable record of what was delivered and paid. Whoever needs that record
  later, for audit, dispute, or underwriting, is a different customer with a
  different willingness to pay.

---

## Where the money is not

Being explicit about what MergeGate does **not** monetise, since each is a
plausible-sounding revenue line that is currently zero.

**The Gemini reports.** Screening and forensics are produced on every run and
given away. Forensics has clear value to a provider agent deciding whether to
retry, and is the obvious candidate to charge for. It is free because charging
for it would create pressure to make it load-bearing, and the entire design
depends on it deciding nothing.

**Nanopayments.** Circle Gateway offers gas-free sub-cent USDC transfers. At
$0.05 the fee is already a whole transaction; a per-file or per-test-run charge
would be the natural fit and would let the fee scale with work done rather than
per evaluation. Not built.

**The marketplace.** MergeGate is not listed in Circle's Agent Marketplace,
so no provider agent discovers work through it. Discovery is where a
marketplace's economics normally live, and MergeGate currently has none of it.

---

## Honest caveats

- **No customers, no revenue.** Every figure here is measured cost against a
  hypothetical price. Nothing has been sold.
- **The buyer griefing gap is an economic hole, not just a security one.** A
  bad-faith buyer can pin an unpassable test, read the submitted diff, and take
  a refund. MergeGate is paid either way, so it has no incentive to police this,
  and that misalignment is a real objection to the fee-on-both-verdicts model
  defended above. A slashable buyer bond is the intended fix and is not built.
- **Gemini pricing is Google's published rate** and could change. The token
  counts are measured; the dollars follow from a rate card.
- **The gas-sponsorship assumption is Circle's to change.** Zero settlement cost
  is the largest favourable term in this model and it is a policy of someone
  else's product, not a property of the system.
