"""Interactive 'bu create' wizard for generating destination configs.

Flow: backup type (DIR or B2) → destination (local directory for DIR, or
B2 bucket/path plus credentials for B2) → method (rsync/duplicity for DIR,
duplicity-only for B2) → source paths → duplicity-specific options.
Every answer is validated and re-prompted until it is correct.

Choice menus are numbered and arrow-key scrollable when running on a TTY;
when piped, they fall back to plain numbered input.
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

from bu.config import Config, VALID_NAME_RE, _default_history_dir, _default_raw_log_dir
from bu.crypto import CryptoError, encrypt_secret


def _prompt(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or (default or "")


def _prompt_yes_no(prompt: str, default: str = "n") -> bool:
    suffix = " [Y/n]" if default == "y" else " [y/N]"
    while True:
        raw = _prompt(prompt + suffix, default).strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no", ""):
            return False
        print("  Please answer y or n.")


# ----------------------------------------------------------------------
# Numbered, arrow-key scrollable choice menu
# ----------------------------------------------------------------------

_MENU_LINES = 0


def _draw_menu(prompt: str, options: list[str], idx: int) -> list[str]:
    lines = [prompt]
    for i, opt in enumerate(options):
        marker = "●" if i == idx else " "
        lines.append(f"  {marker} {i + 1}) {opt}")
    lines.append("  ↑/↓ move · 1-9 jump · Enter confirm")
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


def run_create_wizard(config_dir: Path | None = None, name: str | None = None) -> dict[str, Any]:
    """Run the interactive wizard and write the resulting .toml config.

    ``name`` may be supplied as a positional CLI argument; otherwise it is
    prompted for.  Returns a summary dict.  Raises EOFError/KeyboardInterrupt
    if aborted.
    """
    cfg = Config(config_dir)
    cfg_dir = cfg.config_dir
    existing = set(cfg.list_destinations())

    print("Create a new bu destination\n")

    # ------------------------------------------------------------------
    # 1. Destination name (optional positional argument)
    # ------------------------------------------------------------------
    if name:
        if not VALID_NAME_RE.match(name):
            print(f"Invalid name {name!r} — use only letters, digits, '-' or '_'.")
            raise SystemExit(1)
        if name in existing:
            print(f"Destination {name!r} already exists.")
            raise SystemExit(1)
        print(f"Destination name: {name}")
    else:
        while True:
            name = _prompt("Destination name")
            if not name:
                print("  A name is required.")
                continue
            if not VALID_NAME_RE.match(name):
                print("  Use only letters, digits, '-' or '_' — no spaces.")
                continue
            if name in existing:
                print(f"  Destination {name!r} already exists — pick another name.")
                continue
            break

    # ------------------------------------------------------------------
    # 2. Backup type — drives the destination and method prompts
    # ------------------------------------------------------------------
    backup_type = pick("Backup type:", ["DIR", "B2"], default_index=0)

    # ------------------------------------------------------------------
    # 3. Destination — DIR asks for a local directory, B2 for bucket details
    # ------------------------------------------------------------------
    if backup_type == "DIR":
        while True:
            raw = input("  destination directory: ").strip()
            if not raw:
                print("  A destination directory is required.")
                continue
            p = Path(raw).expanduser()
            if not p.exists():
                if _prompt_yes_no(f"  {p} does not exist — create it?"):
                    try:
                        p.mkdir(parents=True)
                    except OSError as e:
                        print(f"  Cannot create {p}: {e}")
                        continue
                else:
                    continue
            if not p.is_dir():
                print(f"  Not a directory: {p}")
                continue
            if not os.access(p, os.W_OK):
                print(f"  Not writable: {p}")
                continue
            dest = str(Path(raw).expanduser())
            break
    else:  # B2
        print(f"Backblaze B2 destination for {name!r}:")
        while True:
            bucket = input("  bucket name: ").strip()
            if not bucket:
                print("  Bucket name is required.")
                continue
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]*$", bucket):
                print("  Bucket names use letters, digits, and hyphens — no spaces or slashes.")
                continue
            break
        bpath = _prompt("  path within bucket (blank = bucket root)")
        dest = f"b2://{bucket}/{bpath}" if bpath else f"b2://{bucket}"

    extra: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 4. B2 credentials — always for B2 destinations
    # ------------------------------------------------------------------
    if backup_type == "B2":
        print(f"Backblaze B2 credentials for {name!r}:")
        mode = pick("  store as:", ["plaintext", "encrypted"], default_index=0)
        while True:
            aid = _prompt("  account ID")
            if aid:
                break
            print("  Account ID is required.")
        while True:
            akey = getpass.getpass("  application key: ").strip()
            if akey:
                break
            print("  Application key is required.")

        if mode == "encrypted":
            while True:
                pw = getpass.getpass("  encryption password: ")
                pw2 = getpass.getpass("  repeat password: ")
                if pw == pw2 and pw:
                    break
                print("  Passwords don't match (or are empty) — try again.")
            try:
                extra["b2_account_id_enc"] = encrypt_secret(aid, pw)
                extra["b2_account_key_enc"] = encrypt_secret(akey, pw)
            except CryptoError as e:
                print(f"  Encryption failed: {e}")
                raise SystemExit(1)
        else:
            extra["b2_account_id"] = aid
            extra["b2_account_key"] = akey

    # ------------------------------------------------------------------
    # 5. Method — DIR offers both, B2 is always duplicity
    # ------------------------------------------------------------------
    if backup_type == "DIR":
        method = pick("Method:", ["rsync", "duplicity"], default_index=0)
    else:
        method = "duplicity"
        print("Method: duplicity (B2 backups always use duplicity)")

    # ------------------------------------------------------------------
    # 6. Source paths
    # ------------------------------------------------------------------
    print("Source paths (directories to back up):")
    sources: list[str] = []
    while True:
        label = "  path" if sources else "  path (required)"
        raw = input(f"{label}: ").strip()
        if not raw:
            if sources:
                break
            print("  At least one source path is required.")
            continue
        p = Path(raw).expanduser()
        if not p.exists():
            print(f"  Not found: {raw}")
            continue
        if not p.is_dir():
            print(f"  Not a directory: {raw}")
            continue
        sources.append(str(Path(raw).expanduser()))
        if not _prompt_yes_no("  add another source?"):
            break

    # ------------------------------------------------------------------
    # 7. GPG passphrase — duplicity only
    # ------------------------------------------------------------------
    if method == "duplicity":
        choice = pick("  GPG passphrase handling:", ["prompt", "file"], default_index=0)
        if choice == "file":
            default_pf = str(Path.home() / ".config" / "bu" / "secrets" / f"{name}.pass")
            pf = _prompt("  passphrase file", default_pf)
            while True:
                pp = getpass.getpass("  passphrase: ").strip()
                if pp:
                    break
                print("  Passphrase is required.")
            _write_passphrase_file(Path(pf).expanduser(), pp)
            extra["passphrase_file"] = str(Path(pf).expanduser())

    # ------------------------------------------------------------------
    # 8. Optional duplicity settings
    # ------------------------------------------------------------------
    if method == "duplicity":
        if raw := _prompt("  full_if_older_than (e.g. 30D, blank = default)"):
            extra["full_if_older_than"] = raw
        if raw := _prompt("  verbosity (0-9, blank = auto)"):
            if raw.isdigit() and 0 <= int(raw) <= 9:
                extra["verbosity"] = raw
            else:
                print("  Ignored invalid verbosity (must be 0-9).")

    # ------------------------------------------------------------------
    # 9. Assemble and write the config
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
