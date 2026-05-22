"""Carveout enforcement for the autonomous loop.

The loop must never edit paths that define what 'applied' means (the
streak verifier), what counts as a failure (the reporting taxonomy),
Nida's identity (profile, resume, credentials), or the loop's own
machinery (which would let it grade itself).
"""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath


# Directory prefixes (POSIX-form). Any path beneath these is immutable.
_IMMUTABLE_DIR_PREFIXES: tuple[str, ...] = (
    "src/applypilot/loop/",
    "tests/",
    ".git/",
)


# Exact file paths (POSIX-form).
_IMMUTABLE_FILES: frozenset[str] = frozenset({
    "profile.json",
    "resume.pdf",
    "resume.txt",
    ".env",
    "CLAUDE.md",
    "CONTEXT.md",
    "docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md",
})


# Functions that must never be edited even if their containing file is editable.
IMMUTABLE_FUNCTIONS: frozenset[str] = frozenset({
    "_verify_submission_success",
    "_classify_failure",
})


BRICK_THRESHOLD_SECONDS = 10.0


def _normalize(path: str | PurePosixPath) -> str:
    """Return POSIX-form path string, no leading './'."""
    s = str(path).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def is_immutable_path(path: str | PurePosixPath) -> bool:
    """True if the loop must not edit this path."""
    s = _normalize(path)
    if s in _IMMUTABLE_FILES:
        return True
    for prefix in _IMMUTABLE_DIR_PREFIXES:
        if s.startswith(prefix):
            return True
    return False


def touches_immutable_functions(
    file_path: Path | str,
    immutable: set[str] | frozenset[str] = IMMUTABLE_FUNCTIONS,
) -> bool:
    """Return True if file_path contains a definition of an immutable function.

    Conservative: if the file cannot be parsed (e.g. syntax error mid-edit),
    returns True so the patch is rejected rather than risk silent corruption.
    """
    p = Path(file_path)
    if not p.exists():
        return False
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in immutable:
                return True
    return False


def brick_detected(returncode: int, elapsed_s: float) -> bool:
    """True if a subprocess failed faster than any real apply could finish.

    Used by runner.apply_one to distinguish 'my last patch broke the
    codebase' from 'this specific job failed legitimately'. Zero exit code
    is never a brick, regardless of how fast it returned.
    """
    if returncode == 0:
        return False
    return elapsed_s <= BRICK_THRESHOLD_SECONDS
