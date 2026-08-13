# Charlie History Archive (2026-04-24 to 2026-05-04)

This file contains archived history entries for Charlie (Tester) to keep history.md under 15KB. Entries are organized by issue and phase.

## 2026-04-24 Project Onboarding

- Charlie owns test strategy, acceptance criteria, and quality gates for the control plane.
- Highest-risk areas: auth, operator actions, backend integration failures, regression coverage.
- Core ADME/OSDU M25 services: storage, search, schema, legal, entitlements, workflow, file, dataset, indexer, notification, eds.
- Reusable Streamlit test pattern: monkeypatch module-level `st` import with `tests.support.streamlit_recorder.StreamlitRecorder`
- Key test paths: `app\main.py`, `app\pages\`, `tests\conftest.py`, `tests\test_main.py`
- Operator workflow needs: welcome/settings pages, two auth modes (user_impersonation, service_principal), required connection inputs, service-by-service health reporting

## Issue #2 ADME Connection Architecture (2026-04-24 to 2026-04-24)

**Testing Plan:**
- Coverage for auth-mode-specific required fields
- Per-service health matrices for M25 services
- Explicit partial-failure handling without secret leakage
- Product signoff before scope creep

**Phase 1 - Planning:**
- Identified critical review risks: auth switching, unauthorized access, timeouts, mixed health states
- Set review gate: blocked on test coverage for dangerous paths
- Identified scope drift concern on data_partition_id

**Phase 2 - Implementation Review:**
- Rejected because Indexer probe was `GET /api/indexer/v2/reindex` (mutating, invalid health check)
- Named Kevin as required reviser (Satya authored the contract, Kevin owns health probes)

**Phase 3 - Kevin's Fix:**
- Changed Indexer probe to `GET /api/indexer/v2/readiness_check` (read-only, valid)
- Tests updated to pin readiness endpoint and guard against regression
- Added EDS health endpoint coverage

**Final Approval (2026-04-24):**
- All acceptance criteria verified as met
- Auth-mode-specific field coverage (conditional client_secret)
- Per-service health matrices for all 11 M25 services
- Explicit partial-failure handling (no secret leakage)
- Indexer readiness probe regression protection
- No scope creep beyond contract
- Ready to close issue #2

## Issue #3 Streamlit Import-Path Fix (2026-04-24)

**Final Review & Approval:**
- Minimal impact (4-line bootstrap in app/main.py and page scripts)
- Idempotent (guards against double-insertion)
- Meaningful regression coverage (subprocess tests simulate Streamlit-style loading)
- No test regressions
- Production-ready
- Ready to close issue #3

## Issue #4 Interactive Browser Login (2026-04-24)

**Acceptance Criteria & Test Gates:**
- Auth behavior: DeviceCodeCredential removed, InteractiveBrowserCredential active
- UI help text: browser sign-in guidance present, device-code wording removed
- Test coverage: >=90% auth.py coverage, unit/integration tests passing
- Reviewer gates: credential replacement verified, error messages browser-friendly, service principal unchanged, headless fallback explicit

**Final Review & Approval (2026-04-24):**
- DeviceCodeCredential removed entirely (no imports, no references)
- InteractiveBrowserCredential active (correct import, instantiation, constructor call)
- Service-principal auth unchanged (ClientSecretCredential still used)
- UI text clean (browser guidance present, device-code wording removed)
- Error messages browser-friendly (browser login language, 'Run Test Connection again' guidance)
- Test coverage: 92% auth.py (exceeds 90% gate), all tests passing, no regressions
- Headless fallback: CredentialUnavailableError raised, graceful error handling
- Production-ready, ready to close issue #4

## Issue #5 Auth Callback Fix (2026-04-25)

**Acceptance Criteria & Test Gates:**
- Browser sign-in ÔåÆ token exchange success (no AADSTS7000218)
- Settings page success state
- Error handling (cancelled browser, unavailable)
- Code review: public client ID, scope preservation, service principal untouched
- Test coverage: >=90%, unit/integration/regression tests
- Integration: end-to-end Settings flow

**Final Review & Approval (2026-04-25):**
- Azure CLI public client ID correctly defined and used for USER_IMPERSONATION
- Service-principal ClientSecretCredential path unchanged
- Scope derivation uses connection.client_id (token audience = ADME resource)
- Test coverage: 93% (exceeds 90%)
- End-to-end Settings workflow: browser auth succeeds, green validation summary
- Error handling: AADSTS7000218 eliminated, CredentialUnavailableError graceful
- No blockers, production-ready, ready to close issue #5

## Issue #6 Tenant-Compatible Auth (2026-04-25)

**Testing Plan & Review Gates:**
- Multi-tenant auth preserved (tenant_id passed to InteractiveBrowserCredential)
- Token acquisition unchanged
- Session storage unaffected
- Unit tests verify credential construction with tenant_id
- Help text mentions tenant ID requirement

**Final Review & Approval (2026-04-25):**
- Tenant-aware auth behavior preserved
- Token acquisition and session storage unaffected
- Unit tests verify tenant_id passed to credential constructor
- Help text updated to mention tenant ID requirement
- No cross-tenant auth confusion
- Production-ready, ready to close issue #6

## Issue #7 Auth Redirect to Localhost (2026-04-25)

**Acceptance Criteria & Test Gates:**
- Interactive browser auth redirects to localhost:8400
- Settings page guidance matches implementation behavior
- No localhost:8400 in error messages (implementation detail)
- Tenant-aware auth preserved
- Token acquisition and session storage unchanged
- Unit tests verify redirect_uri parameter passed
- Help text audit and update required

**Final Review & Approval (2026-04-25):**
- InteractiveBrowserCredential receives explicit `redirect_uri="http://localhost:8400"`
- Settings page guidance matches implemented behavior
- Implementation detail (localhost:8400) not exposed in error messages
- Tenant-aware auth preserved
- Token acquisition and session storage unaffected
- Unit tests verify redirect_uri parameter passed to credential
- Help text consistent with behavior
- Multi-tenant compatibility verified
- Production-ready, ready to close issue #7

## Issue #8 MSAL Auth Integration (2026-05-05)

**Final Completion & Team Validation:**
- Satya: Lead review and final validation
- Kevin: Auth-service implementation (MSAL + pending flow handling)
- Scott: Documentation and README updates
- Judson: Settings page integration
- Charlie: Quality gate and regression coverage (distinguished stale vs new pending flows)
- Full test suite: 70 tests passing, Ruff clean, mypy clean
- Ready for merge

## Manual Token Scope Configuration (2026-05-05)

**Status:** COMPLETE
**Decision:** Manual token scope configuration merged to decisions.md
**Outcome:** ADMEConnection now includes token_scope field with ADME default fallback. Settings UI exposes non-secret Token scope field. Both auth paths consume connection.scope. Validation: pytest 80, ruff, mypy clean.

## Learnings Summary

- Reusable Streamlit test pattern (monkeypatch st) is effective for page-level testing
- Auth workflow testing requires coverage of mode switching, secret masking, and per-service health states
- Test gates must be comprehensive: credential behavior, error messages, UI text, regression coverage
- Multi-auth-mode design is complex; regression tests must distinguish stale flows from new ones
- Health probe selection is critical: avoid mutating endpoints, use read-only or dedicated health endpoints
- Operator UX requires clear messaging for browser redirects, tenant/scope requirements, and error recovery
- Team sign-off protocol: lead review, named reviser if issues found, comprehensive re-review after fixes
- Acceptance criteria defined upfront enable fast iteration and clear gate definition

<!-- Archived 2026-05-18T20:00:00Z by Scribe (history summarization) -->

# Project Context

- **Owner:** Eirik Haughom
- **Project:** Streamlit control plane app for Azure Data Manager for Energy (ADME)
- **Stack:** Python, Streamlit, Azure, ADME/OSDU APIs
- **Created:** 2026-04-24

## Current Role Summary

- Charlie owns test strategy, acceptance criteria, and quality gates for the control plane.
- The highest-risk areas are likely auth, operator actions, backend integration failures, and regression coverage.
- 2026-04-24: Issue #2 health validation should cover the core ADME/OSDU M25 services: storage, search, schema, legal, entitlements, workflow, file, dataset, indexer, notification, and eds.
- 2026-04-24: A reusable Streamlit test pattern in this repo is to monkeypatch the module-level `st` import with `tests.support.streamlit_recorder.StreamlitRecorder` and assert recorded UI calls.
- 2026-04-24: Key test paths for the welcome/settings work are `app/main.py`, `app/pages/`, `tests/conftest.py`, and `tests/test_main.py`.
- 2026-04-24: `app/models/connection.py` is the shared UI/backend contract for auth methods and health probes; verify scope drift on contract changes.
- 2026-05-05: Issue #8 auth review added regression coverage that distinguishes stale MSAL pending flows from newly generated retry flows after missing-pending, auth-denial, state-mismatch, and token-exchange failures.
- 2026-05-05: Manual token scope review accepted Kevin/Judson blank-as-default fallback (superseding Satya's earlier blank-invalid stance) since tests, auth behavior, and operator guidance stayed internally consistent. Settings field guidance must itself say token scope is not a token or secret — README-only safety wording is insufficient.
- 2026-05-05: Production Settings copy must satisfy lint gates (Ruff E501) in addition to assertion gates; lockout-safe revisions still need to clear lint.
- 2026-05-07 (Legal Tags review): When the page uses selection widgets (`st.selectbox`, `st.toggle`, `st.multiselect`, `st.date_input`, `st.text_area`), `StreamlitRecorder` MUST expose explicit methods returning configured `widget_values[label]` — the bare `__getattr__` fallback returns `None`, which silently breaks every page-render assertion. Five widget primitives are now in `tests/support/streamlit_recorder.py` and should be reused for future pages.
- 2026-05-07 (Legal Tags review): OSDU `:properties` endpoint uses a colon, not a slash — `/api/legal/v1/legaltags:properties`. This is a recurring footgun: when controller research (Darryl) and spec-style assumption (Satya) diverge on URL shape, the controller source wins. Pin the URL with a service test that asserts the exact path so silent regressions surface immediately.
- 2026-05-07 (Legal Tags review): Page widgets bound with `key=...` write to `st.session_state` AND read from `widget_values` separately in the recorder — they do not auto-sync. To force a "filter changed" flow, set BOTH `session_state[key]` and `widget_values[label]`; or test the equivalent path via the explicit Refresh button. Tests that only set `widget_values` will see the page read the old `session_state` value and skip the branch.
- 2026-05-07 (Legal Tags review): Page-test `_patch_services` must monkeypatch the names AS IMPORTED INTO THE PAGE MODULE'S NAMESPACE (e.g., `monkeypatch.setattr(page_module, "list_legal_tags", ...)`), not the source module. The page does `from app.services.legal_tags import (list_legal_tags, ...)` so patches against `app.services.legal_tags` would miss the page's bound name.
- 2026-05-07 (Legal Tags review): Reviewer judgment when 3+ specialists diverge on the same artifact — assign canonical authority per topic, not per author: Darryl wins on OSDU controller facts (URL paths, response shapes), Satya wins on internal contract style (request body shape, locked session keys), Kevin wins on backend implementation coherence. Document divergences as non-blocking flags so the next iteration can true them up without blocking ship.

## Reviewer log (older issues — full detail in history-archive.md)

- **Issue #2** (M25 health probes) — initial REJECT (Indexer probe used `/reindex` which is PATCH/POST, not a valid GET probe); Kevin reassigned per lockout. APPROVE after Kevin changed probe to `GET /api/indexer/v2/readiness_check`, locked in by tests. 11 services covered, client_secret masked, partial-failure semantics defined.
- **Issue #3** (Streamlit import path) — APPROVE. 4-line idempotent bootstrap, subprocess regression test.
- **Issue #4** (DeviceCode → InteractiveBrowser) — APPROVE. 92% auth.py coverage, headless fallback via `CredentialUnavailableError`.
- **Issue #5** (Azure CLI public client ID for user impersonation) — APPROVE. 93% coverage, AADSTS7000218 eliminated, service-principal path unchanged.
- **Issue #6** (customer's app registration + hardcoded ADME scope) — APPROVE. 24/24 tests; AZURE_CLI_PUBLIC_CLIENT_ID removed; scope hardcoded to `https://energy.azure.com/.default`.
- **Issue #7** (`redirect_uri="http://localhost:8400"`) — APPROVE. 26/26 tests; tenant-agnostic redirect URI, new-browser-tab UX guidance.

## Issue #8 Auth Flow — Team Completion (2026-05-05)

**Status:** APPROVE. Full suite 70 passed, Ruff + mypy clean. MSAL `PublicClientApplication` auth-code + PKCE replaces `InteractiveBrowserCredential` for user impersonation; service-principal path unchanged.

## 2026-05-05: Manual Token Scope Configuration

**Status:** APPROVE (final). Pytest 80, Ruff, mypy all clean. `ADMEConnection.token_scope` added; `connection.scope` accessor trims and falls back to ADME default. Both auth paths consume `connection.scope`. Settings exposes non-secret Token scope field with explicit non-secret guidance.

## 2026-05-05 Settings Store Persistence Tests

- Added `tests/test_settings_store.py` (20 tests): round-trip save/load, `client_secret` drop asserted via loaded model AND raw on-disk bytes, single-active-row invariant via partial unique index, delete-of-active clears `get_active_connection_name()`, idempotent `initialize_store`, `ADME_SETTINGS_DB` env override, unknown/empty name handling, upsert preserves active flag.
- Extended `tests/test_connection_state.py` (+5): `ensure_session_defaults` hydrates `CONNECTION_KEY` from active stored row, returns None when nothing active, preserves in-flight session connection over disk value, swallows `SettingsStoreError` during hydration, `save_connection` writes through to store and marks active while dropping `client_secret`.
- Isolation: `monkeypatch.setenv("ADME_SETTINGS_DB", str(tmp_path / "settings.db"))`. No test touches `Path.home()`.
- Targeted suite: 35 passed in 2.02s. settings_store.py 84% coverage; connection_state.py 83%.
- Verdict: Kevin's implementation matches Satya's contract.

### 2026-05-05 — Test pollution from real ~/.adme-ingestion-tool/settings.db

**Symptom:** Two tests passed individually but failed in the full suite:
- `tests/test_main.py::test_main_prompts_operator_to_open_settings_when_not_configured`
- `tests/test_settings_page.py::test_settings_page_defaults_token_scope_to_adme_resource_scope`

**Root cause:** `app.connection_state.ensure_session_defaults` hydrates from the on-disk SQLite store via `settings_store.get_db_path()`. When a test did not set `ADME_SETTINGS_DB`, that resolved to `~/.adme-ingestion-tool/settings.db` — the operator's REAL profile DB, populated by actual app use. Hydration found an active stored connection and broke the "no configuration yet" assumption. Order-dependent because the operator's real DB was already populated before pytest started.

**Fix:** Autouse fixture `_isolate_settings_db` in `tests/conftest.py` that sets `ADME_SETTINGS_DB` to `tmp_path/settings.db` for EVERY test.

**Lesson — durable test isolation for environment-driven file paths:** If production code reads a path from the environment with a home-directory default, EVERY test must redirect that env var. Per-test opt-in fixtures are fragile — one new test that forgets to request the fixture re-opens the leak. Autouse is the only durable fix. Pattern: any module that reads `Path.home() / ...` or similar needs an autouse isolation fixture at the conftest root, not at individual test-file scope.

**No reset hook needed:** `app.services.settings_store` opens short-lived `sqlite3` connections via `closing()` per call with no module-level state. Switching the env var between tests is sufficient; no cache to clear.

### 2026-05-05 Entitlements service + page test pass — APPROVE with two non-blocking notes

**Files added:**
- `tests/test_entitlements_service.py` (24 tests): happy-path member.self + groups, 401/403/500 with JSON body, 502 with text body, missing correlation header, `Timeout` and `ConnectionError` transport failures, correlation-ID case-insensitive lookup across all four header names (parametrized) plus first-hit-wins and fallback-to-later-candidates, trailing-slash URL stripping, outgoing headers (Authorization Bearer, data-partition-id, Accept JSON, timeout=5, allow_redirects=False), invalid-connection ValueError, empty-token ValueError (parametrized).
- `tests/test_entitlements_page.py` (10 tests): no-connection preflight, user-impersonation no-token preflight, missing data partition preflight, auto-run-once on first render, no re-fire on second render without button, Re-run button bypasses guard, two history entries per run, clear-history button empties session state, error rendering surfaces friendly message + HTTP status + correlation_id, user-impersonation with stored `UserAuthState` runs the test.

**Recorder extension:** Added `StreamlitRecorder.expander` so `with st.expander(...)` blocks behave as context managers (Judson's page uses expanders for raw JSON; settings page never did, so this is a net-new tool).

**Test_main fix:** `test_main_prompts_operator_to_open_settings_when_not_configured` previously asserted exactly one `page_link` call; Judson's `main.py` deliberately adds a second `page_link` to the entitlements page. Updated the assertion to filter by args for the Settings link instead of unpacking the full list. The Entitlements link was an intentional UX addition, not a regression.

**Validation:**
- `python -m pytest -q tests/test_entitlements_service.py tests/test_entitlements_page.py`: 34 passed.
- `python -m pytest -q`: **139 passed**, 87% total coverage. `app/services/entitlements.py` 85%, `app/pages/2_🔑_Entitlements.py` 86%.
- `python -m ruff check` on touched test files: clean.

**Verdict: APPROVE.** Kevin's service and Judson's page satisfy Satya's contract for operators.

**Non-blocking flags (note, do not block):**
1. *URL discrepancy:* Satya's contract quoted `/api/entitlements/v2/members/{me}` with a literal `{me}` placeholder per the ADME doc convention. Kevin shipped `/me` (the actual ADME endpoint path operators must hit). The page test pins the URL operators see (`.../members/me`), which is correct. The contract text is the doc-ambiguity, not the implementation. Operators are unaffected.
2. *error_message convention:* Satya's contract said `error_message` defaults to empty string on success (mirroring `ServiceHealthResult`). The shipped `EntitlementsCallResult` defaults to `None` and Kevin sets it to `None` on success. The page reads `error_message` only on the failure path (`_render_error_block` with `or "Unknown error."`), so operators never see the difference. Note for future contract alignment but no operator-visible impact.

**Lessons:**
- *Streamlit page coverage requires the recorder to know every context manager the page uses.* Adding a new context-manager primitive to a page (`st.expander`) breaks every page test until the recorder gains a matching helper. Pattern: when a page introduces a new `with st.X(...)` block, add the matching `X` method to `StreamlitRecorder` returning `StreamlitContext` rather than relying on the `__getattr__` fallback (which returns `None`, not a context manager).
- *Cross-page assertion fragility:* `[item] = list_of_calls` style unpacking breaks the moment another agent adds a second of the same widget for unrelated UX. Prefer args-filtered selectors in shared/global page tests so navigation additions don't cause spurious failures.
Charlie (Tester) owns test strategy, acceptance criteria, and quality gates for the control plane. Highest-risk areas: auth, operator actions, backend integration failures, regression coverage.

**Key learnings from prior work:**
- Reusable Streamlit test pattern: monkeypatch st import with 	ests.support.streamlit_recorder.StreamlitRecorder
- Health probe selection critical: avoid mutating endpoints, use read-only or dedicated endpoints
- Team sign-off protocol: lead review, named reviser for issues, comprehensive re-review after fixes
- Acceptance criteria defined upfront enable fast iteration and clear gate definition
- Operator UX requires clear messaging for browser flows, tenant/scope, error recovery
- Auth testing must cover mode switching, secret masking, per-service health, pending-flow regression

**Archived work:** Issues #2–#7 (auth architecture, browser login, callback fix, tenant auth, redirect). Issue #8 (MSAL integration) and manual token scope completed 2026-05-05. See history-archive.md for full details.

## 2026-05-15T10:52:00Z: Bulk Load CSV Generation (#17) + Abort (#31) Test Coverage

**Status:** TESTS WRITTEN — 34 new tests added, 39/43 pass, 4 abort tests expected-fail (implementation pending Judson).

**What was added:**
- `tests/test_bulk_load_page.py`: 34 new tests across 7 test classes covering the CSV-generation tab workflow and abort-button behavior.
- `tests/support/streamlit_recorder.py`: Added `tabs()`, `progress()`, and `StreamlitProgressMock` to support the new page primitives (`st.tabs`, `st.progress` with `.progress()` updates).

**CSV generation tab coverage (Issue #17) — 28 tests, all passing:**
- `TestCSVGenerationKindPicker` (2): kind selectbox populated from `list_schema_kinds`; empty-schemas warning.
- `TestCSVUpload` (2): CSV bytes stored in `GEN_CSV_DATA_KEY`; new CSV resets downstream mapping/manifests.
- `TestCSVAutoMap` (4): auto_map called when kind+CSV present; skipped when no CSV or no kind; cached mapping skips re-call.
- `TestCSVMappingOverride` (5): selectbox per schema field; (unmapped)+headers in options; confidence % indicator; low-confidence warning; unmapped-required-fields warning.
- `TestCSVGenerate` (4): disabled without mappings; disabled without legal tag; disabled without ACL owners; calls `generate_manifests` with confirmed mappings on click.
- `TestCSVManifestPreview` (3): count summary after generate; Submit button renders when manifests exist; Submit disabled when no manifests.
- `TestCSVSubmit` (3): calls `submit_manifest` per manifest; stores results in session state; progress bar updates during loop.
- `TestCSVErrorHandling` (5): empty CSV parse error; SchemaNotFoundError; generate failure sticky error; submit_manifest failure captured; results section renders mixed summary.

**Abort button coverage (Issue #31) — 6 tests, 4 expected-fail:**
- `TestAbortRegisteredDatasets` (3): flag stops loop early; partial results preserved; abort message displayed. First and third FAIL (loop runs to completion — no abort check implemented yet). Second passes (partial results trivially preserved since loop completes).
- `TestAbortCSVGeneration` (3): flag stops CSV submit early; partial results preserved; abort message displayed. Same pattern — first and third FAIL.

**Key session-state keys tested against (locked by page module):**
- `gen_kind`, `gen_csv_data`, `gen_mapping_result`, `gen_confirmed_mappings`, `gen_manifests`, `gen_submit_results`, `gen_legal_tag`, `gen_acl_owners`, `gen_acl_viewers`, `gen_last_error`
- Abort keys (proposed for Judson): `bulk_abort_requested`, `gen_abort_requested`

## Learnings

- **`or` vs `is not None` for falsy defaults in test helpers.** `kinds or _DEFAULT` evaluates `[]` as falsy and returns the default. When a test explicitly passes `kinds=[]`, use `kinds if kinds is not None else _DEFAULT`. Caught this causing test_no_schemas_shows_warning to pass wrong data.
- **Recorder `selectbox` reads `widget_values[label]`, not `session_state[key]`.** Page widgets with `key=...` in real Streamlit sync session_state ↔ widget value automatically. In the recorder they are decoupled. Tests MUST set both `session_state[key]` (for the page's session-state reads) and `widget_values[label]` (for the recorder's selectbox return value). The `_setup_csv_tab_session` helper now does both.
- **`st.tabs` and `st.progress` needed explicit recorder support.** The `__getattr__` fallback returns `None`, which breaks `tab_a, tab_b = st.tabs(...)` (can't unpack None) and `bar = st.progress(0); bar.progress(0.5)` (AttributeError on None). Added `tabs()` → list of `StreamlitContext`, and `progress()` → `StreamlitProgressMock` with `.progress()` updates.
- **Abort tests are intentionally red gates.** Writing tests before implementation (test-first for #31) makes the acceptance criteria concrete and machine-verifiable. When Judson wires up the abort check, these 4 tests turn green with zero reviewer effort.



**Status:** PLANNING COMPLETE, SYNTHESIZED WITH TEAM

**Acceptance criteria A1–A8 locked and ready for implementation review:**

- **A1:** Storage configuration & mode switching (SQLite default .adme_dev.db, PostgreSQL via DATABASE_URL, unambiguous mode, clear startup log)
- **A2:** Session ↔ persistent storage sync (connection persists, auth NOT persisted, health time-scoped, secrets NEVER)
- **A3:** Migration safety & backward compatibility (version-controlled schema, fresh-install initialization, identical Postgres/SQLite schemas, pre-persistent-storage migration)
- **A4:** Secret handling & sensitive data (no logging of secrets, masked UI, env-only DATABASE_URL, .gitignore enforcement)
- **A5:** Failure states & recovery (connection failure graceful, corrupt DB detected, transaction rollback, clear state handling)
- **A6:** Streamlit reruns & concurrent access (no data race, no per-interaction re-read, session/storage separation clear in code)
- **A7:** CI/CD feasibility (tests without external Postgres, migrations tested in CI, optional Postgres developer path, no environment branches)
- **A8:** Data integrity & constraints (NOT NULL/UNIQUE where needed, stable primary keys, UTC timestamps)

**Test phases ready to execute:**
1. **Unit tests (Phase 1):** Schema/migration, connection persistence, health results, secret handling, failure recovery, concurrent access
2. **Integration tests (Phase 2):** Settings → DB → Welcome flow, auth method switching, health persistence, backward compatibility
3. **System/acceptance tests (Phase 3):** Full pytest with coverage, ruff, mypy, manual dev and Postgres paths
4. **Operational tests (Phase 4):** Data survives restart, Postgres path documented, no secret leakage

**Critical review gates defined:**
- [ ] SQLAlchemy ORM abstraction only (no raw SQL)
- [ ] grep -r "client_secret" app/storage/ returns nothing
- [ ] Schema audit: correct PKs, constraints, no orphaned data
- [ ] Transaction audit: all writes atomic
- [ ] CI/CD audit: no external service deps
- [ ] Error handling audit: all DB errors caught with user-friendly messages

**Known risks & mitigations:**
1. Streamlit session ↔ DB sync timing → lock mechanism or read-once-per-session
2. SQLite vs Postgres behavior → SQLAlchemy + matrix tests
3. Client secret leakage → validator wrapping, log filtering, fresh review
4. Operator confusion (DB vs session) → clear UI labels, integration test proof

**Role confirmation:**
- Satya: review all phases, arbitrate conflicts
- Kevin: Phase 1 implementation (storage layer)
- Judson: Phase 2 implementation (UI persistence)
- Scott: Phase 3 implementation (deployment, secrets plumbing)
- Charlie: Phase 4 gating (acceptance criteria verification)

**Ready to gate implementation:** All A1–A8 criteria and test suites committed to decisions.md. Team sign-off required before coding begins.

## 2026-05-05T20:00:00.287+02:00: Persistent Storage Verification Implementation

- Added storage bridge tests that prove persisted connection and health state can
  hydrate Settings and Welcome flows without operator re-entry of non-secret
  fields, while keeping client secrets out of storage-bound calls.
- Added concrete `app.storage` contract tests for SQLite default/redaction,
  migration initialization, non-secret profile round-trip, active profile
  restart survival, health result timestamp retrieval, and rollback under
  injected health-result write failure.
- Concrete `app.storage` appeared during the run, so the acceptance tests were
  adapted to its SQLAlchemy repository classes and UI bridge.
- Validation: `python -m pytest --no-cov -q` passed with 101 passed and 1
  skipped; configured `python -m pytest`, Ruff, and mypy also passed.

## 2026-05-06T06:44:31.579Z: PR #9 Storage Alternative Comparison

**Verdict:** Local implementation satisfies all 8 acceptance criteria. PR #9 covers profile persistence only; misses PostgreSQL, migrations, health persistence, and failure-mode testing.

**Acceptance criteria verification:**
1. ✓ SQLite default at `.adme/adme.db`
2. ✓ PostgreSQL via `DATABASE_URL`
3. ✓ No PGlite
4. ✓ SQLAlchemy/Alembic boundary under `app/storage`
5. ✓ No persisted secrets
6. ✓ SQLite auto-migrates; PostgreSQL revision check
7. ✓ Profile/health hydration in Streamlit pages
8. ✓ Test coverage for migration, round-trip, secret rejection, health atomicity

**PR #9 gaps:** Profile persistence only; missing PostgreSQL production validation, migration verification, health persistence and atomicity, and failure-mode testing.


### 2026-05-06 Entitlements 405 fix — APPROVE

**Files added/edited:**
- `tests/test_token_utils.py` (new, 17 tests): hand-crafted JWTs for `extract_object_id`. Happy path with `oid` claim, parametrized realistic UUIDs, padding edge cases (0/1/2 padding chars, byte-length asserted in-test), missing `oid`, empty token, single-segment, invalid base64, valid base64 + non-JSON payload, non-dict JSON payload, non-string `oid` (int/float/bool/None/list/dict — all collapse to `None` because the helper requires `isinstance(oid, str) and oid`), empty-string `oid`.
- `tests/test_entitlements_service.py` (rewrite, 27 tests): deleted all `fetch_member_self` tests; added `fetch_my_groups` mirror suite (happy path with ADME-shaped `{desId, memberEmail, groups}` payload, 401/403/500, timeout, `ConnectionError`, headers/timeout/allow_redirects, URL building with quoted OID + `?type=none`, special-char OID escapes `a+b/c d` to `a%2Bb%2Fc%20d`, trailing-slash endpoint stripping, empty-token + empty-OID + invalid-connection `ValueError`s); kept all `fetch_groups` tests + correlation-id case-insensitive parametrize.
- `tests/test_entitlements_page.py` (rewrite, 14 tests): deleted `fetch_member_self` tests; added no-OID preflight (HTTP not fired, `st.error` mentions Object ID, `page_link` to Settings), auto-run with `extract_object_id` called once and OID forwarded to `fetch_my_groups`, identity card renders `desId`/`memberEmail` from response, `Groups you belong to (N)` count header, empty-groups friendly admin info message, all-groups expander exists once + `expanded=False` + does NOT block `fetch_groups`, Re-run button bypasses guard, history has 2 entries with labels `members.{oid}.groups` and `groups`, error path surfaces message+status+correlation_id and suppresses the `Authenticated as` identity success.
- `tests/test_entitlements_page.py` reuses Judson's existing `StreamlitRecorder.expander`; no recorder extension needed.

**Validation:**
- Targeted: `python -m pytest -q tests/test_token_utils.py tests/test_entitlements_service.py tests/test_entitlements_page.py` -> **68 passed** in 10.38s.
- Full: `python -m pytest -q` -> **183 passed** in 10.37s, **87% total coverage**. `token_utils.py` 100%, `entitlements.py` 86%, `2_🔑_Entitlements.py` 90%.

**Verdict: APPROVE.** Kevin's URL is correct (`/api/entitlements/v2/members/{quoted_oid}/groups?type=none`, `urllib.parse.quote(safe="")`, Bearer + `data-partition-id` + JSON Accept, 5s timeout, no redirects, `ValueError` on empty OID). Judson's page hits every contract bullet: preflight OID guard with no HTTP fired, identity-from-my-groups, count header, friendly admin message on empty groups, secondary all-groups expander collapsed by default but not gating the call, Re-run bypass, 2-entry history with correct labels, error path with no identity card.

**Non-blocking note (do not block, log for future contract alignment):**
- Satya's contract specified `MY_GROUPS_ENDPOINT_LABEL = "members.{oid}.groups"` as a *constant* with literal `{oid}` placeholder, justified by "we want a stable history label that does **not** leak per-user OIDs into chart axes / session history." Kevin shipped a per-call interpolated label `f"members.{object_id}.groups"` with no constant exported. Judson's page mitigates the chart-axis leak with a regex collapse to `"my groups"`, but the history *table* still surfaces the raw OID. Operator-private data on operator-private machine, so impact is bounded — but flag for contract alignment if/when this label is consumed by a multi-tenant view.

**Lessons:**
- *JWT padding tests must assert their own preconditions.* Crafting a payload that exercises 1- or 2-char padding requires the JSON byte length to be `len % 3 == 2` or `== 1` respectively. Hand-counting fails silently if the payload happens to land on a 0-padding boundary; the test still passes but no longer covers the padding branch. Pattern: `assert len(json_bytes) % 3 == <expected>` inside the test, immediately before the encode call.
- *Reviewer rejection didn't fire here, but the `{oid}`-constant deviation is a textbook case where strict-vs-flexible interpretation of "label" matters.* When a contract specifies a constant *symbol* (not just a value), shipping an inline f-string is a contract breach even when the rendered string at runtime can be adapted to. Future Satya contracts that say "constant" should be treated as a structural test target (`from app.services.entitlements import MY_GROUPS_ENDPOINT_LABEL`).

### 2026-05-06 Ingestion MVP test pass — APPROVE

**Files added:**
- `tests/test_osdu_models.py` (35 tests): every documented `parse_workflow_status` mapping (case-insensitive, whitespace-tolerant), `None` and blank → UNKNOWN, garbage → UNKNOWN, `WorkflowStatus` enum membership + StrEnum value semantics, frozen-dataclass smoke tests for `WorkflowRunResult`/`LegalTagCheckResult`/`SearchResult` (construction with required fields, `FrozenInstanceError` on mutation, per-instance `records` default-factory).
- `tests/test_ingestion_service.py` (73 tests): `validate_manifest_json` table (TNO sample after substitution, blank/invalid JSON, top-level array, missing executionContext, missing manifest, no entity arrays, non-list section, item missing kind, non-string kind); `substitute_manifest_placeholders` happy + parametrized blank-input rejections + unresolved `{{` guard; `check_legal_tag` happy + curated 404 (tag-name + partition + "not found") + 401/403/500 + timeout + ConnectionError + headers (Bearer, data-partition-id, JSON Accept, no Content-Type for GET) + correlation-id case-insensitive parametrize + URL-encoded special chars + blank-name/blank-token/invalid-connection ValueErrors; `submit_manifest` happy (`runId`+`workflowId`+`status`→IN_PROGRESS) + 2xx-without-runId failure + 400/401/403/500 + timeout + ConnectionError + headers (Bearer, data-partition-id, JSON Accept, JSON Content-Type) + POST body equals manifest_payload exactly + blank-token + invalid-payload (empty dict / non-dict / None) ValueErrors; `get_workflow_status` parametrized parse-each-status table + URL template + 401/404/500 + timeout + blank-run_id/blank-token ValueErrors.
