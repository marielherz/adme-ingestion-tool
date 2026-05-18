# darryl — history

> **Summary note (Scribe, 2026-05-18T20:00:00Z):** Earlier entries archived to history-archive.md to keep this file under 15 KB. Recent learnings retained below.
    property names. `map_csv_column_names_to_parameters()` lowercases both sides.
  - Type coercion uses `int()`, `float()`, `bool()`, `datetime_YYYY-MM-DD()` wrappers
    in the JSON template. Mapper should infer from schema `type`/`format`.
  - Relationship fields (`x-osdu-relationship` annotation) need full OSDU ID construction:
    `{partition}:{group}--{EntityType}:{value}:`.
  - Namespace placeholder `<namespace>:` is replaced at generation time via
    `--schema-ns-value` flag.
- **Upstream source:** Azure/osdu-data-load-tno (C# rewrite, main branch). CSV data
  originates from OSDU open-test-data GitLab archive `rc--3.0.0/1-data/3-provided/TNO/`.
  Mapping configs: `tno_well_data_template_mapping.json`,
  `tno_wellbore_data_template_mapping.json`, `tno_misc_master_data_template_mapping.json`.
- **Loading order confirmed:** ReferenceData → MiscMasterData → Wells → Wellbores →
  WorkProducts. Batch size configurable (default 25 for master-data, via Storage API
  `PUT /api/storage/v2/records` with batch ≤500).
  confirms `Osdu_ingest` is the R3 DAG that consumes this envelope.
- **Decision doc:** `.squad/decisions/inbox/darryl-tno-sample-manifest.md`.

### 2026-05-07: Verified legal-tag and ACL formats for Ingestion-page UX

- **Legal tag stored form:** `<instance>-<data-partition-id>-<rest>`. The
  Legal POST API auto-prepends `<instance>-<partition>-` if missing
  (Microsoft Learn — *How to manage legal tags*). Downstream GETs and
  manifest references must use the FULL stored form. ADME ships zero
  pre-created legal tags — operator must POST to
  `/api/legal/v1/legaltags` once before any ingestion will succeed.
- **ACL group grammar (verified):**
  `{groupType}.{serviceName|resourceName}.{permission}@{partition}.{domain}`
  with `{domain}` literally `dataservices.energy` for ADME. Source:
  Microsoft Learn — *Entitlement service*.
- **Pre-created defaults (auto on partition provisioning):**
  `data.default.owners@{partition}.dataservices.energy` and
  `data.default.viewers@{partition}.dataservices.energy`. Confirmed by the
  Azure TNO loader's `appsettings.json` shipped values, which match the
  Learn grammar exactly. There is NO shorter realm.
- **Caller membership matters more than group existence:** the two default
  groups exist on every partition, but a fresh user is not in them. Submit
  will be HTTP 202 then the DAG fails inside Storage with
  `dataAuthorizationFailure`. Worth catching pre-submit.
- **Cheapest read-only pre-flight:** reuse `check_legal_tag` (already
  exists) plus `fetch_my_groups` (entitlements page already calls it) and
  case-insensitive cross-check the typed ACL group emails against the
  caller's flattened group list. Do NOT use
  `POST /entitlements/v2/groups/{g}/members` — that requires group-OWNER
  and mutates server state.
- **Decision doc:**
  `.squad/decisions/inbox/darryl-legal-tag-acl-defaults.md`. Recommends
  (a) Suggest-defaults button, (b) Test legal tag + ACL access pre-flight
  button, (c) updated placeholder/help text per field. No code changes
  yet — Judson/Kevin/Charlie/Satya called out for follow-up.


### 2026-05-07: Full Legal Service API contract verified for "🏷️ Legal Tags" page

- **Authoritative source:** the OSDU `LegalTagApi.java` controller
  (https://community.opengroup.org/osdu/platform/security-and-compliance/legal/-/raw/master/legal-core/src/main/java/org/opengroup/osdu/legal/api/LegalTagApi.java).
  Cross-checked against Microsoft Learn (How to manage legal tags) and the
  OSDU community API doc.
- **Endpoint catalog (M25.1, all under `/api/legal/v1`):**
  `GET /legaltags?valid=true|false` (list, no server pagination),
  `GET /legaltags/{name}` (get one),
  `POST /legaltags` (create, 201),
  `PUT /legaltags` (update — only `description`, `contractId`,
  `expirationDate`, `extensionProperties` are mutable; everything else is
  set-on-create-and-frozen),
  `DELETE /legaltags/{name}` (REAL hard delete, returns 204, gated to
  `users.datalake.admins` only — there is no separate "deactivate"
  endpoint),
  `POST /legaltags:validate` (on-demand recompute — different from
  `?valid=true` which lags by up to 24h),
  `GET /legaltags:properties` (canonical source for dropdown values —
  must call this rather than hard-coding enums; partition-specific),
  `POST /legaltags:batchRetrieve`, `POST /legaltags:query` (M23+, may be
  feature-flagged off → 405).
- **Required create fields:** `name`, `properties.countryOfOrigin`
  (ISO Alpha-2 array, NOT alpha-3), `properties.contractId` (use literal
  `"No Contract Related"` if no contract), `properties.originator`,
  `properties.dataType`, `properties.securityClassification`,
  `properties.personalData`, `properties.exportClassification`. Optional:
  `description`, `properties.expirationDate` (defaults `9999-12-31`),
  `properties.extensionProperties`.
- **Permission tiers per controller `@PreAuthorize`:** read =
  `users.datalake.viewers`+, create/update = `users.datalake.editors`+,
  delete = `users.datalake.admins` only. Page must hide Delete for
  non-admins.
- **Active/inactive is NOT a field.** Validity is derived from
  `expirationDate` + COO config + the once-a-day batch validation pass.
  Only ways to "turn off" a tag: delete it, or PUT a past expiration date.
- **TNO loader behavior:** the Azure C# loader CREATES the legal tag at
  runtime as step 2/6 — does not assume pre-existing. Its
  `appsettings.json` template just configures the tag NAME +
  ACL viewer/owner. Real property values are hardcoded in the C# source
  with sensible defaults (US, Public Domain, EAR99, etc.).
- **Recommended first-time-operator default tag** (Section D of the
  decision doc): name `{partition}-default-legal-tag`, country `["US"]`,
  contract `"No Contract Related"`, expiration `2099-12-31`, originator
  `"ADME Operator"`, dataType `"Public Domain Data"`, security `"Public"`,
  personal `"No Personal Data"`, export `"EAR99"`. Zero-friction on any
  ADME partition.
- **Followups flagged:** Kevin → new `app/services/legal.py` module
  mirroring `entitlements.py` shape; Judson → new
  `app/pages/4_🏷️_Legal_Tags.py` reusing Suggest-defaults +
  sticky-error patterns; Charlie → contract tests for name auto-prefix,
  mutable-field whitelist, admin-only delete gate, `:validate` vs
  `?valid=true` divergence; Satya → confirm module boundary
  (`legal.py` separate from `ingestion.py` — yes).
- **Cross-page improvement:** the Ingestion page should grow a "Pick from
  existing tags" dropdown (single `GET /legaltags` call) and switch its
  pre-flight from `GET /legaltags/{name}` to `POST :validate` (strictly
  more correct).
- **Decision doc:** `.squad/decisions/inbox/darryl-legal-tags-api.md`.

### 2026-05-13: TNO master-data vendoring assessment and plan

- **Manifest envelope:** Master-data uses `"MasterData"` array in
  `Manifest:1.0.0` (vs `"ReferenceData"` for ref-data). The
  `_TIER_TO_SECTION` dict in `bulk_loader.py` already maps
  `"master-data" → "MasterData"` — zero code changes needed for the
  section lookup.
- **Target entities (minimum viable):** Organisation
  (`osdu:wks:master-data--Organisation:1.0.0`), Well
  (`osdu:wks:master-data--Well:1.0.0`), Wellbore
  (`osdu:wks:master-data--Wellbore:1.0.0`).
- **Schema analysis from vendored schemas:**
  - Well: extends AbstractFacility. Key fields: `FacilityName`,
    `FacilityOperator` (→ Organisation SRN), `SpatialLocation`,
    `VerticalMeasurements`, `DefaultVerticalMeasurementID`,
    `DefaultVerticalCRSID` (→ CoordinateReferenceSystem ref-data),
    `InterestTypeID` (→ WellInterestType ref-data). Required top-level:
    `kind`, `acl`, `legal`.
  - Wellbore: extends AbstractFacility. Key fields: `WellID`
    (→ Well SRN, **hard dependency**), `SequenceNumber`,
    `TrajectoryTypeID` (→ WellboreTrajectoryType ref-data),
    `DefinitiveTrajectoryID` (→ WPC WellboreTrajectory, future),
    `DrillingReasons`, `TargetFormation`, `PrimaryMaterialID`
    (→ MaterialType ref-data).
  - Organisation: minimal — `OrganisationName`, `OrganisationID`,
    `OrganisationDescription`. No master-data FK deps. References
    `OrganisationType` ref-data (not in current 13 vendored ref-data
    manifests — may need adding).
- **Load order within MasterData array:** Organisation → Well → Wellbore.
  The OSDU spec explicitly states dependent items must appear AFTER
  their targets within the array.
- **`csv_to_json.py` assessment:** Template-driven CSV → manifest
  generator. Reads `{{parameter}}` tokens from JSON templates, maps to
  CSV column headers, produces per-row records. Supports type coercion
  (`int()`, `float()`, `bool()`, `datetime_*`), nested array parameters,
  schema validation, ACL/legal injection. **NOT needed at runtime** —
  pre-built manifests are the path for v2. Useful as offline tooling if
  regeneration from updated CSVs is ever needed.
- **ACL/legal handling:** Same as reference-data — manifests ship with
  empty `acl.owners`, `acl.viewers`, `legal.legaltags` arrays;
  `_inject_acl_and_legal()` in `bulk_loader.py` fills them at submit
  time. No code change needed.
- **`dataset.json` change:** Flip `master-data.enabled` to `true`, add
  `manifest_glob` pointing at `../../osdu/rc--3.0.0/master-data/load_*.json`.
- **Volve pattern:** Same structure, much smaller (~1 well, ~20
  wellbores, 1 org). Single combined manifest. Goes under
  `app/data/datasets/volve/master-data/`.
- **Open risk:** Need to confirm upstream `v0.27.0` ships pre-built
  master-data manifests vs CSV-only. If CSV-only, run `csv_to_json.py`
  offline once and commit output.
- **Decision doc:** `.squad/decisions/inbox/darryl-tno-master-data-plan.md`.


- 2026-05-18T20:00:00Z (Scribe): PR #33 (Search + Manifest Generator) is in upstream review. Backlog Now = #4 Bulk ingestion submit (Lead pick after PR #11-#15 merges).


