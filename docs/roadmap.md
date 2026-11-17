# Capacity — Roadmap

The ordered list of upcoming work. Each milestone ends in a tagged release; there is no `TODO.md`. Hard invariants live in [`CRITICAL.md`](../CRITICAL.md), architectural decisions in [`DECISIONS.md`](../DECISIONS.md), visual rules in [`.stitch/DESIGN.md`](../.stitch/DESIGN.md), and implemented release history in [`archive/roadmap-history.md`](archive/roadmap-history.md) (move completed milestones or `- [x]` items with `proj-test roadmap-archive capacity --done`).

## Where we are

`v0.3.0` is public and its gate is green (`252` tests). A review of architecture and code quality on 2026-11-17 found the calculations sound and the code around them repeated. The command line, the web API and the web page each connect the calculations in their own way. Facts about models and the sample numbers are typed in several places. The page keeps its own copy of numbers and rules. The review listed 14 defects. In most of them the same input gives a different answer depending on the entry point.

The four milestones below remove the causes in order and come before the unscheduled feature extensions. File paths are as of `v0.3.0`; Milestone 3 (`P2`) moves them.

Decisions taken by the owner on 2026-11-17:

1. An empty API request no longer returns the sample answer (`P1.9`).
2. The offline HTML file is a snapshot. Its sliders need not work (`P3.2`).
3. `recommend --html-out` shows no sample data (`P1.1`).
4. Import paths may change (`P2.1`).
5. The file hash on the benchmark becomes a release-time check (`P0.1`).
6. Self-hosted sample card: no preference was given, so the request default applies, 6 replicas and 48 GPUs (`P1.4`).

## Milestones at a glance

| # | Milestone | Done when | Size (rough) |
| :--- | :--- | :--- | :--- |
| 1 | `v0.3.1` — Gates ready for change (`P0`) | A comment edit in an engine file keeps the suite green, a module moved into a sub-folder is still scanned, and one script defines the gate | 1 to 2 days |
| 2 | `v0.4.0` — Tidy in place, then one path from request to result (`P1`) | 13 of the 14 known defects are fixed (the last one goes with `P3.2`), and the command line, the API and the page call one function per operation | 1.5 to 2 weeks |
| 3 | `v0.5.0` — Folders by job (`P2`) | The guard states one rule per folder and the gate is green | 2 to 4 days |
| 4 | `v0.6.0` — The page shows only what the engine computed (`P3`) | No number or rule is typed in a template or in the browser script | 3 to 5 days |

Each milestone ends in a tagged release: Milestone 1 (`P0`) a patch release with no change in behaviour, Milestones 2 to 4 (`P1` to `P3`) a minor release each. Milestone 2 (`P1`) may ship in two releases, one per part.

## Milestone 1 — `v0.3.1`: Gates ready for change (`P0`)

- [ ] **P0.1 The benchmark is checked on behaviour, not on file bytes**
  - Today a test compares a hash of six engine files, so a comment edit forces a live benchmark run and hand edits to the README.
  - Keep the replay of the committed trace as the check on every run. Move the hash comparison to a release-time check that is off by default. Record the commit id in the receipt. Add tests that a damaged receipt and a drifted replay are rejected. Amend `ADR-008`.
  - Files: `benchmarks/run_l3_benchmark.py`, `tests/test_l3_receipt.py`, `DECISIONS.md`, `AGENTS.md`.
  - Done when: a comment edit in `capacity/burst.py` leaves the suite green, and a changed burndown weight still fails it.
- [ ] **P0.2 The architecture guards follow a move into sub-folders**
  - Both guards assume every module sits directly in `capacity/`. After a move, moved modules are no longer scanned.
  - Scan sub-folders. Name the adapter files once, by folder where possible. Resolve relative imports from the file's real package. Assert that every file is classified exactly once. Add the new import spellings to the guard's own examples.
  - Files: `tests/test_isolation_guard.py`, `specs/ast.rules.yaml`.
  - Done when: a probe module in a sub-folder that imports the Cloud Monitoring adapter fails both guards.
- [ ] **P0.3 Shared test fixtures**
  - Add `tests/conftest.py` with one fake Cloud Monitoring client that owns the patch target (repeated in 9 places today), one time-series builder, one cache fake and one loader for the generated gateway callback (it is loaded three different ways today).
  - Remove the second leak scanner in `tests/test_capacity.py`. Test through public functions where a private name is imported today, and take the private name out of `__all__` in `capacity/core.py`.
  - Done when: the patch target string appears once in `tests/`.
- [ ] **P0.4 Tests where the refactor will cut**
  - Cover what has no test today: Cloud Monitoring errors (missing credentials, permission denied, API error) through to exit code 1 on the command line and HTTP 502 on the API; `--folder-map` parsing; trace files shaped as a mapping; the `--format json` branches; scenario compilation from live series; rejection of NaN and infinity; `recommend --html-out`; the `collect` paths with `--check-auth` and with a live project; the lifecycle class of a model id.
  - Files: `tests/`, exercising `capacity/core.py`, `capacity/cli.py`, `capacity/cli_formatters.py` and `capacity/scenario.py`.
- [ ] **P0.5 One definition of the gate**
  - Put the gate in one script in the repository and call it from `AGENTS.md`, the workspace runner and `cloudbuild.yaml`. Run the `ast-grep` scan, the format check and the benchmark replay once, not twice.
  - Correct the two outdated notes in `AGENTS.md`: the lock check does run offline, and the hygiene test walks the directory.
  - Done when: the three places run the same script.

## Milestone 2 — `v0.4.0`: Tidy in place, then one path from request to result (`P1`)

Part A (`P1.1` to `P1.7`) fixes 11 of the 14 defects without new structure. Part B (`P1.8` to `P1.11`) gives every operation one shape and fixes two more (`P1.8`, `P1.9`). The last defect, the browser script replacing the page's numbers, goes with `P3.2`. Command-line text and the README blocks stay identical except where a decision above changes them.

- [ ] **P1.1 The page shows the data it was given**
  - `recommend --html-out` renders the sizing tab only, with no sample data. Its duty-cycle card comes from the data; today it always reads 23.8%.
  - On a scenario page the gateway cards and inputs, the sizer inputs and badge, and the cache badge, banner and inputs come from the computed result. Today they keep the values of the built-in demo, and the sizer badge stays green when the saving it reports is negative.
  - With a capacity override every tab uses the same capacity for a model.
  - An unknown capacity shows "?" in every cell. The forecast column names the real horizon, and the response carries the horizon and the requested percentiles.
  - The browser's cache badge follows the engine's rule for models that cache at token boundaries. Today it can read "healthy, 0.0% hit".
  - Files: `capacity/portal.py`, `capacity/portal_sections.py`, `capacity/portal_template.py`, `capacity/portal_scripts.py`, `capacity/models.py`.
  - Done when: one render test per case asserts engine values, not phrases.
- [ ] **P1.2 Trace input has one clock and one window rule**
  - A plain list of minutes is turned into a dated series in four places with two start hours. The same list forecasts 467,000 through `--trace-file` and 400,000 through a scenario file. A trace file shaped as a mapping can report a duty cycle above 100%.
  - Files: `capacity/forecasting.py`, `capacity/cli.py`, `capacity/cli_formatters.py`, `capacity/portal.py`.
  - Done when: one list gives one forecast through every entry point, and a duty cycle never exceeds 100%.
- [ ] **P1.3 Small fixes**
  - `optimize --mode self-hosted` works without `--model`. Today the default is a managed model id.
  - Scenario compilation fails on an unknown model and no longer assumes a default model and capacity (`capacity/scenario.py`).
  - The pooling mode `silo_stranding_audit` either changes the page or is removed. An annotation `note` is either shown or removed.
  - The test for leftover placeholders also matches names with digits (`tests/test_portal_export.py`).
- [ ] **P1.4 The built-in demo is the default scenario**
  - The page builder has a second branch that repeats the default scenario as literals and has drifted in one block. Delete it and build the demo from a default scenario.
  - Keep the sample data (trace, tenants and their weights, jobs, projects) in one module. The self-hosted sample follows the request default: 6 replicas, 48 GPUs.
  - Files: `capacity/portal.py`, `capacity/scenario.py`, `capacity/cli.py`, `capacity/finops.py`, `capacity/forecasting.py`.
  - Done when: `capacity demo` and a scenario file that sets only defaults give the same result.
- [ ] **P1.5 Named constants and one version**
  - Name the numbers that the self-hosted sizer, the cache audit and the gateway audit assume, each with a one-line source.
  - Read the version from the package metadata. It is typed in ten lines today.
  - Files: `capacity/self_hosted.py`, `capacity/burst.py`, `capacity/gateway.py`, `capacity/finops.py`, `capacity/api.py`, `capacity/cli.py`, `capacity/portal.py`, `capacity/scenario.py`.
- [ ] **P1.6 Dead code and start-up time**
  - Load the Cloud Monitoring SDK only when a command needs it. Every command pays about half a second for it today.
  - Delete the fallback for a missing SDK (it cannot run, because the SDK is a required dependency), the function that only forwards to another, and the re-export lists that keep old import paths alive.
  - Move the body of the `collect` command out of the formatter module into a module of its own. `capacity/cli.py` is at the 800-line cap, so one more line fails the gate. Free room in it before other `P1` tasks add to it, for example by moving the text reports of `demo`, `gateway` and `schedule` beside the other formatters.
  - Files: `capacity/core.py`, `capacity/self_hosted.py`, `capacity/cli.py`, `capacity/cli_formatters.py`.
  - Done when: `capacity demo` starts without importing the SDK.
- [ ] **P1.7 Complexity rules on, as a ratchet**
  - Switch on the complexity lint rules that are listed as debt, with ceilings set to today's worst values. Lower the ceilings as functions are split. Remove the five entries from `tests/standards_debt.json`.
  - Files: `pyproject.toml`, `tests/standards_debt.json`.
- [ ] **P1.8 One model catalog**
  - One record per model holds capacity, family rules, lifecycle and display label. Today these sit in six places and are matched by substring: an id that contains "pro" is read as the Pro tier, and an unknown id gets default weights without an error.
  - An unknown id is the same error everywhere. Overrides from the environment or a scenario produce a catalog.
  - The rule for the 1-second and the 10-second bucket lives in one function. The Envoy expression is built from the family record.
  - Files: `capacity/model_families.py`, `capacity/capacity.py`, `capacity/burst.py`, `capacity/gateway.py`, `capacity/scenario.py`, `capacity/core.py`, `capacity/portal_sections.py`, `benchmarks/trace_backtest.py`.
  - Done when: adding a model is one record, and no module matches model names by substring.
- [ ] **P1.9 Every operation takes a checked request and returns a typed result**
  - Each operation is one function that takes its request model and looks the model up itself, as optimisation and the gateway bundle already do.
  - The capacity mode moves into the request, so the API accepts self-hosted chargeback as the command line does.
  - Data fields (series, tenants, jobs, projects) become required. An empty request is rejected. Defaults remain only for prices and percentiles.
  - The command line takes its defaults from the same request models, so `optimize`, `schedule` and `chargeback` run on sample data today when no file is given, and the README quick start uses that. Decide when this task starts whether they then need an explicit sample switch, as `recommend --demo` has, or an input file from `examples/`.
  - Remove the helper that unpacks requests, the defaults repeated in function signatures, the reflection tests that keep the two in step, and the silent re-clamping inside the calculations.
  - Files: `capacity/finops.py`, `capacity/burst.py`, `capacity/self_hosted.py`, `capacity/gateway.py`, `capacity/capacity.py`, `capacity/models.py`, `capacity/api.py`, `capacity/cli.py`, `tests/test_request_contract.py`.
  - Done when: the command line and the API each make one call per operation and give the same result for the same request in both modes.
- [ ] **P1.10 Scenario evaluation returns a typed report, outside the page**
  - Move whole-scenario evaluation out of the page module into one function that returns a typed report. `capacity demo --format json`, the page and an API route read that report.
  - The report holds only what the scenario defines. Today a scenario's result also carries sample self-hosted series under the scenario's own project label.
  - Files: `capacity/portal.py`, `capacity/scenario.py`, `capacity/cli.py`, `capacity/api.py`.
  - Done when: the page renderer takes a report and calls no calculation.
- [ ] **P1.11 Telemetry series have a typed key**
  - Replace the text key (`folder/model@region`) with a typed key. Filter by model and region, and apply the folder map, in one place.
  - `collect` reads through the same port as `recommend`, and the access check returns a typed result.
  - Files: `capacity/forecasting.py`, `capacity/core.py`, `capacity/capacity.py`, `capacity/scenario.py`, `capacity/cli_formatters.py`.

## Milestone 3 — `v0.5.0`: Folders by job (`P2`)

Target layout across Ports, Adapters and Contracts:

```text
capacity/
  contract/    requests, results, scenario schema
  engine/      sizing, burst, commitment, chargeback, scheduler, pooling, kpis,
               self_hosted, stats, catalog
  ingest/      prometheus, request_logs, trace_files
  telemetry/   port, local, cloud_monitoring
  gateway/     litellm, envoy, gke, header_policies, callback template
  showcase/    evaluate (scenario to report), sample data, page renderer, assets/
  cli/         one module per command, output formatting
  api.py
```

- [ ] **P2.1 Move modules into folders by job**
  - Move with `git mv`. Old import paths are not kept: no re-export modules.
  - Update the imports in `tests/` and `benchmarks/`, the entry points in `pyproject.toml`, `Dockerfile` and `capacity/cli.py`, the lint exemption for the gateway callback, and the path the gateway generator reads the callback from.
  - Done when: the gate is green and the README blocks are unchanged.
- [ ] **P2.2 Split the modules that hold several jobs**
  - `finops` into commitment, chargeback, scheduler, pooling and KPIs. `forecasting` into maths, port and local adapter. `models` into requests and results. The command line into one module per command.
  - Done when: no module exists only to keep another one under the line cap.
- [ ] **P2.3 The guard states one rule per folder**
  - The engine imports only the contract. One file imports the cloud SDK. The page renderer imports no calculation. The command line and the API may import anything.
  - Files: `specs/ast.rules.yaml`, `tests/test_isolation_guard.py`, the repository map in `AGENTS.md`, `ADR-005` in `DECISIONS.md`, and the links in `docs/`.

## Milestone 4 — `v0.6.0`: The page shows only what the engine computed (`P3`)

- [ ] **P3.1 One typed, display-ready object for the page**
  - It carries every card, table, label and control default. The renderer turns it into HTML and escapes everything in one place.
  - Files: `capacity/portal.py`, `capacity/portal_sections.py`, `capacity/portal_template.py`.
  - Done when: no template holds a number, and a test renders a hand-made object.
- [ ] **P3.2 No formulas in the browser**
  - Today the first change to a control on a scenario page replaces the server's numbers with ones from the script's own tables, which do not know the scenario.
  - The offline file is a snapshot: its controls are disabled or removed. The served page sends control changes to the API and shows the answer. This needs one new route for the burst window at a fixed number of GSUs.
  - Delete the tables and rules typed in the script. Amend `ADR-004`.
  - Files: `capacity/portal_scripts.py`, `capacity/api.py`, `capacity/models.py`, `DECISIONS.md`.
  - Done when: the script holds no capacity, threshold or weight.
- [ ] **P3.3 HTML, CSS and JavaScript as files**
  - Keep them as real files inside the package and join them at render time, so the export stays one file. Extend the identifier scan to `.js` and `.css`.
  - Files: `capacity/portal_template.py`, `capacity/portal_scripts.py`, `tests/test_repo_hygiene.py`.
- [ ] **P3.4 Page tests on values**
  - Send hostile text through every scenario text field. Render once per combination of page sections and assert engine values.
  - Files: `tests/test_portal_export.py`.

## Unscheduled backlog

Items below have no target release and use plain bullets rather than `- [ ]` checkboxes so `work_sync.py` counts only the 23 scheduled milestone tasks above. Promote an item into a milestone (`- [ ]`) when scheduling it.

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
- **U5 Start-up and serving**
  - The environment file is loaded when the command line or the API is imported, so an untracked local file can change test results. Load it in the command-line entry point and at server start.
  - `serve` hands the scenario path to the API through an environment variable, and the file is read and the page recomputed on every request. Load it once, or when the file changes.
  - The recommendations route does its sizing on the server's event loop. The other sizing routes already run in the thread pool.
  - Zero padding builds a list as long as the whole window for every series. The percentiles can be computed from the count of zeros.
  - Files: `capacity/api.py`, `capacity/cli.py`, `capacity/capacity.py`.
  - Done when: importing the command line or the API reads no file, the scenario page is computed once per scenario, and no sizing runs on the event loop.
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
  - If the file hash stays as a release-time check (`P0.1`), it should cover the files that decide the numbers: the model family rules, the request models, the gateway callback and the benchmark code.
  - Files: `benchmarks/live_sse_harness.py`, `benchmarks/trace_backtest.py`, `benchmarks/run_l3_benchmark.py`, `benchmarks/prepare_corpus.py`, `tests/test_l3_receipt.py`, `README.md`, `DECISIONS.md`.
  - Done when: two live runs give the same figures, the gate replays both halves, and the README and `ADR-008` describe each arm and the oracle as what they are.
- **U9 Gate extras**
  - No step measures test coverage. Start with a report that fails nothing.
  - Lint and type checks target Python 3.12 while every gate runs 3.13. Run one gate on 3.12 or raise the floor.
  - The suite prints two deprecation warnings: the async test plugin under the next pytest, and the HTTP test client.
  - The gateway callback is tested only through its generated text. Add direct tests of `capacity/litellm_burndown_callback.py`.
  - The container image is built in Cloud Build and never started. Start it once and call the health route.
  - The hygiene scans walk the working directory, so an untracked local file can fail the suite on one machine only.
  - Tests import `benchmarks` without a `pythonpath` setting. It works only through the editable install.
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
- **Read-Only GCE GPU/TPU Supply Advisory (`--mode self-hosted` Placement Bridge):** Pair self-hosted replica sizing ([`capacity/self_hosted.py`](../capacity/self_hosted.py)) with read-only GCE Capacity Advisory REST checks (`regions/{r}/advice/calendarMode` with `100%/75%/50%/25%` split planning plus alpha `regions/{r}/advice/capacity` for `SPOT` obtainability/uptime and `FLEX_START` estimated queue wait, omitting `guestAccelerators` on built-in GPU families `G2/G4/A2/A3/A4/A4X` and respecting `87h`/`720h` Calendar lead times).
- **Sub-Second `10s`-Bucket Occupancy Guard in `optimize_hybrid_commitment` ([`capacity/finops.py`](../capacity/finops.py)):** Surfaced by the consolidated 7-day Azure trace backtest ([`benchmarks/results/l3_receipt.json`](../benchmarks/results/l3_receipt.json)): when highly bursty `10s` coding arrivals plus light weekend traffic hold sub-second 168h bucket occupancy (`37.02%`) below the `42.55%` PT-vs-Priority break-even threshold, coupling `optimize_hybrid_commitment` directly to empirical `10s` bucket fill factors can close the remaining `3.34%` gap to the `L2` hindsight bound while preserving an optional explicit SLA floor.
- **ADK Prefix-Cache Reference Patterns ([`capacity/burst.py`](../capacity/burst.py)):** Capture concrete ADK runtime patterns (`instruction=""` + fixed `static_instruction` + `ContextCacheConfig(min_tokens=4096)`, trailing `<system-reminder>` callbacks, transient user-turn memory injection, and reclaim-thresholded tool output pruning to prevent Block 0 cache thrash).
- **Per-Turn Model Routing & Multi-Backend Proxy Recipes ([`capacity/gateway.py`](../capacity/gateway.py)):** Model per-turn tier switching (`gemini-3.5-flash-lite` -> `gemini-3.8-flash` -> `gemini-4-argon`) alongside gateway session affinity and `thought_signature` preservation checks.
- **Multi-Node Cloud Redis & Envoy Global Rate-Limit Service Integration Suite:** Extend [`benchmarks/live_sse_harness.py`](../benchmarks/live_sse_harness.py) with an optional Docker Compose Envoy AI Gateway + `envoyproxy/ratelimit` + Valkey cluster testbench for `>2,000 RPS` load testing.
- **RDMA / NIXL KV-Cache Transfer Latency Modeling ([`capacity/self_hosted.py`](../capacity/self_hosted.py)):** Incorporate inter-node KV-block transfer bandwidth overhead into disaggregated Prefill/Decode pool sizing for `>128K` context prompts.

### Showcase presentation gates

- **Hero SVG word budget (`docs/assets/architecture_card.svg`):** Trim visible text in [`docs/assets/architecture_card.svg`](assets/architecture_card.svg) to `<= 35–45` words so first-screen skimmers read the closed-loop diagram in 5 seconds.
- **README body budget & "Why not" section (`README.md`):** Tighten [`README.md`](../README.md) prose to `<= 800` words with an explicit `"Why not"` comparison section while keeping verbatim CLI/YAML and receipt parity tests green.

## Shipped

- **v0.2.0-v0.3.0 (Passes A-G + 3P Claude):** Phase 1 (v0.2.0) and Phase 2 (v0.3.0) dual-mode GSU/vLLM sizing, 1P Gemini + 3P Claude burndown and prefix-cache physics, LiteLLM/Envoy/GKE gateway generator, disaggregated prefill/decode sizer, 3-Tier EDF scheduler, FOCUS 1.2 chargeback, --scenario-file and capacity collect, GM3 Light workbench, and L3 Azure/SSE benchmark — [archive](archive/roadmap-history.md#v020-v030-passes-a-g-3p-claude)

