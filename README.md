# MergeGate

**Trust and settlement infrastructure for AI agents buying software from AI
agents.**

An agent can write code today. It cannot get paid for it without a human in the
loop, because nobody can safely answer the question *did this agent actually
deliver what was asked?* Card rails need a human accountable for the charge;
"LLM-as-a-judge" replaces that human with a model that can be argued into
approving anything.

MergeGate answers it differently. A buyer agent funds USDC escrow against a
signed, immutable task contract whose acceptance test is fixed and hashed before
any work begins. A provider agent submits a commit. A sealed sandbox runs the
**buyer-pinned** grader against that diff, in an environment the provider cannot
influence. Escrow releases on PASS or refunds on FAIL, and one receipt binds
contract, grader, artifact, environment, decision and settlement transaction
into a single object anyone can verify offline.

> **No LLM sits in the payment-authority path.** The release condition is a
> reproducible test contract, not a model's opinion, an optimistic timeout, or a
> discretionary approval. No model is called at any point in contract creation,
> evaluation, settlement, or receipt issuance.

This is the deterministic *evaluator* of the ERC-8183 agent-job pattern for
GitHub code, running on Base mainnet.

### Live, on Base mainnet

| | |
| --- | --- |
| **Dashboard** | [mergegate-api-1031148889398.us-central1.run.app](https://mergegate-api-1031148889398.us-central1.run.app) |
| **PASS flow** (0.25 USDC released to the provider) | [settlement tx](https://basescan.org/tx/0xf8cb4b0f35af41019b0ab57efee70ab451eaa85e718cb0eb91aed35e5acfe9b6) · block 49972831 |
| **FAIL flow** (0.25 USDC refunded to the buyer) | [refund tx](https://basescan.org/tx/0x8362ac904dad8ce8f740b29d3183d8a1659ba01b2a71a1b09fe35e5c97245354) · block 49972989 |

The FAIL flow is the one worth opening. That submission's code was **correct**
and would have passed the buyer's tests. It was refused anyway, before the tests
ran, because it also edited a protected CI file. That is the difference between
a control layer and a test runner wired to a transfer.

---

## The agent-to-agent protocol

No human appears anywhere in this loop.

| Step | Actor | What happens |
| --- | --- | --- |
| 1 | **Buyer agent** | Pins the terms: repo, base commit, grader bundle, writable paths, protected paths, commands, deadline, price. Canonicalizes and hashes them into `contract_hash`. |
| 2 | **Buyer agent** | Signs a mandate (*pay X to provider Y iff contract C evaluates PASS before T*) and funds escrow itself. No checkout, no approval click. |
| 3 | **Provider agent** | Reads the published mandate, sees the acceptance test is already fixed and hashed, and submits a commit. |
| 4 | **Verifier** | Assembles base tree + provider diff + buyer grader in a sealed sandbox and runs only the pinned commands. |
| 5 | **Settlement** | The mandate is *executed*, not re-decided. PASS releases, FAIL refunds. |
| 6 | **Receipt** | One signed object binds the whole chain, verifiable by anyone offline. |

The provider agent can inspect the contract before committing work, and knows
the grader hash was fixed beforehand, so it can tell the terms cannot move after
submission. That is what makes the deal legible to a machine: the acceptance
criterion is a hash, not a promise.

The only human action in the entire system is provisioning the wallet credential
once, the way a service account is provisioned. Every transfer after that is
agent-initiated.

## Economics

Three parties, two payments, one of which is ours.

| Payment | From | To | Why |
| --- | --- | --- | --- |
| Reward | escrow | provider agent | the work, released only on PASS |
| Verifier fee | escrow | verifier | the evaluation, charged per run regardless of verdict |

Escrow is funded with reward **plus** fee, so the fee is covered whichever way
the verdict goes. Funding only the reward would leave nothing for the fee after
a release, and because a failed fee transfer is deliberately non-fatal, that
shortfall would silently drop the fee rather than fail loudly.

MergeGate captures the verifier fee. That is the revenue mechanism, it is
implemented, and it settles on mainnet.

**Honest caveat on the rate.** The demo charges 0.05 on a 0.25 reward. That is
20%, a demo number and not a proposed rate, and anyone dividing those two
figures should know we know. Sustainable pricing is closer to a small percentage
of settlement value or a flat per-evaluation fee, and neither is validated.

**Known game-theory gap.** A buyer sets the acceptance test, so a buyer acting
in bad faith can pin a test the provider cannot pass, collect the work as a
public diff, and take a refund. The verifier still gets paid, so MergeGate has
no incentive to police it.

It is tempting to answer that the provider can simply read the tests before
accepting, and that answer is wrong here. The contract publishes `grader_hash`
and `grader_paths`, never the bundle contents. A provider can therefore verify
that the tests **cannot change** after submission, which is the property the
hash exists to give, but cannot verify that they are **passable**. Those are
different guarantees and only the first is implemented.

The shape of a fix is a slashable buyer bond or a non-refundable attempt fee
that survives a refund, so a buyer pays something for every evaluation they
trigger. Publishing the grader bundle alongside its hash would also close it,
at the cost of revealing the acceptance test up front. Neither is built.
Trusted-buyer scope avoids the situation rather than solving it.

## What MergeGate does and does not claim

> **Scope of the guarantee: verified contract acceptance, not code quality,
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

**Scope:** v1 is **trusted-buyer** escrow: private repos, approved providers.
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
buyer agent ──signs mandate──> escrow (USDC, Base mainnet)
     │                              │
     │  pins contract + grader      │  releases / refunds
     ▼                              ▼
task contract ──> sealed sandbox verifier ──> bound receipt
  (immutable)      (Cloud Run Job, gVisor)     (offline-verifiable)
                            ▲                        │
provider agent ──diff───────┘                        ▼
                                              dashboard + API
                                              (Cloud Run service)
```

Two workloads with deliberately opposite network postures:

| | Outbound network | Why |
| --- | --- | --- |
| **API / dashboard** (Cloud Run *service*) | allowed | must reach Circle to settle and GitHub to read submissions |
| **Verifier** (Cloud Run *job*) | no TCP egress | grading must be deterministic and un-influenceable |

Sealing the API too would silently break settlement, which is why the deny-all
VPC is attached to the job alone.

State lives in Firestore: `mergegate_tasks` (settlement state machines),
`mergegate_receipts` (issued receipts), `mergegate_contracts` (funded contract
terms and their funding transaction). Secrets (the receipt signing key, the
GitHub webhook secret, the Circle CLI session) live in Secret Manager and are
mounted, never baked into the image or passed as plain environment variables.

---

## Implementation status

Nothing below is asserted from intent: a row is only marked done when a test
or a real run demonstrates it. On-chain rows stayed "not yet" until they had
run against real USDC and produced a transaction hash we can cite; the mainnet
rows below now do.

| Gate | What it establishes | Status |
| --- | --- | --- |
| P0.1 agent-funded escrow | Buyer agent funds and signs the mandate; no human checkout | **Done on mainnet**: the buyer agent funds escrow and seals the contract with no human step ([funding tx](https://basescan.org/tx/0xaf13670e060dfa86cd1fddd5da3171525e7934c1e76317769035a5485fa4c27d)) |
| P0.2 immutable contract + pinned grader | Terms and grader hash fixed before submission | **Done**: `mergegate/contract.py`, tested |
| P0.3 neutral sandbox verifier | Provider cannot influence the effective grader | **Done (logic)**: `mergegate/verifier/`, attacks tested end to end; verifier image built and pinned by real digest |
| P0.4 artifact binding | Pay only for the exact verified SHA + tree hash | **Done**: a new head SHA invalidates the prior verification; a stale result for a superseded SHA is dropped |
| P0.5 idempotent settlement | One contract → one settlement action | **Done**: `mergegate/settlement.py`; replayed and out-of-order event sequences settle exactly once |
| P0.6 conditional-mandate execution | Settlement is deterministic, not discretionary | **Done**: `mergegate/mandate.py`; the executor receives a decision, it does not make one |
| P0.7 bound receipt | One object binds the whole chain, offline-verifiable | **Done**: `mergegate/receipt.py`; 13 bound fields survive an attacker holding the signing key |
| P1.1 conftest / persisted-file gaming | Provider test hooks cannot survive grader injection | **Done**: hostile `conftest.py` and `sitecustomize.py` quarantined, asserted against a real pytest run |
| P1.1b grader confidentiality | Provider code cannot read the graded tests at run time | **Done**: a submission that implemented nothing passed by scraping expected values out of the test file; a startup audit hook outside the workspace now blocks it |
| P1.2 `.git` history leakage | No reading reference solutions from git history | **Done**: `git archive` never creates `.git`; a run that tries to read the gold patch fails |
| P1.3 protected / graded path enforcement | Path violations reject regardless of test results | **Done**: `mergegate/paths.py`, tested |
| P1.4 sandbox isolation | No outbound TCP, no secrets, resource limits | **Done (measured)**: probed inside a real Cloud Run Job: all outbound TCP blocked, DNS still resolves (disclosed, not hidden) |
| P1.5 env-sniffing / tamper detection | Harness-tampering attempts recorded in the receipt | **Partial**: quarantined hooks and purged grader files are recorded as tamper signals; no dedicated env-sniffing probe |
| P2.1 two mainnet demo flows | PASS→release and protected-path FAIL→refund | **Done on mainnet**: both run live with real USDC, txs confirmed on-chain (see below) |
| P2.2 verifier fee | Verifier-fee tx bound into the receipt | **Partial**: escrow pays the verifier a per-run fee as a distinct mainnet tx, bound into the receipt and settled live. It is a plain USDC transfer; **x402/Gateway integration is deferred**, so anyone expecting the protocol should read this row |

---

## How neutrality is demonstrated

The claim is not "it runs in a sandbox"; it is that the provider cannot
influence the grader. Assembly is ordered so the buyer's contribution always
overwrites the provider's:

1. Materialize the pinned base tree (`git archive`, so no `.git` is ever created).
2. Guard every touched path. A protected- or grader-path violation is a hard
   reject and **the pinned commands never run**.
3. Apply the provider's changes to allowed source paths only.
4. Quarantine test hooks the provider introduced or modified *anywhere*:
   `src/conftest.py` sits inside an allowed path and pytest would still execute
   it. Allowed to write is not allowed to grade.
5. Purge the grader paths, then inject the buyer's bundle, so the graded bytes
   are the buyer's.
6. Install a runtime guard, outside the workspace, that stops provider code
   *reading* the grader.
7. Hash the tree.

Steps 2 and 5 are deliberately redundant: a defense that depends on one check
being correct fails when that check is wrong.

Step 6 exists because steps 2 to 5 were not enough, and a reviewer's question
exposed it. They all stop the provider **editing** the tests. None of them stop
it **reading** them, and reading is sufficient: a submission that implements
nothing can scrape the expected values out of the test file at import time and
answer from a lookup table. Every assertion passes and the code does nothing.

That was demonstrated against this system, not imagined, and it passed. The
defense is a CPython audit hook loaded from a `sitecustomize` module on
`PYTHONPATH` in a directory outside the workspace: it is in place before any
test or provider module runs, audit hooks cannot be removed once installed, and
the provider's diff cannot reach the file. It blocks reads of grader paths only
from frames belonging to provider source paths, so pytest reading its own test
files is unaffected. `tests/test_grader_confidentiality.py` runs the original
attack, a lazy variant that defers the read to call time, and an
honest-submission control, because a guard that broke honest runs would be worse
than none.

`tests/test_verifier_neutrality.py` runs the documented attacks against a real
repository with a real `pytest` process and asserts each one fails: rewriting
the graded tests, a `conftest.py` hook that forces every outcome to pass, a
`sitecustomize.py` that runs before any test is imported, reading the reference
solution out of `.git`, and functionally-correct code that disables the CI gate
on its way past. Mocking the runner would prove nothing; the grade has to
actually be computed.

Tamper signals (quarantined hooks, purged grader files) are recorded in the
manifest rather than silently fixed up, so they can surface in the receipt.

## Settlement

MergeGate settles through Circle **agent wallets**, driven by the `circle` CLI,
not the REST Developer-Controlled Wallets API. They are separate products
holding separate wallets, and the funded Base wallets exist only in the former.

Double-payment has two independent guards. The state machine refuses a second
settlement (P0.5), and the settlement key is passed to Circle as the transfer's
idempotency key, so a repeated key returns the original transaction instead of
sending a new one. Both are verified: the first by replayed and out-of-order
event tests, the second against real Circle infrastructure on Base Sepolia:
sending twice with one key moved 0.25 USDC exactly once and returned the same
transaction hash both times.

Circle requires idempotency keys to be **UUIDs** and rejects a bare
`sha256:<hex>` with `400 Invalid request body`. The rail derives a UUIDv5 from
the settlement key over a fixed namespace, so the mapping stays deterministic:
a random UUID would satisfy the format and silently destroy the guard, since a
retry would present a fresh key.

## The two demo flows, run live on Base mainnet

Both ran end to end on **Base mainnet** with real USDC, driven by
`python -m mergegate.demo`. Every hash and transaction below came out of those
runs. Each settlement transaction was independently confirmed through a public
Base RPC, not just through Circle's response.

**PASS → release.** The provider fixed the bug; escrow paid out.

| | |
| --- | --- |
| contract | `sha256:be8e3ab1a764ef807cbd31db06417bd23af7250ff8519d935a17deffcbf1b9e0` |
| grader | `sha256:83018d118089f7a1a267f815dccde1933e92fff615e70d00c8a6b31dd5e2a7a6` |
| submission | `97e4bd614868913199f264effb1e954a2f799373` |
| escrow funded | [`0x2d41e529…`](https://basescan.org/tx/0x2d41e529c3567f31195355cf82c4e789d8373a940647ec33993910bdd629b0a2) |
| release, 0.25 USDC | [`0xf8cb4b0f…`](https://basescan.org/tx/0xf8cb4b0f35af41019b0ab57efee70ab451eaa85e718cb0eb91aed35e5acfe9b6), block 49972831 |
| verifier fee, 0.05 USDC | [`0x58a5bdd3…`](https://basescan.org/tx/0x58a5bdd3d8f82eb2c3d6c7729761d56b160a4e8df87dbb1718ed5384c0417d92), block 49972855 |

**FAIL → refund.** This is the one that matters. The submission's code is
*correct*: it would have passed the buyer's tests, but it also edited
`.github/workflows/deploy.yml`. The pinned commands never ran (`commands: 0`),
and escrow returned to the buyer.

| | |
| --- | --- |
| contract | `sha256:5fad3810abcb6705dad2f88fb8bd447fbc2dd6e61eb5d193968b200dd91e139d` |
| submission | `1758ca302557dcc9d6c1eee6b5aad92cd7bcfe0e` |
| escrow funded | [`0x39913b9f…`](https://basescan.org/tx/0x39913b9fc210c35e1ff55df04ea828fcf206b4f47a098c6e3cfe5dd964062ba9) |
| refund, 0.25 USDC | [`0x8362ac90…`](https://basescan.org/tx/0x8362ac904dad8ce8f740b29d3183d8a1659ba01b2a71a1b09fe35e5c97245354), block 49972989 |
| verifier fee, 0.05 USDC | [`0x2426472b…`](https://basescan.org/tx/0x2426472b1c6ac01538d2e776fe8f7fa94f1c18cfc9fd86611238b389de15b6c5), block 49973001 |

The refund receipt names the failed term rather than reporting a generic
failure:

> contract evaluated FAIL: `.github/workflows/deploy.yml` modifies a
> contract-protected path (pattern: `.github/**`)

Both runs happened **with the runtime grader guard active**, so these receipts
attest the pipeline as it stands rather than an earlier version of it.

Mainnet balances moved exactly as the mandates specified: buyer 2.29 → 1.94
(−0.35, being 0.10 of fees plus the 0.25 released, since the refunded 0.25 came
back), provider 0.39 → 0.64 (+0.25), verifier-fee wallet 0.10 → 0.20 (+0.10),
and escrow net zero at 2.01 with 0.60 in and 0.60 out.

Both receipts re-verify offline against the published signing key
(`mergegate-e5683130`): 18 and 17 checks. They are committed under
`demo/receipts/mainnet-guarded/`. The earlier pair, run before the guard
existed, is kept in `demo/receipts/mainnet/` rather than deleted: they are
honest records of what the system did at the time, and both still verify.

## The app

Served by the same Cloud Run service that receives webhooks.

| Page | What it shows |
| --- | --- |
| `/` | Settlements, with mainnet and testnet rows distinguished |
| `/contracts/{hash}` | The pinned terms, the mandate, and the escrow funding transaction |
| `/evaluations/{id}` | Which stage the run reached, the pinned commands, path-guard result, tamper signals |
| `/receipts` | Every receipt with its live verification status |
| `/receipts/{id}` | The verdict, the binding, and the on-chain settlement |
| `/receipts/{id}.json` | The raw signed receipt, byte-identical to what was signed |
| `/verifier` | Pinned environment, grading order, the measured egress probe, defeated attacks |
| `/health`, `/api/status` | Liveness and machine-readable status |

Four properties are deliberate, and each has a test:

**Nothing rendered is illustrative.** Every figure comes from a receipt that was
issued. An empty datastore renders zeroes and an empty table rather than seeded
rows.

**Receipts are re-verified on every request** against the published public key,
not read from a stored flag. Altering a receipt changes what the page says. The
service holds only the public half of the key, so it can verify and cannot sign.

**A datastore failure is reported, not disguised.** An unreachable Firestore and
an empty system look identical on screen unless the failure is surfaced, so the
page distinguishes "could not read the datastore" from "nothing has settled".

**Evaluation stage states are derived from the manifest.** The FAIL page shows
the path guard failing and the later stages marked *not run*. A page that
always showed the same ticks would be describing a run it never read.

One honest gap: the contract page needs terms and a funding transaction, and a
receipt binds neither. Contracts are persisted at funding time now, but the two
mainnet contracts predate that. Their terms were reconstructed and then
*verified*: rebuilding each one reproduces its bound `contract_hash` exactly,
and the backfill refuses to store anything that fails that check. Where no
record exists the page says the terms were not recorded rather than inventing
them.

## Sandbox network posture: measured, not assumed

The receipt records the sandbox's egress policy, so that field has to be true.

An earlier version of this code asserted `default-deny`. Probing inside a real
Cloud Run Job showed the opposite: **Cloud Run grants internet egress by
default**, and the job reached Cloudflare on :443 and resolved DNS. MergeGate
would have signed a receipt containing a false statement, worse than having no
guarantee at all.

The fix is a custom VPC (`mergegate-sealed`) with no Cloud NAT plus an explicit
deny-all egress firewall rule, attached to the verifier job with
`--vpc-egress=all-traffic`. Re-probing gave:

| Probe | Before | After |
| --- | --- | --- |
| loopback | works | works |
| TCP to three public addresses | **reachable** | blocked |
| DNS resolution | works | **still works** |

So the claim is `deny-tcp-egress; dns-resolution-available`. A graded run
cannot fetch anything, and cannot reach an external model API, but DNS remains a
residual outbound signalling channel. That is disclosed in the constant, in the
receipt, and here, rather than rounded up to "default-deny".

What DNS is worth to an attacker is now much smaller than it was. The obvious
target would be the buyer's grader, and provider code can no longer read it (see
the runtime guard above). The environment carries no secrets and no cloud
identity to leak. What remains is a low-bandwidth channel for exfiltrating
something the provider already possesses, which is its own submission. That is a
real limit and worth stating, but it is not a route to gaming the grade.

Note this applies to the **verifier job only**. The API service needs outbound
access to reach Circle and GitHub; applying the sealed VPC to it would silently
break settlement.

## What the receipt proves

A signature over "PASS" proves someone said PASS. The value is the **binding**:
one object tying together which code, judged by which tests, in which
environment, under whose mandate, settling which payment.

Thirteen bound fields are cross-checked against the manifest and mandate the
receipt carries, so editing any of them fails verification **even for an attacker
holding the signing key**. `tests/test_receipt.py` proves this by re-signing each
tampered variant; without that test, the tampering cases would only be
demonstrating that Ed25519 works.

Five fields (`settlement_tx`, `verifier_fee_tx`, `reason`, `settlement_asset`,
`settlement_chain`) have nothing inside the receipt to check them against and
rest on the signature alone. Confirming those means comparing the receipt to the
chain, which no offline verifier can do. A receipt proves the decision was the
deterministic result of the mandate and the verdict; confirming the money moved
requires looking at Base.

## Roadmap to a permissionless market

v1 is trusted-buyer on purpose: private repos, approved providers. That is a
real limit, and it is worth being precise about *which* problem it defers,
because the honest answer is not the one that sounds most impressive.

The blocker is not verification. The evaluator already works without trusting
either party: the grader is pinned before work starts, the sandbox is neutral,
and the receipt is checkable by anyone. Nothing about that needs a trusted
buyer.

The blocker is **information asymmetry around delivery**. A provider must reveal
a working diff to be graded, and a buyer who has seen that diff has most of the
value whether or not they pay. Three things would have to hold in an open
market:

1. **A buyer who triggers an evaluation pays for it**, so failing a provider on
   a pinned technicality is not free. A slashable bond or a non-refundable
   attempt fee.
2. **Reputation with stakes**, so a buyer who systematically refuses work is
   priced out rather than merely disliked.
3. **Delivery without full disclosure**, so a provider can prove a diff passes
   without handing over the diff first.

Point 3 is where people reach for zero-knowledge proofs, and it is worth being
careful. Proving "I ran these tests and they passed" in zero knowledge is
plausible in principle and enormously expensive for arbitrary code execution
today. It is not something to promise on a slide. The nearer-term shape is
commit-reveal with escrowed disclosure: the provider commits to a diff hash, the
verifier evaluates in an enclave the buyer cannot read, and the diff is revealed
only on settlement. That trades a cryptographic guarantee for a hardware trust
assumption, which is a genuine downgrade and should be named as one.

None of this is built. It is stated so the scope limit reads as a considered
boundary rather than an unexamined one.

## Development

Requires Python 3.11+.

```bash
git clone --recurse-submodules https://github.com/4KInc/mergegate.git
cd mergegate
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check . && ruff format --check . && mypy mergegate tests && pytest -q
```

Install with `pip install -e ".[dev]"` rather than hand-picking packages. CI
does exactly that, and a venv holding a different dependency set type-checks
against different stubs. mypy passed locally while failing in CI for two
commits because `google-cloud-firestore` was absent here and present there. If
mypy disagrees with CI, clear `.mypy_cache` before believing either: a stale
cache reported success after the discrepancy was already fixed.

If you cloned without `--recurse-submodules`, the shared engine will be missing
and imports will say so:

```bash
git submodule update --init --recursive
```

### Running the demo

`mergegate/demo.py` drives one task from funding to receipt. It moves real USDC.

```bash
cp .env.example .env          # fill in, or use .env.mainnet for Base mainnet
python -m mergegate.demo pass --env .env.mainnet
python -m mergegate.demo fail --env .env.mainnet
```

Settlement runs through the `circle` CLI, not the REST API. Circle agent
wallets and Developer-Controlled Wallets are separate products holding separate
wallets, and the funded Base wallets exist only in the former. The CLI session
is a bearer credential: anyone holding it can move USDC.

Both flows push to the demo repository and rewrite its `main`, which is why
`4KInc/mergegate-demo-task` exists and holds nothing precious.

## Design

Screens in `design/screens/` were generated with Google Stitch (project
`14966020786333238841`, design system "MergeGate Proof") and carry the real
mainnet values. They are design references; the live pages are Jinja templates
in `mergegate/templates/` that reuse the same theme.

The Stitch HTML is not served directly, for one specific reason: generated
markup invents values. An audit of the regenerated screens caught a fabricated
wallet address suffix, a truncation whose tail matched no real address,
produced despite the prompt supplying the full address. That is tolerable in a
picture and unacceptable in a page rendering signed financial receipts, so every
value on a live page comes from a receipt or raises.

## License

Apache-2.0.
