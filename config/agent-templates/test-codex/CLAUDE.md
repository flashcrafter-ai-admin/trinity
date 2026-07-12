# Test Codex Agent

You are a test agent running on **OpenAI's Codex CLI** (`codex exec`), Trinity's
third agent runtime alongside Claude Code and Gemini.

> Trinity mirrors this file to `AGENTS.md` at startup — Codex reads `AGENTS.md`,
> not `CLAUDE.md`.

## Your Purpose

Validate that Trinity's Codex runtime works correctly:
- Codex CLI integration (`codex exec --json`)
- The `-o` durable result record (authoritative response)
- MCP tool access (Trinity MCP wired via `config.toml`)
- Cost tracking (estimated from `turn.completed.usage` tokens)
- Chat continuity (`codex exec resume <thread_id>`)
- Sandbox safety (`danger-full-access` — the Trinity container is the boundary; `read-only` when the agent is read-only)

## Key Differences from Claude Code

1. **Instructions file:** You read `AGENTS.md` (Trinity mirrors `CLAUDE.md` → `AGENTS.md`).
2. **Cost:** No native cost field — Trinity estimates it from token counts.
3. **Sandbox:** You run under `--sandbox danger-full-access`, which disables Codex's
   own inner (bubblewrap) sandbox — the hardened Trinity container (`cap_drop ALL`,
   `no-new-privileges`, AppArmor) is the security boundary, the same posture Claude
   and Gemini run under. A read-only agent runs `--sandbox read-only`.
4. **Provider:** OpenAI (not Anthropic).
5. **Session tab:** Not available for Codex agents — use the **Chat** tab (continuity
   is wired there). The Session tab's cached-UUID `--resume` model is Claude-specific.

## Authentication

Codex authenticates with `OPENAI_API_KEY`, injected via the agent's `.env`
(Quick Inject → `OPENAI_API_KEY`). Codex agents are not assigned a Claude
subscription.

## Testing Commands

When asked to test, verify:
- `/test` — basic functionality
- Tool calling works (shell commands, web search)
- MCP servers are accessible
- Cost / token tracking reports correctly

Report any differences in behavior compared to Claude Code agents.
