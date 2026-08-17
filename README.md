# MergeGate - Circle Agentic Economy Prize Submission

[![CI](https://github.com/4KInc/mergegate/actions/workflows/ci.yml/badge.svg)](https://github.com/4KInc/mergegate/actions/workflows/ci.yml)

> Submission for the [$50K Circle Agentic Economy Prize](https://www.xprize.org/prizes/build-with-gemini) (Build with Gemini XPRIZE)

---

### A buyer agent funds USDC escrow against an acceptance test it pins and hashes before any work exists, and a sealed deterministic evaluator, not a model and not a human, decides whether the money moves.

An AI agent can write code today. It cannot get paid for it without a human in the loop, because nobody can safely answer the question *did this agent actually deliver what was asked?* Card rails need a human accountable for the charge. LLM-as-a-judge replaces that human with a model that can be argued into approving anything.

MergeGate answers it with a test the seller cannot touch. The proof is a submission whose code was **correct** and was refused anyway: [0.25 USDC refunded on Base mainnet](https://basescan.org/tx/0xc9a5e865dc66000fcc2478bf71ca42fe5359163c0928ff380022942178d27d25), because it also edited a file the contract protected.

**This is a native Circle agent-to-agent economy, not a verifier with a payment rail bolted on.** A buyer agent funds programmable USDC escrow through a Circle Agent Wallet and signs the release condition before any work exists. Settlement releases or refunds as Circle Agent Wallet transfers on Base mainnet, submitted by Circle's own relayers so MergeGate pays no gas on those legs ([release](https://basescan.org/tx/0xa1303e97235b39357d73ff82d90c6f6d757dafc2490abb18aa37098cf06dfbae) submitted by `0x211d9824…`, [refund](https://basescan.org/tx/0xc9a5e865dc66000fcc2478bf71ca42fe5359163c0928ff380022942178d27d25) by the same, [verifier fee](https://basescan.org/tx/0x6f94ef377c10f961a5252eadd8832ade991c47d22a76788e73ea81fe65507d5f) by `0xe1963570…`, none of them a wallet we fund). And the verifier itself is an x402 service that **Circle's own CLI pays**: `circle services pay` verifies the buyer's EIP-3009 authorization, including the **ERC-1271 signature of a Circle Agent Wallet**, before settling the fee. Remove Circle and there is no escrow, no gas model, no agent-payable verifier, and no settlement.

Paying for code is the **application**. A release condition no party can move is the **thesis**.

---

## Judge's Path (60 seconds)

| What | Link |
|------|------|
| **Judge landing page** | [mergegate.dev/judge](https://mergegate.dev/judge) - the whole case on one page: the refusal, the payment, where it ran, what it does not claim |
| **The refusal that matters** | [FAIL evaluation](https://mergegate.dev/evaluations/4KInc-mergegate-demo-task-e6bd8ffbc565) - correct code, rejected before the tests ran, `commands executed: 0` |
| **Mainnet refund tx** | [0.25 USDC back to buyer](https://basescan.org/tx/0xc9a5e865dc66000fcc2478bf71ca42fe5359163c0928ff380022942178d27d25) - block 50060179 |
| **Verifiable receipt** | [PASS](https://mergegate.dev/receipts/4KInc-mergegate-demo-task-e8a00740eb5f) - re-verified on every page load, never a cached flag |
| **Repo + tests** | [GitHub](https://github.com/4KInc/mergegate) - 490 tests across 26 files, CI-enforced (`ruff` + `mypy` + `pytest`) |

## Eligibility Confirmation

| Requirement | Evidence |
|-------------|----------|
| Uses Circle Agent Stack | Agent Wallets and Circle CLI (2 of the 5 named components), plus x402 / EIP-3009 settlement with ERC-1271 Agent-Wallet signature verification. See [coverage](#circle-agent-stack-coverage-2-of-5-named-components-and-the-depth-that-matters) |
| Public GitHub repo | [4KInc/mergegate](https://github.com/4KInc/mergegate) |
| Real USDC transaction | [3 mainnet txs on Basescan](#mainnet-transactions-base-l2) |
| Agent wallet addresses | [Buyer](https://basescan.org/address/0x5c34e3e05f0f1b9c4e3b92846791c6516dd431a2), [Escrow](https://basescan.org/address/0x0c744ecb3949b3582cdd2dbc70dc876405eec44d), [Provider](https://basescan.org/address/0xbe1424b7bcc149523f749ceb7a8316d8ba6ba558), [Verifier fee](https://basescan.org/address/0xe36b612ba0fd6bed653e997d5060228e548825f5) |
| Agent-driven, not human checkout | The buyer agent funds escrow and signs the mandate itself; settlement executes that mandate. The only human action in the system is provisioning the wallet credential once, the way a service account is provisioned |

## What Is This?

Most agent-payment demos prove an agent *can* spend money. **MergeGate proves an agent can be refused.**

A buyer agent pins the terms of a coding task: repository, base commit, the exact test bundle, which files may change, which must not, the commands to run, the deadline, the price. It canonicalizes and hashes all of it into a `contract_hash`, funds USDC escrow itself, and signs a mandate: *pay X to provider Y if and only if contract C evaluates PASS before T*.

A provider agent submits a commit. A sealed evaluator assembles the base tree, applies the provider's diff to allowed paths only, **overwrites the test tree with the buyer's grader bundle**, and runs only the pinned commands. Escrow releases or refunds on that result alone.

> **No LLM sits in the payment-authority path.** No model is called at any point in contract creation, evaluation, settlement, or receipt issuance. The release condition is a reproducible test contract, not a model's opinion, an optimistic timeout, or a discretionary approval.

Gemini **is** here, on the other side of that line, in four advisory roles. It cannot change a verdict, move escrow, or enter a receipt, and that boundary is enforced by tests rather than by convention. See [Gemini, and where it is not](#gemini-and-where-it-is-not), including the false positive it produced on our own honest submission.

## How It Works (5 Steps)

```
Buyer agent wants code written
      |
      v
1. PIN THE ACCEPTANCE TEST, THEN FUND
   Terms hashed into contract_hash BEFORE the provider sees the task.
   Buyer funds escrow with reward + verifier fee, signs the mandate.
      |
      v
2. PROVIDER AGENT SUBMITS A COMMIT
   It can read the terms and confirm the grader hash is already fixed,
   so it knows the goalposts cannot move after submission.
      |
      v
3. SEALED EVALUATION
   Cloud Run job, pinned by image digest, on a VPC with no Cloud NAT.
   Base tree -> provider diff (allowed paths only) -> quarantine provider
   test hooks -> purge grader paths -> inject the BUYER's bundle -> run
   only the pinned commands. The provider cannot influence the grader.
      |
      v
4. THE MANDATE IS EXECUTED, NOT RE-DECIDED
   PASS releases to the provider. FAIL refunds the buyer.
   No discretion exists at this step: the directive is a total function
   of the mandate and the manifest.
      |
      v
5. ONE SIGNED RECEIPT
   Binds contract, grader, artifact, tree, image digest, execution id,
   funding tx, verdict and settlement tx. Re-verifiable offline.
```

**Step 3 is the innovation.** Anyone can run tests and trigger a transfer. The hard part is making the test result something the *seller* cannot influence, and proving it, which is why the attacks are executed against a real repository with a real `pytest` process rather than mocked.

### Settlement lifecycle

```
FUNDED -> SUBMITTED -> VERIFYING -> PASS/FAIL -> SETTLED or REFUNDED
                                             \-> EXPIRED
```

`EXPIRED` exists because a task the verifier never answered used to stay open forever with escrow funded. It returns the buyer's money when the deadline passes with no verdict. **A task that has a verdict may never expire**: `execute_mandate` checks the deadline before the verdict, so if graded tasks could expire, anyone able to delay settlement past `T` could convert a provider's PASS into a refund by doing nothing.

## Mainnet Transactions (Base L2)

Both flows executed on **Base mainnet** with real USDC, graded inside the sealed Cloud Run job. Every settlement was independently confirmed through a public Base RPC, not only through the payment provider's response.

| Flow | From | To | Amount | Basescan |
|------|------|----|--------|----------|
| Escrow funded | Buyer `0x5c34...` | Escrow `0x0c74...` | 0.30 | [block 50059994](https://basescan.org/tx/0x0d8caf15d5c6953b3e3677ba44ea831728508666906e76edba7109c20c672805) |
| **PASS, released** | Escrow `0x0c74...` | Provider `0xbe14...` | 0.25 | [block 50060061](https://basescan.org/tx/0xa1303e97235b39357d73ff82d90c6f6d757dafc2490abb18aa37098cf06dfbae) |
| **FAIL, refunded** | Escrow `0x0c74...` | Buyer `0x5c34...` | 0.25 | [block 50060179](https://basescan.org/tx/0xc9a5e865dc66000fcc2478bf71ca42fe5359163c0928ff380022942178d27d25) |
| **x402 verifier fee** | Buyer `0x5c34...` | Fee wallet `0xe36b...` | 0.05 | [block 50018597](https://basescan.org/tx/0xb40552f201885ff233a35c66c39114f651dc84b062aa7484ec2c974db59a86d7) |

The PASS was graded in execution `mergegate-verifier-5rbrl`, the FAIL in `mergegate-verifier-mc5bj`, and both receipts carry the network posture measured from inside those jobs.

**These are self-paid.** All of these transfers are between wallets MergeGate operates. They demonstrate that the settlement mechanism genuinely executes on mainnet. They are **not** third-party revenue, and MergeGate has had none. See [Limitations](#limitations--honest-assessment).

## The Refusal, In Detail

The FAIL flow is the one worth opening, because it is the only thing here that a test runner wired to a transfer could not do.

That submission fixed the bug correctly. It would have passed the buyer's tests. It also rewrote `.github/workflows/deploy.yml`, which the contract listed as protected, so **the pinned commands never ran at all** and escrow returned to the buyer. The receipt names the term rather than reporting a generic failure:

> contract evaluated FAIL: `.github/workflows/deploy.yml` modifies a contract-protected path (pattern: `.github/**`)

A submission that disables the deploy gate has not satisfied the contract, it has routed around it.

### The attacks that are actually executed

Not described, run. Against a real repository, with a real `pytest` process, asserted to fail:

| Attack | Defense |
|---|---|
| Rewrite the graded tests | Grader bundle injected *after* the diff, overwriting anything left there |
| `conftest.py` hook forcing every outcome to pass | Provider-introduced test hooks quarantined anywhere in the tree |
| `sitecustomize.py` running before any test imports | Same quarantine, applied before interpreter startup |
| Read the reference solution out of `.git` | Repository metadata not present in the graded tree |
| Read the buyer's tests at runtime and answer from a lookup table | CPython audit hook loaded outside the workspace |
| Correct code that disables the CI gate | Path guard, evaluated before any command runs |

One defense is worth naming because the obvious version is insufficient: a path guard alone permits `src/conftest.py`, which sits inside an allowed source path, and pytest will still execute it. **Allowed to write is not allowed to grade.**

## Circle Agent Stack Coverage: 2 of 5 named components, and the depth that matters

Counted honestly rather than rounded up, because the interesting number here is not how many components are ticked but how much of the system stops working without them.

| Named component | Status | Evidence |
|----------------|---|---------|
| **Agent Wallets** | **Used** | 4 wallets (buyer, escrow, provider, verifier fee) with independent spending policies read live from Circle via `circle wallet limit` (`payments/policy.py`), shown on [/wallets](https://mergegate.dev/wallets) |
| **Circle CLI** | **Used** | Every transfer, funding, release, refund and balance query shells out to the `circle` binary, not the REST API (`payments/circle_cli.py`, `circle wallet transfer …`) |
| **Gateway Nanopayments** | Not used | Evaluated and proven unusable for these wallets. See [below](#circle-nanopayments-measured-not-adopted) |
| **Agent Marketplace** | Not used | MergeGate is not listed there. It publishes an agent-discoverable [OpenAPI spec](https://mergegate.dev/openapi.yaml) and a 9-tool MCP server, but that is discovery, not the Marketplace |
| **Circle Skills** | Not used | `circle skill install` consumes skills published to `circlefin/skills`. [SKILL.md](SKILL.md) is MergeGate's own agent-facing guide and is not published there |

**Separately, and not one of the five:** MergeGate settles the verifier fee over **x402 / EIP-3009**, and `circle services pay` verifies the payer's **ERC-1271 Agent-Wallet signature** before settling. That is a Circle-supported settlement standard rather than a named Stack component, so it is described rather than counted.

### Why two components is the wrong thing to measure

Take Circle out and nothing is left to demonstrate. There is no escrow to fund, because escrow *is* a Circle Agent Wallet under a policy the operator cannot widen. There is no gas model, because the release and refund legs are submitted by Circle's relayers and MergeGate pays nothing for them. There is no agent-payable verifier, because the fee is collected by Circle's own CLI verifying a Circle Agent Wallet's signature. And there is no settlement, because the mandate executes as a `circle wallet transfer`.

The two components MergeGate does use are load-bearing for the entire economy. The three it does not are, in this design, either unusable (Gateway) or unrelated to settlement (Marketplace, Skills).

### Circle Nanopayments: measured, not adopted

MergeGate settles on Circle Agent Wallets with gas sponsored by Circle, so the flat 0.05 USDC verifier fee costs the buyer nothing in gas. Gateway was evaluated as the rail for charging per unit of evidence rather than per evaluation.

It is not wired in, and the reason is specific rather than a matter of time. A Gateway balance has to live somewhere, and for these wallets it cannot live on Base. The `direct` deposit method keeps funds on the source chain but requires native gas there, and these agent wallets hold **zero ETH by design**, which is the same property that makes the escrow legs cost us no gas. The `eco` method works without gas but lands the balance on **Polygon**, which is not the chain MergeGate settles on. A real 0.5 USDC eco deposit was executed on mainnet ([`0x1bf870c6...`](https://basescan.org/tx/0x1bf870c62f43f944122d51cdc5f2b56c4e708c95cc7dbae4c56dee83f6560fc7)) and had not credited to a queryable Gateway balance on any supported chain when this was written.

One framing is worth refusing explicitly, because it is the obvious pitch. Gateway is not a way to avoid per-payment on-chain cost here: a transfer out of Gateway settles through an on-chain `gatewayMint` on the destination chain, so the transaction count per payment is unchanged. Gateway's real contribution is a unified balance spendable across chains without pre-funding each one. That is genuinely useful to a multi-chain evidence market and is the shape of the v2 argument, but it is not a claim this deployment has earned.

## Gemini, and Where It Is Not

Four advisory roles, each bounded by deterministic code that decides what may be *acted on*.

| Role | What Gemini does | What decides |
|---|---|---|
| **Draft** | Turns a plain request into structured contract terms | A policy engine validates; a draft that fails cannot be funded |
| **Assess** | Rates feasibility before a provider accepts | Confidence is capped deterministically when the tests are hidden |
| **Screen** | Flags malicious code and test gaming in the diff | Nothing. The verdict is already computed |
| **Explain** | Turns a FAIL into a structured retry plan | The contract's own path guard refuses plans naming protected paths |

**The boundary is enforced three ways**, in `tests/test_gemini_boundary.py`: the settlement modules are parsed and asserted not to import the advisory layer, settlement is byte-identical for hostile model output, and a diff that successfully steers the screening still refunds correctly. Prompt injection is not prevented. It is made **worthless**.

### The false positive we published

Screening our own honest PASS submission, Gemini flagged the provider for knowing the grader's assertions. It was objecting to a comment that lives in the **buyer's own base tree**, which the provider's diff *deletes*.

It has now scored that same wrong finding **40/100, then 10/100, then 25/100** across three runs on equivalent input. Not converging.

Had it carried gating power, correct work would have been refused and 0.25 USDC would have gone to the wrong party, and whether it was refused would depend on which run you got. That is the argument for the architecture rather than an embarrassment to it. The tests passed, the guard found no violation, and the provider was paid with the flag visible on the evaluation page next to a payment that completed normally.

## Where It Runs

Grading happens inside a sealed Cloud Run job, pinned by image digest, on a VPC with no Cloud NAT. The posture below was **measured from inside that job**, on the pinned image, by [`egress_probe.py`](mergegate/verifier/egress_probe.py), and is written into the signed receipt.

```
deny-tcp-egress-except-google-restricted-vip-199.36.153.4/30; dns-resolution-available
```

| Probe | Before | Now |
| --- | --- | --- |
| loopback (control) | works | works |
| `1.1.1.1:443` | **reachable** | blocked |
| `142.250.72.46:443` (a Google *public* address) | blocked | blocked |
| `199.36.153.4:443` (restricted API VIP) | reachable | **still reachable, deliberately** |
| DNS resolution | works | **still works** |

**Not a flat deny, and the reason is worth stating.** A completely sealed job cannot receive its inputs: they arrive on a Cloud Storage volume, and gcsfuse dials `storage.googleapis.com` from inside the same network namespace as the graded code. The first live sealed run died at mount. Exactly one destination is now allowed, and the posture string names it rather than rounding it off. Two residual channels are disclosed: that VIP, and DNS.

## What the Receipt Proves

A signature over "PASS" proves someone said PASS. The value is the **binding**: one object tying together which code, judged by which tests, in which environment, under whose mandate, settling which payment.

**15 of the 22 bound fields are cross-checked** against the manifest and mandate the receipt carries, so editing any of them fails verification **even for an attacker holding the signing key**. `tests/test_receipt.py` proves this by re-signing each tampered variant; without that, the tampering cases would only demonstrate that Ed25519 works.

Seven fields (`settlement_tx`, `verifier_fee_tx`, `funding_tx`, `execution_id`, `reason`, `settlement_asset`, `settlement_chain`) have nothing inside the receipt to check them against and rest on the signature alone. Confirming those means comparing the receipt to the chain, which no offline verifier can do.

## Try It Live

### Verify a receipt

```bash
# In the browser: every receipt page re-verifies on load
open https://mergegate.dev/receipts/4KInc-mergegate-demo-task-e6bd8ffbc565

# On your machine, offline, with no MergeGate access
git clone --recurse-submodules https://github.com/4KInc/mergegate.git
cd mergegate && pip install -e .
mergegate verify receipt.json --public-key bKniJaFvoeSt4_LmdfiKemxeIqaz-ALsjSFtiNWzA8U
```

Exit `0` verified, `1` failed verification, `2` could not check. **Do not treat 2 as 1**: that is a fraud alarm raised over your own missing key. `--recurse-submodules` is not optional, because canonical JSON, Merkle hashing and signature verification come from a shared engine rather than being reimplemented.

### Pay the verifier over x402

```bash
circle services pay https://mergegate.dev/x402/verify \
  --address 0x5c34e3e05f0f1b9c4e3b92846791c6516dd431a2 --chain BASE
```

**Circle's own CLI is the payer here, and Circle's own wallet standard is what gets verified.** `circle services pay` presents an EIP-3009 authorization; MergeGate verifies it, and because a Circle Agent Wallet is a smart contract account rather than an EOA, that verification goes through **ERC-1271 `isValidSignature`** (`x402_settle.py:280`) rather than ECDSA recovery. Then 0.05 USDC settles on Base. Agent to agent, no human, no dashboard.

### Run the whole loop

```bash
python -m mergegate.demo retry --env .env.mainnet
```

FAIL, remediate, PASS, paid. Roughly 0.60 USDC and about 90 seconds. The buyer pays **two** verifier fees across a retry, which is the honest cost and the reason retries are budgeted.

## MCP Server

Nine read-only tools, hand-rolled stdio JSON-RPC with no SDK dependency.

```json
{"mcpServers": {"mergegate": {"command": "mergegate-mcp",
  "env": {"MERGEGATE_SERVICE": "https://mergegate.dev",
          "MERGEGATE_RECEIPT_PUBLIC_KEY": "bKniJaFvoeSt4_LmdfiKemxeIqaz-ALsjSFtiNWzA8U"}}}}
```

| Tool | For |
| --- | --- |
| `mergegate_status` | Anyone. What this deployment attests, and what it does not |
| `mergegate_draft_task` | Buyers. Terms from a plain request, **plus the policy verdict** |
| `mergegate_inspect_contract` | Providers. The pinned terms, including `terms_visibility` |
| `mergegate_assess_contract` | Providers, before accepting. Feasibility plus a path check |
| `mergegate_get_retry_plan` | Providers, after a FAIL. A plan and whether it is actionable |
| `mergegate_list_receipts` / `mergegate_get_receipt` | Either. Did I get paid, and against what |
| `mergegate_verify_receipt` | Either. Re-verify against a **pinned** key |
| `mergegate_wallet_policies` | Counterparties. What these wallets can and cannot spend |

**Read only, deliberately.** Nothing on it funds escrow, signs a mandate or moves USDC. An MCP server is driven by whatever the model decides to call, so a funding tool would be a wallet-draining primitive one prompt injection away. A test asserts no tool name contains `fund`, `pay`, `transfer`, `settle` or `sign`.

## Why Not Just Use...

| Alternative | Why it does not answer this |
|---|---|
| **CI plus a transfer** | CI tells you the tests passed. It does not tell you the *seller* could not influence which tests ran, and that is the entire question when the seller is being paid on the result |
| **LLM-as-a-judge** | A model that can be argued into approving anything is not a release condition. Our own screening flagged an honest submission and scored it three different ways on equivalent input |
| **Escrow with human release** | That is the human in the loop this exists to remove |
| **Optimistic release with a challenge window** | Works when disputes are rare and adjudication is cheap. For code, adjudication is the expensive part, which is what MergeGate makes deterministic |

## Economics

Measured, not projected. Full detail in [ECONOMICS.md](ECONOMICS.md).

| | PASS | FAIL |
| --- | --- | --- |
| Revenue (verifier fee) | $0.05 | $0.05 |
| Settlement gas | $0 (Circle sponsors) | $0 |
| Gemini | $0.0025 | $0.0049 |
| Compute | ~$0 (free tier) | ~$0 |
| **Gross margin** | **$0.0476** | **$0.0451** |

Two measurements worth keeping, and one correction. **Gas on the four escrow legs is genuinely zero to MergeGate**: each was submitted by an address we neither configure nor fund, because Agent Wallets sponsor gas. Those submitters are *three different* Circle relayers, not one, which an earlier version of this README got wrong. **The x402 leg is the exception and is not sponsored**: EIP-3009 makes the recipient submit, so MergeGate runs its own relayer (`0x349eF760…`, derived from a key we hold) and pays 96,381 gas at 0.006 gwei, about 0.00000058 ETH. Second, **thinking tokens are 80% of the Gemini bill**, six times the visible output, so any cost model built from prompt and response length alone understates it by roughly 4x.

**The 20% demo fee rate is indefensible and we know it.** 0.05 on a 0.25 reward was chosen so both numbers are legible in a block explorer. A 20% take on delivered work is far outside what code marketplaces sustain. The rate is also the wrong shape: verification cost is roughly flat in task size while a percentage fee grows without bound. A defensible price is $0.05 to $0.25 flat, which on a $50 task is 0.1% rather than 20%. None of it is validated, because there are no customers.

## Architecture

```
Buyer agent                Provider agent
     |                           |
     | fund + sign mandate       | submit commit
     v                           v
  Circle Agent Wallets      GitHub webhook (HMAC)
     |                           |
     +------------+--------------+
                  v
        Settlement state machine  (Firestore, per-task transaction)
                  |
                  v
        Sealed Cloud Run job      (no Cloud NAT, image pinned by digest)
          base tree -> diff -> quarantine hooks -> inject grader -> run
                  |
                  v
        VerificationManifest      (only the job can construct one)
                  |
                  v
        execute_mandate()         (total function; no discretion)
                  |
                  v
        Circle transfer + Ed25519 receipt
```

The orchestrator re-checks the returned manifest against the request that asked for it and **refuses on any mismatch rather than degrading to a FAIL**, because an orchestrator that could turn "I could not reach the verifier" into "the work is rejected" would be a way to refuse payment by breaking infrastructure.

## Proof Items

| Item | Value |
|------|-------|
| Public repo | [github.com/4KInc/mergegate](https://github.com/4KInc/mergegate) |
| Judge landing page | [`/judge`](https://mergegate.dev/judge) |
| **Mainnet PASS** | [Escrow to Provider, 0.25 USDC](https://basescan.org/tx/0xa1303e97235b39357d73ff82d90c6f6d757dafc2490abb18aa37098cf06dfbae) block 50060061 |
| **Mainnet FAIL** | [Escrow to Buyer, 0.25 USDC](https://basescan.org/tx/0xc9a5e865dc66000fcc2478bf71ca42fe5359163c0928ff380022942178d27d25) block 50060179 |
| **Mainnet x402** | [0.05 USDC via `circle services pay`](https://basescan.org/tx/0xb40552f201885ff233a35c66c39114f651dc84b062aa7484ec2c974db59a86d7) block 50018597 |
| Signed receipts | [PASS](https://mergegate.dev/receipts/4KInc-mergegate-demo-task-e8a00740eb5f) and [FAIL](https://mergegate.dev/receipts/4KInc-mergegate-demo-task-e6bd8ffbc565) |
| Evaluation detail | [`/evaluations/{id}`](https://mergegate.dev/evaluations/4KInc-mergegate-demo-task-e6bd8ffbc565) - stepper, failed term, commands run |
| Pinned contract terms | [`/contracts/{hash}`](https://mergegate.dev/contracts/sha256:69fe3f44d0697a72cd07d641f7ff8c2674c3005c26c04ab2251f59f1350fab9e) |
| Sandbox probe | [`/verifier`](https://mergegate.dev/verifier) - measured egress, before and after |
| Wallet policies | [`/wallets`](https://mergegate.dev/wallets) - read live from Circle, never a typed table |
| Integration guide | [`/docs`](https://mergegate.dev/docs) |
| OpenAPI spec | [`/openapi.yaml`](https://mergegate.dev/openapi.yaml) and [`/openapi.json`](https://mergegate.dev/openapi.json) - generated from the running app |
| x402 endpoint | [`/x402/verify`](https://mergegate.dev/x402/verify) - answers 402 with a payment challenge until paid |
| Verification key | [`/api/verification-key`](https://mergegate.dev/api/verification-key) |
| Agent skill | [SKILL.md](SKILL.md) - written for buyer *and* provider agents |

## Tests

**490 tests across 26 files.** CI-enforced with `ruff` + `mypy` + `pytest` on every push.

| Suite | Tests | Covers |
|-------|-------|--------|
| `test_web` | 43 | Dashboard, receipts, judge page, live-data derivation |
| `test_receipt` | 40 | Binding, tampering under a held signing key, offline verification |
| `test_drafting_and_retry` | 32 | Gemini draft policy gate, retry plans, path checking |
| `test_contract` | 27 | Immutability, grader pinning, terms visibility |
| `test_integrate` | 26 | Documented surface matches the running app, contrast, nav |
| `test_webhook` | 25 | HMAC, replay, out-of-order delivery |
| `test_settlement` | 25 | State machine, idempotency, expiry, the griefing guard |
| `test_x402_settle` | 24 | EIP-3009, ERC-1271 smart-account signatures, forged rejection |
| `test_gemini_boundary` | 23 | Structural, behavioural and adversarial advisory isolation |
| `test_demo` | 22 | Both flows end to end, remediation, the closed retry loop |
| `test_payments` | 21 | Rail behaviour, idempotency keys, fee non-fatality |
| `test_screening` | 20 | Diff screening, prompt-injection resistance |
| `test_paths` | 20 | Path guard classification |
| `test_sandbox_spec` | 18 | Sandbox spec asserted as the object actually submitted |
| `test_mcp` | 18 | Protocol handler, read-only tool-name enforcement |
| `test_app` | 17 | HTTP boundary, OpenAPI generation and drift |
| `test_sealed_evaluation` | 15 | Dispatch, result verification, refusal on mismatch |
| `test_verifier_neutrality` | 13 | The documented attacks, executed |
| `test_cli` | 12 | Exit codes, key provenance, dash-leading keys |
| `test_feasibility` | 11 | Confidence cap when criteria are hidden |
| `test_git_source` | 10 | Base tree materialization, submission building |
| `test_store` | 9 | Firestore-backed state, transactional apply |
| `test_egress_probe` | 6 | Probe arithmetic, dead-control detection |
| `test_grader_confidentiality` | 5 | Runtime read of the grader blocked |
| `test_end_to_end` | 5 | Full pipeline |
| `test_execution_environment` | 4 | Receipts cannot claim isolation the run lacked |

## Limitations & Honest Assessment

Stated here rather than left for a reviewer to find.

| Limitation | Status |
|---|---|
| **Verified contract acceptance is not code quality** | A PASS means the submission satisfied the buyer's pinned tests. It does not mean the code is good, secure, or mergeable. METR's work found test-passing is a poor proxy for what a maintainer merges |
| **Buyer griefing is unsolved** | A bad-faith buyer can pin an unpassable test, read the diff, and take the refund. A provider can verify the tests **cannot change**, not that they are **passable**. Scope is trusted buyers and approved providers. The v2 fix is a slashable buyer bond, and it is not built |
| **Custody is real** | MergeGate holds escrow authority. This is programmable escrow with policy-bound settlement. It is **not** non-custodial and **not** trustless |
| **The sandbox has two residual channels** | DNS, and one Google API address without which the job cannot receive its inputs. Both are disclosed on the receipt |
| **Gemini has been wrong here, publicly** | 40/100, then 10/100, then 25/100 on equivalent input. Advisory only, and that is why |
| **x402 carries the verifier fee, not the reward** | The 0.25 release and refund are plain USDC transfers through agent wallets |
| **The 20% demo fee rate** | A demo figure chosen for explorer legibility, not a business model |
| **No customers, no third-party revenue** | Every transaction shown is self-paid between wallets we operate |
| **Nanopayments not adopted** | Evaluated and documented above, with the deposit tx that did not credit |
| **Seven receipt fields rest on the signature alone** | Confirming those means looking at the chain |

## The One Sentence

> MergeGate is the settlement layer for agent-to-agent software work: the buyer pins the acceptance test before it funds anything, a sealed deterministic evaluator the seller cannot influence decides, and escrow releases or refunds on that verdict alone, leaving one signed receipt that anyone can re-verify offline.

## Documentation

| Document | What it covers |
|---|---|
| [POSITIONING.md](POSITIONING.md) | Prior art, the defensible wedge, honest boundaries in full |
| [ECONOMICS.md](ECONOMICS.md) | Measured unit economics, retry cost, why the demo rate is indefensible |
| [SKILL.md](SKILL.md) | Agent-facing guide for buyers and providers |

## License

Apache-2.0
