<p align="center">
  <img src="assets/icons/bu-icon.svg" width="88" height="88" alt="bu icon">
</p>

# bu

Backup utility that mirrors directories with rsync, timestamped hard-linked
snapshots, or encrypted incremental duplicity archives (local disk or Backblaze B2).

## Usage

```
bu ACTION [ARGS...]
```

- **`ACTION`** — one of: `backup`, `backup-restart`, `backup-remove`, `backup-dry-run`, `restore`, `restore-test`, `prune`, `status`, `list`, `config`, `delete`, `history`, `log`, `encrypt`, `create`, `help`
- **`NAME`** — a named destination defined in the configuration

Commands take positional arguments only — no flags to remember. Use `bu help`
(or `bu help COMMAND`) for usage.

### Commands

| Command | Description |
|---------|-------------|
| `bu backup NAME` | Back up source directories to the destination (rsync, snapshot, or duplicity). Refuses to run while the previous run is not `completed` |
| `bu backup-restart NAME` | Reset a failed/ongoing backup's status and start again; refuses when the last run completed (snapshot resumes the same timestamped directory) |
| `bu backup-remove NAME` | Remove an incomplete snapshot directory (snapshot only; refuses when the last run completed; asks for confirmation) |
| `bu backup-dry-run NAME` | List the files the next backup would consider, writing nothing. Paths stream to stdout as they are found; the filters in effect and the totals go to stderr |
| `bu restore NAME RESTORE_DIR [PATH]` | Restore files into a local directory (never deletes; refuses while the last backup is not `completed`) |
| `bu restore-test NAME` | Restore the last backup's marker files into a temporary directory and verify them — proves the backup is readable. Read-only; refuses while the last backup is not `completed` |
| `bu prune NAME` | List the snapshots a retention policy would remove (snapshot only). **Lists only — deletes nothing** |
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
| `restore_test_dirs` | No | Absolute paths inside the sources that receive a marker file before every backup, so `bu restore-test` can verify the backup (see below) |
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

Setting `exclude_files` replaces the defaults with your own list. A source's
own `exclude` files (see [Include lists](#include-lists-optional)) are **added**
to these defaults rather than replacing them, so the global exclusions really
do apply to every source.

One pattern per line — the same format works for rsync and duplicity:

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

> **Watch out:** rsync keeps trailing spaces as part of the pattern, so
> `Pictures/Takeout ` (with a stray space) silently never matches.
> `bu backup`/`bu status` warn about such lines.

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
- `exclude` — rsync-style exclusion file for that source. These are added to
the destination-level `exclude_files` rather than replacing them, and are
consulted first — so a `+ ` exception here can override a broad default.
- Per source, the filter order is: exclusion rules, then the includes, then
  everything else is skipped — so exclusions win over includes. Removing a
  line stops updating that path but never deletes the copy already in the
  backup (excluded files are protected from `--delete`).

### Restore-test markers (optional)

A backup is only worth having if you can read it back. Point
`restore_test_dirs` at one or more directories inside your sources and `bu`
writes a marker file into each of them **before** every backup:

```toml
restore_test_dirs = [
    "/Users/paul",
    "/Users/paul/Library/CloudStorage/Dropbox",
]
```

Entries are absolute paths that must sit inside one of that destination's
source paths — a stray entry is reported as a warning by `bu status` and
`bu backup` and then skipped, so it can never block a backup. `bu create`
fills this in with the root of every source it is given.

Before each source is transferred, bu creates the directory if it does not
exist and appends the backup's run identifier to `backup-status-<name>.txt`
inside it: the snapshot directory name for `snapshot` destinations, a UTC
timestamp otherwise. The marker is then backed up like any other file, so the
newest copy records which run wrote it.

`bu restore-test NAME` reads the identifiers the last completed backup
recorded, restores each marker file from the destination into a temporary
directory, and checks that its newest entry is the run that just completed.
A pass proves the newest backup is both present and readable. The temporary
directory is always removed, and neither the destination nor the source trees
are modified. Nothing is checked when no `restore_test_dirs` are configured.

> A `duplicity` destination needs its archive passphrase to read anything
> back. `bu restore-test` uses `passphrase_file` or `BU_PASSPHRASE` /
> `PASSPHRASE` when they are set, and otherwise prompts on the terminal — once
> for the whole run, not once per marker. Run unattended with neither a stored
> passphrase nor a terminal, it reports the missing passphrase and fails
> instead of blocking.

> A marker at the **root** of a source is let through that source's include
> list, which would otherwise skip anything not listed — so an
> `include-home.txt`-style list needs no marker entry. A marker in a
> *subdirectory* must be covered by the list like any other path, and an
> exclusion pattern matching the marker wins over it. `bu status` and
> `bu backup` warn about both.

> bu records each directory's **real on-disk name**. macOS volumes are
> case-insensitive but case-*preserving*, so a configured `.../backups` can
> name a folder actually called `Backups`; the backup stores the on-disk
> spelling, and a destination on a case-**sensitive** volume (external disks
> are often formatted Case-sensitive APFS) would not find the configured one.
> Recording the configured spelling would make `bu restore-test` report
> `Backup path not found` even though the marker is present.

> When a source is cloud-synced (Dropbox, Google Drive) the marker file is
> uploaded like any other file and appears on your other devices. It is a few
> dozen bytes plus one line per backup.

### Pruning snapshots

`bu prune NAME` works out which timestamped snapshots a retention policy would
remove. It applies to **snapshot** destinations only — other methods say so and
exit — and in this version it **lists the plan and deletes nothing**:

```
Prune plan — orange (15 snapshot(s))
Retention: all snapshots from today; one per day for the last 7 days; one per
           week for the last month; one per month for the last year; one per
           year before that.

  ✓ 2026-09-27-18.04.56   today
  ✓ 2026-07-31-19.14.31   newest of month 2026-07
  ✓ 2025-08-20-19.34.49   newest of year 2025
  ✓ 2022-12-03-15.52.04   newest of year 2022
  - 2022-09-07-20.00.52   superseded by 2022-12-03-15.52.04 (year 2022)
  - 2022-08-28-17.21.22   superseded by 2022-12-03-15.52.04 (year 2022)

10 kept, 5 would be removed — nothing was deleted (listing only).
```

One line per snapshot, newest first: kept snapshots are highlighted and the
removal candidates are dimmed. Colour is dropped automatically when the output
is not a terminal (piped, redirected, logged), where the `✓` and `-` markers
carry the same meaning.

The policy is fixed, and measured in whole UTC calendar days so it always
agrees with the snapshot directory names:

| age | kept |
|-----|------|
| today | every snapshot |
| 1–7 days | one per day |
| 8–31 days | one per week |
| 32–365 days | one per month |
| older | one per year |

The **newest** snapshot in each period is the one kept. The directory an
interrupted or failed run is using is never a candidate — `bu backup-remove`
clears that one — and the listing says so when it applies.

`bu prune` reads the destination and writes nothing at all: no status file,
no history entry, no log. It never touches the source trees, so it is safe to
run at any time, including while a backup is in progress.

### Creating a config

```bash
bu create photos
```

The interactive wizard asks for the backup type (Directory or Backblaze B2),
destination, credentials, method, and source paths, then writes the config.
For snapshot destinations it verifies hard-link support on the destination
before writing the config. It also sets `exclude_files` to the global and
per-name exclusion files, creating both (with sensible defaults) if neither
exists yet, and sets `restore_test_dirs` to the root of every source path.
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

> duplicity refuses to run when the process may open fewer than 1024 files,
> and macOS gives Finder/launchd/cron processes only 256. bu raises the limit
> for itself and every tool it runs, so `bu backup`/`bu restore` work from any
> launch context.

> **macOS: run bu from a process with Full Disk Access.** Several common
> sources are protected by TCC — `~/Pictures/Photos Library.photoslibrary`,
> `~/Library/Mail`, `~/Library/Safari`, and the Dropbox folder under
> `~/Library/CloudStorage/`. Without Full Disk Access (System Settings →
> Privacy & Security → Full Disk Access) rsync fails with
> `Operation not permitted` and the run ends in `error`. Google Drive stays
> readable without it, so one blocked source is easy to miss — check for
> `Operation not permitted` in `bu log NAME`.
>
> A blocked source never costs you data you already have: rsync reports
> `IO error encountered -- skipping file deletion`, so the files already in
> the snapshot are left alone. The run still exits non-zero and is marked
> `error` (`bu backup-restart` to retry); the affected subtree is simply
> missing from that snapshot.

> macOS ships Apple's openrsync as `/usr/bin/rsync`, which cannot copy
> Unix socket files (e.g. `~/.gnupg/S.gpg-agent*`) to SMB/NFS shares —
> it fails with `mkstempsock: Operation not supported`. bu passes
> `--no-specials` so socket/fifo files are skipped (they are runtime
> objects, not backup data), warns about openrsync, and prefers a full
> rsync when one is installed (`brew install rsync`, found
> automatically). You can also force a specific binary with the
> `BU_RSYNC` environment variable.

> When a file disappears between rsync's scan and the transfer — a cloud
> client rewriting it, or you renaming or moving something mid-run — rsync
> reports `some files vanished before they could be transferred` (exit code
> 24). bu treats that as a **warning**, not a failure: the file is simply
> absent from the new snapshot, and the previous snapshot still holds it.
> `bu backup` prints the warning, `bu status` lists it under `Warnings`, and
> the run still completes. Every other non-zero code remains fatal.

> **About `._*` files on SMB/NAS destinations:** when macOS writes any
> file or folder to an SMB share that doesn't store extended attributes
> natively (like the WD MyBookLive), it creates a hidden `._name`
> AppleDouble sidecar next to it. This is macOS behaviour, not bu —
> copying with Finder does the same. These files contain no backup data
> and are safe to delete:
>
> ```
> find /Volumes/backups/bogano -name '._*' -delete
> ```
>
> In practice you rarely need to: bu mirrors with `--delete`, so each
> backup automatically removes leftover `._*` files (they are never in
> the source), and macOS only recreates them next to files that change
> in that run. The only way to avoid them entirely is a destination
> that stores Mac metadata natively (e.g. another Mac, APFS/HFS+
> volume, or an SMB server with native xattr support).

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

# Preview the files the next backup would consider (nothing is written)
bu backup-dry-run photos > /tmp/photos-files.txt

# Run a backup
bu backup photos

# Check backup status
bu status photos

# Restore everything into ./restore
bu restore photos ./restore

# Or restore just one folder within the backup
bu restore photos ./restore Photos

# Check the newest backup is actually readable (uses restore_test_dirs)
bu restore-test photos

# See which snapshots a retention policy would drop (deletes nothing)
bu prune notes

# snapshot destinations restore from the newest snapshot by default;
# a timestamp PATH picks an older one
bu restore notes ./restore 2026-08-15-21.09.41

# View action history
bu history photos

# Show usage
bu help backup
```

## License

MIT — see [LICENSE](LICENSE). The icon source (`assets/icons/bu-icon.svg`) and
its raster set (PNG sizes + `favicon.ico`) are covered by the same licence.
