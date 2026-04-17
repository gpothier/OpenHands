# Cherry-Pick Tracking for `my` Branch

This file tracks which branches should be cherry-picked into the `my` branch when syncing with upstream.

## Remotes

This repo is a fork. The remotes are:
- `origin` - the fork (gpothier/OpenHands)
- `upstream` - the original repo (OpenHands/OpenHands)

If `upstream` is not configured, add it:
```bash
git remote add upstream https://github.com/OpenHands/OpenHands.git
```

## Sync Workflow

When upstream `main` has new commits, follow this workflow:

```bash
# 1. Fetch from UPSTREAM (not origin!) and sync main
git fetch upstream
git checkout main
git pull upstream main
git push origin main  # Update fork's main to match upstream

# 2. Check if any INCLUDE branches were merged upstream
#    For each branch, check: git log upstream/main --oneline | grep -i "<branch-description>"
#    If merged, tell user to delete the branch and remove from this file

# 3. Rebase all feature/fix branches onto updated main
#    (resolve conflicts in each branch as needed)
for branch in <list from INCLUDE section>; do
    git checkout "$branch" && git rebase main || echo "CONFLICT in $branch - resolve manually"
done

# 4. Create temporary branch from fresh main
git checkout main
git checkout -b mytmp

# 5. Merge each branch listed in INCLUDE section
for branch in <list from INCLUDE section>; do
    git merge "$branch" -m "Merge $branch into mytmp"
done

# 6. Copy this tracking file from old 'my' to 'mytmp'
git checkout my -- CHERRY_PICK.md
git commit --amend --no-edit  # include the file in the last merge, or commit separately

# 7. Verify everything works, then promote mytmp to my
git branch -D my
git branch -m mytmp my
git push origin my --force-with-lease
```

## Branches to Cherry-Pick (INCLUDE)

These branches will be merged into `my` during sync:

- feature/sandbox-type-ui-selection
- feature/local-vscode-ssh-squashed-extra-env
- fix/chat-input-typing-lag
- fix/sticky-conversation-header

## Branches to Skip (EXCLUDE)

These branches exist but should NOT be cherry-picked:

- feature/vscode-proxy  # Already included in feature/sandbox-type-ui-selection
- feature/local-vscode-ssh  # Original branch, replaced by feature/local-vscode-ssh-squashed-extra-env
- feature/local-vscode-ssh-squashed  # Replaced by feature/local-vscode-ssh-squashed-extra-env (uses extra_env)
- feature/sandbox-sysbox  # Sysbox is too unstable
- feature/sandbox-docker-socket-passthrough  # Highly insecure
- auto-workspace-dir  # Now implemented in feature/rootless-docker-per-sandbox
- feature/rootless-docker-per-sandbox  # Superseded by Firecracker

## Branches Merged Upstream

These branches were merged upstream and can be deleted:

- fix/llm-base-url-fallback  # Merged as PR #13880

## Notes

- When a branch is merged upstream:
  1. Move it to "Branches Merged Upstream" with the PR number
  2. Tell the user to delete the branch (don't delete it yourself)
- When creating a new feature branch, add it to the appropriate list
- To disable a branch temporarily, move it from INCLUDE to EXCLUDE with a comment
