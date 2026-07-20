# Agency Client Onboarding System

## Requirement

AGENCY-001 defines a Trinity-native agency operations system for end-to-end
client setup, service-track execution, maintenance, communications, and
incident response. It ports fc-agency's portable operating principles without
copying its bespoke runtime or multiplying workflow phases into standing
containers.

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
| `agency-orchestrator` | Routes work, manages gates, reconciles drift, and projects verified state | `shared-out/status.json`, routing packets, gate packets |
| `client-onboarding-agent` | Resolves paid-client identity, validates intake/access, prepares service-track handoff | client setup artifacts, handoff packets |
| `agency-ads` | Owns Google Ads and LSA onboarding plus maintenance workflows | Ads/LSA plans, dry-run evidence, maintenance scans |
| `agency-website-seo` | Owns website, landing page, SEO, GBP/reviews, and maintenance workflows | site/SEO plans, QA evidence, deploy readiness |
| `agency-comms` | Owns drafts, reply classification, approval packets, and approved sends | comms summaries, drafts, send evidence |
| `it-ai` | Owns factory runtime incidents and root-cause remediation | incident reports, fixes, validation evidence |

## Workflow

1. **Route** — orchestrator classifies the request and selects one owner.
2. **Onboard** — client onboarding resolves identity, validates intake/access,
   initializes durable client context, and creates track handoffs.
3. **Execute Tracks** — Ads and website/SEO agents run their own launch or
   maintenance workflows using OpenClaw skills and evidence gates.
4. **Communicate** — comms agent drafts, gates, sends, and classifies client
   communications when requested by any workflow.
5. **Reconcile and QA** — owning agents run QA; orchestrator runs
   reconciliation when surfaces disagree.
6. **Incident Response** — `it-ai` handles factory/runtime failures separately
   from client delivery.

## Shared File Contracts

The system manifest enables shared folders for every agent. Each template
documents its file ownership. Common contracts:

- Client setup artifacts are owned by `client-onboarding-agent`.
- Routing, gate, and reconciliation packets are owned by `agency-orchestrator`.
- Communication drafts and send evidence are owned by `agency-comms`.
- Ads and LSA artifacts are owned by `agency-ads`.
- Website, SEO, GBP/reviews, and deploy artifacts are owned by
  `agency-website-seo`.
- Incident reports and prevention artifacts are owned by `it-ai`.

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
