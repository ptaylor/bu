# bu

Backup utility that mirrors directories with rsync, timestamped hard-linked
snapshots, or encrypted incremental duplicity archives (local disk or Backblaze B2).

## Usage

```
bu ACTION [ARGS...]
```

- **`ACTION`** — one of: `backup`, `restore`, `status`, `list`, `config`, `history`, `log`, `encrypt`, `create`, `help`
- **`NAME`** — a named destination defined in the configuration

Commands take positional arguments only — no flags to remember. Use `bu help`
(or `bu help COMMAND`) for usage.

### Commands

| Command | Description |
|---------|-------------|
| `bu backup NAME` | Back up source directories to the destination (rsync, snapshot, or duplicity). Refuses to run while the previous run is not `completed` |
| `bu backup-restart NAME` | Reset a failed/ongoing backup's status and start again; refuses when the last run completed (snapshot resumes the same timestamped directory) |
| `bu backup-remove NAME` | Remove an incomplete snapshot directory (snapshot only; refuses when the last run completed; asks for confirmation) |
| `bu restore NAME RESTORE_DIR [PATH]` | Restore files into a local directory (never deletes; refuses while the last backup is not `completed`) |
| `bu status NAME` | Show config, destination state, and last backup details |
| `bu list` | List all configured destinations |
| `bu config [NAME]` | Edit (or create) a destination config in `$EDITOR` |
| `bu delete NAME` | Delete a destination config file (contents are kept) |
| `bu create [NAME]` | Interactively create a new destination config |
| `bu history NAME` | Show action history in reverse chronological order |
| `bu log NAME` | Print the raw execution log |
| `bu encrypt` | Encrypt a secret (e.g. B2 credentials) for use in config |
| `bu help [COMMAND]` | Show usage for bu or for a specific command |

## Configuration

Each destination is a separate `.toml` file under `~/.config/bu/` (or `$BU_CONFIG_DIR`).

### Example: `~/.config/bu/photos.toml`

```toml
method = "rsync"
source_paths = [
    "~/Photos",
    "~/Camera",
]
destination = "/mnt/backup"
```

### Example: `~/.config/bu/docs.toml` (encrypted Backblaze B2)

```toml
method = "duplicity"
source_paths = [
    "~/Documents",
]
destination = "b2://my-bucket/docs"

# Backblaze B2 credentials (plaintext, or encrypted via 'bu encrypt')
b2_account_id = "..."
b2_account_key = "..."

# Optional duplicity settings
passphrase_file = "~/.config/bu/secrets/docs.pass"   # GPG passphrase (file, env, or prompt)
full_if_older_than = "30D"                           # full-backup cadence
verbosity = 6                                        # 0 = quiet, 6 = list files, 9 = debug
```

### Example: `~/.config/bu/notes.toml` (timestamped hard-linked snapshots)

```toml
method = "snapshot"
source_paths = [
    "~/notes",
]
destination = "/mnt/backup"
```

Each backup creates a new `destination/<UTC timestamp>/` (e.g.
`2026-08-15-21.09.41`) holding every source in its own subdirectory.
Unchanged files are hard-linked to the previous snapshot via
`rsync --link-dest`, so many timestamped backups share the same disk
blocks. The destination filesystem must support hard links. An interrupted
snapshot backup is recoverable: `bu backup-restart` resumes into the SAME
timestamped directory, or `bu backup-remove` discards it (after
confirmation).

> rsync decides whether a file is unchanged by size and modification time.
> A file changed within the same second as the previous backup — to the
> same size — is treated as unchanged and hard-linked (the same limitation
> as rsync's default quick check and rsnapshot).

### Config reference

| Key | Required | Description |
|-----|----------|-------------|
| `method` | Yes | `"rsync"`, `"duplicity"`, or `"snapshot"` |
| `source_paths` | Yes | Array of directory paths to back up; entries may also be tables with per-source `include`/`exclude` filter files (see below) |
| `destination` | Yes | Base target directory, or a `b2://bucket/path` URL (duplicity only) |
| `history_file` | No | Path to structured JSON-lines action history |
| `log_file` | No | Path to raw execution log |
| `exclude_files` | No | List of exclusion files; defaults to `exclude.txt` (global) + `exclude-NAME.txt` (per destination) |
| `passphrase_file` | No | duplicity: file whose first line holds the GPG passphrase (perms 0600) |
| `full_if_older_than` | No | duplicity: force a full backup when the last one is older than this (e.g. `"30D"`) |
| `verbosity` | No | duplicity: output verbosity 0-9 (default: automatic; 9 is debug-level and very noisy) |
| `b2_account_id` / `b2_account_key` | No | Backblaze B2 credentials, plaintext |
| `b2_account_id_enc` / `b2_account_key_enc` | No | Backblaze B2 credentials, encrypted via `bu encrypt` |

For `rsync`, each source is mirrored directly into `destination/<source name>`.
For `duplicity`, each source gets its own encrypted archive under the same
layout.
For `snapshot`, each backup creates `destination/<UTC timestamp>/<source name>/`;
unchanged files are hard-linked from the previous snapshot via
`rsync --link-dest` (the destination filesystem must support hard links).

### Exclusions

Files can be skipped during backup with exclusion files. Without an
`exclude_files` key, `bu` uses two default files (missing files are ignored):

- `~/.config/bu/exclude.txt` — global exclusions for every destination
- `~/.config/bu/exclude-NAME.txt` — exclusions for the `NAME` destination

Setting `exclude_files` replaces the defaults with your own list. The same
file format works for rsync and duplicity:

```
# comments and blank lines are ignored
*.tmp
**/node_modules
Cache
.DS_Store
+ .keep     # '+ ' includes an exception; plain lines exclude
```

One pattern per line, with identical rules for rsync and duplicity:

- `name` or `*.glob` — matches at **any depth**
- `**/...` — explicitly any depth
- `/name`, or any pattern containing `/` (e.g. `Library/Logs`) — anchored to
the top level of each source
- `+ ` include / `- ` exclude modifiers; `#` comments and blank lines are ignored

A pattern matching a directory also excludes its contents. bu passes the
file to rsync as-is and translates each pattern for duplicity (which
requires `**/`-prefixed globs or source-absolute paths).

> **Watch out:** un-anchored patterns are matched by duplicity against full
absolute paths, so they also match when a *parent* directory of the source
has that name — e.g. bare `Library` excludes everything when the source
itself lives under a `Library` folder. Use `/Library` to anchor the
exclusion to the source root instead.

### Include lists (optional)

Instead of excluding what you *don't* want, a source can map to an include
file that lists what you *do* want — everything else is skipped:

```toml
source_paths = [
    { path = "/Users/paul", include = "~/.config/bu/include-paul.txt", exclude = "~/.config/bu/exclude-paul.txt" },
]
```

- `include` — file listing paths relative to that source (one per line;
  `#` comments and blank lines ignored). Only the listed paths are backed
  up; a line of `.` backs up the whole source. A single file or a list.
- `exclude` — rsync-style exclusion file for that source. When omitted, the
  destination-level `exclude_files` defaults apply.
- Per source, the filter order is: exclusion rules, then the includes, then
  everything else is skipped — so exclusions win over includes. Removing a
  line stops updating that path but never deletes the copy already in the
  backup (excluded files are protected from `--delete`).

### Creating a config

```bash
bu create photos
```

The interactive wizard asks for the backup type (Directory or Backblaze B2),
destination, credentials, method, and source paths, then writes the config.
For snapshot destinations it verifies hard-link support on the destination
before writing the config. It also sets `exclude_files` to the global and
per-name exclusion files, creating both (with sensible defaults) if neither
exists yet.
Alternatively, `bu config photos` opens `$EDITOR` (default `vi`) with a
pre-filled template; after saving, the TOML is validated.

To store B2 credentials encrypted instead of in plaintext, run `bu encrypt`
and paste the armored blobs into `b2_account_id_enc` / `b2_account_key_enc`.

### State files

Per-destination runtime files are stored under `~/.local/state/bu/`:

```
~/.local/state/bu/
├── history/name.json              # Structured action history (JSON-lines)
├── logs/name.log                  # Raw execution output per run
└── status/bu-name-status.txt      # Last backup state (B2 destinations)
```

## Requirements

- `rsync` — for `method = "rsync"` destinations
- `rsync` and a hard-link-capable filesystem — for `method = "snapshot"`
  destinations
- `duplicity` (3.x) and `gpg` — for `method = "duplicity"` destinations
  (local disk or Backblaze B2)

## Installation

```bash
# Development install
pip install -e ".[dev]"

# Or install to a bin directory via symlink
./install.sh ~/.local/bin
```

## Examples

```bash
# Create a new destination config
bu create photos

# Run a backup
bu backup photos

# Check backup status
bu status photos

# Restore everything into ./restore
bu restore photos ./restore

# Or restore just one folder within the backup
bu restore photos ./restore Photos

# snapshot destinations restore from the newest snapshot by default;
# a timestamp PATH picks an older one
bu restore notes ./restore 2026-08-15-21.09.41

# View action history
bu history photos

# Show usage
bu help backup
```

## License

MIT — see [LICENSE](LICENSE).
