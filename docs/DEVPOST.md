# Devpost submission text

Paste the block below at the very top. Every link was checked against the live
service and every transaction confirmed on-chain by block number through a
public Base RPC.

Fill in `[VIDEO LINK]` and make **both** repositories public before submitting,
or a judge following `base_sha` from the receipt hits a 404 on
`mergegate-demo-task`.

---

## Header block

> **Live on Base mainnet · Built on the Circle agent stack · Hosted on Google Cloud**
>
> **2-minute demo:** [VIDEO LINK]
> **Live app:** https://mergegate-api-1031148889398.us-central1.run.app
> **Code:** https://github.com/4KInc/mergegate
>
> | | |
> | --- | --- |
> | Mainnet PASS, provider paid 0.25 USDC | [`0xf8cb4b0f`](https://basescan.org/tx/0xf8cb4b0f35af41019b0ab57efee70ab451eaa85e718cb0eb91aed35e5acfe9b6) block 49972831 |
> | Mainnet FAIL, buyer refunded 0.25 USDC | [`0x8362ac90`](https://basescan.org/tx/0x8362ac904dad8ce8f740b29d3183d8a1659ba01b2a71a1b09fe35e5c97245354) block 49972989 |
> | Buyer agent wallet | [`0x5c34e3e0…`](https://basescan.org/address/0x5c34e3e05f0f1b9c4e3b92846791c6516dd431a2) |
> | Verifier fee wallet | [`0xe36b612b…`](https://basescan.org/address/0xe36b612ba0fd6bed653e997d5060228e548825f5) |
> | Verifiable receipt, re-checked on load | [PASS](https://mergegate-api-1031148889398.us-central1.run.app/receipts/4KInc-mergegate-demo-task-97e4bd614868) · [FAIL](https://mergegate-api-1031148889398.us-central1.run.app/receipts/4KInc-mergegate-demo-task-1758ca302557) |

## Inspiration

An AI agent can write code. It cannot get paid for it without a human in the
loop, because nobody can safely answer the question *did this agent deliver what
was asked?* Card rails need a human accountable for the charge. LLM-as-a-judge
replaces that human with a model that can be argued into approving anything.

## What it does

A buyer agent pins a task's terms, repository, base commit, test bundle,
writable paths, protected paths, commands, deadline and price, hashes them into
a contract, funds USDC escrow itself and signs a mandate: *pay X to provider Y
if and only if contract C evaluates PASS before T.* A provider agent submits a
commit. A sealed sandbox grades it against the buyer's tests in an environment
the provider cannot influence. Escrow releases or refunds. One receipt binds the
whole chain and verifies offline.

**No LLM is called anywhere** in contract creation, evaluation, settlement or
receipt issuance.

The demo's FAIL flow is the point. That submission's code was **correct** and
would have passed the buyer's tests. It was refused before the tests ran,
because it also edited a protected CI file, and the refund names the exact term:

> contract evaluated FAIL: `.github/workflows/deploy.yml` modifies a
> contract-protected path (pattern: `.github/**`)

## How we built it

Circle agent wallets for programmable escrow, driven by the `circle` CLI.
Verifier on Cloud Run Jobs under gVisor on a sealed VPC. API, dashboard and
webhook on Cloud Run. Firestore for settlement state, receipts and funded
contracts. Secret Manager for the signing key, webhook secret and CLI session.
Receipt signing, canonical JSON (RFC 8785) and Merkle hashing come from a shared
engine rather than being rebuilt.

The verifier is also priced as an **x402 service**: `circle services inspect`
reports `/x402/verify` as payable at $0.05 USDC on Base.

## Challenges

Four claims broke under testing, and finding them is most of the engineering:

1. **A submission implementing nothing passed.** It read the buyer's tests at
   runtime and answered from a lookup table. Every defense stopped the provider
   *editing* the tests; none stopped it *reading* them. Fixed with a CPython
   audit hook loaded outside the workspace.
2. **The sandbox reached the internet** while the code asserted `default-deny`,
   a field written into a signed receipt. Fixed with a no-NAT VPC and a deny-all
   egress rule. DNS still resolves and is disclosed rather than rounded up.
3. **The webhook returned 422 for every delivery** and never ran signature
   verification, from a lazy FastAPI import breaking annotation resolution. Only
   a real HTTP request revealed it.
4. **Settlement de-duplication lived in memory** on a platform that cold-starts.
   True in tests, false in production. Now Firestore-backed with a per-task
   transaction.

## Accomplishments

Both flows settled on Base mainnet with real USDC, every transaction confirmed
on-chain independently of the payment provider's own response. Double-payment
has two independent guards: the settlement state machine, and the settlement key
passed to Circle as an idempotency UUID, verified by sending the same key twice
and watching one transfer result. Receipts re-verify offline; thirteen bound
fields survive an attacker holding the signing key, proven by re-signing each
tampered variant.

## What we learned

Deploying is a test. Three of the four failures above were invisible locally and
appeared only against real infrastructure. "Local passes" meant nothing while
the local environment held a different dependency set than CI.

## What's next

x402 **settlement**, which needs a relayer holding gas to submit the signed
EIP-3009 authorization. The challenge half is live and Circle's client reads it;
no fee moves through x402 yet. A buyer bond to close the griefing gap. Beyond
that, delivery without full disclosure, which is the real blocker to a
permissionless market rather than verification, which already needs no trusted
party.

## Honest limits

Stated here rather than left for a reviewer to find:

- **x402 settlement is not implemented.** The fee that moves is a plain USDC
  transfer bound into the receipt.
- **Buyer griefing is unsolved.** A provider can verify the tests cannot change,
  not that they are passable, because the contract publishes the grader hash
  rather than the bundle.
- **The 20% demo fee rate** is a demo figure, not a business model.
- **Trusted-buyer scope**: private repos, approved providers.
- **The guarantee is verified contract acceptance**, not code quality, security
  or mergeworthiness.
- **MergeGate holds escrow authority.** This is not described as non-custodial.
