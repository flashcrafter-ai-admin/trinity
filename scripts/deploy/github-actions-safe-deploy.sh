#!/usr/bin/env bash
set -euo pipefail

log() {
    printf '[github-deploy] %s\n' "$*"
}

parse_target_commit() {
    local command="${1:-}"
    if [[ "$command" =~ ^deploy[[:space:]]+([0-9a-f]{40})$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    return 64
}

assert_exact_clean_worktree() {
    local worktree_path="$1"
    local target_commit="$2"
    local actual_commit actual_tree target_tree drift
    actual_commit=$(git -C "$worktree_path" rev-parse HEAD)
    if [[ "$actual_commit" != "$target_commit" ]]; then
        log "Deployment worktree points to unexpected commit: $worktree_path"
        return 73
    fi
    drift=$(git -C "$worktree_path" status --porcelain=v1 --untracked-files=all)
    if [[ -n "$drift" ]] \
        || ! git -C "$worktree_path" diff --quiet -- \
        || ! git -C "$worktree_path" diff --cached --quiet --; then
        log "Deployment worktree contains tracked or untracked drift: $worktree_path"
        return 74
    fi
    actual_tree=$(git -C "$worktree_path" write-tree)
    target_tree=$(git -C "$worktree_path" rev-parse "${target_commit}^{tree}")
    if [[ "$actual_tree" != "$target_tree" ]]; then
        log "Deployment worktree tree does not match requested commit: $worktree_path"
        return 74
    fi
}

main() {
    local target_commit
    if ! target_commit=$(parse_target_commit "${SSH_ORIGINAL_COMMAND:-}"); then
        log "Rejected command. Expected: deploy <40-character lowercase commit SHA>"
        exit 64
    fi

    : "${TRINITY_PRIMARY_DIR:?TRINITY_PRIMARY_DIR must point to the persistent host checkout}"

    local remote_name="${TRINITY_DEPLOY_REMOTE:-flashcrafter}"
    local project_name="${TRINITY_COMPOSE_PROJECT:-trinity}"
    local deploy_root="${TRINITY_DEPLOY_ROOT:-${TRINITY_PRIMARY_DIR%/*}}"
    local deploy_prefix="${TRINITY_DEPLOY_PREFIX:-trinity-deploy}"
    local short_commit="${target_commit:0:8}"
    local deploy_dir="$deploy_root/$deploy_prefix-$short_commit"
    local env_file="${TRINITY_ENV_FILE:-$TRINITY_PRIMARY_DIR/.env}"
    local backup_dir="${TRINITY_BACKUP_DIR:-$TRINITY_PRIMARY_DIR/backups/persistent-state}"
    local compose_override="${TRINITY_COMPOSE_OVERRIDE:-}"
    local lock_file="${TRINITY_DEPLOY_LOCK_FILE:-$deploy_root/.trinity-deploy.lock}"
    local lock_timeout="${TRINITY_DEPLOY_LOCK_TIMEOUT_SECONDS:-2100}"

    test -d "$TRINITY_PRIMARY_DIR/.git"
    test -f "$env_file"
    test -w "$deploy_root"

    mkdir -p "$(dirname "$lock_file")"
    exec 9>"$lock_file"
    if ! flock -w "$lock_timeout" 9; then
        log "Timed out waiting for deployment lock: $lock_file"
        exit 75
    fi

    log "Fetching $remote_name/dev"
    git -C "$TRINITY_PRIMARY_DIR" fetch --prune "$remote_name" dev

    local remote_commit
    remote_commit=$(git -C "$TRINITY_PRIMARY_DIR" rev-parse "refs/remotes/$remote_name/dev")
    if [[ "$remote_commit" != "$target_commit" ]]; then
        log "Refusing stale deployment: requested $target_commit but $remote_name/dev is $remote_commit"
        exit 65
    fi

    if [[ -e "$deploy_dir" ]]; then
        local existing_commit
        existing_commit=$(git -C "$deploy_dir" rev-parse HEAD)
        if [[ "$existing_commit" != "$target_commit" ]]; then
            log "Existing deployment path points to unexpected commit: $deploy_dir"
            exit 73
        fi
    else
        log "Creating immutable deployment worktree: $deploy_dir"
        git -C "$TRINITY_PRIMARY_DIR" worktree add --detach "$deploy_dir" "$target_commit"
    fi
    assert_exact_clean_worktree "$deploy_dir" "$target_commit"

    local compose_args=(-f docker-compose.prod.yml)
    if [[ -n "$compose_override" ]]; then
        test -f "$compose_override"
        compose_args+=(-f "$compose_override")
    fi

    log "Running backup-first safe upgrade for $target_commit"
    (
        cd "$deploy_dir"
        ./scripts/deploy/safe-upgrade.sh \
            --project-name "$project_name" \
            --env-file "$env_file" \
            --backup-dir "$backup_dir" \
            "${compose_args[@]}"
    )

    local running_commit working_dir
    running_commit=$(docker inspect trinity-backend --format '{{range .Config.Env}}{{println .}}{{end}}' \
        | sed -n 's/^GIT_COMMIT=//p')
    working_dir=$(docker inspect trinity-backend \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')

    if [[ "$running_commit" != "$target_commit" || "$working_dir" != "$deploy_dir" ]]; then
        log "Runtime verification failed: commit=$running_commit working_dir=$working_dir"
        exit 70
    fi

    log "Pruning superseded clean deployment worktrees"
    while IFS= read -r worktree_path; do
        case "$worktree_path" in
            "$deploy_root/$deploy_prefix-"*) ;;
            *) continue ;;
        esac
        [[ "$worktree_path" != "$deploy_dir" ]] || continue
        if [[ -z "$(git -C "$worktree_path" status --porcelain)" ]]; then
            git -C "$TRINITY_PRIMARY_DIR" worktree remove "$worktree_path"
        else
            log "Preserving dirty deployment worktree: $worktree_path"
        fi
    done < <(git -C "$TRINITY_PRIMARY_DIR" worktree list --porcelain | sed -n 's/^worktree //p')

    log "Deployment verified: $target_commit"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
