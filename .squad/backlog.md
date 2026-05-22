# ADME Ingestion Tool — Backlog

Maintained by Satya. Last updated: 2026-05-18 (post upstream merges of PRs #11–#15 + PR #33 in review).

This document is the source of truth for what we're working on, what's next, and what's been deferred. `decisions.md` records *why* we decided what; this doc records *what* and *when*. When priorities change, update here first.

## In Review

**PR #33** — `feat(search): wave 3 — multi-kind search, query builder, manifest generator + TNO master-data fixtures + generate_tno_master_data.py`. Open against `EirikHaughom/adme-ingestion-tool:main` from branch `marielherz_SearchAndManifestGen_clean`. Bundles: multi-kind/aggregate search (was Later #9), Lucene field-builder UI (was Later #10), `manifest_generator.py` service wired into the Bulk Load page (was Next #2), TNO master-data load files + `dataset.json` enabled flag flipped (was Next #2b), and `scripts/generate_tno_master_data.py` helper.

## Definitions

- **Now**: actively being worked or next up — one or two items max.
- **Next**: confirmed, scheduled immediately after Now.
- **Later**: agreed-on direction, not scheduled yet.
- **Ideas**: needs discussion before commitment.
- **Tech debt / follow-ups**: small cleanups flagged by agents during shipping.
- **Done**: shipped, with rough date and headline.

Size scale: **XS** (≤1 hr touch-up) · **S** (single page/module, few hours) · **M** (new page or service, half/full day) · **L** (multi-page feature, multi-agent, 2+ days) · **XL** (architectural).

---

## Now

### 4. Bulk ingestion submit — **M** — owner: Judson + Kevin
Paste-many or queue several manifests at once with one progress view. Now unblocked by Run history (#1 shipped — results have somewhere to live). Promoted from Later because: (a) the upstream bulk-load + manifest-gen story is now end-to-end (registered datasets, CSV-to-manifest generation, run history persistence); (b) the next natural operator plateau is "queue N manifests, watch them complete" instead of one-at-a-time submits; (c) ownership pair is already warm from the bulk-load and manifest-generator work. Scope sketch: a "Queue" panel (likely a new tab on the Bulk Load page or a sibling section on the Ingestion page) that accepts multiple pasted manifests or a multi-select from registered datasets, submits sequentially with stop-on-first-failure (matching the existing `bulk_loader` contract), shows per-manifest status + correlation id, and writes each completion to `run_history`. Sequential is fine for v1; parallel submit is a v2 question.

**Sequencing caveat:** PR #33 is in review against upstream. To avoid rework, design + interface contract can happen now; implementation against the Bulk Load page should land *after* PR #33 merges so we don't fork the page surface. If PR #33 stalls in review, fallback is to branch from `marielherz_SearchAndManifestGen_clean` and reconcile at PR time.

---

## Next

### 2c. AI-assisted schema mapping (v2 mapper) — **M** — owner: Kevin + Darryl review
When `auto_map()` returns low confidence (many unmatched required fields), offer an "🤖 AI Suggest" flow that sends OSDU schema fields + CSV headers + sample rows to an LLM and returns proposed `FieldMapping` pairs the operator can review/edit. Requires Azure OpenAI (or configurable model endpoint). Returns the same `MappingResult` shape as the heuristic so the UI doesn't change. Scope: `ai_map()` function in `manifest_generator.py`, config for model endpoint, and the "AI Suggest" button in the Bulk Load page CSV flow. Touches PR #33 surface — sequence after #33 merges.

### 5. Saved searches — **S** — owner: Judson
Name + persist a `(kind, query, returnedFields, limit)` tuple locally so operators can re-run a frequent query without retyping. Pairs with run history's storage choice. Reference UX grounding: [OSDUBootcamp Module 4](https://github.com/EirikHaughom/OSDUBootcamp/tree/main/Labs/Module%204%20-%20Constructing%20Searches) — shows the building blocks operators compose (kind filter, Lucene query, field projection, limit). The save flow should capture all four. Touches PR #33 surface (search page) — sequence after #33 merges.

### 6. Export search results — **S** — owner: Judson
"Download CSV / JSON" on the Search page so operators can hand a result set to a notebook or share with a colleague. For result sets > 1000, use the cursor search API (`/api/search/v2/query/cursor`) to paginate through all results before export. Honor the OSDU 10,000 totalCount ceiling and warn when the result set is bigger than what's been pulled. Reference: [OSDUBootcamp Module 4 §4.2](https://github.com/EirikHaughom/OSDUBootcamp/tree/main/Labs/Module%204%20-%20Constructing%20Searches) — cursor pagination pattern. Touches PR #33 surface — sequence after #33 merges.

---

## Later

### 8. Record edit / delete (Storage write paths) — **M** — owner: Kevin
Today Search can *view* a full record (GET `/api/storage/v2/records/{id}`). Adding edit (`PUT`) and delete (`DELETE`) lets the Search page round-trip changes. Security-sensitive — needs explicit confirmation UI and a clear "this writes to OSDU" affordance.

### 11. App branding / favicon / About page — **XS** — owner: Scott or Judson
Replace the default Streamlit branding, add an About page with build/version info and a link to the GitHub repo. Cosmetic; do it once the feature surface stabilizes.

### 12. Operator quickstart doc — **S** — owner: Scott
Standalone `docs/quickstart.md` (or expanded README section) walking a new operator from clone → run → first ingest. README has prerequisites today; this is the missing "happy path" narrative. Increasingly valuable now that the operator surface is broad (Instance Config → Entitlements → Legal Tags → File Upload → Manifest → Ingest → Search → History → Bulk Load).

---

## Ideas

These need conversation before they become commitments — flagging them so they don't get lost.

### 13. Geo-spatial / GIS search — **L** — owner: TBD
OSDU Search supports `spatialFilter` (bounding box, distance, polygon). Useful for E&P workflows but introduces a map widget (folium / pydeck) and a real UX question: what does the result look like, a list or a map? Defer until a user explicitly asks for it.

### 15. Replace `verification.py` with `search.py` — **S** — owner: Kevin
Kevin flagged during Search v1 that `verification.py::search_records_by_kind` duplicates ~120 LOC of HTTP plumbing (`_call_search`, correlation extraction, JSON parsing, truncation) that now also lives in `search.py` and `legal_tags.py`. The post-ingest verification flow could call `search.search_records` directly and the orphan `SearchResult` dataclass could be deleted. See tech debt list — could also be a candidate if we do another ingestion-touching feature.

### 16. Extract shared HTTP plumbing into `app/services/_http.py` — **M** — owner: Kevin
The deeper version of #15: `_call_*` / correlation / JSON helpers are now triplicated across `legal_tags.py`, `verification.py`, `search.py`. One internal helper module would DRY the lot. Pure refactor — schedule when a service-touching feature is already in flight.

---

## Tech debt / follow-ups

Small flagged items from shipping. Not features; do opportunistically.

- **Reconcile legal-tag update body shape** (Kevin, from `kevin-legal-tags-impl-notes.md`) — flagged a 400-risk where PUT body shape may not match OSDU's expectation under some property combinations. Low-frequency, but worth verifying with a partition that has real tags.
- **Orphan `SearchResult` dataclass cleanup** (Kevin, search v1 follow-up) — kept because `verification.py` + page 4 still import it. Cleared with #15 above.
- **`sort` as kwarg on `search.search_records`** (Kevin) — today fixed to `createTime DESC`. Promote to kwarg if a future Search feature needs relevance ordering (omit `sort` → `_score DESC`).
- **`sample_limit` kwarg on `search.list_kinds`** (Kevin) — currently 100 for the page-sample fallback; lift if dropdowns look sparse in real partitions.
- **Page warning text** (Charlie, ingestion review) — Page 4 post-ingest warning reads "search index has not caught up yet" while the contract said "indexing delayed". Semantically equivalent; align if anyone is in the file.
- **README operator-flow wording drift check** (Scott, auth review) — already fixed once for MSAL; re-scan when a new auth-touching feature lands so we don't reintroduce stale "separate tab" wording.
- **Patch File Upload contract doc** (Satya, from `kevin-file-upload-impl.md`) — three divergences shipped against Darryl's authoritative cite: (a) `kind` uses literal `osdu:` schema authority, not `{partition}:` prefix; (b) `FILES_TIMEOUT_SECONDS = 15`, not 10; (c) metadata POST body includes `"status": "compliant"` in the `legal` block. Wire shape is correct as shipped; the contract doc just needs to catch up.
- **File Service uploadURL 5xx retry policy** (Kevin, file upload v1 open question) — `get_upload_url` currently does no internal retries on rare ADME 5xx, matching the established service pattern (page handles re-run UX). Confirm with Brady whether this stays or gets a bounded retry; if it changes, revisit the legal_tags / search / ingestion services for consistency.
- **Chunked upload for files > 100 MB** (Darryl, file upload research) — v1 caps single-PUT at 100 MB (`MAX_FILE_BYTES_V1`). Anything larger needs Azure Put Block + Put Block List with progress + resume; out of scope for v1 but worth a follow-up when an operator hits the cap. Page should show a clear "use Azure Storage Explorer + manual metadata POST" hint when the gate trips.
- **Branch hygiene** (Coordinator, 2026-05-18) — 8 stale local branches deleted on rebase to clean PR #33; `marielherz_SearchAndManifestGen_clean` is the live PR branch. Routine going forward: prune merged branches after each upstream merge.

---

## Done

- **PR #33 opened — search wave 3 + manifest-gen wiring + TNO master-data** — 2026-05-18 — in review against `EirikHaughom/adme-ingestion-tool:main`. Bundles the work below into one clean cherry-pick on top of post-#15 main:
  - **Generate from CSV wired into Bulk Load page** (was Next #2) — `app/pages/9_📥_Bulk_Load.py` gains a "📄 Generate from CSV" tab: operator picks a kind → uploads CSV → reviews `auto_map` output in an editable table → confirms → `generate_manifests` produces manifests fed into the existing submit pipeline.
  - **TNO master-data manifests vendored** (was Next #2b) — `app/data/datasets/tno/master-data/load_Organisation.json`, `load_Well.json`, `load_Wellbore.json`; `dataset.json` `master-data.enabled` flipped to `true`. Helper script `scripts/generate_tno_master_data.py` produces these from the upstream CSVs.
  - **Multi-kind search + aggregateBy** (was Later #9) — `app/pages/7_🔍_Search.py` kind selector is now a `multiselect`; supports OSDU `kind: []` and `aggregateBy: "kind"` via new `build_multi_kind_query` helper.
  - **Field-builder UI for Lucene queries** (was Later #10) — query builder lets operators compose `data.{field}:{value}` clauses with AND/OR combinators and `returnedFields` projection instead of typing raw Lucene.
- **Upstream merges** — 2026-05-15 → 2026-05-18 — Eirik merged PRs #11 (Ingestion), #12 (File Upload), #13 (Run History), #14 (TNO Vendor), #15 (Bulk Load) into `EirikHaughom/adme-ingestion-tool:main`. The fork's feature surface is now upstream.
- **Run history page (Operate › History)** (was Now #1) — 2026-05-13 — shipped in PR #13. `app/services/run_history.py` + `app/pages/8_📊_History.py` give operators a cross-session list of past workflow runs (run id, kind, status, latency, when) plus recent file uploads.
- **Manifest Builder v1 (Ingest › Manifest)** — 2026-05-11 — form-driven construction of a single `osdu:wks:dataset--File.Generic:1.0.0` manifest from operator inputs, exposed as a `🛠️ Build manifest` expander above the manifest editor on the Manifest page. Two pick modes: "From recent uploads" (reads in-session `upload_summary` rows produced by the File Upload page) and "Paste manually" (operator supplies FileSource + record id directly). Auto-fills display name, description, ACL/legal pickers; emits valid Workflow-ready JSON into the editor for review or hand-edit before submit. New pure service `app/services/manifest_builder.py` (`build_file_generic_manifest`), UI in `app/pages/5_📄_Manifest.py`. Same shipping pass also reorganized the sidebar nav into three groups — Setup / Ingest / Operate — and renamed/regrouped the File Upload and Manifest pages accordingly. Validator loosened: `Data` block on WPC records now accepts the OSDU work-product-component object shape. Walkthrough doc: `docs/walkthroughs/tno-end-to-end.md`. 713 tests pass.
- **File Upload page (Operate › File Upload)** — 2026-05-11 — branch `marielherz_FileUpload`, PR #12 (9 files, +3,251). Three-phase OSDU File Service v2 flow: GET `/api/file/v2/files/uploadURL` → PUT bytes to Azure signed URL (with `x-ms-blob-type: BlockBlob`) → POST `/api/file/v2/files/metadata`. New `app/services/files.py`, three result dataclasses (`UploadURLResult`, `UploadBytesResult`, `FileMetadataResult`), new page `6_📂_File_Upload.py`, 100 MB single-PUT cap. 600/600 tests pass.
- **CSV → manifest helper service** (was Later #7) — 2026-05-13 — shipped as `app/services/manifest_generator.py`: `list_schema_kinds`, `load_schema`, `extract_schema_fields`, `auto_map` (heuristic fuzzy matching), `generate_manifests`. 36 tests. Pure Python, no new deps. Page wiring shipped in PR #33 above.
- **Search page (Operate › Search)** — 2026-05-11 — kind dropdown + Lucene query + record fetch + pagination respecting OSDU 10,000 offset+limit ceiling. New `app/services/search.py`, four new dataclasses, +122 tests. Wave 3 enhancements shipped in PR #33.
- **Ingestion MVP page (Operate › Ingestion)** — 2026-05-06 — manifest paste → legal-tag pre-flight → workflow submit → status polling → post-ingest verification. 152 tests, 88% coverage. PR #11.
- **Legal Tags page (Setup › Legal Tags)** — full CRUD against `/api/legal/v1/legaltags`.
- **Entitlements page (Setup › Entitlements)** — group membership view for the authenticated principal.
- **Instance Configuration page** — connection form, keyring-backed secret storage, MSAL user-impersonation auth-code + PKCE flow with redirect-back-to-Streamlit, manual token-scope override.
- **MSAL auth refactor** — replaced `InteractiveBrowserCredential` with an app-managed MSAL `PublicClientApplication` flow on `http://localhost:8501`.
- **Squad team + governance scaffolding** — `.squad/` directory, charters, decision ledger, orchestration logs, casting registry.
