"""Interactive 'bu create' wizard for generating destination configs.

Flow: backup type (DIR or B2) → destination (local directory for DIR, or
B2 bucket/path plus credentials for B2) → method (rsync/duplicity for DIR,
duplicity-only for B2) → source paths → duplicity-specific options →
exclusion files (global + per-name).
Every answer is validated and re-prompted until it is correct.

Choice menus are numbered and arrow-key scrollable when running on a TTY;
when piped, they fall back to plain numbered input.  On a TTY the wizard
uses ANSI colors, step headers, and colored prompts; piped output is plain
(no escape sequences).
"""

from __future__ import annotations

import getpass
import os
import re
import sys
import termios
import tty
from pathlib import Path
from typing import Any

from bu.config import VALID_NAME_RE, Config, _default_history_dir, _default_raw_log_dir
from bu.crypto import CryptoError, encrypt_secret

# ----------------------------------------------------------------------
# ANSI styling — automatically disabled when output is not a TTY
# ----------------------------------------------------------------------

_COLOR = sys.stdout.isatty()

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


def _paint(color: str, text: str) -> str:
    """Wrap ``text`` in an ANSI color when stdout is a TTY."""
    if not _COLOR:
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def _error(msg: str) -> None:
    print(_paint("red", f"  ✖ {msg}"))


def _note(msg: str) -> None:
    print(_paint("yellow", f"  • {msg}"))


def _header(title: str) -> None:
    print()
    print(_paint("cyan", f"● {title}"))
    print(_paint("dim", "─" * 48))


def _prompt(prompt: str, default: str | None = None) -> str:
    suffix = _paint("dim", f" [{default}]") if default else ""
    return input(f"{_paint('green', '❯')} {prompt}{suffix}: ").strip() or (default or "")


def _prompt_yes_no(prompt: str, default: str = "n") -> bool:
    suffix = _paint("dim", " [Y/n]" if default == "y" else " [y/N]")
    while True:
        raw = _prompt(prompt + suffix, default).strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no", ""):
            return False
        _error("Please answer y or n.")


# ----------------------------------------------------------------------
# Numbered, arrow-key scrollable choice menu
# ----------------------------------------------------------------------

_MENU_LINES = 0


def _draw_menu(prompt: str, options: list[str], idx: int) -> list[str]:
    lines = [_paint("bold", prompt)]
    for i, opt in enumerate(options):
        if i == idx:
            lines.append(_paint("cyan", f"  ● {i + 1}) {opt}"))
        else:
            lines.append(_paint("dim", f"    {i + 1}) {opt}"))
    lines.append(_paint("dim", "  ↑/↓ move · 1-9 jump · Enter confirm"))
    return lines


def _render_menu(prompt: str, options: list[str], idx: int) -> None:
    global _MENU_LINES
    if _MENU_LINES:
        sys.stdout.write(f"\033[{_MENU_LINES}A")
    for line in _draw_menu(prompt, options, idx):
        sys.stdout.write("\033[2K")
        sys.stdout.write(line)
        sys.stdout.write("\n")
    _MENU_LINES = len(options) + 2
    sys.stdout.flush()


def _pick_fallback(prompt: str, options: list[str], default_index: int) -> str:
    """Plain numbered input for non-TTY contexts."""
    while True:
        print(prompt)
        for i, opt in enumerate(options):
            print(f"  {i + 1}) {opt}")
        raw = input(f"  choice [1-{len(options)}] (default {default_index + 1}): ").strip().lower()
        if not raw:
            return options[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw in options:
            return raw
        print(f"  Invalid — enter a number 1-{len(options)} or the option name.")


def pick(prompt: str, options: list[str], default_index: int = 0) -> str:
    """Show a numbered, arrow-key scrollable menu.  Returns chosen option."""
    if not sys.stdin.isatty():
        return _pick_fallback(prompt, options, default_index)

    idx = default_index
    _render_menu(prompt, options, idx)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # arrow keys: ESC [ A/B
                seq = sys.stdin.read(2)
                if seq == "[A":
                    idx = (idx - 1) % len(options)
                elif seq == "[B":
                    idx = (idx + 1) % len(options)
                _render_menu(prompt, options, idx)
            elif ch in ("\r", "\n"):
                break
            elif ch.isdigit():
                n = int(ch)
                if 1 <= n <= len(options):
                    idx = n - 1
                    _render_menu(prompt, options, idx)
            elif ch in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return options[idx]


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_multiline_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"""\n' + escaped + '\n"""'


def _toml_value(value: Any) -> str:
    """Render a value as TOML — lists are always written multiline."""
    if isinstance(value, list):
        return _toml_str_list(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if "\n" in value:
        return _toml_multiline_str(value)
    return _toml_str(value)


def _toml_str_list(values: list[str]) -> str:
    """Render a list of strings as a multiline TOML array."""
    if not values:
        return "[]"
    parts = ["["]
    for v in values:
        parts.append(f"    {_toml_str(v)},")
    parts.append("]")
    return "\n".join(parts)


def _write_passphrase_file(path: Path, passphrase: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(passphrase + "\n")
    os.chmod(path, 0o600)


_GLOBAL_EXCLUDE_TEMPLATE = """\
# Global exclusions — applied to every destination.
# Shared rsync/duplicity format: '#' comments and blank lines are ignored,
# one glob per line, relative to each source root.
#   '*'    within a path component
#   '**'   across directories
#   '?'    single character, '[...]' character ranges
#   '/x' or 'a/b' — anchored to the top level of each source
#   '+ '   include an exception; a plain line (or '- ') excludes
#
# Common developer files
.git
.svn
.hg
CVS
__pycache__
*.pyc
*.pyo
node_modules
.pytest_cache
.mypy_cache
.ruff_cache
.venv
build
dist
target
*.o
*.obj
*.class
#
# Common editor / OS files
.DS_Store
Thumbs.db
desktop.ini
.idea
.vscode
*~
*.swp
*.swo
*.bak
*.tmp
"""


def run_create_wizard(config_dir: Path | None = None, name: str | None = None) -> dict[str, Any]:
    """Run the interactive wizard and write the resulting .toml config.

    ``name`` may be supplied as a positional CLI argument; otherwise it is
    prompted for.  Returns a summary dict.  Raises EOFError/KeyboardInterrupt
    if aborted.
    """
    cfg = Config(config_dir)
    cfg_dir = cfg.config_dir
    existing = set(cfg.list_destinations())

    print(_paint("bold", "Create a new backup destination"))
    print(_paint("dim", "Ctrl-C aborts at any time."))

    step = 0

    def _section(title: str) -> None:
        nonlocal step
        step += 1
        _header(f"Step {step} · {title}")

    # ------------------------------------------------------------------
    # 1. Destination name (optional positional argument)
    # ------------------------------------------------------------------
    _section("Destination name")
    if name:
        if not VALID_NAME_RE.match(name):
            _error(f"Invalid name {name!r} — use only letters, digits, '-' or '_'.")
            raise SystemExit(1)
        if name in existing:
            _error(f"Destination {name!r} already exists.")
            raise SystemExit(1)
        print(_paint("green", f"  ✓ {name}"))
    else:
        while True:
            name = _prompt("name")
            if not name:
                _error("A name is required.")
                continue
            if not VALID_NAME_RE.match(name):
                _error("Use only letters, digits, '-' or '_' — no spaces.")
                continue
            if name in existing:
                _error(f"Destination {name!r} already exists — pick another name.")
                continue
            break

    # ------------------------------------------------------------------
    # 2. Backup type — drives the destination and method prompts
    # ------------------------------------------------------------------
    _section("Backup Destination")
    backup_type = pick("Where should backups go?", ["Directory", "Backblaze (B2)"], default_index=0)
    is_b2 = backup_type != "Directory"

    # ------------------------------------------------------------------
    # 3. Destination — DIR asks for a local directory, B2 for bucket details
    # ------------------------------------------------------------------
    _section("Destination")
    if not is_b2:
        while True:
            raw = _prompt("destination directory")
            if not raw:
                _error("A destination directory is required.")
                continue
            p = Path(raw).expanduser()
            if not p.exists():
                if _prompt_yes_no(f"{p} does not exist — create it?"):
                    try:
                        p.mkdir(parents=True)
                    except OSError as e:
                        _error(f"Cannot create {p}: {e}")
                        continue
                else:
                    continue
            if not p.is_dir():
                _error(f"Not a directory: {p}")
                continue
            if not os.access(p, os.W_OK):
                _error(f"Not writable: {p}")
                continue
            dest = str(Path(raw).expanduser())
            break
    else:  # B2
        while True:
            bucket = _prompt("bucket name")
            if not bucket:
                _error("Bucket name is required.")
                continue
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]*$", bucket):
                _error("Bucket names use letters, digits, and hyphens — no spaces or slashes.")
                continue
            break
        bpath = _prompt("path within bucket (blank = bucket root)")
        dest = f"b2://{bucket}/{bpath}" if bpath else f"b2://{bucket}"
    print(_paint("green", f"  ✓ {dest}"))

    extra: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 4. B2 credentials — always for B2 destinations
    # ------------------------------------------------------------------
    if is_b2:
        _section("Backblaze B2 credentials")
        mode = pick("How do you want to store the B2 credentials?", ["plaintext", "encrypted"], default_index=0)
        while True:
            aid = _prompt("account ID")
            if aid:
                break
            _error("Account ID is required.")
        while True:
            akey = getpass.getpass(_paint("yellow", "  application key") + ": ").strip()
            if akey:
                break
            _error("Application key is required.")

        if mode == "encrypted":
            while True:
                pw = getpass.getpass(_paint("yellow", "  encryption password") + ": ")
                pw2 = getpass.getpass(_paint("yellow", "  repeat password") + ": ")
                if pw == pw2 and pw:
                    break
                _error("Passwords don't match (or are empty) — try again.")
            try:
                extra["b2_account_id_enc"] = encrypt_secret(aid, pw)
                extra["b2_account_key_enc"] = encrypt_secret(akey, pw)
            except CryptoError as e:
                _error(f"Encryption failed: {e}")
                raise SystemExit(1)
        else:
            extra["b2_account_id"] = aid
            extra["b2_account_key"] = akey

    # ------------------------------------------------------------------
    # 5. Method — DIR offers both, B2 is always duplicity
    # ------------------------------------------------------------------
    _section("Backup method")
    if not is_b2:
        method = pick("Backup method:", ["rsync", "duplicity"], default_index=0)
    else:
        method = "duplicity"
        print(_paint("dim", "  duplicity — B2 backups are always encrypted with duplicity"))

    # ------------------------------------------------------------------
    # 6. Source paths
    # ------------------------------------------------------------------
    _section("Source paths")
    print(_paint("dim", "  Directories to back up (one per archive):"))
    sources: list[str] = []
    while True:
        label = _paint("green", "❯") + (" path" if sources else " path (required)")
        raw = input(f"{label}: ").strip()
        if not raw:
            if sources:
                break
            _error("At least one source path is required.")
            continue
        p = Path(raw).expanduser()
        if not p.exists():
            _error(f"Not found: {raw}")
            continue
        if not p.is_dir():
            _error(f"Not a directory: {raw}")
            continue
        sources.append(str(Path(raw).expanduser()))
        if not _prompt_yes_no("add another source?"):
            break

    # ------------------------------------------------------------------
    # 7. GPG passphrase — duplicity only
    # ------------------------------------------------------------------
    if method == "duplicity":
        _section("GPG passphrase")
        choice = pick(
            "How do you want to handle the duplicity GPG passphrase?",
            ["prompt", "file"],
            default_index=0,
        )
        if choice == "file":
            default_pf = str(Path.home() / ".config" / "bu" / "secrets" / f"{name}.pass")
            pf = _prompt("passphrase file", default_pf)
            while True:
                pp = getpass.getpass(_paint("yellow", "  passphrase") + ": ").strip()
                if pp:
                    break
                _error("Passphrase is required.")
            _write_passphrase_file(Path(pf).expanduser(), pp)
            extra["passphrase_file"] = str(Path(pf).expanduser())

    # ------------------------------------------------------------------
    # 8. Optional duplicity settings
    # ------------------------------------------------------------------
    if method == "duplicity":
        _section("duplicity options")
        if raw := _prompt("change default duplicity 'full if older than' setting (e.g. 30D, blank = default)"):
            extra["full_if_older_than"] = raw
        verbosity = pick(
            "How verbose should duplicity be?",
            ["Verbose (6)", "Quiet (0)"],
            default_index=0,
        )
        extra["verbosity"] = "0" if verbosity == "Quiet (0)" else "6"

    # ------------------------------------------------------------------
    # 9. Exclusion files — global + per-name, created on demand
    # ------------------------------------------------------------------
    _section("Exclusion files")
    global_excl = cfg_dir / "exclude.txt"
    name_excl = cfg_dir / f"exclude-{name}.txt"

    if not global_excl.exists() and not name_excl.exists():
        cfg_dir.mkdir(parents=True, exist_ok=True)
        global_excl.write_text(_GLOBAL_EXCLUDE_TEMPLATE)
        name_excl.write_text(
            f"# Exclusions for {name!r} — add one glob per line.\n"
            "# Shared rsync/duplicity format; see exclude.txt for the global list.\n"
        )
        print(_paint("green", f"  ✓ Created {global_excl.name} with common defaults"))
        print(_paint("green", f"  ✓ Created {name_excl.name} (empty)"))
    else:
        print(_paint("dim", f"  Using {global_excl.name} and {name_excl.name} (already exist)"))
        if not global_excl.exists() or not name_excl.exists():
            _note("One of the two files does not exist yet — it is ignored until created.")

    extra["exclude_files"] = [str(global_excl), str(name_excl)]
    print(_paint("yellow", "  • Check and edit both exclusion files to match your needs"))

    # ------------------------------------------------------------------
    # 10. Assemble and write the config
    # ------------------------------------------------------------------
    history_path = str(_default_history_dir() / f"{name}.json")
    log_path = str(_default_raw_log_dir() / f"{name}.log")

    cfg_dir.mkdir(parents=True, exist_ok=True)
    file_path = cfg_dir / f"{name}.toml"

    lines = [f"# {method} destination {name!r} — created by 'bu create'"]
    lines.append(f"method = {_toml_str(method)}")
    lines.append(f"source_paths = {_toml_str_list(sources)}")
    lines.append(f"destination = {_toml_str(dest)}")
    for key, value in extra.items():
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append(f"history_file = {_toml_str(history_path)}")
    lines.append(f"log_file = {_toml_str(log_path)}")
    file_path.write_text("\n".join(lines) + "\n")

    dest_cfg = Config(cfg_dir).get(name)

    return {
        "ok": True,
        "file": str(file_path),
        "destination": {
            "name": name,
            "method": dest_cfg.method,
            "path": dest_cfg.destination,
            "source_paths": dest_cfg.source_paths,
        },
    }
