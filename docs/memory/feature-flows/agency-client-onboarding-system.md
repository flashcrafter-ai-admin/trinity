# Agency Client Onboarding System

## Requirement

AGENCY-001 defines a Trinity-native multi-agent system for end-to-end agency
client onboarding. It ports fc-agency's portable operating principles without
copying its bespoke runtime or domain-specific state surfaces.

## Design Principles

1. **Authority maps before automation** — every artifact has one owner and
   named readers. Shared files are projections or handoffs, not casual shared
   databases.
2. **Agents own workflows** — each agent advances its own domain work and
   writes runtime pipeline state. Trinity deploys, schedules, observes, and
   gates; it does not become a DAG engine.
3. **Humans are gates, not drivers** — routine work runs autonomously. Humans
   approve critical-edge actions, resolve drift, and provide feedback.
4. **Evidence is mandatory** — no stage advances without evidence, actor,
   source, timestamp, and next action.
5. **Side effects are idempotent** — outbound sends, access changes, launches,
   and spend-affecting changes carry stable effect keys and precondition checks.
6. **Projection drift is loud** — if state surfaces disagree, agents surface the
   conflict instead of silently overwriting one side.

## Agent Roster

| Agent | Responsibility | Primary Outputs |
|---|---|---|
| `agency-orchestrator` | Owns onboarding pipeline, assigns stages, reconciles drift, raises gates | `shared-out/pipeline/`, `shared-out/directives/`, `shared-out/status.json` |
| `agency-intake` | Normalizes the client brief, service scope, contacts, missing inputs | `shared-out/client/client-brief.json`, `shared-out/evidence/` |
| `agency-comms` | Drafts client updates and checks inbound communication context | `shared-out/drafts/`, `shared-out/comms/status.json` |
| `agency-access` | Tracks access grants and admin-critical prerequisites | `shared-out/access/access-matrix.json` |
| `agency-ads-onboarding` | Prepares Google Ads launch readiness, tracking, dry-run plans, and approval gates | `shared-out/ads/` |
| `agency-lsa-onboarding` | Prepares Local Services Ads eligibility, verification, budget readiness, and approval gates | `shared-out/lsa/` |
| `agency-website-seo-onboarding` | Prepares website, landing pages, tracking, local SEO, GBP/reviews evidence, QA, and deployment gates | `shared-out/website-seo/` |
| `agency-ads-maintenance` | Runs post-launch read-only Google Ads and LSA monitoring and drafts gated optimizations | `shared-out/ads-maintenance/` |
| `agency-website-seo-maintenance` | Runs post-launch website, local SEO, GBP/reviews, content, and tracking maintenance | `shared-out/website-seo-maintenance/` |
| `agency-state-reconciliation` | Detects drift between pipeline state, shared projections, external evidence, and client artifacts | `shared-out/reconciliation/` |
| `agency-reporting-qa` | Verifies evidence, compiles status/readiness reports, audits gates | `shared-out/reports/`, `shared-out/qa/` |

## Workflow

1. **Intake** — normalize client identity, agreement/service scope, contacts,
   locations, channels, and missing information.
2. **Access Readiness** — determine required grants, record links/instructions,
   and gate any admin-level access action.
3. **Track Planning** — ads, local-presence, website/SEO agents produce
   evidence-backed plans and blockers.
4. **Implementation Readiness** — agents prepare draft actions and exact
   operator approvals for any external send, launch, spend, or admin change.
5. **QA and Launch Readiness** — reporting/QA verifies artifacts, identifies
   drift, and compiles the launch/readiness report.
6. **Client Handoff** — orchestrator summarizes state, open gates, evidence,
   and next autonomous schedules.

## Shared File Contracts

The system manifest enables shared folders for every agent. Each template
documents its file ownership. Common contracts:

- `client/client-brief.json` — owner: `agency-intake`; readers: all agents.
- `pipeline/onboarding-state.json` — owner: `agency-orchestrator`; readers:
  all agents.
- `directives/current.json` — owner: `agency-orchestrator`; readers: all agents.
- `access/access-matrix.json` — owner: `agency-access`; readers:
  orchestrator, comms, reporting/QA, domain-track agents.
- `drafts/*.json` — owner: `agency-comms`; readers: orchestrator,
  reporting/QA.
- `ads/*.json`, `lsa/*.json`, `website-seo/*.json` — owned by the
  corresponding onboarding agent; readers: orchestrator and reporting/QA.
- `ads-maintenance/*.json` and `website-seo-maintenance/*.json` — owned by
  the corresponding maintenance agent; readers: orchestrator and reporting/QA.
- `reports/*.md` and `qa/*.json` — owner: `agency-reporting-qa`; readers: all
  agents.

Files include `schema_version`, `client_slug`, `updated_at`, `writer`,
`evidence[]`, `status`, and `next_action` where applicable.

JSON artifacts owned by an agent are updated structurally, not with brittle
line-context patches. The owning agent loads the current JSON object, updates
fields by key, and writes the full object with stable formatting. Missing files
are created from the documented schema; invalid JSON is moved aside with a
timestamped `.invalid` suffix and recorded as drift before a fresh projection is
written.

## Runtime Pipeline State

Agents use template-owned pipeline definitions as the durable workflow contract
and write runtime state under:

```text
~/.trinity/pipeline-state/client-onboarding/<client-slug>.json
```

The state document keeps:

- `pipeline_id`
- `instance_id`
- `client_slug`
- `status`
- `current_stage`
- `steps[]` with `status`, `owner`, `evidence`, `operator_queue_item_id`
- `events[]` append-only activity notes
- `correlation_id`
- `updated_at`

## Operator Gates

An agent must request approval before:

- sending SMS/email or posting to external client channels
- changing campaign budgets, bidding, ads, domains, DNS, hosting, or billing
- granting/revoking admin access
- launching or publishing client-visible surfaces
- resolving ambiguous source-of-truth drift

The approval request includes exact action text, target, effect key,
preconditions, rollback plan, evidence, and expiry.

## Validation

- Template directories must contain valid `template.yaml` and non-empty
  `CLAUDE.md`.
- The system manifest must parse with `services.system_service.parse_manifest`
  and pass `validate_manifest`.
- No real credentials, internal URLs, client names, or PII are committed.
