---
description: Start or continue a client onboarding workflow
allowed-tools: Read, Write, Glob, Bash, mcp__trinity__list_agents, mcp__trinity__chat_with_agent
---

# Onboard Client

Start or continue a client onboarding workflow.

## Steps

1. Parse the client slug, service scope, target launch date if present, and any
   operator notes from the user message.
2. Create or update
   `~/.trinity/pipeline-state/client-onboarding/<client-slug>.json`.
3. Write the current projection to
   `/home/developer/shared-out/pipeline/onboarding-state.json`.
4. Ask the intake, access, and communications agents for their current status.
5. Ask the relevant domain agents to build or refresh readiness plans.
6. Ask reporting/QA to identify missing evidence and gated actions.
7. Return a concise status report with blockers, owners, gates, and next
   autonomous actions.

Do not approve or perform external side effects. Queue exact approval requests
when a worker needs a client send, access change, launch, publish action, or
spend-affecting change.
