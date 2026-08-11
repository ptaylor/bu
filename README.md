# bu

Backup utility using rsync (and eventually duplicity) to mirror directories.

## Usage

```
bu <ACTION> <DESTINATION> [OPTIONS...]
```

- **`<ACTION>`** — one of: `backup`, `status`, `config`, `history`
- **`<DESTINATION>`** — a named destination defined in the configuration
- **`[OPTIONS]`** — action-specific flags

### Commands

| Command | Description |
|---------|-------------|
| `bu backup <DEST>` | Mirror source directories to the destination via rsync |
| `bu status <DEST>` | Show config, destination state, and last backup details |
| `bu config [<DEST>]` | Edit (or create) a destination config in `$EDITOR` |
| `bu history <DEST>` | Show action history in reverse chronological order |

Common options: `--dry-run` (`-n`), `--json`, `--config <DIR>`.

## Configuration

Each destination is a separate `.toml` file under `~/.config/bu/` (or `$BU_CONFIG_DIR`).

### Example: `~/.config/bu/photos.toml`

```toml
method = "rsync"
source_paths = ["~/Photos", "~/Camera"]
destination = "/mnt/backup"
history_file = "/home/paul/.local/state/bu/history/photos.json"
log_file = "/home/paul/.local/state/bu/logs/photos.log"
```

### Config reference

| Key | Required | Description |
|-----|----------|-------------|
| `method` | Yes | `"rsync"` or `"duplicity"` |
| `source_paths` | Yes | Array of directory paths to back up |
| `destination` | Yes | Base target directory (files go to `<dest>/<name>/`) |
| `history_file` | No | Path to structured JSON-lines action history |
| `log_file` | No | Path to raw execution log |

### Creating a config

```bash
bu config photos --method rsync
```

Opens `$EDITOR` (default `vi`) with a pre-filled template. If the file doesn't
exist, it's created with sample values. After saving, the TOML is validated.

### State files

Per-destination runtime files are stored under `~/.local/state/bu/`:

```
~/.local/state/bu/
├── history/<name>.json    # Structured action history (JSON-lines)
└── logs/<name>.log        # Raw execution output per run
```

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
bu config photos --method rsync

# See what would be backed up
bu backup photos --dry-run

# Run a backup
bu backup photos

# Check backup status
bu status photos

# View action history
bu history photos

# Machine-readable output
bu status photos --json
bu history photos --json
```

## License

MIT — see [LICENSE](LICENSE).
