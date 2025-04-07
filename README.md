# Vintage Story Server Manager (vs-mgr)

A robust utility for managing, updating, and backing up Vintage Story game servers.

## Introduction

Vintage Story Server Manager is a command-line tool designed to simplify the management of Vintage Story game servers on Linux systems. It provides commands for server information, version checking, and safe updates with automated backups.

## Features

- **Server Updates:** Safely update your server to new versions with automatic backup
- **Backup Management:** Create, manage, and rotate server data backups
- **Version Checking:** Check for new versions and verify your current installation
- **Service Integration:** Works with systemd to manage the server service
- **Dry-Run Mode:** Preview operations without making changes

## Requirements

- **Python:** Requires Python 3.12 or higher
- **Operating System:** Designed for Linux systems with systemd
- **Dependencies:**
  - `packaging` - For version comparison
  - `pydantic` - For configuration validation
  - `requests` - For HTTP operations
  - `rich` - For console output
  - `zstandard` - For compression

### Sudo Requirements

**IMPORTANT:** This tool requires passwordless sudo access for certain operations:

- Starting/stopping the game server service
- Writing to server installation directories
- Changing file ownership

To set up passwordless sudo for the specific commands needed, add the following to your sudoers file (use `visudo`):

```
# Allow vs-mgr to manage the Vintage Story server without a password
yourusername ALL=(ALL) NOPASSWD: /usr/bin/systemctl start vintagestoryserver
yourusername ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop vintagestoryserver
yourusername ALL=(ALL) NOPASSWD: /usr/bin/systemctl status vintagestoryserver
yourusername ALL=(ALL) NOPASSWD: /usr/bin/rsync
yourusername ALL=(ALL) NOPASSWD: /usr/bin/chown
yourusername ALL=(ALL) NOPASSWD: /usr/bin/mkdir
```

Replace `yourusername` with your actual username and `vintagestoryserver` with your configured service name if different.

## Installation

1. Clone this repository:

   ```
   git clone https://github.com/yourusername/vs-mgr.git
   cd vs-mgr
   ```

2. Install dependencies:
   ```
   pip install .
   ```

## Configuration

On first run, generate a default configuration file:

```
python -m main --generate-config
```

This creates a configuration file at `~/.config/vs_manage/config.toml`. Edit this file to match your server setup.

Key configuration options:

- `server_dir`: Path to server installation
- `data_dir`: Path to server data
- `backup_dir`: Path for backups
- `service_name`: Name of the systemd service
- `server_user`: Username:group for file ownership
- `max_backups`: Number of backups to keep

## Usage

### Basic Commands

**Check server information:**

```
python -m main info
```

**Check for available updates:**

```
python -m main check-version
```

**Update the server:**

```
python -m main update 1.19.4
```

### Advanced Options

**Update with options:**

```
python -m main update 1.19.4 --skip-backup --max-backups 5
```

**Preview update without making changes:**

```
python -m main --dry-run update 1.19.4
```

## Security Considerations

- The tool uses `sudo` for operations that require elevated privileges. Ensure permissions are set correctly.
- The Python fallback update method (when `rsync` is unavailable) performs best-effort permission handling.
- For security-sensitive environments, review the code and consider running the tool as a dedicated user.

## Troubleshooting

### Common Issues

**Update Fails Due to Permissions:**

- Ensure sudo is properly configured
- Check that the configured user has access to server directories

**Service Won't Start/Stop:**

- Verify the service name in the configuration
- Check the systemd service status manually

**Error: "Could not determine server version":**

- Check server log files exist and are readable

## License

[Your license information here]

## Contributing

[Your contribution guidelines here]
