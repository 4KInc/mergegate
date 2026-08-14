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
or a real run demonstrates it. On-chain rows stayed "not yet" until they had run against real USDC and
produced a transaction hash we can cite; the mainnet rows below now do.

| Gate | What it establishes | Status |
| --- | --- | --- |
| P0.1 agent-funded escrow | Buyer agent funds and signs the mandate; no human checkout | **Done on mainnet** — the buyer agent funds escrow and seals the contract with no human step ([funding tx](https://basescan.org/tx/0xaf13670e060dfa86cd1fddd5da3171525e7934c1e76317769035a5485fa4c27d)) |
| P0.2 immutable contract + pinned grader | Terms and grader hash fixed before submission | **Done** — `mergegate/contract.py`, tested |
| P0.3 neutral sandbox verifier | Provider cannot influence the effective grader | **Done (logic)** — `mergegate/verifier/`, attacks tested end to end; verifier image built and pinned by real digest |
| P0.4 artifact binding | Pay only for the exact verified SHA + tree hash | **Done** — a new head SHA invalidates the prior verification; a stale result for a superseded SHA is dropped |
| P0.5 idempotent settlement | One contract → one settlement action | **Done** — `mergegate/settlement.py`; replayed and out-of-order event sequences settle exactly once |
| P0.6 conditional-mandate execution | Settlement is deterministic, not discretionary | **Done** — `mergegate/mandate.py`; the executor receives a decision, it does not make one |
| P0.7 bound receipt | One object binds the whole chain, offline-verifiable | **Done** — `mergegate/receipt.py`; 13 bound fields survive an attacker holding the signing key |
| P1.1 conftest / persisted-file gaming | Provider test hooks cannot survive grader injection | **Done** — hostile `conftest.py` and `sitecustomize.py` quarantined, asserted against a real pytest run |
| P1.2 `.git` history leakage | No reading reference solutions from git history | **Done** — `git archive` never creates `.git`; a run that tries to read the gold patch fails |
| P1.3 protected / graded path enforcement | Path violations reject regardless of test results | **Done** — `mergegate/paths.py`, tested |
| P1.4 sandbox isolation | No outbound TCP, no secrets, resource limits | **Done (measured)** — probed inside a real Cloud Run Job: all outbound TCP blocked, DNS still resolves (disclosed, not hidden) |
| P1.5 env-sniffing / tamper detection | Harness-tampering attempts recorded in the receipt | **Partial** — quarantined hooks and purged grader files are recorded as tamper signals; no dedicated env-sniffing probe |
| P2.1 two mainnet demo flows | PASS→release and protected-path FAIL→refund | **Done on mainnet** — both run live with real USDC, txs confirmed on-chain (see below) |
| P2.2 verifier fee | Verifier-fee tx bound into the receipt | **Partial** — escrow pays the verifier a per-run fee as a distinct mainnet tx, bound into the receipt; it is a plain USDC transfer, **not x402/Gateway** |

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

## Settlement

MergeGate settles through Circle **agent wallets**, driven by the `circle` CLI —
not the REST Developer-Controlled Wallets API. They are separate products
holding separate wallets, and the funded Base wallets exist only in the former.

Double-payment has two independent guards. The state machine refuses a second
settlement (P0.5), and the settlement key is passed to Circle as the transfer's
idempotency key, so a repeated key returns the original transaction instead of
sending a new one. Both are verified: the first by replayed and out-of-order
event tests, the second against real Circle infrastructure on Base Sepolia —
sending twice with one key moved 0.25 USDC exactly once and returned the same
transaction hash both times.

Circle requires idempotency keys to be **UUIDs** and rejects a bare
`sha256:<hex>` with `400 Invalid request body`. The rail derives a UUIDv5 from
the settlement key over a fixed namespace, so the mapping stays deterministic —
a random UUID would satisfy the format and silently destroy the guard, since a
retry would present a fresh key.

## The two demo flows, run live on Base MAINNET

Both ran end to end on **Base mainnet** with real USDC, driven by
`python -m mergegate.demo`. Every hash and transaction below came out of those
runs. Each settlement transaction was independently confirmed through a public
Base RPC, not just through Circle's response.

**PASS → release.** The provider fixed the bug; escrow paid out.

| | |
| --- | --- |
| contract | `sha256:b878016960fa33111fdc3f49840ef5e9aa93ae7cc089c9cb083ba7379b187162` |
| grader | `sha256:83018d118089f7a1a267f815dccde1933e92fff615e70d00c8a6b31dd5e2a7a6` |
| submission | `e83964b7b61edcd1eae5c425c0eacd6e0dd210ff` |
| escrow funded | [`0xaf13670e…`](https://basescan.org/tx/0xaf13670e060dfa86cd1fddd5da3171525e7934c1e76317769035a5485fa4c27d) |
| release, 0.25 USDC | [`0xb8a45ef2…`](https://basescan.org/tx/0xb8a45ef2bb14bff0ce99f5058b5a40be368424bc1e99f053993b84e9f12fe827) — block 49945815 |
| verifier fee, 0.05 USDC | [`0xeb5c1603…`](https://basescan.org/tx/0xeb5c16037349ad081168f3dcea99f912366013b45a754c39cce74b22714c0723) — block 49945839 |

**FAIL → refund.** This is the one that matters. The submission's code is
*correct* — it would have passed the buyer's tests — but it also edited
`.github/workflows/deploy.yml`. The pinned commands never ran (`commands: 0`),
and escrow returned to the buyer.

| | |
| --- | --- |
| contract | `sha256:7090242fb45b88a3eb1e0f65c2245cff1fc1ce6c5e3b85c03e2af98a2683d346` |
| submission | `272356dbfc2b165f64e8c55734364d8488a730aa` |
| escrow funded | [`0x7ae6ca91…`](https://basescan.org/tx/0x7ae6ca918d4466539ee7313015073742033c576ffad49490bcd625b47dfe20ad) |
| refund, 0.25 USDC | [`0x4581edf6…`](https://basescan.org/tx/0x4581edf6e7ab61e0f776ce52655ad77e0d7c99e85fcd235f5a53013cce895b1e) — block 49946711 |
| verifier fee, 0.05 USDC | [`0x75ca88ed…`](https://basescan.org/tx/0x75ca88edadcf72225b0baddaf7c036449c9952f667b3242cac6df4c3bb928280) — block 49946736 |

The refund receipt names the failed term rather than reporting a generic
failure:

> contract evaluated FAIL — `.github/workflows/deploy.yml` modifies a
> contract-protected path (pattern: `.github/**`)

Mainnet balances moved exactly as the mandates specified: buyer 2.64 → 2.29
(−0.60 across both runs, of which 0.25 came back as the refund), provider
0.14 → 0.39 (+0.25), and the verifier-fee wallet 0.00 → 0.10. That fee wallet
was empty beforehand, so its balance came only from these runs.

Both receipts re-verify offline against the Secret Manager signing key
(`mergegate-e5683130`) — 18 and 17 checks. They are committed under
`demo/receipts/mainnet/`, alongside the earlier Base Sepolia pair in
`demo/receipts/`.

## The dashboard

Live at **https://mergegate-api-1031148889398.us-central1.run.app**, served by
the same Cloud Run service that receives webhooks.

It reads receipts from **Firestore**, not from a bundle baked into the image, so
a settlement appears without a redeploy. Verified by deleting a receipt from
Firestore and watching the live page drop from 4 contracts to 3, then restoring
it — no deploy in between.

Receipts are **re-verified on every request** against the published public key
rather than trusted from a stored flag: altering a receipt changes what the page
says. The service holds only the public half of the signing key, so it can
verify and cannot sign.

Two things it deliberately will not do. It does not fall back to the shipped
bundle when Firestore is configured but unreachable — stale receipts presented
as live state would be a quiet lie, so it shows a failure banner distinguishing
"could not read the datastore" from "nothing has settled". And it does not
aggregate mainnet and testnet under one label: the table carries a Network
column, and the header says totals span every network shown.

## Sandbox network posture — measured, not assumed

The receipt records the sandbox's egress policy, so that field has to be true.

An earlier version of this code asserted `default-deny`. Probing inside a real
Cloud Run Job showed the opposite: **Cloud Run grants internet egress by
default**, and the job reached Cloudflare on :443 and resolved DNS. MergeGate
would have signed a receipt containing a false statement — worse than having no
guarantee at all.

The fix is a custom VPC (`mergegate-sealed`) with no Cloud NAT plus an explicit
deny-all egress firewall rule, attached to the verifier job with
`--vpc-egress=all-traffic`. Re-probing gave:

| Probe | Before | After |
| --- | --- | --- |
| loopback | works | works |
| TCP to three public addresses | **reachable** | blocked |
| DNS resolution | works | **still works** |

So the claim is `deny-tcp-egress; dns-resolution-available` — a graded run
cannot fetch anything, but DNS remains a residual signalling channel. That is
disclosed in the constant, in the receipt, and here, rather than rounded up to
"default-deny".

Note this applies to the **verifier job only**. The API service needs outbound
access to reach Circle and GitHub; applying the sealed VPC to it would silently
break settlement.

## What the receipt proves

A signature over "PASS" proves someone said PASS. The value is the **binding** —
one object tying together which code, judged by which tests, in which
environment, under whose mandate, settling which payment.

Thirteen bound fields are cross-checked against the manifest and mandate the
receipt carries, so editing any of them fails verification **even for an attacker
holding the signing key**. `tests/test_receipt.py` proves this by re-signing each
tampered variant; without that test, the tampering cases would only be
demonstrating that Ed25519 works.

Five fields — `settlement_tx`, `verifier_fee_tx`, `reason`, `settlement_asset`,
`settlement_chain` — have nothing inside the receipt to check them against and
rest on the signature alone. Confirming those means comparing the receipt to the
chain, which no offline verifier can do. A receipt proves the decision was the
deterministic result of the mandate and the verdict; confirming the money moved
requires looking at Base.

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
