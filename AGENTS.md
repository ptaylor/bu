# AGENTS.md

Guidance for AI coding agents working on **bu** — a backup utility (Python, click
CLI) that backs up source directories via **rsync** (local) or **duplicity**
(encrypted, local or Backblaze B2).

## Command format rules

```
bu ACTION NAME [ARGS...]
```

- `ACTION` is one of: `backup`, `restore`, `status`, `list`, `config`,
  `delete`, `history`, `log`, `encrypt`, `create`, `help`.
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
- Required keys: `method` (`"rsync"` or `"duplicity"`), `source_paths`
  (list of directories), `destination` (path or URL).
- Optional keys: `history_file`, `log_file`, plus method-specific extras.
- `_RESERVED_KEYS` in `src/bu/config.py` lists keys handled by the config layer;
  anything else flows through to the backend as `extra` config.
- Generated configs (wizard and the `bu config NAME` sample template) must
  use multiline TOML lists:

  ```toml
  source_paths = [
      "~/Photos",
      "~/Camera",
  ]
  ```

- `bu config NAME` validates the TOML after `$EDITOR` closes:
  rsync + URL-ish destination → error ("rsync is local-only");
  duplicity with `b2:/...` (missing `//`) → error.

## Method patterns

### rsync (`src/bu/backends/local.py`)

- Local directories only. `destination = "/mnt/backup"`.
- Each source is mirrored **directly into the destination** as
  `<destination>/<source basename>/` — there is NO destination-name subfolder.
- Backup uses `rsync -a --delete` (exact mirror) plus `--stats`; restore never
  uses `--delete` and excludes the status file.
- Status file: `<destination>/bu-<NAME>-status.txt` (JSON content), written at
  backup start/end (`started`/`completed`/`error`). Dry-runs write nothing.
  The status file is NOT synced back on restore.

### duplicity (`src/bu/backends/duplicity.py`)

- Local directory or B2 URL. `destination = "/mnt/backup"` or
  `destination = "b2://bucket-name/path"`.
- duplicity 3.x takes exactly ONE source per invocation → loop once per source,
  each into its own archive subdir `<destination>/<source basename>/`
  (same layout rule as rsync; B2 URLs get `/subdir` appended).
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
  result errors, and raw log).

## Runtime state

- `~/.local/state/bu/history/<name>.json` — structured JSON-lines action history
  (`ActionLogger` in `src/bu/logging.py`).
- `~/.local/state/bu/logs/<name>.log` — raw execution log (override base dir
  with `BU_LOG_DIR`). Duplicity stderr is condensed in logs too.
- Defaults honor `XDG_STATE_HOME`.

## Codebase conventions

- `src/bu/cli.py` — command definitions only; thin wrappers around
  `action_*` handlers.
- `src/bu/actions.py` — action handlers, formatters, sample config templates.
- `src/bu/config.py` — config loading, `DestinationConfig`, validation.
- `src/bu/wizard.py` — interactive `bu create` (arrow-key menus via termios).
- `src/bu/backends/base.py` — `Backend` ABC, `LiveWindow` (docker-style
  redraw, title + yellow divider + throttled status line), `run_streaming`.
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
