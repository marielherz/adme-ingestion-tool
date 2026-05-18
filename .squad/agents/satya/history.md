# satya — history

> **Summary note (Scribe, 2026-05-18T20:00:00Z):** Earlier entries archived to history-archive.md to keep this file under 15 KB. Recent learnings retained below.
- Locked verification as the truth source: workflow `finished` is never reported as success in the UI until `search_records_by_kind` returns. Indexing-delay mitigation is 3 retries × 5s.
- Reused entitlements patterns verbatim (per-call _call_* helper, 5s timeout, frozen result dataclasses, correlation-id probe, error-body extraction). New modules duplicate the helpers rather than import them — refactor to a shared module is a deliberate v2.
- Ownership split: Kevin ships models + services, Judson ships page (validate-only path can start before Kevin's HTTP code lands because `validate_manifest_json` is pure), Charlie writes tests against the locked signatures, Darryl supplies `TNO_SAMPLE_MANIFEST` content.

### 2026-05-07 — Legal Tags page MVP contract locked

- Wrote .squad/decisions/inbox/satya-legal-tags-page-contract.md covering app/services/legal_tags.py (6 functions + ported _call_legal helper), 5 new dataclasses on app/models/osdu.py, app/pages/4_🏷️_Legal_Tags.py (single-page layout, no tabs), and full test scope across service/page/model.
- LEGAL_TAGS_PATH single source of truth: defined in legal_tags.py, ingestion.py imports it. Kevin authorized to also extract a shared _http.py if mechanically clean; otherwise accept duplication for v1.
- Locked outbound properties payload key shape (camelCase server keys) so create/update payload is unambiguous regardless of whether page or service builds the dict.
- Section 7 specifies three fallback strategies behind feature flags so Judson + Charlie do NOT block on Darryl's research: (a) update-as-delete-then-recreate if PUT unsupported, (b) free-text form if properties endpoint 404s, (c) Deactivate relabel if DELETE only sets isValid=False.
- Kevin can start immediately on signatures + dataclasses; Judson can scaffold the page UX + session keys against the doc in parallel; Charlie can write dataclass tests immediately, service tests after Kevin lands signatures, page tests after session-key contract lands.
- 2026-05-05T19:48:42.932+02:00: Storage architecture plan — chose SQLite (via SQLAlchemy 2.x + Alembic) as dev default; production uses operator-supplied PostgreSQL through a single `DATABASE_URL` env var. Rejected PGlite because it is JS/WASM only and has no Python embedding story; SQLite gives the same single-file/zero-install outcome with stdlib driver support.
- 2026-05-05T19:48:42.932+02:00: Storage scope kept deliberately narrow for Phase 1: connection_profile + health_run_summary only. Secrets (client_secret, MSAL tokens) are forbidden in the DB and Charlie gates that boundary. `ADMEConnection` stays a dataclass; repositories return domain dataclasses, not ORM objects, so existing contracts in `app/connection_state.py` and `app/models/connection.py` are unaffected.
- 2026-05-05T19:48:42.932+02:00: ORM portability rule: dialect-portable column types only (no `JSONB`, no `ARRAY`, no Postgres-only server defaults). Alembic auto-upgrade is allowed on SQLite startup but Postgres operators must run migrations explicitly. Phase ordering is Kevin (storage layer + repos) -> Judson (Settings/Welcome wiring) -> Scott (prod deploy + secret-store decision) -> Charlie (matrix tests).
- 2026-05-05T19:48:42.932+02:00: Open follow-on decisions to track: (1) production secret storage strategy owned by Scott, (2) multi-operator scoping model when we move past single-operator, (3) whether to publish a `[postgres]` install extra.

## 2026-05-05 Storage implementation review (APPROVE)
- Verified storage boundary lives under app/storage with repository/domain interface (ConnectionProfile, HealthRunSummary) outside ORM rows.
- Defaults to SQLite at .adme/adme.db when DATABASE_URL is unset; invalid or non-sqlite/non-postgresql URLs raise instead of falling back, satisfying the no-broken-prod-fallback rule.
- Connection profile persistence rejects client_secret at the repository and the storage_bridge strips it before crossing the boundary; ADMEConnection rebuilt from rows always has client_secret=''. No MSAL/auth code/token persistence.
- Database URLs redacted via safe_description; raw URL hidden from StorageConfig repr.
- SQLite dev auto-migrates via Alembic on ensure_storage_ready; PostgreSQL gets a head-revision check that raises StorageMigrationError with operator guidance instead of auto-upgrading.
- Settings/Welcome hydrate persisted profile and latest health run without touching auth state; user impersonation and client_secret remain session-only.
- Coordinator validation passed: pytest 101 passed/1 skipped, ruff clean, mypy clean.
- Non-blocking follow-ups: storage_bridge reflective dispatch (_first_callable / _accepts_keyword) is more elastic than needed now that app.storage exports a stable API; consider trimming once no alternate storage backends are anticipated. load_persisted_connection_state skips restoring saved health when a session connection already exists - acceptable but worth a Judson UX pass later.

## 2026-05-06T06:44:31.579Z: PR #9 Storage Comparison Review

**Finding:** Local implementation satisfies all acceptance criteria with strong secret rejection/redaction and complete models. PR #9 adds surface features but lacks PostgreSQL production path and complete health/migration coverage.

**Rationale:**
- Local: SQLAlchemy 2.x + Alembic boundary at pp/storage/ package level
- SQLite default + PostgreSQL production fully specified
- Secret rejection strong: client_secret rejected before persistence
- StorageConfig.url redacted for safety
- Profile and health models complete
- All tests passing (101 passed, 1 skipped)

**Recommendation:** STICK WITH LOCAL; close PR #9 as superseded. Cherry-pick test isolation and raw-bytes secret assertions if beneficial.

## Learnings

### 2026-05-06: Ingestion MVP contract locked
- Wrote .squad/decisions/inbox/satya-ingestion-mvp-contract.md covering services (ingestion.py, erification.py), models (osdu.py), page (3_📥_Ingestion.py), and tests.
- Locked polling: native Streamlit `st.rerun()` + `time.sleep` ladder (2s/5s/10s, 30-min timeout) with a manual "Refresh status now" escape hatch. Rejected `st.autorefresh` / `streamlit-extras` to keep deps unchanged.
- Locked verification as the truth source: workflow `finished` is never reported as success in the UI until `search_records_by_kind` returns. Indexing-delay mitigation is 3 retries × 5s.
- Reused entitlements patterns verbatim (per-call _call_* helper, 5s timeout, frozen result dataclasses, correlation-id probe, error-body extraction). New modules duplicate the helpers rather than import them — refactor to a shared module is a deliberate v2.
- Ownership split: Kevin ships models + services, Judson ships page (validate-only path can start before Kevin's HTTP code lands because `validate_manifest_json` is pure), Charlie writes tests against the locked signatures, Darryl supplies `TNO_SAMPLE_MANIFEST` content.

### 2026-05-07 — Legal Tags page MVP contract locked

- Wrote .squad/decisions/inbox/satya-legal-tags-page-contract.md covering app/services/legal_tags.py (6 functions + ported _call_legal helper), 5 new dataclasses on app/models/osdu.py, app/pages/4_🏷️_Legal_Tags.py (single-page layout, no tabs), and full test scope across service/page/model.
- LEGAL_TAGS_PATH single source of truth: defined in legal_tags.py, ingestion.py imports it. Kevin authorized to also extract a shared _http.py if mechanically clean; otherwise accept duplication for v1.
- Locked outbound properties payload key shape (camelCase server keys) so create/update payload is unambiguous regardless of whether page or service builds the dict.
- Section 7 specifies three fallback strategies behind feature flags so Judson + Charlie do NOT block on Darryl's research: (a) update-as-delete-then-recreate if PUT unsupported, (b) free-text form if properties endpoint 404s, (c) Deactivate relabel if DELETE only sets isValid=False.
- Kevin can start immediately on signatures + dataclasses; Judson can scaffold the page UX + session keys against the doc in parallel; Charlie can write dataclass tests immediately, service tests after Kevin lands signatures, page tests after session-key contract lands.
- Satya produced the bulk-load architecture decision (sync, premium): dataset
  registry pattern, `app/data/datasets/{key}/` layout, sequential submit with
  stop-on-first-failure, page placement under Operate.
- Kevin implemented `app/services/bulk_loader.py` + 4 new dataclasses in
  `app/models/osdu.py`, migrated TNO + added Volve `dataset.json`,
  restructured `app/data/tno/` -> `app/data/osdu/` + `app/data/datasets/tno/`,
  12 service tests. All gates green.
- Judson shipped `app/pages/9_📥_Bulk_Load.py`, wired `app/main.py` nav,
  added `N999` ignore in `pyproject.toml`, 9 page tests. All gates green.
- Coordinator branch-untangled RunHistory + TNOVendor work into separate
  clean commits, opened PRs #13, #14, #15 against
  `EirikHaughom/adme-ingestion-tool`.

## 2026-05-13 — Manifest Generator contract

- Designed interface contract for CSV-to-OSDU manifest generation.
  Decision at `.squad/decisions/inbox/satya-manifest-generator-contract.md`.
- Key call: v1 ships heuristic fuzzy name-match (no LLM). Service lives
  at `app/services/manifest_generator.py`, integrates into the existing
  Bulk Load page. CLI and standalone page are deferred.
- `csv_to_json.py` stays vendored and unmodified; `generate_manifests`
  calls `create_manifest_from_row` directly with an in-memory template.
- Kevin builds the service, Darryl validates OSDU schema correctness,
  Judson wires the Bulk Load page UI.
- Deferred: LLM-assisted mapping (v2), custom schema upload, type transforms,
  standalone CLI/page.

- 2026-05-18T20:00:00Z (Scribe): PR #33 (Search + Manifest Generator) is in upstream review. Backlog Now = #4 Bulk ingestion submit (Lead pick after PR #11-#15 merges).


