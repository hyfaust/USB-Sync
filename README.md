# USB Sync Toolkit

USB Sync Toolkit is a small Windows/Linux-friendly file synchronization suite for moving data between a USB drive and multiple computers.

It consists of two scripts:

- `usb_sync.py` — synchronizes folders and commits target changes into Git
- `usb_rollback.py` — lists backup points and restores previous versions
- `gen_section.py` / `gen_section.pl` / `gen_section.awk` — generate a sync section from a path list

## Features

- Folder-only sync
- Multiple independent sync groups in one INI file
- Optional source-side backups via CLI flag
- Optional section preference override via CLI flag
- Git-based target history
- Ignore rules with wildcard and regex support
- `!` negation rules for force-tracking specific files
- Separate log files per target folder
- Size-based log rotation
- Interactive completion summary

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
log_max_bytes = 1048576

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
- `log_max_bytes` controls the size threshold for rotating log files
- `--source-backup` enables source-side `.bak` backups for that run

## Synchronization

Run:

```bash
python usb_sync.py --config path/to/config.ini
python usb_sync.py --config path/to/config.ini --source-backup
python usb_sync.py --config path/to/config.ini --prefer target
python usb_sync.py --config path/to/config.ini --prefer source.1
python usb_sync.py --config path/to/config.ini --prefer docs.source.1
```

Behavior:

- The newest file version wins by modification time
- Missing sources are skipped
- Source-side backups are created only when `--source-backup` is passed
- `--prefer target` and `--prefer target.1` are equivalent
- `--prefer source.1` prefers the first source in each section
- `--prefer source.N` falls back to `source.N-1`, then lower sources, until one exists
- `--prefer docs.source.1` applies only to the `docs` section
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
- When enabled, source backups are stored beside each source folder in a `.source_name.sync_backups` directory

## Notes

- Target-side backup folders are disabled
- Source backups are disabled by default
- Target history is expected to be recovered through Git
- The scripts are designed for local/offline USB workflows

## Section Generator

The generator scripts read a text file where each line is either a full path or a plain file name.
They deduplicate entries, infer the common parent folder from the first absolute path, and output one sync section ready to paste into `config.ini`.

### Python

```bash
python gen_section.py input.txt > output.ini
```

### Perl

```bash
perl gen_section.pl input.txt > output.ini
```

### awk

```bash
awk -f gen_section.awk input.txt > output.ini
```

### Windows CMD batch processing

Process every `*.txt` file in the current directory:

```cmd
gen_sections.bat
```

Or call the generators directly:

```cmd
for %f in (*.txt) do python gen_section.py "%f" >> output.ini
for %f in (*.txt) do perl gen_section.pl "%f" >> output.ini
for %f in (*.txt) do awk -f gen_section.awk "%f" >> output.ini
```

If you put the command in a `.bat` file, replace `%f` with `%%f`:

```bat
for %%f in (*.txt) do python gen_section.py "%%f" >> output.ini
for %%f in (*.txt) do perl gen_section.pl "%%f" >> output.ini
for %%f in (*.txt) do awk -f gen_section.awk "%%f" >> output.ini
```
