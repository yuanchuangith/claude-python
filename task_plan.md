# Local backend and MCP startup
- [x] Identify entrypoints and existing local configuration.
- [x] Start backend and MCP gateway using local configuration.
- [x] Verify health, admin login, and MCP configuration endpoint; record URLs and logs.

Completed: 2026-10-02. User confirmed the built-in MCP gateway. No upstream MCP server is configured, so tool calls were not exercised.

## Default model configuration
- [x] Configure user-provided MiMo endpoint/token/model locally.
- [x] Restart and verify effective default model and admin password.


## Frontend Agent Look integration
- [x] Register Look stdio through gateway with executable and directory allowlists.
- [x] Upgrade frontend discovery, tool validation and permission classification for 41 tools.
- [x] Sync Look skill/references into Agent skill registry.
- [x] Run backend/frontend regression and real gateway integration checks.

Completed 2026-10-02: gateway registration healthy, 41 tools, six real frontend adapter calls passed. Frontend dev server on 3111 serves the new adapter and skill assets. See findings.md for validation limits and pre-existing failures.

## CPM browser gateway connectivity
- Confirmed Origin http://cpm.rongchang.gxp2.com:51124 added to ignored local config; credentials unchanged.
- Added opt-in ACTIONDESIGN_AGENT_ALLOW_PRIVATE_NETWORK with explicit non-wildcard origins; tested middleware dependency Starlette >=1.7.0.
- Live models/admin login private-network preflight, admin login and authenticated models return 200; unrelated origins remain rejected.
- Four added tests pass. App/admin suite: 74 passed, 5 failed. Pre-change in-memory baseline: 70 passed, same 5 failed; prior working-tree edits preserved.
- Gateway launcher PID 35732. Actual browser refresh remains to be verified by user.
## CPM local default credential
- User requested default access. Configured a dedicated Client API Key in ignored .env.local.json; key was not output or placed in frontend code.
- Applies only to a loopback peer, exact configured CPM Origin, no supplied credentials and /api/actiondesign-agent runtime routes. Admin and /v1 remain explicitly authenticated; revoked/invalid keys fail closed.
- Default key included in secret redaction. Focused default/compatibility/key-store tests: 28 passed.
- Live CPM Origin /models request with no authentication headers returned 200, default model mimo-v2.6-pro. Admin password unchanged. Default access kept following user clarification.
## 2026-10-16 Full request log admin viewer
- [x] Inspect existing log and admin flows.
- [x] Add plaintext HTTP request/response capture and independent system log tab, filtering and clear.
- [x] Verify tests, restart local service and check live endpoints.

Errors: PowerShell wildcard rg path rejected; use directory path. Linked frontend agent-context.md absent in sibling repository; proceed with backend source and supplied document.

Completed 2026-10-17: local plaintext capture enabled and gateway restarted, PID 24560 (launcher). Eight new system log tests and related regression suite: 108 passed; one existing admin runtime-policy expectation excluded because HEAD already exposes openai-responses but the old test expects only two protocols. Browser login/tab/filter/detail/clear-confirmation/logout verified. API deletion verified on isolated test stores; live history preserved.

Resolved during implementation: use installed ruff executable because venv ruff module cannot locate its executable; test HTTPX proxy mounts bypassed for MockTransport; browser dialog verification changed to asynchronous handler; Unicode full-text search preserved while malformed JSON remains raw text; partial patch failures reapplied after reading exact context.

## 2026-10-17 Shared business knowledge for both frontend Agents
- User resumed implementation of ../component/doc/business-operation/knowledge-requirements-2026-10-17.md.
- [x] Inspect effective MiMo metadata, native tool calling and existing LocalDocRAG / frontend contracts.
- [x] Connect shared read-only business evidence to both existing Agents; preserve development knowledge and execution isolation.
- [x] Complete local host routing/injection and verify evidence pagination/version boundaries.
- [x] Fix reproduced partial-table completions and route evidence-backed answers through required knowledge review; preserve state and effect isolation.
- [x] Run actual MiMo / real knowledge-source tasks separately for both Agents; record limits honestly. Final 04:29 batch ended naturally; execution of validation is complete, not semantic-quality acceptance.
- [x] Update current docs and run scoped checks; no component/global builds, commits, push or deployment.
- [ ] Resolve remaining KB-08/13/14 and requirement section 7 semantic-quality failures: missed field conflicts, unsupported inverses/combinations/units, Reviewer false positives/negatives and non-converging evidence expansion. The full user objective is not marked achieved.
- Delegation: frontend_shared_knowledge owns component source/tests and connection contracts; backend_knowledge owns LocalDocRAG service/tests/API/ARCHITECTURE. Root owns gateway/live validation/status docs.

## 2026-10-17 User follow-up: verify effectiveness and let both Agents use the library
- [x] Restore current context, preserve dirty changes and inspect actual previous failures.
- [x] Add a shared read-only business-knowledge usage Skill to both existing Agent catalogs, with source-first rule extraction and scoped application; no copied business answers or wider authority.
- [x] Verify catalog/activation/source isolation and scoped regressions, then run actual MiMo tasks against the same real library for both Agents. Twenty affected final regression files passed; actual Skill activation confirmed in both domains, semantic acceptance separate.
- [x] Compare final answers against canonical source clauses and retain pass/fail evidence; update current docs with actual outcomes. Final07:49 six cases and07:55 same-fact pair ended; all load Skill, but whole-answer semantic acceptance fails. The separate unresolved-quality task above remains open.

## 2026-10-17 Admin session usability
- [x] Identify memory-only refresh logout and fixed one-hour expiration; preserve other dirty changes.
- [x] Add HttpOnly cookie session restore, renewal/revocation, and precise 401 handling.
- [x] Verify API/browser refresh and logout behavior, apply local 24-hour idle TTL, restart gateway and document limits.
