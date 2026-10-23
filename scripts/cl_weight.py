#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-2-Clause
"""Measure the Weight Lines (WL) of a pending change and enforce the Small CL limit.

The change is the git staging area against HEAD, or the jj working-copy commit @
against its parent @-. Every added or deleted line is weighted by its kind:

    code 1.0, config 0.5, test 0.5, comment 0.3, doc 0.2, generated 0, blank 0

A deleted file, and a file change without line hunks (rename, binary, mode or
empty file), counts as one line of the file's kind.

Comment-only lines are recognised in Python, JavaScript, TypeScript, Rust, Go,
C and C++ (elsewhere they weigh as the file's kind). Python string statements
such as docstrings count as comments, and Rust `#[cfg(test)] mod` blocks in
source files weigh as test.

A repository can correct file categories in `.azote-cl.toml` at its root. Only
a `[paths]` table is accepted; each key (generated, test, config, doc, code)
holds globs matched against the repository-relative path with fnmatch, where
`*` also matches `/`. Overrides are tried in that order before the built-in
rules. The limit and the weights cannot be configured.

    [paths]
    generated = ["third_party/*"]

Exit status: 0 within the limit (or allowed), 1 over the limit, 2 on error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

LIMIT = 300
# Weights in tenths of a line, so totals stay exact.
WEIGHTS = {
    "code": 10,
    "config": 5,
    "test": 5,
    "comment": 3,
    "doc": 2,
    "generated": 0,
    "blank": 0,
}
# File categories, in the order both overrides and built-in rules are tried.
CATEGORIES = ("generated", "test", "config", "doc", "code")
CONFIG_FILE = ".azote-cl.toml"
GIT_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

GENERATED_NAMES = frozenset({
    "Cargo.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
    "yarn.lock", "bun.lock", "bun.lockb", "go.sum", "uv.lock", "poetry.lock",
    "Pipfile.lock", "pdm.lock", "composer.lock", "Gemfile.lock", "flake.lock",
})
GENERATED_GLOBS = (
    "*.pb.go", "*_pb2.py", "*_pb2.pyi", "*_pb2_grpc.py", "*.pb.h", "*.pb.cc",
    "*.min.js", "*.min.css",
)
GENERATED_MARKER = re.compile(r"@generated|Code generated .* DO NOT EDIT")
TEST_DIRS = frozenset({"test", "tests", "__tests__", "testdata", "testing"})
TEST_GLOBS = (
    "test_*.py", "*_test.py", "conftest.py", "*_test.go", "*_test.c",
    "*_test.cc", "*_test.cpp", "*_unittest.cc", "*.test.*", "*.spec.*",
)
CONFIG_NAMES = frozenset({
    "Dockerfile", "Containerfile", "Makefile", "GNUmakefile", "CMakeLists.txt",
    "Justfile", "justfile", ".gitignore", ".gitattributes", ".editorconfig",
    ".dockerignore", ".npmrc", ".nvmrc", ".python-version",
})
CONFIG_GLOBS = (
    "*.toml", "*.yaml", "*.yml", "*.json", "*.jsonc", "*.json5", "*.ini",
    "*.cfg", "*.conf", "*.cmake", "*.mk", ".env*", "requirements*.txt",
)
DOC_GLOBS = (
    "*.md", "*.markdown", "*.rst", "*.txt", "*.adoc", "LICENSE*", "COPYING*",
    "NOTICE*", "AUTHORS*", "CHANGELOG*", "README*",
)

C_ESCAPE = re.compile(rb"\\([0-7]{3}|.)", re.S)
C_ESCAPES = {b"a": b"\a", b"b": b"\b", b"t": b"\t", b"n": b"\n", b"v": b"\v", b"f": b"\f",
             b"r": b"\r"}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
QUOTED_PAIR = re.compile(r'^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$')


class CLError(Exception):
    """A failure that ends the run with exit status 2."""


@dataclass
class FileDiff:
    old_path: str | None = None
    new_path: str | None = None
    added: list[tuple[int, str]] = field(default_factory=list)
    deleted: list[tuple[int, str]] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass
class Analysis:
    """Line numbers (1-based) of one file version that weigh differently."""

    comment: set[int] = field(default_factory=set)
    test: set[int] = field(default_factory=set)


@dataclass
class Report:
    path: str
    category: str
    added: int
    deleted: int
    kinds: Counter[str]

    @property
    def tenths(self) -> int:
        return sum(WEIGHTS[kind] * n for kind, n in self.kinds.items())


def _unquote(raw: str) -> str:
    """Decode a path that git wrapped in quotes with C-style escapes."""
    if len(raw) < 2 or not raw[0] == raw[-1] == '"':
        return raw
    # Octal escapes are UTF-8 bytes, so decode bytes rather than characters.
    data = C_ESCAPE.sub(lambda m: bytes([int(m[1], 8)]) if len(m[1]) == 3
                        else C_ESCAPES.get(m[1], m[1]), raw[1:-1].encode())
    return data.decode("utf-8", "replace")


def _strip_prefix(raw: str) -> str | None:
    path = _unquote(raw)
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def _header_paths(rest: str) -> tuple[str | None, str | None]:
    """Parse the `a/X b/Y` part of a `diff --git` line."""
    if '"' in rest and (m := QUOTED_PAIR.match(rest)):
        return _strip_prefix(m[1]), _strip_prefix(m[2])
    half = (len(rest) - 1) // 2
    if rest[half:half + 1] == " " and rest[2:half] == rest[half + 3:]:
        return rest[2:half], rest[2:half]
    old, _, new = rest.partition(" b/")
    return _strip_prefix(old), new


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a git-format diff (as printed by `git diff` and `jj diff --git`)."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    old_ln = new_ln = old_left = new_left = 0
    for line in text.split("\n"):
        if cur is not None and (old_left > 0 or new_left > 0):
            tag, body = line[:1], line[1:]
            if tag in ("-", " "):
                if tag == "-":
                    cur.deleted.append((old_ln, body))
                old_ln, old_left = old_ln + 1, old_left - 1
            if tag in ("+", " "):
                if tag == "+":
                    cur.added.append((new_ln, body))
                new_ln, new_left = new_ln + 1, new_left - 1
            continue
        if line.startswith("diff --git "):
            cur = FileDiff(*_header_paths(line[len("diff --git "):]))
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur.old_path = None
        elif line.startswith("deleted file mode"):
            cur.new_path = None
        elif line.startswith(("rename from ", "copy from ")):
            cur.old_path = _unquote(line.split(" ", 2)[2])
        elif line.startswith(("rename to ", "copy to ")):
            cur.new_path = _unquote(line.split(" ", 2)[2])
        elif line.startswith("--- "):
            cur.old_path = _strip_prefix(line[4:])
        elif line.startswith("+++ "):
            cur.new_path = _strip_prefix(line[4:])
        elif m := HUNK.match(line):
            old_ln, new_ln = int(m[1]), int(m[3])
            old_left = 1 if m[2] is None else int(m[2])
            new_left = 1 if m[4] is None else int(m[4])
    return files


def _matches(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def classify(path: str, head: str, overrides: dict[str, list[str]]) -> str:
    """Return the category of a file from its path and first lines."""
    for category in CATEGORIES:
        if _matches(path, overrides.get(category, [])):
            return category
    posix = PurePosixPath(path)
    name, dirs = posix.name, set(posix.parts[:-1])
    if name in GENERATED_NAMES or _matches(name, GENERATED_GLOBS) or GENERATED_MARKER.search(head):
        return "generated"
    if dirs & TEST_DIRS or _matches(name, TEST_GLOBS):
        return "test"
    if name in CONFIG_NAMES or _matches(name, CONFIG_GLOBS) or ".github" in dirs:
        return "config"
    if _matches(name, DOC_GLOBS) or dirs & {"doc", "docs"}:
        return "doc"
    return "code"


LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".mts": "js", ".cts": "js", ".tsx": "js",
    ".rs": "rust", ".go": "go",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".c++": "c",
    ".hh": "c", ".hpp": "c", ".hxx": "c", ".h++": "c", ".ipp": "c", ".inl": "c",
}
PY_STRING = re.compile(r"([rRbBuUfF]{0,2})('''|\"\"\"|'|\")")
RUST_RAW = re.compile(r'[bc]?r(#*)"')
RUST_CHAR = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^\\'\n])'")
C_RAW = re.compile(r'(?:u8|[uUL])?R"([^()\\\s]{0,16})\(')
C_STRING = re.compile(r"(?:u8|[uUL])?(['\"])")
RUST_TEST_MOD = re.compile(
    r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]\s*(?:#\s*\[[^\]]*\]\s*)*"
    r"(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+\w+\s*\{")


class _Scanner:
    """Find the lines of a source file that hold code and those that hold comments.

    Strings are tracked so comment markers inside them are not mistaken for
    comments. Python string statements (docstrings) count as comments. The
    `masked` copy of the text has comments and strings blanked out.
    """

    def __init__(self, text: str, lang: str) -> None:
        self.s, self.lang = text, lang
        self.i, self.line = 0, 1
        self.code: set[int] = set()
        self.comment: set[int] = set()
        self.masked = list(text)
        # Python state for telling docstrings from other strings.
        self.depth = 0
        self.continued = self.backslash = False
        self.pending: tuple[int, int] | None = None

    def run(self, in_template: bool = False) -> None:
        """Scan to the end, or to the `}` closing a JS template substitution."""
        braces = 0
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\n":
                self._newline()
            elif c in " \t\r\f\v":
                self.i += 1
            elif in_template and c == "}" and braces == 0:
                return
            elif not (self._comment() or self._string()):
                braces += {"{": 1, "}": -1}.get(c, 0)
                self._code(c)
        self._flush_pending("comment")

    def _consume(self, end: int, kind: str) -> None:
        """Consume s[i:end] as a "comment", "string" or (Python) "docstring" span."""
        end, first = min(end, len(self.s)), self.line
        for j in range(self.i, end):
            if self.s[j] == "\n":
                self.line += 1
            else:
                self.masked[j] = " "
        self.i = end
        if kind == "docstring":
            self.pending = (first, self.line)
        else:
            (self.comment if kind == "comment" else self.code).update(range(first, self.line + 1))

    def _flush_pending(self, kind: str) -> None:
        if self.pending:
            first, last = self.pending
            (self.comment if kind == "comment" else self.code).update(range(first, last + 1))
            self.pending = None

    def _newline(self) -> None:
        self._flush_pending("comment")
        self.continued, self.backslash = self.backslash, False
        self.line += 1
        self.i += 1

    def _code(self, c: str) -> None:
        self._flush_pending("code")
        self.code.add(self.line)
        self.backslash = c == "\\"
        if c in "([{":
            self.depth += 1
        elif c in ")]}":
            self.depth = max(0, self.depth - 1)
        self.i += 1

    def _comment(self) -> bool:
        s, i = self.s, self.i
        if s.startswith("#" if self.lang == "python" else "//", i):
            end = s.find("\n", i)
            self._consume(len(s) if end < 0 else end, "comment")
        elif self.lang != "python" and s.startswith("/*", i):
            self._consume(self._block_end(i + 2), "comment")
        else:
            return False
        return True

    def _block_end(self, j: int) -> int:
        depth = 1
        while depth:
            close = self.s.find("*/", j)
            if close < 0:
                return len(self.s)
            opened = self.s.find("/*", j, close) if self.lang == "rust" else -1
            if opened >= 0:
                depth, j = depth + 1, opened + 2
            else:
                depth, j = depth - 1, close + 2
        return j

    def _quoted_end(self, j: int, quote: str, multiline: bool) -> int:
        s = self.s
        while j < len(s):
            if s[j] == "\\":
                j += 2
            elif s.startswith(quote, j):
                return j + len(quote)
            elif s[j] == "\n" and not multiline:
                return j
            else:
                j += 1
        return len(s)

    def _string(self) -> bool:
        s, i, lang = self.s, self.i, self.lang
        after_word = i > 0 and (s[i - 1].isalnum() or s[i - 1] == "_")
        if lang == "python":
            m = PY_STRING.match(s, i)
            if not m or (m[1] and after_word):
                return False
            self._flush_pending("code")
            doc = self.line not in self.code and self.depth == 0 and not self.continued
            end = self._quoted_end(m.end(), m[2], multiline=len(m[2]) == 3)
            self.backslash = False
            self._consume(end, "docstring" if doc else "string")
            return True
        if lang == "js" and s[i] == "`":
            self._template()
            return True
        if lang == "go" and s[i] == "`":
            end = s.find("`", i + 1)
            end = len(s) if end < 0 else end + 1
        elif lang == "rust" and not after_word and (m := RUST_RAW.match(s, i)):
            close = '"' + m[1]
            end = s.find(close, m.end())
            end = len(s) if end < 0 else end + len(close)
        elif lang == "rust" and s[i] == "'":
            if not (m := RUST_CHAR.match(s, i)):
                return False  # a lifetime or loop label
            end = m.end()
        elif lang == "c" and not after_word and (m := C_RAW.match(s, i)):
            close = ")" + m[1] + '"'
            end = s.find(close, m.end())
            end = len(s) if end < 0 else end + len(close)
        elif lang == "c" and (m := C_STRING.match(s, i)):
            # A quote right after a word is a digit separator (1'000), not a char.
            if after_word and (m.end() - i > 1 or m[1] == "'"):
                return False
            end = self._quoted_end(m.end(), m[1], multiline=False)
        elif s[i] in "\"'":
            end = self._quoted_end(i + 1, s[i], multiline=lang == "rust" and s[i] == '"')
        else:
            return False
        self._consume(end, "string")
        return True

    def _template(self) -> None:
        """Consume a JS template literal, scanning `${...}` substitutions as code."""
        s, j = self.s, self.i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
            elif s[j] == "`":
                self._consume(j + 1, "string")
                return
            elif s.startswith("${", j):
                self._consume(j + 2, "string")
                self.run(in_template=True)
                j = self.i + 1
            else:
                j += 1
        self._consume(j, "string")


def _rust_test_lines(masked: str) -> set[int]:
    """Lines of `#[cfg(test)] mod name { ... }` blocks."""
    lines: set[int] = set()
    for m in RUST_TEST_MOD.finditer(masked):
        depth, end = 0, len(masked)
        for j in range(m.end() - 1, len(masked)):
            depth += {"{": 1, "}": -1}.get(masked[j], 0)
            if depth == 0:
                end = j
                break
        first = masked.count("\n", 0, m.start()) + 1
        lines.update(range(first, masked.count("\n", 0, end) + 2))
    return lines


def analyze(path: str, text: str) -> Analysis | None:
    """Find comment-only lines (and Rust inline test lines) of a source file."""
    lang = LANGUAGES.get(PurePosixPath(path).suffix.lower())
    if lang is None:
        return None
    scanner = _Scanner(text, lang)
    scanner.run()
    test = _rust_test_lines("".join(scanner.masked)) if lang == "rust" else set()
    return Analysis(comment=scanner.comment - scanner.code, test=test)


def _line_kind(category: str, body: str, lineno: int, analysis: Analysis | None) -> str:
    if category == "generated":
        return "generated"
    if not body.strip():
        return "blank"
    if analysis is not None and category in ("code", "test"):
        if lineno in analysis.comment:
            return "comment"
        if lineno in analysis.test:
            return "test"
    return category


def weigh(diff: FileDiff, category: str, old: Analysis | None = None,
          new: Analysis | None = None) -> Report:
    """Weigh one file's diff; `old` and `new` describe the two file versions."""
    if diff.new_path is None or not (diff.added or diff.deleted):
        kinds = Counter({category: 1})
    else:
        kinds = Counter(_line_kind(category, body, ln, old) for ln, body in diff.deleted)
        kinds.update(_line_kind(category, body, ln, new) for ln, body in diff.added)
    return Report(diff.path, category, len(diff.added), len(diff.deleted), kinds)


def _run(cmd: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True)
    except OSError as e:
        raise CLError(f"cannot run {cmd[0]}: {e}") from e
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise CLError(f"`{' '.join(cmd)}` failed: {stderr}")
    return proc.stdout.decode("utf-8", "replace")


class Git:
    name = "git"

    def __init__(self, cwd: Path) -> None:
        self.root = Path(_run(["git", "rev-parse", "--show-toplevel"], cwd).strip())
        head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"],
                              cwd=self.root, capture_output=True)
        self.base = "HEAD" if head.returncode == 0 else GIT_EMPTY_TREE
        self.label = "git: staged vs " + ("HEAD" if self.base == "HEAD" else "empty tree")

    def diff(self) -> str:
        return _run(["git", "-c", "core.quotepath=false", "diff", "--cached", "--no-color",
                     "--no-ext-diff", "--no-textconv", "--no-relative", "--src-prefix=a/",
                     "--dst-prefix=b/", "-U0", "-M", self.base, "--"], self.root)

    def show(self, side: str, path: str) -> str:
        spec = f"{self.base}:{path}" if side == "old" else f":{path}"
        return _run(["git", "cat-file", "blob", spec], self.root)


class Jj:
    name = "jj"
    label = "jj: @ vs @-"

    def __init__(self, cwd: Path) -> None:
        self.root = Path(_run(["jj", "root"], cwd).strip())

    def diff(self) -> str:
        return _run(["jj", "--no-pager", "--color=never", "diff", "-r", "@", "--git",
                     "--context", "0"], self.root)

    def show(self, side: str, path: str) -> str:
        quoted = path.replace("\\", "\\\\").replace('"', '\\"')
        return _run(["jj", "--no-pager", "--color=never", "--ignore-working-copy", "file",
                     "show", "-r", "@-" if side == "old" else "@", f'root-file:"{quoted}"'],
                    self.root)


def open_vcs(choice: str, cwd: Path) -> Git | Jj:
    """Pick jj when the repository has a .jj directory and jj is installed."""
    if choice == "auto":
        for directory in (cwd, *cwd.parents):
            if (directory / ".jj").is_dir():
                if shutil.which("jj"):
                    choice = "jj"
                    break
                if not (directory / ".git").exists():
                    raise CLError(f"{directory} is a jj repository without a colocated git "
                                  "repository, and jj is not installed")
                print("cl_weight: .jj found but jj is not installed; using git",
                      file=sys.stderr)
            if (directory / ".git").exists():
                choice = "git"
                break
        else:
            raise CLError("not inside a jj or git repository")
    return Jj(cwd) if choice == "jj" else Git(cwd)


def load_overrides(root: Path) -> dict[str, list[str]]:
    """Read the `[paths]` table of the repository's config file, if any."""
    config = root / CONFIG_FILE
    if not config.is_file():
        return {}
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise CLError(f"{CONFIG_FILE}: {e}") from e
    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        raise CLError(f"{CONFIG_FILE}: [paths] must be a table")
    unknown = (set(data) - {"paths"}) | (set(paths) - set(CATEGORIES))
    if unknown:
        raise CLError(f"{CONFIG_FILE}: unknown keys: {', '.join(sorted(unknown))}")
    for key, patterns in paths.items():
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise CLError(f"{CONFIG_FILE}: paths.{key} must be a list of glob strings")
    return paths


def measure(vcs: Git | Jj, overrides: dict[str, list[str]]) -> list[Report]:
    cache: dict[tuple[str, str], str] = {}

    def content(side: str, path: str) -> str:
        if (side, path) not in cache:
            try:
                cache[side, path] = vcs.show(side, path)
            except CLError:
                # Submodules and unreadable blobs have no text; only the
                # generated-marker check and comment detection lose input.
                cache[side, path] = ""
        return cache[side, path]

    reports = []
    for diff in parse_diff(vcs.diff()):
        side = "old" if diff.new_path is None else "new"
        head = "\n".join(content(side, diff.path).split("\n", 5)[:5])
        category = classify(diff.path, head, overrides)
        old = new = None
        # A deleted file counts as one line, so only kept files need analysis.
        if category in ("code", "test") and diff.new_path is not None:
            if diff.deleted and diff.old_path is not None:
                old = analyze(diff.old_path, content("old", diff.old_path))
            if diff.added:
                new = analyze(diff.new_path, content("new", diff.new_path))
        reports.append(weigh(diff, category, old, new))
    return reports


def render(label: str, reports: list[Report], allowed: str | None) -> str:
    total = sum(r.tenths for r in reports)
    lines = [f"Weight Lines ({label})", f"{'WL':>7} {'+':>5} {'-':>5}  {'kind':<9} path"]
    for r in sorted(reports, key=lambda r: (-r.tenths, r.path)):
        lines.append(f"{r.tenths / 10:7.1f} {r.added:5} {r.deleted:5}  {r.category:<9} {r.path}")
    kinds: Counter[str] = Counter()
    for r in reports:
        kinds.update(r.kinds)
    lines.append("Lines by kind: " + ", ".join(
        f"{kind} {kinds[kind]} x {WEIGHTS[kind] / 10:g}" for kind in WEIGHTS if kinds[kind]))
    if total <= LIMIT * 10:
        status = "OK"
    elif allowed:
        status = f"OVER LIMIT, allowed by the User: {allowed}"
    else:
        status = "OVER LIMIT: split this change into smaller CLs"
    lines.append(f"Total: {total / 10:.1f} / {LIMIT} WL  {status}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vcs", choices=("auto", "git", "jj"), default="auto")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--allow-large", metavar="REASON",
                        help="accept a change over the limit; only with the User's approval")
    args = parser.parse_args(argv)
    if args.allow_large is not None and not args.allow_large.strip():
        parser.error("--allow-large needs a non-empty reason")
    try:
        vcs = open_vcs(args.vcs, Path.cwd())
        reports = measure(vcs, load_overrides(vcs.root))
    except CLError as e:
        print(f"cl_weight: {e}", file=sys.stderr)
        return 2
    total = sum(r.tenths for r in reports)
    ok = total <= LIMIT * 10
    if args.json:
        print(json.dumps({
            "vcs": vcs.name,
            "limit": LIMIT,
            "total": total / 10,
            "ok": ok,
            "allowed_large": args.allow_large,
            "files": [{"path": r.path, "category": r.category, "added": r.added,
                       "deleted": r.deleted, "weight": r.tenths / 10, "kinds": r.kinds}
                      for r in reports],
        }, indent=2))
    else:
        print(render(vcs.label, reports, args.allow_large))
    return 0 if ok or args.allow_large else 1


if __name__ == "__main__":
    sys.exit(main())
