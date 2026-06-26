# kevin — history

> **Summary note (Scribe, 2026-05-18T20:00:00Z):** Earlier entries archived to history-archive.md to keep this file under 15 KB. Recent learnings retained below.
- Schema: connection_profiles, active_profile, health_runs, health_run_results
- Transactions atomic per operation (save profile + set active = one transaction; record health run + all service rows = one transaction)

**Conflict resolved with Scott:**
- Scott proposed split environment variables (ADME_STORAGE_ENGINE, ADME_DB_HOST, ADME_DB_PORT, ADME_DB_USER, ADME_DB_PASSWORD)
- Scott proposed storing client_secret in database
- Decision: Single DATABASE_URL (Satya/Kevin/Charlie consensus). Scott to reconcile Key Vault integration to resolve into DATABASE_URL before storage layer sees it.

**Phase 1 readiness (Kevin owns):**
- Add sqlalchemy>=2.0, alembic>=1.14, psycopg[binary] as optional postgres extra
- Repository contracts published before UI work (Judson)
- Exit criteria: repo tests pass SQLite (in-memory + file) and PostgreSQL (testcontainers)
- No app/ui code imports SQLAlchemy directly

**Notes:** Satya/Scott/Judson/Charlie sign-off required before Kevin begins implementation. DATABASE_URL configuration precedence and secret redaction strategy locked. Future: Scott decides where client_secret lives in prod (Key Vault, env, OS keychain).

- 2026-05-15T12:27:55.007+02:00: Ported PR #9's useful forget-connection idea as a storage-bridge API only. `app\storage\repositories\connection_profiles.py` already had delete/clear-active repository primitives, so the new bridge function delegates to the SQLAlchemy boundary, strips no new secrets, and leaves session auth cleanup to existing UI/session behavior.
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

## 2026-05-13 — Legal-tag pre-flight: GET→POST validate

- Switched `check_legal_tag` in `app/services/ingestion.py` from
  `GET /api/legal/v1/legaltags/{name}` to
  `POST /api/legal/v1/legaltags:validate` with body `{"names": [tag]}`.
- The validate endpoint returns `{"invalidLegalTags": [...]}`;
  tag is valid when it is NOT in that list. This confirms validity,
  not just existence — matches what the workflow service checks internally.
- Updated `_call_legal` helper to accept `method` and `json_body` kwargs
  (defaults preserve existing GET callers if any appear later).
- Updated `LegalTagCheckResult` docstring in `app/models/osdu.py`.
- Replaced the old 404-specific curated message with an "is not valid in
  partition" message when the tag appears in `invalidLegalTags`.
- Updated 10 tests in `tests/test_ingestion_service.py`: switched from
  `_patch_get` to `_patch_post`, new response shape assertions, replaced
  the 404 test with an `invalidLegalTags` non-empty test, replaced
  URL-encoding test with body-content test. All 189 tests green.

## 2026-05-13 — Manifest generator service (Satya contract)

- Implemented `app/services/manifest_generator.py` (~380 lines): five public
  functions per Satya's contract — `list_schema_kinds`, `load_schema`,
  `extract_schema_fields`, `auto_map`, `generate_manifests`.
- Added `SchemaField`, `FieldMapping`, `MappingResult` dataclasses to
  `app/models/osdu.py`.
- Schema walker resolves `$ref` and `allOf` across the vendored OSDU schema
  tree (abstract schemas, entity schemas). System/envelope fields are excluded.
- Mapping heuristic v1: normalise → exact match → suffix match → Jaccard
  token overlap (≥ 0.5). Pure stdlib, no new dependencies.
- `generate_manifests` reads CSV, applies confirmed mapping to build data
  blocks, wraps records in Manifest:1.0.0 envelope with correct section
  (MasterData / ReferenceData / Data), batches at 1000 rows.
- 36 tests in `tests/test_manifest_generator.py` covering all public
  functions plus edge cases (empty CSV, no matches, missing required,
  batching, real TNO CSV end-to-end). 216 related tests green.
- Key learning: OSDU schema `required` arrays inside abstract sub-schemas
  (e.g. AbstractAnyCrsFeatureCollection) describe object shape, not CSV
  mapping requirements. All `data.*` fields are technically optional per
  Darryl's conventions doc §7.1. Required propagation must stop at the
  allOf composition boundary to avoid false positives.

- 2026-05-18T20:00:00Z (Scribe): PR #33 (Search + Manifest Generator) is in upstream review. Backlog Now = #4 Bulk ingestion submit (Lead pick after PR #11-#15 merges).



## Learnings (2026-05-22) — bulk ingest v1

- `ADMEConnection` fields: endpoint/tenant_id/client_id/data_partition_id (no `name`, no `base_url`). Copy the `_connection()` builder from tests/test_bulk_loader_service.py.
- Manifest envelope: `parsed = {'executionContext': {'manifest': {ReferenceData/MasterData/Data}}}`. `inject_acl_and_legal` expects the INNER manifest, not the top-level. Harvest helpers must descend into `executionContext.manifest` too.
- Promoted `_inject_acl_and_legal` -> public `inject_acl_and_legal`; kept private alias for back-compat.
- Final-lock 2026-05-22 §4: aborted rows MUST NOT write run history. Skipped rows also skip history.
- Breaker reset semantics: success resets counter to 0; threshold is consecutive failures only.
- `WorkflowRunResult` does not expose response headers, so `_parse_retry_after` is implemented and unit-tested but not yet wired through `submit_manifest` (deferred — needs ingestion service to surface Retry-After).
- `progress_callback` exceptions are caught + logged; never propagate to caller.
- Synthetic run_ids for non-network outcomes: `bulk-load:rejected:{uuid}`, `bulk-load:error:{uuid}`, `bulk-load:skipped:{uuid}`.
