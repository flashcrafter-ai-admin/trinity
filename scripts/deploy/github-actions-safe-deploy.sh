#!/usr/bin/env bash
set -euo pipefail

log() {
    printf '[github-deploy] %s\n' "$*"
}

git_no_replace() {
    if [[ "${1:-}" != "-C" || -z "${2:-}" || ! -d "${2:-}" ]]; then
        log "Governed Git invocation requires an existing explicit worktree"
        return 64
    fi
    local worktree_path
    worktree_path=$(cd "$2" && pwd -P)
    shift 2
    env \
        -u GIT_DIR \
        -u GIT_WORK_TREE \
        -u GIT_INDEX_FILE \
        -u GIT_OBJECT_DIRECTORY \
        -u GIT_ALTERNATE_OBJECT_DIRECTORIES \
        -u GIT_COMMON_DIR \
        -u GIT_NAMESPACE \
        -u GIT_REPLACE_REF_BASE \
        -u GIT_GRAFT_FILE \
        -u GIT_SHALLOW_FILE \
        -u GIT_CONFIG \
        -u GIT_CONFIG_COUNT \
        -u GIT_CONFIG_PARAMETERS \
        -u GIT_CONFIG_GLOBAL \
        -u GIT_CONFIG_SYSTEM \
        -u GIT_EXEC_PATH \
        PATH=/usr/local/bin:/usr/bin:/bin \
        GIT_CONFIG_GLOBAL=/dev/null \
        GIT_CONFIG_NOSYSTEM=1 \
        GIT_CONFIG_COUNT=8 \
        GIT_CONFIG_KEY_0=core.fsmonitor \
        GIT_CONFIG_VALUE_0=false \
        GIT_CONFIG_KEY_1=core.untrackedcache \
        GIT_CONFIG_VALUE_1=false \
        GIT_CONFIG_KEY_2=core.ignorestat \
        GIT_CONFIG_VALUE_2=false \
        GIT_CONFIG_KEY_3=core.filemode \
        GIT_CONFIG_VALUE_3=true \
        GIT_CONFIG_KEY_4=core.precomposeunicode \
        GIT_CONFIG_VALUE_4=false \
        GIT_CONFIG_KEY_5=core.hooksPath \
        GIT_CONFIG_VALUE_5=/dev/null \
        GIT_CONFIG_KEY_6=credential.helper \
        GIT_CONFIG_VALUE_6= \
        GIT_CONFIG_KEY_7=credential.helper \
        GIT_CONFIG_VALUE_7='!gh auth git-credential' \
        GIT_LITERAL_PATHSPECS=1 \
        GIT_NO_REPLACE_OBJECTS=1 \
        GIT_WORK_TREE="$worktree_path" \
        git -C "$worktree_path" "$@"
}

assert_no_git_replacement_refs() {
    local worktree_path="$1"
    local replacement_refs worktree_redirect worktree_status=0
    worktree_redirect=$(git_no_replace -C "$worktree_path" \
        config --get-all core.worktree) || worktree_status=$?
    if [[ "$worktree_status" -gt 1 || -n "$worktree_redirect" ]]; then
        log "Deployment repository contains a local core.worktree redirect: $worktree_path"
        return 74
    fi
    replacement_refs=$(git_no_replace -C "$worktree_path" \
        for-each-ref --format='%(refname)' refs/replace)
    if [[ -n "$replacement_refs" ]]; then
        log "Deployment repository contains replacement refs: $worktree_path"
        return 74
    fi
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
    local actual_commit actual_tree target_tree drift index_flags
    assert_no_git_replacement_refs "$worktree_path" || return $?
    actual_commit=$(git_no_replace -C "$worktree_path" rev-parse HEAD)
    if [[ "$actual_commit" != "$target_commit" ]]; then
        log "Deployment worktree points to unexpected commit: $worktree_path"
        return 73
    fi
    drift=$(git_no_replace -C "$worktree_path" status \
        --porcelain=v1 --untracked-files=all --ignored=matching)
    index_flags=$(git_no_replace -C "$worktree_path" ls-files -v | sed -n '/^[a-zS] /p')
    if [[ -n "$drift" ]] \
        || [[ -n "$index_flags" ]] \
        || ! git_no_replace -C "$worktree_path" diff --quiet -- \
        || ! git_no_replace -C "$worktree_path" diff --cached --quiet --; then
        log "Deployment worktree contains tracked, untracked, ignored, or index-hidden drift: $worktree_path"
        return 74
    fi
    actual_tree=$(git_no_replace -C "$worktree_path" write-tree)
    target_tree=$(git_no_replace -C "$worktree_path" rev-parse "${target_commit}^{tree}")
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
    local external_snapshot_receipt="${TRINITY_EXTERNAL_DB_SNAPSHOT_RECEIPT:-}"
    local external_snapshot_public_key="${TRINITY_EXTERNAL_DB_SNAPSHOT_PUBLIC_KEY:-}"
    local external_snapshot_verifier="${TRINITY_EXTERNAL_DB_SNAPSHOT_VERIFY_COMMAND:-}"
    local lock_file="${TRINITY_DEPLOY_LOCK_FILE:-$deploy_root/.trinity-deploy.lock}"
    local lock_timeout="${TRINITY_DEPLOY_LOCK_TIMEOUT_SECONDS:-2100}"

    test -d "$TRINITY_PRIMARY_DIR/.git"
    test -f "$env_file"
    test -w "$deploy_root"
    assert_no_git_replacement_refs "$TRINITY_PRIMARY_DIR"

    mkdir -p "$(dirname "$lock_file")"
    exec 9>"$lock_file"
    if ! flock -w "$lock_timeout" 9; then
        log "Timed out waiting for deployment lock: $lock_file"
        exit 75
    fi

    log "Fetching $remote_name/dev"
    git_no_replace -C "$TRINITY_PRIMARY_DIR" fetch --prune "$remote_name" dev

    local remote_commit
    remote_commit=$(git_no_replace -C "$TRINITY_PRIMARY_DIR" rev-parse "refs/remotes/$remote_name/dev")
    if [[ "$remote_commit" != "$target_commit" ]]; then
        log "Refusing stale deployment: requested $target_commit but $remote_name/dev is $remote_commit"
        exit 65
    fi

    if [[ -e "$deploy_dir" ]]; then
        local existing_commit
        existing_commit=$(git_no_replace -C "$deploy_dir" rev-parse HEAD)
        if [[ "$existing_commit" != "$target_commit" ]]; then
            log "Existing deployment path points to unexpected commit: $deploy_dir"
            exit 73
        fi
    else
        log "Creating immutable deployment worktree: $deploy_dir"
        git_no_replace -C "$TRINITY_PRIMARY_DIR" worktree add --detach "$deploy_dir" "$target_commit"
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
        export TRINITY_EXPECTED_SOURCE_REVISION="$target_commit"
        local upgrade_args=(
            --project-name "$project_name"
            --env-file "$env_file"
            --backup-dir "$backup_dir"
            "${compose_args[@]}"
        )
        if [[ -n "$external_snapshot_receipt" ]]; then
            upgrade_args+=(--external-db-snapshot-receipt "$external_snapshot_receipt")
        fi
        if [[ -n "$external_snapshot_public_key" ]]; then
            upgrade_args+=(--external-db-snapshot-public-key "$external_snapshot_public_key")
        fi
        if [[ -n "$external_snapshot_verifier" ]]; then
            upgrade_args+=(--external-db-snapshot-verify-command "$external_snapshot_verifier")
        fi
        ./scripts/deploy/safe-upgrade.sh "${upgrade_args[@]}"
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
        if [[ -z "$(git_no_replace -C "$worktree_path" status --porcelain)" ]]; then
            git_no_replace -C "$TRINITY_PRIMARY_DIR" worktree remove "$worktree_path"
        else
            log "Preserving dirty deployment worktree: $worktree_path"
        fi
    done < <(git_no_replace -C "$TRINITY_PRIMARY_DIR" worktree list --porcelain | sed -n 's/^worktree //p')

    log "Deployment verified: $target_commit"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
