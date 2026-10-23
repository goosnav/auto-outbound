# Local HTTP contract

The application serves a single local user on `127.0.0.1`. This is not a hosted or multi-user API. All work is stored in the same local workspace used by the CLI.

Fetch `GET /api/session` from the same origin. Keep its token in memory and send it as `X-Session-Token` on every mutation. Cross-site requests and unexpected Host headers are rejected. Never put the token in a URL. CORS is not enabled. `GET /api/openapi.json` supplies the versioned FastAPI schema.

| Method | Path | Operation |
|---|---|---|
| GET | `/health/ready` | Readiness, app identity and version |
| GET | `/api/session` | Session token, local paths, CLI capability status |
| GET/POST | `/api/campaigns` | List/create campaigns |
| GET/PATCH | `/api/campaigns/{id}` | Campaign, leads, jobs, counts, offer documents |
| POST | `/api/campaigns/{id}/documents` | Multipart `file`: extract and save offer text |
| DELETE | `/api/campaigns/{id}/documents/{document_id}` | Remove offer document and invalidate drafts |
| POST | `/api/campaigns/{id}/import` | Multipart CSV/XLSX lead import |
| POST | `/api/campaigns/{id}/verification/import` | Multipart `file` plus optional `source` |
| POST | `/api/campaigns/{id}/outcomes/import` | Multipart sender/CRM CSV |
| POST | `/api/campaigns/{id}/jobs` | `{ "stage": "pipeline" }` or individual stage |
| GET | `/api/jobs/{id}` | Durable progress and recent events |
| POST | `/api/jobs/{id}/pause` | Stop active calls and save state |
| POST | `/api/jobs/{id}/resume` | Continue with retained counters and valid caches |
| POST | `/api/jobs/{id}/cancel` | Stop without scheduling more work |
| PATCH | `/api/leads/{id}` | Edit contacts/drafts and record review/verification |
| GET | `/api/campaigns/{id}/export?format=csv&scope=all` | CSV/XLSX; scopes all, approved, callable |
| GET/POST | `/api/suppressions` | Global email/domain exclusions |
| POST | `/api/suppressions/import` | Multipart exclusion CSV |
| POST | `/api/demo` | Create a clearly fictional offline campaign |
| POST | `/api/quit` | Graceful local server and agent shutdown |

Campaign settings: provider, model, target_leads, min_fit, discovery_queries, max_agent_calls, max_minutes, concurrency, agent_timeout_seconds, max_turns, max_cost_usd and research_pages. See the OpenAPI models for bounds. Edits that could race an active job return `409`; pause first.

Job states: queued, running, paused, completed, failed, budget_exhausted, cancelled. `completed`/`total` describe the **current stage**, not an invented global ETA. `stage_index`/`stage_count` describe pipeline position. Optional reported usage remains null when unavailable. `cost_unreported` is a conservative Claude allowance, not a claim of actual spend.

Errors use `{"error":{"code":"STABLE-CODE","message":"Actionable message"}}`. Errors omit submitted offer contents, raw provider stderr, tokens, and credentials. No endpoint sends messages, makes calls, or exports stored provider credentials.
