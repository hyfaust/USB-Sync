# USB Sync Toolkit

USB Sync Toolkit is a small Windows/Linux-friendly file synchronization suite for moving data between a USB drive and multiple computers.

It consists of two scripts:

- `usb_sync.py` — synchronizes folders and commits target changes into Git
- `usb_rollback.py` — lists backup points and restores previous versions
- `gen_section.py` / `gen_section.pl` / `gen_section.awk` — generate a sync section from a path list

## Features

- Folder-only sync
- Multiple independent sync groups in one INI file
- Source-side backups only
- Git-based target history
- Ignore rules with wildcard and regex support
- `!` negation rules for force-tracking specific files
- Separate log files per target folder
- Interactive completion summary and pause

## Requirements

- Python 3.10+
- Git installed and available in `PATH`

## Configuration

Use a single `config.ini` file with one `[global]` section and one or more sync sections.

### Example

```ini
[global]
log_file_dir = ./logs
ignore = *
backup_limit = 5

[docs]
sources = ./dira, ./dirb
target = ./target_docs
ignore = *,!readme.txt

[code]
sources = ./code_a, ./code_b
target = ./target_code
backup_limit = 10
```

### Rules

- `sources` and `target` must be folders
- Omitted keys inherit from `[global]`
- A blank key means "do not inherit"
- `ignore = *` ignores everything
- `!` re-includes matched files
- Relative paths are resolved from the config file directory first

## Synchronization

Run:

```bash
python usb_sync.py --config path/to/config.ini
```

Behavior:

- The newest file version wins by modification time
- Missing sources are skipped
- Source-side backups are created before overwrite or delete
- Target changes are committed to Git
- The first successful sync creates an initial `sync init` commit

## Rollback

Run:

```bash
python usb_rollback.py list --config path/to/config.ini
python usb_rollback.py restore --config path/to/config.ini
```

### List

`list` shows:

- Git commits for each target
- Source backup files with human-readable timestamps

### Restore

`restore` supports:

- all groups
- one group
- only targets
- only sources
- one specific source

Target rollback uses Git and moves back one commit at a time.
Source rollback restores files from the latest available backup for each file.

## Output Files

- Logs are written into the configured log directory
- Each target produces one log file named `sync_<target>.log`
- Source backups are stored beside each source folder in a `.source_name.sync_backups` directory

## Notes

- Target-side backup folders are disabled
- Target history is expected to be recovered through Git
- The scripts are designed for local/offline USB workflows

## Section Generator

The generator scripts read a text file where each line is either a full path or a plain file name.
They deduplicate entries, infer the common parent folder from the first absolute path, and output one sync section ready to paste into `config.ini`.
