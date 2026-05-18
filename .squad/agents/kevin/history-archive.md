
<!-- Archived 2026-05-18T20:00:00Z by Scribe (history summarization) -->

# Project Context

- **Owner:** Eirik Haughom
- **Project:** Streamlit control plane app for Azure Data Manager for Energy (ADME)
- **Stack:** Python, Streamlit, Azure, ADME/OSDU APIs
- **Created:** 2026-04-24

## Learnings

- 2026-05-15: Issue #26 — Added `search_with_aggregation()` and `build_multi_kind_query()` to `app/services/search.py`. `search_with_aggregation` posts to `/api/search/v2/query` with optional `aggregateBy` field in the body and returns `SearchAggregationResult` (new model in `app/models/osdu.py`) including parsed `AggregationBucket` entries. `build_multi_kind_query` converts a list of kinds into an effective kind + Lucene query pair — single kind passes through, multiple kinds use wildcard kind with `(kind:"..." OR kind:"...")` filter. `_parse_aggregation_buckets` internal helper handles malformed entries defensively. Existing `search_records` and all 112 prior tests untouched — all 146 tests pass.
- Kevin owns backend services, ADME integrations, and data-handling logic for the control plane.
- Reliability and explicit error handling matter because the app coordinates platform operations, not just passive reporting.
- `app/models/connection.py` is the shared ADME contract; backend auth and health services should consume `ADMEConnection`, `ServiceHealthResult`, and `OSDU_SERVICES` without redefining them.
- Judson's backend-facing API for issue #2 is intentionally thin: `app/services/auth.py:get_token(connection)` and `app/services/health.py:check_all(connection, token)`.
- Health validation should preserve `OSDU_SERVICES` order, classify non-2xx HTTP responses as `unhealthy`, and classify transport failures like timeouts as `error`.
- Health probes must send both `Authorization` and `data-partition-id`, and tests should keep `tests/conftest.py` fixtures aligned with that required input.
- EDS belongs in the issue #2 service matrix and should use the explicit readiness health endpoint (`/api/eds/v1/health/readiness_check`) with `GET`, not the business `retrievalInstructions` API.
- Indexer health validation must use the non-mutating readiness endpoint (`/api/indexer/v2/readiness_check`); `reindex` is an operational action, not a safe probe contract.
- 2026-05-05T14:11:09.427+02:00: Issue #8 auth service now exposes MSAL user-flow helpers that return a redacted pending-flow wrapper and a session-scoped user auth state; user tokens are supplied back to `get_token()` explicitly and service-principal auth remains on `ClientSecretCredential`.
- 2026-05-05T15:11:17.396+02:00: `ADMEConnection.token_scope` is static configuration; `connection.scope` trims it and falls back to the ADME default when blank so blank UI input does not invalidate otherwise valid connections.
- 2026-05-05T15:11:17.396+02:00: Token scope Settings guidance was mechanically wrapped to satisfy Ruff E501 without changing copy semantics or backend auth behavior.
- 2026-05-05: Added `app/services/settings_store.py` — stdlib-only sqlite3 store at `~/.adme-ingestion-tool/settings.db` (override via `ADME_SETTINGS_DB`). Schema matches Satya's spec: `connections` table keyed on operator-supplied `name`, partial unique index on `is_active = 1` enforces at-most-one-active. `client_secret` is dropped on every write — never persisted. Activation switches run inside `BEGIN IMMEDIATE` so the partial unique index stays honest.
- 2026-05-05: All settings_store public functions self-initialize (call `initialize_store` first) so callers don't have to remember ordering. Errors are raised as `SettingsStoreError` with operator-safe messages; raw sqlite3 exceptions are logged at error level but never leaked. `set_active_connection` raises if the name is unknown — this is enforcement, not a no-op.
- 2026-05-05: Wired `app/connection_state.py`: `ensure_session_defaults` now hydrates `CONNECTION_KEY` from the active row when the session is empty (best-effort: hydration failures are swallowed because the form can always re-collect input). `save_connection` gained an optional `name="default"` and now also writes through to the store and sets active. Added `forget_saved_connection` helper for the deferred picker UI.
- 2026-05-05: Disk persistence is additive only. The in-memory contract is untouched: `get_connection`, `clear_user_auth_state`, and the auth/health helpers still behave exactly as before. Auth material (tokens, MSAL pending flows, user auth state) is explicitly out of the disk store and remains session-only.
- 2026-05-05T19:48:42.932+02:00: Persistent storage planning keeps PGlite out of backend scope for this Python/Streamlit app; dev should default to SQLite through SQLAlchemy, while production uses an operator-supplied PostgreSQL database through a single database URL contract.
- 2026-05-05T19:48:42.932+02:00: First persisted aggregates should be non-secret ADME connection profiles, an active-profile pointer, and latest health-check runs/results; client secrets, MSAL pending flows, user tokens, and auth caches remain Streamlit-session or external-secret concerns.
- 2026-05-05T19:48:42.932+02:00: Proposed backend storage modules are `app\storage\config.py`, `engine.py`, `models.py`, `session.py`, `repositories\connection_profiles.py`, and `repositories\health_runs.py`, with Alembic metadata sourced from the storage models.
- 2026-05-05T20:00:00.287+02:00: Implemented `app\storage\` with SQLAlchemy 2.x repositories, Alembic migrations, SQLite auto-migration, PostgreSQL revision checks, URL redaction, and explicit rejection of secret-bearing connection profiles.
- 2026-05-05T20:00:00.287+02:00: Alembic's required `app.storage.migrations` package owns migration helpers because Python cannot expose both an importable `app.storage.migrations` module and a same-named migrations package.
- 2026-05-06T06:44:31.579Z: Reviewed PR #9 alternative storage implementation; local version provides clear SQLAlchemy/Alembic boundary, PostgreSQL production path, strong secret rejection/redaction, and complete profile+health model. Recommended STICK WITH LOCAL and close PR #9. Cherry-pick test DB override and raw secret checks if beneficial.

## 2026-04-24 Issue #2 Contract Corrections (Revision Batch)
- Fixed Indexer probe contract: removed mutating GET /api/indexer/v2/reindex, replaced with read-only GET /api/indexer/v2/readiness_check
- Finalized EDS probe: confirmed GET /api/eds/v1/health/readiness_check (dedicated health endpoint), rejected POST /api/eds/v1/retrievalInstructions (business API, false negatives)
- Established health status semantics: healthy (2xx), unhealthy (non-2xx with code/detail), error (transport/timeout, no status_code)
- No redirect following (redirects hide auth/gateway misconfig)
- Deterministic result ordering per OSDU_SERVICES list for UI/test matrix rendering
- All tests updated and re-run against corrected contracts
- Issue #2 updated with real current status
- Ready for Judson's UI integration

## 2026-04-24 Issue #2 Implementation Complete
- Implemented app/services/auth.py: get_token(connection) function with DeviceCodeCredential (user impersonation) and ClientSecretCredential (service principal) flows
- Implemented app/services/health.py: check_all(connection, token) with concurrent ThreadPoolExecutor probes, 5s timeout per service, explicit healthy/unhealthy/error semantics
- Probes consume OSDU_SERVICES canonical list, return results in deterministic order (enables matrix UI and test assertions)
- Includes corrected Indexer probe (GET /api/indexer/v2/readiness_check) and EDS probe (GET /api/eds/v1/health/readiness_check)
- No redirect following (prevents hiding auth/gateway misconfig)
- Error handling preserves meaningful messages without leaking secrets
- Backend tests validate auth flows, health probes, timeouts, partial failures, deterministic ordering
- Tests locked to readiness endpoints prevent reversion to mutating paths
- All backend tests passing, integrated with Judson's UI pages

## 2026-04-24 Issue #4 Backend Implementation Complete
- Replaced DeviceCodeCredential with InteractiveBrowserCredential in app/services/auth.py
- _build_credential() now calls: InteractiveBrowserCredential(client_id=..., tenant_id=...)
- Removed _device_code_prompt_callback() function (no longer needed)
- Updated error messages to reference 'interactive login' or 'browser authentication' instead of device codes
- Updated type annotations: DeviceCodeCredential | ClientSecretCredential → InteractiveBrowserCredential | ClientSecretCredential
- Service-principal auth unchanged (continues using ClientSecretCredential)
- Credential cleanup pattern (_close_credential()) preserved
- Error handling strategy: CredentialUnavailableError, ClientAuthenticationError, AzureError all provide "Run Test Connection again" guidance
- Updated tests: test_auth.py and test_auth_service.py monkeypatch InteractiveBrowserCredential, removed callback assertions
- All validation clean: pytest, ruff, mypy passing; no regressions in service-principal tests

## 2026-04-25 Issue #5 Interactive Auth Callback Fix Implementation
- Root cause: InteractiveBrowserCredential was passing ADME confidential-client app ID; Azure AD rejected post-callback token exchange with AADSTS7000218 because confidential clients require client_secret which public-client flows don't send
- Fixed by: Using Azure CLI well-known public client ID (`04b07795-a710-4f9e-9640-a91e60e60e08`) for credential instantiation while preserving `connection.client_id` for scope derivation
- Why it works: Azure CLI's public client is trusted by all Azure AD tenants; token's audience determined by scope, not client ID
- Changes: Added AZURE_CLI_PUBLIC_CLIENT_ID constant, updated _build_credential() USER_IMPERSONATION path, left service-principal path unchanged
- Tests: Updated test_auth.py and test_auth_service.py assertions, added AADSTS7000218 regression test, added callback success integration test
- Validation: All tests passing (18/18), ruff clean, mypy strict passing, no regressions, code coverage 93% (exceeds >=90% gate)
- Status: Implementation complete, approved for merge

## 2026-04-25 Issue #6 Tenant-Compatible Interactive Auth Implementation
- Root cause: Azure CLI public client ID is blocked in some enterprise tenants (IPS-Energy) due to consent policies or allowlists
- Solution: Removed hardcoded AZURE_CLI_PUBLIC_CLIENT_ID; InteractiveBrowserCredential now uses `connection.client_id` (customer's own app registration)
- Scope fix: Updated `ADMEConnection.scope` property to return hardcoded `https://energy.azure.com/.default` (constant across all ADME instances)
- Why it works: Customer's configured app is guaranteed to exist in their tenant and be authorized; hardcoded scope is resource-based (ADME's identity), not client-based
- Changes made:
  - `app/services/auth.py`: Removed AZURE_CLI_PUBLIC_CLIENT_ID constant; InteractiveBrowserCredential instantiation now uses connection.client_id
  - `app/models/connection.py`: scope property now returns hardcoded constant instead of deriving from client_id
  - Tests: Updated scope assertions in test_auth.py and test_auth_service.py; added test_interactive_uses_connection_client_id; added test_scope_is_hardcoded_adme_resource
- Validation: All tests passing (24/24), ruff clean, mypy strict passing, no regressions
- Service principal: Unchanged logic, uses same hardcoded scope
- Status: Implementation complete, approved for merge

## 2026-04-25 Issue #7 Auth Redirect Implementation
- Root cause: InteractiveBrowserCredential starts ephemeral HTTP server on localhost:8400 to capture OAuth code. SDK must receive code on that server; cannot redirect to Streamlit (8501) without breaking token exchange.
- Solution: Pass explicit `redirect_uri="http://localhost:8400"` to InteractiveBrowserCredential in app/services/auth.py
- Why explicit parameter: Makes port deterministic (no SDK-version drift), self-documents intended behavior, ensures alignment with app registration
- Changes made:
  - Added INTERACTIVE_BROWSER_REDIRECT_URI constant in app/services/auth.py
  - Updated _build_credential() USER_IMPERSONATION path to pass explicit redirect_uri parameter
  - Left ClientSecretCredential (service principal) behavior unchanged
  - Updated test assertions in test_auth.py and test_auth_service.py to verify redirect_uri parameter
- Validation: All tests passing (26/26), ruff clean, mypy clean, no regressions in service-principal tests
- Status: Implementation complete, approved for merge


## Issue #8 Auth Flow - Team Completion (2026-05-05)

**Status:** ✅ COMPLETE & VALIDATED

All team members successfully completed assigned work for MSAL auth integration:
- Satya: Lead review and final validation
- Kevin: Auth-service implementation
- Scott: Documentation and README updates
- Judson: Settings page integration
- Charlie: Quality gate and regression coverage

Final outcome: Full test suite passed (70), Ruff clean, mypy clean. Ready for merge.
## 2026-05-05: Manual Token Scope Configuration (Complete)

**Status:** COMPLETE
**Decision:** Manual token scope configuration merged to decisions.md
**Outcome:** ADMEConnection now includes token_scope field with ADME default fallback. Settings UI exposes non-secret Token scope field. Both auth paths (user and service principal) consume connection.scope. All validation passed: pytest 80, ruff, mypy.
## Learnings

### 2026-05-06 — client_secret persistence via OS keyring
- Lifted Satya's secret never persisted exclusion. SQLite schema unchanged; secret now lives in the OS keyring (Windows Credential Manager / macOS Keychain / Secret Service on Linux), keyed by `(KEYRING_SERVICE_NAME='adme-ingestion-tool', connection_name)`.
- Added `keyring>=25.0` to `requirements.txt` (runtime, not dev).
- New private helpers in `app/services/settings_store.py`: `_store_secret(name, secret)` and `_load_secret(name)`. Both lazy-import `keyring` so module import never fails on machines without the package.
- `_store_secret` raises `SettingsStoreError` on backend failure so save_connection can surface 'secret not persisted' to the operator. `_load_secret` swallows everything and returns None — hydration is best-effort.
- `save_connection`: DB row first, then `_store_secret` AFTER commit so rollback never orphans a secret. Empty secret → delete_password (PasswordDeleteError treated as valid no-op).
- `delete_connection`: keyring entry cleared BEFORE the DB delete (opposite ordering, same orphan-prevention). Keyring failure here is logged, not raised.
- `_row_to_connection` now hydrates `client_secret` via `_load_secret` — so list_connections, load_connection, and ensure_session_defaults all transparently restore the secret with no change to `app/connection_state.py`.

### 2026-05-06 — Test isolation gotcha
- Autouse `_isolate_settings_db` only covers SQLite. Without an autouse keyring fake, every existing test calling save_connection would hit the real Windows Credential Manager. Added autouse `_isolate_keyring` in `tests/conftest.py` that installs an in-memory fake keyring module into sys.modules.
- Three pre-existing tests asserted `client_secret == ''` after round-trip — encoded the old drop-on-save contract. Updated: defense-in-depth check (secret bytes absent from SQLite file) preserved; secret IS now expected back from the faked keyring.
- New file `tests/test_settings_store_keyring.py` covers: round-trip, empty-secret-deletes, no-entry-noop, delete clears both stores, backend exception -> None, missing package -> None, set failure -> SettingsStoreError + DB row preserved.

### Counts
- Targeted suite (settings_store + keyring + connection_state): 45 passed.
- Full suite: 115 passed (was 105; +10 new keyring tests).
## 2026-05-05 Entitlements service implementation (Mariel)
- Added EntitlementsCallResult (frozen dataclass) to app/models/connection.py alongside ServiceHealthResult; updated module docstring to mention entitlements as a co-tenant of the UI/backend contract.
- Created app/services/entitlements.py with fetch_member_self + fetch_groups, mirroring health.py: stdlib + requests, ENTITLEMENTS_TIMEOUT_SECONDS=5, allow_redirects=False, perf_counter latency rounded to 2dp, no internal retries.
- Both fetchers route through _call_entitlements which validates connection.is_valid() and a non-empty token (ValueError, matching health.check_all), strips trailing slash from connection.endpoint, sends Authorization + data-partition-id + Accept: application/json.
- Followed Mariel's task verbatim where it diverged from Satya's spec: members.self path is /api/entitlements/v2/members/me (not literal {me}); endpoint label is members.self (not members/{me}). The /members/me form matches the actual ADME entitlements contract.
- Correlation ID extraction: case-insensitive lookup over correlation-id, x-correlation-id, request-id, x-request-id; first hit wins; built a lowercase mapping rather than relying on requests' CaseInsensitiveDict so any mapping-like headers object works.
- Success path (2xx): parsed JSON dict goes into both data and raw_response. Non-dict JSON bodies degrade data to None but the call is still ok=True (defensive — entitlements always returns objects in practice).
- Error path (non-2xx): error_message picks message/detail/error/title/errors fields shape-tolerantly and truncates to 500 chars; raw_response is parsed JSON if available, else raw text, else None.
- Timeout returns ok=False, http_status=None, error_message='Request timed out after 5s'. Other RequestException returns 'TypeName: msg'. Defensive bare-Exception branch added for safety (pragma: no cover).
- ruff and mypy both clean. No new dependencies. Did NOT touch tests (Charlie owns) or any page (Judson owns).

## 2026-05-06 Entitlements 405 fix — my-groups + OID extraction (Mariel)
- New `app/services/token_utils.py` with `extract_object_id(token)`: stdlib-only base64url + json. Pads payload segment with `=` until len%%4==0, returns `payload.get('oid')`, swallows ValueError/binascii.Error/UnicodeDecodeError/IndexError to None. No signature verification — trust boundary is MSAL, not this helper. Module docstring is explicit about that.
- `app/services/entitlements.py`: deleted `fetch_member_self`, `MEMBERS_SELF_ENDPOINT_LABEL`, `MEMBERS_SELF_PATH`. The /members/me endpoint does not exist on ADME (returns 405); ripping it out at import time so any stale caller fails loudly (Charlie/Judson catch).
- Added `MY_GROUPS_PATH_TEMPLATE = '/api/entitlements/v2/members/{object_id}/groups'` and `fetch_my_groups(connection, token, object_id)`. URL-encodes the OID via `urllib.parse.quote(object_id, safe='')` — defensive even though OIDs are GUIDs — then appends literal `?type=none`. Reuses `_call_entitlements` verbatim (timeout, correlation-id extraction, error parsing).
- Per Mariel's explicit instruction the endpoint label is the f-string `f'members.{object_id}.groups'` (carries the actual OID). This diverges from Satya's note about a literal `{oid}` placeholder; followed Mariel because she's the requester and the task statement called it out specifically.
- `object_id` validation matches the existing `token` empty-check pattern: ValueError on empty/whitespace before any HTTP work. `fetch_groups` and `GROUPS_PATH` untouched.
- Tests and page rewire deliberately not touched — Charlie and Judson own those. ruff/mypy clean on both edited files.

## Learnings

### 2026-05-06: Ingestion MVP services landed
- Shipped three new modules per Satya's locked contract:
  - `app/models/osdu.py` (104 LOC): `WorkflowStatus` StrEnum, `parse_workflow_status` (case-insensitive, whitespace-trimmed; running/in progress/in_progress/submitted/queued -> IN_PROGRESS, finished/success/succeeded/completed -> FINISHED, failed/error -> FAILED, else UNKNOWN), and frozen dataclasses `WorkflowRunResult`, `LegalTagCheckResult`, `SearchResult`.
  - `app/services/ingestion.py` (~560 LOC): constants `LEGAL_TAGS_PATH`, `WORKFLOW_INGEST_RUN_PATH`, `WORKFLOW_RUN_STATUS_PATH_TEMPLATE`, `TNO_SAMPLE_MANIFEST` (Darryl's payload, placeholders preserved verbatim), `TNO_SAMPLE_DESCRIPTION`. Public functions: `substitute_manifest_placeholders` (pure; ValueError on blank inputs OR unresolved `{{...}}` after substitution), `validate_manifest_json` (pure; first-failure-wins rule chain), `check_legal_tag`, `submit_manifest`, `get_workflow_status`. Curated 404 message on legal tag check; `submit_manifest` 2xx-without-runId is surfaced as ok=False with a curated message. Internal `_call` shared by `_call_workflow`/`_call_legal` to keep the entitlements-pattern parity without importing across services.
  - `app/services/verification.py` (~250 LOC): `SEARCH_QUERY_PATH`, `DEFAULT_SEARCH_LIMIT=100`, `search_records_by_kind` POSTing exactly `{"kind": kind, "limit": limit, "offset": 0}`. Records list filtered to dict items only (defensive, mirrors entitlements `_extract_groups`). `totalCount` preferred over `len(results)`; falls back when missing.
- All three modules duplicate the entitlements helpers (`_elapsed_ms`, `_extract_correlation_id`, `_try_parse_json`, `_error_message_from_json`, `_truncate`) verbatim per Satya's "no shared module yet" call - refactor is a v2 follow-up.
- 5s timeout, `allow_redirects=False`, Bearer + data-partition-id + Accept JSON headers, Content-Type only on POST. ValueError on invalid connection / blank token / blank function-specific args. Transport failures (Timeout / RequestException / broad Exception) return ok=False with http_status=None and never raise.
- Quality gates green: ruff clean, mypy strict (disallow_untyped_defs) clean, round-trip sanity check (substitute -> validate on TNO sample) returns True.
- Did NOT modify entitlements.py or any of Judson's files. Did NOT touch `app/pages/3_??_Ingestion.py`.

### 2026-05-07: Legal tags service module landed
- Shipped `app/services/legal_tags.py` (~720 LOC) with all six public functions per Satya's locked contract: list/get/create/update/delete/get_legal_tag_properties. Internal `_call_legal` duplicates entitlements/ingestion HTTP wrapper verbatim (5s timeout, allow_redirects=False, Bearer + data-partition-id + Accept JSON, Content-Type only on POST/PUT). Did NOT extract shared `_http.py` — Satya's "duplication acceptable for v1" rule applied.
- Extended `app/models/osdu.py` with five new frozen+slots dataclasses: LegalTag, LegalTagPropertiesSpec, LegalTagListResult, LegalTagDetailResult, LegalTagOperationResult, LegalTagPropertiesResult. All result envelopes carry the standard ok/http_status/latency_ms/correlation_id/error_message/raw_response fields per Satya section 2.
- Refactored `app/services/ingestion.py`: `LEGAL_TAGS_PATH` now imported from `app.services.legal_tags` and re-exported via `__all__`. Identity check confirms `app.services.ingestion.LEGAL_TAGS_PATH is app.services.legal_tags.LEGAL_TAGS_PATH` — single source of truth wired.
- Three Satya-vs-Darryl divergences resolved per Darryl's verified-from-controller research, documented in `.squad/decisions/inbox/kevin-legal-tags-impl-notes.md`:
  (1) Properties path is `:properties` colon-form, not `/properties` slash-form.
  (2) Properties response: countries are dicts (alpha-2 -> name), exports use `exportClassificationControlNumbers` not `exportClassifications`. Parser accepts BOTH shapes; dicts surface as sorted keys.
  (3) Update body: kept Satya's nested `{name, description, properties}` shape (NOT Darryl's flat shape) because the page is being built against it in parallel; flagged 400-risk for Charlie to verify against a real cluster.
- LEGAL_TAG_VALIDATE_PATH constant exported but no validate function shipped (out of scope for this batch).
- delete_legal_tag returns curated 404 message matching ingestion's check_legal_tag pattern. get_legal_tag_properties returns spec=None + ok=False on 404 so the page can detect the fallback branch.
- create_legal_tag validates the seven required properties keys (countryOfOrigin/contractId/originator/dataType/securityClassification/personalData/exportClassification) before any HTTP work.
- Quality gates green: ruff clean, mypy strict clean, pytest -q tests/test_ingestion_service.py tests/test_osdu_models.py = 108 passed (no regressions).
- Did NOT touch Judson's `app/pages/4_🏷️_Legal_Tags.py` or any of Charlie's test files.
## 2026-05-05: Persistent Storage Backend Contract (Complete)

**Status:** PLANNING COMPLETE, SYNTHESIZED WITH TEAM

**Decision:** SQLAlchemy 2.x + Alembic for dev SQLite and production PostgreSQL. Single `DATABASE_URL` configuration knob (not split ADME_*_* variables).

**Key contract elements:**
- `app/storage/` package boundary: config, engine, session, models, repositories
- Repositories return domain dataclasses (ADMEConnection, HealthRunSummary), not ORM objects
- No ORM imports outside `app/storage/` — keeps existing contracts in `app/connection_state` and `app/models/connection` unaffected
- Strict: no client_secret or auth tokens in database (Charlie gates this)
- Portable types only: String, Text, Integer, Boolean, DateTime (no JSONB, ARRAY, Postgres-only defaults)
