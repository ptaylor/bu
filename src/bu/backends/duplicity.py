"""duplicity method — encrypted incremental backups via the duplicity CLI.

Backs up to a local ``file://`` target or a Backblaze B2 ``b2://`` target:
``<destination>/<source name>`` (one archive subdirectory per source, like the
rsync method).

Passphrase resolution order (backup and restore):
    1. ``passphrase_file`` key in the .toml — file containing the passphrase
       on its first line (permissions should be 0600)
    2. ``BU_PASSPHRASE`` or ``PASSPHRASE`` environment variable
    3. Interactive prompt (getpass) — restore/backup prompt when nothing
       else is configured

Configuration keys:
    destination: str — base target directory for duplicity archives
    passphrase_file: str — optional path to a file holding the passphrase
    full_if_older_than: str — optional duplicity full-backup cadence (e.g. "30D")
"""

from __future__ import annotations

import datetime
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bu.backends.base import Backend, LiveWindow, run_streaming
from bu.crypto import CryptoError, decrypt_secret


def translate_exclude_line(line: str) -> tuple[bool, str] | None:
    """Parse one rsync-style exclusion line into (include, pattern).

    Returns None for blank/comment lines.  ``+ `` / ``- `` modifiers map to
    duplicity include/exclude.  The pattern is returned raw — anchoring to
    the source root and ``**/`` prefixing happen per source in
    ``_exclude_args``.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    include = False
    if line.startswith("+ "):
        include, pattern = True, line[2:].strip()
    elif line.startswith("- "):
        include, pattern = False, line[2:].strip()
    else:
        pattern = line
    if not pattern:
        return None
    return include, pattern


def condense_stderr(stderr: str) -> list[str]:
    """Condense duplicity's multi-line stderr into single-line error messages.

    Each "Error processing remote file (...)" block is followed by a long
    Python traceback; every block is reduced to one line.  GPG passphrase
    failures become a single friendly line instead of a 30-line traceback.
    """
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        return []
    if len(lines) == 1:
        return lines

    # Split into blocks, one per "Error processing remote file (...)" line.
    blocks: list[list[str]] = []
    current: list[str] = []
    for ln in lines:
        if ln.startswith("Error processing"):
            if current:
                blocks.append(current)
            current = [ln]
        else:
            current.append(ln)
    if current:
        blocks.append(current)

    summaries: list[str] = []
    for block in blocks:
        text = "\n".join(block)
        first = block[0] if block else ""
        if "Bad session key" in text:
            summaries.append(
                "GPG decryption failed: Bad session key — is the passphrase correct?"
            )
        elif "GPG Failed" in text:
            summaries.append("GPG failed — check the passphrase and that gpg-agent is working.")
        elif first.startswith("Traceback"):
            summaries.append("duplicity failed with an internal error (see the log for details).")
        elif first.startswith("Error processing remote file ("):
            # Single line, but drop the long timestamped archive filename.
            m = re.match(r"^Error processing remote file \(([^)]*)\):\s*(.*)$", first)
            if m and m.group(2):
                summaries.append(f"Error processing remote file ({m.group(1)}): {m.group(2)}")
            elif m:
                summaries.append(f"Error processing remote file ({m.group(1)}).")
            else:
                summaries.append(first)
        else:
            summaries.append(first)

    # Deduplicate repeated identical messages (one per failed file).
    seen: set[str] = set()
    out: list[str] = []
    for s in summaries:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class DuplicityMethod(Backend):
    """Back up using the duplicity CLI with symmetric GPG encryption."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _target_url(self, subdir: str | None = None, account_id: str | None = None) -> str:
        """Return the duplicity target URL.

        If ``destination`` is a full URL (e.g. ``b2://bucket/path``) it is
        used as-is; otherwise it is treated as a local path:
        ``file://<destination>[/<subdir>]``.

        duplicity 3.x reads the B2 account ID from the URL username
        (``b2://<account_id>@bucket/path``); when ``account_id`` is omitted
        it is taken from the resolved-credentials cache.
        """
        destination = self.config.get("destination", "")
        if not destination:
            raise ValueError("duplicity method requires 'destination' in config")
        if "://" in destination:
            url = destination.rstrip("/")
            if self._is_b2():
                aid = account_id
                if aid is None:
                    creds = getattr(self, "_b2_creds_cache", None)
                    aid = creds[0] if creds else None
                if aid:
                    scheme, _, rest = url.partition("://")
                    url = f"{scheme}://{quote(aid, safe='')}@{rest}"
            return f"{url}/{subdir}" if subdir else url
        base = Path(destination).expanduser().resolve()
        return f"file://{base / subdir}" if subdir else f"file://{base}"

    def _is_b2(self) -> bool:
        """True when the destination is a Backblaze B2 URL."""
        return self.config.get("destination", "").startswith("b2://")

    def _status_path(self) -> Path:
        """Return the status file path.

        Local targets: ``<destination>/bu-<name>-status.txt`` — the same
        strategy as the rsync method.  Remote (B2) targets have no local
        destination, so the file lives under the state directory instead.
        """
        name = self.config.get("_name", "unknown")
        if self._is_b2():
            from bu.config import _default_state_dir

            return _default_state_dir() / "status" / f"bu-{name}-status.txt"
        base = Path(self.config.get("destination", "")).expanduser().resolve()
        return base / f"bu-{name}-status.txt"

    def _write_status(self, state: str, **extra: Any) -> None:
        """Write (or overwrite) bu-<name>-status.txt with the current backup state."""
        status: dict[str, Any] = {
            "destination": self.config.get("_name", "unknown"),
            "method": "duplicity",
            "source_paths": self.config.get("_source_paths", []),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "state": state,
        }
        status.update(extra)
        path = self._status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(status, fh, indent=2, default=str)

    def _archive_subdirs(self) -> list[str]:
        """List archive subdirectories under the destination (one per source)."""
        base = Path(self.config.get("destination", "")).expanduser().resolve()
        root = base
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def _resolve_passphrase(self) -> str:
        """Resolve the GPG passphrase from file, env, or interactive prompt."""
        name = self.config.get("_name", "unknown")

        # 1. passphrase_file from config
        pf = self.config.get("passphrase_file", "")
        if pf:
            p = Path(pf).expanduser()
            if not p.exists():
                raise ValueError(f"Passphrase file not found: {p}")
            lines = p.read_text().splitlines()
            if not lines:
                raise ValueError(f"Passphrase file is empty: {p}")
            return lines[0].strip()

        # 2. Environment variables
        for env_var in ("BU_PASSPHRASE", "PASSPHRASE"):
            if val := os.environ.get(env_var):
                return val

        # 3. Interactive prompt
        if not sys.stdin.isatty():
            raise ValueError(
                f"No passphrase configured for {name!r}: set 'passphrase_file' "
                "in the config or run interactively to be prompted."
            )
        return getpass.getpass(f"GPG passphrase for '{name}': ")

    def _non_interactive_passphrase(self) -> str | None:
        """Resolve passphrase from file or env only — never prompts."""
        # passphrase_file from config
        pf = self.config.get("passphrase_file", "")
        if pf:
            p = Path(pf).expanduser()
            if p.exists():
                lines = p.read_text().splitlines()
                if lines:
                    return lines[0].strip()

        # Environment variables
        for env_var in ("BU_PASSPHRASE", "PASSPHRASE"):
            if val := os.environ.get(env_var):
                return val

        return None

    def _b2_credentials(self) -> tuple[str, str] | None:
        """Resolve Backblaze B2 credentials, or None when not configured.

        Resolution order:
            1. Plaintext ``b2_account_id`` + ``b2_account_key`` config keys
            2. Encrypted ``b2_account_id_enc`` + ``b2_account_key_enc`` keys
               (prompts for the password via getpass)
            3. ``B2_ACCOUNT_ID`` / ``B2_APPLICATION_KEY`` environment vars

        Successful resolutions are cached so the encrypted-credentials
        password is only prompted for once per run.
        """
        cached = getattr(self, "_b2_creds_cache", None)
        if cached:
            return cached

        name = self.config.get("_name", "unknown")

        # 1. Plaintext in config
        aid = self.config.get("b2_account_id", "")
        akey = self.config.get("b2_account_key", "")
        if aid and akey:
            self._b2_creds_cache = (aid, akey)
            return aid, akey

        # 2. Encrypted in config — prompt for password
        aid_enc = self.config.get("b2_account_id_enc", "")
        akey_enc = self.config.get("b2_account_key_enc", "")
        if aid_enc or akey_enc:
            if not sys.stdin.isatty():
                raise ValueError(
                    f"Encrypted B2 credentials for {name!r} need an "
                    "interactive terminal to prompt for the password."
                )
            pw = getpass.getpass(f"Password for B2 credentials of '{name}': ")
            try:
                aid = decrypt_secret(aid_enc, pw) if aid_enc else ""
                akey = decrypt_secret(akey_enc, pw) if akey_enc else ""
            except CryptoError as e:
                raise ValueError(f"Failed to decrypt B2 credentials: {e}") from e
            self._b2_creds_cache = (aid, akey)
            return aid, akey

        # 3. Environment fallback
        env_aid = os.environ.get("B2_ACCOUNT_ID", "")
        env_akey = os.environ.get("B2_APPLICATION_KEY", "")
        if env_aid and env_akey:
            self._b2_creds_cache = (env_aid, env_akey)
            return env_aid, env_akey

        return None

    def _b2_env(self) -> tuple[dict[str, str], list[str]]:
        """Return (env, errors) with B2 credentials if the target is B2.

        duplicity 3.x ignores ``B2_ACCOUNT_ID``/``B2_APPLICATION_KEY``:
        the account ID goes into the target URL (see ``_target_url``) and
        the application key is passed as the ``BACKEND_PASSWORD`` env var.
        """
        if not self._is_b2():
            return {}, []
        try:
            creds = self._b2_credentials()
        except ValueError as e:
            return {}, [str(e)]
        if not creds:
            return {}, [
                (
                    "Backblaze B2 destination requires credentials: set "
                    "b2_account_id/b2_account_key (or the _enc variants) in "
                    "the config, or B2_ACCOUNT_ID/B2_APPLICATION_KEY env vars."
                )
            ]
        return {"BACKEND_PASSWORD": creds[1]}, []

    def _exclude_args(self, source: Path) -> list[str]:
        """Build duplicity include/exclude args from rsync-style exclusion files.

        Each line is translated to a glob (duplicity 3.x raises
        FilePrefixError on bare patterns) and passed as
        ``--include=<glob>`` or ``--exclude=<glob>``.  Anchoring mirrors
        rsync: a leading ``/`` or any pattern containing ``/`` (not counting
        a trailing slash) is anchored to the resolved source root; other
        patterns get a ``**/`` prefix (any depth).  ``**`` patterns pass
        through unchanged.  Missing files are skipped.
        """
        args: list[str] = []
        for path in self.config.get("_exclude_files", []):
            if not isinstance(path, str):
                continue
            p = Path(path)
            if not p.is_file():
                continue
            for line in p.read_text().splitlines():
                translated = translate_exclude_line(line)
                if translated is None:
                    continue
                include, pattern = translated
                if pattern.startswith("/"):
                    pattern = f"{source}{pattern}"
                elif "/" in pattern.rstrip("/"):
                    # rsync anchors full-path patterns to the source root.
                    pattern = f"{source}/{pattern}"
                elif not pattern.startswith("**"):
                    pattern = "**/" + pattern
                flag = "--include" if include else "--exclude"
                args.append(f"{flag}={pattern}")
        return args

    def _run_duplicity(
        self,
        args: list[str],
        passphrase: str | None,
        scroll_lines: int = 0,
        window: LiveWindow | None = None,
        title: str = "Live output",
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run the duplicity CLI.  Returns ``{files, bytes, errors, stdout, stderr}``."""
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        if passphrase is not None:
            env["PASSPHRASE"] = passphrase
        else:
            env["PASSPHRASE"] = ""

        cmd = ["duplicity"] + args
        try:
            # stderr is captured silently — duplicity's tracebacks are
            # condensed into single-line errors and re-emitted below.
            proc = run_streaming(
                cmd, env=env, scroll_lines=scroll_lines, window=window,
                title=title, silence_stderr=True,
            )
        except FileNotFoundError:
            return {"files": 0, "bytes": 0,
                    "errors": ["duplicity binary not found. Is it installed?"],
                    "stdout": "", "stderr": ""}

        errors: list[str] = []
        if proc.returncode != 0:
            errors.extend(condense_stderr(proc.stderr))
            # Show the condensed lines in the live window (if one is active)
            if window is not None and window.active:
                for line in errors:
                    window.write(line + "\n")

        # Parse the "[ Backup Statistics ]" section
        files = self._parse_duplicity_stat(proc.stdout, "NewFiles")
        bytes_backed = self._parse_duplicity_stat(proc.stdout, "NewFileSize")

        return {
            "files": files,
            "bytes": bytes_backed,
            "errors": errors,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @staticmethod
    def _parse_duplicity_stat(output: str, key: str) -> int:
        """Parse an integer value from duplicity's Backup Statistics block."""
        m = re.search(rf"^{key}\s+(\d+)", output, re.MULTILINE)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except ValueError:
            return 0

    def _verbosity(self) -> int:
        """Return the duplicity verbosity level.

        Uses the optional ``verbosity`` config key (0-9); otherwise defaults
        to 6 when stdout is a TTY (file-level logs) and 4 when piped.
        """
        if "verbosity" in self.config:
            try:
                return max(0, min(9, int(self.config["verbosity"])))
            except (TypeError, ValueError):
                pass
        return 6 if sys.stdout.isatty() else 4

    def _duplicity_version(self) -> str:
        """Return the duplicity version string, or empty on failure."""
        try:
            proc = subprocess.run(
                ["duplicity", "--version"], capture_output=True, text=True, check=False,
            )
            return proc.stdout.split("\n")[0].strip()
        except (FileNotFoundError, OSError):
            return ""

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        errors: list[str] = []

        # Validate sources are directories
        for sp in source_paths:
            p = Path(sp).expanduser()
            if not p.exists():
                errors.append(f"Source not found: {p}")
            elif not p.is_dir():
                errors.append(f"Source must be a directory (not a file): {p}")
        if errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": errors}

        # Validate destination (local paths only — URLs like b2:// skip this)
        if not self._is_b2():
            base = Path(self.config.get("destination", "")).expanduser()
            if not base.exists():
                errors.append(f"Destination not found: {base}")
            elif not base.is_dir():
                errors.append(f"Destination is not a directory: {base}")
        if errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": errors}

        # Resolve passphrase unless dry-run
        passphrase = None
        if not dry_run:
            try:
                passphrase = self._resolve_passphrase()
            except ValueError as e:
                return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": [str(e)]}

        # B2 credentials if targeting Backblaze
        b2_env, b2_errors = self._b2_env()
        if b2_errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": b2_errors}

        if not dry_run:
            self._write_status("started")

        # duplicity 3.x takes exactly one source per invocation, so run
        # once per source into its own archive subdirectory.  The loop
        # stops on the first failure — e.g. a bad passphrase would make
        # every remaining source fail the same way.
        total_files = 0
        total_bytes = 0
        all_errors: list[str] = []
        all_stdout: list[str] = []
        all_stderr: list[str] = []

        used_subdirs: set[str] = set()
        window = LiveWindow(
            scroll_lines,
            title=f"Backup output — {self.config.get('_name', '?')}",
        )
        window.__enter__()
        try:
            for idx, sp in enumerate(source_paths):
                src = Path(sp).expanduser().resolve()

                # Unique archive subdir per source (basename, deduped)
                subdir = src.name or f"src{idx}"
                candidate = subdir
                n = 2
                while candidate in used_subdirs:
                    candidate = f"{subdir}_{n}"
                    n += 1
                used_subdirs.add(candidate)

                # Exclusions translated per source: leading-`/` patterns are
                # anchored to this source's root (missing files are ignored).
                exclude_args = self._exclude_args(src)

                args: list[str] = []
                if cadence := self.config.get("full_if_older_than"):
                    args.append(f"--full-if-older-than={cadence}")
                args.append(f"--verbosity={self._verbosity()}")
                if sys.stdout.isatty():
                    args.append("--progress")
                if dry_run:
                    args.append("--dry-run")
                args.extend(exclude_args)
                args.append(str(src))
                args.append(self._target_url(candidate))

                result = self._run_duplicity(args, passphrase, window=window, extra_env=b2_env)
                total_files += result["files"]
                total_bytes += result["bytes"]
                all_stdout.append(result.get("stdout", ""))
                all_stderr.append(result.get("stderr", ""))
                if result["errors"]:
                    # Fail fast: a bad passphrase (or any other failure) would
                    # make every remaining source fail the same way.
                    all_errors.extend(result["errors"])
                    all_errors.append(
                        f"Stopped — remaining sources not backed up after {candidate!r} failed"
                    )
                    break
        finally:
            window.__exit__(None, None, None)

        if not dry_run:
            if all_errors:
                self._write_status(
                    "error",
                    files_copied=total_files,
                    bytes_copied=total_bytes,
                    errors=all_errors,
                )
            else:
                self._write_status(
                    "completed",
                    files_copied=total_files,
                    bytes_copied=total_bytes,
                )

        return {
            "files_copied": total_files,
            "files_skipped": 0,
            "bytes_copied": total_bytes,
            "errors": all_errors,
            "stdout": "\n".join(all_stdout).strip(),
            "stderr": "\n".join(all_stderr).strip(),
            "duplicity_version": self._duplicity_version(),
        }

    def restore(
        self,
        restore_path: str,
        path_within_backup: str | None = None,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        errors: list[str] = []

        restore_dir = Path(restore_path).expanduser()
        if not restore_dir.exists():
            errors.append(f"Restore destination not found: {restore_dir}")
        elif not restore_dir.is_dir():
            errors.append(f"Restore destination is not a directory: {restore_dir}")
        if errors:
            return {"files_restored": 0, "bytes_restored": 0, "errors": errors}

        # Restore always needs the passphrase
        try:
            passphrase = self._resolve_passphrase()
        except ValueError as e:
            return {"files_restored": 0, "bytes_restored": 0, "errors": [str(e)]}

        # B2 credentials if targeting Backblaze
        b2_env, b2_errors = self._b2_env()
        if b2_errors:
            return {"files_restored": 0, "bytes_restored": 0, "errors": b2_errors}

        # Determine which archives to restore
        if path_within_backup:
            # The first component names the archive subdir (source basename);
            # the remainder is a path within that archive.
            parts = Path(path_within_backup).parts
            archives = [parts[0]]
            inner_path = "/".join(parts[1:]) if len(parts) > 1 else None
        else:
            archives = self._archive_subdirs()
            inner_path = None

        if not archives:
            return {"files_restored": 0, "bytes_restored": 0,
                    "errors": ["No backup archives found"]}

        total_files = 0
        total_bytes = 0
        all_errors: list[str] = []
        all_stdout: list[str] = []
        all_stderr: list[str] = []

        window = LiveWindow(
            scroll_lines,
            title=f"Restore output — {self.config.get('_name', '?')}",
        )
        window.__enter__()
        try:
            for subdir in archives:
                # A specific subpath restores into a subdirectory named
                # after its final component (path 'Travel/Paris' → RESTORE_DIR/Paris);
                # otherwise restore each archive into its own subdirectory.
                dest_dir = restore_dir / Path(path_within_backup).name if path_within_backup else restore_dir / subdir
                if not dry_run:
                    dest_dir.mkdir(parents=True, exist_ok=True)

                args = ["restore"]
                args.append(f"--verbosity={self._verbosity()}")
                if inner_path:
                    args.append(f"--path-to-restore={inner_path}")
                if sys.stdout.isatty():
                    args.append("--progress")
                if dry_run:
                    args.append("--dry-run")
                args.append(self._target_url(subdir))
                args.append(str(dest_dir))

                result = self._run_duplicity(args, passphrase, window=window, extra_env=b2_env)
                total_files += result["files"]
                total_bytes += result["bytes"]
                all_errors.extend(result["errors"])
                all_stdout.append(result.get("stdout", ""))
                all_stderr.append(result.get("stderr", ""))
        finally:
            window.__exit__(None, None, None)

        return {
            "files_restored": total_files,
            "bytes_restored": total_bytes,
            "errors": all_errors,
            "stdout": "\n".join(all_stdout).strip(),
            "stderr": "\n".join(all_stderr).strip(),
            "duplicity_version": self._duplicity_version(),
        }

    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = self.config.get("_name", "unknown")
        source_paths = self.config.get("_source_paths", [])
        is_b2 = self._is_b2()

        if is_b2:
            base = Path("")
            result: dict[str, Any] = {
                "name": name,
                "method": "duplicity",
                "config_ok": True,
                "source_paths": source_paths,
                "dest_path": self.config.get("destination", ""),
                "dest_exists": None,  # remote — not checked locally
            }
        else:
            base = Path(self.config.get("destination", "")).expanduser()
            result = {
                "name": name,
                "method": "duplicity",
                "config_ok": True,
                "source_paths": source_paths,
                "dest_path": str(base),
                "dest_exists": base.is_dir(),
            }

        # Source existence checks
        sources_status: list[dict[str, Any]] = []
        for sp in source_paths:
            p = Path(sp).expanduser()
            sources_status.append({"path": str(p), "exists": p.is_dir()})
        result["sources"] = sources_status

        # Passphrase availability (file/env only — status never prompts)
        passphrase = self._non_interactive_passphrase()
        result["passphrase_ready"] = passphrase is not None

        # B2 status: never prompt, use only non-interactive credential sources
        b2_env: dict[str, str] = {}
        b2_aid: str | None = None
        if is_b2:
            aid = self.config.get("b2_account_id", "") or os.environ.get("B2_ACCOUNT_ID", "")
            akey = self.config.get("b2_account_key", "") or os.environ.get("B2_APPLICATION_KEY", "")
            if aid and akey:
                b2_env = {"BACKEND_PASSWORD": akey}
                b2_aid = aid
            else:
                result["passphrase_ready"] = False

        # Collection status per archive, if backup exists and passphrase known.
        # B2 chains live under per-source subdirectories, so list those from
        # the configured source basenames (the names used for backups).
        if is_b2:
            subdirs: list[str] = []
            for sp in source_paths:
                bn = Path(sp).expanduser().name
                if bn not in subdirs:
                    subdirs.append(bn)
            archives = subdirs
        else:
            archives = self._archive_subdirs()
        result["archives"] = archives

        if is_b2:
            if passphrase and b2_env and archives:
                combined_output: list[str] = []
                combined_errors: list[str] = []
                for subdir in archives:
                    r = self._run_duplicity(
                        ["collection-status", self._target_url(subdir, account_id=b2_aid)],
                        passphrase,
                        extra_env=b2_env,
                    )
                    combined_output.append(f"--- {subdir} ---")
                    combined_output.append(r.get("stdout", "").strip())
                    combined_errors.extend(r["errors"])
                lock_errors = [e for e in combined_errors if "already running" in e]
                if lock_errors and len(lock_errors) == len(combined_errors):
                    # A backup is in progress — fall back to the status file
                    # written by the running instance instead of an error.
                    result["last_backup"] = None
                else:
                    result["last_backup"] = {
                        "state": "error" if combined_errors else "completed",
                        "timestamp": "",
                        "output": "\n".join(combined_output)[:500],
                        "errors": combined_errors,
                    }
            else:
                result["last_backup"] = None
        elif archives and passphrase:
            combined_output: list[str] = []
            combined_errors: list[str] = []
            for subdir in archives:
                r = self._run_duplicity(
                    ["collection-status", self._target_url(subdir)], passphrase,
                )
                combined_output.append(f"--- {subdir} ---")
                combined_output.append(r.get("stdout", "").strip())
                combined_errors.extend(r["errors"])
            lock_errors = [e for e in combined_errors if "already running" in e]
            if lock_errors and len(lock_errors) == len(combined_errors):
                # A backup is in progress — fall back to the status file
                # written by the running instance instead of an error.
                result["last_backup"] = None
            else:
                result["last_backup"] = {
                    "state": "error" if combined_errors else "completed",
                    "timestamp": "",
                    "output": "\n".join(combined_output)[:500],
                    "errors": combined_errors,
                }
        else:
            result["last_backup"] = None

        # Fall back to the written status file when collection-status
        # produced nothing (e.g. no passphrase available non-interactively).
        if not result.get("last_backup"):
            sp = self._status_path()
            if sp.exists():
                try:
                    result["last_backup"] = json.loads(sp.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

        return result

