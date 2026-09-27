"""Filter-list helpers shared by the backends.

Include files list literal paths relative to a source root (one per line,
``#`` comments and blank lines ignored).  Exclude files keep the rsync
glob format (see ``translate_exclude_line`` in the duplicity backend).
"""

from __future__ import annotations

import re
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


def marker_filename(name: str) -> str:
    """Name of the restore-test marker file for destination ``name``.

    One file per destination, so two destinations sharing a source directory
    never overwrite each other's markers.
    """
    return f"backup-status-{name}.txt"


def marker_include_rules(filename: str) -> list[str]:
    """rsync ``--include`` rule that keeps a source-root marker in the backup.

    A source narrowed by an include list is closed off with a trailing
    ``--exclude=*`` (see ``build_include_rules``), so a marker file sitting at
    the source root would be skipped without ever appearing in anybody's
    include file.  Emitted ahead of the include-list rules, this anchored rule
    lets exactly that one file through.
    """
    return [f"+ /{filename}"]


def last_entry(text: str) -> str:
    """Return the last non-blank line of marker content (``""`` when empty)."""
    for line in reversed(text.splitlines()):
        if stripped := line.strip():
            return stripped
    return ""


def _glob_fragment(pattern: str) -> str:
    """Translate one slash-free rsync glob into a regex fragment."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = i + 1
            if end < len(pattern) and pattern[end] in "!^":
                end += 1
            if end < len(pattern) and pattern[end] == "]":
                end += 1
            while end < len(pattern) and pattern[end] != "]":
                end += 1
            if end >= len(pattern):
                out.append(re.escape(char))
            else:
                body = pattern[i + 1:end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def _path_regex(pattern: str) -> str:
    """Translate an rsync glob path (which may contain ``/``) into a regex."""
    segments = pattern.split("/")
    out: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            out.append(".*" if last else "(?:.*/)?")
            continue
        out.append(_glob_fragment(segment) + ("" if last else "/"))
    return "".join(out)


def pattern_matches(pattern: str, rel_path: str) -> bool:
    """Approximate rsync's match of one exclusion pattern against a path.

    A leading ``/`` — or any ``/`` inside the pattern — anchors it to the
    source root; a bare pattern matches at any depth.  A pattern matching a
    directory also covers everything inside it.  Only used to *warn* about
    filters that would hide a restore-test marker, so it stays close to
    rsync's rules without attempting a full wildmatch implementation.
    """
    text = pattern.strip()
    if not text or text.startswith("#"):
        return False
    if text[:2] in ("+ ", "- "):
        text = text[2:].strip()
    if not text:
        return False
    rel = rel_path.strip("/")
    if not rel:
        return False
    anchored = text.startswith("/") or "/" in text.rstrip("/")
    regex = re.compile(_path_regex(text.lstrip("/")) + r"(?:/.*)?")
    candidates = [rel] if anchored else rel.split("/")
    return any(regex.fullmatch(candidate) for candidate in candidates)


def restore_test_warnings(
    *,
    marker_file: str,
    rel_dir: str,
    include_files: list[str] | None = None,
    exclude_files: list[str] | None = None,
) -> list[str]:
    """Warn when a restore-test marker cannot reach the backup through filters.

    Two ways a marker goes silently missing: a source that narrows itself with
    an include list which does not cover the directory, and an exclusion
    pattern matching the directory or the marker file (exclusions are
    consulted first, so they win over the marker's own include rule).
    """
    warnings: list[str] = []
    rel = rel_dir.strip("/")
    marker_rel = f"{rel}/{marker_file}" if rel else marker_file

    lines: list[str] = []
    for path in include_files or []:
        lines.extend(read_filter_lines(Path(path)))
    # A marker at the source root needs no include-list entry: bu lets the
    # marker filename itself through the narrowing rules.  Anything deeper has
    # to be covered by the list like any other path.
    if rel and lines and not any(ln in (".", "/") for ln in lines):
        covered = any(
            ln.strip("/") == rel or rel.startswith(ln.strip("/") + "/")
            for ln in lines
        )
        if not covered:
            warnings.append(
                f"restore-test path {marker_rel!r} is not covered by its source's "
                "include list — the marker would never be backed up"
            )

    for path in exclude_files or []:
        for pattern in read_filter_lines(Path(path)):
            if pattern_matches(pattern, rel) or pattern_matches(pattern, marker_rel):
                warnings.append(
                    f"exclusion pattern {pattern!r} in {path} matches restore-test "
                    f"path {marker_rel!r} — the marker would never be backed up"
                )
                break
    return warnings


def trailing_whitespace_warnings(paths: list[str]) -> list[str]:
    """Warn about exclusion-file patterns with trailing whitespace.

    rsync reads these files itself and keeps trailing spaces as part of
    the pattern, so a line like ``Pictures/Takeout `` silently never
    matches.  Missing files and blank/comment lines are ignored; one
    warning is returned per offending line.
    """
    warnings: list[str] = []
    for path in paths:
        p = Path(path).expanduser()
        if not p.is_file():
            continue
        for lineno, raw in enumerate(p.read_text(errors="replace").splitlines(), 1):
            stripped = raw.rstrip()
            if not stripped or stripped.startswith("#") or stripped == raw:
                continue
            warnings.append(
                f"exclusion pattern {stripped!r} in {p} (line {lineno}) has "
                "trailing whitespace — rsync keeps the space, so the pattern "
                "never matches"
            )
    return warnings
