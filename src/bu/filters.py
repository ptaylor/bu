"""Filter-list helpers shared by the backends.

Include files list literal paths relative to a source root (one per line,
``#`` comments and blank lines ignored).  Exclude files keep the rsync
glob format (see ``translate_exclude_line`` in the duplicity backend).
"""

from __future__ import annotations

from pathlib import Path


def read_filter_lines(path: Path) -> list[str]:
    """Return non-blank, non-comment lines from a filter file (missing file → [])."""
    if not path.is_file():
        return []
    lines: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def build_include_rules(paths: list[str]) -> list[str]:
    """Translate literal include paths into rsync ``--include`` patterns.

    ``paths`` are relative to a source root.  Each path expands to the
    parent-dir chain plus the item itself (``+ /a/``, ``+ /a/b/``,
    ``+ /a/b/c``, ``+ /a/b/c/**``) so nested files and directories are
    still reached through the trailing ``- *`` exclusion.  A path of
    ``.`` means the whole source — returns an empty list (no narrowing).
    """
    if any(p in (".", "/") for p in paths):
        return []
    rules: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = raw.strip("/")
        if not path:
            continue
        parts = path.split("/")
        cur = ""
        for part in parts[:-1]:
            cur = f"{cur}/{part}" if cur else part
            rule = f"+ /{cur}/"
            if rule not in seen:
                seen.add(rule)
                rules.append(rule)
        for suffix in ("", "/**"):
            rule = f"+ /{path}{suffix}"
            if rule not in seen:
                seen.add(rule)
                rules.append(rule)
    return rules
