# AGENTS.md

Guidance for AI coding agents working on **bu** — a backup utility (Python, click
CLI) that backs up source directories via **rsync** (local), **snapshot**
(hard-linked timestamped local snapshots), or **duplicity** (encrypted, local
or Backblaze B2).

## Command format rules

```
bu ACTION NAME [ARGS...]
```

- `ACTION` is one of: `backup`, `backup-restart`, `backup-remove`, `restore`,
  `status`, `list`, `config`, `delete`, `history`, `log`, `encrypt`,
  `create`, `help`.
- `NAME` is a named destination that exists as a `.toml` config file
  (except `list`, `encrypt`, `create`, and bare `config`, which take no
  name).
- No command-line options — commands take positional arguments only. Use
  `bu help` or `bu help ACTION` for usage (there is no `--help`). The
  live-output window height is automatic (18 lines on a TTY, off when piped).
- Commands are defined in `src/bu/cli.py` and listed in definition order via
  `OrderedGroup`. Keep the module docstring's action list in sync.
- Every command exits non-zero (`sys.exit(1)`) on any error; friendly messages
  go to stderr via `click.echo(..., err=True)`.
- `bu restore NAME RESTORE_DIR [PATH]` — `PATH` is a path within the backup;
  its files are restored into `RESTORE_DIR/<final path component>`
  (e.g. path `x/y` → `RESTORE_DIR/y`). Restore NEVER deletes files.
- `bu backup` refuses to run while the destination status file is not
  `completed` (prevents concurrent runs and silently overwriting a failed
  one).  `bu backup-restart NAME` resets the status and runs again — for
  snapshot it resumes the SAME timestamp dir recorded in the status file
  (that dir is excluded from `--link-dest` selection).  `bu backup-remove
  NAME` (snapshot only, always confirms) deletes the incomplete snapshot
  dir and resets the status.  Both refuse to run when the status is
  `completed` (nothing to recover).  `bu restore` refuses while the status
  is not `completed`.  `bu status` prints the remediation hints.
- `bu delete NAME` removes ONLY the config file (always prompts for
  confirmation). It must always warn that backup contents are not deleted.

### Destination names

- Names may contain only `A-Za-z0-9_-` — enforced by `VALID_NAME_RE` in
  `src/bu/config.py` (the single source of truth; `wizard.py` and `actions.py`
  import it, don't redefine it).
- A config file is `name.toml`; the filename stem is the destination name.

## Configuration model

- One TOML file per destination under `~/.config/bu/` (override with
  `BU_CONFIG_DIR`). Example: `~/.config/bu/photos.toml`.
- Required keys: `method` (`"rsync"`, `"duplicity"`, or `"snapshot"`), `source_paths`
  (list of directories), `destination` (path or URL).
- Optional keys: `history_file`, `log_file`, `exclude_files`, plus
  method-specific extras.
- `exclude_files` (list) defaults to `<config_dir>/exclude.txt` (global) and
  `<config_dir>/exclude-<name>.txt` (per destination); the key replaces the
  defaults. Missing files are skipped by the backends. Shared
  rsync/duplicity/snapshot format: `#` comments, blank lines, one glob per line (`*`, `**`, `?`,
  `[...]`), `+ ` include / `- ` exclude modifiers. Anchoring mirrors rsync: a
  leading `/` or any pattern containing `/` is anchored to the top level of
  each source; bare patterns match at any depth. rsync gets the file as-is;
  duplicity gets each pattern translated (`**/`-prefixed globs, or
  `<source absolute path>` + pattern for anchored ones) as
  `--include`/`--exclude` args (duplicity 3.x rejects bare globs with
  FilePrefixError). Un-anchored patterns match absolute paths, so they also
  fire when a *parent* of the source matches (e.g. bare `Library` kills
  sources under `~/Library/...`; use `/Library`).
- `_RESERVED_KEYS` in `src/bu/config.py` lists keys handled by the config layer;
  anything else flows through to the backend as `extra` config.
- `Config(strict=True)` (default) raises if any `.toml` file is invalid;
  `strict=False` collects per-file errors in `cfg.errors` (name → message) and
  `Config.get(name)` raises that specific error.  `bu list` uses lenient mode
  (shows invalid files, exits 1), and `bu delete`/`bu config NAME` can still
  fix or remove broken files.
- `source_paths` entries may be plain strings or tables with optional
  per-source filter files:
  `{ path = "/src", include = ["inc.txt"], exclude = ["exc.txt"] }`.
  `include` lists paths relative to that source (only listed paths are
  backed up, `.` = whole source); `exclude` is an rsync-style exclusion file
  for that source (falls back to the destination-level exclude files when
  omitted).  `DestinationConfig` parses them; backends receive
  `_source_includes` and `_source_excludes` lists aligned with
  `_source_paths`.  `src/bu/filters.py` has `read_filter_lines` and
  `build_include_rules` (parent-chain `+` rules for rsync).
- Generated configs (wizard and the `bu config NAME` sample template) must
  use multiline TOML lists:

  ```toml
  source_paths = [
      "~/Photos",
      "~/Camera",
  ]
  ```

- `bu config NAME` validates the TOML after `$EDITOR` closes:
  rsync/snapshot + URL-ish destination → error ("rsync and snapshot are
  local-only");
  duplicity with `b2:/...` (missing `//`) → error.

## Method patterns

### rsync (`src/bu/backends/local.py`)

- Local directories only. `destination = "/mnt/backup"`.
- rsync binary resolution (`resolve_rsync_binary` in local.py): honors
  `$BU_RSYNC`; otherwise prefers a full rsync (3.x) on PATH, and when PATH
  only offers Apple's openrsync it checks `/opt/homebrew/bin/rsync`,
  `/usr/local/bin/rsync`, `/opt/local/bin/rsync`.  openrsync cannot copy
  Unix socket files to SMB/NFS (`mkstempsock: Operation not supported`),
  so bu always passes `--no-specials` (sockets/fifos skipped) and adds a
  warning to backup/status `notes` when openrsync is in use.
- Each source is mirrored **directly into the destination** as
  `<destination>/<source basename>/` — there is NO destination-name subfolder.
- Backup uses `rsync -a --delete` (exact mirror) plus `--stats`, and
  `--exclude-from=<file>` for each exclusion file that exists; restore never
  uses `--delete` and excludes the status file.  Per-source include files
  become `--include` rules followed by a trailing `--exclude=*`
  (exclusion rules → include rules → skip everything else, so exclusions
  win over includes).
- Status file: `<destination>/bu-<NAME>-status.txt` (JSON content), written at
  backup start/end (`started`/`completed`/`error`). Dry-runs write nothing.
  The status file is NOT synced back on restore.

### snapshot (`src/bu/backends/snapshot.py`)

- Local directories only. `destination = "/mnt/backup"`.
- Each backup creates `<destination>/<YYYY-MM-DD-HH.MM.SS>/` (UTC
  `%Y-%m-%d-%H.%M.%S`; same-second collisions get `-2`, `-3` suffixes),
  containing `<source basename>/` per source (duplicate basenames deduped
  with `_2` like duplicity).
- Unchanged files are hard-linked from the newest snapshot containing that
  source's subdir via `rsync -a --link-dest=<abs path>`; no `--link-dest`
  on the first backup or for sources added later.  rsync's default quick
  check compares size and mtime, so a file modified within the same second
  as the previous backup (same size) is treated as unchanged and
  hard-linked (same caveat as rsnapshot).  Per-source include/exclude
  filter files work as with rsync (shared `_rsync_one`).
- Hard-link support is verified by the `bu create` wizard and before the
  first backup: a random file in the destination is hard-linked to a second
  random name, the original deleted, and the content of the link checked.
  Later backups skip the check when `bu-<NAME>-status.txt` already records
  method `snapshot`.  Failure aborts with an error (FAT/exFAT/SMB targets).
- Status file: `<destination>/bu-<NAME>-status.txt` (same as rsync) with a
  `snapshot` key holding the timestamp, recorded from the very first
  `started` write so `bu backup-restart` can resume the SAME directory.
  Restore defaults to the latest snapshot; a PATH whose first component is
  a timestamp selects that snapshot.  Restore never deletes.
- `bu status` lists ALL snapshots (oldest→newest, latest marked).

### duplicity (`src/bu/backends/duplicity.py`)

- Local directory or B2 URL. `destination = "/mnt/backup"` or
  `destination = "b2://bucket-name/path"`.
- duplicity 3.x takes exactly ONE source per invocation → loop once per source,
  each into its own archive subdir `<destination>/<source basename>/`
  (same layout rule as rsync; B2 URLs get `/subdir` appended).
- Exclusion files are passed to duplicity as translated per-pattern
  `--include=<glob>` / `--exclude=<glob>` args (`translate_exclude_line` in
  `duplicity.py`); only existing files are read.  Per-source include files
  become `--include=<source>/<path>` args with a closing `--exclude=**`
  placed after the include args (exclusion args come first so they win).
- Passphrase resolution order (never store it in the config):
  1. `passphrase_file` key — first line of the file (perms should be 0600)
  2. `BU_PASSPHRASE` / `PASSPHRASE` environment variable
  3. interactive `getpass` prompt (TTY only)
  - Non-interactive contexts (status) never prompt.
- B2 credentials in config:
  - plaintext `b2_account_id` / `b2_account_key`, or
  - encrypted `b2_account_id_enc` / `b2_account_key_enc` — armored GPG blobs
    from `bu encrypt` (crypto via the `gpg` binary in `src/bu/crypto.py` — no
    `cryptography` package; it doesn't build on this platform).
- duplicity 3.x ignores `B2_ACCOUNT_ID`/`B2_APPLICATION_KEY` env vars. It reads
  the account ID from the URL username (`b2://<account_id>@bucket/path`) and the
  application key from the `BACKEND_PASSWORD` env var. `_target_url` embeds the
  account ID; `_b2_env` forwards the key as `BACKEND_PASSWORD`; resolved creds
  are cached on the backend instance.
- Status file strategy mirrors rsync: local → `<destination>/bu-<NAME>-status.txt`,
  B2 → `~/.local/state/bu/status/bu-<NAME>-status.txt`. `bu status` falls back
  to this file when it can't run `collection-status`.
- duplicity's stderr tracebacks are condensed to single-line errors
  (`condense_stderr` in `duplicity.py`): stderr is captured with
  `silence_stderr=True` and only condensed lines are re-emitted (live window,
  result errors, and raw log).  `bu status` also captures collection-status
  stdout silently (`silence_stdout=True` in `run_streaming`): the raw report
  lands in `last_backup.output`, not on the terminal.

## Runtime state

- `~/.local/state/bu/history/<name>.json` — structured JSON-lines action history
  (`ActionLogger` in `src/bu/logging.py`).
- `~/.local/state/bu/logs/<name>.log` — raw execution log (override base dir
  with `BU_LOG_DIR`). A "State: running" marker entry is written when an action
  starts, so the log exists even mid-run; the full entry is appended at the end.
  Duplicity stderr is condensed in logs too.
- Defaults honor `XDG_STATE_HOME`.

## Codebase conventions

- `src/bu/cli.py` — command definitions only; thin wrappers around
  `action_*` handlers.
- `src/bu/actions.py` — action handlers, formatters, sample config templates.
- `src/bu/config.py` — config loading, `DestinationConfig`, validation.
- `src/bu/wizard.py` — interactive `bu create` (arrow-key menus via termios).
  The wizard always writes `exclude_files = [global, per-name]`; when neither
  file exists it creates both — `exclude.txt` pre-filled with common
  developer/editor patterns, `exclude-<name>.txt` empty — and tells the user
  to review them.  It also creates an empty `include-<name>.txt` and writes a
  commented-out per-source include example
  (`# source_paths = [{ path = ..., include = ... }]`) in the generated
  config.
- `src/bu/backends/base.py` — `Backend` ABC, `LiveWindow` (docker-style
  redraw, title + yellow divider + status line showing elapsed time and the
  current source path via `set_context`), `run_streaming`.
- `src/bu/backends/{local,duplicity}.py` — `RsyncMethod`, `DuplicityMethod`.
- Backends receive `config["_name"]` and `config["_source_paths"]` internal keys.

### Environment & testing

- Use the project venv: `.venv/bin/python3` / `.venv/bin/bu`
  (`pip install -e ".[dev]"`; Python 3.11 in the venv).
- Tests must export temp dirs: `BU_CONFIG_DIR`, `BU_LOG_DIR`, `XDG_STATE_HOME`
  (plain shell assignment without `export` is NOT visible to child processes).
- TTY-only features (live window, wizard menus, progress bars) must be tested
  through a PTY (the terminal here is piped by default).
- Lint with ruff (line-length 100).
- Keep sample templates, wizard output, and README in sync when changing config
  keys, layout, or command behavior.
