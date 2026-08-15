# Devpost submission text

Paste the block below at the very top. Every link was checked against the live
service and every transaction confirmed on-chain by block number through a
public Base RPC.

Fill in `[VIDEO LINK]`. Both repositories are already public, so a judge
following `base_sha` out of a receipt reaches the demo repo.

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
> | Mainnet x402 payment, verifier fee 0.05 USDC, paid by `circle services pay` | [`0xb40552f2`](https://basescan.org/tx/0xb40552f201885ff233a35c66c39114f651dc84b062aa7484ec2c974db59a86d7) block 50018597 |
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

The verifier is also sold as an **x402 service**, and Circle's own CLI pays it.
`circle services pay` against `/x402/verify` verifies the buyer's EIP-3009
authorization, including the ERC-1271 signature of a Circle Agent Wallet, and
settles 0.05 USDC on Base. Agent to agent, no human, no dashboard.

## Challenges

Five claims broke under testing, and finding them is most of the engineering:

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
5. **Every real Circle x402 payment failed, and the logs could not say why.**
   x402 specifies the payment header as `X-PAYMENT`; Circle's CLI sends
   `payment-signature`. A genuine payment arrived indistinguishable from an
   unpaid request, so the server returned the challenge and the CLI reported a
   rejection. Three plausible theories were wrong first. Pointing
   `circle services pay` at a local server that printed its own request headers
   answered it in one run. Circle Agent Wallets also turned out to be smart
   contract accounts, whose ERC-1271 signatures do not ECDSA-recover to the
   account address, which looks exactly like a forgery until you check for
   contract code.

## Accomplishments

Both task flows settled on Base mainnet with real USDC, and the verifier fee
settles over x402 driven by Circle's own CLI. Every transaction was confirmed
on-chain independently of the payment provider's own response. Double-payment
has two independent guards: the settlement state machine, and the settlement key
passed to Circle as an idempotency UUID, verified by sending the same key twice
and watching one transfer result. Receipts re-verify offline; thirteen bound
fields survive an attacker holding the signing key, proven by re-signing each
tampered variant.

## What we learned

Deploying is a test. Four of the five failures above were invisible locally and
appeared only against real infrastructure. "Local passes" meant nothing while
the local environment held a different dependency set than CI. The x402 one went
further: it was invisible even in production logs, because a payment the server
never saw is indistinguishable from a request that carried none. Reproducing a
vendor's client against a server that prints what it receives found in one run
what three deploys of theorising did not.

## What's next

A buyer bond to close the griefing gap. Circle Gateway nanopayments, so the
verifier fee can be sub-cent rather than a whole transaction. Beyond that,
delivery without full disclosure, which is the real blocker to a permissionless
market rather than verification, which already needs no trusted party.

## Honest limits

Stated here rather than left for a reviewer to find:

- **x402 carries the verifier fee, not the task reward.** The 0.25 USDC
  release and refund are plain USDC transfers through Circle agent wallets and
  are what the receipt binds. x402 settles the 0.05 fee.
- **Buyer griefing is unsolved.** A provider can verify the tests cannot change,
  not that they are passable, because the contract publishes the grader hash
  rather than the bundle.
- **The 20% demo fee rate** is a demo figure, not a business model.
- **Trusted-buyer scope**: private repos, approved providers.
- **The guarantee is verified contract acceptance**, not code quality, security
  or mergeworthiness.
- **MergeGate holds escrow authority.** This is not described as non-custodial.
