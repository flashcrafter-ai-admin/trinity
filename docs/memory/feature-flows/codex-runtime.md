# Feature: OpenAI Codex Runtime (#1187)

## Overview

A Trinity **harness IS an `AgentRuntime`** — the pluggable execution engine inside
the agent container. **Codex** is the third runtime, alongside Claude Code
(default) and Gemini CLI. An agent runs on Codex when its template declares
`runtime: { type: codex, model: gpt-5.1-codex }`; the backend creates the
container with `AGENT_RUNTIME=codex`, and `codex_runtime.py` implements the ABC.

The hard part is **not** wiring a new CLI — it's achieving full parity with
Trinity's Claude-specific safety layer (system prompt, read-only mode,
guardrails, credential sanitization), which a naive port silently bypasses.

This is an MVP (follow-up to spike #854). For adding a *fourth* runtime, see the
[Harness Authoring Guide](../harness-authoring-guide.md).

## Flow: UI → API → Runtime → Side Effects

1. **Create.** Template `runtime.type: codex` → `crud.py` sets `AGENT_RUNTIME=codex`
   env + `trinity.agent-runtime=codex` label. Codex agents **skip** Claude-subscription
   auto-assign (`is_claude_runtime()` gate) — no `CLAUDE_CODE_OAUTH_TOKEN`, no
   persisted `subscription_id`. `lifecycle.py` mirrors the skip on recreate.
2. **Startup** (`startup.sh`). Mirrors `CLAUDE.md` → `AGENTS.md` (Codex reads
   `AGENTS.md`), creates `CODEX_HOME` under `$TMPDIR` (off the git-tracked repo),
   gitignores `.tmp/`. MCP config (`trinity_mcp.py`) writes `$CODEX_HOME/config.toml`.
3. **Chat / Task.** `POST /api/chat` → `runtime.execute()`; `POST /api/task` →
   `runtime.execute_headless()`. Both build `codex exec --json --skip-git-repo-check
   -C /home/developer --sandbox <mode> -o <CODEX_HOME>/<exec_id>-last.txt
   [-m model] [resume <thread_id>] -- <prompt>` and stream the JSONL.
   `<mode>` = `danger-full-access` normally (no inner bwrap sandbox — see Safety
   parity), `read-only` when the agent is read-only. Exec-level flags **precede**
   `resume` (the `resume` sub-subcommand rejects them — the turn-2+ continuity
   fix); `--` ends options so a `-`-leading prompt can't be reparsed as a flag.
4. **Parse.** `thread.started`→session id; `turn.completed.usage`→tokens (cost
   estimated; `reasoning_output_tokens` ⊂ `output_tokens`, never double-counted);
   `item.completed`→agent message / tool activity; `turn.failed`/`error`→error.
   The `-o` file is the **authoritative** response (read-then-delete in `finally`);
   JSONL `agent_message` is the fallback.
5. **Return.** `(response_text, execution_log, ExecutionMetadata, …)` — same shape
   as Claude/Gemini, so the backend treats Codex executions identically.

## Safety parity (Phase C — blocking)

| Control | Claude | Codex |
|---------|--------|-------|
| System prompt | `--append-system-prompt` | prepended to the prompt; `AGENTS.md` for identity; MCP-tool naming made runtime-aware (no `mcp__trinity__` prefix — see MCP) |
| Sandbox | none (container is the boundary) | normal → `--sandbox danger-full-access` (Codex's own bwrap sandbox can't create a user namespace in the hardened container — `bwrap: No permissions…` — which blocks every tool; drop it, the container stays the boundary, same as Claude) |
| Read-only | PreToolUse hook on `~/.trinity/read-only-config.json` | reads the same file → `--sandbox read-only` (Codex has no PreToolUse hook; a fail-closed enforcement story is a fast-follow) |
| Guardrails | `--disallowedTools` + turn caps | sandbox + network; unmappable tool-names **logged** (not dropped) |
| Credential redaction | sanitizer over response + logs | identical sanitizer calls |

## Error → HTTP mapping

auth (missing/invalid key, 401) → **503**; rate-limit → **429**;
runtime-unavailable → **500** (NOT 503 — 503 is the backend's AUTH signal, and the
dispatch breaker counts AUTH only); early pipe drop → **502** (SUB-003 guard).
Generic failures staying at 500 keep the AUTH path and SUB-003 auto-switch inert
for Codex; the #678 reader-race retry never matches a Codex 502 (no
`recovery_attempted` marker).

## Capabilities & Session tab

`CodexRuntime.capabilities()` → `chat_continuity=True` (`codex exec resume`),
`session_tab_resume=False`, `mcp_support=True`, `cost_reporting="estimated"`.
Because `session_tab_resume=False`, the backend gates the Session-tab cached-UUID
`--resume` turn off (one constant `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` in
`sessions.py` → stateless turn) and the frontend hides the Session tab. The Chat
tab (with continuity) stays.

## MCP

`_inject_codex_mcp` / `_configure_codex_mcp_servers` write `$CODEX_HOME/config.toml`
directly (merging, idempotent — same approach the Gemini path uses for its
settings.json). The Trinity HTTP MCP references the token via `bearer_token_env_var`
= `TRINITY_MCP_API_KEY` — **the literal secret is never persisted** to config.toml.

Registering the server is only half of MCP working. `PLATFORM_INSTRUCTIONS`
documents the tools with Claude's `mcp__trinity__<tool>` prefix; Codex
auto-discovers MCP tools by bare name and answers `unknown MCP server` to a
prefixed call. So the platform prompt is **runtime-aware**:
`platform_prompt_service.get_platform_system_prompt(runtime=…)` /
`compose_system_prompt(runtime=…)` strip the prefix and add a Codex orientation
note for `runtime="codex"` (Claude/Gemini/unknown unchanged). The runtime is
threaded from `routers/chat.py` + `services/task_execution_service.py`, resolved
best-effort + lazily from the `trinity.agent-runtime` label
(`docker_service.get_agent_runtime`).

## Key files

| Layer | File |
|-------|------|
| Base image | `docker/base-image/Dockerfile` (`@openai/codex@0.139.0`), `startup.sh` (AGENTS.md, CODEX_HOME) |
| Runtime | `docker/base-image/agent_server/services/codex_runtime.py` |
| Contract | `runtime_adapter.py` (`RuntimeCapabilities`, factory + validation), `models.py` (`ExecutionMetadata.status/error_code`) |
| MCP | `agent_server/services/trinity_mcp.py`, `services/platform_prompt_service.py` (runtime-aware tool naming) |
| Backend | `services/agent_service/{crud,lifecycle,helpers,terminal}.py`, `routers/sessions.py`, `routers/chat.py` + `services/task_execution_service.py` (thread runtime), `services/docker_service.py` (`get_agent_runtime`) |
| Frontend | `components/RuntimeBadge.vue`, `components/AgentTerminal.vue`, `views/AgentDetail.vue` |
| Template | `config/agent-templates/test-codex/` |

## Tests

Unit (`tests/unit/test_codex_*`, `test_runtime_*`, `test_session_tab_gate_codex`,
`test_platform_prompt_runtime`): JSONL parser → metadata, cost (no reasoning
double-count + cached pricing + default), error→status (pipe-drop 502, generic
500-not-503), capabilities matrix, factory + unknown-runtime validation,
subscription skip, MCP config (+ no dup on restart + token-not-persisted),
**sandbox resolution** (normal → `danger-full-access`, read-only stays, no dead
`network_access` flag), **runtime-aware prompt** (codex omits `mcp__trinity__` +
gets the orientation note, Claude/Gemini unchanged), resume arg-order guard,
Session-tab gate + backend inertness. E2E in `/verify-local`: a real
`AGENT_RUNTIME=codex` agent with an injected `OPENAI_API_KEY`, one `/api/chat` +
one `/api/task` turn (tools create+read a file; MCP `list_agents`).

## Out of scope (fast-follow)

Shared subprocess-helper DRY extraction; Session-tab cached-UUID resume for Codex;
backend reading `ExecutionMetadata.error_code` directly; Codex SSE streaming;
vision/images; a post-creation runtime-switch endpoint.
