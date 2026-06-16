---
description: Normalize client onboarding inputs
allowed-tools: Read, Write, Glob, Bash
---

# Intake

Normalize client identity, contacts, locations, agreement scope, service scope,
launch timing, billing assumptions, and missing inputs.

Write `/home/developer/shared-out/client/client-brief.json` with:

- `schema_version`
- `client_slug`
- `status`
- `contacts`
- `locations`
- `services`
- `missing_inputs`
- `evidence`
- `next_action`

Do not invent missing facts. Mark them as blockers or questions for
communications.
