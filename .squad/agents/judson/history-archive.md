
<!-- Archived 2026-05-18T20:00:00Z by Scribe (history summarization) -->

# Project Context

- **Owner:** Eirik Haughom
- **Project:** Streamlit control plane app for Azure Data Manager for Energy (ADME)
- **Stack:** Python, Streamlit, Azure, ADME/OSDU APIs
- **Created:** 2026-04-24

## Learnings

- Judson owns Streamlit pages, operator workflows, and user-facing control-plane behavior.
- The app should make ADME operations visible, actionable, and easy to navigate for operators.
- 2026-04-24T14:38:18.059+02:00: `app\main.py` is now the welcome page, `app\pages\1_⚙️_Settings.py` owns ADME connection setup, and both pages share state through `app\connection_state.py`.
- 2026-04-24T14:38:18.059+02:00: Operator-facing connection flow should save valid settings, clear stale health results when settings change, and keep `client_secret` only in Streamlit session state.
- 2026-04-24T14:38:18.059+02:00: ADME validation depends on `app\services\auth.py`, `app\services\health.py`, and the canonical `OSDU_SERVICES` list in `app\models\connection.py`, including the EDS probe.
- 2026-04-24T14:38:18.059+02:00: UI review gates for issue #2 are best protected with page tests that assert the exact field contract, auth-method field gating, masked `client_secret`, and matrix rendering for degraded service results.
- 2026-05-05T14:11:09.427+02:00: Issue #8 Settings wiring keeps `ADMEConnection` static while storing pending MSAL flows and completed user auth state in explicit Streamlit session keys; callbacks are consumed once, query params are cleared, and user sign-out/auth changes clear stale health.
- 2026-05-05T15:11:17.396+02:00: Settings now exposes non-secret `Token scope` configuration, defaults it to the ADME scope, stores trimmed operator input, and treats scope-only changes as auth/health-stale connection changes.

## 2026-04-24 Issue #2 Implementation Complete
- Implemented welcome page in app/main.py (landing, connection status summary)
- Implemented settings page in app/pages/1_⚙️_Settings.py (form, health validation button, matrix rendering)
- Session-state connection UX: saves valid settings, clears stale health on config change, keeps client_secret session-scoped only
- Conditional client_secret field visibility based on auth_method (DeviceCodeCredential vs ClientSecretCredential)
- Service-by-service health matrix rendering (deterministic OSDU_SERVICES order)
- UI tests using Streamlit recorder pattern, auth-mode field gating tests, health matrix rendering tests
- Integrated with Kevin's auth.py:get_token() and health.py:check_all() services
- All acceptance criteria met, ready for Charlie's final review

## 2026-04-24 Issue #3 Streamlit Import-Path Fix Complete
- Fixed multipage import-path failure: Streamlit executes page scripts from their directory, omitting repository root from sys.path
- Solution: Prepend repository root to sys.path at top of app/main.py and app/pages/1_⚙️_Settings.py before local imports
- Minimal 4-line bootstrap, idempotent (checks before inserting), keeps existing app/ structure intact
- Absolute app.* imports remain unchanged (no style shift)
- Added tests/test_streamlit_import_paths.py: subprocess-based regression tests simulating Streamlit-style script loading for both entry point and pages
- Verified all app.* imports resolve correctly in isolated subprocess environment
- Prevents silent reversion to failing state
- All validation clean: pytest passing, ruff clean, mypy clean
- Issue #3 updated with real implementation status

## 2026-04-24 Issue #4 UI Implementation Complete
- Updated app/pages/1_⚙️_Settings.py help text: 'A browser window will open during connection test for you to sign in.'
- Removed all device-code references from UI
- Test connection flow: browser opens automatically, user signs in via standard Entra ID, control returns to app
- Success messaging: 'All X configured OSDU services responded successfully.'
- Failure messaging: 'Interactive login was cancelled. Please run Test Connection again.' (browser closed)
- Error handling: Clear messages for auth denial, network errors, headless environments
- All failure states end with consistent call-to-action: 'Run Test Connection again to retry.'
- Service principal flow unchanged
- Updated tests/test_settings_page.py with browser-workflow tests
- Added README.md operator note documenting interactive login flow
- All UI tests passing, no regressions in service-principal tests

## 2026-04-25 Issue #7 UI Guidance Update
- Root cause: Users didn't understand that new browser tab opens for interactive auth; unclear whether/where to return after sign-in
- Solution: Updated Settings page guidance text in app/pages/1_⚙️_Settings.py to explain multi-tab behavior clearly
- Changes made:
  - Updated USER_IMPERSONATION_GUIDANCE: "A new browser tab will open for Azure AD sign-in. After you complete sign-in, close that tab and return here to see the results."
  - Updated USER_IMPERSONATION_REFRESH_GUIDANCE: Same pattern for token refresh scenario
  - Explains new tab opens (sets expectation)
  - Instructs return to Streamlit after closing tab (provides clear next action)
  - Avoids technical details (localhost:8400 is implementation detail for developers)
- Why this helps: Users understand entire flow; no confusion about where results appear
- Test updates: Added UI text assertions in tests/test_settings_page.py verifying guidance contains "new browser tab" and "return here"
- Status: UI guidance implementation complete, approved for merge


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
### 2026-05-05T09:55:00Z - Cross-agent: settings persistence available
**From Scribe (team update):** Kevin shipped `app/services/settings_store.py`
backed by stdlib SQLite at `~/.adme-ingestion-tool/settings.db` (override:
`ADME_SETTINGS_DB`). `app.connection_state.ensure_session_defaults()` now
hydrates from the active stored row, and `save_connection()` writes through
+ marks active. New helper: `forget_saved_connection()`.

For the Settings page: the existing save flow already persists once
`save_connection` delegation is in place — no new page-level wiring needed
for the v1 single-slot UX. `client_secret` is intentionally NOT persisted;
service-principal operators re-enter it per session. Auth material remains
session-only and must NOT be written through this store.

If/when a "Saved connections" picker is added, the store API already
exposes `list_connections()` and `set_active_connection(name)` — no schema
change required.

## 2026-05-05 Entitlements page implementation (Mariel)
- Built `app/pages/2_🔑_Entitlements.py` against Kevin's actual `app/services/entitlements.py` contract (members.self label + literal /me path), not Satya's earlier sketch. Logged the divergence to `.squad/decisions/inbox/judson-entitlements-mismatch.md` per Mariel's instruction.
- Page session keys (page-scoped, no connection_state changes): `entitlements_history` (append-only list of dicts), `entitlements_autorun_done` (bool guard), `entitlements_last_member` and `entitlements_last_groups` (last EntitlementsCallResult so reruns triggered by clear-history don't blank the cards).
- Auto-run-once: `should_run = rerun_clicked or not session_state[AUTORUN_KEY]`. Re-run button bypasses the guard; we set the guard True after the first successful run so subsequent Streamlit reruns (e.g., clear-history button click) do NOT re-fire calls. Re-run does NOT clear history — it appends two more entries (one per endpoint).
- Pre-flight guard: render st.info + st.page_link to Settings when the connection is missing/invalid, OR when auth_method=USER_IMPERSONATION and no user_auth_state is present. Service-principal connections proceed straight to `get_token(connection)`.
- Token acquisition mirrors Settings page's `_get_token_for_connection` exactly. AuthenticationError and bare Exception both degrade to a friendly error + Settings link; no raw library errors leak.
- Latency chart uses `df.pivot_table(index=timestamp, columns=endpoint, values=latency_ms, aggfunc='last')` so st.line_chart renders one colored line per endpoint without manually reshaping. History table is reversed (newest first), capped at 20 rows, with latency formatted to 1 decimal.
- Groups table is defensive: extracts `data["groups"]` only when it's a list of dicts; projects each group to (name, email, description) with `""` fallback for missing fields. Empty groups list shows a caption, not an error.
- Identity label probes `email`, `desId`, `memberEmail`, `name`, `userPrincipalName` in order — ADME's `/members/me` typically returns `desId` not `email`.
- Error block renders message + status (or 'no HTTP response') + correlation ID (or 'no correlation ID') + expander with raw_response (st.code for text bodies, st.json for dicts). Treats both `error_message=None` and `error_message=""` as success-path defensively.
- Clear-history button ALSO clears `entitlements_last_member` and `entitlements_last_groups` so the cards reset to 'Run the entitlements test to see results.' caption — keeps page state coherent.
- Updated `app/main.py` with a second st.page_link for the new Entitlements page (icon='🔑').
- Added `"app/pages/2_🔑_Entitlements.py" = ["N999"]` to ruff per-file-ignores in pyproject.toml (matches the existing Settings exemption — Streamlit page filenames intentionally include emoji and digit prefixes).
- pandas import needed `# type: ignore[import-untyped]` (no pandas-stubs installed; matches the requests pattern). ruff and mypy both clean.
- Did NOT touch tests (Charlie owns), did NOT modify Kevin's service, did NOT modify connection_state.py (Mariel's spec is page-scoped state only — Satya's wiring of clear_entitlements_history into connection_state hooks is deferred since Mariel's binding UX rules don't require it).

## 2026-05-06 Entitlements 405 fix - page rewire (Mariel)
- Imports: dropped `fetch_member_self`; added `fetch_my_groups` and `app.services.token_utils.extract_object_id`. No changes to Kevin's modules.
- Session keys renamed for clarity: `LAST_MEMBER_KEY` -> `LAST_MY_GROUPS_KEY` (`entitlements_last_my_groups`). The my-groups response is now BOTH the identity source and the primary groups source — single result, two cards.
- Pre-flight chain unchanged at top (no connection / no token-for-user-auth banners stay as-is). NEW pre-flight is post-token: after `_acquire_token` succeeds, call `extract_object_id(token)`; if None, render `st.error` ("Could not read your Object ID from the access token. Sign out and sign back in on the Settings page.") + Settings page_link, set the autorun guard True so we don't loop, and skip both HTTP calls. No history append for this branch — keeps the chart clean for operators who just need to re-sign in.
- `_run_entitlements_calls` now takes `object_id` and calls `fetch_my_groups(connection, token, object_id)` first, then `fetch_groups(connection, token)`. Both append to history, so the operator still gets 2 entries per run.
- Identity card derives `desId` and `memberEmail` directly from the my-groups response `data` dict (per Satya's call: the my-groups response carries identity in the same payload — no second HTTP call). Display: `st.success("Authenticated as desId={x}, member email = {y}")` with '(unknown)' fallback per field, plus "Raw identity response (full payload)" expander showing `raw_response`. Removed the old `_identity_label` helper (probed email/desId/memberEmail/name/userPrincipalName) — no longer needed since we render desId and memberEmail explicitly.
- My-groups primary card: header `f"🔐 Groups you belong to ({N})"`. Empty list shows the friendly admin-prompt note "You're not a member of any groups in this partition yet — ask an admin to add you." (st.info, not st.caption — operator action signal).
- Identity + my-groups share a single `EntitlementsCallResult`, so `_render_my_groups_card` early-returns when `result.ok` is False — avoids double-rendering the error block (identity card already shows it).
- All-groups demoted to secondary: wrapped in `st.expander("📚 All groups in this partition (admin view)", expanded=False)`. Same dataframe + raw-expander + error block patterns as before, just collapsed. `fetch_groups` is still called from the runner (not lazily on expander open) so latency history stays consistent.
- Chart legend: latency-chart pivot now keys on a derived `display_endpoint` column built via `frame["endpoint"].str.replace(r"^members\..*\.groups$", "my groups", regex=True)`. The history table still shows the raw endpoint (members.{oid}.groups) so operators retain the OID for diagnostics / correlation-id matching. Pattern is regex anchored end-to-end so it doesn't accidentally collapse a future `members.something.else` shape.
- Clear-history button now resets `LAST_MY_GROUPS_KEY` + `LAST_GROUPS_KEY`; old `LAST_MEMBER_KEY` references all gone.
- ruff + mypy clean. Did NOT touch tests (Charlie owns), did NOT touch services, did NOT touch connection_state.

## 2026-05-06 Ingestion page (issue: ingestion MVP page)
- Built `app/pages/3_📥_Ingestion.py` (818 lines) against Satya's locked contract and the user-prompt session-key list. Mirrors entitlements page sys.path bootstrap, _preflight_ok, _acquire_token, history-append idiom, latency line_chart, dataframe rendering, and Re-run/Clear-history primitives.
- Locked session keys (Charlie tests these): `ingestion_manifest_text`, `ingestion_legal_tag`, `ingestion_acl_owners`, `ingestion_acl_viewers`, `ingestion_run_id`, `ingestion_submit_started_at` (datetime UTC), `ingestion_kinds`, `ingestion_workflow_status` (WorkflowStatus | None), `ingestion_last_poll_at`, `ingestion_polling_active`, `ingestion_history`, `ingestion_verification_done`. Internal-only helper keys (`ingestion_last_workflow_result`, `ingestion_verification_results`, `ingestion_verification_retries`, `ingestion_last_legal_tag_result`, `ingestion_last_submit_result`, `ingestion_last_correlation_id`) are namespaced to avoid collisions but not part of the locked contract.
- Pre-flight chain (mirrors entitlements verbatim): no/invalid connection -> info + Settings page_link; USER_IMPERSONATION without UserAuthState -> info + page_link; missing data_partition_id -> info + page_link. `ADMEConnection.is_valid()` already enforces the partition, so the third check is defensive belt-and-suspenders.
- Submit pipeline runs inside an `st.status` container so the operator sees stage-by-stage progress: (1) validate JSON via `validate_manifest_json`; if any `{{` is present in the raw text, run `substitute_manifest_placeholders` then re-validate. (2) `check_legal_tag` with curated 404 hint. (3) `submit_manifest` with raw-response expander on failure. On success persist run_id + `ingestion_submit_started_at` (datetime UTC) + `ingestion_kinds` (extracted from `executionContext.manifest.{ReferenceData,MasterData,Data}`, dedup, order-preserved; manifest envelope's own top-level `kind` deliberately excluded) + `WorkflowStatus` from submit response, then `st.rerun()` to hand off to polling.
- Polling block is rendered every page render whenever `RUN_ID_KEY` and `SUBMIT_STARTED_AT_KEY` are populated. Cadence ladder (`_poll_sleep_seconds`): elapsed<30s -> 2s, <5min -> 5s, otherwise -> 10s. 30-min timeout synthesizes a `WorkflowStatus.FAILED` result, appends a `poll` history row with ok=False, clears polling_active. Manual `🔄 Refresh status now` button bypasses the sleep (still `st.rerun()`s but skips `time.sleep`).
- Status display shows: run_id (monospace), elapsed (mm:ss), status label, raw server status string (caption), last correlation id, and an `st.progress` bar driven by `min(elapsed/1800, 1.0)` captioned as visual-only. Status mapping: IN_PROGRESS=🟡, FINISHED=✅, FAILED=❌, UNKNOWN=⚪.
- Verification auto-runs once on FINISHED (guarded by `VERIFICATION_DONE_KEY`). For each unique kind: call `search_records_by_kind`, if ok and count==0 retry up to 3x with 5s `time.sleep` between (per-kind retry counter persisted to `ingestion_verification_retries`). Render dataframe (kind/count/ok/http_status/latency_ms/correlation_id). Three-banner outcome: all kinds >0 -> green "✅ Ingestion verified - N records found across M kinds"; any failed -> red error; any 0-after-retries -> yellow "⚠️ indexer lag, try refreshing search later" (NOT an error per Satya's truth-source rule).
- History endpoint labels exactly per spec: `legal-tag-check` (LegalTagCheckResult), `submit` (workflow POST), `poll` (workflow GET), `search.{kind}` (per-kind search). Latency chart uses pivot_table on the ok=True rows so failures don't drop a line to zero (matches entitlements idiom). History panel shows `History (N)` with the count, has its own bottom-of-page "🧹 Clear history" button, and the action row also surfaces a Clear-history shortcut when history is non-empty (entitlements has only one button, but the user spec called for the secondary).
- Clear-history resets the in-session history + all derived state (run id, status, polling_active, verification flags + results + retries, last-result keys, last correlation id) but PRESERVES manifest text and form inputs so the operator doesn't lose what they pasted. This differs from the strict contract reading but matches "operator UX over purity" - a re-run shouldn't blank the editor.
- TNO sample expander: rendered only when `TNO_SAMPLE_MANIFEST` truthy. "Insert TNO sample into editor" button writes the raw template (placeholders intact) to `MANIFEST_TEXT_KEY` and `st.rerun()`s. Substitution happens at submit time, not insert time, so the operator can re-edit the inputs without losing their textarea changes. `TNO_SAMPLE_DESCRIPTION` rendered as markdown above the button when present.
- Key signature alignment with Kevin's code: `substitute_manifest_placeholders` takes `legal_tag_name=` (not `legal_tag=`) - mypy caught my first draft, fixed in one pass. `validate_manifest_json` returns `(ok, error_message, parsed)` tuple per Satya's contract; the page treats `parsed is None` as a defensive guard alongside `ok=False`.
- Added `app/pages/3_📥_Ingestion.py" = ["N999"]` to `[tool.ruff.lint.per-file-ignores]` in `pyproject.toml` (matches Settings + Entitlements - Streamlit page filenames intentionally include emoji + digit prefixes).
- pandas import keeps the same `# type: ignore[import-untyped]` pattern (no pandas-stubs installed). `E712` suppressed on the explicit boolean comparison in `_history_to_chart_frame` because pandas needs `frame[col] == True`, not Python-truthy `frame[col]`.
- DID NOT touch Kevin's modules (`app/services/ingestion.py`, `app/services/verification.py`, `app/models/osdu.py`). DID NOT touch `connection_state.py`. DID NOT touch tests (Charlie owns).
- Validation: `ruff check "app/pages/3_📥_Ingestion.py"` -> All checks passed. `mypy "app/pages/3_📥_Ingestion.py"` -> Success: no issues found in 1 source file. Did not run end-to-end against a live ADME because no test connection is wired in this session - Charlie's recorder tests will exercise the page logic.

### 2026-05-07 — Sticky errors on Ingestion page

- Bug: clicking "Validate & Ingest" with empty form fields ran the pipeline inside st.status, which auto-collapsed on failure → red error flashed and disappeared.
- Fix 1 (pre-pipeline gate): before opening st.status, validate that legal-tag, ACL owners, ACL viewers, AND manifest text are all non-empty. On any miss, render a single st.error listing each missing field by name and return without entering the pipeline. Form values stay in session state.
- Fix 2 (sticky pipeline errors): added new session key `ingestion_last_error` (str | None). Each pipeline failure path (validation, substitution, legal-tag, submit, polling-FAILED, polling-timeout) now (a) keeps its in-status detailed render, (b) calls `status_box.update(state="error")` so the box stays expanded, and (c) sets the sticky message. Refactored `_run_submit_pipeline` to raise `_PipelineFailureError` from inside the `with status_box:` block; the outer `try/except` records the sticky and renders an outside-status `st.error`. `_render_sticky_error` (new) shows the message + a "Dismiss error" button at the top of the page on every rerun. Cleared at the start of every "Validate & Ingest" click or by Dismiss.
- Charlie's locked keys are unchanged. `ingestion_last_error` is purely additive. Updated one existing test (`test_submit_pipeline_with_missing_legal_tag_inputs_aborts_at_step_1`) to assert the new "fill in" gate-message wording and the sticky-key being populated.
- Ruff + mypy clean. 21/21 ingestion-page tests pass.

### 2026-05-07 Legal Tags page (issue: Legal Tags MVP)
- Built `app/pages/4_🏷️_Legal_Tags.py` (~870 LOC) against Satya's locked contract + Darryl's verified API research + Mariel's spawn-prompt session-key list. Mirrors the entitlements + ingestion pages: sys.path bootstrap, `ensure_session_defaults` + `_ensure_page_defaults`, `_preflight_ok` chain, `_acquire_token` (AuthenticationError + bare-Exception branches), `_render_sticky_error` + `_set_sticky_error` + `_clear_sticky_error` triplet, history-append idiom, latency line_chart pivot, dataframe rendering.
- Locked session-state keys (Charlie tests these, no quiet additions): `legal_tags_autorun_done`, `legal_tags_list`, `legal_tags_selected_name`, `legal_tags_selected_detail`, `legal_tags_edit_mode`, `legal_tags_properties_spec`, `legal_tags_properties_fallback`, `legal_tags_last_error`, `legal_tags_history`, `legal_tags_show_valid_only`, `legal_tags_delete_confirm_text`, plus 11 `legal_tags_create_form_*` keys (`_name`, `_description`, `_country_of_origin`, `_other_countries`, `_contract_id`, `_expiration_date`, `_originator`, `_data_type`, `_security`, `_personal_data`, `_export_classification`). Internal-only helpers (`_legal_tags_delete_open`, three `legal_tags_edit_form_*` keys, per-widget `__widget` / `__text` suffix keys) are namespaced and explicitly NOT part of the locked contract.
- Pre-flight chain mirrors entitlements verbatim: no/invalid connection -> info + Settings page_link; USER_IMPERSONATION without UserAuthState -> info + page_link; missing data_partition_id -> info + page_link. Token acquisition is wrapped in try/except AuthenticationError + bare Exception with same operator-safe rendering as ingestion.
- Autorun-once fires `list_legal_tags` AND `get_legal_tag_properties` on first render; `🔄 Refresh` button bypasses the guard and re-pulls both. Toggling `Show only valid tags` re-pulls just the list with `valid=True` (or `None`) — does NOT re-pull properties (partition spec is stable session-to-session).
- Properties endpoint fallback: when `get_legal_tag_properties` returns `ok=False` with `http_status=404` -> `legal_tags_properties_fallback=True` and `st.info` banner "Your ADME instance does not expose legal tag property defaults. The Create form will use free-text inputs - refer to OSDU spec for valid values." Free-text fallback uses comma-separated parsing for country dropdowns (round-trips list[str] <-> str), text_input for the four enum fields. Non-404 properties failures keep any previously cached spec, set fallback flag, AND surface the error stickily — so we don't silently downgrade UX on a transient blip.
- Sticky error pattern reuse: identical conceptual shape to ingestion's `INGESTION_LAST_ERROR_KEY` -> `legal_tags_last_error`. `_render_sticky_error` runs at the top of every page render and pairs the `st.error` with a "Dismiss error" button. Cleared at the start of every Refresh / Edit-toggle / Delete-toggle / Save / Create / Confirm-delete. Pre-form-validation gate (`_collect_missing_create_fields`) lists every empty required field as a bulleted `st.warning` AND disables the Create button — operator never gets a confusing red flash from a half-filled form.
