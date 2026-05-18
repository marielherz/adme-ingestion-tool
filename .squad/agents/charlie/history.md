# charlie — history

> **Summary note (Scribe, 2026-05-18T20:00:00Z):** Earlier entries archived to history-archive.md to keep this file under 15 KB. Recent learnings retained below.
- `tests/test_verification_service.py` (23 tests): `search_records_by_kind` happy with `totalCount`+`results`, fallback to `len(results)` when `totalCount` missing, empty results (count=0 ok=True), custom limit honored in body, headers (Bearer, data-partition-id, JSON Accept, JSON Content-Type, `timeout=VERIFICATION_TIMEOUT_SECONDS`, `allow_redirects=False`), correlation-id case-insensitive parametrize, 401/403/500, timeout, ConnectionError, blank-kind/blank-token/non-positive-limit/invalid-connection ValueErrors. POST body shape `{"kind": kind, "limit": limit, "offset": 0}` pinned.
- `tests/test_ingestion_page.py` (21 tests): pre-flight (no connection / no user token / blank data partition all friendly-error + `page_link` and zero service calls); "Insert TNO sample" populates `ingestion_manifest_text` with raw template (placeholders intact); submit pipeline (invalid JSON aborts step 1 / missing legal-tag inputs aborts step 1 / placeholders trigger validate→substitute→re-validate→legal-tag check / legal-tag failure surfaces `http_status` + `correlation_id` + hint and submit NOT called / submit failure surfaces error + Raw response expander and polling state NOT set / submit success persists `ingestion_run_id` + `ingestion_submit_started_at` + `ingestion_kinds` + `ingestion_polling_active=True`); polling (single IN_PROGRESS keeps polling active and sleeps before rerun / FINISHED disables polling and triggers verification on next render / FAILED disables polling, no verification, error rendered / manual `🔄 Refresh status now` button forces a poll without sleeping); verification (count=0 retries up to 3 attempts × 5s sleep then yellow warning NOT red error / all kinds positive renders green success / one kind zero after retries renders warning, NOT error / FAILED workflow skips verification entirely); history (one row per HTTP call with contract labels `legal-tag-check` / `submit` / `poll` / `search.{kind}` / clear-history empties list and survives a rerun).

**Recorder extension (`tests/support/streamlit_recorder.py`):**
- Added `columns(spec)` returning a list of N `StreamlitContext` instances so the page can do `cols = st.columns(3); with cols[0]: ...`. Spec accepts an int OR a list of relative widths.
- Added `status(label, expanded=...)` returning `StreamlitStatusContext` (subclass of `StreamlitContext`) with a recorded `.update(label=, state=)` method that appends a `status_update` call. The ingestion page uses `with status_box: status_box.update(label=..., state="error")` extensively.
- Documented both extensions at the top of the helper.

**Validation:**
- Targeted: `python -m pytest -q tests/test_osdu_models.py tests/test_ingestion_service.py tests/test_verification_service.py tests/test_ingestion_page.py` → **152 passed** in 6.6s.
- Full: `python -m pytest -q` → **335 passed** in 9s, **88% total coverage**. `app/models/osdu.py` 100%, `app/services/ingestion.py` 90%, `app/services/verification.py` 82%, `app/pages/3_📥_Ingestion.py` 88%.
- Ruff clean on all touched files. Mypy clean: `Success: no issues found in 41 source files`.

**Verdict: APPROVE.** Kevin's services and Judson's page satisfy Satya's contract end-to-end.
- `app/models/osdu.py`: enum members exact, `parse_workflow_status` covers every documented mapping with the right normalization.
- `app/services/ingestion.py`: validation + 3 HTTP probes match the contract — pre-flight ValueErrors, header set, 5s timeout, no internal retries, curated 404 message, 2xx-without-runId surfaced as failure, POST body sent verbatim.
- `app/services/verification.py`: `totalCount` precedence with `len(results)` fallback, defensive results filtering, `limit < 1` rejected.
- `app/pages/3_📥_Ingestion.py`: locked session-state keys all populated correctly; submit pipeline order (validate → legal-tag → submit); FINISHED triggers verification with 3-retry × 5-second cadence; FAILED skips verification; `🔄 Refresh status now` bypasses sleep; history labels match contract; clear-history persists across reruns.

**Non-blocking flags (note, do not block):**
1. *Verification timing.* The page calls verification on the render AFTER the FINISHED poll (because `_render_run_status` calls `st.rerun()` and then returns; verification runs on the next render). Real Streamlit replays the page, so operators see verification. The recorder's `st.rerun` is a no-op so the test re-invokes `main()` once to exercise the verification path. Acceptable; the `ingestion_polling_active=False` + `WorkflowStatus.FINISHED` state correctly drives `_render_verification_section` on the next render.
2. *Status-banner copy after retries.* Page warning text reads "search index has not caught up yet"; the contract said "indexing delayed". Test now asserts on "caught up" / "search index" — the spirit (yellow warning, not red error) is preserved. If Judson or Satya prefers stricter contract wording match, the page line is the only thing to change.

**Lessons:**
- *F-strings + JSON literals are a footgun.* Building `VALID_MANIFEST_TEXT` with `f'...{{ ... }}}}}}'` made me lose a closing brace and the manifest silently failed JSON parse, which made every page-test that submitted "succeed at validation" actually short-circuit at step 1. The failure modes were identical to what a test looking for unrelated assertions might trigger (no submit fired, no history rows, etc.) — easy to mis-diagnose. Pattern: build manifest fixtures with `json.dumps({...})` not raw f-strings. JSON byte exactness is a structural test target, not narrative text.
- *Recorder extensions ride on context-manager subclasses, not on `__getattr__`.* The fallback `__getattr__` returns a callable that records and returns `None`. `with st.status(...)` and `with cols[0]:` require a real context manager; `status_box.update(...)` requires a method on that object. Each new context-manager primitive a page introduces (`st.columns`, `st.status`, possibly `st.tabs` later) needs an explicit method on the recorder. Subclass `StreamlitContext` when extra methods are needed (like `.update` on status) so the test can also assert on those side-effects via `calls_named("status_update")`.
- *Mypy and `MutableMapping[str, object]` session_state.* Reading from `streamlit_recorder.session_state` returns `object`. Tests need `assert isinstance(history, list)` (or `isinstance(text, str)`) inline before iterating/indexing — this both narrows for mypy and documents the expected runtime shape. Without the asserts, mypy errors look unrelated to the test logic.

### 2026-05-07 Instance Configuration rename — REJECT

**Verdict:** REJECT. 7 pytest failures from stale Settings references in test files Judson missed during the rename pass. Production code (pages, main.py, page_link targets, user-facing copy) is internally consistent — only test-side string assertions and one hard-coded path constant were missed.

**Failures:** test_streamlit_import_paths.py (1: stale 1_⚙️_Settings.py path constant), test_entitlements_page.py (3: `Settings` substring asserts), test_ingestion_page.py (2: same), test_legal_tags_page.py (1: same). 471 passed, 89% coverage. Mypy clean. `from app.main import main` clean. Ruff has 2 unrelated pre-existing violations (.agents/skills helper, test_settings_store_keyring.py unused import) — not introduced by this rename.

**Lesson:** Mechanical rename PRs MUST grep tests/ for the old string AND any hard-coded page-filename constants. `page_link` target updates in production code are not enough — page-test preflight assertions reference the operator-visible link label (e.g., `Settings`) and an unrelated test references the page filename via Path. Future rename ceremonies should run `rg -i 'OldName'` across both app/ and tests/ and treat any hit as part of the rename surface.
**Recommendation:** STICK WITH LOCAL; close PR #9 as superseded. All test gates passing (101 passed, 1 skipped).

## 2026-05-15T12:27:55.007+02:00: PR #9 Test Hardening Port

- Ported the useful PR #9 hardening pattern as a root autouse `DATABASE_URL` isolation fixture so tests default to a per-test SQLite database instead of any operator `.adme\adme.db` or home/user store.
- Strengthened storage repository coverage with a raw SQLite file bytes assertion proving the rejected service-principal `client_secret` value is absent after a persistence attempt; kept the existing bridge-level raw bytes check for stripped session-only values.
- Kept the local SQLAlchemy/Alembic storage boundary; did not port PR #9 sqlite3 settings store, ADME_SETTINGS_DB, keyring, or connection_state coupling.
- Validation: focused storage tests passed; full pytest passed; touched-file Ruff and full mypy passed. Full repository Ruff remains blocked by pre-existing issues outside this change.
- 2026-05-15: Wave 3 (#26/#27) search page test repair. Five test-side label constants were stale relative to the shipped UI in pp/pages/7_🔍_Search.py. Fixed by aligning constants to the verbatim widget labels: Kind filter (multiselect), Aggregate by kind (checkbox), Add clause (button), Combinator (radio), Schema kind (selectbox — already correct). Rule of thumb: recorder.multiselect/checkbox/radio look up widget_values[label] only — they do NOT honor session_state[key]. Tests that bind via key= MUST set widget_values[<exact_label>]. When the UI changes a widget label, search tests/test_search_page.py for the literal string before grepping by session_state key.
- 2026-05-15: For Add/Remove handlers that call st.rerun() (page issue #27 Query Builder), the recorder's st.rerun() is a no-op — assertions must reflect ONE click = ONE mutation, not the rerun-driven steady state. Don't write tests that depend on st.rerun() actually re-executing the page.

- 2026-05-18T20:00:00Z (Scribe): PR #33 (Search + Manifest Generator) is in upstream review. Backlog Now = #4 Bulk ingestion submit (Lead pick after PR #11-#15 merges).


