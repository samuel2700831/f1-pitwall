## Reviewer files

- [Evaluated source snapshot](f1-pitwall-source.zip): exact reviewable source plus security controls.
- [Completed scored workbook](f1-pitwall-evaluation-results.xlsx)
- [Recorded trace evidence](f1-pitwall-evaluation-evidence.zip)
- Browser review: extract `f1-pitwall-source.zip` and open `f1-pitwall/evals/reports/submission-review.html` locally; no Excel or LangSmith account needed.
- Complete scored case results: `f1-pitwall/evals/reports/paired-results.json` inside the source ZIP.
- [ZIP hashes and integrity checks](package-verification.json)

The root application files may predate Phase 2. Use the evaluated source snapshot for this evaluation submission. The included secret-scan workflow must be installed in the repository before it runs.

# F1 Pitwall evaluation submission

## Use case, agent and outcome

F1 Pitwall is an Austin 2025 historical-conditions strategy simulation for Charles Leclerc at Ferrari. The target user is a human strategy director who needs a timely, evidence-backed recommendation to stay out or consider a pit stop, with explicit uncertainty and human authorization before any stop. Rivals follow historical pace/stops; Leclerc follows simulated state and human decisions. The three deterministic specialists are Rival Analyst, Race Conditions & Incidents, and Leclerc Performance. A Gemini Strategy Director calls their tools, recalls saved intent, compares numerical strategies and submits a structured proposal.

**Evaluation one-liner:** At a frozen Austin 2025 checkpoint, can the Strategy Director use all three specialists and saved context to recommend an allowed strategy, explain decision-critical evidence, and request human review correctly within a bounded demo response and token budget?

## Five primary metrics and scoring

| Metric | Baseline | Improved | Delta | Pass bar | Improved passes |
| --- | --- | --- | --- | --- | --- |
| Original model strategy correctness | 100.00% | 100.00% | +0.00 pp | ≥95.00% | Yes |
| Original model human handoff correctness | 94.59% | 100.00% | +5.41 pp | ≥100.00% | Yes |
| Decision-critical explanation coverage | 86.96% | 100.00% | +13.04 pp | ≥90.00% | Yes |
| Checkpoint response latency | 30.14 | 26.81 | -3.32 | ≤40.00 | Yes |
| Average model token budget | 34,001.34 | 36,864.87 | +2,863.53 | ≤40,000.00 | Yes |

The first three metrics are binary per applicable case, aggregated as passed/applicable. Action correctness uses an owner-approved allowed set. Handoff correctness scores the original model proposal before application guards. Explanation coverage requires at least one term from every labeled concept group using the versioned lexical rubric. A case PASS means all its applicable contract checks pass, not a weighted average of the five headline metrics.

Latency is nearest-rank p95 across 38 genuine assessments, including the same 12-second minimum model-round pacing. Between-case waits, checkpoint preparation and LangSmith upload are excluded. Token budget is average provider-reported total tokens across all rounds per genuine case, with complete usage required. Tokens are a cost proxy; no monetary bill or savings claim is inferred. Budgets were defined after inspecting the baseline and before the candidate, as baseline-informed engineering targets.

Secondary hard guardrails separately check final-action safety, required evidence tools, exact specialist outputs, no future data, numeric grounding, no guaranteed overtakes, memory retrieval and bounded recovery. No LLM judge is used. Lexical coverage is narrower than semantic correctness; passing it does not prove every claim or strategy is optimal.

## Evaluator types

| Type | Implemented | Evidence |
| --- | --- | --- |
| Code-based | Yes | Executed deterministic quality, handoff, safety, numeric, latency/token-budget and recovery checks |
| Trajectory | Yes, narrow workflow contracts | Required delegates/tools before proposal, bounded tool/model retries and budgets, no unauthorized state change, nested trace hierarchy. Not a semantic assessment of private reasoning. |
| Human review | Expected labels approved; model-output ratings pending | Dataset approval is pinned to its hash. Workbook provides separate editable baseline/improved review fields. No response rating is fabricated. |
| LLM-as-judge | No | No judge prompt/output or judge-vs-human agreement is claimed or required for this deterministic route. |

## Golden dataset and identical-case design

All 40 expected labels were approved by the owner and remain unchanged. There are 38 genuine Gemini cases plus two explicitly scripted malformed-provider recovery fixtures. The dataset contains 6 routine, 4 tyre-wear, 6 damage, 6 weather, 6 race-control, 3 memory, 5 evidence-quality and 4 recovery cases. Memory cases include a saved instruction that cannot authorize a stop and a stale-weather note that must not override current evidence. There are 30 development and 10 preassigned holdout cases. The holdout was visible during development and is not blinded.

All case labels and substantive frozen inputs identical. Only car.telemetry.snapshot_id and nested telemetry read_at/called_at wall-clock bookkeeping are excluded from equality. Historical as_of, timing_at, last_lap_at, position_at and available_at remain compared.

Bundled race provenance is recorded in `data/austin-2025-source.json`: OpenF1 race session 9888 (meeting 1271, October 19, 2025), covering lap, stint, position, interval, pit, race-control and weather observations. The demo uses 56 historical laps. Test defaults and injected damage, weather and feed failures are simulated inputs, not claims about what happened on those laps. Historical driver pit stops do not authorize Leclerc’s simulated car to stop.

Both live cohorts use `gemini-3.5-flash-lite`, the same dataset/replay hashes and the same rubric `1.2.1` implementation hash. Prompt/source versions differ deliberately and are preserved in metadata and source snapshots.

## Results and measured delta

The untouched original baseline scored 35/40 with rubric 1.1. The same saved baseline outputs score **37/40** under 1.2.1. The live candidate scores **40/40** under that identical rubric: **+7.5 percentage points** in all-check case pass rate. Grader-only baseline fixes: rain-medium, unknown-tyres.

Primary headline gates: True. All secondary contract gates: True. Combined release gates: True.

Case regressions: none observed in this paired run. Remaining candidate failures: none in this 40-case cohort.

Performance tradeoffs: median checkpoint latency 22.81s → 25.58s (+2.78s). Mean tokens 34,001.34 → 36,864.87 (+8.42%). Higher-is-worse primary performance measures that regressed: mean_total_tokens. Passing a budget does not erase a regression.

## Top three failure findings

1. **Decision-critical context omitted.** `puncture-sc` recommended a puncture stop but omitted safety-car context. The comparable baseline has three explanation failures: `puncture-sc`, `missing-rivals`, `partial-rivals`.
2. **Original human handoff wrong with incomplete rivals.** `missing-rivals` and `partial-rivals` proposed automatic hold. Application guards corrected both, but original-model scores remain failed.
3. **Evaluator rejected valid wording.** `rain-medium` said “dry tyres” and `unknown-tyres` said “unconfirmed.” Rubric 1.1 rejected these; the revised rubric accepts them for both runs. This is an evaluator failure, not a third fabricated model failure mode. Boundary/plural regressions are covered by automated tests.

Failure modes overlap. Costs use golden-case severity points and assumed review seconds, not observed human effort or money. Comparable baseline unique failed-case review effort: 6.0 minutes; candidate: 0.0 minutes. Cluster totals must not be summed as independent case counts.

## Four targeted changes

1. **Handoff priority in the Director prompt.** Missing and partial rival data caused original needs_human=false. The prompt now prioritizes evidence gaps and explicitly copies the selected option’s approval flag before ordinary automatic hold rules.
2. **Constraints beside numerical options.** compare_strategies now returns decision_constraints: current flags, reduced transit, active damage, tyre/weather mismatch, and evidence gaps. These are computed observations and rules, not stored scenario answers.
3. **Multi-trigger explanations.** The prompt requires all critical coexisting context, including safety-car transit benefits alongside puncture urgency. It permits two to four concise sentences rather than forcing two.
4. **Versioned lexical rubric.** Rubric 1.2.1 accepts domain synonyms and common noun plurals and uses term boundaries. Both cohorts use this exact scorer. Original 1.1 outputs and scores remain immutable; two baseline case fixes are grader-only, not model improvement.

The three agent changes were evaluated as one live candidate. This does not isolate each change’s causal contribution. Earlier local policy ablations measured 20→33→37→38→40/40; those results are separate and are not Gemini quality gains.

## Regressions, limits and next steps

The candidate includes every executed case, including errors and failures; no successful retry replaced a failed quality result. One paired run is not a statistical reliability claim. Tight handoff requirements are checked separately from application corrections. Specialist accuracy is tested against frozen source inputs, not an independent physical simulator. Memory scoring verifies retrieval, not full semantic obedience.

Next: repeat the paired live experiment across seeds/time windows, add unseen incomplete-feed and combined-hazard cases, inspect explanations with an owner-calibrated semantic rubric, and reduce duplicated tool evidence if latency/token budgets fail. Preserve the lexical rubric version and never tune expected labels to make a model answer pass.

## Supporting materials and reproduction

Automated verification passed 216 backend tests and 84 separate local scenario checks; the machine-readable predeploy summary is included in the review package. Code-based and trajectory checks are exercised by the backend tests; no unit-test count is a substitute for the paired live quality results.

- `golden-v1.json`, `label-approval.json`: unchanged cases and approval tied to the dataset hash.
- `submission-metrics.json`: primary rubric, populations and numerical budgets.
- `../pitwall/agents.py`: Director prompt, tool declarations, workflow and application guards.
- `../pitwall/evals.py`: executable deterministic scorers, gates, fault injection, immutable regrading and run comparison.
- `../pitwall/specialists.py`, `../pitwall/evidence_quality.py`, `../pitwall/gemini.py`, `../pitwall/langsmith_export.py`: specialist tools, quality rules, provider adapter and trace export/readback.
- `../tests/test_phase2_evals.py`, `../tests/test_live_evals_setup.py`, `../tests/test_submission_evals.py`: adversarial grading and recovery/export regressions.
- `reports/paired-results.json`: full inputs, expected behaviors, both outputs, all checks, failures, hashes and links for every case. A spreadsheet and browser-readable report accompany it.

```sh
python3 -m unittest discover -s tests
python3 -m pitwall.evals regrade gemini-live-baseline-20261101 new-baseline-rubric
python3 -m pitwall.evals run --stage new-candidate --mode gemini --request-interval 12 --interval 10
python3 -m pitwall.langsmith_export upload new-baseline-rubric
python3 -m pitwall.langsmith_export upload new-candidate
python3 evals/build_submission.py --baseline new-baseline-rubric --candidate new-candidate
```

Original inference/source artifacts are included in the review evidence archive. Private browser races and credential files are excluded.

## LangSmith evidence

- [Comparable baseline](https://smith.langchain.com/o/7e454ef8-ab95-4d2b-85a0-1af31630a092/a/f1-pitwall/datasets/2a9e1c76-9bca-4301-b747-103e2a219dec/compare?selectedSessions=6c8ab299-488d-48cc-944a-f1fabe940527): 40 cases and 966 spans verified.
- [Improved experiment](https://smith.langchain.com/o/7e454ef8-ab95-4d2b-85a0-1af31630a092/a/f1-pitwall/datasets/2a9e1c76-9bca-4301-b747-103e2a219dec/compare?selectedSessions=68b12dcf-67fa-4803-bb6d-a92e1afa3066): 40 cases and 980 spans verified.
Both exports preserve actual recorded timing, nested model/tool calls, normalized usage, errors, case IDs, original prompt/source fingerprints and effective scoring versions. Regraded baseline trace outputs retain their original checks; current rubric scores are separate LangSmith feedback and grading-revision metadata. Workspace links require account access. The offline report and evidence package provide review without LangSmith credentials.

Baseline: 96 genuine model calls, 1,292,051 provider-reported tokens; model-request latency median 1.49s, nearest-rank p95 3.35s (pacing excluded).
Candidate: 100 genuine model calls, 1,400,865 provider-reported tokens; model-request latency median 1.41s, nearest-rank p95 2.80s (pacing excluded).
