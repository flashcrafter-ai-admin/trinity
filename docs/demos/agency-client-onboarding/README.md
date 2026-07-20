# FlashCrafter Agency Ops Demo

This demo deploys the simplified FlashCrafter agency operations fleet from
`config/manifests/flashcrafter-agency-ops.yaml`.

## Deploy

```bash
curl -sS -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/manifests/flashcrafter-agency-ops.yaml
```

For an API dry run, submit the same manifest through `POST /api/systems/deploy`
with `dry_run: true` in the JSON wrapper used by the UI/MCP route.

## Agent Roster

The manifest creates these final agent names:

- `flashcrafter-agency-ops-orchestrator`
- `flashcrafter-agency-ops-client-onboarding`
- `flashcrafter-agency-ops-ads`
- `flashcrafter-agency-ops-website-seo`
- `flashcrafter-agency-ops-comms`
- `flashcrafter-agency-ops-it-ai`

## Credentials

Inject credentials through Trinity per-agent configuration. Do not commit live
credentials into templates or manifests. Use the `.env.example` files in each
template as the operator checklist for optional integrations.

Typical integrations include Google Ads, Local Services Ads, Google Business
Profile, analytics/search console access, website/CMS access, and client
communication channels.

## Start Onboarding

Ask the onboarding agent to start a client, or ask the orchestrator to route the request:

```text
/onboard-client client_slug=<client-slug> services=<ads,lsa,website-seo> launch_target=<date-or-none>
```

The orchestrator routes to onboarding, Ads, website/SEO, comms, or IT. Intake,
access tracking, QA, reporting, and reconciliation are workflows/skills inside
the owning agents, not separate standing agents. The system must request
operator approval for outbound client sends, launch/publish actions,
spend-affecting changes, access changes, and ambiguous authority conflicts.

## State Model

Runtime workflow state belongs under:

```text
~/.trinity/pipeline-state/client-onboarding/<client-slug>.json
```

Shared folders are projections and handoffs. Each template documents its
one-writer file contracts in `CLAUDE.md`.
