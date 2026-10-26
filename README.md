# F1 Pitwall — Austin 2025

A working strategy-director application for **Charles Leclerc / Ferrari / the 2025 United States Grand Prix at Circuit of the Americas, Austin**. Historical pace and conditions provide the replay world. Your strategy controls Leclerc's tyres and repairs. **His historical pit calls never execute or appear as strategy instructions.**

## Run

With Python 3.12 or newer:

```sh
python3 -m pitwall.server --open
```

Open **http://127.0.0.1:8001**, or double-click `start.command` on macOS. Leave the server running. No dependencies or key are required for Local analysis. Gemini uses the existing private server credential when configured.

## Demo walkthrough

1. A new race opens at **lap 1 of 56**, ready but stationary. Click **Start Race** at the top. Prominent cards show Leclerc’s **simulated position**, who is ahead and by how much, **estimated pit-exit position**, and pace-based recovery opportunities. Leclerc drives automatically with the simulated tyres, damage and clean historical pace reference. Choose **1× actual simulation time, 2×, 3×**, or faster demo speeds through 80×; **Pause** holds it for Resume. The yellow Pause button remains visible. **Stop** ends the current run, cancels pending pit commands/assessments and blocks further actions. **Reset Race** stops the current run and opens a clean lap-1 run; the previous run stays saved.
2. All three analytical specialists refresh each lap. The three live status tiles update at each lap; their detailed reports are in the **right-side Agent workspace**, selected with Rivals / Track / Leclerc tabs: rival positions, gaps, tyres, ages and recent pace; weather/temperature, race-control messages and car incidents; simulated Leclerc pace, tyre wear, cliff proximity and approved stops.
3. Open any tool name to inspect its returned information. The eleven scoped tool calls and their payloads are real checkpoint reads. Use the Rivals / Track & incidents / Leclerc buttons to choose a specialist. The always-visible tool-call feed shows all three agents’ actual read timestamps and opens the corresponding returned data. Recent lap refresh history shows when data was retrieved. Open tools stay open during refreshes.
4. Automatic running compares the evidence each lap and stays out without a confirmation when no material strategy change is justified. A VSC, tyre warning or saved note alone does not pause the race. A stop recommendation or a materially increased pit benefit produces a decision request. The Gemini Director combines all three reports and saved intent. Choose **Box now**, **Stay out**, or a delayed stop. If running was paused for this call, your decision resumes it.
5. **Go to lap 20** jumps directly to that lap, processes skipped-lap consequences, preserves memory and authorized stops, and refreshes all three agents plus a Director assessment there. Direct navigation can supersede unapproved advice; it cannot authorize a pit. This differs from **Advance to next decision**, which stops at the first important event.
6. Try an undercut at **lap 22**: the bundled historical gap/tyre snapshot produces a nearby-rival opportunity against Norris. Read the opportunity and estimates before authorizing a stop. It is a screening signal, not a guaranteed pass.
7. Click **Broken front wing** or **Puncture**. Each incident triggers a fresh assessment but no automatic repair. Stay out to retain damage, or authorize a stop. New tyres repair a puncture; wing replacement is a separate checked option adding an assumed five seconds.
8. Save an instruction in **Race memory**, jump again, and refresh. Your tyres, damage, approved stops, lap history and intent remain saved. **Export race** contains the full record, including agent reports and tool results.

A pending box recommendation displays a persistent **BOX, BOX** beacon (or **PIT WINDOW** for a delayed call), with a slow yellow pulse, a browser-tab alert and a **Review pit call** shortcut to the tyre/repair and authorization controls. It clears after approval, Stay out, Stop, Reset or a superseding assessment. The recommendation source is labelled, including local recovery. Reduced-motion preferences replace the pulse with a solid light.

A later new-scenario entry keeps Leclerc on his **starting tyre set with accumulated age**. It does not import the tyres from his later historical strategy. Going backwards opens a fresh isolated scenario.

## Agents, tools and autonomy

Agent and LLM scenario tests run entirely on the backend. There is no test panel in the race UI. Use the pre-deployment gate below; it writes machine-readable pass/fail reports and returns a nonzero exit code if any required check fails. Tests use temporary databases and never edit your saved race.

The three specialists are deterministic analytical workers with their own read-only tools. Only the Strategy Director uses an LLM. Local mode uses an explicitly labeled deterministic coordinator.

| Agent | Tools | Retrieved information |
| --- | --- | --- |
| Rival Analyst | `read_rival_positions`, `read_rival_pits`, `read_rival_pace`, `screen_undercut` | All 19 opponents, historical neighbors/gaps, tyres and age, completed pace, recent pit entries/exits, candidate undercuts. |
| Race Conditions & Incidents | `read_weather`, `read_race_control`, `inspect_car_incidents` | Weather, track temperature, evidence age, flags/SC/VSC, persistent car damage and modeled pace loss. |
| Leclerc Performance | `read_simulated_telemetry`, `estimate_tyre_degradation`, `read_stint_history`, `evaluate_pit_consequences` | Your simulated position, tyres, age, lap times, wear penalty, laps until cliff, pit-cost breakdown by flag, rejoin position/neighbor/gap and recovery targets, authorized pit queue and stint history. |
| Strategy Director | `get_race_snapshot`, `recall_memory`, the three `delegate_*` tools, `compare_strategies`, `propose_decision` | Combine current evidence and intent, compare/stress-test pit windows, explain a recommendation and request your decision. |

The Director must recall memory, consult all three specialists and compare strategies before a proposal is accepted. It has no execution, archive, shell, arbitrary HTTP or credential-reading tool. There is no fourth Situation Agent. The numerical sensitivity check is a tool.

The loop allows at most nine model rounds, fifteen Director tool calls and two invalid-call corrections. Unapproved recommendations never change the car. Only a queue marked `authorized_by: human` can execute a pit. A routine hold cannot cancel an approved stop. Puncture, tyre/weather mismatch and red-flag guards prevent unsafe automatic advice. A selected option at least four modeled seconds worse than the cheapest option requires human review; the numerical tool returns explicit approval requirements per option. Existing human-authorized pit coverage is checked separately from a new approval request.

The specialists run on each checkpoint without spending LLM quota on every quiet lap. Gemini is called for a strategy trigger, explicit assessment, incident or direct lap jump. Actual Director tool-call order and retrieved payloads are also available under the technical trace.

## Clock, navigation and recovery

The server owns the race clock, so browser polling does not advance or duplicate laps. The delay for a lap is based on the available pace estimate plus simulated tyre/damage effects, divided by playback speed. Until representative completed pace exists, the clock uses a labeled/modelled 100-second starting assumption. At 1×, a modeled second takes a wall-clock second; 2× and 3× divide that duration. A visible lap countdown/progress bar continues between lap boundaries. Pausing and changing speed preserve the current lap’s progress. Lap durations are modeled, not a claim of measured live telemetry.

Every transition persists the car state, version and retry receipt. Start/Pause is durable. Browser refresh resumes the saved view; it does not reset the race. A server restart preserves the checkpoint and marks a previously running clock interrupted, requiring explicit Resume. Incomplete analyses are marked interrupted for retry. Failed analyses leave the car safe and stop the clock. A result based on an older version is discarded.

Automatic running pauses for important unacknowledged events. An unchanged or acknowledged condition does not trigger another review after a fixed lap count. A materially increased modeled pit benefit (10-second bands) can trigger a new call. Routine hold recommendations clear the assessment pause and resume a previously running race. Direct Go to Lap reaches the selected destination and refreshes the final checkpoint; skipped urgent decisions are not silently approved. A red-flag boundary blocks advancement rather than inventing a restart.

An undercut candidate requires a green/dry track, an aged Leclerc tyre set and an observed rival ahead within five seconds on an aged set. The tool displays the historical gap and assumed fresh-tyre advantage. It does not know future rival choices or guarantee position gain.

## Data and provenance

Source: [OpenF1](https://openf1.org/docs/), **race session 9888, meeting 1271, October 19, 2025**. Historical observations are bundled; the app does not use a paid live feed.

| File | Content |
| --- | --- |
| `data/austin-2025-demo.json` | 1,528 time-gated controlled-driver/world events across the 56-lap race. |
| `data/austin-2025-rivals.json` | 19 opponent identities and 22,427 time-gated rival events. |
| `data/austin-2025-source.json` | Source queries, counts, SHA-256 fingerprints and normalization description. |

Leclerc's historical pit visits and later stint-change events are removed from the web bundle. Only his starting tyre set is retained. Pit-affected/transition laps are excluded from pace estimates. Other completed laps have an illustrative historical tyre-wear estimate subtracted to produce a wear-neutral reference; your simulated tyre penalty is then applied independently. Original historical boundary timestamps schedule the replay world. They do not execute a stop.

Timing model v5 resolves each crossed lap against that same completed lap's neutral pace, then carries the difference between simulated elapsed time and the reference clock. It does not integrate mismatched rolling pace averages into the race gap. The reference clock's original pit-affected durations are replaced by clean pace estimates; only your authorized stop contributes simulated pit loss. Forecasts still use only previously completed clean laps (up to three), never the upcoming lap. Checkpoints reconcile millisecond timestamp differences so the preceding completion is visible. A fresh later entry reconstructs the same unpit timing state as advancing from lap 1, with fresh intent/memory.

Real-data regression: before any strategy or incident diverges, laps 2–22 reproduce the recorded Leclerc gap and position. At the start of lap 15 this is P2, 5.054 seconds behind the historical leader; the old model incorrectly accumulated a 16.067-second gap and P7. With the current illustrative coefficients, a human-approved medium-tyre stop on lap 15 produces P11 at lap 16 rather than the old model's P19. Subsequent results are counterfactual estimates, not validated race predictions.

Completed laps are available only after their finish. Opponent stint ends and future stops/weather remain hidden. Opponent pit entry is visible at its recorded time; completed visits wait for their duration. Historical rival positions, last completed laps, tyres and source gaps are preserved. The simulator carries Leclerc’s leader-relative time gap forward from the scenario entry, adding modeled pace differences and authorized pit/repair losses. Sorting that carried gap among current rival gaps produces a **simulated classification**. Later historical Leclerc pit calls never reset it. Before timing exists, grid gaps use a disclosed 0.3-second headway assumption. Stale stopped-car timing is marked estimated and drifts behind instead of freezing near the front.

To regenerate the Austin bundles:

```sh
python3 -m pitwall.import_race
```

Downloads are cached under ignored `data/raw/austin-2025/`. The importer validates session 9888 and writes complete bundles atomically. Dataset fingerprints are pinned to runs. Spa bundles remain as regression fixtures and earlier history; they are not used by the current web app.

## Simulation assumptions

This is an educational single-driver model, not calibrated race physics. Rivals retain historical pace and stops without reacting to your strategy. Pit exit and catch opportunities are numerical estimates; finishing position and successful passes are not guaranteed. The Director receives these computed values and must not invent them.

- At most eight forecast laps, assuming current conditions persist; no future observations enter a recommendation.
- Assumed net pit transit: **20.5 seconds**, reduced to 65% under VSC or 50% under SC. Tyre service is **2.5 seconds**, with **2 seconds** traffic allowance. Stationary service is never scaled down by SC/VSC.
- Compound-specific wear, tyre-age cliffs, fresh-tyre offsets and wet/dry penalties are inspectable in `simulation.py`.
- Assumed broken-wing loss: 4 seconds/lap; puncture loss: 25 seconds/lap. Losses add. Wing service takes **7.5 seconds**, parallel with the 2.5-second tyre service, so replacement adds 5 seconds. A puncture adds **1.5 seconds** for checks. Green tyre-only loss is 25 seconds; VSC is about 17.83 seconds and SC 14.75 seconds. These are inspectable demo assumptions, not circuit calibration.
- Changing the selected tyre compound or wing-repair checkbox requests a read-only preview and recalculates pit exit, costs, fresh pace and recovery. Ask the Director to assess that selection; the human-approved transition uses the same cost model.
- Sensitivity uses 0.65× / 1.40× wear and ±3 seconds pit cost. Ranges are assumptions, not confidence intervals.
- Relative cumulative time still compares against holding entry tyres on an undamaged car. Simulated position separately uses the carried race-gap state. Recovery integrates current observed rival pace and modeled wear for at most eight laps, adds one second per prospective pass and stops at an uncatchable intervening car. No passing is forecast under SC/VSC or red. Traffic, defending, future pit stops and weather can invalidate catch estimates.
- The condition clock uses historical lap boundaries; it does not recalculate event timing, traffic, fuel, detailed aerodynamics, tyre inventory or sporting regulations after a changed strategy.

## Gemini and privacy

The default Director is `gemini-3.5-flash-lite` through the native generateContent API. Existing private credentials continue to work. On a new installation, run `Configure Gemini.command`, paste a key at its hidden terminal prompt and restart. Use an AI Studio Free tier project if you want to avoid enabling billing; quotas and eligibility apply.

Server settings: `GEMINI_API_KEY` or `GOOGLE_API_KEY`, optional `GEMINI_MODEL`, and `PITWALL_LLM_PROVIDER=gemini`. Environment files are not automatically loaded. The private file `.local/gemini.json` is ignored by Git and Docker; credentials never reach browser payloads, tool traces or exports.

Provider failure produces labeled local recovery with human review. Quota failures stop immediately; transient failures get at most one retry. There is no automatic paid-provider fallback. The retained OpenAI adapter is explicit opt-in only.

Austin runs are saved in `.local/austin-web.sqlite3`; earlier Spa records remain in `.local/web.sqlite3`. A persistent HttpOnly/SameSite cookie separates browser sessions. Use the same browser to resume and export before clearing cookies. Exported JSON is a record, not an import feature. The server binds to localhost and serves only explicit assets; archives and credentials cannot be downloaded through static routes.

## Validation and files

```sh
python3 -m pitwall.predeploy          # unit tests + full backend local matrix
python3 -m pitwall.predeploy --live   # also run all 14 scenarios against Gemini
python3 tests/http_smoke.py          # HTTP checks, with the server running
```

The local matrix covers 14 scenarios at laps 1, 8, 15, 25, 40 and 56 (84 evaluations): current conditions, fresh tyres, worn tyres, puncture, broken wing, rain on slicks, SC, VSC, missing weather, red flag, combined puncture/wing damage, undercut opportunity, saved intent and an already-authorized pit. All three specialist reports and the Director decision are checked. Injected inputs are labelled test scenarios, rather than stored answers.

The live matrix runs the same 14 cases at lap 15 with real Gemini, spacing assessments to respect free-tier limits. It scores the original model proposal separately from the application decision. A corrected bad answer still fails the original-proposal check. Provider recovery fails the live-LLM check; a provider error stops further requests and marks remaining cases not run. Reports retain inputs, returned tools, prompt and dataset fingerprints, original/final proposals and expected/actual checks. Passing these contracts does not certify physics or optimal strategy, or independently grade every claim in a rationale.

```sh
python3 -m pitwall.validate --mode llm --resume  # after cooldown, retry failed/unrun cases
```

Resume requires the same prompt, provider/model, simulation version, dataset fingerprints, cases and checkpoints. It retains previous failed attempts in `attempt_history` and skips already-passing cases. Reports are saved under ignored `.local/validation/`: `predeploy-summary.json`, `local-scenarios.json`, and `llm-scenarios.json`. No paid-provider fallback is used. Earlier red-flag/rate-limit and weather/tradeoff results remain in `llm-before-red-guard.json` and `llm-before-weather-tradeoff-checks.json`. Later failed attempts are retained in the final report’s `attempt_history`.

Backend unit coverage includes replay gating, human authorization, persistent damage, exact Austin jumps, all 56 boundaries, timing calibration, memory, retries, restart, stale results, failure recovery, credential privacy, all scenario/agent combinations, source/proposal separation, report resume behavior and Stop cancellation of queued/in-flight assessments. HTTP checks include session isolation, read-only reports/exports and terminal Stop/fresh Reset behavior.

`tests/browser_smoke.mjs` validates the actual product controls and visual layout, rather than providing a user-facing testing feature. It checks 1×/2×/3× timing, high-contrast visible Pause, Stop, Reset, saved history, fresh state, lap-15 timing, pit approval/repairs, agents/tool data, exports, SC/VSC behavior and widths 320–1500px. It requires installed Playwright and Chrome; set `PLAYWRIGHT_MODULE` if needed. Only explicit human authorization can execute a stop.

The UI uses a Ferrari-inspired scarlet hero, carbon-black panels, a yellow SF monogram, tricolore detail, number-16 driver identity and telemetry-style data. It remains an independent simulation.

Implementation: `import_race.py` (clean Austin bundles), `rivals.py` (opponent feed), `replay.py` (time-gated memory foundation), `simulation.py` (car, wear, escalation policy), `race_model.py` (classification, pit costs, rejoin and sequential recovery), `specialists.py` (scoped tools), `agents.py` (Director instructions and validation), `gemini.py` (provider), `service.py` (clock/transactions/jobs), `server.py` (HTTP), `web/` (interface).

The Dockerfile bundles Austin data and listens on `PORT` with `/health`. Mount durable storage at `/app/.local`. Use one server process/instance for this SQLite/clock implementation. This F1 project is running locally; no cloud deployment or GitHub push is included. The older Common Health application is unchanged.

The browser acceptance walkthrough also checks 1×/2×/3× timing, mid-lap speed changes, all 19 rival rows, agent tabs and timestamped reads, live repair previews, puncture checks, pit approval, automatic stay-out under SC for successive laps, distinct VSC/SC costs and disabled passing under neutralization. Branch, job status and memory are read in one SQLite snapshot to prevent a completed job appearing with an older car/assessment view. Earlier v3/v4 runs remain readable as archived history; start a new race for the corrected v5 timing model. Their already-accumulated timing errors are not silently rewritten.
