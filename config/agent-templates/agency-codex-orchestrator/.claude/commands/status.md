---
description: Summarize onboarding state
allowed-tools: Read, Glob, Bash, mcp__trinity__list_agents, mcp__trinity__chat_with_agent
---

# Onboarding Status

Read runtime pipeline state, shared projections, and worker status files. Report:

- active clients
- current stage by client
- blocked steps and owner
- pending operator approvals
- drift items
- next scheduled or delegated action

If shared projections disagree with runtime pipeline state, mark the conflict as
drift and ask `agency-state-reconciliation` to classify it.
