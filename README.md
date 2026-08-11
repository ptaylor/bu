# bu

Backup utility for managing backups to various backends.

## Usage

```
bu <ACTION> <DESTINATION> [OPTIONS...]
```

- **`<ACTION>`** — one of: `backup`, `restore`, `check`, `verify`, `status`
- **`<DESTINATION>`** — a named destination defined in the configuration file
- **`[OPTIONS]`** — action-specific flags and parameters

### Commands

| Command | Description |
|---------|-------------|
| `bu backup <DEST>` | Back up configured source paths to the destination |
| `bu restore <DEST> --to <PATH>` | Restore data from the destination to a local path |
| `bu check <DEST>` | Show what files would be backed up (dry-run diff) |
| `bu verify <DEST>` | Verify integrity of backed-up data |
| `bu status <DEST>` | Show status/summary of the backup destination |

Common options: `--dry-run` (`-n`), `--json`, `--config <PATH>`.

## Configuration

Configuration is stored in TOML format. The default location is:

```
~/.config/bu/config.toml
```

Override with the `$BU_CONFIG_PATH` environment variable or `--config` flag.

### Example config.toml

```toml
# Local filesystem backup
[destinations.docs]
backend = "local"
source_paths = ["~/Documents", "~/notes"]
target_path = "/mnt/backup/docs"

# Amazon S3 backup
[destinations.photos]
backend = "s3"
source_paths = ["~/Photos"]
bucket = "my-photo-backups"
region = "us-east-1"

# Backblaze B2 (S3-compatible)
[destinations.offsite]
backend = "s3"
source_paths = ["~/Projects", "~/Documents"]
bucket = "my-b2-bucket"
endpoint_url = "https://s3.us-west-002.backblazeb2.com"
region = "us-west-002"
access_key = "000..."   # or use AWS_ACCESS_KEY_ID env var
secret_key = "..."      # or use AWS_SECRET_ACCESS_KEY env var
prefix = "laptop-backup"

# rsync over SSH
[destinations.server]
backend = "rsync"
source_paths = ["~/data", "~/configs"]
host = "backup.example.com"
user = "paul"
path = "/backups/home"
port = 22
ssh_key = "~/.ssh/id_rsa"
```

### Backend Configuration Reference

#### `local`

| Key | Required | Description |
|-----|----------|-------------|
| `target_path` | Yes | Destination directory for backups |

#### `s3` (AWS S3, B2, MinIO, etc.)

| Key | Required | Description |
|-----|----------|-------------|
| `bucket` | Yes | S3 bucket name |
| `prefix` | No | Key prefix (folder) within the bucket |
| `region` | No | AWS/S3 region |
| `endpoint_url` | No | Custom endpoint for S3-compatible services |
| `access_key` | No | Access key ID (falls back to AWS env vars) |
| `secret_key` | No | Secret access key (falls back to AWS env vars) |

#### `rsync`

| Key | Required | Description |
|-----|----------|-------------|
| `host` | Yes | Remote hostname or IP |
| `path` | Yes | Remote destination path |
| `user` | No | SSH username (defaults to current user) |
| `port` | No | SSH port (default: 22) |
| `ssh_key` | No | Path to SSH private key |
| `rsync_opts` | No | Array of extra rsync options |

## Installation

### From source

```bash
pip install .
```

### Development install

```bash
pip install -e ".[dev]"
```

## Examples

```bash
# Check what would be backed up
bu check photos

# Run a backup
bu backup photos

# Dry-run a backup to see what would happen
bu backup --dry-run server

# Verify backup integrity
bu verify photos

# Full checksum verification
bu verify --full photos

# Show backup status
bu status photos

# Restore everything to a directory
bu restore photos --to /tmp/restored

# Machine-readable output
bu status photos --json
```

## License

MIT — see [LICENSE](LICENSE).
