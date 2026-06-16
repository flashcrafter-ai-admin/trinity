# Agency Client Onboarding Demo

This demo deploys the AGENCY-001 multi-agent onboarding fleet from
`config/manifests/agency-client-onboarding.yaml`.

## Deploy

```bash
curl -sS -X POST http://localhost:8000/api/systems/deploy \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/x-yaml" \
  --data-binary @config/manifests/agency-client-onboarding.yaml
```

For an API dry run, submit the same manifest through `POST /api/systems/deploy`
with `dry_run: true` in the JSON wrapper used by the UI/MCP route.

## Agent Roster

The manifest creates these final agent names:

- `agency-client-onboarding-orchestrator`
- `agency-client-onboarding-intake`
- `agency-client-onboarding-comms`
- `agency-client-onboarding-access`
- `agency-client-onboarding-ads-onboarding`
- `agency-client-onboarding-lsa-onboarding`
- `agency-client-onboarding-website-seo-onboarding`
- `agency-client-onboarding-ads-maintenance`
- `agency-client-onboarding-website-seo-maintenance`
- `agency-client-onboarding-state-reconciliation`
- `agency-client-onboarding-reporting-qa`

## Credentials

Inject credentials through Trinity per-agent configuration. Do not commit live
credentials into templates or manifests. Use the `.env.example` files in each
template as the operator checklist for optional integrations.

Typical integrations include Google Ads, Local Services Ads, Google Business
Profile, analytics/search console access, website/CMS access, and client
communication channels.

## Start Onboarding

Ask the orchestrator to start a client:

```text
/onboard-client client_slug=<client-slug> services=<ads,lsa,website-seo> launch_target=<date-or-none>
```

The orchestrator should delegate to intake, access, communications, domain
onboarding, reconciliation, and reporting/QA agents. It must request operator
approval for outbound client sends, launch/publish actions, spend-affecting
changes, access changes, and ambiguous authority conflicts.

## State Model

Runtime workflow state belongs under:

```text
~/.trinity/pipeline-state/client-onboarding/<client-slug>.json
```

Shared folders are projections and handoffs. Each template documents its
one-writer file contracts in `CLAUDE.md`.
