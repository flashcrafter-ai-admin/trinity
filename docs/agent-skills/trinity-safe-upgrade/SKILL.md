---
name: trinity-safe-upgrade
description: Use when upgrading, deploying, rolling forward, pruning, or cleaning a running Trinity instance with real state. Ensures agents preserve database data, backend /data, .env encryption keys, and agent workspace volumes before changing Docker services or worktrees.
---

# Trinity Safe Upgrade

Use this skill before changing a running Trinity instance that may contain work the operator cares about.

## Non-Negotiables

- Keep the same Docker Compose project name for the instance. Do not accidentally create a second set of volumes.
- Run a persistent-state backup before changing code, images, containers, or worktrees.
- Never run `docker compose down -v` or `docker volume rm` during a routine upgrade.
- Do not delete or recreate agent containers as a hidden side effect. Agent recreation is an explicit follow-up, and only after a backup exists.
- Treat external PostgreSQL as incomplete until a fresh Ed25519-signed snapshot receipt and a root-controlled provider verifier prove that exact snapshot exists.
- A governed deploy must use the exact requested Git object as its build context, reject ignored/index-hidden drift, and activate with `docker compose up --no-build`.

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

The wrapper runs `scripts/deploy/backup-persistent-state.sh`, rebuilds platform services, starts the selected services without rebuilding, waits for backend health, and requires `/api/version` to match the exact deployed commit. The governed SSH path freezes source, compose inputs, and `.env` before build.

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
- `agent-workspaces/*.tgz` exactly restores every discovered `agent-*-workspace` volume, and every `agent-*` container has its governed volume.
- `env.backup` contains exactly one nonempty value for every required recovery/authentication key.
- `manifest.txt` ends with `backup_complete=yes`; any skip option makes the bundle partial and unusable for an upgrade.

## References

- Upgrade runbook: `docs/user-docs/guides/deploying/upgrading.md`
- Backup details: `docs/user-docs/guides/deploying/backup-and-restore.md`
- Requirements entry: `docs/memory/requirements/infrastructure.md`
- Feature flow: `docs/memory/feature-flows/safe-upgrade-persistent-state.md`
