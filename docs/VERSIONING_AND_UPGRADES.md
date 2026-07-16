# Trinity Versioning and Upgrade Guide

## Versioning Philosophy

Trinity follows [Semantic Versioning](https://semver.org/) (SemVer):

```
MAJOR.MINOR.PATCH (e.g., 1.2.3)
```

| Version | When to Increment | Example |
|---------|-------------------|---------|
| **MAJOR** | Breaking changes requiring migration | Database schema changes, API incompatibilities |
| **MINOR** | New features, backward compatible | Gemini runtime support, new MCP tools |
| **PATCH** | Bug fixes, security patches | Fix port allocation bug, UI fixes |

## Current Version: 0.x (Pre-1.0)

During the `0.x` phase:
- API may change between minor versions
- Breaking changes documented in git history and PR descriptions
- Recommended: Pin to specific version tags

**Post-1.0**: Strict semver adherence with deprecation warnings before breaking changes.

---

## Component Versioning

Trinity consists of multiple components that version together:

| Component | Location | Versioning Strategy |
|-----------|----------|---------------------|
| **Backend** | `src/backend/` | Single version with platform |
| **Frontend** | `src/frontend/` | Single version with platform |
| **Base Image** | `docker/base-image/` | Single version with platform |
| **Agent Server** | `docker/base-image/agent_server/` | Single version with platform |
| **MCP Server** | `src/mcp-server/` | Single version with platform |

All components share the same version number for simplicity.

---

## Version Tagging Strategy

### Git Tags

```bash
# Release tags
v0.9.0          # Feature release
v0.9.1          # Patch release
v1.0.0          # Major release

# Pre-release tags
v1.0.0-alpha.1  # Alpha testing
v1.0.0-beta.1   # Beta testing
v1.0.0-rc.1     # Release candidate
```

### Docker Image Tags

```bash
# Production tags
trinity-agent-base:0.9.0      # Specific version (recommended)
trinity-agent-base:0.9        # Latest patch in minor version
trinity-agent-base:latest     # Latest release (not for production)

# Development tags
trinity-agent-base:main       # Latest main branch (CI/CD only)
trinity-agent-base:dev        # Development builds
```

---

## Upgrade Categories

### 1. Non-Breaking Updates (PATCH)

**Examples**: Bug fixes, documentation updates, UI tweaks

**Upgrade Process**:
```bash
./scripts/deploy/safe-upgrade.sh --project-name trinity
```

The wrapper resolves the current exact `HEAD`, rejects tracked, untracked,
ignored, or hidden-index drift, and builds only from a byte-verified Git archive.
For production, pass the original `--env-file` and Compose files. Activation uses
`--no-build` without removing volumes or the agent network.

**Downtime**: ~30 seconds (container restart)

### 2. Feature Updates (MINOR)

**Examples**: Gemini runtime support, new API endpoints

**Upgrade Process**:
```bash
./scripts/deploy/safe-upgrade.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  -f docker-compose.prod.yml
```

Rebuild and attest the agent base and leaf images as a separate governed release
when their Dockerfiles or runtime contracts change. A platform upgrade does not
silently recreate running agents.

**Downtime**: 2-5 minutes

**Impact on Running Agents**:
- Existing agents continue running (no rebuild needed)
- New features available only after agent recreation
- For Gemini support: Agents must be recreated with `runtime: gemini-cli`

### 3. Breaking Updates (MAJOR)

**Examples**: Database schema changes, API breaking changes

**Upgrade Process**:
```bash
# First create and verify a complete recovery bundle.
./scripts/deploy/backup-persistent-state.sh \
  --project-name trinity \
  --env-file /path/to/.env \
  --output-dir /srv/trinity-backups/persistent-state
```

Run a separately reviewed migration plan after the complete-backup gate. Use
`safe-upgrade.sh` for the actual build and activation. Never use `down -v` as a
migration shortcut, and recreate agents only when the reviewed image cutover
explicitly requires it.

**Downtime**: 10-30 minutes

---

## Gemini Runtime Upgrade (v0.9.0)

This is a **MINOR** version update. Here's the specific upgrade path:

### What Changed

| Component | Change Type | Impact |
|-----------|-------------|--------|
| Base Image | Modified | New agents need rebuild |
| Backend | Modified | Restart required |
| Frontend | No change | N/A |
| Database | No change | No migration needed |
| Config | Optional | Add `GOOGLE_API_KEY` if using Gemini |

### Upgrade Steps

```bash
# 1. Fetch and check out the reviewed release object
git fetch --tags origin
git checkout --detach v0.9.0

# 2. (Optional) Add Google API key for Gemini
echo "GOOGLE_API_KEY=your-key" >> .env

# 3. Rebuild base image
./scripts/deploy/build-base-image.sh

# 4. Restart backend
docker compose restart backend

# 5. Verify
curl http://localhost:8000/health
```

### Agent Migration

**Existing agents**: Continue working unchanged (use Claude Code)

**To use Gemini on existing agent**:
1. Note agent's template and configuration
2. Delete the agent
3. Recreate with `runtime: gemini-cli` in template

**New agents**: Can choose runtime at creation time

### Rollback

```bash
# If issues occur
git checkout v0.8.0  # Previous version
./scripts/deploy/build-base-image.sh
docker compose restart backend
```

---

## Recommended Upgrade Practices

### Pre-Upgrade Checklist

- [ ] Review git history for breaking changes (`git log --oneline`)
- [ ] Produce `backup_complete=yes` with verified database restore/snapshot evidence
- [ ] Verify backend `/data`, every Compose named volume, and every agent workspace archive
- [ ] Verify `.env` recovery keys, writer pause, stable inventories, and artifact digests
- [ ] Note running agents and active work before the drain window
- [ ] Schedule maintenance window if production

### Post-Upgrade Verification

```bash
# 1. Health check
curl http://localhost:8000/health

# 2. Test agent creation
# Create test agent via UI

# 3. Test chat
# Send message to test agent

# 4. Verify logs
docker compose logs backend --tail=50
```

### Rollback Plan

Always have a rollback plan:

```bash
# Quick rollback (config/code only)
git checkout <previous-tag>
docker compose up -d

# Full rollback (including data) follows the verified bundle runbook.
# See docs/user-docs/guides/deploying/backup-and-restore.md.
```

---

## Version History

| Version | Date | Type | Key Changes |
|---------|------|------|-------------|
| 0.9.0 | 2025-12-28 | MINOR | Gemini CLI runtime support |
| 0.8.x | 2025-12-24 | PATCH | Test suite fixes, bug fixes |
| 0.8.0 | 2025-12-23 | MINOR | First-time setup wizard, API keys management |

See `git log --oneline` for detailed history.

---

## Upgrade Provenance

`safe-upgrade.sh` authenticates from inside the running backend and requires
`/api/version` to return the exact full and short deployed Git SHA. Health alone,
an unauthenticated response, or `unknown` provenance is not release evidence.

---

## Questions?

If you encounter upgrade issues:
1. Check [Known Issues](KNOWN_ISSUES.md)
2. Review [Troubleshooting](onboarding/04-troubleshooting.md)
3. Open an issue on GitHub
