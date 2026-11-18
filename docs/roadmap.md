# Capacity — Roadmap

The ordered list of upcoming work. Each milestone ends in a tagged release; there is no `TODO.md`. Hard invariants live in [`CRITICAL.md`](../CRITICAL.md), architectural decisions in [`DECISIONS.md`](../DECISIONS.md), visual rules in [`.stitch/DESIGN.md`](../.stitch/DESIGN.md), and implemented release history in [`archive/roadmap-history.md`](archive/roadmap-history.md) (move completed milestones or `- [x]` items with `proj-test roadmap-archive capacity --done`).

## Where we are

`v0.6.1` is tagged on `main` and its gate (`./scripts/gate.sh`) is green (Milestones 1–4 / `P0`–`P3` plus all 20 `Review 2026-11-18` fixes complete). Every module lives in a job-scoped folder (`contract/`, `engine/`, `ingest/`, `telemetry/`, `gateway/`, `showcase/`, `cli/`, `api.py`) guarded by one rule per folder, and the GM3 Light workbench renders solely from a typed `PageView` with single-point escaping, real `.html`/`.css`/`.js`/`.svg` package assets, disabled controls on offline snapshots, and live debounced `AbortController` FastAPI calls on `capacity serve`.

Decisions taken by the owner on 2026-11-17:

1. An empty API request no longer returns the sample answer; CLI `optimize`, `schedule` and `chargeback` require an explicit `--demo` switch or input file (shipped in `v0.4.0` / `P1.9`).
2. The offline HTML file is a snapshot. Its sliders need not work (shipped in `v0.6.0` / `P3.2`).
3. `recommend --html-out` shows no sample data (shipped in `v0.4.0` / `P1.1`).
4. Import paths may change (shipped in `v0.5.0` / `P2.1`).
5. The file hash on the benchmark is a release-time check (shipped in `v0.3.1` / `P0.1`).
6. Self-hosted sample card: no preference was given, so the request default applies, 6 replicas and 48 GPUs (shipped in `v0.4.0` / `P1.4`).

## Milestones at a glance

| # | Milestone | Status |
| :--- | :--- | :--- |
| 1–5 | `v0.3.1` (`P0`), `v0.4.0` (`P1`), `v0.5.0` (`P2`), `v0.6.0` (`P3`), `v0.6.1` (`P0`–`P2` review fixes) | Shipped — see [`## Shipped`](#shipped) and [`archive/roadmap-history.md`](archive/roadmap-history.md) |
| 6 | `v0.7.0` — Cloud Console Capacity, Reallocation & Hourly Spillover Workbench | Scheduled (`6` open `- [ ]` tasks) |
| 7 | `v0.7.1` — Cross-Repo `bq-eval` Burndown, OTel Span & Hourly `FOCUS 1.2` Wire Alignment | Scheduled (`1` open `- [ ]` task) |
| Next | `v0.8.0+` — Promote selected items from [`## Unscheduled backlog`](#unscheduled-backlog) | Unscheduled |

Each scheduled milestone ends in a minor tagged release.

## Milestone 6 — `v0.7.0`: Cloud Console Capacity, Reallocation & Hourly Spillover Workbench

Port the finalized Cloud Console visual design, per-model `P50`/`P75`/`P90`/`P95` reallocation workflow, and Tab 2 hourly 1-minute spillover line graph cleanly into `capacity` while preserving `ADR-004` (all capacities, percentiles, and reallocation math live in `capacity/engine/` + FastAPI; browser JS renders typed engine outputs with zero embedded formulas).

- [ ] **Cloud Console theme & top app bar (`capacity/showcase/assets/workbench.{html,css}`, `scenario.py`, `view.py`):** Adopt Cloud Console GM3 Light palette (`#f8f9fa` canvas, `#ffffff` `4px`/`6px` hairline cards, `#1a73e8`/`#3367d6` Cloud Blue, flat `2px` underline tabs, dense `#f1f3f4` data tables, Cloud header bar with scope chip), plus `ScenarioPresentation.hidden_tabs` and card-trimming flags for focused executive views.
- [ ] **KPI card clarity (`capacity/contract/scenario.py`, `capacity/showcase/tabs.py`):** Rename *Active vs 24x7 Duty Cycle* to *Active Window Share (Business Hours)*, add optional `total_requests` to `ModelInventoryAnnotation` so *Throttled Calls (% of Traffic)* reports exact `429` percentage alongside spillover share, and make the renewal-cliff KPI opt-in.
- [ ] **Full-width *Contracted PT vs. Active Target Percentile* chart with model filter (`capacity/showcase/reallocation_cards.py`, `renderer.py`, `workbench.js`):** Render full-width grouped bars driven by a `Target P` (`P50`/`P75`/`P90`/`P95`) dropdown and model filter pills (auto-excluding high-scale outliers by default so peer models remain readable, rescaling dynamically to visible models).
- [ ] **Full-width 1P & 3P per-model reallocation planners (`capacity/engine/reallocation.py`, `api.py`, `reallocation_cards.py`):** Separate 1P Gemini and 3P Claude reallocation tables with explicit formula (`Target GSUs = Contracted + From Existing (Realloc Δ) + Buy New GSUs`), per-row + bulk `Target P` selectors backed by `POST /api/v1/reallocate` on `capacity serve` (and pre-computed snapshots on `--html-out`), full-width *Recommended 1P GSU Reallocations* directly below 1P, and editable 3P what-if inputs when 3P Cloud Monitoring telemetry is not yet attached.
- [ ] **Tab 2 hourly 1-minute PT spillover SVG line graph (`capacity/engine/reallocation.py`, `tabs.py`, `renderer.py`, `workbench.js`):** Compute per-model, per-hour 1-minute GSU arrays and per-local-day active `P50`/`P75`/`P90`/`P95` reference lines (`timezone_offset_hours` configurable, e.g. `EST` primary + `UTC` secondary), rendered as an interactive SVG line chart with model/hour selectors, `Worst First` / `Chronological` ordering, shaded red spillover area above contracted PT, toggleable daily `P` lines, and a minute-snapping mouseover crosshair + tooltip.
- [ ] **Tests & `v0.7.0` release gate (`tests/test_reallocation.py`, `tests/test_portal_export.py`):** Verify `POST /api/v1/reallocate`, `hidden_tabs`, hourly spillover series, and Cloud Console HTML escaping invariants under `./scripts/gate.sh` (`proj-test capacity`), then tag `v0.7.0`.

## Milestone 7 — `v0.7.1`: Cross-Repo `bq-eval` Burndown, OTel Span & Hourly `FOCUS 1.2` Wire Alignment

- [ ] **Align `capacity/ingest/request_logs.py` & `FOCUS 1.2` contract fixtures with `bq-eval` (`BQ-G1`):** Accept `usage_hour` timestamps alongside `timestamp` in [`capacity/ingest/request_logs.py`](../capacity/ingest/request_logs.py) (`_record_time_utc`), preserve `model_name`, `apphub_application_id` (`tenant`), `cached_input_tokens`, `thinking_tokens`, and pre-weighted `weighted_burndown_tokens` exported by `v_capacity_hourly_burndown_export` without re-weighting hourly sums across the `>200K` cliff, parse string `status_code` values (`"OK"` / `"ERROR"`) on exported OpenTelemetry span JSONL alongside integer HTTP codes, and add a round-trip contract test verifying hourly `FOCUS 1.2` Two-Pool Chargeback parity against [`capacity/engine/chargeback.py`](../capacity/engine/chargeback.py) (`done when`: `./scripts/gate.sh` passes round-trip `v_capacity_hourly_burndown_export` and OTel JSONL fixtures with `0` timestamp/status errors, then tag `v0.7.1`).

## Unscheduled backlog

Items below have no target release and use plain bullets rather than `- [ ]` checkboxes so `work_sync.py` counts only scheduled milestone tasks (currently `0`). Promote an item into a milestone (`- [ ]`) when scheduling it.

### Next horizons (`v0.7.0+` feature extensions)

- **Zero-Privilege Hosted Web Mode (`AUTH_MODE=user` Bring-Your-Own-Bearer-Token on Cloud Run):** Run `capacity serve` on Cloud Run under a zero-IAM service account (`AUTH_MODE=user`, server-side ADC disabled) where visitors browse offline scenarios or pass a short-lived `Authorization: Bearer` token so live Cloud Monitoring queries execute strictly under the caller's own GCP identity and quota.
- **Read-Only GCE GPU/TPU Supply Advisory (`--mode self-hosted` Placement Bridge):** Pair self-hosted replica sizing ([`capacity/engine/self_hosted.py`](../capacity/engine/self_hosted.py)) with read-only GCE Capacity Advisory REST checks (`regions/{r}/advice/calendarMode` with `100%/75%/50%/25%` split planning plus alpha `regions/{r}/advice/capacity` for `SPOT` obtainability/uptime and `FLEX_START` estimated queue wait, omitting `guestAccelerators` on built-in GPU families `G2/G4/A2/A3/A4/A4X` and respecting `87h`/`720h` Calendar lead times).
- **Sub-Second `10s`-Bucket Occupancy Guard in `optimize_hybrid_commitment` ([`capacity/engine/commitment.py`](../capacity/engine/commitment.py)):** Surfaced by the consolidated 7-day Azure trace backtest ([`benchmarks/results/l3_receipt.json`](../benchmarks/results/l3_receipt.json)): when highly bursty `10s` coding arrivals plus light weekend traffic hold sub-second 168h bucket occupancy (`37.02%`) below the `42.55%` PT-vs-Priority break-even threshold, coupling `optimize_hybrid_commitment` directly to empirical `10s` bucket fill factors can close the remaining `3.34%` gap to the `L2` hindsight bound while preserving an optional explicit SLA floor.
- **ADK Prefix-Cache Reference Patterns ([`capacity/engine/burst.py`](../capacity/engine/burst.py)):** Capture concrete ADK runtime patterns (`instruction=""` + fixed `static_instruction` + `ContextCacheConfig(min_tokens=4096)`, trailing `<system-reminder>` callbacks, transient user-turn memory injection, and reclaim-thresholded tool output pruning to prevent Block 0 cache thrash).
- **Per-Turn Model Routing & Multi-Backend Proxy Recipes ([`capacity/gateway/bundle.py`](../capacity/gateway/bundle.py)):** Model per-turn tier switching (`gemini-3.5-flash-lite` -> `gemini-3.8-flash` -> `gemini-4-argon`) alongside gateway session affinity and `thought_signature` preservation checks.
- **Multi-Node Cloud Redis & Envoy Global Rate-Limit Service Integration Suite:** Extend [`benchmarks/live_sse_harness.py`](../benchmarks/live_sse_harness.py) with an optional Docker Compose Envoy AI Gateway + `envoyproxy/ratelimit` + Valkey cluster testbench for `>2,000 RPS` load testing.
- **RDMA / NIXL KV-Cache Transfer Latency Modeling ([`capacity/engine/self_hosted.py`](../capacity/engine/self_hosted.py)):** Incorporate inter-node KV-block transfer bandwidth overhead into disaggregated Prefill/Decode pool sizing for `>128K` context prompts.
- **Stateless MCP `2026-07-28` Adapter (`SEP-2640` Skills + `ui://` MCP Apps):** Expose the post-`P1.9` typed operations over stateless Streamable HTTP MCP, bundle [`skills/capacity/SKILL.md`](../skills/capacity/SKILL.md) via `io.modelcontextprotocol/skills` (`SEP-2640`) for hosted AI clients, and serve self-contained HTML scorecards (`showcase/renderer.py --html-out`) as `ui://` MCP Apps.

### Showcase presentation gates

- **Hero SVG word budget (`docs/assets/architecture_card.svg`):** Trim visible text in [`docs/assets/architecture_card.svg`](assets/architecture_card.svg) to `<= 35–45` words so first-screen skimmers read the closed-loop diagram in 5 seconds.
- **README body budget & "Why not" section (`README.md`):** Tighten [`README.md`](../README.md) prose to `<= 800` words with an explicit `"Why not"` comparison section while keeping verbatim CLI/YAML and receipt parity tests green.

## Shipped

- **v0.2.0-v0.3.0 (Passes A-G + 3P Claude):** Phase 1 (v0.2.0) and Phase 2 (v0.3.0) dual-mode GSU/vLLM sizing, 1P Gemini + 3P Claude burndown and prefix-cache physics, LiteLLM/Envoy/GKE gateway generator, disaggregated prefill/decode sizer, 3-Tier EDF scheduler, FOCUS 1.2 chargeback, --scenario-file and capacity collect, GM3 Light workbench, and L3 Azure/SSE benchmark — [archive](archive/roadmap-history.md#v020-v030-passes-a-g-3p-claude)
- **v0.3.1 (Milestone 1 - P0):** A comment edit in an engine file keeps the suite green, a module moved into a sub-folder is still scanned, and one script defines the gate — [archive](archive/roadmap-history.md#v031-milestone-1---p0)
- **v0.4.0 (Milestone 2 - P1):** Unified model catalog, single request-to-result path across CLI/API/workbench, typed `ShowcaseReport` and `SeriesKey`, lazy Cloud Monitoring SDK and `.env` loading, and complexity lint ratchet — [archive](archive/roadmap-history.md#v040-milestone-2---p1-tidy-in-place-then-one-path-from-request-to-result)
- **Milestone 3 — `v0.5.0`: Folders by job (`P2`):** The guard states one rule per folder and the gate is green — [archive](archive/roadmap-history.md#milestone-3---v050-folders-by-job-p2)
- **v0.6.0 (Milestone 4 - P3):** Display-ready `PageView` escaped in one place, zero formulas or thresholds in browser JavaScript (disabled snapshot `--html-out`, live API controls + `POST /api/v1/burst/window` on `serve`), real `.html`/`.css`/`.js`/`.svg` package assets, and hostile-text + section-combination value tests — [archive](archive/roadmap-history.md#milestone-4---v060-the-page-shows-only-what-the-engine-computed-p3)
- **v0.6.1 (Review 2026-11-18 - P0-P2):** Untimestamped & unequal-interval Prometheus ingestion, unfiltered 3P Claude Monitoring, zero-filled multi-day JSONL & single-model filtering, scalar chargeback broadcast, strict GSU_CAPACITY_* & Claude family overrides, shared DualCache Redis callback + failure refunds, cached scenario error handling, debounced AbortController workbench error states, uv 0.12.24 alignment, and GoogleAuthError 502 mapping — [archive](archive/roadmap-history.md#v061-review-2026-11-18---p0-p2)

