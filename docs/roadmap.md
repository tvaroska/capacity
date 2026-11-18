# Capacity — Roadmap

The ordered list of upcoming work. Each milestone ends in a tagged release; there is no `TODO.md`. Hard invariants live in [`CRITICAL.md`](../CRITICAL.md), architectural decisions in [`DECISIONS.md`](../DECISIONS.md), visual rules in [`.stitch/DESIGN.md`](../.stitch/DESIGN.md), and implemented release history in [`archive/roadmap-history.md`](archive/roadmap-history.md) (move completed milestones or `- [x]` items with `proj-test roadmap-archive capacity --done`).

## Where we are

`v0.6.0` is tagged on `main` and its gate (`./scripts/gate.sh`) is green (Milestones 1–4 / `P0`–`P3` complete). Every module lives in a job-scoped folder (`contract/`, `engine/`, `ingest/`, `telemetry/`, `gateway/`, `showcase/`, `cli/`, `api.py`) guarded by one rule per folder, and the GM3 Light workbench renders solely from a typed `PageView` with single-point escaping, real `.html`/`.css`/`.js`/`.svg` package assets, disabled controls on offline snapshots, and live FastAPI calls on `capacity serve`.

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
| 1–4 | `v0.3.1` (`P0`), `v0.4.0` (`P1`), `v0.5.0` (`P2`), `v0.6.0` (`P3`) | Shipped — see [`## Shipped`](#shipped) and [`archive/roadmap-history.md`](archive/roadmap-history.md) |
| Next | `v0.6.1` — Fix the `P0` items in [`## Review 2026-11-18`](#review-2026-11-18-next-steps-p0p4) (wrong numbers from ingest, chargeback and the gateway callback) | Proposed, not yet scheduled |
| Later | `v0.7.0` — Promote selected items from [`## Unscheduled backlog`](#unscheduled-backlog) | Unscheduled (`0` open `- [ ]` tasks) |

Each scheduled milestone ends in a minor tagged release.

## Review 2026-11-18: next steps (`P0`–`P4`)

Daily architecture, code and test review at `v0.6.0` (`e95f059`). The gate was green before the review (286/286 in 24 s, no test slower than 2.9 s). Every `P0` was reproduced with a short script. Nothing below repeats a `U` item. Where an item adds to one it says "extends U#". All items are **pre-existing** (code already present in `v0.4.0` and only moved in `v0.5.0`) except R13 and R14, which came with `v0.6.0`. Items are plain bullets like the rest of the backlog. Promote them to `- [ ]` when scheduling them. Write the failing test first for each one.

Mutation spot-check (9 one-line mutants in a scratch copy): 7 were caught, and 2 survived (R15, R16). The audit's `SIBLING_SYS_PATH_TRAVERSAL` hit on `tests/test_isolation_guard.py:337` is a false positive: the line finds the ast-grep binary next to the interpreter and does not change `sys.path`.

### `P0`: wrong numbers or silent fallbacks

- **R1 Prometheus: scrapes without timestamps are added together.** `capacity/ingest/prometheus.py:62-80`, `:107-121`. Untimestamped samples all land in one `current_scrape` dict. Two appended `/metrics` dumps (1,000,000 then 1,060,000) give 2,060,000 tokens where the true delta is 60,000 (34× too high). The comment at `:19` promises one-minute spacing that never happens.
  - Next: start a new scrape when a `(metric, labels)` key repeats, or reject untimestamped multi-scrape input with one error line.
  - Verify: new `test_untimestamped_concatenated_scrapes_are_differenced` in `tests/test_critical_invariants.py` expects a prompt delta of 60,000 and one interval.
- **R2 Prometheus: the tenant TPM is an unweighted mean of interval rates.** `capacity/ingest/prometheus.py:166` (`sum(interval_tpms)/len(interval_tpms)`), `:170`, `:179`. Downstream, `cli/optimize.py:132` and `engine/commitment.py:90` read each point as one minute. Scrapes at 0 s, 60 s and 3,600 s with 600k tokens in the first minute give a tenant TPM of 300,000. The true hourly mean is 10,000, so chargeback is 30× too high.
  - Next: weight by interval length (`Σ tokens / Σ minutes`) and expand `tpm_series` to per-minute points, or return interval lengths.
  - Verify: test with a 1-minute plus 59-minute layout asserting `hourly_consumed_tpm_by_tenant["default"] == [10000.0]`.
- **R3 Cloud Monitoring without `--model` never queries 3P Claude `token_count`.** `capacity/telemetry/cloud_monitoring.py:259-262`. `has_partner_filter` is False when `models` is None, so all Claude PT usage is missing from an unfiltered `recommend --project X`. This breaks `ADR-002`. The existing test at `tests/test_claude_3p.py:213-229` only covers explicit `models=[...]`.
  - Next: `query_partner = models_filter is None or has_partner_filter`, and keep the per-model skip at `:297`.
  - Verify: extend `test_fetch_throughput_partner_token_count_weighting_and_mixed_models` with a `fetch_throughput("demo-proj", days=1)` call and assert both the Gemini and the `claude-sonnet-4-6` `SeriesKey` are present.
- **R4 JSONL logs: `tpm_series` drops idle minutes, so `recommend` puts new dates on them and `optimize` treats them as 24x7.** `capacity/ingest/request_logs.py:445-446`. Callers: `capacity/cli/recommend.py:118-121` lays the sparse minutes onto consecutive minutes from the trace epoch, so 3 days of traffic become 1 "day" with one daily peak for the Holt forecast. `capacity/cli/optimize.py:132,175` uses `active_duty_cycle_pct=100`, against `ADR-001` and `ADR-006`.
  - Next: return a zero-filled minute series from the first record to the last (as `_minute_series_by_key` already does) or a sparse `dict[datetime, float]`. Feed real timestamps to `LocalTraceAdapter`. Derive the duty cycle in `optimize --vllm-file`.
  - Verify: test that `recommend --vllm-file` on a 3-day log reports 3 daily peaks (`total_minutes ≥ 2,885`).
- **R5 Chargeback: a scalar tenant beside hourly lists counts as one hour, then zeros.** `capacity/contract/requests.py:116` accepts `HourlyTpmSeries | BoundedTpmFloat`. `capacity/engine/commitment.py:33-41` (`pad_series_matrix`) turns a scalar into `[x]` and zero-pads it. `capacity/engine/chargeback.py:43`. Two tenants at 1M TPM split 4% / 96% instead of 50% / 50%. Lists of different lengths are also aligned at hour 0 without a message (`engine/pooling.py:29`).
  - Next: broadcast a scalar as a constant (or drop the scalar arm; touches U11), and reject lists of different lengths with one `ValueError`.
  - Verify: new `test_chargeback_scalar_tenant_is_constant` expects 50/50.
- **R6 `GSU_CAPACITY_*` overrides: a malformed value is ignored without a message, and `0` crashes later.** `capacity/engine/catalog.py:469-472` (`except ValueError: continue`), `:437-454`. `GSU_CAPACITY_GEMINI_3_8_FLASH=3,000` silently keeps 120,900. `=0` is accepted and raises `ZeroDivisionError` at `engine/commitment.py:81` (same division at `chargeback.py:59` and `burst.py:132`). The scenario path already validates `ge=1` (`contract/scenario.py:96`).
  - Next: raise `ValueError("<key>=<val>: expected a positive integer TPM per GSU")` when parsing fails or the value is ≤ 0.
  - Verify: `build_model_catalog(env={...: "3,000"})` and `{...: "0"}` both raise. Add both cases to `tests/test_critical_invariants.py`.
- **R7 The generated LiteLLM callback never writes to the shared cache.** `capacity/gateway/litellm_burndown_callback.py:136-142` checks `hasattr(cache, "async_increment")`. LiteLLM's `DualCache` method is `async_increment_cache`, so on a real proxy every increment goes to the per-process `_local_window` and nothing is logged. With the default 12 pods × 4 workers, a team can burn about 48× its funded share before it spills. The fake at `tests/conftest.py:120` copies the wrong name, so the suite stays green. `DECISIONS.md:70` documents the wrong name too.
  - Next: add a pinned `litellm` dev extra and assert the real method names first (the method name here comes from LiteLLM's docs, since litellm is not installed in `.venv`). Then call `async_increment_cache`, log once or fail closed when the shared cache is missing, rename the fake's method, and fix `ADR-007` / `DECISIONS.md:70`.
  - Verify: test with a cache exposing only `async_get_cache` / `async_increment_cache` and 2 callback instances, asserting 20 shared increments and an empty `_local_window`.
- **R8 The per-team funded ceiling reads `max_budget_tpm`, which LiteLLM's key object does not have.** `capacity/gateway/litellm_burndown_callback.py:155-160`. `getattr(..., None)` always falls back to `committed_tpm // business_unit_shares`, so a team with `tpm_limit=5000` never spills. The tests build `SimpleNamespace(max_budget_tpm=...)` themselves (`tests/test_claude_3p.py:303`, `tests/test_physics_oracles.py:232`), so they cannot catch it.
  - Next: read `team_tpm_limit`, then `tpm_limit` (or one documented team metadata key), log when the default is used, and drop `max_budget_tpm` from the tests.
  - Verify: a team with `tpm_limit=5000` spills at about request 5.

### `P1`: missing tests, weak guards, gate drift

- **R9 The generated LiteLLM config hardcodes per-token prices for every model.** `capacity/gateway/litellm.py:175-176`, `:193-194`. Every model gets the same PT and spillover prices, and the Claude "standard" spillover is priced at 1.8× even though `GATEWAY_HEADER_POLICIES` tier 3 says 1.0×. So spend logs and budgets on a deployed proxy are wrong.
  - Next: take prices from the request (`paygo_per_1m_burndown_tokens_usd`) or from a catalog field, with the spillover factor from `supports_priority_paygo`. Or omit the `*_cost_per_token` fields.
  - Verify: test that bundles for two models differ, and that the Claude spillover price equals the PT price × 1.0.
- **R10 Multi-model JSONL: `snapshot.model` is the last record's model, while `tpm_series` sums all models.** `capacity/ingest/request_logs.py:272`, `:419`. `cli/optimize.py:146` then sizes Gemini load against a Llama replica profile.
  - Next: for single-model commands, fail with one line naming the models unless `--model` picks one, then filter the series.
  - Verify: `optimize --mode self-hosted --vllm-file <mixed.jsonl>` exits non-zero and names both models.
- **R11 Prometheus: a counter first seen in a later scrape is counted in full.** `capacity/ingest/prometheus.py:43-45` (`curr - 0`). A 5M counter seen only in scrape 2 becomes one minute of 20M weighted TPM.
  - Next: a delta of 0 when the key is missing from the previous scrape.
  - Verify: new row in the counter-reset table at `tests/test_critical_invariants.py:421-428`.
- **R12 Minute series: zero-filling stops above the cell cap without a message, and the showcase keeps only the first region.** `capacity/ingest/request_logs.py:168-172`: 21 keys × 50,001 minutes give a series of length 2, which turns all-hours percentiles into active-only ones. `capacity/showcase/evaluate.py:347-349` keeps only the first region per model instead of summing them when `by_region=False`.
  - Next: raise `ValueError` above the cap (as `_hourly_series_by_tenant:143-152` does), and sum regions element-wise.
  - Verify: one test that expects a `ValueError` above the cap, and one that two regions are summed.
- **R13 A bad scenario file returns a plain 500 on every sizing route (regression in `v0.6.0`, extends U5).** `capacity/api.py:95-102` (`_active_scenario_catalog`, added in `e95f059`) re-reads `CAPACITY_SCENARIO_FILE` inside 9 routes. `{not json` gives a 500 with no JSON body on `/api/v1/optimize`, `/api/v1/gateway/config` and `/`.
  - Next: load and validate once at startup and keep the last good copy. Until then, map `ValidationError`/`ValueError`/`OSError` to a 503 JSON detail.
  - Verify: a TestClient test with a corrupted scenario file expects a 503 JSON response (or a 200 from the cached catalog).
- **R14 The workbench swallows API errors and leaves stale numbers on screen (`v0.6.0`).** `capacity/showcase/assets/workbench.js:18-31` (`postJson` returns `null` on non-2xx, and an empty input becomes `0`). Callers at `:44`, `:91`, `:117` and `:194` return early. A `fetch` rejection is unhandled.
  - Next: show a visible error state (text plus `badge-danger`) on a non-OK response or a network error, and treat `""` as invalid.
  - Verify: a TestClient test that the page includes the error-state element ids, plus a manual check under `capacity serve` (clear a field and the badge shows an error).
- **R15 Mutation survives: a flat (idle) counter is not tested.** Changing `diff < 0` to `diff <= 0` at `capacity/ingest/prometheus.py:45` keeps 286/286 green, yet makes an idle server report its whole cumulative counter (500,000 TPM instead of 0).
  - Next: add a `"flat counter is zero traffic"` row to the parametrize table at `tests/test_critical_invariants.py:411-438`.
  - Verify: re-apply the mutant in a scratch copy; that row must fail.
- **R16 Mutation survives: the NaN/Inf 422 test never checks the sanitized value.** `tests/test_refactor_cutpoints.py:314-320` checks only the status and that `detail` is a list. Changing `return repr(node)` to `return None` at `capacity/api.py:87` stays green.
  - Next: assert `detail[0]["input"] == repr(bad_num)` and `detail[0]["loc"] == ["body", "tpm_series", 0]`.
  - Verify: re-apply the mutant in a scratch copy; the test must fail.
- **R17 Cloud Build uses a uv three minor versions older than the one that wrote the lockfile.** `cloudbuild.yaml:14` and `Dockerfile:3` pin uv `0.9.1`. The local uv is `0.12.24` and `uv.lock:2` is `revision = 5`. No `[tool.uv] required-version` is set. Whether `0.9.1` accepts the lock under `lock --check` / `sync --frozen` is **unverified**.
  - Next: pin one uv version everywhere (`required-version` plus matching image tags) and note it in the gate gotchas in `AGENTS.md`.
  - Verify: `docker run --rm -v "$PWD":/w -w /w ghcr.io/astral-sh/uv:0.9.1-python3.13-bookworm-slim uv lock --check`.

### `P2`: boundaries and refactor-blocking coupling

- **R18 The engine reads `os.environ`, and both import guards allow stdlib I/O in `engine/`.** `capacity/engine/catalog.py:5,464`. Every `catalog or build_model_catalog()` fallback (`commitment.py:51`, `chargeback.py:31`, `scheduler.py:33,174`, `pooling.py:20`, `burst.py:76`, `sizing.py:53,72,85,247`) depends on the server's environment. `specs/ast.rules.yaml:43-75` and `tests/test_isolation_guard.py:37` (`sys.stdlib_module_names`) would also let `pathlib`, `socket` or `subprocess` through.
  - Next: read the environment in the adapters (`build_model_catalog(env=os.environ)` in `cli/` and `api.py`), default the engine to `env={}`, and deny stdlib I/O roots in the `engine`/`contract` rules.
  - Verify: a probe `import os` in `capacity/engine/` fails `tests/test_isolation_guard.py`.
- **R19 Unknown models report `recommended_gsus=0`.** `capacity/engine/sizing.py:97-100`, `:148`, `:176-180`. **Owner decision (2026-11-18): keep `0`.** There will be no `None` and no `UnknownModelError` on the `recommend` path. `showcase/evaluate.py:370` still fails closed for scenarios.
  - Next: state in `ADR-001` that `recommend` reports `0` GSUs with `gsu_capacity_tpm: null` for models outside the catalog, and add a test that pins this behaviour.
  - Verify: a sparse series keyed `unknown-model-x` gives `recommended_gsus == 0` and `gsu_capacity_tpm is None`.
- **R20 Capacity overrides for models outside the catalog silently get Gemini 1P rules.** `capacity/engine/catalog.py:449-454`. `GSU_CAPACITY_CLAUDE_HAIKU_4_5=50000` gives the `gemini_1p` family: 4.0× output instead of 5.0×, the wrong buckets, and Priority spillover that Claude lacks (`ADR-007`).
  - Next: infer the family from the publisher prefix, or require the override to name it.
  - Verify: `build_model_catalog(env={"GSU_CAPACITY_CLAUDE_HAIKU_4_5": "50000"}).require("claude-haiku-4-5").family_spec.family == "claude_3p"`.
- **R21 Workbench input race.** `capacity/showcase/assets/workbench.js:33-231`. Each `oninput` fires an unsequenced `fetch`, so an older response can overwrite a newer one.
  - Next: an `AbortController` per handler (or a request id), plus a debounce of about 150 ms.
  - Verify: `grep -n AbortController capacity/showcase/assets/workbench.js`.
- **R22 Badge and banner rules exist in both Python and JS and already differ (extends U3).** `tabs.py:111-120` ↔ `workbench.js:49-57`, `tabs.py:261-311` ↔ `:126-175`, `tabs.py:319-330` ↔ `:99-108`, `governance_tabs.py:173-184` ↔ `:209-230`. With no bottleneck warnings, Python shows the status text (`governance_tabs.py:184`), while JS keeps the old banner (`workbench.js:228-230`).
  - Next: return `BadgeView`/`BannerView` tone and text from the API, so JS only assigns `className`/`textContent`.
  - Verify: `grep -c "badge badge-" capacity/showcase/assets/workbench.js` returns 0.
- **R23 Callback state: the local window grows without limit, failures are never refunded, and spill decisions read a stale value.** `capacity/gateway/litellm_burndown_callback.py:120-142` (one key per team per minute, never removed), `:162-200` (no `async_log_failure_event`, so each failed call keeps its 1,024-token reservation; the spill check uses the value read before this request's own increment).
  - Next: fold into R7. Drop keys older than the current minute minus 2, refund `-reserved` on failure, and decide on the total that `_increment_counter` returns.
  - Verify: `time_fn` over 10 minutes leaves `len(_local_window) <= 3`, and 10 concurrent pre-calls at a 5×1,024 ceiling spill from the 5th call.
- **R24 Tests patch private module globals.** `tests/test_critical_invariants.py:289,312` (`scheduler._EXACT_SEARCH_NODE_BUDGET`), `tests/test_ingestion.py:174` (`cloud_monitoring.datetime`), `tests/test_l3_receipt.py:65,90,111-112` (`catalog.GEMINI_1P_FLASH_SPEC`, benchmark functions). A rename or a `from datetime import` turns these into silent no-ops.
  - Next: make the search budget a keyword argument, inject `now: Callable[[], datetime]` into the Cloud Monitoring adapter, and pass `catalog=` into `run_track_a_backtest`.
  - Verify: `rg -n 'monkeypatch.setattr\(\w+, "_' tests/` returns nothing.

### `P3`: cleanup and doc contracts

- **R25 Gaps in `specs/ast.rules.yaml`.** The header at `:2-11` says "one import rule per folder", but there is no `contract/` or `ingest/` rule. `exec`, `compile`, `getattr(importlib, "import_module")` and `importlib.__import__` all pass (`:39-41`, and `:201-214` bans only `eval`). The Google SDK regex at `:100-104` misses `from google import genai`. The Python guard covers most of these cases.
  - Next: add `contract`/`ingest` rules, extend `no-eval-builtin` to cover `exec`/`compile`, and add the dynamic-import patterns. Or reword the header to "a subset; the Python guard is authoritative".
  - Verify: add the bypasses as `invalid_examples`, then `uv run --no-sync pytest tests/test_repo_hygiene.py -k ast`.
- **R26 The engine silently fixes up bad input.** `capacity/contract/requests.py:86-90` has no `min_replica_count > max_replica_count` validator, and `self_hosted.py` raises max to min. `capacity/engine/kpis.py:39-41` caps `ecur_168h_pct` at 100, which hides over-reservation. `capacity/contract/common.py:11-14` keeps a hardcoded `"0.6.0"` fallback.
  - Next: add a `model_validator`, return uncapped ECUR (or an `over_reservation` flag), and keep one version source.
  - Verify: `uv run --no-sync pytest -k "self_hosted or kpi"` with the new cases.
- **R27 A placeholder project in the Cloud Monitoring adapter.** `capacity/telemetry/cloud_monitoring.py:208-209` turns an empty project list into `"default-project"`.
  - Next: raise `ValueError("at least one project")`.
  - Verify: `fetch_throughput([])` raises.
- **R28 `SKILL.md` commands fail as written.** `skills/capacity/SKILL.md:47,49` (`optimize`, `schedule`) lack `--demo` or an input file, which has been required since `v0.4.0`. `.agents/skills/capacity/SKILL.md` is a copy, not a symlink.
  - Next: add `--demo`, make the copy a symlink, and add both files to the documentation-contract command test.
  - Verify: every fenced `capacity ...` command in both `SKILL.md` files exits 0 under the doc-contract test.
- **R29 A README example prints `?` replicas.** `README.md:56` runs `recommend --mode self-hosted` on managed-model OTel spans (`examples/otel_genai_spans.jsonl`).
  - Next: point it at `examples/vllm_requests.jsonl`, or drop `--mode self-hosted`.
  - Verify: the example output contains no `?`.
- **R30 Self-hosted calibration from a vLLM snapshot lives only in the CLI.** `capacity/cli/optimize.py` (self-hosted branch, `observed_cache_hit_rate` and KV/queue mapping). `/api/v1/self-hosted/audit` callers get different results from the same telemetry.
  - Next: one shared "snapshot → `SelfHostedAuditRequest`" function in `contract/` or `engine/`.
  - Verify: `grep -n observed_cache_hit_rate capacity/cli/optimize.py` returns nothing.
- **R31 The ast-grep binary lookup is written twice, and one copy falls back to a home-directory binary.** `tests/test_repo_hygiene.py:49-60` (venv → `which` → `~/.local/bin`, which is 0.51.1 on this host against the locked 0.51.2) vs `tests/test_isolation_guard.py:337-341`.
  - Next: one `resolve_ast_grep()` in `tests/conftest.py` that uses the venv binary or fails.
  - Verify: `rg -n 'executable\).parent' tests/` matches only `conftest.py`.
- **R32 Duplicated inline fakes.** LiteLLM key and response `SimpleNamespace` trees appear at `tests/test_physics_oracles.py:232-245` and `tests/test_claude_3p.py:303-310`. ADC patches appear at `tests/test_refactor_cutpoints.py:346,353-355` and `tests/test_scenario_and_collect.py:280-282`.
  - Next: add `make_litellm_key`, `make_litellm_response` and a `fake_adc` fixture to `conftest.py` (fixes R8 in the tests at the same time).
  - Verify: `rg -n 'SimpleNamespace\(team_id|patch\("google.auth.default"' tests/ | grep -v conftest` returns nothing.
- **R33 The U items cite paths from before `v0.5.0`.** U1–U12 still name `capacity/finops.py`, `capacity/gateway.py`, `capacity/telemetry_parsers.py`, `capacity/portal*.py`, `capacity/models.py`, `capacity/core.py` and `capacity/cli_formatters.py`, none of which exist now. U6 and U10 refer to `P1.9` and `P3.1` as future work, but both have shipped.
  - Next: map each path to its folder (`engine/`, `gateway/`, `ingest/`, `showcase/`, `contract/`, `cli/formatters.py`) the next time a U item is promoted.
  - Verify: `grep -nE 'capacity/(finops|gateway|telemetry_parsers|portal[a-z_]*|models|core|cli_formatters)\.py' docs/roadmap.md` returns nothing.

### `P4`: ergonomics and optional

- **R34 The Track-A replay runs three times per suite (about 4.3 s of 22 s).** `tests/test_l3_receipt.py:66` replays only to assert `schema_version`.
  - Next: read `schema_version` from the receipt file instead.
  - Verify: `uv run --no-sync pytest -q --durations=5 -p no:cacheprovider tests/test_l3_receipt.py` shows at most 2 replays.
- **R35 `proj-test capacity` does not create the venv on a fresh host.** The workspace runner calls `scripts/gate.sh` directly.
  - Next: add `uv sync --frozen --extra dev --offline` to the runner when `.venv/bin/pytest` is missing, matching `cloudbuild.yaml:25`.
  - Verify: the gate passes with `.venv` moved aside.
- **R36 `_PUSH: "auto"` is a dead value.** `cloudbuild.yaml:6` sets it, and `:42` only tests `== "true"`.
  - Next: default to `"false"` and add a comment explaining when it pushes.
  - Verify: `grep -n _PUSH cloudbuild.yaml`.
- **R37 `gateway --target` is ignored with `--format json`.** `capacity/cli/gateway.py:65-69`.
  - Next: filter the JSON by target, or say in the help text that `--target` only affects the table.
  - Verify: `uv run --no-sync capacity gateway -t envoy -f json | grep -c litellm_config_yaml` returns 0.
- **R38 Investigate: `/api/v1/recommendations` with no network or credentials hung for about 60 s, then returned a plain 500.** The exception type was not captured; most likely a google-auth `TransportError` that is not mapped to `GCPQueryError`.
  - Next: capture the exception, map it to a 502 JSON detail, and set a client timeout.
  - Verify: a TestClient test with `google.auth.default` raising `TransportError` expects a 502 JSON response.
- **R39 The benchmark's cache claim is only shown against the weakest baseline (extends U8).** The capacity arm's `effective_prefix_cache_hit_pct` (26.84) equals the `unweighted_60s_default` arm's, and `tests/test_l3_receipt.py` compares it only against the stripped round-robin arm.
  - Next: either assert and publish the comparison with the naive gateway, or drop the cache-advantage claim from the README.
  - Verify: `uv run --no-sync pytest tests/test_l3_receipt.py`.

## Unscheduled backlog

Items below have no target release and use plain bullets rather than `- [ ]` checkboxes so `work_sync.py` counts only scheduled milestone tasks (currently `0`). Promote an item into a milestone (`- [ ]`) when scheduling it.

### Review follow-ups (`U1`–`U12`)

Pick one up when a milestone touches the same files. Items marked *changes output* alter the API, the scenario file format or published figures, so they need an owner decision before work starts.

- **U1 Split the longest functions**
  - 17 functions are longer than 100 lines and hold about half of all function lines. The longest outside the page and the command line: the gateway bundle (349 lines), the request-log parser (303), the Prometheus parser (192, the most branches in the package), scenario compilation (187), the cache audit (154), chargeback (136) and the Cloud Monitoring fetch (118). The page and command functions on that list are reshaped by Milestones 2 to 4 (`P1` to `P3`).
  - Request-log parser: one step turns a raw record into a typed usage record, using tables of field names; a second step adds the records up. Gateway bundle: one builder per gateway. Cloud Monitoring fetch: one query plan per mode and one loop that folds the results.
  - Before touching a parser, store one sample file per source format with its expected totals.
  - Files: `capacity/gateway.py`, `capacity/telemetry_parsers.py`, `capacity/scenario.py`, `capacity/burst.py`, `capacity/finops.py`, `capacity/core.py`.
  - Done when: these functions are split and the ceilings from `P1.7` are lowered to match.
- **U2 Repetition and unused pieces inside modules**
  - Written twice: padding a set of series to equal length (`capacity/finops.py`); the cost search for reserved against burst capacity, in two similar versions (`capacity/finops.py`, `capacity/self_hosted.py`); the table printer (`capacity/cli_formatters.py`); the size limits for one request (`capacity/models.py`, `capacity/telemetry_parsers.py`); the field lists of the Prometheus result and of a scheduled job row.
  - Written five times in a row: "counter difference, restart on reset" (`capacity/telemetry_parsers.py`).
  - The KPI scorecard types two burndown weights that the model family record already holds (`capacity/finops.py`).
  - Three fields of the model family record are never read, while the Cloud Monitoring adapter types the metric names that one of them holds (`capacity/model_families.py`, `capacity/core.py`).
  - Done when: each of these exists once and the family record has no unread field.
- **U3 Result fields with a fixed set of values**
  - These result fields are free text although only a few values are possible: the capacity mode, a job's status, the gateway scale status, the enforcement mode, the cache granularity and the spillover tier. The page picks a badge colour by comparing the status text.
  - The values stay the same; the published API schema gains the lists of allowed values.
  - Files: `capacity/models.py`, `capacity/model_families.py`, `capacity/portal.py`.
  - Done when: each field is typed as a fixed set of values and no code compares one as free text.
- **U4 Numbers, rounding and wording kept apart** *(changes output)*
  - Values are rounded while a result is built, so anything computed from a result starts from rounded inputs. Advice sentences are composed inside the calculations, and some type a number that is a variable beside them ("4,096" for the block size).
  - Direction: results carry full-precision values and their findings as data (a code plus its numbers). Rounding and sentences happen where a result is shown.
  - This changes the JSON output and may move benchmark figures.
  - Files: `capacity/burst.py`, `capacity/finops.py`, `capacity/self_hosted.py`, `capacity/gateway.py`, `DECISIONS.md`.
  - Done when: the decision is recorded; if it is yes, no calculation rounds a value it returns or builds a sentence.
- **U5 Serving and zero-padded percentiles**
  - `serve` hands the scenario path to the API through an environment variable, and the file is read and the page recomputed on every request. Load it once, or when the file changes.
  - The recommendations route does its sizing on the server's event loop. The other sizing routes already run in the thread pool.
  - Zero padding builds a list as long as the whole window for every series. The percentiles can be computed from the count of zeros.
  - Files: `capacity/api.py`, `capacity/cli.py`, `capacity/capacity.py`.
  - Done when: the scenario page is computed once per scenario, and no sizing runs on the event loop.
- **U6 Names that say what they hold** *(changes output)*
  - The forecast fields are named for 24 hours and hold any horizon. The telemetry snapshot, its parser and its route are named after one inference server and also carry the request logs of managed models. The page has five names: portal, showcase, workbench, scorecard and web calculator.
  - Renaming a response field or a route changes the API. Do it together with `P1.9`, which already changes the API.
  - Files: `capacity/models.py`, `capacity/telemetry_parsers.py`, `capacity/api.py`, `capacity/portal.py`, `README.md`.
  - Done when: field, class and route names say what they hold, and the page has one name.
- **U7 Tests that are easier to change**
  - Dozens of assertions look for exact fragments of command-line, HTML and YAML text, and three README blocks must equal live output character for character. Keep the full command-line text and the generated files as stored expected output, with one command to refresh them and one to rewrite the README figures from the benchmark receipt.
  - Ten test functions are longer than 50 lines. The longest is 116 lines and makes 36 assertions across more than a dozen routes. Split them into one case per behaviour. Some tests assert only an exit code or a status.
  - One test asserts a wall-clock limit of 100 ms. The hygiene test reads `specs/ast.rules.yaml` with regular expressions instead of a YAML parser.
  - A few tests restate the code or cannot fail: a capacity table is copied into a test, a count that is never negative is asserted to be at least zero, and one test only counts the POST routes.
  - Small clean-ups: ten async markers that the test configuration already implies; a lint exemption for the benchmarks that nothing uses; the 800-line cap checked by two tests with two definitions; a page test file that renders a page when it is imported, so a rendering error stops the whole file from being collected.
  - Files: `tests/test_capacity.py`, `tests/test_claude_3p.py`, `tests/test_physics_oracles.py`, `tests/test_documentation_contract.py`, `tests/test_repo_hygiene.py`, `tests/test_request_contract.py`, `tests/test_isolation_guard.py`, `tests/test_portal_export.py`, `pyproject.toml`.
  - Done when: a wording change in command-line output needs one refreshed file, and every test asserts a value.
- **U8 A benchmark that repeats and says what it measures** *(changes output)*
  - The live half is not repeatable. After 16 warm-up requests the rest are sent concurrently, and one baseline count moves between runs (61 or 62). Replica choice also uses a per-process string hash. The receipt digest covers a timestamp and latencies, so no two runs give the same receipt.
  - No gate reruns the live half: the test compares the committed figures with thresholds. Send requests in a fixed order, drop the per-process hash, keep the timestamp and latencies out of the digest, and replay the live half in the gate.
  - The two baselines in the live half are scripted in the harness (how long a blackhole lasts, how much cache a stripped signature keeps), and the test then asserts those outcomes. The reference for the drift figure is the engine's own burndown function.
  - The policy measured for the product (a 78% duty cycle, a trend clamp, weekly rounding) lives in the benchmark file, not in any product entry point. The oracle is the same simulator and the engine's own scheduler, searched over fixed commitment levels; `ADR-008` and a code comment call it independent.
  - The benchmark files type engine constants again. The corpus builder reads two sample files from the temp directory that nothing in the repository produces.
  - The test thresholds are loose: the gap to the oracle may reach 8.0 where the committed value is 3.34, and nothing compares the product arm with buying no commitment.
  - The live harness is one closure of about 150 lines, and the benchmark entry point returns three different shapes depending on a text argument.
  - The receipt's digest is stored beside the data it covers, so it catches an accidental edit and nothing more. `ADR-008` calls the receipt tamper-evident.
  - Extend the release-time source hash (`--check-source-hash`) to cover all files that decide the numbers: the model family rules, the request models, the gateway callback and the benchmark code.
  - Files: `benchmarks/live_sse_harness.py`, `benchmarks/trace_backtest.py`, `benchmarks/run_l3_benchmark.py`, `benchmarks/prepare_corpus.py`, `tests/test_l3_receipt.py`, `README.md`, `DECISIONS.md`.
  - Done when: two live runs give the same figures, the gate replays both halves, and the README and `ADR-008` describe each arm and the oracle as what they are.
- **U9 Gate extras**
  - No step measures test coverage. Start with a report that fails nothing.
  - Lint and type checks target Python 3.12 while every gate runs 3.13. Run one gate on 3.12 or raise the floor.
  - The suite prints two deprecation warnings: the async test plugin under the next pytest, and the HTTP test client.
  - The gateway callback is tested only through its generated text. Add direct tests of `capacity/litellm_burndown_callback.py`.
  - The container image is built in Cloud Build and never started. Start it once and call the health route.
  - The hygiene scans walk the working directory, so an untracked local file can fail the suite on one machine only.
  - Files: `pyproject.toml`, `cloudbuild.yaml`, `tests/`.
  - Done when: each point is fixed, or dropped with a one-line reason.
- **U10 Page content with a source**
  - Page text holds figures that come from neither the result nor the scenario (for example "0.77% -> 89.9%", "0.014% Priority spillover vs 2.58%" and "9x retry storms"). It also holds claims that are printed whatever the data says: "0% HTTP 429 Inside PT Ceiling" beside a card that totals real 429s, and "EMEA + AMER + APAC" pooling for any list of projects.
  - `P3.1` takes numbers out of the templates. This item decides what each of these becomes: a result value, a cited figure, or nothing.
  - The commitment result and the self-hosted recommendations are computed for the page and never shown. A `recommend --html-out` page never names the project.
  - The served page cannot run under a strict content security policy, because handlers, style and script are inline.
  - Files: `capacity/portal_sections.py`, `capacity/portal_template.py`, `capacity/portal.py`.
  - Done when: every figure and claim on the page comes from the result or names its source, and nothing is computed that the page does not show.
- **U11 Scenario fields that do something** *(changes output)*
  - Two blocks of the scenario file reuse the full optimisation request, 21 fields, but only 5 and 7 of them are read. The rest can be set and change nothing.
  - Give each block a model with only the fields it uses, and raise the schema version.
  - Files: `capacity/scenario.py`, `capacity/portal.py`, `examples/scenarios/multi_tenant_enterprise.json`.
  - Done when: every field a scenario file can set changes the result.
- **U12 Command-line input and text**
  - `--folder-map` drops an entry without "=" in silence. It should fail with one error line.
  - The self-hosted report prints "(<60%)" after the duty cycle whatever its value.
  - The unit is "Reps" on the command line and "Rep" on the page.
  - The report functions print straight to the terminal, so they can be tested only through the command-line runner. Let them return text.
  - Files: `capacity/cli_formatters.py`, `capacity/portal.py`.
  - Done when: a malformed folder map fails with one line, the report states a threshold only when it applies, and the formatters return text.

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

