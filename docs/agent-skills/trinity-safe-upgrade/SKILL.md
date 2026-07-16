---
name: trinity-safe-upgrade
description: Use when upgrading, deploying, rolling forward, pruning, or cleaning a running Trinity instance with real state. Preserves the database, backend /data, every Compose named volume, .env recovery keys, and agent workspaces before changing Docker services or worktrees.
---

# Trinity Safe Upgrade

Use this skill before changing a running Trinity instance that may contain work the operator cares about.

## Non-Negotiables

- Keep the same Docker Compose project name for the instance. Do not accidentally create a second set of volumes.
- Run a persistent-state backup before changing code, images, containers, or worktrees.
- Never run `docker compose down -v` or `docker volume rm` during a routine upgrade.
- Do not delete or recreate agent containers as a hidden side effect. Agent recreation is an explicit follow-up, and only after a backup exists.
- Treat external PostgreSQL as incomplete until a fresh Ed25519-signed snapshot receipt and a root-controlled provider verifier prove that exact snapshot exists.
- A governed deploy must byte-verify an extracted exact Git object, close every resolved Compose build input over that source, reject ignored/index-hidden drift, and activate with `docker compose up --no-build` from frozen runtime inputs.

## Standard Path

From the Trinity repo on the target host:

```bash
./scripts/deploy/safe-upgrade.sh --project-name trinity
```

For production with a host env file or override compose file, pass the same inputs that created the running stack:

```bash
./scripts/deploy/safe-upgrade.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  -f docker-compose.prod.yml \
  -f /path/to/host-override.yml
```

The wrapper resolves and freezes an exact clean Git commit, runs `scripts/deploy/backup-persistent-state.sh`, rebuilds platform services, starts the selected services without rebuilding, and waits for every selected service to become healthy or pass its explicit endpoint probe. It then authenticates from inside the backend before requiring `/api/version` to match the full and short deployed commit. The governed SSH path freezes source, compose inputs, and `.env` before build.

## Backup-Only Path

Use this before any destructive operation or manual migration:

```bash
./scripts/deploy/backup-persistent-state.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  --output-dir /srv/trinity-backups/persistent-state
```

Expected evidence:

- `manifest.txt` exists in the new backup bundle.
- `postgres.dump` exists and was restored with `pg_restore --exit-on-error` into a fresh temporary PostgreSQL instance when bundled PostgreSQL is authoritative.
- External PostgreSQL evidence is fresh, signature-valid, database-bound, and independently provider-verified.
- `backend-data.tgz` exactly restores the paused live `/data` tree.
- `platform-volumes/*.tgz` exactly restores every named volume mounted by the Compose project, including Redis AOF, agent configuration, and log volumes; its inventory must remain stable for the entire transaction.
- `agent-workspaces/*.tgz` exactly restores every discovered `agent-*-workspace` volume, and every `agent-*` container has its governed volume.
- Backend, scheduler, Redis, Vector, agents, and any bundled PostgreSQL writer remain paused while their physical state is archived.
- `env.backup` contains exactly one nonempty value for every required recovery/authentication key.
- `manifest.txt` ends with `backup_complete=yes`; any skip option makes the bundle partial and unusable for an upgrade.

## References

- Upgrade runbook: `docs/user-docs/guides/deploying/upgrading.md`
- Backup details: `docs/user-docs/guides/deploying/backup-and-restore.md`
- Requirements entry: `docs/memory/requirements/infrastructure.md`
- Feature flow: `docs/memory/feature-flows/safe-upgrade-persistent-state.md`
