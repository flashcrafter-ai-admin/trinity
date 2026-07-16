# Upgrading Trinity

Apply code changes safely to a running Trinity instance. The procedure below keeps agent containers running throughout the upgrade — only the platform services are restarted.

## When to Run This

Run this procedure for every reviewed release that includes changes to platform code (backend, frontend, MCP server, scheduler). Skip it for documentation-only changes.

---

## Pre-flight Checklist

Before touching anything:

- [ ] **Back up persistent state** (Step 1 below — do this first, always)
- [ ] Confirm at least 2 GB disk free: `df -h /`
- [ ] Note the current Git SHA: `git rev-parse HEAD`
- [ ] Check that no critical agent tasks are running: `docker ps --filter "label=trinity.platform=agent"`
- [ ] Confirm Docker is running: `docker info >/dev/null 2>&1`

---

## Recommended Procedure

Use the safe upgrade wrapper when operating a real instance:

```bash
./scripts/deploy/safe-upgrade.sh --project-name trinity
```

For production with an env file or host-specific override:

```bash
./scripts/deploy/safe-upgrade.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  -f docker-compose.prod.yml \
  -f /path/to/host-override.yml
```

The wrapper runs `backup-persistent-state.sh`, rebuilds platform images, starts the platform services with the same compose project and `--no-build`, waits for backend health, and authenticates inside the backend before requiring `/api/version` to match the exact deployed commit. The governed SSH deploy path rejects tracked, untracked, ignored, and index-hidden drift; byte-verifies the extracted Git tree; and rejects Compose build inputs outside that frozen source. Agent containers are not deleted. Every Compose named volume and `agent-*-workspace` volume is verified and preserved first.

Use the manual procedure below only when you need to perform the same steps by hand.

---

## Manual Procedure

### Step 1: Back Up Persistent State

The database is critical, but it is not the only state that matters. Back up the database, backend `/data`, every Compose named volume, `.env`, and agent workspace volume before every upgrade:

```bash
./scripts/deploy/backup-persistent-state.sh --project-name trinity
```

Production example:

```bash
./scripts/deploy/backup-persistent-state.sh \
  --project-name trinity \
  --output-dir /srv/trinity-backups/persistent-state
```

The backup bundle is created under `backups/persistent-state/` by default. It is intentionally gitignored and chmod 700 because `env.backup` contains secrets when `.env` exists.

Do not use `cp` or `tar` against a live SQLite file. The governed backup uses SQLite's online backup API, verifies integrity and schema/table counts against the paused source, and selects PostgreSQL authority from the running backend's exact `DATABASE_URL`.

### Step 2: Check Out the Reviewed Release

```bash
git fetch origin <reviewed-commit-sha>
git checkout --detach <reviewed-commit-sha>
```

Review what changed:

```bash
git log --oneline -10
git diff HEAD~1 HEAD --stat
```

If `docker/base-image/Dockerfile` appears in the diff, see [Step 5: Base Image Upgrade](#step-5-base-image-upgrade-if-needed) below.

### Step 3: Rebuild Platform Services

When updating Trinity code, rebuild the platform images only:

```bash
docker compose build --no-cache backend frontend mcp-server scheduler
```

The `trinity-agent-base` image is **not** rebuilt by this command. It changes much less often, and rebuilding it forces every agent to be re-deployed. Rebuild it only when `docker/base-image/Dockerfile` itself changes, via `./scripts/deploy/build-base-image.sh`.

For production:

```bash
docker compose -f docker-compose.prod.yml build --no-cache backend frontend mcp-server scheduler
```

### Step 4: Restart Platform Services

> **Use `docker compose restart` or `safe-upgrade.sh`, not `down -v`.** `docker compose down` removes the `trinity-agent-network`, which orphans every running agent container — they keep running but lose their network and have to be removed and recreated. `docker compose down -v` is destructive: it removes the compose-managed data volumes. The only times to use `down` are: (1) intentional full teardown, (2) recovering from a corrupted compose state.

```bash
# Development
docker compose restart backend frontend mcp-server scheduler

# Production
docker compose -f docker-compose.prod.yml restart backend frontend mcp-server scheduler
```

Services restart in parallel. The backend typically takes 10–20 seconds to become healthy.

### Step 5: Verify

Run the six-probe verification list:

```bash
# 1. Backend
curl -s http://localhost:8000/health
# Expected: {"status":"healthy",...}

# 2. Scheduler
curl -s http://localhost:8001/health
# Expected: {"status":"healthy","active_schedules":N}

# 3. Frontend (HTTP 200)
curl -s -o /dev/null -w '%{http_code}' http://localhost
# Expected: 200

# 4. Redis
docker exec trinity-redis redis-cli ping
# Expected: PONG

# 5. MCP Server
curl -s http://localhost:8080/health
# Expected: 200 OK

# 6. Vector (log aggregation)
docker exec trinity-vector wget -q -O - http://localhost:8686/health
# Expected: non-empty response
```

All six probes must pass before you declare the upgrade complete.

**Confirm the new version is live.** After the probes pass, check that the backend is actually running the build you just deployed:

`safe-upgrade.sh` obtains an admin token from `/token` inside the backend container and then verifies the authenticated `/api/version` response. Both `git_commit` and `git_commit_short` must match the requested full SHA; `"unknown"`, an unauthenticated response, or a merely healthy backend fails the release.

**Note:** JWT tokens are invalidated when the backend restarts. Users with active web UI sessions will need to log in again. MCP clients (Claude Code) will need to reconnect — run `/mcp` in your Claude Code session or restart the client.

---

## Step 5: Base Image Upgrade (if needed)

Rebuild the base image only when `docker/base-image/Dockerfile` changes:

```bash
./scripts/deploy/build-base-image.sh
```

After the base image is rebuilt, existing agent containers continue using the old image until they are individually stopped and recreated. There is no automatic roll-forward — agents pick up the new base image the next time they are (re)created.

---

## Rollback

If something goes wrong after the upgrade:

First identify the backup bundle created immediately before the upgrade:

```bash
ls -td backups/persistent-state/* | head -1
```

### 1. Stop platform services

```bash
# Development
docker compose stop backend frontend mcp-server scheduler

# Production
docker compose -f docker-compose.prod.yml stop backend frontend mcp-server scheduler
```

### 2. Restore the database backup

```bash
# Development (named volume)
docker run --rm \
  -v trinity_trinity-data:/data \
  -v ~/backups:/backup \
  alpine cp /backup/trinity-<timestamp>.db /data/trinity.db

# Production (bind mount — adjust path)
cp ~/backups/trinity-<timestamp>.db /srv/trinity-data/trinity.db
```

### 3. Check out the previous version

```bash
git checkout <previous-sha>
# or
git checkout <previous-tag>
```

### 4. Rebuild and restart

```bash
docker compose build --no-cache backend frontend mcp-server scheduler
docker compose restart backend frontend mcp-server scheduler
```

### 5. Run the six-probe verification to confirm rollback succeeded.

---

## See Also

- [Backup and Restore](backup-and-restore.md) — Detailed backup procedures
- [Monitoring](monitoring.md) — Six-probe health check and recovery patterns
- [Single-Server Deployment](single-server.md) — Initial setup reference
