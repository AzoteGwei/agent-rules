---
name: azote-coding-rules
description: Mandatory rules for writing and committing code. Every commit is a Small CL of at most 300 Weight Lines (checked by scripts/cl_weight.py), reviewed by an independent reviewer subagent before it is committed, made with jj when the repository has a .jj directory (git otherwise), and signed. Covers Google-style CL descriptions and which language to use for reports, code comments and docs. Use whenever you change, split or commit code.
---

# Coding rules

These rules apply to every change you make in a repository. "The User" is the
person you work for. `<skill>` below is the directory that holds this file.

## Language

- Report to the User in the language the User used to start the conversation.
- Write code comments in English only.
- Write docs and CL descriptions in the language the repository (or the file
  being edited) already uses. If there is nothing to follow, ask the User.

## Version control: jj before git

Walk up from the working directory. If a `.jj` directory exists and the `jj`
command is installed, use **only jj**: do not run git commands at all (use
`jj git fetch` / `jj git push` for remotes). Use git only when there is no
`.jj` directory, or `jj` is not installed. Never use interactive commands
(editors, `-i`, `jj split` without paths). See
[`references/vcs.md`](references/vcs.md) for the commands.

The change being prepared is:

- jj: the working-copy commit `@`, compared with `@-`.
- git: the staged changes, compared with `HEAD`.

## Workflow for every commit

1. **Plan small CLs.** Split the task into a sequence of self-contained CLs
   that each do one thing and leave the build and tests passing. Read
   [`small-cls.md`](references/eng-practices/review/developer/small-cls.md)
   for how to split.
2. **Make one CL's change** (jj: in `@`; git: stage only this CL's files).
3. **Measure it.** From inside the repository run
   `python3 <skill>/scripts/cl_weight.py`. Exit status 1 means the CL is over
   300 Weight Lines: split it and do not commit. Exit status 2 is an error:
   fix the cause or ask the User.
   - jj: `jj split -m "<summary>" <paths>` moves the given paths into a new
     parent `@-` and keeps the rest in `@`. Run `jj edit @-` and take that
     first part through steps 3–5 as its own CL. Instead of `jj new` in
     step 6, run `jj next --edit`: the approved first part becomes `@-` and
     the rest is `@` again. Do step 7, then treat the rest as the next CL,
     with a new reviewer.
   - git: unstage the paths that belong to a later CL (`git restore --staged
     <paths>`).
4. **Write the CL description** (see below). jj: `jj describe -m "..."`.
5. **Get it reviewed** by an independent reviewer (see below) until it is
   approved.
6. **Commit** (before the first one, check the identity; see "Commit
   identity and attribution"). jj: `jj new` (the described `@` becomes the
   commit).
   git: `git commit -F <message file>`.
7. **Check the signature** (see below).

## Small CLs (mandatory)

Weight Lines (WL) count every added and deleted line by kind:

| Kind      | Weight | Examples                                          |
|-----------|--------|---------------------------------------------------|
| code      | 1.0    | source files, unknown files                       |
| config    | 0.5    | `*.toml`, `*.yaml`, `*.json`, `Dockerfile`        |
| test      | 0.5    | `tests/`, `test_*.py`, `*_test.go`, `*.test.ts`, Rust `#[cfg(test)] mod` |
| comment   | 0.3    | comment-only lines and docstrings in code and tests |
| doc       | 0.2    | `*.md`, `*.rst`, `*.txt`, `docs/`, `LICENSE`      |
| generated | 0      | lockfiles, `*.pb.go`, `*.min.js`, `@generated` files |

Blank lines weigh 0. A deleted file, and a file change without line hunks
(rename, binary, mode change), counts as one line. Comments are detected for
Python, JavaScript, TypeScript, Rust, Go, C and C++.

A repository can correct the file categories in `.azote-cl.toml` at its root
(globs match the path from the repository root; `*` also matches `/`):

```toml
[paths]
generated = ["third_party/*"]
test = ["e2e/*"]
config = []
doc = []
code = []
```

The limit and the weights cannot be changed.

**Escape hatch.** Pass `--allow-large "<reason>"` only after the User has
explicitly approved that specific large CL in the conversation, for example a
mechanical rename or vendored code. Never approve it yourself, and mention the
approval in the CL description.

## Independent review before every commit

Before each commit, start a **new** reviewer subagent with its own context: not
a fork of yours and not one that already reviewed another commit. Give it the
prompt in [`references/reviewer-prompt.md`](references/reviewer-prompt.md)
with the fields filled in: repository path, VCS, diff command, skill path, the
CL description and the `cl_weight.py` report.

The reviewer checks the implementation, the documentation and the tests, and
**rejects redundant or unrelated tests**. It answers `APPROVE` or
`REQUEST_CHANGES` with findings.

- On `REQUEST_CHANGES`: fix every blocking finding (and re-measure), then send
  the update to the **same** reviewer (in Claude Code, `SendMessage` to that
  agent), so it keeps the whole history of this commit. Do not start a new
  reviewer for the same commit.
- Do not argue findings away. If you believe a finding is wrong, explain why
  to the reviewer; if you still disagree, ask the User.
- After **3** rounds of `REQUEST_CHANGES` for the same commit, stop and ask
  the User how to proceed.
- Commit only after `APPROVE`. The next commit gets a new reviewer.

## CL descriptions

Follow
[`cl-descriptions.md`](references/eng-practices/review/developer/cl-descriptions.md):

- First line: a short, imperative summary that stands on its own
  ("Add …", "Fix …", "Remove …"), without a trailing period.
- A blank line, then a body that explains what changes and why: the problem,
  the approach, and anything the reviewer or a future reader should know
  (limitations, follow-up CLs, `--allow-large` approval).

## Commit identity and attribution

The User configures who authors and commits. Use that configuration as it is:

- Never set or override the identity: no `jj metaedit --author` or
  `--update-author`, no `git commit --author` or `--reset-author`, no
  `GIT_AUTHOR_*`, `GIT_COMMITTER_*`, `JJ_USER` or `JJ_EMAIL` environment
  variables, and no edits to identity config. Pass no `git -c`,
  `jj --config` or `jj --config-file` overrides to any command that creates
  or rewrites commits.
- Before the first commit, check that an identity is configured. jj:
  `jj config get user.name` and `jj config get user.email`. git:
  `git config user.name` and `git config user.email`. If either is missing or
  empty, **stop and ask the User**; never make one up.
- Commits carry no agent attribution: no `Co-authored-by:` or similar
  trailers (and no `git commit --signoff` or `--trailer`), no "Generated
  with ..." lines, no agent or tool names as credit or byline, no URLs
  pointing at an agent, unless the User explicitly asks for them.

## Signed commits

The User configures commit signing. Never disable or work around it: no
`--no-gpg-sign`, no `-c commit.gpgsign=false`, no edits to signing config.

After every commit, check that it is signed:

- jj: `jj log -r @- --no-graph -T 'if(signature, signature.status(), "unsigned")'`
  must print `good`.
- git: `git log -1 --format=%G?` must print `G` (or `U`, good but untrusted).

Rewriting history (`jj squash`, `jj describe` or `jj rebase` on earlier
commits, `git commit --amend`, `git rebase`) must re-sign every rewritten
commit; only rewrite history when the User asks, and check the signatures of
all rewritten commits afterwards.

If a commit fails because of signing, or any commit in the range you are
working on (jj: mutable commits; git: commits not yet pushed) shows as unsigned
or with a bad signature in `jj log` / `git log`, **stop and ask the User what
happened**. Do not retry with signing turned off and do not continue with
other commits.

## Stop and ask the User when

- a CL cannot be split below 300 WL and an `--allow-large` approval is needed;
- the reviewer has requested changes 3 times for the same commit, or you
  disagree with a finding;
- signing fails, or a commit is unsigned;
- the commit identity (`user.name` / `user.email`) is not configured;
- `cl_weight.py` exits with status 2 and the cause is not yours to fix;
- the docs language or the CL description language is unclear.
