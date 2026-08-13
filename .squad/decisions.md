# Squad Decisions

## Active Decisions

### 2026-05-11T16:30:00Z: OSDU File Service upload API — authoritative reference for ADME

**By:** Darryl (OSDU/ADME expert)
**Requested by:** Brady (mariel)
**What:** End-to-end research on the ADME File Service v2 upload flow. API facts and recommended defaults for a v1 file-upload page. Page design is out of scope.

---

## 1. End-to-end flow (canonical for ADME)

The canonical flow on Microsoft ADME is **File Service v2**. There is no v3 in the ADME-supported surface — `/api/file/v2/*` is what Microsoft documents and what the `csv-parser` / `Osdu_ingest` DAGs consume. The OSDU community repo still hosts an R2 prototype README with `POST /v2/getLocation` and `POST /v2/getFileLocation`, but those are not the endpoints ADME exposes. **Use v2 `uploadURL` + `metadata`.** The `dataset` service exists in OSDU but ADME does not surface it as a primary upload path; ignore for v1.

Three steps, in order:

1. **GET** `https://{DNS}/api/file/v2/files/uploadURL` (Authorization + data-partition-id) → response contains `SignedURL` and `FileSource`.
2. **PUT** the file bytes to `SignedURL`. Header `x-ms-blob-type: BlockBlob` is **required** on Azure. No Bearer token; SAS in the query string is the auth.
3. **POST** `https://{DNS}/api/file/v2/files/metadata` with a Storage record whose `data.DatasetProperties.FileSourceInfo.FileSource` equals the `FileSource` from step 1. Response is `{id, ...}` — that `id` is the searchable Storage record id (kind `osdu:wks:dataset--File.Generic:1.0.0`).

Sources:
- Microsoft Learn — *Tutorial: Perform CSV parser ingestion* (steps 3, 4, 5) confirms all three calls verbatim. https://learn.microsoft.com/en-us/azure/energy-data-services/tutorial-csv-ingestion
- Microsoft Learn — *CSV parser ingestion concepts* references the same File Service v2.
- Azure Storage REST — *Put Blob* confirms the `x-ms-blob-type` requirement.

---

## 2. Endpoint specs

### 2.1 GET `/api/file/v2/files/uploadURL`

- **Method/path:** `GET /api/file/v2/files/uploadURL`
- **Headers (required):**
  - `Authorization: Bearer <token>`
  - `data-partition-id: <partition>` (lowercase header name in MS Learn; ADME is case-insensitive but match the doc)
- **Request body:** none.
- **Response (200) — observed ADME shape (flat):**
  ```json
  {
    "SignedURL": "https://<acct>.blob.core.windows.net/<container>/<blob>?sv=...&sig=...",
    "FileSource": "/<partition>/<yyyy>/<MM>/<dd>/<uuid>"
  }
  ```
  Note: older OSDU community docs (R2 prototype) show a nested `{Location: {SignedURL}}` envelope. ADME's response is **flat** per the Microsoft Learn tutorial. Code defensively: read `body.get("Location", body)` then pull `SignedURL` / `FileSource`. Some ADME builds also include `FileID`, `CreatedBy`, `CreatedAt` — ignore unless needed.
- **Errors:**
  - `401 Unauthorized` — missing/expired Bearer.
  - `403 Forbidden` — caller lacks `users.datalake.editors` or equivalent file-service write group.
  - `400 Bad Request` — missing/invalid `data-partition-id`.
  - `404 Not Found` — wrong partition slug (ADME returns 404 instead of 400 in some builds — surface both).
- **SAS lifetime:** signed URL valid up to **7 days** per OSDU spec; ADME default is shorter (≈1 hour). Don't cache.
- **Size limit:** none on this call; this only allocates a slot.

### 2.2 PUT `<SignedURL>` (Azure Blob Storage, not ADME)

- **Method/path:** `PUT <SignedURL>` (full URL from step 1, including SAS query).
- **Headers (required for Azure):**
  - `x-ms-blob-type: BlockBlob` — **mandatory.** Without it Azure returns `400 Bad Request — One of the request inputs is not valid` or `411 Length Required`.
  - `Content-Length: <bytes>`
- **Headers (recommended):**
  - `Content-Type: <mime>` — forward the file's MIME (e.g. `text/csv`, `application/zip`). If unknown, use `application/octet-stream`. Azure stores it and returns it on GET. Microsoft's tutorial omits `Content-Type` and that works too — Azure defaults to `application/octet-stream`.
- **Do NOT add:** `Authorization` header. The SAS in the query string IS the auth. Adding a Bearer token causes Azure to return `403 AuthenticationFailed — Server failed to authenticate the request`.
- **Request body:** raw file bytes.
- **Response (201 Created):** empty body. Headers include `ETag`, `Content-MD5`, `x-ms-request-id`.
- **Size limits (Azure block blob via single Put Blob, REST version 2019-12-12+):**
  - Hard max single-PUT: **5,000 MiB** (≈5.24 GB).
  - Practical recommendation: **chunk anything > 100 MiB** using Put Block + Put Block List. Single-PUT works up to ~256 MiB reliably; beyond that, network failures cause full restart.
  - For v1: cap at 100 MB single-PUT (see §7).
- **Errors on PUT:**
  - `403 AuthenticationFailed` — SAS expired or malformed; client added Bearer.
  - `400 InvalidHeaderValue` — missing/wrong `x-ms-blob-type`.
  - `413 Request Entity Too Large` — body exceeded service-version max.
  - `409 Conflict` (rare) — concurrent overwrite with lease.
- Source: https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob

### 2.3 POST `/api/file/v2/files/metadata`

- **Method/path:** `POST /api/file/v2/files/metadata`
- **Headers (required):**
  - `Authorization: Bearer <token>`
  - `data-partition-id: <partition>`
  - `Content-Type: application/json`
- **Request body** (verbatim from MS Learn tutorial, parameterized):
  ```json
  {
    "kind": "osdu:wks:dataset--File.Generic:1.0.0",
    "acl": {
      "viewers": ["data.default.viewers@<partition>.dataservices.energy"],
      "owners":  ["data.default.owners@<partition>.dataservices.energy"]
    },
    "legal": {
      "legaltags": ["<partition>-<TagName>"],
      "otherRelevantDataCountries": ["US"],
      "status": "compliant"
    },
    "data": {
      "DatasetProperties": {
        "FileSourceInfo": {
          "FileSource": "<value from step 1>",
          "Name": "wellbore.csv"
        }
      }
    }
  }
  ```
  Optional additions inside `data` that downstream tooling expects: `Name`, `Description`, `TotalSize` (string-of-bytes), `EncodingFormatTypeID` (e.g. `text/csv`), `SchemaFormatTypeID`. None are required by the metadata POST itself, but they make the record useful in Search.
- **Response (201):**
  ```json
  { "id": "<partition>:dataset--File.Generic:<uuid>", "status": "Created" }
  ```
  The `id` is the Storage record id. It is searchable after the indexer catches up (seconds to minutes). It is the value to embed as a `Datasets[*]` reference in a larger manifest if/when the operator goes that route.
- **Errors:**
  - `400` — schema-validation failure (most common: missing legal/acl, wrong `kind`, malformed `FileSource`, bad legal tag slug).
  - `401` / `403` — auth / entitlements.
  - `409` — duplicate record id (only if caller supplies `id`).

---

## 3. Signed URL PUT — quirks to handle

1. **`x-ms-blob-type: BlockBlob` is mandatory.** Already covered. Single biggest source of "why did my PUT fail" tickets.
2. **No Bearer on PUT.** SAS query string is the auth. The HTTP client must NOT inherit the Bearer from the surrounding session. In `requests`, build a fresh `Session` or call `requests.put(url, data=..., headers={...})` without merging default headers; `httpx.Client` defaults must be cleared.
3. **Single-PUT threshold:** Use single PUT for ≤ 100 MiB. Beyond that, switch to staged blocks (`PUT block` + `PUT block list`). The Microsoft tutorial and the `osdu-data-load-tno` loader both use single-PUT for small files only.
4. **Content-Type:** forward the source MIME when known. Falling back to `application/octet-stream` is safe and the Azure default if header omitted. The MIME stored on the blob is informational — ADME indexing keys off `EncodingFormatTypeID` in the metadata record, not the blob's Content-Type.
5. **Don't send `Content-Encoding`** unless the bytes are pre-compressed; ADME's downstream parsers do not transparently decode.
6. **Retry on PUT:** signed URL is stable until SAS expiry. Retries with exponential backoff are safe (Azure idempotent on full overwrite). If SAS expired, must restart from step 1.
7. **Stream, don't buffer.** A 100 MB file should be streamed (`requests` with file handle or `httpx` with `content=file`). Buffering doubles memory.

---

## 4. FileSource format

`FileSource` returned by ADME File Service v2 is an opaque string. Observed shapes:

```
/<partition>/<yyyy>/<MM>/<dd>/<uuid>
```

Example: `/opendes/2026/05/11/3f2c1a8b-9f3d-4c7e-bb70-2e1ad8f9c001`

**Treat it as opaque.** Pass it verbatim into the metadata POST body. Do not parse, prefix, or normalize. The File Service internally maps this to a permanent blob location separate from the SAS-signed staging location.

It is **the same value** that goes into `Manifest.Datasets[*].data.DatasetProperties.FileSourceInfo.FileSource` for full manifest ingestion via Workflow Service.

---

## 5. Manifest integration — two patterns

**Pattern A — File-only ingestion (recommended for v1):**

GET uploadURL → PUT bytes → POST metadata. Done. The result is a Storage record of kind `osdu:wks:dataset--File.Generic:1.0.0` that is discoverable via Search. No Workflow Service involvement. No DAG. Operator gets a usable file id (`<partition>:dataset--File.Generic:<uuid>`) in three calls.

This is what Microsoft's CSV tutorial uses as the file-prep step and what the `csv-parser` DAG consumes. For an operator who just wants "drop a file into the platform," this IS the flow.

**Pattern B — Full manifest ingestion via Workflow Service:**

Operator uploads the file (steps 1–3 as above to get the `FileSource`), then constructs a larger manifest with `ReferenceData` / `MasterData` / `Data.Datasets[*]` containing the same metadata record (or a reference to it) plus WorkProductComponents and a WorkProduct, and POSTs to `/api/workflow/v1/workflow/Osdu_ingest/workflowRun`. This is the existing Ingestion page's territory.

**Recommendation for v1: do Pattern A only.** Reasons:
- Complete in a single page, three calls, no orchestration polling.
- Existing Ingestion page covers Pattern B and can be taught to accept a pre-uploaded file's `id` (or its `FileSource`) as input — that's a small follow-up, not blocking v1.
- Pattern A's output IS the input most operators want when their next move is "now search for it" or "now reference it from a manifest."

---

## 6. Auth model

- **`uploadURL` GET and `metadata` POST:** standard ADME auth — Bearer access token + `data-partition-id`. Works with **either** user-impersonation (MSAL interactive / device code) or service principal (client credentials), provided the principal is a member of `users.datalake.editors` (or equivalent partition-level writer group) and the partition's File Service entitlements. The existing app already supports both auth modes via the connection state.
- **SignedURL PUT:** auth is the SAS embedded in the query string. Identity of the original requester is **not** carried into the PUT. The PUT will succeed for whichever process holds the URL within its expiry window. Treat the URL as a credential — log only the host, never the query string.
- **Practical recommendation:** for v1, use whatever auth mode the connection is already configured with. No special handling needed. The signed URL operation is identity-agnostic.

---

## 7. Recommended defaults for v1

| Setting | Value | Rationale |
|---|---|---|
| Default `kind` | `osdu:wks:dataset--File.Generic:1.0.0` | Microsoft-documented; matches CSV tutorial and TNO loader. Generic-enough catch-all. |
| Max single-PUT file size | **100 MB** | Below Azure's recommended chunk threshold. Larger files = explicit "future work" banner with guidance to use Azure Storage Explorer + manual metadata POST. |
| Chunked upload | **Out of scope for v1.** | Implementing Put Block / Put Block List with progress + resume is a separate workstream. |
| Content-Type | Detected via `mimetypes.guess_type(filename)`, fallback `application/octet-stream` | Standard. Forwarded on PUT and stored in metadata record's `EncodingFormatTypeID`. |
| ACL viewers/owners | Same selectboxes as Ingestion page (`data.*.viewers@<partition>.dataservices.energy` / `...owners@...`) | Consistency with existing UX. Default to `data.default.viewers` / `data.default.owners`. |
| Legal tag | Same selectbox as Ingestion page; list valid tags from `/api/legal/v1/legaltags` and require selection | Mandatory in metadata POST. Tag name must be fully-qualified `<partition>-<TagName>`. |
| Required form fields | local file, kind (preselected), ACL viewer, ACL owner, legal tag, optional Name/Description | Minimum to get a clean metadata record. |
| Response surfaced to operator | Final Storage record `id`, `FileSource`, blob URL host (no SAS), upload size, MIME | Operator needs the `id` to reference downstream. Hide SAS. |

---

## Cross-references

- Microsoft Learn — CSV parser tutorial (canonical curl for all three calls): https://learn.microsoft.com/en-us/azure/energy-data-services/tutorial-csv-ingestion
- Microsoft Learn — Manifest ingestion concepts: https://learn.microsoft.com/en-us/azure/energy-data-services/concepts-manifest-ingestion
- Azure Storage — Put Blob spec (`x-ms-blob-type`, size limits): https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob
- OSDU File service repo (legacy R2 prototype docs — **do not use endpoints from here for ADME**): https://community.opengroup.org/osdu/platform/system/file
- Azure TNO loader (real-world file upload sequence): https://github.com/Azure/osdu-data-load-tno

**Why:** Grounds the v1 File Upload page design in confirmed ADME endpoint behavior, eliminates v2/v3 ambiguity, and pins the single-PUT size threshold and default kind/ACL/legal patterns so the page implementer doesn't have to re-research.

---

### 2026-05-11: OSDU Search & Storage API facts for the Operate → Search page
**By:** Darryl (OSDU/ADME platform expert) — requested by Brady (mariel)
**Why:** Satya needs an authoritative API contract before drafting the Search page service module. This is the API-shape brief; UI / service design is out of scope and left to Satya and Kevin.

**Source authority** (all consulted at write time):
- OSDU community Search Service tutorial, release 0.15 — `community.opengroup.org/osdu/platform/system/search-service/.../docs/tutorial/SearchService.md`
- OSDU Storage Service tutorial (M25 surface, mirrored in our local `.github/extensions/osdu-api/extension.mjs` reference)
- Microsoft ADME release notes (Jan 2026 wildcard rate-limit guardrail)
- ADME indexing & search workflow concepts (`learn.microsoft.com/azure/energy-data-services/concepts-index-and-search`)
- Confirmed shape used in-house by `app/services/verification.py::search_records_by_kind` (POST `/api/search/v2/query` with `kind`, `limit`, `offset`)

> Naming note: the tutorial calls the parameter `query` (not `query_string`). Our existing call in `verification.py` omits `query` entirely (kind-only search) — that is valid and returns all records of the kind.

---

## 1. `POST /api/search/v2/query` — full request body schema (v1)

**Endpoint:** `POST {endpoint}/api/search/v2/query`
**Required headers:**
- `Authorization: Bearer {token}`
- `Data-Partition-Id: {partition}` (case-insensitive in practice; OSDU docs use `Data-Partition-Id`, our code uses `data-partition-id` — both work)
- `Content-Type: application/json`
- `Accept: application/json`
- Optional: `Correlation-Id: {guid}` — strongly recommended for support tickets

**Required role:** `users.datalake.viewers` (or editors/admins).

**Request body fields:**

| Field | Type | Required | Notes |
|---|---|---|---|
| `kind` | string OR string[] | **yes** | `authority:source:entity:version`. Supports wildcards per segment: `*:*:*:*`, `opendes:*:well:1.0.0`, `opendes:welldb:*:*`. Can also be passed as comma-separated string or JSON array for multi-kind. |
| `query` | string | no | Lucene query syntax. Default OR. Omit to match all records of the kind. |
| `limit` | int | no | **Default 10. Max 1000.** For larger pages, use `query_with_cursor`. |
| `offset` | int | no | Starting offset. **HARD CONSTRAINT: `offset + limit` must be ≤ 10,000.** Beyond that, server rejects — use cursor. |
| `sort` | object | no | `{ "field": ["data.X", "_score"], "order": ["ASC","DESC"] }`. Lengths must match. Case-insensitive. Default order is by `_score DESC` (relevance). |
| `returnedFields` | string[] | no | Field projection. Use to keep payload small for list views. Note: the `index` meta block is **excluded by default**; you must request `"index"` explicitly if you want indexing-status info. |
| `aggregateBy` | string | no | Returns unique values + counts for a single field. Max 1000 unique values. Supports nested syntax. |
| `trackTotalCount` | bool | no | **Default false.** When false, server caps reported total at 10,000 even if true count is higher. Set `true` for an accurate `totalCount` — slower. |
| `queryAsOwner` | bool | no | Default false. True restricts results to records the caller owns. |
| `spatialFilter` | object | no | `byBoundingBox` / `byDistance` / `byGeoPolygon`. Out of scope for v1 list view. |
| `cursor` | — | n/a | Not valid on `/query`. Cursor lives on `/query_with_cursor` only. |

### 1a. Lucene `query` syntax — what's actually supported (verified examples)

```jsonc
// Free-text across all queryable fields
{ "kind": "*:*:*:*", "query": "well" }

// Field-scoped — note 'data.' prefix is required for record payload fields
{ "kind": "osdu:wks:master-data--Well:1.0.0", "query": "data.SpudDate:[2020-01-01 TO 2024-12-31]" }

// OR / AND / NOT (uppercase, case-sensitive operators)
{ "query": "data.Rig_Contractor:(Ocean OR Drilling) AND data.Status:Active" }

// Exact phrase
{ "query": "data.Rig_Contractor:\"Ocean Drilling\"" }

// Exists check
{ "query": "_exists_:data.Status" }

// Range — inclusive [ ] / exclusive { }
{ "query": "data.ProjDepth:(>=10 AND <20)" }

// Wildcards in values — ? = one char, * = zero or more
{ "query": "data.Well_Name:Eag*" }   // TRAILING wildcards only
// Leading wildcards (e.g. "*eagle") are DISABLED by OSDU Search.

// Nested arrays
{ "query": "nested(data.Markers, (MarkerMeasuredDepth:>10000))" }
```

**Reserved chars** that must be escaped with `\`: `+ - = && || ! ( ) { } [ ] ^ " ~ * ? : \ /`
(`<` and `>` cannot be escaped — strip them or use the explicit range syntax instead.)

**Date format** for date-range queries: ISO 8601 (`2024-12-29T00:00:00.000Z` or `2024-12-29`).

### 1b. `sort` — what fields are reliable

Sort supports: `string`, `int`, `float`, `double`, `long`, `datetime`, nested object, nested array of objects.
Sort **does not support**: array of strings, geo-point, geo-shape.

**`createTime desc` reliability:** Records produced by Storage carry `createTime` at the top level (not inside `data.`). It is a datetime field and is sortable. However, OSDU Search does **not validate** the field exists before sorting — different kinds may type the same field differently. For a cross-kind (`*:*:*:*`) browse list, `createTime DESC` is reliable in ADME because Storage stamps it consistently on every record at creation time.

```jsonc
{ "kind": "*:*:*:*", "sort": { "field": ["createTime"], "order": ["DESC"] } }
```

**Warning:** broad-kind sorts (e.g. `*:*:*:*` + sort) are expensive. Server timeout is 60s; you'll see HTTP 504 "Request timed out after waiting for 1m" on cold/large partitions. For the v1 browse list this is acceptable — fall back to relevance order (omit `sort`) if 504s appear.

### 1c. `aggregateBy` — exists, syntax

Yes. Returns unique values + per-value counts for one field, up to 1000 unique values.

```jsonc
{ "kind": "*:*:*:*", "aggregateBy": "kind" }
// → response includes "aggregations": [ { "key": "...", "count": N }, ... ]
```

Also works for `data.*` fields and for nested arrays via `aggregateBy: "nested(data.Markers, MarkerMeasuredDepth)"`.

### 1d. `totalCount` — reliable or approximate?

**Approximate by default.** With `trackTotalCount` omitted or `false`, the server caps the reported count at **10,000** (returns exactly `10000` when there are more). For accurate counts, pass `trackTotalCount: true` — at the cost of performance. Our `SearchResult` model already falls back to `len(results)` when `totalCount` is missing, which is correct defensive behavior.

For the browse view, prefer the cheap path: do NOT set `trackTotalCount`. Surface the count in the UI with a "≥" indicator when it equals 10,000 (e.g., "10,000+ matches").

---

## 2. Listing available kinds in the partition (with counts)

**Two options. Recommend option A for v1.**

### Option A — Search aggregation (recommended)

```http
POST /api/search/v2/query
{
  "kind": "*:*:*:*",
  "aggregateBy": "kind",
  "limit": 0
}
```

Returns up to 1000 distinct kinds + per-kind document counts in the `aggregations` array. One round-trip. Includes counts. Honors entitlements (only kinds the caller can see).

⚠️ **ADME wildcard rate limit (Jan 2026):** the fully-unbounded `*:*:*:*` pattern on `/query` and `/query_with_cursor` is throttled in ADME — burst of 2 tokens, refill 1 token / 5s, ≈12 such calls per minute per caller. Exceeding it returns **HTTP 429**. For the kinds-discovery call this is fine (we run it once on page load / explicit refresh). Cache the result in session state. Do NOT re-issue on every keystroke.

### Option B — Storage `/query/kinds`

```http
GET /api/storage/v2/query/kinds?limit=100
```

Returns kinds with cursor pagination. **No counts.** Useful as a fallback if the partition is in the 429 throttle window or aggregation is rejected. v1 can skip this; document for v2.

---

## 3. `GET /api/storage/v2/records/{id}` — fetch full record

**Endpoint:** `GET {endpoint}/api/storage/v2/records/{id}`
**Required role:** `users.datalake.viewers` plus data-group ACL viewer on the record.

**Headers:** same `Authorization` + `Data-Partition-Id`. No request body.

**URL encoding rules for the `{id}`:**
- Record id format: `{partition}:{kind-entity-or-source}:{unique-id}` — contains colons (`:`) and potentially periods, hyphens, slashes in the unique-id segment.
- Colons (`:`) are **sub-delim** reserved chars in URI paths but ARE allowed unencoded inside a path segment per RFC 3986. In practice OSDU/ADME accepts them unencoded — our codebase elsewhere uses raw colons.
- **Always pass `id` through `urllib.parse.quote(id, safe=":")`** before composing the URL. This encodes any `/`, `#`, `?`, `%`, spaces, or non-ASCII while leaving colons readable. Without this, IDs containing `/` (rare but legal in some authority-encoded ids) break path routing.
- Do NOT use `quote_plus` — it encodes spaces as `+`, which is wrong for path segments.

```python
from urllib.parse import quote
url = f"{endpoint}/api/storage/v2/records/{quote(record_id, safe=':')}"
```

**Optional query params:**
- `attribute` — repeatable; filter which top-level fields to return. Useful for detail-view perf, but for v1 we want the full record so omit.

**Response shape (200):** full record object — `id`, `version`, `kind`, `acl`, `legal`, `data`, `meta`, `tags`, `ancestry`, `createTime`, `createUser`, `modifyTime`, `modifyUser`.

**404 behavior:** OSDU returns `404 Not Found` with a JSON body `{"code": 404, "reason": "Not Found", "message": "Record with id '...' was not found"}`. This is also what you get if the record exists but the caller lacks ACL view rights on it — the API intentionally does NOT distinguish, to avoid leaking record existence. Treat 404 as "not visible to you" in the UI rather than "definitely doesn't exist".

**With-version path:**
```
GET /api/storage/v2/records/{id}/{version}
```
Where `{version}` is the numeric version (int64, from `recordIdVersions` in the create response or from `GET /records/versions/{id}` which lists them). v1 of the Search page does not need this — default `GET /records/{id}` returns the latest version. Document for v2.

**Other useful related endpoints (informational, not for v1):**
- `POST /api/storage/v2/query/records` — batch fetch up to 100 records by id. Use if/when we add multi-select fetch.

---

## 4. Pagination strategy recommendation for v1

**Recommendation: offset-based for v1. Move to cursor in v2 if/when we add "load all" / export.**

| | Offset (`/query`) | Cursor (`/query_with_cursor`) |
|---|---|---|
| Max page size | 1000 | 1000 |
| Total reachable | offset+limit ≤ **10,000** | unbounded |
| Stateless | yes | no — server holds context for 1 min |
| UI semantics | "page N of M" | "next page" only |
| Stable snapshot | no (live index) | yes (snapshot at first call) |
| Re-entrant / shareable | yes (URL params) | no |
| Cursor expiry | n/a | 1 minute, refreshed each call |

For a **browse view of 100-record pages**, OSDU fully supports `limit: 100, offset: N*100` up to N=99 (10,000 records). That covers any practical first-pass browse. The 10,000 ceiling is a known OSDU/Elasticsearch limit — beyond that the server rejects.

**Concrete v1 plan:**
- Page size **100**, `offset` increments of 100, hard-stop at `offset = 9900`.
- If `totalCount` reports `10000+` and the user paginates to the end, show a banner: *"Showing first 10,000 results. Narrow your kind or query to see more."*
- Defer cursor pagination until we add an "Export visible results" feature — that's the right time to take on the 1-minute session lifecycle.

---

## 5. Defaults for the Search page (operator-friendly)

```python
# app/services/search.py (proposed module — Satya owns the file)
SEARCH_QUERY_PATH = "/api/search/v2/query"

DEFAULT_SEARCH_DEFAULTS: dict = {
    "kind": "*:*:*:*",            # all kinds the caller can see
    "limit": 100,                 # one screenful
    "offset": 0,
    "sort": {                     # newest first — reliable on Storage-stamped createTime
        "field": ["createTime"],
        "order": ["DESC"],
    },
    "returnedFields": [           # list-view projection; keeps payload small
        "id",
        "kind",
        "createTime",
        "modifyTime",
        "version",
    ],
    # trackTotalCount intentionally omitted — fast path, cap at 10k
    # query intentionally omitted — empty string is NOT valid Lucene; omit the key entirely
}

SEARCH_MAX_PAGE_SIZE = 1000          # OSDU hard cap
SEARCH_OFFSET_CEILING = 10_000       # offset+limit must stay ≤ 10000
SEARCH_KINDS_AGGREGATION_BODY = {    # one-shot kinds discovery
    "kind": "*:*:*:*",
    "aggregateBy": "kind",
    "limit": 0,
}
```

**Operator-friendly behaviors to wire into the page (specifying API surface, not UI):**

1. **Empty query box → omit `query` entirely.** Do not send `"query": ""` — Lucene rejects empty strings on some indexers. Omitting yields a pure kind-scoped match-all, which is what the user expects.
2. **Kind dropdown defaults to `*:*:*:*`** but is replaced with the literal selected kind once the user picks one — narrowing avoids the wildcard rate-limit window.
3. **Lucene escaping helper:** when the user types into a "simple search" box (not a "raw Lucene" mode), apply backslash-escaping to the reserved set `+ - = && || ! ( ) { } [ ] ^ " ~ * ? : \ /` before composing `data.*:user-text`. Two input modes — "simple" (escaped) and "advanced" (raw, user assumes responsibility).
4. **Timeout:** stay on 30s shared `INGESTION_TIMEOUT_SECONDS` (we already learned 5s is too tight on cold partitions; same logic applies to a sorted `*:*:*:*` first hit).
5. **429 handling:** show a friendly "ADME is rate-limiting unbounded queries — pick a specific kind to continue" message rather than a raw retry, when the unbounded `*:*:*:*` pattern returns 429. Do NOT auto-retry the wildcard.
6. **404 from `GET /records/{id}`:** treat as "Record not found or not visible" — never as a hard error in the detail panel.

---

## Open questions punted to v2

- Cursor-based paging for export / "fetch all" flows.
- Versioned record viewer (`/records/{id}/{version}`) — list versions via `/records/versions/{id}`, render diff.
- Spatial filters in the UI (bbox / radius).
- `queryAsOwner` toggle (Operate persona may want "only mine").
- Saved searches / shareable query URLs.

— Darryl

---

### 2026-05-12: TNO Full-Dataset Loader — Research Contract for Kevin (Backlog #4)

**By:** Darryl (OSDU/ADME Expert)
**Requested by:** Mariel
**Scope:** Pure research. Answers questions for sub-pieces 4a (manifest archive) and 4b (open-test-data acquisition), and informs 4c (dependency ordering). No code.
**Primary source:** [Azure/osdu-data-load-tno](https://github.com/Azure/osdu-data-load-tno) (Apache-2.0, C# loader, last touched 9–10 months ago).
**Underlying data source:** [OSDU Forum GitLab — open-test-data](https://community.opengroup.org/osdu/data/open-test-data) (public, anonymous).

---

## Section 1 — Manifest archive (sub-piece 4a)

### Q1. What are the manifest categories? What's the file layout?

**Answer:** Manifests are **NOT vendored as a curated tree inside the Azure repo.** They are **generated at load time** from CSV templates that ship inside the open-test-data GitLab archive (under `rc--3.0.0/` or `rc--1.0.0/`, depending on the loader version target).

The Azure loader's `src/` tree has only two manifest-related directories `[source: api/contents/src]`:

```
src/
├── OSDU.DataLoad.Application/      # CQRS handlers (Download, LoadAll, etc.)
├── OSDU.DataLoad.Console/          # CLI entry + appsettings.json
├── OSDU.DataLoad.Domain/
├── OSDU.DataLoad.Infrastructure/   # OsduHttpClient, Azure Identity
└── generate-manifest-scripts/      # Python — csv_to_json.py + csv_to_json_wrapper.py
```

The README explicitly says: *"The manifest generation is extremely complex - it was so complex that porting it to C# proved infeasible. Instead, the original python scripts are used."* `[source: docs/DATA_LOAD_PROCESS.md L21-L23]`

So the architecture is: **C# orchestrator shells out to Python** (`csv_to_json_wrapper.py`) which reads CSVs from the downloaded open-test-data tree and emits per-row manifest JSON files.

**Categories the loader processes, in submit order** `[source: docs/DATA_LOAD_PROCESS.md — Step 6]`:

1. Reference Data
2. Misc Master Data
3. Wells
4. Wellbores
5. Documents
6. Well Logs
7. Well Markers
8. Wellbore Trajectories
9. Work Products

**Counts:** Not stated in README. The CSV-row count drives manifest count per category — we will only know exact numbers by inspecting the downloaded archive. Order-of-magnitude expectation from OSDU community knowledge: hundreds of reference-data entities, ~100 wells, ~200 wellbores, hundreds–thousands of logs/markers/trajectories, dozens of work products.

### Q2. Static JSON vs CSV-generated?

The split is:

| Category | Source shape | How produced |
|---|---|---|
| Reference Data | CSV + JSON template | Generated per-row via `csv_to_json.py` |
| Misc Master Data | CSV + JSON template | Generated per-row |
| Wells, Wellbores | CSV + JSON template | Generated per-row |
| Well Logs, Markers, Trajectories | CSV + JSON template | Generated per-row |
| Documents | File metadata (mostly) | Produced from file-upload registry |
| **Work Products** | **Static JSON templates** | Per-WP folder JSON, **updated** with uploaded file IDs, ACL, legal tag, partition `[source: docs/DATA_LOAD_PROCESS.md — Step 5]` |

So Work Products are the only "static JSON" category — and even those get patched at runtime. **Everything else is CSV-driven.**

### Q3. Placeholders — what tokens?

The Python wrapper accepts these substitutions as CLI args `[source: src/generate-manifest-scripts/csv_to_json_wrapper.py L23-L36]`:

- `--acl-viewer` → fills ACL viewer list
- `--acl-owner` → fills ACL owner list
- `--legal-tag` → fills legal tag name
- `--schema-ns-name` (default literal: `<namespace>`) → string to find
- `--schema-ns-value` → string to replace it with (this is the **data partition** substitution, but named generically because it's also used for schema namespace rewrites)

**Token format differs from ours.** TNO uses `<namespace>` (angle brackets, single token name) — generic find/replace, not the `{{DATA_PARTITION_ID}}` Jinja-style we use in [`app/services/manifest_builder.py`](app/services/manifest_builder.py).

**Implications for Kevin:**
- Our `substitute_manifest_placeholders` won't be a drop-in. Either (a) pre-process TNO manifests to convert `<namespace>` → `{{DATA_PARTITION_ID}}`, or (b) extend the substitutor to accept TNO's token style as an alias.
- ACL viewer/owner and legal tag concepts are the same — just different surface syntax in the templates.
- No extra tokens beyond those four. Confirmed from the wrapper signature.

### Q4. Vendor vs fetch-on-demand?

| Approach | Pros | Cons |
|---|---|---|
| **Vendor a pinned snapshot** | Reproducible. Offline-capable. CI cache-friendly. Apache-2.0 allows it. Can pre-convert tokens to our style. | Repo bloat — CSVs alone are tens of MB; full archive ~2.2 GB. Drift risk if upstream releases fix data. Must re-snapshot to update. |
| **Fetch on demand into a local cache** | Repo stays lean. Always current (or pinnable to a tag). Matches the Azure loader's behavior — they download every run unless `--overwrite` skipped. | Requires network at first run. Cache invalidation logic needed. Token-style conversion happens at runtime. |

**Darryl's recommendation: hybrid — vendor only the templates + CSVs (the *"recipe"*, ~small MB), fetch the file payloads on demand.** This is the natural seam:

- **Vendored under `app/data/tno-manifests/{version}/`:** CSV templates, JSON templates for work products, schema files. Pin to `rc--3.0.0` tag (latest stable). Convert `<namespace>` → `{{DATA_PARTITION_ID}}` at vendor time so our existing substitutor works unmodified.
- **Fetched on demand into `~/.adme-ingestion/cache/tno/{version}/`:** the heavy file payloads (seismic, log curves, etc.) under `1-data/`.

This makes "load reference data only" (see Q12) trivial — zero network, no large download — while keeping the full-fat path viable.

---

## Section 2 — Open Test Data acquisition (sub-piece 4b)

### Q5. Where does the data live?

**Primary source:** `https://community.opengroup.org/osdu/data/open-test-data/-/archive/master/open-test-data-master.zip` `[source: docs/CONFIGURATION.md — OSDU_TestDataUrl]`

This is the OSDU Forum's self-hosted GitLab (the OSDU Forum is the open-source steward; "community.opengroup.org" is operated by The Open Group). The default `TestDataUrl` in `appsettings.json` points at the `master` branch archive endpoint, which returns a generated zip of HEAD.

**Better long-term pinning:** Use the tag `rc--3.0.0` instead of `master` to avoid the moving target:
`https://community.opengroup.org/osdu/data/open-test-data/-/archive/rc--3.0.0/open-test-data-rc--3.0.0.zip`

The `rc--3.0.0` tag was the most recent merge on the `master` branch as of Nov 2025 `[source: open-test-data README — branches list]`.

There is **also** an AWS S3 mirror for the heavy seismic/Volve payloads:
`s3://osdu-seismic-test-data/` (public-read, `--no-sign-request`) `[source: open-test-data README — "TNO / Volve Dataset"]`. The Azure loader does not use this. We probably won't need it for v1.

### Q6. Size breakdown

README quotes ~2.2 GB total `[source: README.md — Available Commands]`. The README does NOT publish a per-category breakdown. Based on the repo layout (`1-data/`, `4-instances/`, `3-schemas/`, `2-scripts/`):

- **`4-instances/` — manifests + CSV templates:** few hundred MB at most (small JSON/CSV)
- **`3-schemas/`:** small (a few MB of JSON schemas)
- **`2-scripts/`:** trivial
- **`1-data/` — the file payloads:** the bulk. Well logs (LAS/DLIS), seismic samples, document binaries.

The breakdown is **not authoritative in the README** — call this out for Mariel. We'd need to measure the unzipped tree to publish real numbers. Order-of-magnitude: ~2 GB of that ~2.2 GB lives under `1-data/`.

### Q7. Smallest meaningful subset

**Yes — reference data + master data can be loaded WITHOUT the file payloads.** Reference data and master data manifests only point to *records*, not to files. Wells and Wellbores have no file attachments at the master-data level. File attachments enter the picture only for **work-product components** (Well Logs, Documents, Markers, Trajectories) — those reference uploaded file IDs.

**Tiered subsets we could ship:**

| Tier | What | Approx size | Demo value |
|---|---|---|---|
| **Tier 0 — Reference only** | Reference Data manifests (vendored CSVs/templates only) | < 50 MB on disk | Proves manifest-batch plumbing. No files, no work products. |
| **Tier 1 — Reference + Master** | + Wells + Wellbores | < 100 MB | Proves cross-manifest dependencies (wellbore → well). |
| **Tier 2 — + Documents** | + uploaded document files + WP/WPC manifests | + few hundred MB | Proves the file-upload + WP linkage. |
| **Tier 3 — Full** | All 9 waves incl. logs, markers, trajectories, seismic-adjacent | ~2.2 GB | Full TNO experience. |

### Q8. Auth on the data source

**Public, anonymous.** The GitLab archive URL is a plain HTTP GET with no auth. The C# loader's `DownloadDataHandler` issues an anonymous HTTP request to `OSDU_TestDataUrl` `[source: README — appsettings.json keys; loader does not configure GitLab credentials anywhere]`.

Azure CLI login (`az login`) is required only for **OSDU API calls** (uses Azure Identity for `Bearer` tokens) — NOT for the data download. Two distinct auth contexts.

### Q9. File layout after download

Top-level inside the unzipped archive (per the open-test-data README):

```
open-test-data-master/  (or open-test-data-rc--3.0.0/)
├── rc--3.0.0/
│   ├── 1-data/         # raw file payloads (LAS, segy, docs, etc.)
│   ├── 2-scripts/      # data-prep helpers (not used by loader)
│   ├── 3-schemas/      # OSDU schemas as JSON
│   └── 4-instances/    # manifest templates + CSV inputs
│       ├── ReferenceData/
│       ├── MasterData/
│       │   ├── Well/
│       │   └── Wellbore/
│       └── WorkProduct/
└── README.md
```

The Azure loader's `DownloadDataHandler` then **reorganizes** files into `~/osdu-data/tno/` with a structure its handlers expect `[source: docs/DATA_LOAD_PROCESS.md — Step 1 "Extracts and organizes files into expected directory structure"]`. The exact reorg map is not documented in the README — Kevin will need to inspect `DownloadDataHandler.cs` to confirm, or we just adopt our own layout and skip the reorg.

---

## Section 3 — Dependency ordering (informs sub-piece 4c)

### Q10. Submit waves — actual ordering

**Authoritative 9-wave order** `[source: docs/DATA_LOAD_PROCESS.md — Step 6]`:

1. **Reference Data** (foundation lookup data — units, coordinate refs, etc.)
2. **Misc Master Data** (additional dependencies — orgs, fields, etc.)
3. **Wells** (well master data)
4. **Wellbores** (depends on wells)
5. **Documents** (document files — already uploaded in Step 3)
6. **Well Logs** (log files and data)
7. **Well Markers** (geological markers)
8. **Wellbore Trajectories** (directional surveys)
9. **Work Products** (final metadata referencing uploaded files)

Inside each wave, the loader does NOT enforce sub-ordering between sibling records (e.g., it does not order wellbores by parent well). The dependency is satisfied because the **parent wave finished first**. Wellbores 3 reference wells from wave 3 — by the time wellbores submit, all wells exist.

**Two transport mechanisms** are interleaved in the loader, which is important `[source: docs/DATA_LOAD_PROCESS.md mermaid diagram]`:

- The diagram shows `PUT /api/storage/v2/records (batch ≤500)` for the per-wave loop — **direct Storage Service writes, NOT the Workflow Service / `Osdu_ingest` DAG**.
- BUT the config has `MasterDataManifestSubmissionBatchSize=25` for `MiscMasterData / Well / Wellbores` submitted to the **workflow service** `[source: docs/CONFIGURATION.md L1-L8]`.

These are contradictory in the docs. Best reading: the loader uses **direct Storage PUT** for most records (fast, no DAG orchestration overhead) and the **Workflow Service path only for the master-data subset that needs full ingestion-pipeline processing** (validation, enrichment). Kevin should confirm by reading `SubmitManifestsHandler.cs` — call this out as an open question.

**For our app, which uses the Workflow Service / `Osdu_ingest` DAG everywhere:** we already pay the per-submit overhead. We do NOT need to replicate the Azure loader's storage-direct shortcut. Our batches will be smaller (25–50 per workflow run) and we'll fire many runs per wave. That's fine — slower, but matches our existing single-manifest plumbing.

### Q11. Indexing wait between waves?

**The Azure loader does NOT document an explicit indexer wait.** No mention in DATA_LOAD_PROCESS.md, no Search-poll step in the mermaid sequence, no `IndexerWait` config knob in CONFIGURATION.md.

Plausible reasons it works without one:
- Workflow Service runs synchronously enough that by the time the run reports `finished`, records are stored.
- The Storage Service direct PUT path: records are immediately retrievable by ID via `/storage/v2/records/{id}` once written, even before the indexer catches up. Cross-record references (wellbore→well) only require the parent record to *exist* in Storage, not to be *searchable*.
- Indexer lag is typically seconds, and waves take minutes — so by the time wave N+1 submits, wave N is indexed incidentally.

**Recommendation for our app:** Don't add an indexer-poll step in v1. Trust the Workflow Service's `finished` signal. If we see flakiness on cross-wave references later, add an optional Search-poll for a canary record (e.g., poll for "any reference-data record" after wave 1 before starting wave 2) gated behind a `--wait-for-indexing` flag.

---

## Section 4 — Recommendations

### Q12. Phasing — smallest meaningful "TNO loader v1"

**Recommended v1 scope: "Reference Data only, vendored, no file assets."**

Why this cut:
- Proves the **bulk-loader plumbing**: queueing, batching, progress UI, error/retry, run-status polling across N manifests instead of 1.
- Zero network dependency on the GitLab archive — vendored CSVs ship with the app.
- Zero file-upload surface — Reference Data manifests have no file attachments.
- Reuses every existing service unchanged.
- Demo story: *"Loaded N hundred reference records into your partition in M minutes from a single click."*

**v1 deliverables:**
1. Vendored `app/data/tno/rc--3.0.0/reference-data/` (CSVs + templates, tokens pre-converted to `{{DATA_PARTITION_ID}}` style).
2. New page `8_🚚_TNO_Loader.py` with: tier selector (v1 = reference only), preview of what will load (counts per type), "Start load" button, progress table (per-batch status), failure-detail expander.
3. Service `services/tno_loader.py`: generates manifests from vendored CSVs, batches into Workflow Service runs, polls each run, aggregates results.
4. Resume/idempotency: skip records already present (Search by `id`) — optional, behind a checkbox.

**v2 (later):** Add Master Data tier (Wells + Wellbores). Still no files.

**v3 (later):** Add Documents tier — proves file-upload integration into bulk flow.

**v4 (full):** Logs/Markers/Trajectories/WPs — full 2.2 GB story, fetch-on-demand cache, optional.

Each tier ships independently and is feature-gated.

### Q13. Reuse from existing services

Confirmed reusable as-is:

| Service / function | Status | Notes |
|---|---|---|
| `submit_manifest` | ✅ Reuse | One call per batch — same payload shape, just N×ReferenceData entries. |
| `get_workflow_status` | ✅ Reuse | Poll per runId. |
| `substitute_manifest_placeholders` | ✅ Reuse **if** we pre-convert TNO `<namespace>` tokens to `{{DATA_PARTITION_ID}}` at vendor time. Otherwise needs alias support. |
| `validate_manifest_json` (post-loosening) | ✅ Reuse | Validate each generated manifest before submission. Cheap insurance against bad CSV rows. |
| `get_upload_url` + `upload_file_bytes` + `post_file_metadata` | ✅ Reuse — **but only needed from v3 onward** (Documents tier). Not required for v1. |
| `services/auth.py` token handling | ✅ Reuse | Same Bearer flow. |

**New code Kevin will need to write:**
- CSV-to-manifest generator (or thin wrapper around `csv_to_json.py` — we could ship the Apache-2.0 Python file directly under `app/vendor/`).
- Batch orchestrator (queue of manifest groups → workflow runs → aggregate status).
- Streamlit page + progress UI.
- Tier/subset selector.

### Q14. Open questions for Mariel

1. **Vendor the Python `csv_to_json.py`** (Apache-2.0, ~29 KB) **or rewrite in Python natively for our app?** The Azure team explicitly said porting is *"extremely complex"* — strong signal to vendor. Recommendation: vendor under `app/vendor/csv_to_json.py` with a NOTICE entry. ✋ Confirm OK.
2. **Pin to `rc--3.0.0` or track `master`?** Recommendation: pin. ✋ Confirm.
3. **Token style — pre-convert at vendor time, or extend `substitute_manifest_placeholders` to accept `<namespace>` alias?** Recommendation: pre-convert. Cleaner, no runtime branching. ✋ Confirm.
4. **Tier UX — single page with tier selector, or separate page per tier?** Recommendation: single page with selector (matches current page-per-flow pattern but avoids 4 near-duplicate pages). ✋ Confirm.
5. **Idempotency / resume — in v1?** Skipping records that already exist requires Search calls per record (slow) or a local SQLite "loaded-record-ledger" (more code). Recommendation: **skip in v1**, document "re-running will create duplicates if not cleaned up first," add in v2. ✋ Confirm.
6. **Indexer-wait flag — in v1?** Recommendation: no, ship without it; add if flakiness observed. ✋ Confirm.
7. **Where do batch settings live?** Per-load form input, or a config section in Settings page? Recommendation: form input on the loader page with sane defaults (batch=25 matching Azure loader's `MasterDataManifestSubmissionBatchSize`). ✋ Confirm.
8. **Telemetry — should we log per-batch durations + record counts to a local file for performance tuning?** Recommendation: yes, append to `~/.adme-ingestion/load-history.jsonl`. ✋ Confirm.

---

## Citation index

- [README.md](https://github.com/Azure/osdu-data-load-tno/blob/main/README.md)
- [docs/DATA_LOAD_PROCESS.md](https://github.com/Azure/osdu-data-load-tno/blob/main/docs/DATA_LOAD_PROCESS.md) — 6-step process, mermaid diagrams, 9-wave order
- [docs/CONFIGURATION.md](https://github.com/Azure/osdu-data-load-tno/blob/main/docs/CONFIGURATION.md) — env vars, `MasterDataManifestSubmissionBatchSize=25`, `TestDataUrl` default
- [src/generate-manifest-scripts/csv_to_json_wrapper.py](https://github.com/Azure/osdu-data-load-tno/blob/main/src/generate-manifest-scripts/csv_to_json_wrapper.py) — placeholder CLI args
- [src/generate-manifest-scripts/csv_to_json.py](https://github.com/Azure/osdu-data-load-tno/blob/main/src/generate-manifest-scripts/csv_to_json.py) — 29 KB, the generator
- [open-test-data README](https://community.opengroup.org/osdu/data/open-test-data/-/blob/master/README.md) — repo layout, tag list, AWS S3 mirror
- Apache-2.0 license on both repos → vendoring permitted with NOTICE.

---

**Why:** Mariel asked for parallel research so Kevin can start implementing Backlog #4 without rediscovering the wires. This contract answers the 14 questions across manifest archive, data acquisition, ordering, and recommends a v1 scope cut.

---

### 2026-05-12: Bulk Load page — implementation choices
**By:** Judson

Built `app/pages/9_📥_Bulk_Load.py` against Satya §3, Kevin's actual
service contract (`list_datasets`, `preview_tier`, `submit_tier`
generator), and the dataclasses Kevin added in `app/models/osdu.py`
(`DatasetDescriptor`, `DatasetTier`, `ManifestPreview`, `SubmitResult`).
Wired into navigation as the last entry in the Ingest group. 9 page
tests in `tests/test_bulk_load_page.py` — all green.

## What I shipped

- New page: `app/pages/9_📥_Bulk_Load.py` (~700 LOC).
- `app/main.py`: added `BULK_LOAD_PAGE_PATH` and registered the page in
  the Ingest group after Manifest + File.
- `pyproject.toml`: added the new emoji-named page to `[tool.ruff.lint
  .per-file-ignores]` for N999 (matches Settings + Entitlements + ...).
- New tests: `tests/test_bulk_load_page.py` — 9 cases.

## Locked session keys (Charlie tests these)

`bulk_dataset_id`, `bulk_tier`, `bulk_legal_tag`, `bulk_acl_owners`,
`bulk_acl_viewers`, `bulk_preview_seen` (tuple `(dataset_id, tier)` or
`None`), `bulk_preview_results` (`list[ManifestPreview]`),
`bulk_submit_results` (`list[SubmitResult]`), `bulk_last_error`
(`str | None`). Internal helper keys (`bulk_options_autorun_done`,
`bulk_legal_tag_options`, `bulk_acl_owner_options`,
`bulk_acl_viewer_options`) are NOT part of the locked contract.

## UX adjustments vs Satya §3

1. **Tier selector — filter approach.** Satya offered two options
   (per-option disabled formatting OR filter + info block). Streamlit's
   `radio`/`selectbox` doesn't support per-option `disabled=`, so I
   took the filter route: enabled tiers in the radio, disabled tiers
   listed in an `st.info` block below. Cleaner UX, no risk of the
   operator picking a disabled tier and hitting an error. For v1 the
   radio is effectively a 1-option control showing `reference-data`.
2. **No abort button (v1).** Per Satya §3 step 7's v1 simplification —
   added a `TODO(judson)` comment and a caption: *"Submission runs to
   completion — to stop, close the browser tab."*
3. **Progress bar replaced by streamed write/markdown rows.** First
   draft used `st.progress`/`st.empty`/`st.container` for live
   per-manifest status, but the existing streamlit_recorder mock's
   default `__getattr__` returns `None`, which broke the test suite
   (couldn't call `.progress()`/`.write()` on `None`). Simplified to
   plain `st.write` + `st.markdown` calls per iteration — same UX
   clarity (operator sees `**N of M** — load_foo.json` followed by
   ✅/❌ row), no recorder extension needed. The persistent summary +
   dataframe still render after the loop completes.
4. **NOTICE.md fallback.** Reads `descriptor.root_dir /
   descriptor.notice_path`, asserts it resolves under `DATA_ROOT`
   defensively (so BYO descriptors can't escape `app/data/`), and shows
   "NOTICE not available" when missing. TNO's `notice_path` is
   `../../osdu/rc--3.0.0/NOTICE.md` (legitimate path-up to the shared
   tree); resolved path stays under `app/data/`.
5. **Legal-tag / ACL inputs — mirrored Manifest page's
   selectbox-with-fallback pattern**, not the spec's "newline-separated
   text inputs" wording. The page reuses
   `_render_option_field`: when `list_legal_tags`/`fetch_groups`
   succeeds, the operator gets dropdowns; otherwise text inputs with a
   "⚠️ Couldn't load …" caption. The values are passed to `submit_tier`
   as single-element lists (`acl_owners=[value]`), which is what
   Kevin's `_inject_acl_and_legal` expects. Matches the existing
   Manifest UX exactly — operators don't see a different shape for
   bulk vs single-manifest submits.
6. **Preview gate scope.** `bulk_preview_seen` is the tuple
   `(dataset_id, tier)`, per the spec. The main render path detects a
   dataset/tier change versus the stored seen-key and clears both
   `bulk_preview_seen` and `bulk_preview_results` — so the operator
   can never submit a payload whose preview was for a different tier.
7. **Cache invalidation on mount.** `_clear_cache()` is called at the
   top of `main()` (per Satya §1: "cache invalidated on page mount so
   a freshly dropped folder appears without restart").
8. **Submit error handling.** A `ValueError` from `submit_tier` (e.g.,
   tier flipped to disabled between Preview and Submit) gets caught,
   stored in `bulk_last_error`, and the page reruns. Any other
   exception is wrapped in an operator-safe summary (type + message,
   no raw traceback). The pre-iteration `ValueError`s would only fire
   if the registry changed between Preview and Submit — unlikely but
   defensible.

## Test coverage

9 cases in `tests/test_bulk_load_page.py`:

1. `test_page_renders_without_crashing_on_clean_session` — no
   connection → preflight info + page_link, no service calls.
2. `test_dataset_selector_populates_from_list_datasets` — mocks
   `list_datasets` to return TNO + Volve, asserts selectbox options.
3. `test_selecting_tno_shows_source_url` — asserts the source URL is
   rendered via `st.markdown` in the Source & license expander.
4. `test_tier_selector_filters_to_enabled_and_lists_disabled` —
   asserts the radio shows only `reference-data` and the info block
   mentions `master-data` + `work-products`.
5. `test_submit_button_disabled_before_preview` — even with
   legal+ACL filled, no preview → Submit `disabled=True` and the
   "Run Preview first" caption renders.
6. `test_clicking_preview_enables_submit_when_form_complete` — primes
   `bulk_preview_seen` + `bulk_preview_results`, asserts Submit
   becomes `disabled=False` and the preview summary line + dataframe
   render.
7. `test_submit_disabled_when_legal_tag_empty` — even after preview,
   empty legal tag → Submit `disabled=True` with a "legal tag"
   caption.
8. `test_preview_invalidates_when_dataset_changes` — primes a stale
   `bulk_preview_seen=("volve", ...)` while the selector points at
   tno; asserts the gate clears on render.
9. `test_submit_renders_mixed_success_and_failure_results` — drives
   the full Submit path, asserts `submit_tier` was called with the
   right kwargs (acl_owners as list, etc.), asserts ✅/❌ markdown
   rows render per result, asserts the persistent warning summary
   shows "1 of 2 succeeded — 1 failed", and asserts the locked
   `bulk_submit_results` key is stored.

All 9 pass. Full suite: 746 passed, 86% coverage on `app/`.

## Quality gates

- `ruff check .` — clean on new files. The 2 pre-existing errors
  Kevin already flagged (`tests/test_settings_store_keyring.py` F401
  and a vendored skill helper I001) are unchanged and NOT mine to fix.
- `mypy app` — Success: 31 source files clean.
- `pytest tests/test_bulk_load_page.py -v` — 9 passed.
- `pytest -q` — 746 passed in 76.72s.

## Did NOT touch

- `app/services/bulk_loader.py` (Kevin owns) — used as-is.
- `app/models/osdu.py` (Kevin added the dataclasses) — imported only.
- `app/services/ingestion.py` — bulk submit goes through
  `submit_tier` → existing `submit_manifest`. No new HTTP code.
- `app/pages/4_📥_Ingest.py` (landing) — no method card for Bulk Load
  added; out of scope per the spawn prompt. Follow-up: a 4th card on
  the Ingest landing page pointing at this one would be a small
  parity nit.
- The dataset descriptors under `app/data/datasets/` — TNO + Volve
  shipped by Kevin.
- Run History wiring — Kevin's service already records via the
  defensive `record_workflow_submit`/`finish` calls. The page doesn't
  duplicate that.

## Follow-ups for v2

- Mid-loop abort button. Will need a session-scoped cancellation flag
  the loop checks between manifests, or to break out of the generator
  on a Streamlit rerun signal.
- Master-data + work-products tiers. UI already supports them — flip
  the tier descriptor `enabled` flag on TNO once Kevin's service
  layer wires the new tiers.
- Method card on the Ingest landing page (`app/pages/4_📥_Ingest.py`)
  pointing to Bulk Load.
- Run History link from each submit row to the corresponding Run
  History entry — currently we just render the run_id; could become
  a clickable `st.page_link` once Run History (PR #13) merges.

---

# 2026-05-11T20:00:00Z: File page emits upload-summary rows for picker

**By:** Judson (requested by Brady)

**What:** The File page (`app/pages/6_📂_File.py`) now appends a second
shape to `file_upload_history` after a successful 3-phase upload. New
helper `_append_history_upload_summary(record_id, display_name,
file_source)` writes:

```json
{
  "timestamp": "...Z",
  "kind": "upload_summary",
  "record_id": "...",
  "display_name": "...",
  "file_source": "..."
}
```

The discriminator field `kind="upload_summary"` lets readers distinguish
from the existing latency/diagnostic rows (which carry `endpoint`, `ok`,
`http_status`, `latency_ms`, `correlation_id`, `error_message`).

**Why:** The Manifest Builder's "From recent uploads" picker
(`_recent_uploads()` in `5_📄_Manifest.py`) filters `file_upload_history`
for entries with non-empty `record_id`, `display_name`, and
`file_source` strings. Before this change the File page only emitted
latency rows, so the picker was always empty and the end-to-end
upload→manifest flow was broken.

**Invariants preserved:**
- `_append_history` shape unchanged — page diagnostics + tests untouched.
- `FILE_UPLOAD_HISTORY_KEY` constant unchanged.
- Summary rows are only appended on metadata POST `ok=True` AND a
  non-empty `record_id` — failed uploads never pollute the picker.
- Latency chart and history dataframe filter out summary rows
  (`kind == "upload_summary"` or missing `endpoint`) so the diagnostic
  table stays clean.

**Tests:**
- `test_successful_3phase_pipeline_*` updated: history now has 4 entries
  (3 latency + 1 summary); summary fields asserted.
- `test_phase3_failure_*` extended: asserts no summary row when
  metadata POST fails.
- `test_manifest_builder_recent_uploads_picker_uses_filtered_history`
  reseeded with the canonical `kind="upload_summary"` shape.
- 713 tests pass · mypy clean · ruff clean for changed files.

---

### 2026-05-11: Manifest Builder v1 — UI shipped on Manifest page
**By:** Judson (Streamlit UI) — requested by Brady (mariel)

## Shipped
`app/pages/5_📄_Manifest.py` now renders a `🛠️ Build manifest` `st.expander` between the legal-tag/ACL input row and the manifest text editor. The expander wires `app.services.manifest_builder.build_file_generic_manifest` to two pick modes (recent uploads / paste manually), pre-fills display name + description from a picked recent upload, and primes the manifest editor on success via the sentinel pattern.

## Where the expander landed
Order of `main()` is now:
1. `_render_sticky_error()`
2. `_render_input_form(connection)` — legal tag + ACL selectboxes (Builder reads these)
3. `_render_manifest_builder(connection)` — **NEW expander**
4. `_render_manifest_editor()` — text_area
5. `_render_action_row()` — Validate & Ingest

The sentinel-prime block (`pop manifest_builder_pending_text → MANIFEST_TEXT_KEY`) runs at the top of `main()` BEFORE any of those render calls, satisfying Streamlit's widget-mutation rule.

## Tweaks to Satya's contract
1. **New session key `manifest_builder_file_id`** added per Kevin's finding ("file_id is NOT recoverable from file_source"). Paste mode now renders TWO `st.text_input`s side-by-side: FileSource (Azure blob path) + File record id. Both are required when generating from paste mode.
2. **Display name + description widgets are shared between modes** (rendered below the radio/picker block, not inside each mode branch). Recent mode pre-fills them from the picked entry only when those session keys are still blank — so operator edits survive the next rerun without being clobbered if the picker re-renders.
3. **Kind is locked to `DEFAULT_DATASET_KIND`** for v1 via a single-option `st.selectbox` (forward-compatible widget, single value today).
4. **`acl_owners` / `acl_viewers` pass through as single strings** (selectbox values from the form above), matching Kevin's `build_file_generic_manifest` signature exactly. No list conversion in the UI.

## Recent uploads picker — graceful fallback
The contract says "From recent uploads" reads `file_upload_history`. Today the File page populates that key only with **latency rows** (`{timestamp, endpoint, ok, http_status, latency_ms, correlation_id, error_message}`), NOT with `{record_id, display_name, file_source}` entries. The Builder filters `file_upload_history` and surfaces ONLY entries that have all three of those string fields — so today the recent picker is empty and the radio defaults to paste mode automatically. New tests cover both shapes (latency-only history → paste default; richer entries present → recent works end-to-end).

## Follow-ups
- **File page should append richer entries to `file_upload_history`** on successful upload, with `{record_id, display_name, file_source, description}` keys so the Builder's "From recent" picker becomes useful end-to-end. Until that lands, operators always need to paste both values from their upload result. Owner: whoever next touches `app/pages/6_📂_File.py` (Darryl?).
- **Validator gap (Kevin's parallel work)**: until `app/services/ingestion.py::_MANIFEST_SECTION_KEYS` is loosened to accept `Data` as an object with `Datasets`/`WorkProductComponents`/`WorkProduct`, clicking **Validate & Ingest** on a Builder-produced manifest will fail with `"Manifest section 'Data' must be a list."`. The Builder's success message and editor pre-fill still work — only the downstream submit is blocked. Per spawn brief, no special-case handling added on the UI side; the existing error message surfaces as-is.
- **Builder unit tests** (golden fixture, missing-field ValueErrors, blank-description omission) for `app/services/manifest_builder.py` are still owed — see Kevin's note. UI-side tests live in `tests/test_ingestion_page.py` under the "Manifest Builder" section (5 new tests, all green).

## Quality gates
- `pytest -q` → 713 passed (5 new Builder tests added).
- `ruff check app tests` → clean for the touched files (`5_📄_Manifest.py`, `test_ingestion_page.py`); the one pre-existing F401 in `tests/test_settings_store_keyring.py` is unrelated and untouched.
- `mypy app` → clean (0 issues in 26 source files).

---

### 2026-05-11: Search page (Operate › Search) — shipped

**By:** Judson (Streamlit UI), requested by Brady (mariel)
**Files:**
- `app/main.py` — registered `pages/5_🔍_Search.py` in the Operate
  section after Ingestion.
- `app/pages/5_🔍_Search.py` — new page.
- `pyproject.toml` — added `N999` per-file-ignore for the new emoji
  page filename (matches the convention used for pages 1–4).

**Implementation notes:**
- Followed Satya's locked session-state contract verbatim — all 11
  `search_*` keys present with their documented initial values.
- **Post-widget mutation safety:** the text_input is bound to
  `search_query_text`. We NEVER reassign that key after the widget
  renders. The Search / Refresh / pagination handlers snapshot the
  current value into a separate `search_resolved_query` key and call
  `search_records` from that. This is the lesson from the 5/11
  ingestion crash.
- **Pagination ceiling:** Next is disabled when
  `offset + page_size + SEARCH_PAGE_SIZE > 10_000` (OSDU ceiling from
  Darryl) or when `offset + page_size >= total_count`. When we're at
  the ceiling a caption ("OSDU caps offset+limit at 10,000") explains
  why Next is greyed out.
- **Row selection:** Used a selectbox of ids rather than
  `st.dataframe(on_select=…, selection_mode="single-row")` —
  dataframe row-click is unreliable in Streamlit 1.57 and the contract
  flagged this. Selection survives reruns.
- **Full-record cache:** When a record is already cached, the fetch
  button relabels to "🔄 Refresh full record" so operators can pull a
  fresh copy without re-typing the id.
- **Token acquisition** mirrors the Ingestion page exactly (user
  impersonation falls back through `get_user_auth_state`).

**Tests:** `pytest -q tests/test_main.py` — 6 passed. The page test
suite (`tests/test_search_page.py`) is owned by Charlie.

**Lint/types:** `ruff check` clean, `mypy app` clean (22 files).

---

### 2026-05-12: Bulk Load backend — implementation choices
**By:** Kevin

Implemented §§1, 2, 4, 5 of Satya's bulk-load architecture. Notes on
choices the spec left to the implementer:

- **Tier ``enabled`` default.** Satya's example omitted ``"enabled":
  true`` from the reference-data tier. ``_parse_tier`` defaults
  ``enabled`` to ``True`` when ``manifest_glob`` is present and the
  ``enabled`` key is absent. Explicit ``"enabled": true`` is also
  honored (TNO's dataset.json uses the explicit form for clarity).
- **Path-traversal guard.** ``_assert_under_data_root`` resolves with
  ``.resolve()`` and calls ``relative_to(DATA_ROOT)``. Applied to both
  the glob parent and every resolved match. A descriptor with
  ``manifest_glob: "../../../etc/passwd"`` raises ``ValueError``
  during ``_resolve_manifests`` — well before any read.
- **ACL/legal injection — non-destructive.** ``_inject_acl_and_legal``
  deep-copies the parsed body and only writes empty arrays. Already
  populated ``acl.owners``/``acl.viewers``/``legal.legaltags`` are left
  alone. ``legal.otherRelevantDataCountries`` is **not** touched — the
  operator inputs we have don't carry that field and the vendored
  files ship with ``[]``, which OSDU accepts.
- **Submit payload shape.** Each manifest is wrapped in the
  ``executionContext`` envelope the ``Osdu_ingest`` workflow expects,
  with ``AppKey`` fixed to ``"adme-ingestion-tool"`` and the operator's
  ``data_partition_id`` echoed in the Payload. ``submit_manifest`` is
  called once per manifest — no rewrapping inside the ingestion
  service.
- **Sequential generator.** ``submit_tier`` yields one ``SubmitResult``
  at a time. A failing manifest (HTTP error, malformed body, broken
  read) yields ``status="error"`` with the message in ``error`` and the
  loop continues to the next file. The page can abort by stopping
  iteration; the service does not abort itself.
- **Run-history wiring is defensive.** ``record_workflow_submit`` and
  ``record_workflow_finish`` are imported in a ``try/except
  ImportError`` block because ``app/services/run_history.py`` is still
  on the ``marielherz_RunHistory`` branch (PR #13). Calls are gated on
  ``is not None`` and any exception inside them is swallowed —
  telemetry must never fail a submit. TODO comment flags the import
  guard for removal once #13 merges, at which point ``"bulk_load"``
  must be added to the allowed ``submit_source`` set in
  ``run_history.py``.
- **Cache invalidation.** ``list_datasets()`` caches at module level;
  ``_clear_cache()`` resets it for tests and (per Satya §1) for the
  page to call on mount so freshly dropped folders appear without an
  app restart.
- **Volve placeholder.** All three tiers disabled with ``reason="not
  yet vendored — see backlog"``. Tested that the registry surfaces
  both TNO and Volve, sorted by display_name.

### Test coverage

``tests/test_bulk_loader_service.py`` — 12 tests covering registry
discovery, malformed-descriptor skip, unknown-id error, preview
record-counting (13 manifests, all kinds non-empty), disabled-tier
guard, path-traversal block, sequential submit, ACL/legal injection,
non-overwrite of pre-populated records, and continue-past-failure.

Full suite: **737 passed, 87% coverage**. ``ruff check`` clean on new
files; the two pre-existing ruff errors elsewhere in the tree
(``test_settings_store_keyring.py`` F401, vendored skill helper I001)
are unrelated and not touched. ``mypy app`` clean (30 source files).

---

### 2026-05-11: File Upload v1 — services + models implementation notes

**By:** Kevin (Backend)
**Requested by:** Brady (mariel)
**What:** Landed `app/services/files.py` and three new result dataclasses in `app/models/osdu.py` per Satya's contract, reconciled against Darryl's API research. 600/600 tests pass, ruff clean, mypy clean.

---

## Divergences from Satya's contract (resolved in Darryl's favor)

1. **`kind` is `osdu:`-prefixed, not partition-prefixed.** Satya's draft body used `"{partition}:wks:dataset--File.Generic:1.0.0"`. Darryl's authoritative Microsoft Learn cite (CSV parser tutorial) uses literal `"osdu:wks:dataset--File.Generic:1.0.0"`. The `osdu:` here is the **schema authority** — kinds always use the schema authority, only record IDs use the partition prefix. I implemented the literal `osdu:` form via the `FILE_GENERIC_KIND` constant. Satya should update the contract doc; the wire shape is correct as shipped.

2. **`FILES_TIMEOUT_SECONDS = 15`, not Satya's 10.** Per Brady's instruction. Metadata POST in particular can take longer than legal-tag CRUD on a cold partition.

3. **`get_upload_url` signature has no `**` kwargs.** Satya didn't ask for any. Two positional args (connection, token).

4. **`upload_file_bytes(content_type="application/octet-stream", timeout=120)` is fully defaulted.** Satya's contract showed `content_type` positional and `timeout` keyword-only. Per Brady's instruction `content_type` is keyword-only with a default; the page can still pass an inferred MIME but doesn't have to.

5. **Added `"status": "compliant"` to the `legal` block** in the metadata POST body. Darryl's verbatim tutorial example includes it; Satya's skeleton omitted it. Without it, partitions configured for strict legal validation reject the record. Cheap to include, costs nothing when not required.

## Response-parsing decisions

- **`SignedURL` / `FileSource` location:** Darryl confirmed the ADME response is FLAT (`{"SignedURL": ..., "FileSource": ...}`) per Microsoft Learn, but legacy R2-style builds nested them under `Location`. `get_upload_url` reads defensively: `body.get("Location", body)`, then pulls `SignedURL` / `FileSource` / `FileID`. Either shape works without code change.
- **`FileID` is optional.** Some ADME builds include it, some don't. We surface it verbatim into `UploadURLResult.file_id` when present; `None` otherwise.
- **Missing-required-field handling:** if `SignedURL` or `FileSource` is absent or empty after defensive parsing, we treat it as a parse failure: `ok=False`, `http_status` preserved (still a 2xx from ADME's perspective), `error_message="uploadURL response missing required SignedURL or FileSource field."` — operator sees a clear sticky error rather than a `None` PUT later.
- **Metadata POST response:** parsed for `id` (record id) and `version` (record version). `version` is `None` when the server doesn't return one; some ADME builds only return `id` on first-create. Both `_coerce_str` and `_coerce_int` are defensive (reject empty strings, reject `bool` for int).

## `file_id` in metadata POST

`post_file_metadata` accepts `file_id` to match Satya's contract surface, but **does not send it in the request body** — the metadata POST mints a fresh record id server-side. I added a `_ = file_id` line to make the intent explicit and stop ruff from flagging it as unused. The value is informational only; the page can persist it alongside the eventual record id for diagnostics (e.g., the "Phase 3 failed AFTER successful PUT" sticky-error message Satya specified).

## ValueError boundaries

Per the standard pattern, every empty-input boundary raises:
- `upload_file_bytes`: empty `signed_url` or empty `file_bytes`
- `post_file_metadata`: empty `file_source`, `display_name`, `legal_tag`, `acl_owners`, `acl_viewers`
- `_call_files`: invalid connection or empty token

`description` is intentionally NOT required — it's optional in the metadata record. When blank, the `Description` key is omitted from `data` entirely rather than sent as `""`.

## Follow-ups for the next team members

- **Judson:** the page should call `get_upload_url` → `upload_file_bytes(..., content_type=guessed_mime)` → `post_file_metadata(..., file_id=<from step 1>)`. The 100 MB hard cap is exposed as `MAX_FILE_BYTES_V1`; import it for the size-gate check rather than redefining.
- **Charlie:** tests should cover `_call_files` only supporting `GET` and `POST` (raises `ValueError` on `PUT`/`DELETE`). The Azure PUT lives in `upload_file_bytes` and has its own happy-path/failure tests separate from `_call_files`. Correlation-id extraction tests can be ported verbatim from `test_legal_tags_service.py`.
- **Open question:** should `get_upload_url` retry on the rare ADME 5xx? Right now: no internal retries (per the established pattern). Page handles re-run UX.

**Why:** Lands the backend half of File Upload v1 cleanly so Judson can build the page and Charlie can author tests in parallel. Documents the three Darryl-vs-Satya reconciliations so the contract doc gets a clean follow-up patch.

---

### 2026-05-11: Manifest Builder v1 — service implementation
**By:** Kevin (Backend) — requested by Brady (mariel)

## Shipped
`app/services/manifest_builder.py` — pure `build_file_generic_manifest(...)` plus `MANIFEST_WRAPPER_KIND` and `DEFAULT_DATASET_KIND` constants. No HTTP, no IO, no Streamlit.

## Field shape — exact mirror of `post_file_metadata`
- `acl: {"owners": [acl_owners], "viewers": [acl_viewers]}` — single-string-wrapped-in-list, identical to files.py.
- `legal: {"legaltags": [legal_tag], "otherRelevantDataCountries": ["US"], "status": "compliant"}` — verbatim.
- `data.Name` = display_name; `data.DatasetProperties.FileSourceInfo = {FileSource, Name}` — matches files.py.
- Description omitted when blank/whitespace-only (does not emit `""`).
- `id` on the dataset record = `file_id` (per Brady's spec — operator can re-reference the same upload).

## Envelope
Wrapped in `executionContext.{Payload, manifest}`. `Payload` carries `{"AppKey": "adme-ingestion-tool", "data-partition-id": data_partition_id}` — copied from the `TNO_SAMPLE_MANIFEST` shape in `app/services/ingestion.py` so the operator's data partition flows through to Airflow without manual editing.

## Divergences from Satya's contract
1. **Signature uses `description: str` and `acl_owners: str` / `acl_viewers: str`** (per Brady's prompt), not Satya's `description: str | None` / `list[str]`. This matches the existing `post_file_metadata` signature so the Manifest page can pass the same already-validated values without conversion.
2. **`Data` is an object with `WorkProductComponents/WorkProduct/Datasets`**, not a flat list of records. This follows Brady's spec verbatim and the canonical OSDU WPC ingestion pattern.
   - ⚠️ **Validator gap:** `app/services/ingestion.py::_MANIFEST_SECTION_KEYS` currently expects `Data` to be a **list** (it was written for the TNO ReferenceData sample). A manifest produced by this builder will fail `submit_manifest`'s preflight validation with `"Manifest section 'Data' must be a list."`. **Follow-up needed:** loosen `validate_manifest_text` to accept `Data` as either a list (legacy) or an object with `Datasets`/`WorkProductComponents`/`WorkProduct` keys (WPC pattern). UI implementer (Reggie?) and ingestion owner should coordinate on whether the validator update lands before or alongside the Manifest page Builder UI. If the Builder ships first with the current validator, operators will see a confusing error on Submit.
3. **`ResourceSecurityClassification` field** mentioned in Satya's contract is NOT emitted — `post_file_metadata` does not emit it either, so for v1 parity the builder also omits it. Add later if/when files.py starts emitting it.
4. **`PreloadFilePath` field** mentioned in Satya's contract is NOT emitted — same reason as above; not in `post_file_metadata`.

## Tests
Did not write builder-specific tests (per the spawn brief — `pytest -q` to confirm nothing breaks). Existing 694 tests pass. Quality gates clean (ruff + mypy).

## Follow-up for the team
- **Ingestion validator update** (above) — required before Builder is end-to-end usable.
- **Builder unit tests** — Satya's contract listed required tests (golden fixture, missing-field ValueErrors, blank-description omission, ACL passthrough). Hockney or whoever owns testing for this slice should pick those up.
- **`file_id` for paste mode** — Satya flagged "confirm whether file_id is recoverable from file_source alone". Answer: **no.** `file_source` is an opaque Azure Blob path token; `file_id` (the `FileID` returned by `GET /uploadURL`) is a separate value. UI must add a second `st.text_input` for paste mode, OR derive a synthetic id (e.g., `f"{partition}:dataset--File.Generic:{uuid4()}"`).

---

### 2026-05-11: validate_manifest_json now accepts Data as list OR object (WPC shape)
**By:** Kevin (Backend) — requested by Brady (mariel)
**What:** Loosened `app/services/ingestion.py::validate_manifest_json` so the
`Data` section may be either:
1. **Legacy:** a flat list of records (existing behavior, unchanged).
2. **WPC object:** a dict containing any combination of `Datasets` (list of
   record dicts), `WorkProductComponents` (list of record dicts), and
   `WorkProduct` (dict; empty `{}` allowed). Unknown keys inside `Data`
   are permitted for forward compatibility.

`ReferenceData` and `MasterData` remain list-only — the loosening is
scoped to `Data` because that's the only section the OSDU
Work-Product-Component pattern wraps as an object.

Per-record validation (each item is a dict with a non-empty string
`kind`) is applied uniformly to legacy lists, `Data.Datasets[*]`, and
`Data.WorkProductComponents[*]`.

Error messages updated:
- `Data` neither list nor dict → `"Manifest section 'Data' must be a
  list of records or an object with Datasets/WorkProductComponents/
  WorkProduct."`
- Sub-key shape errors carry the full path, e.g.
  `"Manifest section 'Data.Datasets' must be a list."`,
  `"Manifest item at Data.Datasets[0] is missing a string 'kind'."`,
  `"Manifest section 'Data.WorkProduct' must be an object."`

**Why:** `app.services.manifest_builder.build_file_generic_manifest`
emits `Data` as the canonical WPC object. Without this loosening the
Builder's own output would be rejected by the validator, blocking
Submit on Page 5 (Manifest) and the TNO E2E walkthrough.

**Tests added in `tests/test_ingestion_service.py`:**
- Legacy `Data: [...]` list shape still validates ✓
- `Data: {Datasets: [...]}` validates ✓
- `Data: {WorkProductComponents: [...]}` validates ✓
- `Data: {WorkProduct: {...}}` validates ✓
- `Data: {WorkProduct: {}, Datasets: [...]}` validates (Builder's actual
  empty-WorkProduct shape) ✓
- Forward-compat: unknown keys inside `Data` allowed ✓
- `Data: {Datasets: {...}}` (not a list) → ValueError with `Data.Datasets`
- `Data: {WorkProductComponents: "nope"}` → ValueError
- `Data: {WorkProduct: [1,2]}` → ValueError
- `Data: {Datasets: [{"id": "x"}]}` (missing kind) → ValueError
- `Data: "not a list or object"` → ValueError with new message
- `ReferenceData: {...}` (object) still rejected — list-only enforcement
  unchanged
- `MasterData: {...}` (object) still rejected — list-only enforcement
  unchanged
- **Round-trip:** `build_file_generic_manifest(...)` output → `json.dumps`
  → `validate_manifest_json` → `(True, "", parsed)`. Asserts
  `parsed["executionContext"]["manifest"]["Data"]` is a `dict` to
  guard against the Builder ever silently switching shapes.

**Impact:** Submit no longer fails validation for Builder-produced
manifests. Page 4 / Page 5 ingestion flow unblocked for WPC datasets.

**Verification:** `pytest tests/test_ingestion_service.py -q` → 87
passed. `ruff check app/services/ingestion.py
tests/test_ingestion_service.py` → clean.

**Note for the team:** `tests/test_ingestion_page.py` has 21 pre-existing
failures caused by a syntax error in `app/pages/5_📄_Manifest.py` line 153
(`_render_manifest_buildert a manifest"` — looks like a botched edit
collided with a string literal). NOT introduced by this change. Owner
of the Manifest page should fix that before the E2E walkthrough.

---

### 2026-05-11: Search v1 service + model implementation
**By:** Kevin (Backend) — requested by Brady (mariel)
**Files:** `app/services/search.py` (new, 212 LOC), `app/models/osdu.py` (+4 dataclasses)
**Status:** Ready for Judson (page) and Charlie (tests).

---

## What landed

1. **`app/services/search.py`** — three public functions matching Satya's §2 contract:
   - `search_records(connection, token, *, kind, query, limit, offset)` → `SearchPageResult`
   - `list_kinds(connection, token)` → `KindAggregationResult`
   - `get_record(connection, token, record_id)` → `RecordDetailResult`
   - Single `_call_search` helper handling both GET (Storage) and POST (Search), modeled on `legal_tags._call_legal`. Same correlation-header tuple (`correlation-id`, `x-correlation-id`, `request-id`, `x-request-id`), case-insensitive via `header.lower()` lookup.
   - Constants exactly as specified: `SEARCH_QUERY_PATH`, `STORAGE_RECORD_PATH_TEMPLATE`, `SEARCH_TIMEOUT_SECONDS=15`, `DEFAULT_SEARCH_LIMIT=100`, `MAX_OFFSET_PLUS_LIMIT=10_000`, `WILDCARD_KIND="*:*:*:*"`.

2. **`app/models/osdu.py`** — four new `@dataclass(frozen=True, slots=True)` types added directly above `LegalTag` (keeps the search bundle co-located, before the legal-tag bundle): `RecordSummary`, `SearchPageResult`, `KindAggregationResult`, `RecordDetailResult`. All carry `ok`, `latency_ms`, `correlation_id`, `http_status`, `error_message` per the legal_tags / entitlements invariant.

3. **Verification:** ruff clean, mypy clean (21 source files, 0 issues), full suite 478 passed. Smoke imports verified (`from app.services.search import search_records, list_kinds, get_record`).

---

## Divergence from Satya / Darryl (deliberate)

- **Field name `create_time` vs `createTime`.** Satya's contract used `create_time` (snake_case) on `RecordSummary`. Kept snake_case in the dataclass — matches Python convention and the rest of `osdu.py`. The wire field stays `createTime` (parsed off the hit JSON).
- **`KindAggregationResult.from_aggregation: bool`** (Brady's spec) instead of Satya's `source: str` enum (`"aggregation"` | `"page_sample"`). Bool is what Brady asked for; flag is unambiguous and saves the string-enum dance.
- **`SearchPageResult` field order.** Reordered to put `kind/query/offset/limit` first (echo of request), then `records/total_count/has_more`, then the standard envelope (`ok/http_status/latency_ms/correlation_id/error_message/raw_response`). Mirrors `LegalTagListResult` / `LegalTagDetailResult` layout.
- **`SearchPageResult.has_more: bool`** added per Brady's spec (Satya's contract did not include it). Computed: if `totalCount` present, `(offset + len(records)) < total_count`; else `len(records) >= limit` (best-effort signal for the Next button).
- **`record_id` URL encoding.** Used `quote(record_id, safe=":")` per Darryl §3 (Brady's instruction). Satya's contract said `safe=""` — Darryl's reasoning wins (preserves colons in the path segment, which is the OSDU norm and what `verification.py` already does for kinds).
- **Default `sort`.** Used `{"field": ["createTime"], "order": ["DESC"]}` (Darryl §1b verified shape) as a fixed module-level default, not a kwarg. Satya's contract had `sort` as a kwarg; for v1 the page only ever needs createTime-desc, so the kwarg adds API surface for no benefit. **Follow-up:** if the page needs to override sort later (e.g., relevance for free-text queries), promote `sort` back to a kwarg.
- **No `sample_limit` kwarg on `list_kinds`.** Satya had `sample_limit: int = 1000`. Used `DEFAULT_SEARCH_LIMIT (100)` for the fallback sample — 1000 on the wildcard rate-limited path is heavier than necessary for "populate a dropdown best-effort". Easy to lift later if the dropdown looks sparse in real partitions.

---

## Aggregation-fallback path (Brady §1 list_kinds requirement)

`list_kinds` implements all three fallback conditions:

1. **Aggregation rejected** (non-2xx, transport error) → fall through to page-sample.
2. **Aggregation returned empty** (`aggregations: []` or missing) → fall through to page-sample, so the dropdown can still be populated from real hits if any exist.
3. **Aggregation transport-failed** (timeout, DNS) → fall through to page-sample.

Returned `KindAggregationResult.from_aggregation`:
- `True` only when aggregation returned 2xx AND produced ≥1 kind.
- `False` in every fallback path (including the case where the fallback sample also fails — `ok=False, kinds=[], from_aggregation=False`).

Latency on the fallback path is the **sum** of both calls so the history chart reflects total work. Correlation_id prefers the fallback's value when present.

---

## Orphan cleanup — `SearchResult` (NOT deleted)

Satya flagged the existing `SearchResult` dataclass in `app/models/osdu.py:97` as stale and slated for deletion. **It is not stale.** `grep_search` found 4 active import sites:

- `app/services/verification.py` — imports and returns it from `search_records_by_kind` (post-ingest verification on page 4)
- `app/pages/4_📥_Ingestion.py` — imports and type-annotates final/intermediate verification results in three places
- `tests/test_ingestion_page.py` — imports and constructs it in fixtures
- `tests/test_osdu_models.py` — imports and asserts its frozen-dataclass contract

Per Brady's §4 (don't delete if anything imports it) and §5 (do not touch `verification.py` or its callers), **`SearchResult` stays as-is**. Satya can drop the deletion line from the contract.

**Follow-up:** there is real semantic overlap between `verification.search_records_by_kind → SearchResult` and `search.search_records → SearchPageResult`. Both POST `/api/search/v2/query` with the same headers and parse the same response shape. A future refactor could:
- Either: collapse `verification.search_records_by_kind` to call `search.search_records` and adapt the result, then delete the duplicated `_call_search` / correlation-header / JSON-parsing helpers in `verification.py`.
- Or: extract the shared HTTP plumbing (the ~120 LOC of `_call_search`/`_extract_correlation_id`/`_try_parse_json`/`_error_message_from_json`/`_truncate` that's now triplicated across `legal_tags.py`, `verification.py`, and `search.py`) into a single internal `app/services/_http.py` module.

Both are out of scope for the Search v1 ticket. Flagging for whoever owns the next refactor pass.

---

## verification.py impact

Untouched. `verification.py::search_records_by_kind`, its `_call_search` helper, and its 5s timeout all stay. The two modules share endpoint and header shape but live independently. Verified by full-suite green (478 passed).

---

## Follow-ups (none blocking)

1. **DRY the HTTP plumbing** — `_call_*` / correlation / JSON helpers are now in three modules (legal_tags, verification, search). Worth extracting to `app/services/_http.py` next time someone is in this neighborhood.
2. **`sort` as kwarg** on `search_records` if the page needs relevance ordering for free-text queries (Darryl §1b: omit `sort` to get `_score DESC`).
3. **`sample_limit` kwarg on `list_kinds`** if dropdowns are sparse in real partitions.
4. **429 detection helper** — neither this module nor verification.py specially classify HTTP 429 (Darryl §2 wildcard rate-limit). The page can read `http_status == 429` directly from `KindAggregationResult` / `SearchPageResult`. If the friendly-message logic ends up duplicated in Judson's page, lift it into a tiny `search.py` helper.

— Kevin

---

### 2026-05-12: Legal-tag pre-flight switches from GET to POST `:validate`

**By:** Kevin (Backend) for Mariel — backlog item #2 / Darryl's contract flag

**What changed**
- `app/services/ingestion.py::check_legal_tag` now sends
  `POST /api/legal/v1/legaltags:validate` with body
  `{"names": [legal_tag_name]}` (single-element list — endpoint is
  bulk-capable but the Manifest page only ever pre-flights one tag).
- Added `validate_legal_tag` as the real implementation;
  `check_legal_tag` is kept as a thin alias so the Manifest page
  (`5_📄_Manifest.py`) keeps working without any UI churn. The page
  still imports `check_legal_tag` and still gets a `LegalTagCheckResult`
  with the same field set.
- Removed the now-dead private `_call_legal` GET-only wrapper. The
  one remaining caller (`validate_legal_tag`) hits the shared `_call`
  helper directly with `method="POST"`.
- `LEGAL_TAG_VALIDATE_PATH` is reused from `app.services.legal_tags`
  (it was already declared there alongside `LEGAL_TAGS_PATH` and
  `LEGAL_TAG_PROPERTIES_PATH`). Re-exported in ingestion `__all__`.

**Response-shape decisions**
- 2xx response body must be a JSON object with an `invalidLegalTags`
  list.
  - Empty list → `ok=True` (tag is valid).
  - List contains the queried name → `ok=False` with a curated message:
    `Legal tag '{name}' is not valid in partition '{partition}'. The
    workflow service will reject this manifest. Create or fix the tag
    in Legal Tags, then retry.` This is intentionally stronger than the
    old GET-404 "not found" message because `:validate` cannot
    distinguish "doesn't exist" from "exists but invalid" — either way
    the workflow service will reject ingest, so the operator action is
    the same.
  - 2xx without parseable JSON, or 2xx with no `invalidLegalTags` list
    → `ok=False` with a "malformed response" message so we don't
    silently green-light a manifest based on an unexpected shape.
- Non-2xx (400/401/403/404/500/503) falls through to the standard
  `_call` error-body extraction. We no longer special-case 404 — the
  endpoint shouldn't return 404 for an unknown tag (it'll return 200
  with the name in `invalidLegalTags`); if a 404 *does* appear it's
  almost certainly a path/host misconfiguration and we want the raw
  message surfaced.
- Transport errors (timeout, connection error) still produce `ok=False`
  with `http_status=None`. No exception ever leaves the function.

**Divergences from Darryl's contract**
- None on the wire (endpoint + body shape match exactly).
- One copy divergence: the Manifest page's success caption still reads
  `"✅ Legal tag {name} exists in this partition."` even though
  `:validate` actually proves "is valid", not "exists". Per Mariel's
  no-UI-copy-change rule for this branch I left it alone. Tagged as a
  follow-up below for Charlie.

**Tests**
- Rewrote `tests/test_ingestion_service.py::check_legal_tag` block to
  match the new POST path / JSON body / response shape:
  - happy path (`invalidLegalTags: []`)
  - invalid tag (`invalidLegalTags: ["missing-tag"]`) → curated message
    includes name + partition + "not valid"
  - missing `invalidLegalTags` field → malformed-response error
  - 2xx with no JSON body (204) → malformed-response error
  - 4xx/5xx (400/401/403/404/500/503) → error body surfaced
  - timeout, connection error → `http_status=None`
  - outgoing headers include `Content-Type: application/json` (POST has
    a body now)
  - correlation-id case-insensitive
  - tag name with weird chars travels verbatim in the JSON body — no
    URL-encoding (replaces the old `url_encodes_special_chars` test)
- Manifest page tests (`tests/test_ingestion_page.py`) untouched — they
  monkeypatch `check_legal_tag` at the page-module level and only care
  about the `LegalTagCheckResult` shape, which is unchanged.

**Quality gates (this branch)**
- `pytest -q` → **718 passed** (713 prior + 5 net new on this branch)
- `mypy app` → **Success: no issues found in 26 source files**
- `ruff check app tests` → 1 pre-existing F401 in
  `tests/test_settings_store_keyring.py`, not touched by this change.

**Follow-ups**
- 🪧 **For Charlie:** Manifest page success caption "Legal tag X exists
  in this partition" should probably read "is valid" now that the
  pre-flight actually validates. Operator-facing copy is your call —
  flagging not fixing.
- 🪧 **For future:** the `:validate` endpoint accepts a list. If we
  ever want to pre-flight the *full* set of tags referenced by a
  manifest (not just the form-input one) we can pass all of them in a
  single call. Out of scope for this isolated switch.
- 🪧 **For Hockney:** the new tests use `_FakeResponse(raise_on_json=
  True)` to simulate a 204-no-body case. If we add a shared test
  helper for "no JSON body" elsewhere, consider promoting this pattern
  into `tests/conftest.py`.

**Files touched**
- `app/services/ingestion.py` — `check_legal_tag` rewrite +
  `validate_legal_tag` added + `_call_legal` removed + `__all__`
  bump + import of `LEGAL_TAG_VALIDATE_PATH`.
- `tests/test_ingestion_service.py` — pre-flight test block rewritten.
- No page-level files changed. No copy strings changed.

---

### 2026-05-11: Project backlog now lives at `.squad/backlog.md`

**By:** Satya (Lead), requested by Brady (mariel)
**What:** Drafted `.squad/backlog.md` as the single source of truth for "what are we working on, what's next, what's deferred." Structure: Now / Next / Later / Ideas / Tech debt / Done, with size estimates (XS–XL) and likely owner per item.
**Why:** The team had been working turn-by-turn — features were shipping cleanly but there was no shared view of sequencing, no list of the small follow-ups agents had flagged during reviews, and no place for "agreed but not scheduled" ideas to live. Brady asked for a real plan instead of pure context-rebuilding each session.

## Top of the stack

- **Now:** File upload via OSDU File Service (signed-URL flow) — confirmed by Brady as the next big feature.
- **Next:** Manifest builder UI · Run history page · Switch ingestion legal-tag pre-flight to `POST /legaltags:validate` (Darryl's recommendation, XS).
- **Later / Ideas:** Bulk ingest, saved searches, export search results, CSV→manifest, record edit/delete, multi-kind filter, field-builder UI, geo-spatial search, branding, quickstart doc, upstream PR #11 conversation.

## Process

- Backlog is its own doc, not part of `decisions.md` (decisions stay append-only and reason-focused; backlog is order-focused and edited in place).
- When priorities change, update `.squad/backlog.md` first, then write a short decision doc explaining the *why*.
- Tech debt section captures the non-blocking flags agents (Kevin, Charlie, Scott) recorded during reviews — items would otherwise vanish into history.md files.

## Out of scope for this pass

- No code changes.
- No PR work.
- No `decisions.md` edits — it stays append-only.
- File upload remains unstarted; this doc just declares it as Now.

---

### 2026-05-12: Bulk Load — generic OSDU manifest-set ingestion architecture
**By:** Satya

## Decision

Persist user-entered ADME connection settings (the control plane) to a local
SQLite database via the Python stdlib `sqlite3` module. SQLite becomes the
durable backing store; Streamlit `st.session_state` remains the per-rerun
working copy and continues to own all auth/session-only material.

## Why SQLite (and not pglite)

- pglite is JS/WASM (Postgres compiled for the browser). The Streamlit app
  runs Python on the server side; pglite cannot be embedded in that process
  and adds a Node.js/WASM dependency that we will not own.
- SQLite via `sqlite3` is stdlib — zero new runtime dependencies, no install
  step, works under our current emulation/test environment, and gives us a
  single-file database that is trivially git-ignored and trivially deleted
  for a clean reset.
- File-based persistence matches operator expectations for a desktop-style
  control plane: settings survive across sessions for the same OS user, and
  there is no separate service to start.

## Storage location

- Path: `~/.adme-ingestion-tool/settings.db` resolved via
  `Path.home() / ".adme-ingestion-tool" / "settings.db"`.
- Created lazily on first write; directory created with `mkdir(parents=True,
  exist_ok=True)`.
- Overridable via environment variable `ADME_SETTINGS_DB` for tests and
  alternate installs (tests MUST use `tmp_path`, never the real home dir).
- Why user profile (not repo): the database is per-operator state, not
  product code. Storing it in the repo would (a) leak operator-specific
  endpoints/tenant IDs through git, (b) collide between developers sharing
  the checkout, and (c) be wiped by routine `git clean`. The user-profile
  location is the same convention used by `az`, `gh`, `kubectl`, etc.
- `.gitignore`: not required (the path is outside the repo), but add the
  override path (`*.db` under `.adme-ingestion-tool/`) defensively if any
  test fixture writes inside the workspace.

## Schema — `connections` table

```sql
CREATE TABLE IF NOT EXISTS connections (
    name              TEXT PRIMARY KEY,
    endpoint          TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    client_id         TEXT NOT NULL,
    data_partition_id TEXT NOT NULL,
    token_scope       TEXT NOT NULL DEFAULT 'https://energy.azure.com/.default',
    auth_method       TEXT NOT NULL CHECK (auth_method IN
                          ('user_impersonation', 'service_principal')),
    is_active         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_connections_active
    ON connections(is_active) WHERE is_active = 1;
```

### Schema decisions

- **Primary key is `name` (operator-supplied label), not the endpoint.** An
  operator may register the same ADME endpoint under different labels
  (e.g. `prod`, `prod-readonly`) with different auth methods. `name` is the
  stable human handle.
- **No `id` surrogate key.** `name` is short, stable, and already unique.
  Adding an integer PK buys nothing for a single-user local store.
- **`client_secret` is NOT persisted.** Secrets at rest are explicitly out
  of scope (see below). Service-principal connections will require the
  operator to re-enter the secret per session until encryption-at-rest is
  added. The Settings UI must communicate this clearly.
- **No `token_scope` UNIQUE.** Scope is config that may legitimately vary
  per saved connection.
- **`is_active` partial unique index** enforces "at most one active
  connection at a time" without needing a separate `active_connection`
  table. Activating a new row sets others to 0 in the same transaction.
- **Timestamps as ISO 8601 TEXT** (`datetime.now(UTC).isoformat()`).
  SQLite has no native datetime; TEXT is portable and human-readable.
- Field names mirror `app/models/connection.py` (`ADMEConnection`) so the
  store/load mapping is mechanical.

## API surface — `app/services/settings_store.py`

Function signatures only; bodies are Kevin's work.

```python
"""Local SQLite-backed store for persisted ADME connection settings."""

from __future__ import annotations

from pathlib import Path

from app.models.connection import ADMEConnection


DEFAULT_DB_PATH: Path  # = Path.home() / ".adme-ingestion-tool" / "settings.db"


class SettingsStoreError(Exception):
    """Raised when the local settings store cannot be read or written."""


def get_db_path() -> Path: ...
    """Return the resolved SQLite path, honoring ADME_SETTINGS_DB."""


def initialize_store(db_path: Path | None = None) -> None: ...
    """Create the DB file and schema if missing. Idempotent."""


def list_connections(db_path: Path | None = None) -> list[tuple[str, ADMEConnection]]: ...
    """Return saved (name, connection) pairs, ordered by name."""


def load_connection(name: str, db_path: Path | None = None) -> ADMEConnection | None: ...
    """Return the saved connection for name, or None if missing."""


def save_connection(
    name: str,
    connection: ADMEConnection,
    db_path: Path | None = None,
) -> None: ...
    """Insert or update a saved connection by name. client_secret is not persisted."""


def delete_connection(name: str, db_path: Path | None = None) -> None: ...
    """Remove a saved connection by name. No error if missing."""


def get_active_connection_name(db_path: Path | None = None) -> str | None: ...
    """Return the currently active connection name, or None if none active."""


def set_active_connection(name: str, db_path: Path | None = None) -> None: ...
    """Mark name as active and clear the active flag on all other rows.

    Raises SettingsStoreError if name does not exist.
    """


def clear_active_connection(db_path: Path | None = None) -> None: ...
    """Clear the active flag on all rows. Used on Sign Out / reset."""
```

### API design notes

- `db_path` is a thin seam for tests (override with `tmp_path`) and the
  `ADME_SETTINGS_DB` env var. Defaults to `get_db_path()`.
- All functions open and close a short-lived `sqlite3` connection. No
  module-global cursor; no Streamlit caching. Concurrency is single-user.
- `save_connection()` MUST drop `client_secret` before INSERT/UPDATE.
  Document this in the docstring; assert it in tests.
- `set_active_connection()` and `save_connection(... is_active=True)` paths
  must run inside `BEGIN IMMEDIATE` to keep the partial unique index honest
  on activation switches.
- Errors from `sqlite3` are re-raised as `SettingsStoreError` with
  operator-safe messages. No raw connection strings or tokens in messages.

## UI integration touchpoints

### `app/connection_state.py`

- `ensure_session_defaults(session_state)` — call
  `settings_store.initialize_store()` once and, when `CONNECTION_KEY` is
  absent from session state, hydrate it via
  `load_connection(get_active_connection_name())`. Keep all existing keys.
- `save_connection(session_state, connection)` — after updating session
  state, call `settings_store.save_connection(name, connection)` and
  `settings_store.set_active_connection(name)`. The function gains an
  optional `name` argument; for the v1 single-connection UX, default to
  the literal `"default"` label so existing call sites do not break.
- `clear_user_auth_state(session_state)` — unchanged. Auth material is
  session-only and is NOT written to SQLite.
- New helper `forget_saved_connection(session_state, name)` that wraps
  `settings_store.delete_connection` plus any session cleanup.

### `app/pages/1_⚙️_Settings.py`

- `_handle_form_action(...)` — when `save_clicked` succeeds, the existing
  call to `save_connection(st.session_state, draft_connection)` already
  becomes the persistence path once `connection_state.save_connection`
  delegates to the store. No new code in the page for the v1 single-slot
  flow beyond the success message ("Saved for this and future sessions.").
- `_consume_oauth_callback_once()` — unchanged. Auth flow is session-only.
- (Deferred to a follow-up issue: a "Saved connections" picker that lists
  rows from `list_connections()` and lets the operator switch active. For
  this scope we ship single-slot persistence keyed on the `"default"`
  name.)

### `app/main.py`

- No changes required. `ensure_session_defaults()` already runs at the top
  of `main()`, so hydration happens transparently.

## Out of scope (deferred — explicit non-goals)

- **Encryption of secrets at rest.** `client_secret` is NOT persisted in
  v1. Operators re-enter it per session for service-principal auth.
  A future issue can add OS-keychain integration (`keyring`) or a
  passphrase-derived key.
- **Multi-user / shared deployment support.** This store is single-user,
  single-host. Not safe for shared filesystems.
- **Migrations framework.** v1 ships one schema. Future schema changes
  will be handled by an additive migration helper when we get there;
  do not pull in Alembic for a single-table store.
- **Connection picker UI / multiple saved profiles.** The API supports it
  (`list_connections`, `set_active_connection`); the UI ships single-slot.
- **Cross-process locking.** SQLite handles this; we do not add app-level
  locks.
- **Telemetry / audit log of edits.** Not in scope.

## Work breakdown

### Kevin (Backend)

1. Add `app/services/settings_store.py` with the API above. Implement the
   bodies. No new runtime deps; stdlib `sqlite3` + `pathlib` only.
2. Implement `DEFAULT_DB_PATH`, `get_db_path()` env-var override, and
   schema initialization (`CREATE TABLE IF NOT EXISTS` + partial index).
3. Implement save/load/delete/list with the documented client_secret-drop
   guarantee. Use `BEGIN IMMEDIATE` for activation transitions.
4. Wire `app/connection_state.py`:
   - hydrate session from active row in `ensure_session_defaults`;
   - delegate `save_connection` to the store and set active;
   - keep auth helpers untouched.
5. Smoke-test locally with `streamlit run app/main.py`: enter a
   connection, restart the app, confirm it reloads.
6. No README changes — Scott will pick that up after the store lands.

### Charlie (Tester)

1. Add `tests/test_settings_store.py`:
   - happy-path round-trip (save → load → list → delete);
   - `client_secret` is dropped on save (load returns empty string);
   - `set_active_connection` flips active correctly; partial unique
     index allows zero or one active row;
   - `get_active_connection_name` returns None on empty DB and the right
     name after activation;
   - `ADME_SETTINGS_DB` env-var override is honored;
   - schema is idempotent (`initialize_store` called twice is a no-op);
   - missing-name `delete_connection` is a no-op (no exception);
   - `set_active_connection` on unknown name raises
     `SettingsStoreError`;
   - all tests use `tmp_path`; none touch the real home directory.
2. Extend `tests/test_connection_state.py`:
   - `ensure_session_defaults` hydrates `CONNECTION_KEY` from a stubbed
     active row;
   - `save_connection` writes through to the store (mock or fake store);
   - changing the connection still clears auth and health state.
3. Extend `tests/test_settings_page.py` minimally:
   - asserting the existing save flow still passes after persistence
     wiring (no new UI assertions needed for v1).
4. Validation gates: full `pytest`, `ruff check app tests`,
   `mypy app tests` all green before requesting review.

### Sequencing

- Kevin lands `settings_store.py` (steps 1–3) first. Charlie can write
  the store-only tests in parallel against the published signatures.
- Kevin then wires `connection_state.py` (step 4). Charlie's session-
  state and page tests follow.
- Satya reviews before merge; any change to the public function
  signatures above requires lead sign-off.

---

### 2026-05-11: File Upload page contract (page 6, Operate group)

**By:** Satya (Lead)
**Requested by:** Brady (mariel)
**What:** Hand-off contract for the v1 File Upload feature — a new page that drives the canonical OSDU File Service v2 three-call flow (signed URL → PUT bytes → metadata POST). Targets Judson (page), Kevin (service + models), Hockney (tests), Charlie (review).
**Why:** Pulled from backlog **Now**. Unblocks the next ingestion milestone: operators currently have no in-app way to land a file in the data partition without external tools (Storage Explorer, curl). This is the smallest path from "file on my laptop" to "FileSource I can paste into a manifest."

**Pending Kevin reconcile:** Darryl is researching `/api/file/v2/files/uploadURL` and `/files/metadata` payloads in parallel (`.squad/decisions/inbox/darryl-file-upload-api.md`, not yet landed). The endpoint paths, header names, and three-call sequencing in this contract are canonical and won't move. The **metadata POST body shape** (record kind, `data.DatasetProperties.FileSourceInfo` nesting, schema version) is asserted from canonical OSDU docs but should be reconciled against Darryl's verified controller findings before Kevin codes the request body. If Darryl finds a material delta, Kevin notes it in `kevin-file-upload-impl-notes.md` and we adjust; the page contract below does not change.

---

## Scope

**In v1:**
- Single-file upload, file ≤ 100 MB
- One legal tag, one ACL owner group, one ACL viewer group (selectboxes, like Ingestion)
- Display name (defaults to filename) + optional description
- Three-call pipeline behind a single button click
- Sticky error, history dataframe, latency chart (standard page pattern)
- Surface the new record id with a Search link and an "Upload another" button

**Not in v1** (deferred — see backlog):
- Chunked / resumable upload (>100 MB)
- Multiple files in one upload
- Direct paste of `FileSource` into the Ingestion page manifest (cross-page wiring is a separate ticket)
- Updating existing file metadata
- File preview / thumbnails
- Tagging / Acl edits post-upload

---

## Page location & navigation

- **New file:** `app/pages/6_📂_File_Upload.py`
- **Group:** `Operate` (alongside Ingestion and Search)
- **Order:** after Search

**`app/main.py` changes:**
- Add `FILE_UPLOAD_PAGE_PATH = "pages/6_📂_File_Upload.py"` next to the other page-path constants.
- Add a `file_upload_page = st.Page(FILE_UPLOAD_PAGE_PATH, title="File Upload", icon="📂")` declaration alongside `search_page`.
- Append `file_upload_page` to the `Operate` list in the `st.navigation({...})` call: `"Operate": [ingestion_page, search_page, file_upload_page]`.
- No changes to the home page page-links section in v1 (kept lean, like Ingestion / Search).

---

## Service module — `app/services/files.py`

New module. Same shape and conventions as `app/services/legal_tags.py`: stdlib + `requests` only, per-call timeout, no internal retries, returns frozen result dataclasses from `app.models.osdu`. Correlation header extraction reuses the same four-name probe (`correlation-id`, `x-correlation-id`, `request-id`, `x-request-id`).

**Constants:**
```python
FILES_TIMEOUT_SECONDS = 10                     # ADME calls (URL request, metadata POST)
FILE_UPLOAD_BYTES_TIMEOUT_SECONDS = 120        # PUT to Azure blob — bytes can be slow
FILE_UPLOAD_MAX_BYTES = 100 * 1024 * 1024      # 100 MB hard cap in v1

FILES_UPLOAD_URL_PATH = "/api/file/v2/files/uploadURL"
FILES_METADATA_PATH = "/api/file/v2/files/metadata"
```

**Public functions:**

1. `get_upload_url(connection: ADMEConnection, token: str) -> UploadURLResult`
   - GET `{endpoint}{FILES_UPLOAD_URL_PATH}` with standard ADME headers (`Authorization`, `data-partition-id`, `Accept: application/json`).
   - 5–10s timeout. Parses JSON for `Location.SignedURL` (signed URL) and `FileID` plus the `FileSource` returned in the body. Field names per canonical File Service v2 — **pending Kevin reconcile against Darryl's research.**
   - On 2xx and JSON-shape-valid: `ok=True`, populate `signed_url`, `file_source`, `file_id`.
   - On any non-2xx, JSON parse error, or missing required field: `ok=False`, fill `http_status` / `error_message`, leave URL fields `None`.

2. `upload_file_bytes(signed_url: str, file_bytes: bytes, content_type: str, *, timeout: int = FILE_UPLOAD_BYTES_TIMEOUT_SECONDS) -> UploadBytesResult`
   - PUT directly to the signed URL with headers:
     - `x-ms-blob-type: BlockBlob`
     - `Content-Type: {content_type}` (the file's MIME type, or `application/octet-stream` fallback)
     - `Content-Length: {len(file_bytes)}`
   - **No `Authorization`, no `data-partition-id`** — this call is to Azure Blob Storage via the signed URL, not to ADME.
   - **Docstring MUST state explicitly:** "This is the only call in the codebase that does not go through `_call_*`. It hits Azure Blob Storage directly via the signed URL returned by `get_upload_url`. There is no ADME correlation id on this response — `UploadBytesResult.correlation_id` does not exist by design."
   - Success criterion: HTTP 201 (Azure standard for PUT BlockBlob). Treat any other status as failure.
   - `bytes_uploaded` is `len(file_bytes)` on success, `0` on failure.

3. `post_file_metadata(connection, token, *, file_source: str, file_id: str, display_name: str, description: str, legal_tag: str, acl_owners: str, acl_viewers: str) -> FileMetadataResult`
   - POST `{endpoint}{FILES_METADATA_PATH}` with standard ADME headers + JSON body.
   - Body skeleton (**field nesting pending Kevin reconcile**):
     ```json
     {
       "kind": "{partition}:wks:dataset--File.Generic:1.0.0",
       "acl": {"owners": ["{acl_owners}"], "viewers": ["{acl_viewers}"]},
       "legal": {"legaltags": ["{legal_tag}"], "otherRelevantDataCountries": ["US"]},
       "data": {
         "Name": "{display_name}",
         "Description": "{description}",
         "DatasetProperties": {
           "FileSourceInfo": {"FileSource": "{file_source}", "Name": "{display_name}"}
         }
       }
     }
     ```
   - Treat the `kind` partition prefix as derived from `connection.data_partition_id` (Kevin: same helper pattern as ingestion's manifest stamper).
   - `otherRelevantDataCountries` defaults to `["US"]` for v1 — same default Charlie locked in for ingestion. If we need to make this configurable, that's a follow-up.
   - On 2xx: parse `id` (record id) and `version` from response, set `ok=True`.
   - On non-2xx: `ok=False`, populate `error_message`.

**Private helper:**

`_call_files(method, connection, token, path, *, json_body=None, params=None, timeout=FILES_TIMEOUT_SECONDS)` — mirrors `_call_legal` exactly: builds URL, builds headers (auth + data-partition + accept + optional content-type), times the call with `perf_counter`, catches `requests.RequestException`, extracts correlation id. Returns a `(response_or_none, latency_ms, correlation_id, error_message)` tuple — same shape as the legal-tags helper so Kevin can port the test fixtures directly.

`upload_file_bytes` does NOT use `_call_files` (different host, different headers, different success code, no correlation header). It has its own minimal try/except around `requests.put` with its own `perf_counter` timer.

---

## Models — `app/models/osdu.py` extension

Three new frozen dataclasses, added after the existing legal-tag results. All include `ok: bool`, `http_status: int | None`, `latency_ms: float = 0.0`, `error_message: str | None = None` to match the established result shape.

```python
@dataclass(frozen=True)
class UploadURLResult:
    """Outcome of GET /api/file/v2/files/uploadURL."""
    ok: bool
    http_status: int | None = None
    latency_ms: float = 0.0
    correlation_id: str | None = None
    error_message: str | None = None
    signed_url: str | None = None
    file_source: str | None = None
    file_id: str | None = None


@dataclass(frozen=True)
class UploadBytesResult:
    """Outcome of PUT to Azure Blob signed URL.

    NOTE: No ``correlation_id`` field — Azure Blob Storage does not
    emit an ADME correlation header on this call.
    """
    ok: bool
    http_status: int | None = None
    latency_ms: float = 0.0
    error_message: str | None = None
    bytes_uploaded: int = 0


@dataclass(frozen=True)
class FileMetadataResult:
    """Outcome of POST /api/file/v2/files/metadata."""
    ok: bool
    http_status: int | None = None
    latency_ms: float = 0.0
    correlation_id: str | None = None
    error_message: str | None = None
    record_id: str | None = None
    record_version: int | None = None
```

---

## Page layout (top to bottom)

`app/pages/6_📂_File_Upload.py`. Follows the same skeleton as `5_🔍_Search.py`: imports → constants → session defaults → `_render_*` helpers → main `render()` → `render()` call at module bottom.

1. **Title + intro markdown.** `st.title("📂 File Upload")` then 1–2 sentences: "Upload a single file to your ADME instance via the OSDU File Service. Returns a record id and FileSource you can reference from an ingestion manifest."

2. **Pre-flight chain.** Reuse `_render_preflight` pattern from Ingestion / Legal Tags / Search:
   - Connection configured?
   - Token resolvable?
   - File service reachable? (Optional — Kevin's call. v1 may skip the dedicated probe and let the first `get_upload_url` failure surface naturally with the sticky error, same as Search did initially.)

3. **Sticky error panel.** `_render_sticky_error(st.session_state, FILE_UPLOAD_LAST_ERROR_KEY)` with a Dismiss button — same shape as Ingestion. Cleared on explicit dismiss or on the next successful pipeline run.

4. **Step 1 — Select file.**
   - `st.file_uploader("Choose a file", accept_multiple_files=False, key="file_upload_uploader_widget")`
   - When a file is selected, render a caption block: `Filename: {name}` · `Size: {human_readable}` · `MIME: {type or "unknown"}`.
   - If `size > FILE_UPLOAD_MAX_BYTES`: render `st.error("Files over 100 MB are not supported in v1. Use Azure Storage Explorer to upload large files, then register the FileSource via the API directly.")` and gate the submit button off.
   - **Do NOT lock the uploader widget key.** Streamlit manages `UploadedFile` lifecycle internally; locking would corrupt it.

5. **Step 2 — Metadata.** Three `st.selectbox`es populated from live API on first page load (same auto-run pattern as Ingestion's `_ingestion_options_autorun`):
   - Legal tag — from `list_legal_tags`
   - ACL owners — from entitlements groups
   - ACL viewers — from entitlements groups
   - Plus `st.text_input("Display name", value=<filename>, key=FILE_UPLOAD_DISPLAY_NAME_KEY)`
   - Plus `st.text_area("Description (optional)", key=FILE_UPLOAD_DESCRIPTION_KEY)`
   - Default for Display name: when a file is freshly selected and `file_upload_display_name` is empty, prefill it with the filename. Use the same one-shot prefill guard Judson used on Ingestion (set once per uploaded file, never overwrite an operator edit).

6. **Step 3 — Upload + Register button.**
   - `st.button("Upload + Register", type="primary", disabled=<gate>)`
   - **Gate is True when:** no file selected, file > 100 MB, no legal tag selected, no ACL owner / viewer selected, display name empty.
   - On click, the handler runs the full three-call pipeline (below).

7. **Progress indicator.** `with st.status("Uploading file…", expanded=True) as status:` block, with three sequential `status.write("Phase N — …")` markers:
   - "Phase 1 — Requesting signed upload URL…"
   - "Phase 2 — Uploading bytes to Azure Blob Storage…"
   - "Phase 3 — Registering file metadata with ADME…"
   - On success: `status.update(label="Upload complete", state="complete")`.
   - On failure at any phase: `status.update(label="Upload failed at Phase N", state="error")` and write the sticky error.

8. **Result panel** (rendered after a successful pipeline run, gated on `file_upload_last_result` being a successful `FileMetadataResult`):
   - `st.success(f"File registered. Record id: {record_id}")`
   - Display in a small two-column block: Record id, FileSource (with copy-to-clipboard caption — Streamlit's `st.code` is fine for v1), latency totals per phase.
   - `st.page_link(SEARCH_PAGE_PATH, label="View in Search", icon="🔍")` — operator can pivot directly.
   - `st.button("Upload another")` — on click, clears the uploader widget (via `st.rerun()` after clearing relevant keys) and the last-result key. Does NOT clear the legal tag / ACL selections (operator likely wants to upload several files with the same tagging).

9. **History dataframe + latency chart.** Standard pattern, identical to Ingestion / Search:
   - `st.session_state[FILE_UPLOAD_HISTORY_KEY]` is a list of dicts: `{timestamp, filename, size_bytes, record_id, total_latency_ms, phase1_ms, phase2_ms, phase3_ms, ok}`.
   - Render with `st.dataframe`.
   - Line chart of `total_latency_ms` over time. Keep last 50 entries.

---

## Locked session-state keys

All of the following are explicit constants at the top of the page module and registered in `ensure_session_defaults` (Kevin: extend `app/connection_state.py` or — cleaner — add a local `_ensure_file_upload_defaults` like Ingestion does):

| Constant | Key | Default |
|---|---|---|
| `FILE_UPLOAD_AUTORUN_KEY` | `file_upload_autorun_done` | `False` |
| `FILE_UPLOAD_LEGAL_TAG_OPTIONS_KEY` | `file_upload_legal_tag_options` | `None` |
| `FILE_UPLOAD_ACL_OWNER_OPTIONS_KEY` | `file_upload_acl_owner_options` | `None` |
| `FILE_UPLOAD_ACL_VIEWER_OPTIONS_KEY` | `file_upload_acl_viewer_options` | `None` |
| `FILE_UPLOAD_LEGAL_TAG_KEY` | `file_upload_legal_tag` | `None` |
| `FILE_UPLOAD_ACL_OWNERS_KEY` | `file_upload_acl_owners` | `None` |
| `FILE_UPLOAD_ACL_VIEWERS_KEY` | `file_upload_acl_viewers` | `None` |
| `FILE_UPLOAD_DISPLAY_NAME_KEY` | `file_upload_display_name` | `""` |
| `FILE_UPLOAD_DESCRIPTION_KEY` | `file_upload_description` | `""` |
| `FILE_UPLOAD_LAST_RESULT_KEY` | `file_upload_last_result` | `None` |
| `FILE_UPLOAD_HISTORY_KEY` | `file_upload_history` | `[]` |
| `FILE_UPLOAD_LAST_ERROR_KEY` | `file_upload_last_error` | `None` |

**Not locked:** `file_upload_uploader_widget` — Streamlit's `st.file_uploader` binds this automatically and owns the `UploadedFile` lifecycle. Touching it from our code breaks the widget. Tests assert this key is NOT in the locked-keys constant set.

**Widget-mutation guard:** Same pattern as Ingestion. Before the pipeline runs, snapshot the `st.text_input` / `st.text_area` values (display name, description) into locals. Use the locals throughout the pipeline; do not re-read `st.session_state[...]` after the button handler starts, because Streamlit's mid-script rerun can mutate the widget-bound key.

---

## Edge cases & failure handling

| Case | Behavior |
|---|---|
| File > 100 MB | Hard-block in UI before submit. Friendly error pointing to Azure Storage Explorer. No pipeline call. |
| Display name empty | Hard-block submit. (No friendly error needed — just keep button disabled.) |
| No legal tag selected | Hard-block submit. Gate before pipeline opens. |
| No ACL owner OR viewer selected | Hard-block submit. |
| `get_upload_url` returns 401/403 | Sticky error: "Authorization failed when requesting signed URL. Re-check your token and File Service entitlements." No PUT attempted. Pipeline ends at Phase 1. |
| `get_upload_url` returns 5xx / timeout | Sticky error with the server message. Suggest retry. No PUT attempted. |
| `upload_file_bytes` PUT timeout (120s) | Sticky error: "Upload timed out after 120 seconds. The file may be too large or your network is slow. Retry, or use Azure Storage Explorer for files near the 100 MB limit." No metadata POST attempted. |
| `upload_file_bytes` non-201 from Azure | Sticky error with the HTTP status and any body snippet. No metadata POST. The signed URL is one-shot; operator must retry from Phase 1 (just click again). |
| `post_file_metadata` fails AFTER successful PUT | **Critical:** sticky error MUST surface the `file_id` from Phase 1: "File uploaded successfully (file id: `{file_id}`) but metadata registration failed: `{error_message}`. The bytes are in storage but the record does not exist. Contact an admin to register the metadata manually, or retry the upload." The file_id is also written into `file_upload_history` with `ok=False` and the partial-state marker, so it survives a page navigation. |
| Pre-flight chain has any failure | Render pre-flight panel only; do not render Steps 1–3 at all (same as Ingestion). |
| Operator dismisses sticky error | Clear `file_upload_last_error` only. Do not touch `file_upload_last_result` or `file_upload_history`. |

---

## Tests (Hockney's checklist)

**`tests/test_files_service.py`** (mirror `test_legal_tags_service.py`):
- `get_upload_url` happy path (200 with all fields)
- `get_upload_url` 401, 403, 500, timeout, connection-error, JSON-parse-error, missing required field
- `upload_file_bytes` happy path (201) — assert headers include `x-ms-blob-type: BlockBlob` and exclude `Authorization` / `data-partition-id`
- `upload_file_bytes` 400, 403, 503, timeout
- `upload_file_bytes` `bytes_uploaded` correctness on success and on failure (`0`)
- `post_file_metadata` happy path — assert body shape matches the skeleton above and `kind` partition prefix is correct
- `post_file_metadata` 400 (validation), 403 (entitlement), 500
- Correlation-id extraction on `get_upload_url` and `post_file_metadata`, all four header names

**`tests/test_file_upload_page.py`** (mirror `test_search_page.py`):
- Locked session-state keys constant is comprehensive (the locked-keys assertion test catches the omission)
- `file_upload_uploader_widget` is explicitly NOT in the locked set
- Pre-flight gating: page renders only the pre-flight panel when not connected
- File >100 MB blocks the submit button
- Display name auto-prefill from filename, but does not overwrite operator edits
- "Upload another" button clears `file_upload_last_result` and the uploader widget but preserves legal tag / ACL selections
- Sticky-error dismiss clears only `file_upload_last_error`
- Three-phase progress block renders correctly on happy path
- Phase 3 failure surfaces the Phase 1 file_id in the sticky error and the history row

---

## Hand-off summary

- **Kevin** (Backend): owns `app/services/files.py`, model extensions in `app/models/osdu.py`, navigation update in `app/main.py`. Reconcile the metadata POST body shape against Darryl's File Service research before coding the request body — note any deltas in `kevin-file-upload-impl-notes.md`. Do NOT change the contract surface (function signatures, dataclass shapes, page-side keys) without re-circulating.
- **Judson** (Frontend): owns `app/pages/6_📂_File_Upload.py`. Mirror the structural conventions from Ingestion (`4_📥_Ingestion.py`) for autorun + selectboxes and from Search (`5_🔍_Search.py`) for the three-step pipeline rhythm.
- **Hockney** (Tester): owns `tests/test_files_service.py` and `tests/test_file_upload_page.py`. Use `tests/support/streamlit_recorder.py` for page tests, same pattern as Search.
- **Charlie** (Reviewer): reviews the whole bundle before merge. Strict lockout applies — author cannot self-revise on rejection.

**Termination condition for v1:** A clean file ≤ 100 MB uploads via the page in under 15 seconds end-to-end on a healthy instance, produces a record id that resolves via Search, and a phase-3 failure does not leave the operator without the file id.

---

### 2026-05-11: Manifest Builder v1 — contract & placement
**By:** Satya (Lead) — requested by Brady (mariel)
**Status:** Approved, ready for Kevin (service) + UI implementer

## Decision summary

Ship a **Manifest Builder v1** that turns an uploaded FileSource into a valid `dataset--File.Generic:1.0.0` manifest and injects it into the existing Manifest editor for review and submit. This closes the TNO E2E loop: **File upload → Manifest build → Manifest submit → Search**.

---

## Page placement — Option A (locked)

The Builder lives **inside the Manifest page** (`app/pages/5_📄_Manifest.py` after Judson's rename) as a tab or expander **above the JSON editor**. One sidebar entry, one workflow surface, no extra navigation.

- Implement as a `st.tabs(["🛠️ Build", "✍️ Edit & Submit"])` at the top of the Manifest page, OR a `st.expander("🛠️ Build manifest from upload", expanded=False)` above the editor — implementer's call, both acceptable.
- Hand-pasted/hand-edited manifests still work exactly as today. Builder is additive; never required.

Rejected: Option B (separate `5b_🛠️_Builder.py`). Splits the workflow and forces the operator to navigate between pages with session-state carrying the manifest — fragile and worse UX.

---

## Service layer — `app/services/manifest_builder.py` (Kevin owns)

Pure function, plain-dict output, no new dataclasses:

```python
def build_file_generic_manifest(
    *,
    file_source: str,
    file_id: str,
    display_name: str,
    description: str | None,
    kind: str,
    legal_tag: str,
    acl_owners: list[str],
    acl_viewers: list[str],
    data_partition_id: str,
) -> dict:
    ...
```

**Rules:**
- `ValueError` on empty/missing required: `file_source`, `file_id`, `display_name`, `kind`, `legal_tag`, at least one `acl_owner`, at least one `acl_viewer`, `data_partition_id`.
- `description` is optional; omit the field from `data` if blank rather than emitting empty string.
- Field shape MUST match OSDU `dataset--File.Generic:1.0.0` EXACTLY. Reference Kevin's existing `post_file_metadata` body (in `app/services/files.py`) for the canonical field names — in particular:
  - `data.DatasetProperties.FileSourceInfo.FileSource`
  - `data.DatasetProperties.FileSourceInfo.PreloadFilePath` (if applicable per Darryl's File Upload research)
  - `data.ResourceSecurityClassification`, `data.Name`, `data.Description`
  - Top-level `kind`, `acl.owners`, `acl.viewers`, `legal.legaltags`, `legal.otherRelevantDataCountries`
- Output shape is the **manifest envelope** the existing Manifest page expects (single-record list under whatever wrapper the current submit flow consumes). Kevin: confirm against current `app/services/ingestion.py` submit body before finalizing.

**Tests required:**
- Happy path produces dict matching golden fixture.
- Each missing required field raises `ValueError` with field name in message.
- Empty `description` omits the field (does not emit `""`).
- ACL list inputs are passed through verbatim (no dedup, no sort — caller's job).

---

## UI inputs (in the Builder section)

| Input | Widget | Notes |
|---|---|---|
| File source mode | `st.radio` | `"Pick from recent uploads"` / `"Paste FileSource manually"` |
| Recent upload | `st.selectbox` | Source: `st.session_state["file_upload_history"]`. Display `f"{display_name} ({record_id})"`. Disabled in paste mode. |
| FileSource (paste) | `st.text_input` | Disabled in pick mode. |
| File ID | derived | From recent pick → record id; from paste → operator must also supply or derive (Kevin: confirm whether `file_id` is recoverable from `file_source` alone; if not, add a second `st.text_input` for paste mode). |
| Display name | `st.text_input` | Pre-filled from recent pick's display_name; editable. |
| Description | `st.text_area` | Optional. |
| Kind | `st.selectbox` | Default `osdu:wks:dataset--File.Generic:1.0.0`. Single option for v1; selectbox keeps it forward-compatible. |
| ACL owners | `st.selectbox` (or multiselect) | **Reuse the same loaded options the Manifest page already uses.** Do not re-fetch. |
| ACL viewers | same | same |
| Legal tag | `st.selectbox` | same — reuse existing loaded options |

**Generate manifest** button:
1. Validate inputs (block on missing required — show `st.error` listing missing fields).
2. Call `build_file_generic_manifest(...)`.
3. On `ValueError`: show `st.error`, do NOT inject.
4. On success: stash dict in `manifest_builder_last_generated`, set `manifest_builder_pending_text` to `json.dumps(dict, indent=2)`, `st.rerun()`.

---

## Locked session-state keys

| Key | Owner | Purpose |
|---|---|---|
| `manifest_builder_pick_mode` | Builder UI | `"recent"` or `"paste"` |
| `manifest_builder_recent_choice` | Builder UI | record_id of selected recent upload |
| `manifest_builder_file_source` | Builder UI | FileSource value (paste mode) |
| `manifest_builder_display_name` | Builder UI | bound to display_name text_input |
| `manifest_builder_description` | Builder UI | bound to description text_area |
| `manifest_builder_kind` | Builder UI | bound to kind selectbox |
| `manifest_builder_pending_text` | Builder UI → Editor | sentinel; primes the editor on next rerun |
| `manifest_builder_last_generated` | Builder UI | last generated dict, for diagnostics/debug expander |

The existing Manifest editor key (whatever the current page binds to — implementer: read it from the current Manifest page before changing anything) is the **target** of `manifest_builder_pending_text`. Do NOT rename the existing key.

---

## Locked widget-mutation pattern (sentinel-prime)

Streamlit forbids writing to a widget's bound session-state key after the widget has rendered in the same run. Pattern:

```python
# At the TOP of the Manifest page, BEFORE the manifest text_area renders:
if st.session_state.get("manifest_builder_pending_text") is not None:
    st.session_state[MANIFEST_EDITOR_KEY] = st.session_state.pop("manifest_builder_pending_text")

# ... later, the editor renders:
st.text_area("Manifest JSON", key=MANIFEST_EDITOR_KEY, height=400)
```

The Generate button does:
```python
st.session_state["manifest_builder_pending_text"] = json.dumps(generated, indent=2)
st.rerun()
```

This is the **only** sanctioned way to inject. Do not assign to `MANIFEST_EDITOR_KEY` directly from the Generate handler.

---

## Edge cases (locked)

- **No recent uploads in session AND paste mode empty** → Generate blocked, `st.info` explaining "Upload a file first or paste a FileSource."
- **Any required selectbox/legal tag empty** → Generate blocked, `st.error` listing missing fields.
- **`build_file_generic_manifest` raises `ValueError`** → `st.error` with the message in the Builder section, do NOT inject.
- **Operator hand-edits after Generate** → fine. Inject is one-shot per click. No diffing, no warnings.
- **Operator clicks Generate twice** → second click overwrites the editor on next rerun. Acceptable for v1; document in walkthrough.
- **Selected recent upload disappears from history mid-session** (e.g., session cleared) → selectbox falls back to first available or empty; Generate revalidates.

---

## Non-goals for v1 (do NOT scope-creep)

- Multi-record manifests
- Round-trip editing (loading an existing manifest back into the Builder)
- Diff/merge of generated + hand-edited content
- Saving Builder presets
- Cross-session persistence of Builder inputs

---

## Coordination notes

- **Judson** is renaming pages in parallel. Builder code MUST target the **post-rename** filename `app/pages/5_📄_Manifest.py`. If implementer arrives before Judson's rename lands, coordinate merge order with Squad — do NOT ship Builder against the old `4_📥_Ingestion.py` filename.
- **Kevin** owns `app/services/manifest_builder.py` and its tests. UI implementer consumes it.
- **Darryl's** File Upload research is the source of truth for the `dataset--File.Generic:1.0.0` field shape. If field names disagree between this contract and Darryl's notes, Darryl wins — flag the diff back to Squad.

---

## Follow-up: E2E walkthrough doc (Satya, after Builder ships)

After Builder lands and is verified end-to-end against TNO, Satya will write `docs/walkthroughs/tno-end-to-end.md` covering:

1. Open File page, upload a sample file, copy the record id.
2. Open Manifest page, open Builder section, pick the upload (or paste FileSource), click **Generate**.
3. Manifest editor pre-fills with valid JSON — review.
4. Click **Validate & Ingest**.
5. Watch ingestion status, then verify the new record in Search.

Walkthrough is NOT a blocker for Builder ship; it's the receipt that the loop closes.

---

### 2026-05-12: Run History contract (backlog #1)

**By:** Satya (Lead), requested by Mariel
**Why:** Lock the storage + service + UI contract before Judson implements. Run History is the substrate for bulk submit (#4a) and TNO end-to-end (#4) — needs to survive crashes and handle "500 submitted, show me the 12 that failed" without going quadratic.

---

## Decisions

### 1. Storage backend
**Locked: SQLite** via stdlib `sqlite3`. One file, indexed queries, zero install pain, zero new deps. JSON sidecar rejected — linear scans on a 500-record bulk run will be visibly bad, and once we add status filters + date filters in the UI it gets worse. Schema versioning via `PRAGMA user_version` (v1 = the schema below).

### 2. Storage location
**Locked: `{user_home}/.adme-ingestion-tool/run-history.db`.**
- Resolved via `pathlib.Path.home() / ".adme-ingestion-tool" / "run-history.db"`.
- Directory created lazily on first write (`mkdir(parents=True, exist_ok=True)`, mode 0o700 on POSIX, best-effort on Windows).
- Survives repo clones / branch switches. Same parent dir as the existing settings store (consistent mental model for "where does this tool keep state").
- **Override hook:** if the env var `ADME_RUN_HISTORY_DB` is set, use that path instead. Tests use this to point at a tmpdir. No other knobs.

### 3. Schema (v1)

```sql
PRAGMA user_version = 1;
PRAGMA journal_mode = WAL;   -- crash-safe + concurrent readers
PRAGMA foreign_keys = ON;

CREATE TABLE workflow_runs (
    run_id            TEXT PRIMARY KEY,
    submitted_at      TEXT NOT NULL,   -- ISO 8601 UTC, e.g. "2026-05-12T15:00:00Z"
    finished_at       TEXT,            -- NULL until terminal
    status            TEXT NOT NULL,   -- "submitted" | "running" | "finished" | "failed"
    kind              TEXT,            -- top-level manifest kind, or "mixed" for bulk
    correlation_id    TEXT,
    error_message     TEXT,
    latency_ms        INTEGER,         -- end-to-end submit→terminal, NULL until finished
    submit_source     TEXT NOT NULL,   -- "manifest_page" | "builder" | "bulk_runner" | "tno_loader"
    data_partition_id TEXT NOT NULL
);

CREATE TABLE file_uploads (
    record_id         TEXT PRIMARY KEY,
    uploaded_at       TEXT NOT NULL,   -- ISO 8601 UTC
    display_name      TEXT NOT NULL,
    file_source       TEXT NOT NULL,
    size_bytes        INTEGER,
    data_partition_id TEXT NOT NULL
);

CREATE INDEX idx_runs_submitted ON workflow_runs(submitted_at DESC);
CREATE INDEX idx_runs_status    ON workflow_runs(status);
CREATE INDEX idx_runs_partition ON workflow_runs(data_partition_id);
CREATE INDEX idx_uploads_when      ON file_uploads(uploaded_at DESC);
CREATE INDEX idx_uploads_partition ON file_uploads(data_partition_id);
```

**Locked.** Notes:
- All timestamps stored as ISO 8601 UTC strings (`"2026-05-12T15:00:00Z"`) so they sort lexicographically and round-trip without timezone surprises. Helper `_utcnow_iso()` in the module is the only place we format them.
- `status` is the **normalized** value (lowercase string). The service writes only the four allowed values; readers translate to `WorkflowStatus` (from `app.models.osdu`) at the boundary. Don't import the enum into the schema — keeps the DB independent of code.
- `run_id` is the OSDU workflow run id from `submit_manifest` response. If the submit POST itself fails (no run id returned), do NOT insert a row — there's no run to track. Bulk runner will surface those as submit-time errors separately (out of scope for #1).
- WAL mode → safe across Streamlit reruns + a future bulk worker thread reading the table while another writes.

### 4. Service module — `app/services/run_history.py`

**Locked.** New module. Pure functions (no class), one module-level `_get_conn()` that opens/initializes lazily. Returns frozen+slots dataclasses to match `app/models/osdu.py` conventions.

```python
@dataclass(frozen=True, slots=True)
class RunRow:
    run_id: str
    submitted_at: str           # ISO 8601 UTC
    finished_at: str | None
    status: WorkflowStatus      # parsed via parse_workflow_status at read time
    kind: str | None
    correlation_id: str | None
    error_message: str | None
    latency_ms: int | None
    submit_source: str
    data_partition_id: str

@dataclass(frozen=True, slots=True)
class UploadRow:
    record_id: str
    uploaded_at: str
    display_name: str
    file_source: str
    size_bytes: int | None
    data_partition_id: str
```

**Public API (locked):**

```python
def record_workflow_submit(
    *,
    run_id: str,
    submitted_at: str,           # ISO 8601 UTC; caller-supplied so tests can pin it
    kind: str | None,
    correlation_id: str | None,
    submit_source: str,          # one of the four submit_source values
    data_partition_id: str,
) -> None: ...

def record_workflow_finish(
    *,
    run_id: str,
    finished_at: str,
    status: WorkflowStatus,      # FINISHED or FAILED only — others ignored with no-op
    latency_ms: int,
    error_message: str | None = None,
) -> None: ...
# Semantics: UPDATE WHERE run_id = ?. If no row exists (we missed the submit
# for any reason), silently no-op. Do NOT insert a finish-only row — that
# would have NULL submitted_at and break the timeline.

def record_file_upload(
    *,
    record_id: str,
    uploaded_at: str,
    display_name: str,
    file_source: str,
    size_bytes: int | None,
    data_partition_id: str,
) -> None: ...
# INSERT OR REPLACE on record_id (re-uploads overwrite the previous row).

def list_workflow_runs(
    *,
    limit: int = 100,
    status: WorkflowStatus | None = None,
    since: str | None = None,            # ISO 8601 UTC; filters submitted_at >= since
    data_partition_id: str | None = None,
) -> list[RunRow]: ...
# Always ORDER BY submitted_at DESC. limit clamped to [1, 10_000].

def list_file_uploads(
    *,
    limit: int = 100,
    since: str | None = None,
    data_partition_id: str | None = None,
) -> list[UploadRow]: ...
# Always ORDER BY uploaded_at DESC. limit clamped to [1, 10_000].

def delete_run(run_id: str) -> bool: ...   # True if a row was deleted
def delete_upload(record_id: str) -> bool: ...

def purge_older_than(*, days: int) -> tuple[int, int]: ...
# Returns (runs_deleted, uploads_deleted). Compares against submitted_at /
# uploaded_at. days must be >= 1.

def clear_all() -> None: ...
# DELETE FROM both tables. For the "Clear all" UI button.
```

**Connection management (locked):**
- Module-level `_conn: sqlite3.Connection | None = None`.
- `_get_conn()` opens the DB, runs the migration runner, sets `row_factory = sqlite3.Row`, returns the conn.
- Migration runner reads `PRAGMA user_version`, applies migrations from 0 → current (v1 only for now). Pattern: list of `(target_version, sql_script)` tuples, run in order.
- All writes wrapped in `with conn:` (autocommit on success, rollback on exception).
- No threading lock — sqlite3 is fine with the default `check_same_thread=True` since Streamlit reruns happen on the same thread. If bulk runner ever needs a worker thread, it opens its own connection (revisit then, not now).
- Test override: `ADME_RUN_HISTORY_DB` env var read at `_get_conn()` time. Also expose `_reset_for_tests()` (underscore-prefixed) that closes the conn and clears the module-level cache.

### 5. Migration / schema versioning
**Locked.**
- v1 schema applied on first open when `user_version == 0`.
- Future bumps add `(N, sql)` entries to the migration list. Each migration is a single transaction.
- Never downgrade. If `user_version > current_known`, log a warning and proceed read-only-ish (still try to operate; field additions are usually forward-compatible).

### 6. Wiring touchpoints
**Locked.** Judson wires these — they are the only places `record_*` gets called.

| Caller | When | What to record |
|---|---|---|
| `app/pages/5_📄_Manifest.py` — after `submit_manifest()` returns a run id | `record_workflow_submit(submit_source="manifest_page", kind=<top-level kind>, ...)` | submit |
| `app/pages/5_📄_Manifest.py` — after `get_workflow_status()` returns FINISHED/FAILED | `record_workflow_finish(...)` | finish |
| Manifest Builder (same page, Build path) | Same two calls but `submit_source="builder"` | submit + finish |
| `app/pages/6_📂_File.py` — after metadata POST succeeds AND `upload_summary` row is appended | `record_file_upload(...)` | upload |
| Bulk runner (#4a, future) | `submit_source="bulk_runner"` | both |
| TNO loader (#4, future) | `submit_source="tno_loader"` | both |

**Important:** The existing `file_upload_history` session_state list (which the Builder's "recent uploads" picker reads) is NOT touched. Run History is **additive** — it persists across sessions but doesn't replace the in-session picker source. Builder keeps working unchanged.

**`kind` derivation:** for a single manifest, use the top-level `kind` from the manifest JSON. For bulk submits where one workflow call carries multiple kinds, write `"mixed"`. The service does not parse manifests — caller supplies the string.

**Latency:** caller computes `finished_at - submitted_at` in ms and passes it. Service does not subtract timestamps (keeps the timezone/format concern at the boundary).

### 7. UI page — `app/pages/8_📊_History.py`
**Locked.** Placement: **Operate** group in `app/main.py` nav, after Search. Rationale: it's "looking at past work," same family as Search. If Mariel wants it elsewhere later, it's a one-line change in `main.py`.

Three tabs (`st.tabs`):

1. **Workflow runs**
   - Columns: When (relative + absolute on hover), Kind, Status (colored badge: green finished, red failed, amber running/submitted, grey unknown), Latency, Run ID (monospace, copy button), Correlation ID (monospace, copy button), Source.
   - Filters above the table: status multiselect, date range (last 24h / 7d / 30d / all), partition (defaults to current connection's partition; checkbox "Show all partitions").
   - Page size: 100 default, "Load more" button to bump limit.

2. **File uploads**
   - Columns: When, Display name, Record ID (monospace, copy), File source (truncated middle if >60 chars, full on hover), Size (humanized).
   - Filters: date range, partition (same default).

3. **Actions**
   - Button: "Purge runs and uploads older than N days" with a number_input (default 30). Confirms via `st.checkbox("I understand this is permanent")` before the button activates.
   - Button: "Clear all history" — same confirmation pattern.
   - Caption: shows current DB path and row counts.

**Partition default:** show only the current connection's partition by default. Rationale: 95% of the time the operator wants "what did I just do here." The "Show all partitions" toggle handles the cross-partition case. (See open question #1 — happy to flip this if Mariel disagrees.)

**Empty states:** each tab shows a friendly "No runs yet — submit a manifest from the Manifest page" / "No uploads yet" message, never an empty table.

**No row-level actions in v1** beyond the per-row delete button (single delete is cheap, matches what Mariel asked for). Multi-select / bulk delete is explicitly non-goal.

### 8. Test approach
**Locked.**
- `tests/test_run_history_service.py` — service tests with a tmpdir DB via `ADME_RUN_HISTORY_DB`. Cover: empty DB returns empty list; submit then finish updates the row; finish without prior submit no-ops; filters (status / since / partition / limit); purge boundary; clear_all; idempotent re-upload via INSERT OR REPLACE.
- `tests/test_history_page.py` — page tests using `streamlit_recorder` (existing helper) with the service pointed at a tmp DB via the env var (set in the fixture, unset in teardown).
- No mocks of `sqlite3`. The DB is real, just tmpdir.
- Add fixture `run_history_tmp_db` in `tests/conftest.py` (sets env var, calls `run_history._reset_for_tests()` before and after).

### 9. Non-goals for v1 (locked, don't get scope-creeped)
- Bulk operations on history rows (multi-select delete).
- Cross-machine sync.
- Export to CSV (covered by backlog #6).
- Linking workflow runs to the file uploads they reference (different problem — needs manifest parsing).
- Retrying failed runs from the History page (separate UX problem).
- Search inside error_message (do it when there's a real use case).

---

## Open questions for Mariel

1. **Nav placement** — defaulted to Operate (after Search). OK, or do you want a fourth nav group "History"? My vote: Operate. Adding a group for one page feels heavy.
2. **Default partition filter** — defaulted to "current partition only" with a toggle for all partitions. OK, or always show all by default?
3. **Auto-purge policy** — v1 has manual purge only (button on the Actions tab). Do you want auto-purge >30 days on app start? My lean: no — destructive defaults are bad, and the DB stays small (one row per submit). Operator can purge when they want.
4. **Builder submit_source** — Builder lives on the Manifest page but goes through the same `submit_manifest()` path. I split them (`"builder"` vs `"manifest_page"`) so we can later answer "how often does the Builder path get used vs raw paste." Want them merged into one source value instead?

---

### 2026-05-11: Search page (Operate › Search) — contract

**By:** Satya (Lead), requested by Brady (mariel)
**Status:** Locked v1 contract for Kevin (services), Judson (page), Charlie (tests).
**Page:** `app/pages/5_🔍_Search.py`
**Service module (new):** `app/services/search.py`
**Models (additions):** `app/models/osdu.py`

> ⚠️ Darryl's `.squad/decisions/inbox/darryl-search-api.md` had not landed when
> this contract was written. Defaults here follow Search v2 + Storage v2
> conventions used in the rest of the codebase (Bearer + `data-partition-id`,
> per-call timeout, correlation header capture). Kevin reconciles if Darryl's
> findings differ — non-breaking deltas (e.g. exact aggregation field name)
> stay inside the service module and do **not** require contract changes.

---

## 1. Locked session-state keys

Charlie asserts these in `tests/test_search_page.py`. Keys are namespaced
`search_*` (matches `ingestion_*`, `legal_tags_*`, `entitlements_*`).

| Key | Type | Initial value | Purpose |
|---|---|---|---|
| `search_query_text` | `str` | `""` | Free-text Lucene `query` body field |
| `search_kind_filter` | `str` | `"*:*:*:*"` | Selected kind (wildcard = browse all) |
| `search_kind_options` | `list[str]` | `[]` | Dropdown options; `["*:*:*:*"]` + discovered kinds |
| `search_results` | `list[RecordSummary]` | `[]` | Current page of summaries (frozen dataclasses) |
| `search_total_count` | `int \| None` | `None` | Server `totalCount` for paging math; `None` = unknown |
| `search_page_offset` | `int` | `0` | Current offset; advances by `SEARCH_PAGE_SIZE` |
| `search_history` | `list[dict]` | `[]` | Append-only call log (timestamp, op, kind, query, count, latency_ms, ok, http_status, correlation_id) |
| `search_last_error` | `str \| None` | `None` | Sticky error string for the top-of-page banner |
| `search_selected_record_id` | `str \| None` | `None` | Row currently expanded for detail |
| `search_full_record_cache` | `dict[str, dict]` | `{}` | id → full Storage record JSON (populated by Fetch button) |
| `search_autorun_done` | `bool` | `False` | Autorun-once guard — flips True after first auto-browse |

**Constants** (module-level in the page; mirrored as kwargs into the
service module — see §2):
- `SEARCH_PAGE_SIZE = 100`
- `SEARCH_WILDCARD_KIND = "*:*:*:*"`
- `SEARCH_SORT = [{"field": "createTime", "order": "DESC"}]`
- `SEARCH_DATA_PREVIEW_CHARS = 240` (truncation cap for `source` preview in the dataframe)

**State init helper:** `_ensure_search_state()` mirrors
`_ensure_ingestion_state()`. Charlie's test will instantiate the page and
assert every key above is present with the documented initial value.

---

## 2. Service module contract — `app/services/search.py`

Pattern: stdlib + `requests`; **no internal retries**; one `_call_search`
helper modeled on `_call_legal` / `_call_entitlements`. Bearer +
`data-partition-id` headers, correlation_id capture from the standard
header set (`correlation-id`, `x-correlation-id`, `request-id`,
`x-request-id`), error-body truncated to 500 chars.

**Timeout:** `SEARCH_TIMEOUT_SECONDS = 15`. Rationale: Search v2 against
a cold index can be slower than Legal/Entitlements (which use 5s) but is
not Workflow-submit slow (30s). 15s is the middle ground; storage GETs
reuse the same constant.

### Endpoints
- `SEARCH_QUERY_PATH = "/api/search/v2/query"`  (POST)
- `STORAGE_RECORD_PATH = "/api/storage/v2/records"`  (GET `/{id}`)

### Functions

```python
def search_records(
    connection: ADMEConnection,
    token: str,
    *,
    kind: str,
    query: str | None = None,
    limit: int = SEARCH_PAGE_SIZE,
    offset: int = 0,
    sort: list[dict] | None = None,
) -> SearchPageResult: ...
```
- `kind` required and non-empty; wildcard `*:*:*:*` is the canonical
  "browse all". `query` empty/None → omit `query` field from body.
- `sort` defaults to `[{"field": "createTime", "order": "DESC"}]`.
- Request body:
  ```json
  {"kind": "<kind>", "query": "<query?>", "limit": 100,
   "offset": 0, "sort": {"field": ["createTime"], "order": ["DESC"]},
   "returnedFields": ["id","kind","createTime","source","version"]}
  ```
  Kevin: confirm exact `sort` shape against Search v2 OpenAPI when
  reconciling with Darryl. The contract guarantees a list/sequence of
  field+order pairs; the wire shape is service-module-internal.
- Raises `ValueError` when `kind` is empty or `limit < 1`.
- Returns `SearchPageResult` (see §2.b).

```python
def list_kinds(
    connection: ADMEConnection,
    token: str,
    *,
    sample_limit: int = 1000,
) -> KindAggregationResult: ...
```
- **Best-effort.** Try Search v2 aggregation first
  (`aggregateBy: "kind"`); on 4xx/5xx/transport failure fall back to a
  single page query with `kind="*:*:*:*"`, `limit=sample_limit` and
  extract the distinct `kind` values from `results[*].kind`.
- The result distinguishes the two paths via `KindAggregationResult.source`
  (`"aggregation"` vs `"page_sample"`). Page never crashes if both fail —
  result is `ok=True, kinds=[]` from `page_sample` with zero results.

```python
def get_record(
    connection: ADMEConnection,
    token: str,
    record_id: str,
) -> RecordDetailResult: ...
```
- `record_id` required, non-empty; URL-encoded with `quote(..., safe="")`.
- 404 returns `ok=False, http_status=404, error_message="Record not found"`.
- 200 returns `ok=True` with the full record dict in `record`.

### 2.b Result dataclasses (additions to `app/models/osdu.py`)

All `@dataclass(frozen=True, slots=True)`. `ok` and `latency_ms` populated
on every result (same invariant as legal_tags / entitlements).

> ⚠️ `app/models/osdu.py` already has a small `SearchResult` dataclass
> (~line 95) used nowhere yet. **Delete it** — `SearchPageResult` below
> supersedes it. Charlie's test for the search page asserts the new names.

```python
@dataclass(frozen=True, slots=True)
class RecordSummary:
    id: str
    kind: str
    create_time: str | None        # ISO-8601 string from server, unparsed
    version: int | None
    source: dict[str, Any] = field(default_factory=dict)  # raw `source` block from hit


@dataclass(frozen=True, slots=True)
class SearchPageResult:
    kind: str                      # echo of request kind
    query: str | None              # echo of request query
    offset: int                    # echo of request offset
    limit: int                     # echo of request limit
    records: list[RecordSummary] = field(default_factory=list)
    total_count: int | None = None # server `totalCount` if present
    ok: bool = False
    http_status: int | None = None
    latency_ms: float = 0.0
    correlation_id: str | None = None
    error_message: str | None = None
    raw_response: dict | str | None = None


@dataclass(frozen=True, slots=True)
class KindAggregationResult:
    kinds: list[str] = field(default_factory=list)
    source: str = "aggregation"    # "aggregation" | "page_sample"
    ok: bool = False
    http_status: int | None = None
    latency_ms: float = 0.0
    correlation_id: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class RecordDetailResult:
    record_id: str
    record: dict | None = None
    ok: bool = False
    http_status: int | None = None
    latency_ms: float = 0.0
    correlation_id: str | None = None
    error_message: str | None = None
    raw_response: dict | str | None = None
```

### 2.c Failure semantics (mirrors legal_tags)
- Transport failure (timeout, connection error, DNS): `ok=False`,
  `http_status=None`, `error_message` is human-readable.
- 2xx: `ok=True`.
- 3xx/4xx/5xx: `ok=False`, `http_status` populated, `error_message`
  carries the truncated server body (≤500 chars).
- `raw_response` always populated when the server returned a body
  (parsed dict if JSON, raw string otherwise).

---

## 3. Page layout — `app/pages/5_🔍_Search.py`

Sections, top-to-bottom. Same scaffolding helpers as page 4 (Ingestion).

1. **Pre-flight chain** (reuse `_require_ready_connection` pattern from
   page 4 — connection persisted, token present, partition non-empty).
   Stop on first failure with a friendly message and a link hint to
   pages 1/2.
2. **Sticky error banner** — `st.error(st.session_state.search_last_error)`
   if set; cleared at the top of every successful operation.
3. **Toolbar row** (single `st.columns([1, 2, 4, 1])`):
   - **Refresh** button (re-runs the current `kind` + `query` at current offset)
   - **Kind dropdown** — options = `search_kind_options`; defaults to
     `"*:*:*:*"`. Populated by `list_kinds` on the autorun-once tick.
   - **Free-text query** — `st.text_input` bound to `search_query_text`;
     placeholder `'e.g. data.WellName:"15/9-13" OR id:"opendes:*"'`.
   - **Search** button (resets offset to 0, runs).
4. **Pagination row** — `Prev` / `Next` buttons + caption
   `"Showing 1–100 of 12,481"`. `Next` disabled when
   `offset + len(results) >= total_count` (or when `total_count is None`
   and `len(results) < limit`). `Prev` disabled when `offset == 0`.
5. **Results dataframe** — columns: `id`, `kind`, `createTime`,
   `source_preview` (first `SEARCH_DATA_PREVIEW_CHARS` chars of
   `json.dumps(source)`). Selection mode: `single-row` via
   `st.dataframe(..., on_select="rerun", selection_mode="single-row")`;
   selected row id → `search_selected_record_id`.
6. **Selected-record detail panel** (rendered iff
   `search_selected_record_id`):
   - `st.expander("Search hit JSON (from results)")` — shows the
     `RecordSummary.source` blob for that id, pretty-printed.
   - `📥 Fetch full record` button — calls `get_record`, stores in
     `search_full_record_cache[id]`, shows in a second expander
     `st.expander("Full record (from Storage)")`.
   - Cache check first: if id already in `search_full_record_cache`,
     button label becomes `🔄 Refresh full record`.
7. **History dataframe + latency chart** — same shape as page 4 (most
   recent 50 rows; line chart of `latency_ms` keyed by timestamp).

### Autorun-once
On page load, if `not search_autorun_done`:
1. `with st.spinner("Loading available kinds…"): list_kinds(...)`
2. `with st.spinner("Browsing recent records…"): search_records(kind="*:*:*:*", offset=0)`
3. Set `search_autorun_done = True` regardless of outcome.

---

## 4. Edge cases (Kevin in services, Judson on the page, Charlie in tests)

| Case | Behavior |
|---|---|
| Empty result set (`results=[]`, `totalCount=0`) | Show `st.info("No records match this query.")`; pagination disabled both ways. |
| Kind dropdown empty (aggregation + sample both yielded zero) | Dropdown shows only `"*:*:*:*"`; caption `"Kind list unavailable — use free-text query."`. No crash. |
| Query syntax error (400 from server) | Sticky-error banner with the truncated server body; results table preserves the previous page (do not clear `search_results`). |
| `get_record` 404 | `st.warning("Record {id} not found — it may have been deleted or the id is wrong.")`. Cache **not** populated. Selection remains so the user can click again. |
| `offset + limit > total_count` after a Next click | Service still called (server returns empty page); Next button disabled on return. No crash. |
| Large `data` block in record | Dataframe preview already capped at `SEARCH_DATA_PREVIEW_CHARS`. Inline expander and full-record expander show the full JSON via `st.json` (no truncation). |
| Token expired mid-session | `_call_search` reports 401; sticky error banner with friendly text + hint to revisit page 1. Same UX as page 4. |
| Transport failure during `list_kinds` | Dropdown falls back to `["*:*:*:*"]` only; no banner (autorun is silent on aggregation failure). |
| Transport failure during `search_records` | Sticky-error banner; `search_results` cleared; history row recorded with `ok=False`. |

---

## 5. Non-goals for v1 (out of scope — do not implement)

- Saved searches / named-query persistence
- Export results (CSV / JSON download)
- Multi-kind filter (single kind or wildcard only)
- Field-builder UI / query DSL helper
- Record create / edit / delete (page is read-only)
- Geo-spatial / GIS search (no `spatialFilter` in request body)
- Highlighting / facets beyond the kind aggregation
- Cursor / scroll-based pagination (offset-only for v1)
- Cross-partition search

---

## 6. Hand-off

- **Kevin:** owns `app/services/search.py` + the four new dataclasses
  in `app/models/osdu.py` (and deletes the orphan `SearchResult`).
  Reconcile with Darryl's `darryl-search-api.md` when it lands — any
  changes stay inside the service module unless they touch the
  signatures/dataclasses above (in which case ping me).
- **Judson:** owns `app/pages/5_🔍_Search.py`. Reuse the page-4
  scaffolding helpers verbatim where they exist; copy-and-adapt the
  rest. Do **not** invent new session-state keys outside §1 without
  amending this contract.
- **Charlie:** test files `tests/test_search_service.py` and
  `tests/test_search_page.py`. Page test asserts every §1 key; service
  test covers all §4 transport/HTTP cases via `responses`/`requests-mock`.


### 2026-05-15T15:57Z: Search page test labels — UI is source of truth

**By:** Charlie (requested by Mariel)

**What:** Updated `tests/test_search_page.py` constants to match the shipped UI labels in `app/pages/7_🔍_Search.py` (Wave 3, issues #26/#27). UI was NOT changed.

**Label corrections (test → impl):**
- `KIND_SELECT_LABEL` / `MULTI_KIND_LABEL`: `"Kind"` → `"Kind filter"` (page renders `st.multiselect("Kind filter", ...)`)
- `AGGREGATE_TOGGLE_LABEL`: `"Show record counts by kind"` → `"Aggregate by kind"` (page renders `st.checkbox("Aggregate by kind", ...)`)
- `ADD_CLAUSE_LABEL`: `"➕"` → `"Add clause"` (page renders `st.button("Add clause", ...)`)
- `COMBINATOR_LABEL`: `"Combine clauses with"` → `"Combinator"` (page renders `st.radio("Combinator", ...)`)

**Also:** `test_add_clause_button_adds_row` previously asserted `len(clauses) >= 2` after a single click. The page's handler appends exactly one clause per click and `st.rerun()` is a no-op under the recorder, so the realistic expectation is `>= 1`. Adjusted the assertion + added a comment explaining the no-rerun semantics.

**Naming convention going forward:** When a page widget defines a `key=...` AND the recorder needs to drive its value, the test must set `widget_values[<exact label string>]` — the recorder's `multiselect`/`checkbox`/`radio` look up by label first and do NOT consult `session_state[key]`. Test-side label constants should be defined ONCE at module top and reference the exact verbatim page label (no paraphrasing).

**Result:** 60/60 in `test_search_page.py`. No impl changes.



### 2026-05-15T10:30:00Z: Storage engine preference
**By:** Mariel (via Copilot)
**What:** Local persistence will likely use PostgreSQL (Eirik leaning that direction). Current SQLite-via-DATABASE_URL fallback remains. Final call is Eirik's.
**Why:** User directive — captured for team memory



### 2026-05-18T20:00:00Z: darryl-osdu-mapping-conventions (from inbox)
# OSDU CSV → Schema Mapping Conventions for Heuristic Mapper

**Author:** Darryl (OSDU/ADME Domain Expert)  
**Date:** 2026-05-13  
**Status:** PROPOSED  
**For:** Kevin (heuristic mapper implementation)

---

## 1. How the TNO CSV-to-JSON Pipeline Works

The upstream `osdu-data-load-tno` project uses a **template-driven** approach:

1. A **JSON template** contains `{{PARAMETER_NAME}}` placeholders at OSDU schema field positions.
2. A **CSV file** has column headers that match those parameter names (case-insensitive).
3. `csv_to_json.py` reads the template, scans for `{{...}}` tokens, then maps each CSV column header (lowercased) to the corresponding token.
4. For **array fields** (e.g., `NameAliases`, `FacilityStates`), the CSV uses a **numbered suffix convention**: `aliasname_1`, `aliasname_2`, `aliasnametypeid_1`, `aliasnametypeid_2`, etc.

**Key insight for Kevin:** CSV headers are NOT exact OSDU property names. They are **lowercased versions of the template parameter names**, which in turn correspond to the OSDU `data.*` field paths. The mapper should normalize both sides to lowercase for matching.

---

## 2. TNO CSV Column Structures

### 2.1 Organisation CSV

Source: `TNO/contrib/master-data/Misc_master_data/` directory.

| CSV Column | OSDU Schema Path | Type | Notes |
|---|---|---|---|
| `organisationname` | `data.OrganisationName` | string | Primary name |
| `organisationid` | `data.OrganisationID` | string | External identifier |
| `id` | `id` | string | Full OSDU ID: `{{DATA_PARTITION_ID}}:master-data--Organisation:{{id_value}}` |
| `aliasname_1..N` | `data.NameAliases[N-1].AliasName` | string | Array field |
| `aliasnametypeid_1..N` | `data.NameAliases[N-1].AliasNameTypeID` | ref-data ID | Array field |

### 2.2 Well CSV

Source: `TNO/contrib/master-data/Well/` directory.

| CSV Column | OSDU Schema Path | Type | Notes |
|---|---|---|---|
| `facilityname` | `data.FacilityName` | string | Well name (e.g., "BUR-GT-01") |
| `facilityid` | `data.FacilityID` | string | External facility ID |
| `id` | `id` | string | Becomes `{{DATA_PARTITION_ID}}:master-data--Well:{{id_value}}` |
| `currentoperatorid` | `data.CurrentOperatorID` | relationship | → `Organisation` record |
| `initialoperatorid` | `data.InitialOperatorID` | relationship | → `Organisation` record |
| `datasourceorganisationid` | `data.DataSourceOrganisationID` | relationship | → `Organisation` record |
| `operatingenvironmentid` | `data.OperatingEnvironmentID` | ref-data ID | e.g., Onshore/Offshore |
| `facilitytypeid` | `data.FacilityTypeID` | ref-data ID | |
| `source` | `data.Source` | string | e.g., "TNO" |
| `existencekind` | `data.ExistenceKind` | ref-data ID | |
| `aliasname_1..N` | `data.NameAliases[N-1].AliasName` | string | Array |
| `aliasnametypeid_1..N` | `data.NameAliases[N-1].AliasNameTypeID` | ref-data ID | Array |
| `definitionorganisationid_1..N` | `data.NameAliases[N-1].DefinitionOrganisationID` | relationship | Array |
| `facilitystatetypeid_1..N` | `data.FacilityStates[N-1].FacilityStateTypeID` | ref-data ID | Array |
| `facilitystate_effectivedatetime_1..N` | `data.FacilityStates[N-1].EffectiveDateTime` | datetime | Array |
| `facilitystate_terminationdatetime_1..N` | `data.FacilityStates[N-1].TerminationDateTime` | datetime | Array |
| `latitude` | `data.SpatialLocation.Wgs84Coordinates...` | float | Via meta/FoR conversion |
| `longitude` | `data.SpatialLocation.Wgs84Coordinates...` | float | Via meta/FoR conversion |
| `geopoliticalentityid_1..N` | `data.GeoContexts[N-1].GeoPoliticalEntityID` | ref-data ID | Array |

### 2.3 Wellbore CSV

Source: `TNO/contrib/master-data/Wellbore/` directory.

| CSV Column | OSDU Schema Path | Type | Notes |
|---|---|---|---|
| `facilityname` | `data.FacilityName` | string | Wellbore name |
| `facilityid` | `data.FacilityID` | string | External facility ID |
| `id` | `id` | string | Becomes `{{DATA_PARTITION_ID}}:master-data--Wellbore:{{id_value}}` |
| `wellid` | `data.WellID` | relationship | → `Well` record. Full ID pattern. |
| `sequencenumber` | `data.SequenceNumber` | integer | Wrapped as `int({{sequencenumber}})` |
| `currentoperatorid` | `data.CurrentOperatorID` | relationship | → `Organisation` |
| `datasourceorganisationid` | `data.DataSourceOrganisationID` | relationship | → `Organisation` |
| `source` | `data.Source` | string | |
| `existencekind` | `data.ExistenceKind` | ref-data ID | |
| `trajectorytypeid` | `data.TrajectoryTypeID` | ref-data ID | e.g., Vertical/Directional |
| `aliasname_1..N` | `data.NameAliases[N-1].AliasName` | string | Array |
| `aliasnametypeid_1..N` | `data.NameAliases[N-1].AliasNameTypeID` | ref-data ID | Array |
| `facilitystatetypeid_1..N` | `data.FacilityStates[N-1].FacilityStateTypeID` | ref-data ID | Array |
| `facilitystate_effectivedatetime_1..N` | `data.FacilityStates[N-1].EffectiveDateTime` | datetime | Array |

---

## 3. OSDU ID/SRN Construction Rules

### 3.1 Record ID Pattern

```
{DATA_PARTITION_ID}:{group}--{EntityType}:{unique_key}
```

Examples:
- `opendes:master-data--Well:BUR-GT-01`
- `opendes:master-data--Wellbore:BUR-GT-01-S1`
- `opendes:master-data--Organisation:TNO`

### 3.2 Reference-Data ID Pattern

```
{DATA_PARTITION_ID}:reference-data--{EntityType}:{value}:
```

Note the **trailing colon** — this is the version-less form. The schema `pattern` field shows `:[0-9]*$`, meaning version is optional (empty = latest).

### 3.3 Relationship References

When one record references another (e.g., `Wellbore.WellID` → `Well`):
- The value must be the **full OSDU ID** of the target record, including partition prefix.
- The schema's `x-osdu-relationship` annotation tells you which `GroupType` and `EntityType` the target is.
- Example: `data.WellID` must be `opendes:master-data--Well:BUR-GT-01:` (with trailing colon for versionless).

**Heuristic rule:** Any field whose schema has `x-osdu-relationship` is a foreign key. The mapper should detect this and construct the full ID from the CSV value.

---

## 4. Field Type Conventions

### 4.1 Type Wrappers in Templates

The `csv_to_json.py` engine supports inline type coercion in the template:

| Template Syntax | CSV Value | Output JSON Value |
|---|---|---|
| `"{{field}}"` | `"hello"` | `"hello"` (string, default) |
| `"int({{field}})"` | `"42"` | `42` (integer) |
| `"float({{field}})"` | `"52.1234"` | `52.1234` (float) |
| `"bool({{field}})"` | `"true"` | `true` (boolean) |
| `"datetime_YYYY-MM-DD({{field}})"` | `"2023-06-15"` | `"2023-06-15T00:00:00Z"` |
| `"datetime_MM/DD/YYYY({{field}})"` | `"06/15/2023"` | ISO datetime string |

**Heuristic rule:** The mapper should check the schema `type` and `format` fields:
- `"type": "integer"` → wrap with `int()`
- `"type": "number"` or `"format": "float"` → wrap with `float()`
- `"type": "boolean"` → wrap with `bool()`
- `"format": "date-time"` → wrap with `datetime_YYYY-MM-DD()`

### 4.2 String Fields

All fields default to string. No wrapper needed.

### 4.3 System-Managed Fields (NEVER map from CSV)

These are set by the OSDU platform and must not appear in the manifest:

- `version`, `createTime`, `createUser`, `modifyTime`, `modifyUser`
- `tags` (optional, rarely used in TNO)

### 4.4 Envelope Fields (Set by the tool, not CSV)

These are set by the ingestion tool, not per-row CSV data:

- `kind` — from schema version (e.g., `osdu:wks:master-data--Well:1.0.0`)
- `acl.owners`, `acl.viewers` — from tool config
- `legal.legaltags`, `legal.otherRelevantDataCountries` — from tool config

---

## 5. Array Field Conventions

### 5.1 Numbered Suffix Pattern

For any OSDU field that is `"type": "array"`, the CSV uses numbered columns:

```
{fieldname}_1, {fieldname}_2, {fieldname}_3, ...
```

For nested array objects (e.g., `NameAliases[].AliasName`), each property of the array item gets its own set of numbered columns:

```
aliasname_1, aliasname_2, aliasname_3
aliasnametypeid_1, aliasnametypeid_2, aliasnametypeid_3
```

Row `N` columns across all properties of the same array form one array element:
- `aliasname_1` + `aliasnametypeid_1` → `NameAliases[0]`
- `aliasname_2` + `aliasnametypeid_2` → `NameAliases[1]`

### 5.2 Sparse Arrays

If `aliasname_2` is empty for a given row, the engine omits `NameAliases[1]` entirely (the `clear_non_filled_parameters` pass removes unfilled placeholders).

### 5.3 Detection Heuristic

**For Kevin:** To detect array columns in an input CSV, look for column headers matching `^(.+)_([1-9]\d*)$`. Group by the base name. If you find `foo_1` and `foo_2`, then `foo` is an array field. Cross-reference with the OSDU schema to find which array property it maps to.

---

## 6. Schema Inheritance Hierarchy

Every master-data record's `data` block is composed via `allOf` from these abstract schemas:

```
data:
  allOf:
    - AbstractCommonResources.1.0.0   → Source, ExistenceKind, ResourceSecurityClassification, ...
    - AbstractMaster.1.0.0            → NameAliases[], GeoContexts[], SpatialLocation, ...
    - AbstractFacility.1.0.0          → FacilityID, FacilityName, FacilityOperators[], 
                                        FacilityStates[], CurrentOperatorID, InitialOperatorID, ...
    - IndividualProperties             → Entity-specific fields (WellID, SequenceNumber, etc.)
    - ExtensionProperties              → Free-form extension object
```

**Heuristic rule:** When resolving a CSV column to a schema field, the mapper must walk ALL `allOf` branches. A field like `FacilityName` won't appear in `Well.1.0.0.json` directly — it's in `AbstractFacility.1.0.0.json`.

---

## 7. Required vs. Optional Fields

### 7.1 Schema-Level Required

Per the OSDU schema `required` array, only these top-level envelope fields are strictly required:
- `kind`
- `acl`
- `legal`

All `data.*` fields are technically optional in the schema. However, the **ingestion DAG will reject** records missing certain practical fields.

### 7.2 Practically Required for Ingestion

| Entity | Practically Required Fields |
|---|---|
| Organisation | `id`, `data.OrganisationName` |
| Well | `id`, `data.FacilityName`, `data.Source` |
| Wellbore | `id`, `data.FacilityName`, `data.WellID`, `data.Source` |

### 7.3 Required Template Mechanism

The TNO pipeline uses a `required_template` JSON that gets merged into every generated manifest after parameter substitution. This ensures common boilerplate (like `kind`, `legal`, `acl`) is present. The mapper should support this pattern.

---

## 8. Coordinate Handling

### 8.1 SpatialLocation

Wells and Wellbores have `data.SpatialLocation` (from `AbstractMaster`). This is an `AbstractSpatialLocation` object containing:
- `Wgs84Coordinates` — GeoJSON FeatureCollection in EPSG:4326
- `AsIngestedCoordinates` — original CRS coordinates

### 8.2 CSV Convention for Coordinates

The TNO CSVs provide `latitude` and `longitude` as simple float columns. The template constructs the GeoJSON FeatureCollection structure, placing `{{longitude}}` and `{{latitude}}` at the coordinate positions.

**Heuristic rule:** If the mapper sees columns named `latitude`/`longitude` (or `lat`/`lon`/`x`/`y`), map them into the `SpatialLocation.Wgs84Coordinates.features[0].geometry.coordinates` array as `[longitude, latitude]` (GeoJSON order is lon, lat).

---

## 9. Namespace Substitution

All OSDU IDs in the TNO templates use `<namespace>:` as a placeholder. At generation time, `--schema-ns-value` replaces this with the actual data partition ID (e.g., `opendes`).

The mapper should support a `DATA_PARTITION_ID` substitution token in all ID fields.

---

## 10. Loading Order (Dependency Chain)

```
1. Reference Data    (no dependencies)
2. Misc Master Data  (may reference ref-data)
3. Organisation      (may reference ref-data)
4. Well              (references Organisation, ref-data)
5. Wellbore          (references Well, Organisation, ref-data)
```

The heuristic mapper should validate that referenced entities exist before generating a manifest for dependent entities.

---

## 11. Sample CSV Files

Test CSVs are provided at:
- `app/data/datasets/tno/csv/organisations.csv`
- `app/data/datasets/tno/csv/wells.csv`
- `app/data/datasets/tno/csv/wellbores.csv`

These contain realistic TNO Netherlands data with both simple and array fields for testing the generator.



### 2026-05-18T20:00:00Z: darryl-tno-master-data-plan (from inbox)
# TNO Master-Data Vendoring Plan

**Author:** Darryl (OSDU / ADME Ingestion Domain Expert)
**Date:** 2026-05-13
**Status:** PROPOSAL — awaiting team review

---

## 1. Goal

Enable TNO master-data (Organisation, Well, Wellbore) loading via the
Bulk Load page. This is tier 2 of the three-tier load sequence:

```
reference-data  →  master-data  →  work-products
    (done)          (this plan)      (future)
```

---

## 2. Source Data — What We Need and Where It Lives

### 2.1 Upstream repo

All TNO source data lives in the OSDU open-test-data repo:
<https://community.opengroup.org/osdu/data/open-test-data>

Under `rc--3.0.0/` the upstream provides **pre-built load manifests** for
master-data in a directory called `1-data/3-provided/` (or the
equivalent release path). The entities present in the TNO dataset are:

| Entity | OSDU Kind | Upstream source |
|---|---|---|
| Organisation | `osdu:wks:master-data--Organisation:1.0.0` | `load_Organisation.json` or CSV + template |
| Well | `osdu:wks:master-data--Well:1.0.0` | `load_Well.json` or CSV + template |
| Wellbore | `osdu:wks:master-data--Wellbore:1.0.0` | `load_Wellbore.json` or CSV + template |
| Field | `osdu:wks:master-data--Field:1.0.0` | `load_Field.json` (if present) |
| GeoPoliticalEntity | `osdu:wks:master-data--GeoPoliticalEntity:1.0.0` | `load_GeoPoliticalEntity.json` (if present) |

**Two procurement paths:**

1. **Pre-built manifests (preferred for v2).** The upstream repo ships
   ready-to-load `load_*.json` files — same envelope shape we already use
   for reference-data. Vendor those directly under
   `app/data/osdu/rc--3.0.0/master-data/`.

2. **CSV + template (the `csv_to_json.py` path).** The upstream repo
   _also_ ships CSV files (e.g. `Wells.csv`, `Wellbores.csv`) plus JSON
   templates that `csv_to_json.py` consumes. The C# rewrite on `main`
   generates manifests this way programmatically. We would run the
   generation _offline_ and commit the output.

**Recommendation:** Use path 1 — pre-built manifests. Same pattern as
reference-data, no runtime dependency on `csv_to_json.py`, no CSV files
to ship. If pre-built manifests aren't available for a given entity, fall
back to path 2 offline.

### 2.2 What to vendor

From the upstream `v0.27.0` tag (same as our reference-data pin),
extract the master-data manifests and place them at:

```
app/data/osdu/rc--3.0.0/master-data/
    load_Organisation.json
    load_Well.json
    load_Wellbore.json
    (load_Field.json — if present)
    (load_GeoPoliticalEntity.json — if present)
```

Minimum viable set: **Organisation, Well, Wellbore.** Field and
GeoPoliticalEntity are nice-to-have — both are used by TNO Well records
but the ingestion DAG resolves missing references gracefully (they
become dangling SRNs, not failures).

---

## 3. Manifest Shape — OSDU Spec

### 3.1 Envelope

Per `Manifest.1.0.0.json`, the load manifest is:

```json
{
  "kind": "osdu:wks:Manifest:1.0.0",
  "MasterData": [
    { ... record 1 ... },
    { ... record 2 ... }
  ]
}
```

Key difference from reference-data: the array is `"MasterData"` not
`"ReferenceData"`. The bulk_loader code already has this mapping in
`_TIER_TO_SECTION`:

```python
_TIER_TO_SECTION = {
    "reference-data": "ReferenceData",
    "master-data": "MasterData",
    "work-products": "Data",
}
```

So the section lookup works with **zero code changes** to `bulk_loader.py`.

### 3.2 Record shape — Well

```json
{
  "id": "osdu:master-data--Well:<well-id>",
  "kind": "osdu:wks:master-data--Well:1.0.0",
  "acl": { "owners": [], "viewers": [] },
  "legal": { "legaltags": [], "otherRelevantDataCountries": [] },
  "data": {
    "FacilityName": "...",
    "FacilityOperator": "osdu:master-data--Organisation:<org-id>:",
    "DataSourceOrganisationID": "osdu:master-data--Organisation:<org-id>:",
    "SpatialLocation": { ... },
    "VerticalMeasurements": [ ... ],
    "DefaultVerticalMeasurementID": "...",
    "FacilityStates": [ ... ],
    "NameAliases": [ ... ],
    "GeoContexts": [ ... ]
  }
}
```

**Required top-level fields** (from schema): `kind`, `acl`, `legal`.
**Key data fields:**
- `FacilityName` — well name string
- `FacilityOperator` — SRN reference to Organisation
- `SpatialLocation` — lat/lon (AbstractSpatialLocation)
- `VerticalMeasurements` — array of datum entries
- `NameAliases` — alternative names (references `reference-data--AliasNameType`)
- `FacilityStates` — lifecycle (references `reference-data--FacilityStateType`)

**ID pattern:** `^[\w\-\.]+:master-data\-\-Well:[\w\-\.\:\%]+$`

### 3.3 Record shape — Wellbore

```json
{
  "id": "osdu:master-data--Wellbore:<wellbore-id>",
  "kind": "osdu:wks:master-data--Wellbore:1.0.0",
  "acl": { "owners": [], "viewers": [] },
  "legal": { "legaltags": [], "otherRelevantDataCountries": [] },
  "data": {
    "FacilityName": "...",
    "WellID": "osdu:master-data--Well:<parent-well-id>:",
    "SequenceNumber": 1,
    "TrajectoryTypeID": "osdu:reference-data--WellboreTrajectoryType:<type>:",
    "VerticalMeasurements": [ ... ],
    "DrillingReasons": [ ... ],
    "NameAliases": [ ... ],
    "FacilityStates": [ ... ]
  }
}
```

**Critical dependency:** `WellID` is a foreign-key reference to a Well
record. The Well MUST be ingested before the Wellbore, or the DAG will
resolve a dangling SRN. Within a single manifest, array ordering is
honored — Wells before Wellbores in the `MasterData` array. Across
manifests, submit Well manifests first.

### 3.4 Record shape — Organisation

```json
{
  "id": "osdu:master-data--Organisation:<org-id>",
  "kind": "osdu:wks:master-data--Organisation:1.0.0",
  "acl": { "owners": [], "viewers": [] },
  "legal": { "legaltags": [], "otherRelevantDataCountries": [] },
  "data": {
    "OrganisationName": "...",
    "OrganisationID": "...",
    "OrganisationDescription": "..."
  }
}
```

No foreign-key dependencies on other master-data. Can be loaded first or
in the same manifest as Wells (placed before Wells in the array).

---

## 4. Load Ordering

Per the OSDU Manifest schema spec (`Manifest.1.0.0.json`):

> The load sequence follows a well-defined sequence. The 'ReferenceData'
> array is processed first. The 'MasterData' array is processed second.
> Inside arrays, dependent items must be placed behind their relationship
> targets.

**Cross-tier ordering (handled by the Bulk Load page UI):**

```
1. reference-data  — lookup tables (AliasNameType, FacilityStateType, etc.)
2. master-data     — entities (Organisation → Well → Wellbore)
3. work-products   — data objects (WellLog, Trajectory, etc.)
```

**Intra-tier ordering (handled by manifest array ordering):**

Within a single master-data manifest, records must appear in dependency
order:
1. Organisation (no dependencies)
2. Well (references Organisation)
3. Wellbore (references Well)

**Manifest file ordering** (glob sort in `bulk_loader.py`):
Name the files so alphabetical sort gives correct order:

```
load_01_Organisation.json
load_02_Well.json
load_03_Wellbore.json
```

Or use a single combined manifest with all records in the `MasterData`
array in the correct order — this is what the upstream TNO loader does.

**Recommendation:** Ship as a single `load_MasterData.TNO.json` if the
upstream manifest is combined, or split by entity type with numeric
prefixes. Either works; the combined manifest is simpler for the operator
(one workflow run) but larger.

---

## 5. How `csv_to_json.py` Fits (or Doesn't)

### What it does

`csv_to_json.py` is a CSV-to-manifest generator from the Azure
`osdu-data-load-tno` project. It:
1. Reads a JSON template with `{{parameter}}` placeholders
2. Reads a CSV where column headers map to parameter names
3. Generates one manifest record per CSV row
4. Wraps records in the `Manifest:1.0.0` envelope
5. Optionally validates against OSDU JSON schemas
6. Injects ACL/legal at generation time

`csv_to_json_wrapper.py` is a CLI wrapper around the same function.

### Assessment for this plan

**We do NOT need `csv_to_json.py` at runtime.** The pre-built manifests
from upstream already contain the records we need. The vendored code is
useful as:
- **Offline tooling** if we ever need to regenerate manifests from
  updated CSVs (e.g. new TNO release adds wells).
- **Documentation** of the template → manifest mapping for anyone
  writing custom datasets.

The runtime load path in `bulk_loader.py` reads `.json` manifests
directly, injects ACL/legal programmatically via
`_inject_acl_and_legal()`, and submits to the workflow endpoint. No
CSV-to-JSON step needed.

**If** the upstream repo only ships CSVs (not pre-built `.json`) for
master-data, then we run `csv_to_json.py` locally once, commit the
output, and ship the static manifests. The generation is a build-time
step, not a runtime step.

---

## 6. Changes to `dataset.json`

### TNO (`app/data/datasets/tno/dataset.json`)

```diff
  "master-data": {
-   "enabled": false,
-   "reason": "v2 — not yet vendored"
+   "enabled": true,
+   "manifest_glob": "../../osdu/rc--3.0.0/master-data/load_*.json",
+   "description": "TNO master-data: Organisation, Well, Wellbore."
  }
```

The `manifest_glob` uses the same `../../osdu/` relative path pattern
as reference-data. Path resolution + sandbox check in `bulk_loader.py`
already handles this correctly.

### Volve (`app/data/datasets/volve/dataset.json`) — no changes yet

Volve master-data is a separate task. See §7.

---

## 7. Volve Equivalent

Volve follows the same pattern but with different source data:

| Aspect | TNO | Volve |
|---|---|---|
| Source repo | same upstream, `rc--3.0.0` | same upstream, Volve subdirectory |
| Wells | ~450 (Netherlands) | 1 (Volve field, North Sea) |
| Wellbores | ~800 | ~20 |
| Organisations | TNO, EBN, NAM, etc. | Equinor |
| Reference-data | Shared OSDU set (already loaded) | Same shared set |
| Work-products | Trajectories, logs, reports | Trajectories, logs, seismic |

Volve master-data manifests would go in a separate path:
```
app/data/datasets/volve/master-data/
    load_MasterData.Volve.json
```

Or, if Volve reuses the same OSDU-generic master-data path (unlikely
since Wells are dataset-specific), under its own `osdu/` subfolder.
The `dataset.json` glob would point at the Volve-specific location.

**Key difference:** Volve is a _much_ smaller dataset. A single combined
manifest with ~25 records is fine. TNO may need splitting if the combined
manifest exceeds ~5 MB (workflow service payload limit).

---

## 8. Effort Estimates

| Task | Scope | Size |
|---|---|---|
| **Tier 2a: Vendor TNO master-data manifests** | Extract from upstream, place in `rc--3.0.0/master-data/`, update `dataset.json` | Small — file copy + JSON edit |
| **Tier 2b: Validate master-data load** | Submit via Bulk Load page, verify via Search page, fix ordering issues | Medium — depends on ADME instance availability |
| **Tier 2c: Update README/docs** | Document master-data in `rc--3.0.0/README.md`, `tno/README.md` | Small |
| **Tier 3: TNO work-products** | Vendor WPC manifests + file assets, add `work-products` tier | Large — file assets need storage story |
| **Tier 4: Volve reference + master** | Vendor Volve ref-data + master-data, add dataset entry | Medium — smaller data, same pattern |
| **Tier 5: Volve work-products** | Same as tier 3 but for Volve | Large |

**Critical path for Tier 2:** The only blocker is getting the actual
upstream manifest files. If the `v0.27.0` tag has them pre-built, it's
a ~30 minute task to vendor + wire up. If we need to generate from CSV,
add time for template + CSV extraction and a local `csv_to_json.py` run.

---

## 9. Open Questions

1. **Does `v0.27.0` ship pre-built master-data load manifests?** The
   `1-data/3-provided/` directory structure varies by release. Need to
   check the actual upstream tree.

2. **Manifest size limits.** TNO has ~450 wells and ~800 wellbores. If
   the combined manifest exceeds the ADME workflow payload limit (~5 MB),
   we need to split into multiple manifests (Wells-1.json, Wells-2.json,
   etc.). The `bulk_loader.py` glob handles this natively.

3. **`id` prefix.** The reference-data manifests hard-code `"osdu:"` as
   the id authority prefix. Master-data may do the same or may use
   `"<namespace>:"`. If the latter, we need the existing
   `_inject_acl_and_legal` to also handle id prefix substitution, or do
   it at vendor time.

4. **Organisation completeness.** TNO Well records reference operators
   (NAM, Shell, etc.) by Organisation SRN. If those Organisation records
   aren't in the vendored manifest, the references become dangling SRNs.
   The ingestion DAG won't _fail_ — it'll just store the SRN as
   unresolved. Decide: vendor all referenced orgs, or accept dangling?

5. **Field entity.** Some TNO wells reference `master-data--Field`
   records. Same question as #4 — vendor or accept dangling?

---

## 10. Recommended Next Steps

1. **Darryl:** Clone/browse upstream `v0.27.0` tree, confirm which
   master-data manifests are available pre-built vs CSV-only.
2. **Darryl:** Vendor the manifests into `rc--3.0.0/master-data/`.
3. **Satya/Judson:** Update `dataset.json`, run Bulk Load smoke test.
4. **Kevin:** Verify ingested records appear in Search page.
5. **Charlie:** Add test coverage for master-data tier in bulk loader
   tests.



### 2026-05-15T10:52:00Z: CSV generation tab + mid-loop abort
**By:** Judson (via Mariel)

**What:**
1. Completed the "Generate from CSV" tab in the Bulk Load page (issue #17): added
   JSON sample manifest preview before submit. The rest of the tab (kind picker,
   CSV upload, auto-map display, editable mapping overrides, legal/ACL form,
   generate button, submit loop, results section) was already implemented.

2. Added graceful mid-loop abort button to both submit loops (issue #31):
   - Registered datasets: `BULK_ABORT_KEY = "bulk_abort_requested"`
   - CSV generation: `GEN_ABORT_KEY = "gen_abort_requested"`
   - Each loop renders `st.button("⏹️ Abort")` with `on_click` callback
   - Loop checks flag after each iteration; finishes current HTTP call, skips rest
   - Results sections detect partial abort and show "Aborted after N of M"

**Why:**
- Operators need to see what they're submitting before committing (preview gate).
- Long submit runs (50+ manifests) need a graceful escape hatch — previously the
  only option was closing the browser tab.

**Files changed:**
- `app/pages/9_📥_Bulk_Load.py` — main implementation
- `tests/support/streamlit_recorder.py` — selectbox now honours session_state[key]
- `tests/test_bulk_load_page.py` — fixed `fake_list_schema_kinds` falsy-check bug

**Validation:** 43/43 bulk load page tests pass.



### 2026-05-18T20:00:00Z: kevin-cursor-search (from inbox)
# Cursor-based search and export_all_records

**Date:** 2026-05-15T11:00:00Z
**By:** Kevin

## Decision

`search_with_cursor()` hits `/api/search/v2/query_with_cursor` (cursor-based pagination) and returns a `CursorSearchResult`. `export_all_records()` is a generator that yields pages until the cursor is exhausted or an error occurs. Both live in `app/services/search.py`.

## Key choices

- **No offset in cursor body.** OSDU cursor search rejects `offset`; the cursor token itself encodes position. First call omits `cursor` entirely; subsequent calls include it.
- **`has_more = bool(cursor)`.** Empty or null cursor from the response means no more pages — this is the documented OSDU contract.
- **`_call_search` timeout parameter.** Added an optional `timeout` kwarg (defaults to `SEARCH_TIMEOUT_SECONDS = 15`). Cursor/export calls pass `EXPORT_TIMEOUT_SECONDS = 30` because full-export pages can be slower than interactive search.
- **`returned_fields` override.** Callers can pass wider field projections (e.g. `("id", "kind", "data.*")`) for export use cases. Default falls back to `_DEFAULT_RETURNED_FIELDS`.
- **Generator stops on error.** `export_all_records` yields the error page so the UI can display it, then stops iteration. No hidden retries.
- **Existing `search_records()` unchanged.** It continues to use offset+limit pagination for the interactive Search page.

## Evidence

- 112 tests passing (80 existing + 32 new) in `tests/test_search_service.py`.



### 2026-05-18T20:00:00Z: kevin-legaltag-validate (from inbox)
# Legal-tag pre-flight switched to POST validate

**Date:** 2026-05-13
**By:** Kevin
**Scope:** `app/services/ingestion.py` → `check_legal_tag`

## Decision

The ingestion pre-flight legal-tag check now uses
`POST /api/legal/v1/legaltags:validate` instead of
`GET /api/legal/v1/legaltags/{name}`.

## Why

Darryl flagged that the validate endpoint confirms a tag is *valid*
(not just present), and returns the same shape the workflow service
checks internally. This eliminates a class of false-positive pre-flight
results where a tag exists but is invalid (e.g. expired, non-compliant).

## What changed

- `check_legal_tag` sends `{"names": [tag_name]}` via POST.
- Response parsing checks `invalidLegalTags` list; tag is valid when absent.
- `_call_legal` helper now accepts optional `method`/`json_body` kwargs.
- 10 tests updated, 189 total green.



### 2026-05-18T20:00:00Z: kevin-manifest-generator (from inbox)
# Manifest Generator — Implementation Decision

**Author:** Kevin (Backend Dev)
**Date:** 2026-05-13
**Status:** IMPLEMENTED

---

## What

Implemented `app/services/manifest_generator.py` per Satya's contract
(satya-manifest-generator-contract.md) with five public functions:
`list_schema_kinds`, `load_schema`, `extract_schema_fields`, `auto_map`,
`generate_manifests`.

Added `SchemaField`, `FieldMapping`, `MappingResult` to `app/models/osdu.py`.

---

## Key Design Decisions

### 1. Required field propagation stops at allOf boundaries

OSDU abstract schemas (e.g. `AbstractAnyCrsFeatureCollection`) have
internal `required` arrays that describe object shape, not CSV mapping
requirements. Per Darryl's conventions §7.1, all `data.*` fields are
technically optional. The walker only marks a field as required if its
name appears in the `required_names` set passed by the caller — nested
`$ref`-resolved schemas always pass `set()` so their structural
`required` entries do not leak into SchemaField.required.

**Impact:** `MappingResult.confidence` is 1.0 when no practical
required fields are declared. V2 can add a practical-required overlay
if operators need confidence scoring against domain rules.

### 2. Manifest section routing

Kind prefix determines the manifest section:
- `master-data--*` → `MasterData`
- `reference-data--*` → `ReferenceData`
- `work-product-component--*`, `dataset--*`, `work-product--*` → `Data`

### 3. No csv_to_json delegation in v1

The contract mentioned delegating row expansion to the vendored
`csv_to_json` engine. After inspecting that code, its template-parameter
parsing model doesn't align cleanly with the confirmed-mapping flow
(it expects `{{PARAM}}` tokens). Instead, `generate_manifests` builds
data blocks directly from the mapping — simpler, testable, and avoids
coupling to the template engine's internals.

### 4. Batch size at 1000 rows

Manifests are split at 1000 records per envelope to stay within OSDU
Workflow Service payload limits.

---

## Test Coverage

36 tests in `tests/test_manifest_generator.py`:
- Schema discovery, loading, field extraction
- Mapping heuristic (exact, suffix, Jaccard, edge cases)
- Manifest generation (structure, sections, batching, empty CSV, errors)
- End-to-end with real TNO Well CSV

All 216 related tests pass with zero regressions.



### 2026-05-18T20:00:00Z: kevin-multi-kind (from inbox)
# Multi-kind search and aggregation API design

Date: 2026-05-15T14:35:00Z
By: Kevin
Issue: #26

## Decision

Multi-kind search uses a wildcard kind (`*:*:*:*`) with a Lucene `kind:"..."` filter prepended to the query string, because OSDU Search v2 `/query` does not accept kind arrays. `build_multi_kind_query()` encapsulates this convention so callers don't embed Lucene syntax inline.

`search_with_aggregation()` is a separate function from `search_records()` — it returns `SearchAggregationResult` which carries both records and aggregation buckets. The existing `search_records()` is untouched for backward compatibility.

## Notes

- `aggregateBy` is a string field in the POST body (e.g., `"kind"`), not an array.
- When `aggregate_by` is omitted, `search_with_aggregation` behaves identically to `search_records` but returns the richer result type.
- `_parse_aggregation_buckets` rejects entries with empty keys, non-string keys, missing counts, or boolean counts — same defensive posture as the existing `_parse_aggregation_kinds`.
- Multi-kind query with a base query uses `AND` conjunction: `(kind:"A" OR kind:"B") AND <base_query>`.



### 2026-05-18T20:00:00Z: Backlog reconciled post upstream merges + PR #33 open
**By:** Satya (Lead) — requested by Mariel
**What:** Promoted backlog item **#4 Bulk ingestion submit** to the sole "Now" slot. Moved prior Now/Next items #1 (Run history), #2 (Wire Generate from CSV), #2b (TNO master-data manifests), #9 (Multi-kind search), and #10 (Field-builder query UI) to Done — all shipped either in upstream-merged PRs #11–#15 or in PR #33 (currently in review). Removed items #14 (Open PRs upstream — done, the workflow is now established) and #7 (CSV→manifest helper — service was the underlying delivery for #2) from active backlog as they no longer represent open work.
**Why:** Bulk-load and manifest-gen flow is end-to-end. The next operator capability gain is queueing multiple manifests with one progress view — Run History gives completions a home, `bulk_loader` already supports sequential submit semantics, and the Judson+Kevin pair is warm from adjacent work. Picked over #5/#6 (saved searches, export results) and #2c (AI Suggest) because all three touch PR #33's file surface, which would either block on review or create rework risk. #4 can be scoped as additive (new section/tab) and its contract can be designed in parallel with review of #33.
**Notes:** Implementation against the Bulk Load page should sequence after PR #33 merges to avoid forking the page surface; contract design can start immediately. Open question flagged to Mariel: wait for PR #33 to merge before starting #4 implementation, or branch from `marielherz_SearchAndManifestGen_clean` and reconcile at PR time. Backlog and `identity/now.md` updated in place with the 2026-05-18 timestamp.



### 2026-05-18T20:00:00Z: satya-manifest-generator-contract (from inbox)
# Manifest Generator — Interface Contract

**Author:** Satya (Lead)
**Date:** 2026-05-13
**Status:** Proposed

---

## Problem

Operators have CSV data and know their OSDU kind. They should not need to
hand-author JSON templates. The app should infer a column-to-field mapping
from the schema + CSV headers and produce ready-to-submit OSDU manifests.

The existing `csv_to_json.py` engine handles template + CSV → manifests.
The missing piece is **template generation**: schema + CSV headers → template.

---

## 1. Module Location

```
app/services/manifest_generator.py    ← new service (Kevin builds)
app/models/osdu.py                    ← new dataclasses added here
```

No new page for v1. Integration point is the existing Bulk Load page.

---

## 2. Public API

```python
# app/services/manifest_generator.py

def list_schema_kinds(schema_dir: Path | None = None) -> list[str]:
    """Return sorted OSDU kind strings for all vendored schemas.

    Scans ``app/data/osdu/rc--3.0.0/schemas/`` by default.
    Pure filesystem, no network.
    """

def load_schema(kind: str, schema_dir: Path | None = None) -> dict:
    """Load and return the raw JSON schema for an OSDU kind.

    Raises ``SchemaNotFoundError`` if the kind is not vendored.
    """

def extract_schema_fields(schema: dict) -> list[SchemaField]:
    """Walk a schema's ``data`` block and return a flat list of
    mappable fields with dotted paths, types, and required flags.

    Resolves ``$ref`` and ``allOf`` within the vendored schema tree.
    Only surfaces leaf properties under ``data`` — system fields
    (id, kind, acl, legal, meta, ancestry, tags) are handled by
    the manifest wrapper, not by column mapping.
    """

def auto_map(
    schema_fields: list[SchemaField],
    csv_headers: list[str],
) -> MappingResult:
    """Heuristic v1: fuzzy name-matching of CSV headers to schema fields.

    Returns matched pairs + unmatched CSV columns + unmatched required
    schema fields. Never raises — unmapped columns are the operator's
    problem to resolve in the UI.
    """

def generate_manifests(
    *,
    kind: str,
    csv_path: Path,
    mapping: list[FieldMapping],
    legal_tag: str,
    acl_owners: str,
    acl_viewers: str,
    data_partition_id: str,
    schema_dir: Path | None = None,
) -> list[dict]:
    """Produce a list of workflow-ready manifest dicts (one per CSV row).

    Each manifest follows the same ``executionContext`` envelope used
    by ``manifest_builder.build_file_generic_manifest``.

    Delegates row expansion to the vendored ``csv_to_json`` engine
    internally — callers never touch templates directly.

    Raises:
        ValueError: if required inputs are missing/empty.
        SchemaNotFoundError: if the kind is not vendored.
        MappingError: if a required schema field has no mapping.
    """
```

---

## 3. Data Types (added to `app/models/osdu.py`)

```python
@dataclass(frozen=True, slots=True)
class SchemaField:
    """One mappable leaf field extracted from an OSDU schema."""
    path: str              # dotted path under data, e.g. "FacilityName"
    field_type: str        # "string" | "number" | "integer" | "boolean" | "array"
    required: bool
    description: str = ""

@dataclass(frozen=True, slots=True)
class FieldMapping:
    """One CSV-column-to-schema-field binding."""
    csv_header: str
    schema_path: str       # matches SchemaField.path
    transform: str = ""    # v1: "" (none). Future: "int()", "datetime_YYYY-MM-DD()", etc.

@dataclass(frozen=True, slots=True)
class MappingResult:
    """Output of auto_map: matched pairs + leftovers."""
    mappings: list[FieldMapping]
    unmatched_csv: list[str]            # CSV headers with no schema match
    unmatched_required: list[str]       # required schema fields with no CSV match
    confidence: float                   # 0.0–1.0, fraction of required fields matched
```

---

## 4. Mapping Algorithm — v1 Scope

**Heuristic fuzzy name match.** No LLM calls in v1.

1. Normalize both sides: lowercase, strip underscores/hyphens/spaces.
2. Exact match first (normalized CSV header == normalized schema field name).
3. Suffix match: CSV header ends with the schema field's leaf name (handles
   `well_facilityname` → `FacilityName`).
4. Token overlap: Jaccard similarity on word tokens, threshold ≥ 0.5.
5. Remaining unmatched columns/fields are returned for operator review.

No auto-mapping of system fields (id, kind, acl, legal). Those come from
the function kwargs.

---

## 5. Integration Plan

### v1 (ships now)
- **Service only.** `app/services/manifest_generator.py` with full test
  coverage. Pure Python, no Streamlit dependency.
- **Bulk Load page wiring.** Add a "Generate from CSV" flow to
  `app/pages/9_📥_Bulk_Load.py`:
  1. Operator picks an OSDU kind (dropdown from `list_schema_kinds`).
  2. Operator uploads a CSV.
  3. App calls `auto_map`, shows the proposed mapping in an editable table.
  4. Operator confirms/adjusts, clicks Generate.
  5. App calls `generate_manifests`, writes results to a temp dir, and feeds
     them into the existing `submit_tier` pipeline.

### Deferred (not v1)
- **Standalone Streamlit page** (page 10) — only if operators want to
  generate manifests without immediately submitting.
- **CLI wrapper** — only if CI/CD pipelines need headless generation.
- **LLM-assisted mapping (v2)** — send schema + CSV sample to an LLM for
  smarter suggestions. Requires Azure OpenAI dependency. Park it.
- **Custom schema upload** — v1 only uses vendored schemas. Customer-supplied
  schemas are a future capability.
- **Type transforms** — v1 passes all values as strings. Type coercion
  (`int()`, `datetime()`, `float()`, `bool()`) is deferred to v2 unless
  trivially needed for a specific dataset.

---

## 6. Error Cases

| Error | Raised by | Handling |
|---|---|---|
| Kind not in vendored schemas | `load_schema` | `SchemaNotFoundError` — UI shows "Schema not available" |
| CSV has no headers | `auto_map` | `ValueError` — UI shows "Upload a CSV with headers" |
| Required schema field unmapped | `generate_manifests` | `MappingError` — UI highlights missing fields |
| CSV row has empty required cell | `generate_manifests` | Skip row, include in warning summary |
| Malformed CSV | `generate_manifests` | `ValueError` — surface parse error to operator |

---

## 7. File Ownership

| File | Owner | Reviewer |
|---|---|---|
| `app/services/manifest_generator.py` | **Kevin** (backend) | Darryl (OSDU correctness) |
| `app/models/osdu.py` additions | **Kevin** | Satya |
| `app/pages/9_📥_Bulk_Load.py` wiring | **Judson** (UI) | Kevin, Satya |
| `tests/test_manifest_generator.py` | **Kevin** | Charlie (test quality) |
| Schema field extraction test fixtures | **Darryl** | Kevin |

---

## 8. Dependencies

- No new pip dependencies for v1. Uses only stdlib + vendored `csv_to_json`.
- `csv_to_json.py` stays vendored and unmodified — `generate_manifests`
  builds a template dict in memory and calls `create_manifest_from_row`
  directly rather than going through the file-based
  `create_manifest_from_csv` wrapper.

---

## 9. Key Constraints

- **No network in the generator.** All schema resolution is local filesystem.
  The only network call happens downstream when `submit_tier` posts manifests.
- **Same manifest envelope.** Output must match the `executionContext` wrapper
  from `manifest_builder.py` so the existing ingestion pipeline accepts it
  without changes.
- **Path safety.** Any file path resolution must assert paths stay under
  `app/data/` — same sandbox pattern as `bulk_loader.py`.


