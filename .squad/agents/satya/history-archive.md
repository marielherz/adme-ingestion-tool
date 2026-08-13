
<!-- Archived 2026-05-18T20:00:00Z by Scribe (history summarization) -->

# Project Context

- **Owner:** Eirik Haughom
- **Project:** Streamlit control plane app for Azure Data Manager for Energy (ADME)
- **Stack:** Python, Streamlit, Azure, ADME/OSDU APIs
- **Created:** 2026-04-24

## Learnings

- Satya owns scope, architecture decisions, and reviewer gating for the ADME control plane.
- 2026-05-05T14:11:09.427+02:00: Issue #8 implementation handoff keeps auth tokens, MSAL flow payloads, and cache material out of `ADMEConnection`; session auth belongs in `app.connection_state`, and Settings owns callback consume/clear sequencing.
- The product is an operator-facing Streamlit app for managing ADME workflows and platform operations.
- 2026-04-24: Established project scaffolding — flat `app/` layout, `pyproject.toml` as config hub, latest stable deps (Streamlit 1.56+, Python ≥3.11, ruff 0.15+, pytest 9.0+).
- Key file paths: `app/main.py` (entry point), `pyproject.toml` (deps + tool config), `.streamlit/config.toml` (UI theme), `tests/` (test suite).
- Ownership: Judson=UI, Kevin=backend services, Scott=infra/CI, Charlie=tests.
- Design choice: flat `app/` over `src/` layout because Streamlit apps run with `streamlit run app/main.py`, not as installed packages.
- Streamlit theme uses Microsoft Fluent colors (#0078d4 primary) to match ADME branding.

## 2026-04-24 Scribe Consolidation
- Decision to use GitHub Issues for all work tracking (user directive)
- Decision to always update GitHub issues with real status (user directive)
- Streamlit architecture decision documented and archived
- Team ownership split documented: Judson=UI, Kevin=backend, Scott=infra, Charlie=tests

## 2026-04-24 ADME Connection Architecture (Issue #2)
- Defined shared contract in `app/models/connection.py`: `ADMEConnection`, `ServiceHealthResult`, `OSDU_SERVICES`.
- `ADMEConnection.is_valid()` enforces that SP auth requires client_secret.
- `data_partition_id` is a required input (OSDU APIs need the `data-partition-id` header).
- Auth approach: `DeviceCodeCredential` for user impersonation, `ClientSecretCredential` for SP — both via `azure-identity`.
- Health checks: No `/health` endpoint on OSDU services. Use cheap read-only API probes with 5 s timeout, run concurrently via `ThreadPoolExecutor`.
- Page structure: `app/main.py` = welcome, `app/pages/1_⚙️_Settings.py` = settings/connection.
- Backend services go in `app/services/auth.py` and `app/services/health.py`.
- Contract file (`app/models/connection.py`) is the shared interface — changes require lead sign-off.
- Kevin and Judson can work in parallel after the contract is committed.

## 2026-04-24 Issue #2 ADME Connection Design (Issue #2 Complete)
- Approved welcome and settings page architecture for connection management
- Committed shared contract in app/models/connection.py (ADMEConnection, ServiceHealthResult)
- Defined OSDU_SERVICES list for canonical health probes
- Auth design: azure-identity with DeviceCodeCredential (user) and ClientSecretCredential (SP)
- Health checks via concurrent OSDU API probes (5s timeout, no dedicated /health)
- UI: app/main.py (welcome), app/pages/1_⚙️_Settings.py (settings)
- Ownership split: Kevin=services, Judson=UI, Charlie=tests, Scott=no sprint work
- data_partition_id is required input (OSDU header requirement)
- Session state for config, future .env/.secrets support
- Contract file requires Satya sign-off for any changes
- Ready for parallel Kevin/Judson work; Charlie blocks on test coverage

## 2026-04-24 Issue #4 Interactive Browser Login Design
- Approved InteractiveBrowserCredential as correct replacement for DeviceCodeCredential
- Direct 1:1 swap: same OAuth 2.0 authorization code grant, better UX (browser opens automatically vs device-code copy-paste)
- Identified minimal affected files: app/services/auth.py, app/pages/1_⚙️_Settings.py, tests/test_auth.py, tests/test_auth_service.py
- No changes needed: app/models/connection.py, requirements.txt, pyproject.toml, service-principal auth
- Implementation notes documented: _build_credential() pattern, runtime caveats (local-only, credential cleanup, error handling, type annotations)
- Design approved — ready for implementation

## 2026-04-25 Issue #5 Interactive Auth Callback Fix Design
- Root cause analysis: InteractiveBrowserCredential was using ADME confidential-client app ID; Azure AD rejected post-callback token exchange with AADSTS7000218 because confidential clients require client_secret which InteractiveBrowserCredential doesn't send
- Recommended fix: Use Azure CLI well-known public client ID (`04b07795-a710-4f9e-9640-a91e60e60e08`) for credential instantiation while preserving `connection.client_id` for scope derivation
- Why this works: Azure CLI's public client is trusted by all Azure AD tenants; token's audience remains ADME resource via scope derivation
- No UI or model changes needed; minimal backend fix
- Optional future enhancement: add `interactive_client_id` field to ADMEConnection for custom public-client support
- Design approved — ready for implementation

## 2026-04-25 Issue #6 Tenant-Compatible Interactive Auth Design
- Root cause: Azure CLI public client ID (from issue #5) is blocked in IPS-Energy tenant — some enterprises restrict external app consent via policies, conditional access, or allowlists
- New approach: Use customer's own configured app registration (from Settings) instead of hardcoded Microsoft public client
- Scope fix: Hardcode ADME scope to `https://energy.azure.com/.default` (constant across all instances) instead of deriving from client_id
- Why this works: Customer's app is guaranteed to exist in their tenant and be authorized by their admin; scope is resource-based (ADME), not client-based
- No UI or model changes needed beyond scope constant
- Files to change: `app/services/auth.py` (remove AZURE_CLI_PUBLIC_CLIENT_ID, use connection.client_id), `app/models/connection.py` (hardcode scope property), tests (update assertions)
- Design approved — ready for implementation

## 2026-04-25 Issue #7 Auth Redirect Behavior Design
- Root cause: InteractiveBrowserCredential starts ephemeral HTTP server on localhost:8400 to capture OAuth code. SDK must receive code on that server; cannot redirect to Streamlit (8501) without breaking token exchange.
- Key finding: This is fundamental constraint of authorization code flow with local listener. Applies equally to MSAL Python.
- Two-part fix: (1) Update Settings page guidance to explain new browser tab opens for sign-in; users should return to Streamlit after closing it. (2) Explicitly pass `redirect_uri="http://localhost:8400"` to credential in app/services/auth.py.
- Why explicit redirect_uri: Makes port deterministic (no SDK-version drift), self-documents intended behavior, ensures alignment with app registration.
- Out of scope: No custom success pages (not exposed by InteractiveBrowserCredential), no Streamlit redirect (breaks auth), no raw MSAL calls (adds complexity).
- Expected outcome: User understands full UX flow; opens new tab for sign-in → closes tab → returns to Streamlit → sees results.
- Design approved — ready for implementation

## 2026-04-26 Issue #8 App-Returning Auth Flow Design
- Current `InteractiveBrowserCredential` path cannot provide a true redirect-back-to-Streamlit UX because the SDK owns the localhost:8400 callback listener.
- Recommended replacement: app-managed MSAL Python authorization-code + PKCE flow with `redirect_uri=http://localhost:8501` and scope `https://energy.azure.com/.default`.
- Target operator UX: Settings page shows Sign In when unauthenticated, browser returns to Streamlit authenticated after Entra login, and Sign Out clears session state. Service-principal auth remains unchanged.
- Main implementation risk: Streamlit reruns require OAuth callback query parameters to be consumed and cleared exactly once to avoid replaying token exchange.
- Design complete — ready for implementation planning

- 2026-05-05T14:11:09.427+02:00: Final issue #8 review approved the app-returning MSAL auth contract: service wraps MSAL, Streamlit session owns pending/completed auth state, callback params clear once, and service-principal auth remains unchanged.

## Issue #8 Auth Flow - Team Completion (2026-05-05)

**Status:** ✅ COMPLETE & VALIDATED

All team members successfully completed assigned work for MSAL auth integration:
- Satya: Lead review and final validation
- Kevin: Auth-service implementation
- Scott: Documentation and README updates
- Judson: Settings page integration
- Charlie: Quality gate and regression coverage

Final outcome: Full test suite passed (70), Ruff clean, mypy clean. Ready for merge.

- 2026-05-05T15:11:17.396+02:00: Manual token-scope feature contract: add `ADMEConnection.token_scope` defaulting to `ADME_RESOURCE_SCOPE`, keep auth callers on `connection.scope`, and treat scope changes as connection changes that clear user auth, pending flows, and health.
- 2026-05-05T15:11:17.396+02:00: Final manual token-scope review approved the implementation. Accepted blank/whitespace scope as default fallback via `connection.scope`, confirmed both MSAL and service-principal auth use that accessor, accepted Charlie's validation and lockout-safe revision evidence, and independently verified targeted tests (49), full pytest (80), Ruff, and mypy.
## 2026-05-05: Manual Token Scope Configuration (Complete)

**Status:** COMPLETE
**Decision:** Manual token scope configuration merged to decisions.md
**Outcome:** ADMEConnection now includes token_scope field with ADME default fallback. Settings UI exposes non-secret Token scope field. Both auth paths (user and service principal) consume connection.scope. All validation passed: pytest 80, ruff, mypy.
## 2026-05-05 Entitlements page architecture (Mariel)
- New page app/pages/2_🔑_Entitlements.py exercises ADME Entitlements API as operator smoke test (distinct from health probe).
- New service app/services/entitlements.py with fetch_member_self + fetch_groups; mirrors health.py (stdlib + requests, 5s timeout, no internal retries).
- EntitlementsCallResult dataclass lives in app/models/connection.py alongside ServiceHealthResult — keep shared UI/backend contract co-located.
- Result envelope: ok, http_status, latency_ms, correlation_id, error_message, raw_response, data, plus endpoint/path labels.
- Correlation ID: case-insensitive header lookup across correlation-id, x-correlation-id, request-id, x-request-id.
- In-session history at st.session_state['entitlements_history'] as list of dicts (timestamp, endpoint, latency_ms, http_status, ok); cleared on connection/auth/scope change via same hooks as health state.
- Auto-run-once guard prevents Streamlit-rerun re-fire; explicit Re-run button bypasses guard. No token re-prompt on this page; no per-page partition override.
- Out of scope v1: groups pagination, filtering UI, membership management, cross-rerun caching.
- Handoff: Kevin (service + model), Judson (page + chart + history wiring), Charlie (mocked-HTTP service tests + page smoke test). No Scott work, no new deps.

## 2026-05-06 Entitlements 405 fix (Mariel)
- Root cause: `/api/entitlements/v2/members/me` does not exist in ADME — returns 405. Real per-user endpoint is `/api/entitlements/v2/members/{object-id}/groups?type=none`, keyed on Entra OID, and returns desId/memberEmail/groups in one payload.
- Decision: extract OID from JWT `oid` claim via stdlib base64url + json in new `app/services/token_utils.py` (no signature check — we just got the token from MSAL); add `fetch_my_groups(connection, token, object_id)`; delete `fetch_member_self` + `MEMBERS_SELF_*` constants entirely.
- Page: identity card derived from my-groups response (memberEmail + desId/OID); my-groups primary card; all-groups demoted to secondary expander; pre-flight guard renders friendly error and skips HTTP when OID missing.
- History label for the per-user call is the literal string `members.{oid}.groups` — keeps chart axes/session history free of per-user OIDs.
- Handoff: Kevin (service + token_utils), Judson (page rewire), Charlie (delete member-self tests, add token_utils + my-groups tests, update page tests). No Scott, no new deps.

## Learnings

### 2026-05-06: Ingestion MVP contract locked
- Wrote .squad/decisions/inbox/satya-ingestion-mvp-contract.md covering services (ingestion.py, erification.py), models (osdu.py), page (3_📥_Ingestion.py), and tests.
- Locked polling: native Streamlit `st.rerun()` + `time.sleep` ladder (2s/5s/10s, 30-min timeout) with a manual "Refresh status now" escape hatch. Rejected `st.autorefresh` / `streamlit-extras` to keep deps unchanged.
