# Immutable Platform Packages

## Purpose

Platform packages let an administrator publish content-addressed runtime files
once and let agent templates select them without gaining control over Docker
mount configuration.

## Publish flow

1. An administrator creates a tar.gz archive and computes its SHA-256 digest.
2. `POST /api/admin/platform-packages` receives `package_id`, `sha256`, and the
   base64 archive.
3. The backend decodes and hashes the exact archive bytes, safely extracts only
   bounded regular files/directories, and rejects any digest mismatch or unsafe
   member.
4. The backend copies the extracted tree into a newly-created Docker named
   volume and records immutable metadata under the platform data directory.
5. Repeating the exact publish is idempotent. Reusing a package ID with a
   different digest returns a conflict.

If the deterministic Docker volume already exists but no immutable registry
record exists, publication fails closed. Trinity does not adopt or relabel that
volume because its content did not pass the current publish transaction. An
administrator must investigate and remove the orphan before retrying.

Example request (placeholders only):

```json
{
  "package_id": "policy-bundle",
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "archive": "H4sI..."
}
```

## Template selection and agent creation

```yaml
platform_packages:
  - package_id: policy-bundle
    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

The template can provide only the ID and exact digest. During create (including
deploy-local), the backend resolves the immutable registry record and adds a
read-only named-volume mount at
`/opt/trinity/platform-packages/policy-bundle`. The response reports the
resolved ID, digest, and destination, but not the volume name.

## Start, readiness, and recreation

Before start, Trinity compares the container's package labels and mounts with
the registry. A missing registration, digest mismatch, unexpected destination,
or writable mount forces a recreation or fails closed. Recreation discards old
package mount attributes and reconstructs them from registry metadata. The
resolved package set is preserved in a platform-owned container label.

## Security boundaries

- Agent templates cannot request paths, volume names, modes, sources, or moving
  revisions.
- Package mounts never use `shared_folders`; Docker receives `mode: ro`.
- Archives reject traversal, links, devices, FIFOs, sockets, excessive size,
  and excessive member counts.
- An undeclared package has no mount in the agent container.
- Publishing is administrator-only. Agent create/deploy callers can only select
  previously registered immutable content.
