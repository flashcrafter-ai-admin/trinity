# Safe Upgrade Persistent-State Backup

## Summary

Trinity upgrades must preserve the operator's work before changing code or containers. The durable state is not just the application database: agent runtime work lives in `agent-*-workspace` Docker volumes, backend runtime artifacts live in `/data`, and encrypted credentials are unrecoverable without the `.env` encryption key.

## State Model

| State | Location | Backup action |
|---|---|---|
| Platform database, SQLite | `/data/trinity.db` | SQLite online backup API, `PRAGMA integrity_check`, then verified archive |
| Platform database, PostgreSQL | Bundled `postgres` service or external DB from `DATABASE_URL` | Verified `pg_dump -Fc` for bundled service; digest-bound managed snapshot receipt for external DB |
| Credential encryption key and secrets | `.env` on host | Copy into chmod 600 `env.backup` |
| Skills/library runtime cache | backend `/data` | Archive `backend-data.tgz` |
| Agent runtime work | `agent-*-workspace` volumes mounted at `/home/developer` | Archive each volume to `agent-workspaces/*.tgz` |
| Redis | `trinity_redis-data` | Not authoritative; regenerate sessions/counters |
| Platform images | Docker image cache | Rebuild from source |

## Upgrade Flow

```mermaid
flowchart TD
  A["Operator requests upgrade"] --> B["Use same compose project name"]
  B --> C["Validate exact dev SHA and acquire deploy lock"]
  C --> D["Run backup-persistent-state.sh"]
  D --> E["Write manifest and backup bundle"]
  E --> F["Build platform images"]
  F --> G["docker compose up platform services"]
  G --> H["Backend /health passes"]
  H --> I["Running commit and worktree match target"]
```

## Invariants

- A routine upgrade never runs `docker compose down -v`.
- A routine upgrade never runs `docker volume rm`.
- A routine upgrade does not silently change the compose project name, because that creates a second set of compose volumes.
- Agent containers are not removed as part of a platform upgrade. If the agent base image changes, recreate only after a persistent-state backup exists.
- External PostgreSQL requires a nonempty managed-snapshot receipt bound to the active `DATABASE_URL` digest; a backup bundle without that evidence is incomplete and cannot authorize an upgrade.
- The upgrade path has no backup-bypass option. Only an explicitly confirmed first install with no existing project containers or volumes may proceed without a backup.
- A completed bundle must contain a nonempty environment backup, verified backend data, and exactly one authoritative database artifact or bound external snapshot receipt before any image build starts.
- Automated deploy credentials are least-privilege: Tailscale access is tag-scoped to a dedicated OpenSSH port, SSH host identity is pinned, and the deploy key is restricted to `deploy <40-character SHA>`.
- Concurrent deploys serialize through a host lock and are never cancelled mid-upgrade.

## Entry Points

- `scripts/deploy/backup-persistent-state.sh`
- `scripts/deploy/safe-upgrade.sh`
- `scripts/deploy/github-actions-safe-deploy.sh`
- `.github/workflows/deploy-dev.yml`
- `docs/user-docs/guides/deploying/upgrading.md`
- `docs/user-docs/guides/deploying/backup-and-restore.md`
