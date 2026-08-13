---
updated_at: 2026-05-18T20:00:00Z
focus_area: PR #33 in review upstream; next Now is Bulk ingestion submit (#4)
active_prs:
  - "#33: feat(search) wave 3 — multi-kind search, query builder, manifest generator, TNO master-data fixtures (in review against EirikHaughom/adme-ingestion-tool:main)"
recently_merged_upstream:
  - "#11 Ingestion"
  - "#12 File Upload"
  - "#13 Run History"
  - "#14 TNO Vendor"
  - "#15 Bulk Load"
---

# What We're Focused On

PR #33 is open against upstream and awaiting Eirik's review. It bundles the remaining work from the prior "Now/Next" tier: CSV→manifest UI wired into Bulk Load, TNO master-data manifests vendored + enabled, multi-kind search, and the Lucene field-builder.

The new **Now** is **Backlog item #4 — Bulk ingestion submit** (paste-many / queue multiple manifests with one progress view). It is unblocked because Run History (#1) shipped in PR #13, so completed runs have a persistent home. Owners: Judson (UI) + Kevin (service-side queueing on top of existing `bulk_loader`).

Sequencing caveat: implementation against the Bulk Load page should land *after* PR #33 merges to avoid forking the page surface. Contract / interface design can start now.

Open question for Mariel before kickoff: should we (a) wait for PR #33 to merge before starting #4 implementation, or (b) branch from `marielherz_SearchAndManifestGen_clean` and reconcile at PR time?
