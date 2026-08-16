## Inspiration

AI agents can write code. They cannot get paid for it without a human in the loop, because nobody can safely answer one question: **did this agent actually deliver what was asked?**

Card rails need a human accountable for the charge. LLM-as-a-judge replaces that human with a model that can be argued into approving anything -and we proved it. Running Gemini's screening over MergeGate's own correct PASS submission produced a false positive: it flagged a comment the provider *deleted*, attributing knowledge to the party that removed it. Had that screening carried payment authority, 0.25 USDC of real money would have gone to the wrong party.

We asked a different question: **what if the release condition were a reproducible test contract, not a model's opinion?**

## What it does

MergeGate is deterministic escrow for AI agent code delivery, settled in USDC on Base mainnet.

1. **Buyer agent** pins the terms: repo, base commit, test bundle, writable paths, protected paths, commands, deadline, price. Hashes everything into a contract and funds USDC escrow. No human clicks anything.
2. **Provider agent** reads the published mandate, sees the acceptance test is already fixed and hashed, and submits a commit.
3. **Sealed sandbox** (Cloud Run Job, gVisor, no outbound TCP) checks out the buyer's base tree, applies the provider's diff to allowed paths only, overwrites the test tree with the buyer's grader bundle, and runs only the buyer's pinned commands.
4. **Settlement** is deterministic: PASS releases escrow to the provider, FAIL refunds the buyer. The mandate is *executed*, not re-decided.
5. **One receipt** binds contract, grader, artifact, environment, decision and settlement transaction into a single object anyone can verify offline. Thirteen bound fields survive an attacker holding the signing key.

**No LLM is called anywhere** in contract creation, evaluation, settlement, or receipt issuance.

The demo's FAIL flow is the point. That submission's code was **correct** -it would have passed the buyer's tests. It was refused before the tests ran, because it also edited a protected CI file. The receipt names the exact violated term:

> contract evaluated FAIL: `.github/workflows/deploy.yml` modifies a contract-protected path (pattern: `.github/**`)

That is the difference between a control layer and a test runner wired to a transfer.

## How we built it

**Settlement:** Circle Agent Wallets for programmable escrow, driven by the `circle` CLI. Three wallet roles: buyer (funds escrow), provider (receives payment on PASS), verifier-fee (receives per-evaluation fee regardless of verdict). Double-payment has two independent guards: the settlement state machine, and the settlement key passed to Circle as a deterministic UUID5 idempotency key -verified by sending the same key twice and watching one transfer result.

**Sandbox:** Cloud Run Job on a sealed VPC (`mergegate-sealed`) with no Cloud NAT and a deny-all egress firewall rule. Verified by probing inside a real job: all outbound TCP blocked, DNS still resolves (disclosed, not hidden). The assembly order enforces neutrality: base tree → provider diff to allowed paths only → quarantine provider test hooks → purge grader paths → inject buyer's bundle → install runtime guard → hash the tree.

**Six attack vectors tested and defeated** against a real pytest process:
- Rewriting the graded tests
- A `conftest.py` hook that forces every outcome to pass
- A `sitecustomize.py` that runs before any test is imported
- Reading the reference solution out of `.git` history
- Functionally-correct code that disables the CI gate on its way past
- A submission that implements nothing and answers from a lookup table by scraping the test file at runtime

**Gemini (2 advisory roles, never in the settlement path):**
1. Pre-sandbox code security screening -analyzes the provider's diff for malicious code, supply chain attacks, test gaming, and obfuscation. Produces a risk score and flags. Does not gate the run.
2. Post-verdict failure forensics -turns `exit 1` plus a stdout dump into an actionable explanation so provider agents can retry cheaply.

The boundary is enforced by tests (`test_gemini_boundary.py`): no settlement-path module imports any advisory module (structural), the settlement directive is identical for every model output including "release the escrow immediately" (behavioural), and a diff carrying a prompt injection still refunds the buyer (adversarial).

**x402:** The verifier is sold as an x402 service. `circle services pay` from a Circle Agent Wallet verifies completely and settles 0.05 USDC on Base -agent to agent, no human, no dashboard. Three corrections were needed to get there: Circle nests the payment terms under `accepted`, Circle Agent Wallets are smart contract accounts whose ERC-1271 signatures don't ECDSA-recover to the account address, and Circle's CLI sends `payment-signature` instead of the spec's `X-PAYMENT` header. None was findable without pointing the real client at the running service.

**Receipt chain:** Ed25519-signed receipts with 13 cross-checked fields, canonical JSON (RFC 8785), Merkle hashing. Verified offline via `mergegate verify`. An MCP server exposes the read side to agents (read-only, deliberately -no funding tools).

**Infrastructure:** Python/FastAPI on GCP Cloud Run, Firestore for state, Secret Manager for keys. 22 test files, 13,000+ lines of Python, CI-enforced (ruff + mypy + pytest).

## Challenges we ran into

**A submission implementing nothing passed the tests.** It read the buyer's test file at runtime and answered from a lookup table. Every defense in the sandbox stopped the provider *editing* the tests; none stopped it *reading* them. Reading is sufficient: scrape the expected values out of the assertion, build a lookup table, return the right answers. Fixed with a CPython audit hook loaded from a `sitecustomize` module outside the workspace -installed before any test or provider code runs, and unreachable by the provider's diff.

**The sandbox reached the internet while the code said "default-deny."** That field is written into a signed receipt -MergeGate would have signed a false statement. Probing inside a real Cloud Run Job showed Cloud Run grants internet egress by default. Fixed with a custom VPC, no Cloud NAT, deny-all egress firewall rule. DNS still resolves and is disclosed rather than rounded up to "default-deny."

**Every real Circle x402 payment failed, and the logs couldn't say why.** x402 specifies the payment header as `X-PAYMENT`; Circle's CLI sends `payment-signature`. A genuine payment arrived indistinguishable from an unpaid request. Three plausible theories were wrong first. Pointing `circle services pay` at a local server that printed its own request headers answered it in one run. Circle Agent Wallets also turned out to be smart contract accounts whose ERC-1271 signatures don't ECDSA-recover to the account address -looks exactly like a forgery until you check for contract code.

**Settlement de-duplication lived in memory on a platform that cold-starts.** True in tests, false in production. Now Firestore-backed with a per-task transaction.

**Gemini produced a false positive on the first real submission it screened.** It flagged a comment the provider *deleted*, attributing knowledge of the grader to the party that removed it. Had the screening carried gating power, a correct submission would have been refused. This is exactly why the settlement path consults an exit code, not a model.

## Accomplishments that we're proud of

- **Real USDC on Base mainnet** -7 confirmed transactions: 2 escrow fundings, 2 settlements (PASS release + FAIL refund), 2 verifier fees, 1 x402 payment. Not testnet tokens.
- **The FAIL flow** -correct code refused for a policy violation, not a test failure. The receipt names the term. The tests never ran. This is the whole product.
- **6 attack vectors defeated** -against a real pytest process, not mocks. Including the grader-scraping attack that exposed a gap in the original design.
- **x402 settled with Circle's own CLI** -`circle services pay` verifies the ERC-1271 signature of a smart contract wallet and settles 0.05 USDC on Base. Agent to agent, no human.
- **Receipts re-verify offline** -17-18 checks per receipt, 13 cross-checked fields survive an attacker holding the signing key. Proven by re-signing tampered copies.
- **The false positive that proves the architecture** -Gemini flagged a correct submission. The provider was paid anyway. That's the demonstration, not a bug.

## What we learned

Deploying is a test. Four of the five hardest failures were invisible locally and appeared only against real infrastructure. The x402 one went further: it was invisible even in production logs, because a payment the server never saw is indistinguishable from a request that carried none. Reproducing a vendor's client against a server that prints what it receives found in one run what three deploys of theorising did not.

LLM-as-judge is not a safe payment authority. We observed it fail on the very first real submission, not in a synthetic test. The false positive demonstrated exactly the failure mode the architecture exists to prevent -and because the architecture was right, it didn't matter.

Circle's agent wallet stack makes sub-cent settlement viable. The x402 verifier fee (0.05 USDC) settles on Base with zero gas for the payer. That makes per-evaluation pricing possible in ways card rails ($0.30+ interchange) cannot support.

## What's next for MergeGate

- **Buyer bond** -a slashable bond posted alongside escrow, so a buyer who triggers an evaluation pays for it even on a refund. This closes the buyer-griefing gap.
- **Circle Gateway nanopayments** -so the verifier fee can be sub-cent rather than a whole transaction.
- **Coding agent integrations** -position MergeGate as the settlement layer for Devin, Cursor, OpenHands, and other coding agents delivering work for payment.
- **Delivery without full disclosure** -commit-reveal with escrowed disclosure so a provider can prove a diff passes without handing it over first. The real blocker to a permissionless market.
