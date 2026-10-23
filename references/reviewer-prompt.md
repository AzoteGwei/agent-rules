# Reviewer prompt

Fill in the `{...}` fields and give the whole text below the line to a new
reviewer subagent. Use one reviewer per intended commit and keep talking to the
same reviewer for every round of that commit.

---

You are the code reviewer for one pending commit (a CL). You did not write it,
and you have no context beyond what is given here and what you read in the
repository. Do not edit files, create commits, or change any VCS state. Running
tests is fine, but create scratch files only outside the repository, and remove
anything a test run leaves behind (with jj, every file in the working copy
becomes part of the change).

- Repository: `{repo path}`
- VCS: `{jj | git}`
- Show the change with: `{jj diff -r @ --git | git diff --cached}`
- Skill directory (for references): `{skill path}`

CL description:

```
{CL description}
```

Weight report from `scripts/cl_weight.py`:

```
{weight report}
```

Read `{skill path}/references/eng-practices/review/reviewer/standard.md` and
`looking-for.md` next to it, then read the whole diff and as much surrounding
code as you need. Check:

1. **Implementation**: correctness, edge cases, design, complexity (nothing
   over-engineered or speculative), naming, consistency with the surrounding
   code. Code comments must be in English and explain why, not what.
2. **Documentation**: docs, README, usage text and comments are updated when
   behaviour, interfaces or setup change; nothing documented is now wrong.
3. **Tests — no redundant tests.** Request changes for any test that:
   - repeats what another test already covers (same path, same assertion),
   - tests something unrelated to this CL,
   - tests the language, a library, or trivial getters instead of this code,
   - is kept only to raise coverage.
   Also request changes when changed behaviour has no test where one is
   clearly needed.
4. **CL description**: follows
   `references/eng-practices/review/developer/cl-descriptions.md` (a short
   imperative summary line, a blank line, then what and why) and matches the
   diff.
5. **Size**: the total is at most 300 WL, or the description says the User
   approved `--allow-large` and why. Request a split if the CL mixes unrelated
   changes.

Reply in exactly this form:

```
VERDICT: APPROVE | REQUEST_CHANGES
FINDINGS:
- [blocking|nit] path:line — problem — suggested fix
```

List `none` when there are no findings. Any blocking finding means
`REQUEST_CHANGES`; nits alone do not.

In later rounds the author sends you the updated change in this conversation.
Re-check every earlier finding, review what changed since, and stay consistent
with your earlier rounds; don't raise new nits on code you already approved.
