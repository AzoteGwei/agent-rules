# Azote's Preferred Agent Coding Rules

An agent skill that encodes a set of implementation rules for coding agents:

- **Small CLs, enforced.** Every commit must weigh at most 300 Weight Lines,
  measured by `scripts/cl_weight.py`.
- **Independent review before every commit.** A reviewer subagent with its own
  context checks the implementation, the documentation, and rejects redundant
  or unrelated tests.
- **jj before git.** When a repository has a `.jj` directory and `jj` is
  installed, only jj is used.
- **Signed commits.** An unsigned commit, or a signing failure, stops the agent
  until the User explains what happened.
- **The User's identity only.** Commits use the configured author and
  committer as they are, carry no agent attribution (co-author trailers,
  "generated with" lines, URLs), and a missing identity stops the agent.
- **No unapproved pushes.** Commits stay local unless the User explicitly
  allows pushing.
- **Google-style CL descriptions**, and language rules for reports, comments
  and docs.

The rules themselves live in [`SKILL.md`](SKILL.md).

## Install

Clone the repository straight into your agent's skill directory:

```sh
# Claude Code
git clone https://github.com/AzoteGwei/agent-rules ~/.claude/skills/agent-rules
# Agents that load skills from ~/.agents/skills
git clone https://github.com/AzoteGwei/agent-rules ~/.agents/skills/agent-rules
```

Or clone it anywhere and link it there (`-n` replaces an existing link
instead of creating a new link inside it):

```sh
git clone https://github.com/AzoteGwei/agent-rules
mkdir -p ~/.claude/skills  # or ~/.agents/skills, likewise below
ln -sfn "$PWD/agent-rules" ~/.claude/skills/agent-rules
```

`scripts/cl_weight.py` needs Python 3.11 or newer and no third-party packages.

## License

BSD 2-Clause, see [`LICENSE`](LICENSE), except for the notice below.

## Notice

[`references/eng-practices/`](references/eng-practices/) is a copy of
[google/eng-practices](https://github.com/google/eng-practices) at commit
`3bb3ec25b3b0199f4940b1aa75f0ac5c5753301c`, by Google, licensed under the
[Creative Commons Attribution 3.0 Unported License](https://creativecommons.org/licenses/by/3.0/)
(see [`references/eng-practices/LICENSE`](references/eng-practices/LICENSE)).
It is not covered by the BSD 2-Clause License. Only the site build files
(`_config.yml`, `.github/`) were left out; the text is unchanged.
