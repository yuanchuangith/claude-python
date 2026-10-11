Initial inspection: Python package with ActionDesign FastAPI backend and MCP modules.
Backend entrypoint: claude_agent_sdk.actiondesign_gateway.app:app, conventional port 8888. MCP is a gateway/proxy with no configured business MCP server in this checkout. System Python 3.12 already has FastAPI, Uvicorn, MCP. ANTHROPIC_API_KEY exists (value not read).
User confirmed MCP gateway only. Local launcher binds to 127.0.0.1:8888, enables MCP proxy/browser configuration and required service authentication, stores random local credentials in git-ignored .env.local.json.
Running at http://127.0.0.1:8888; admin /admin/; health /api/actiondesign-agent/health. Launch PID 35204; listening worker PID 30452. Logs: logs/local/stdout.log and stderr.log. Admin password: ACTIONDESIGN_ADMIN_PASSWORD in .env.local.json. Restart: powershell -NoProfile -ExecutionPolicy Bypass -File ./start-local.ps1 (after stopping existing instance).
Default model is now mimo-v2.6-pro via the claude-code Anthropic-compatible profile, Singapore endpoint https://token-plan-sgp.xiaomimimo.com/anthropic/v1/messages, explicit bearer auth overrides inherited API-key selection. User model aliases persisted in .env.local.json. Existing admin password unchanged (not recorded here).
# Frontend Look integration, 2026-10-02

- Registered `gxp-lowcode-readonly` in the gateway at http://127.0.0.1:8888, using stdio/gateway_proxy and the current Windows user's existing Look credentials. Node: E:/NodeJs/nvm/v24.15.0/node.exe; launcher: F:/Desktop/git/look_lowcode/scripts/start_mcp.mjs; cwd: F:/Desktop/git/look_lowcode.
- Separate executable and working-directory allowlists are supported. Backend MCP dependency pinned to >=1.23,<2 because SDK v2 breaks this gateway's v1 client API. Installed version: 1.30.0.
- Frontend F:/Desktop/plan/component: 41 reviewed tools from Look revision 8074356, skill 1.3.0 and eight reference documents. Static sync script is reproducible. New tools have schema validation, bounded results, explicit effect classification and serial execution. Maintenance requires intent/current-run evidence as appropriate.
- Live calls passed: connection_status, cpm_snapshot_status, schema_snapshot_status, source_repository_status, search_business_logic_graph, diagnose_codex_input. Database connection is healthy. CPM/Schema snapshots report stale; no forced refresh or business write performed.
- Frontend dev server on 3111 returned web.js HTTP 200 containing the new adapter and skill assets. This verifies the served bundle, not an interactive hosted GXP Agent conversation.
- Gateway proxy/admin tests: 26 passed. Scoped frontend TypeScript check passes with skipLibCheck; full dependency declaration checks have existing third-party errors.
- Broader backend lifecycle tests: 8 passed, 4 failed concerning existing HTTP transport policy expectations. Re-running with the unmodified HEAD mcp_proxy module reproduces all four failures. Existing Ruff findings remain outside changed lines; no broad cleanup performed.
- Credentials remain in ignored .env.local.json. MiMo default configuration and local admin credentials were preserved.

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