# VCS commands

Non-interactive commands for each step of the workflow in `SKILL.md`. Pass
messages with `-m` (or a file) so no editor opens.

## jj (when `.jj` exists and `jj` is installed)

| Step                     | Command |
|--------------------------|---------|
| Status of `@`            | `jj st` |
| Diff for the reviewer    | `jj diff -r @ --git` |
| Measure                  | `python3 <skill>/scripts/cl_weight.py` |
| Describe the CL          | `jj describe -m "<summary>" -m "<body>"` |
| Check the identity       | `jj config get user.name` and `jj config get user.email` |
| Commit (finish `@`)      | `jj new` |
| Check the last signature | `jj log -r @- --no-graph -T 'if(signature, signature.status(), "unsigned")'` |
| Check mutable commits    | `jj log -r 'mutable()' -T 'change_id.short() ++ " " ++ if(signature, signature.status(), "unsigned") ++ "\n"' --no-graph` |
| Split `@`                | `jj split -m "<description of the first part>" <paths>` (the given paths go to a new parent `@-`, the rest stays in `@`) |
| Work on the first part   | `jj edit @-` (measure, describe and review it as its own CL; do not run `jj new` here) |
| Finish it, back to rest  | `jj next --edit` (replaces `jj new`; then check `@-`'s signature) |
| Push                     | `jj git push` |

Notes:

- jj snapshots the working copy on every command, so every file you write is
  part of `@`. Keep scratch files outside the repository.
- `jj split` without paths opens an interactive editor; always pass paths.
- Do not run `git` commands in a jj repository, not even read-only ones.

## git (no `.jj`, or `jj` not installed)

| Step                     | Command |
|--------------------------|---------|
| Status                   | `git status` |
| Stage this CL only       | `git add <paths>` |
| Diff for the reviewer    | `git diff --cached` |
| Measure                  | `python3 <skill>/scripts/cl_weight.py` |
| Check the identity       | `git config user.name` and `git config user.email` |
| Commit                   | `git commit -F <message file>` |
| Check the last signature | `git log -1 --format=%G?` (`G` or `U` is signed) |
| Check unpushed commits   | `git log --format='%h %G? %s' @{upstream}..HEAD` (no upstream: `<base>..HEAD`, or `-n <number of your commits>`) |

Notes:

- `%G?` letters: `G` good, `U` good but untrusted, `N` no signature, `B` bad,
  `X`/`Y` expired signature/key, `R` revoked key, `E` cannot be checked.
  Anything other than `G` or `U` means stop and ask the User.
- Never use `git add -i`, `git add -p`, `git rebase -i` or `git commit`
  without a message.
