# Two-minute demo script

Every URL, hash and command below is real and was checked against the live
service. Nothing here is a placeholder, so you can read it straight off the
screen without editing anything.

**Before recording**

- Browser at 125% zoom, dark theme, no bookmarks bar showing personal tabs.
- Terminal at a large font, in `~/Projects/mergegate`.
- Open these four tabs in order, so you never wait on a page load on camera:
  1. `https://mergegate-api-1031148889398.us-central1.run.app/judge`
  2. `https://mergegate-api-1031148889398.us-central1.run.app/receipts/4KInc-mergegate-demo-task-e6bd8ffbc565`
  3. `https://mergegate-api-1031148889398.us-central1.run.app/evaluations/4KInc-mergegate-demo-task-e6bd8ffbc565`
  4. `https://basescan.org/tx/0xc9a5e865dc66000fcc2478bf71ca42fe5359163c0928ff380022942178d27d25`

Lead with the FAIL flow, not the PASS flow. A payment succeeding is unremarkable;
a payment *refused* for a reason the viewer can check is the whole product.

---

## 0:00 to 0:15: the problem

> "An AI agent can write code today. It cannot get paid for it, because nobody
> can safely answer one question: did this agent actually deliver what was
> asked? Card rails need a human accountable for the charge. LLM-as-a-judge
> replaces that human with a model you can argue into approving anything.
> MergeGate answers it with a test the seller cannot touch."

*On screen:* the dashboard at `/`. Do not narrate the stat tiles.

---

## 0:15 to 0:40: the buyer agent funds escrow

> "The buyer agent pins the terms: repository, base commit, the exact test
> bundle, which files may change, which must not. It hashes all of that into a
> contract, then funds USDC escrow itself and signs a mandate. Pay this
> provider if and only if that contract passes. No human clicks anything."

*On screen:* the contract page, showing the pinned terms and the funding
transaction.

```
https://mergegate-api-1031148889398.us-central1.run.app/contracts/sha256:69fe3f44d0697a72cd07d641f7ff8c2674c3005c26c04ab2251f59f1350fab9e
```

Point at the protected paths chip (`.github/**`) and say "remember this one".

---

## 0:40 to 1:10: the submission that should have passed

> "Now the provider agent submits. Its code is correct. It fixes the bug and it
> would pass the buyer's tests. It also edits one CI file it was told not to
> touch."

*On screen:* the evaluation page. Point at the stepper: the path guard failing,
the later stages greyed out, and the line reading
"Pinned commands executed: 0".

> "The tests never ran. A submission that disables the deploy gate has not
> satisfied the contract, it has routed around it. So escrow refunded the buyer."

*On screen:* switch to Basescan tab, refund transaction, block 50060179.

> "That is real USDC on Base mainnet, and that is the difference between a
> control layer and a test runner wired to a transfer."

---

## 1:10 to 1:40: the receipt

*On screen:* the receipt page for the same run.

> "Every decision emits one receipt. It binds the contract, the grader, the
> exact commit, the tree hash, the verifier image, the decision and the
> settlement transaction into a single object. Fifteen of those fields are
> cross-checked against the manifest the receipt carries, so editing any of them
> fails verification even for someone holding the signing key. The page
> re-verifies it on every request. Seventeen checks, recomputed now, not cached."

If you have a spare beat, add the line that lands hardest with engineers:

> "We proved that by re-signing tampered copies. Otherwise the test would only
> be demonstrating that Ed25519 works."

---

## 1:40 to 2:00: the stack

> "Escrow and settlement run on Circle agent wallets, on Google Cloud. The
> verifier is sold as an x402 service and Circle's own command line pays it:
> five cents of USDC, settled on Base, agent to agent.
>
> Gemini is in here too, and where it sits is the point. It screens the diff for
> malicious code before grading, and explains failures afterwards. It cannot
> change a verdict, move escrow, or enter a receipt. On our own honest
> submission it raised a false flag, and scored that same false flag four times
> higher on one run than another. That is why it decides nothing. Intelligence
> informs; rules decide."

*Final frame:* the dashboard, or the repository URL.

---

## The strongest optional shot

If you have ten spare seconds of runtime, this is the single most persuasive
thing on camera: one agent paying another, live, with nothing hidden.

```bash
circle services pay https://mergegate-api-1031148889398.us-central1.run.app/x402/verify \
  --address 0x5c34e3e05f0f1b9c4e3b92846791c6516dd431a2 --chain BASE
```

It prints, in about five seconds:

```json
{"verified":true,"settled":true,"transaction":"0xb40552f2...","amount_usdc":"0.05"}
```

Open that hash on Basescan and the USDC transfer is there. Costs 0.05 USDC of
real money per take, so decide how many takes you want before you start.

## The closed loop, in one command

The plan's "best demo" is now a single command, and it is the strongest thing
on camera after the refusal itself:

```bash
.venv/bin/python -m mergegate.demo retry --env .env.mainnet
```

It funds escrow, submits correct code that also edits a protected file, gets
refused and refunded, reverts exactly what the contract's guard rejects,
resubmits under a **new** contract linked by `retry_of`, and gets paid. Roughly
0.60 USDC and about 90 seconds.

Say plainly that the buyer pays two verifier fees across the retry. It is the
honest cost, a judge will work it out from the transactions anyway, and saying
it first is worth more than being caught not saying it.

## If you want a live terminal moment

This costs about 0.30 USDC and takes roughly 40 seconds end to end. It is the
strongest possible evidence, and it is also the riskiest thing to do on camera.
Record it separately, keep the take that works, and cut it in.

```bash
.venv/bin/python -m mergegate.demo fail --env .env.mainnet
```

No setup needed. The runner clones the demo repository into a fresh temporary
directory and pins whatever `HEAD` is at that moment as the contract's
`base_sha`, so a previous run having moved `main` does not matter. It will
print the contract hash, the funding transaction, the verdict and the refund
transaction as it goes.

## Things not to claim on camera

These are all in the written record and a judge may have read it before watching.

- x402 **does** settle now, so you may say "paid over x402". What it does not
  do is carry the *task reward*: the 0.25 USDC release and refund are plain
  USDC transfers through Circle agent wallets. x402 carries the 0.05 verifier
  fee. Do not blur the two.
- The sealed job **is** what grades now, and its egress was measured inside it.
  "The tests ran in a sealed sandbox" is fair to say for a run whose receipt
  carries the sealed posture. It is not fair for an in-process run, which is
  still supported and says so.
- Do not say "no network". It blocks outbound TCP to the public internet, and
  leaves two channels open on purpose: DNS, and one Google API address without
  which the job cannot receive its inputs. Both are printed on the receipt.
- The guarantee is verified contract acceptance, not code quality or security.
- MergeGate holds escrow authority. It is not non-custodial.
