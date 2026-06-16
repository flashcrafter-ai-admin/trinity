# Agency Client Onboarding System

> **Requirement ID**: AGENCY-001
> **Priority**: HIGH
> **Status**: Implemented
> **Created**: 2026-06-16

## Overview

Provide a bundled Trinity-native multi-agent system that can onboard a new
agency client end to end using Trinity templates, system manifests, schedules,
shared folders, operator gates, tags/system views, and audit surfaces.

This is a general platform pattern. It must not import fc-agency business data,
Trello as core state, or the bespoke OpenClaw/agency-os runtime.

## Business Context

Agency onboarding requires parallel but coordinated work: intake, access grants,
client communications, ads, LSA, website/SEO, launch readiness, reporting, and
post-launch maintenance handoff. Trinity already owns the infrastructure needed
to run that work in production; this requirement packages it as a deployable
system and reusable template pattern.

## Requirements

### R1. Bundled Agent Templates

The repository MUST provide local templates for the complete onboarding roster:

- orchestrator
- client intake/onboarding
- client communications
- access readiness
- Google Ads onboarding
- LSA onboarding
- website/SEO onboarding
- Google Ads maintenance
- website/SEO maintenance
- state reconciliation
- reporting/QA

Each template MUST include `template.yaml`, non-empty `CLAUDE.md`, `.gitignore`,
and `.env.example` where external credentials may be needed.

### R2. Deployable System Manifest

The repository MUST provide a YAML system manifest that deploys the full fleet
with:

- parser-valid `local:` template references
- explicit permissions
- per-agent shared-folder expose/consume settings
- staggered schedules
- tags and a system view
- no unsupported manifest fields

### R3. Agent-Owned Workflow State

Agents MUST write runtime workflow state under `~/.trinity/pipeline-state/`.
Trinity core MUST NOT own workflow transitions for this system.

### R4. Shared File Contracts

Templates MUST document one-writer shared-folder contracts for client brief,
pipeline state projections, access matrix, communications drafts, domain-track
plans, QA reports, and reconciliation notes.

### R5. Operator Approval Gates

Templates MUST require approval before any external client send, spend/bidding
change, launch/publish action, admin/credential action, or ambiguous authority
resolution.

### R6. Evidence and Idempotency

Templates MUST require evidence for state changes and stable effect keys for
external side effects.

### R7. Spec Traceability

The implementation MUST be linked from `docs/memory/requirements.md` and
`docs/memory/feature-flows.md`.

## Security Considerations

- No real API keys, client names, private URLs, or PII may be committed.
- Credentials use Trinity's normal per-agent `.env` / `.mcp.json` injection.
- Templates must use placeholders and describe required credentials only.
- Approval gates are mandatory for critical-edge side effects.

## Testing Checklist

- Validate all new local templates with `is_trinity_compatible`.
- Parse and validate the system manifest with `parse_manifest` and
  `validate_manifest`.
- Run local template listing tests if practical:
  `python -m pytest tests/unit/test_local_templates_listing.py -v --tb=short`.
- Run system manifest tests if practical:
  `python -m pytest tests/test_systems.py -v --tb=short`.

## Related Documents

- `docs/memory/requirements.md` section 40
- `docs/memory/feature-flows/agency-client-onboarding-system.md`
- `docs/memory/feature-flows/system-manifest.md`
- `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`
- `docs/MULTI_AGENT_SYSTEM_GUIDE.md`

## Success Criteria

An operator can deploy the bundled manifest, provide credentials through Trinity,
start the schedules, and ask the orchestrator to onboard a new client. The agent
fleet can then gather intake, track access, draft client communications, prepare
ads/LSA/website work, request approvals, verify evidence, reconcile drift, and
produce a launch-readiness handoff without relying on fc-agency-specific runtime
state.
