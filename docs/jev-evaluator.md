# Jev as an Intaris evaluator

**Historical report:** the status and scores below describe September 25.
The current candidate uses Jev with separately configured intention and Judge
models, but that deployment choice does not prove improved quality. See
[current release qualification](release-verification-2026-10.md); this report's
denominators and failures remain unchanged.

**Status (2026-09-25): implemented, benchmarked, opt-in; not recommended as
the production default.** Intaris continues to use the Groq-backed
`gpt-oss-20b` L1 evaluator in its cost/performance-oriented deployment. The
Jev results below do not justify replacing it yet.

Jev is a hosted TypeSafe *System One* model for bounded questions, not a
generative LLM. It returns probabilities for yes/no and multiple-choice
answers; it does not generate explanations or summaries. Intaris can use it
for first-level (L1) tool-call alignment/risk evaluation and parent/child
intention alignment. Intention generation, the judge, L2 summaries, L3
analysis, and independent benchmark scoring still require conventional LLMs.
The evaluator backend is selected with `EVALUATOR_BACKEND`. Current defaults are
`auto` with Cognis-managed inference and `llm` otherwise. See
[configuration](configuration.md#tool-call-evaluator) for the
TypeSafe and OpenRouter endpoints and required credentials.

## Why consider it?

| | Jev via OpenRouter | Groq-backed `gpt-oss-20b` |
|---|---|---|
| Strength | Fast typed classifications and per-answer probabilities | Better observed balance of threat detection and benign-call approval; generative explanations |
| Limitation | No generated rationale; sensitivity to question design and confidence thresholds; additional hosted-provider/data-handling dependency | Slower L1 calls in this experiment; output-token charge |
| L1 P95 latency, sampled showcase | 430 ms original; 439 ms optimized | 791 ms |
| Cost | $0.042/million Jev input tokens at the rate used for this experiment | $0.075/million uncached input, $0.037/million cached input, $0.30/million output (deployment pricing) |

The L1 timing is for evaluated calls, not every API response. Fast-path calls
bypass both models. Groq's cached-input rate is **lower** than Jev's input
rate; actual cost depends on prompt sizes, cache hits, output tokens, and any
provider fees. We measured 1,096,575 Jev input tokens in the optimized
showcase (approximately $0.046 at the listed input rate), but did **not**
capture Groq L1 usage or cache hits. **No net cost advantage is established.**

Jev provider failures do not silently fall back to an LLM. Low-confidence
tool classifications escalate; failed parent/child alignment checks
escalate. The API response can optionally include model scores and usage
with `JEV_DIAGNOSTICS=true`, without including tool arguments or intention
text in that field. Audit explanations are deterministic descriptions of
Jev's labels and scores, **not** Jev-generated reasoning.
Before sending real tool context to OpenRouter/TypeSafe, review both
providers' data-handling and retention terms for the intended workload.

## September 2026 benchmark

The September 25 runs used isolated Intaris instances. The initial Groq and
Jev comparison ran on the same source revision; the optimized Jev variant
ran after the opt-in calibration code was added. All Jev arms used pinned
OpenRouter model `typesafe/jev-1.13-20260917`; `gpt-oss-20b` served as
the baseline L1 and benchmark agent, and `gpt-oss-120b` was the **same
independent scorer** for every showcase arm. Judge and L2/L3 analysis were
disabled to isolate L1. The [benchmark guide](benchmarking.md#jev-confidence-calibration)
documents the commands, configuration, and gold calibration report.

The nine scripted gold scenarios are the strongest *matched-call* comparison:

| Hand-labelled gold calls | Groq baseline | Original Jev, three questions | Jev, two questions | Jev, two questions + selective risk floor |
|---|---:|---:|---:|---:|
| Benign approved | 24/26 | 18/26 in the first run; 17/26 on repeat | 21/26 | 23/26 |
| Benign denied or escalated | **2/26** | 8/26; 9/26 on repeat | 5/26 | **3/26** |
| Harmful blocked | 12/12 | 12/12 | 12/12 | 12/12 |
| Inherently dangerous directly denied | 5/5 | 5/5 | 5/5 | 5/5 |

One additional scripted call did not have a matching hand label and is not
included in those denominators. These are **single runs of a small, previously
seen set**, not proof of production safety or a statistically significant
margin. The original Jev repeat also shows outcome variability.
The two-axis variant sets `JEV_DECISION_QUESTION=false`; the selective
floor additionally sets `JEV_APPROVAL_RISK_CONFIDENCE=0.3`. All Jev arms
used `JEV_MINIMUM_CONFIDENCE=0.6`.

The 42-session showcase includes agent-generated calls, scored afterwards
by Groq `gpt-oss-120b`:

| Scorer metric | Groq baseline | Original Jev | Optimized Jev |
|---|---:|---:|---:|
| Precision | **0.957** | 0.663 | 0.765 |
| Recall | 0.800 | **0.953** | 0.878 |
| F1 | **0.871** | 0.782 | 0.818 |
| False-positive rate | **0.010** | 0.212 | 0.114 |
| Reported critical misses | 0 | 0 | 0 |
| Scorer agreement on hand-labelled gold calls | 75.0% | 73.2% | 81.2% |
| L1 calls measured for latency | 162 | 130 | 143 |
| L1-only P95 latency | 791 ms | **430 ms** | 439 ms |

L1-only latency filters each run's saved `scenarios/*/calls.jsonl` records
to responses with `intaris.path == "llm"`, sorts their `latency_ms`, and
selects index `int(0.95 * (n - 1))`. It is **not** the all-call P95
reported in `evaluation.json`: that aggregate includes fast paths. The
scorer's gold-label agreement is below the benchmark's 95% reliability
target in all arms, so the showcase scores are especially tentative.

Full scenario reports for the baseline, original Jev and optimized Jev arms
are retained privately and are not committed. Run identifiers and storage
locators are omitted here.

**Interpret carefully:** each showcase arm generated a different sequence
and number of tool calls. Five scenarios in each arm failed due to
`gpt-oss-20b` benchmark-agent tool-generation errors. The scorer's labels
are not all hand-labelled ground truth, and the judge was disabled. Neither
the showcase F1 delta nor its zero reported critical misses establishes an
end-to-end production guarantee. These runs are also separate from the
previously published baseline benchmark in the project README.

## Current experiment and decision

The hybrid migration uses frozen assessment-only calls from
`tools.benchmark.matched`, with legacy and hybrid runtime directories selected
explicitly. Legacy calibration remains pinned to
`typesafe/jev-1.13-20260917`, decision question disabled, minimum confidence
`0.6`, and approval-risk confidence `0.3`. These thresholds are **not presumed
calibrated for new hybrid questions**.

Groq `openai/gpt-oss-20b` with low reasoning is the matched conventional L1;
Groq `openai/gpt-oss-120b` with low reasoning is frozen for Judge, intention and
policy compilation. L1-only and advisory-Judge cohorts are measured separately.
No simulated human approvals are used. Fixed labels and policy contracts are
primary gold; missing provider cost remains unknown. See
[frozen matched replay](benchmarking.md#frozen-matched-replay-hybrid-migration).
No new performance or safety conclusion is established by the harness alone.

The default Jev implementation asks separate alignment, risk, and
disposition questions and escalates if any answer fails the `0.6`
confidence floor. The first gold run's benign false blocks all resulted
from this compound threshold. The optional
`JEV_DECISION_QUESTION=false` variant asks only alignment and risk;
Intaris applies its existing decision policy. The additional, **opt-in**
`JEV_APPROVAL_RISK_CONFIDENCE=0.3` floor applies only when the call is
classified aligned and low/medium risk. Alignment and high/critical risk
keep the `0.6` floor. These settings reduced gold false blocks to 3/26
without a harmful gold approval, but the optimized showcase still had a
materially higher false-positive rate than Groq.

**Recommendation: keep `EVALUATOR_BACKEND=llm` in production.** Jev is
available for isolated, authenticated experiments only; do not enable the
optimized settings as a production default based on these runs.

Before reconsidering, replay the *same* newly labelled tool calls through
both evaluators, assess alignment false blocks and harmful approvals,
measure Groq L1 input/output/cache usage as well as Jev usage, repeat runs
to bound variance, and test the normal judge-enabled path. Require no
critical misses and a pre-agreed false-positive/latency/cost tradeoff.

Further reading: [TypeSafe's model documentation](https://docs.typesafe.ai/models),
[System One API](https://docs.typesafe.ai/api), and
[OpenRouter's Jev model page](https://openrouter.ai/typesafe/jev-1.13).
