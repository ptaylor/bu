# bu

Backup utility that mirrors directories with rsync, or encrypted incremental
duplicity archives (local disk or Backblaze B2).

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
| `bu backup NAME` | Back up source directories to the destination (rsync or duplicity) |
| `bu restore NAME RESTORE_DIR [PATH]` | Restore files into a local directory (never deletes) |
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
verbosity = 9                                        # 0 = quiet, 9 = verbose
```

### Config reference

| Key | Required | Description |
|-----|----------|-------------|
| `method` | Yes | `"rsync"` or `"duplicity"` |
| `source_paths` | Yes | Array of directory paths to back up |
| `destination` | Yes | Base target directory, or a `b2://bucket/path` URL (duplicity only) |
| `history_file` | No | Path to structured JSON-lines action history |
| `log_file` | No | Path to raw execution log |
| `passphrase_file` | No | duplicity: file whose first line holds the GPG passphrase (perms 0600) |
| `full_if_older_than` | No | duplicity: force a full backup when the last one is older than this (e.g. `"30D"`) |
| `verbosity` | No | duplicity: output verbosity 0-9 (default: automatic) |
| `b2_account_id` / `b2_account_key` | No | Backblaze B2 credentials, plaintext |
| `b2_account_id_enc` / `b2_account_key_enc` | No | Backblaze B2 credentials, encrypted via `bu encrypt` |

For `rsync`, each source is mirrored directly into `destination/<source name>`.
For `duplicity`, each source gets its own encrypted archive under the same
layout.

### Creating a config

```bash
bu create photos
```

The interactive wizard asks for the backup type (Directory or Backblaze B2),
destination, credentials, method, and source paths, then writes the config.
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

# View action history
bu history photos

# Show usage
bu help backup
```

## License

MIT — see [LICENSE](LICENSE).
