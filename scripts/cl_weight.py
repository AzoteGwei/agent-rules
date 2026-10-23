# SPDX-License-Identifier: BSD-2-Clause
"""Measure the Weight Lines (WL) of a pending change and enforce the Small CL limit.

The change is the git staging area against HEAD, or the jj working-copy commit @
against its parent @-. Every added or deleted line is weighted by its kind:

    code 1.0, config 0.5, test 0.5, comment 0.3, doc 0.2, generated 0, blank 0

A deleted file, and a file change without line hunks (rename, binary, mode or
empty file), counts as one line of the file's kind.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath

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
