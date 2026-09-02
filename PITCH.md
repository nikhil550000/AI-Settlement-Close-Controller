# 5-minute pitch — AI Settlement Close Controller

Razorpay AI Buildathon 2026, Track 4. Every number is read off a committed
artifact; nothing here is a claim the repo cannot reproduce.

**The spine, in one line:** *my deterministic path beats my AI on the task I
generated — and the AI is the reason the system survives the task I didn't.*

Not a feature tour. Four of the five minutes are one argument.

Measured length: **762 spoken words** — 5:04 at 150 wpm, 4:45 at 160. The three
live moments are narrated over, not silent, so the only dead air is about ten
seconds of held screen. That lands at roughly 5:00, which is tight: rehearse it
once against a timer, and if you run over, take the cut named at the bottom
rather than speeding up. Do not add material.

---

## Act 1 — the wedge (0:00 – 0:45)

> **Screen:** you, or the README positioning section.

"Razorpay already ships reconciliation. The Settlement Reconciliation API,
Razorpay Recon, and the Bookkeeping Agent — which posts accounting entries,
quote, *based on predefined rules*.

That phrase is the boundary I built against. Predefined rules cover the happy
path. This is the layer above them: the settlement-close exceptions where the
merchant's ledger, Razorpay's settlement data, and the bank statement disagree
in ways a rule can't resolve.

One command closes that loop across a hundred and fifty cases."

---

## Act 2 — the run, and the bar (0:45 – 1:40)

> **Screen:** terminal, `uv run reconcile`. It finishes in about a second. Then
> cut to `data/report.html`, already open in a second tab.

"Eighty of a hundred and fifty close fully automatically. Thirty match clean;
fifty get a correcting journal entry from an allowlist of six templates, applied
to the ledger, then re-reconciled to a zero-paise residual. That's everything
ground truth marks automatable — at zero false matches.

The other seventy aren't hidden. That's the exception list: categorised,
evidence-linked, each naming the action it needs.

Two disclosures my spec requires on camera. The data is synthetic and its ground
truth comes from the same generator, so this measures whether the pipeline
recovers injected intent — not that the intent resembles real books. And the
batch is anomaly-enriched tenfold, so match rate here isn't comparable to any
industry figure.

Seven hundred cases a second. Six hundred and five tests. A clean clone
reproduces these metrics byte for byte."

> **Screen:** scroll one `AUTO_CLOSED` case's audit trail so the six validations
> are visible. Two seconds, silent.

---

## Act 3 — the benchmark was fake (1:40 – 2:55)

> **Screen:** the console summary, cursor on the row of `1.0000`s.

"Now the part I'd actually want judged.

Every accuracy number there is one point zero zero zero zero. Perfect, across six
seeds, with no AI involved anywhere.

That is not a result. So I went looking for why.

Five of my decision boundaries were keyword lists — is this credit from Razorpay,
is this a reversal, is this a tax position. Each separated my own generator's
string pools perfectly: a hundred percent hit, zero percent miss. My import guard
couldn't see it, because a shared *vocabulary* isn't an import. Six hundred tests
couldn't see it, because not one fed the pipeline a string my generator couldn't
have written.

So I rebuilt the same hundred and fifty cases, same answer key copied byte for
byte, and changed only the words the bank used. RZRPAY instead of RAZORPAY.
SUSPENSE-CR instead of MISC CREDIT."

> **Screen:** `uv run reconcile --semantics keyword --data-dir data/heldout_vocab`.
> Let the traceback land. Hold it for a beat.

"It doesn't degrade. It cannot complete a run. Same code that scored one point
zero.

With the model reading those narrations instead —"

> **Screen:** same command, `--semantics llm --cache-path data/semantics_cache.json`.

"— a hundred and fifty out of a hundred and fifty. And no API key: the responses
are cached and committed."

---

## Act 4 — judgment on the money path (2:55 – 4:10)

> **Screen:** the two-arm contested table in README.

"That was robustness. This is judgment.

Building a second fixture, I found a real bug in my own matcher. Tier two matches
a credit to a settlement by exact amount inside a window — and that key isn't
unique to a settlement. Two settlements each saw exactly one candidate, both
claimed the same credit, both would have closed clean. A guaranteed false match,
that six hundred tests and six seeds never hit.

Fixed: when two settlements contest a credit, both abstain.

But a human resolves it in seconds — if the narration says *UPI collections* and
one settlement is entirely UPI. No rule in my spec can express that. So I let the
model try, with false match rate as the referee.

It resolved every contest that was decidable. And on one narration that said
nothing at all, it picked a settlement anyway. A coin flip presented as an answer
— and here, a coin flip books real money against the wrong settlement.

The fix wasn't a better prompt. I made the verifier check the *justification*: a
settlement only wins if its own payment method actually appears in the narration
the model read. Ungrounded answers get discarded.

Six of twelve to eight of twelve, strictly additive, zero false matches — and the
genuinely ambiguous cases are left alone."

---

## Act 5 — where AI belongs, and what broke (4:10 – 4:45)

> **Screen:** `FAILURES.md`, scrolling slowly.

"So where does AI belong? Not on the money. The ledger path is deterministic —
six templates on an allowlist, nine validations before anything posts, integer
paise end to end.

The model earns three spots: reading bank prose the rules weren't written for,
resolving a contest no rule can express, and proposing a column map for a
statement format it's never seen — which a deterministic parser accepts or
rejects. It got an unseen Kotak export right first try.

What broke and what I did about it is a file in the repo. Nine incidents,
including a constraint that made auto-close unreachable for all fifty cases, and
a review this week that caught me shipping the classifier arm that measured
*worst*.

The honest summary: my deterministic path is better than my AI on the task I
generated. The AI is the reason the system survives the task I didn't."

---

## Recording notes

**Show, don't assert.** Three live moments earn their screen time: the run
finishing in a second, the `MetricsError` on the held-out vocabulary, and the
same command succeeding with `--semantics llm`. All three verified working
offline with no API key. Everything else can be a still.

**Do not:**
- Open with architecture. Open with the wedge.
- Read the metrics table aloud. Say 80-of-150 and zero false matches, move on.
- Apologise for having no UI. They asked for a repo, a video and the
  architecture. Say "the CLI is the product" once, if at all.
- Claim the AI is essential to reconciliation. It isn't, you measured that, and
  saying so is the strongest thing in the pitch.

**If you overrun**, cut the throughput/test-count sentence in Act 2 — never Act 3
or 4.

**Likely questions, and the honest answers:**

- *"In what sense is this an agent?"* — The adapter loop proposes, a deterministic
  verifier judges, it repairs from the failure text, and it terminates at a
  budget. Everywhere a rule already decides correctly I deliberately don't invoke
  a model, and the ablation measures what that costs.
- *"Isn't the held-out vocabulary rigged?"* — The keyword lists predate it by
  several sessions, so it isn't tuned to fail. But I chose the axis knowing where
  they'd break. It proves the mechanism is real; it doesn't establish how often
  it happens in a real bank feed. That's in Known limitations.
- *"Why is match rate only 20%?"* — §1.6 defines `match_rate` as no-adjustment
  matches over total cases, and the batch is anomaly-enriched tenfold on purpose.
  The number that answers your question is 80 of 150 closed automatically, which
  is everything ground truth says is closable.
- *"What would you do next?"* — Contested credits currently fall out of the batch
  entirely — about twelve thousand rupees attached to no case and no metric. It's
  safe but invisible, it's in Known limitations, and it's the first thing I'd fix.
