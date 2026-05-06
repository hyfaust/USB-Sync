#!/usr/bin/env python3
"""
Rollback utility for usb_sync.py.

Supports git-like list and restore commands for sync groups defined in the
same INI configuration used by the sync script.
"""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import usb_sync


BACKUP_NAME_RE = re.compile(r"^(?P<stem>.+)\.(?P<ts>\d{8}T\d{6}\d{6})\.bak$")


@dataclass(frozen=True)
class SourceBackupEntry:
    group_name: str
    source_root: Path
    backup_root: Path
    original_path: Path
    backup_file: Path
    timestamp: datetime
    size: int


@dataclass(frozen=True)
class TargetCommitEntry:
    target_root: Path
    commit_hash: str
    short_hash: str
    timestamp_text: str
    subject: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rollback synchronized folders.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=None,
        help="Path to config.ini. Defaults to config.ini next to the script.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_scope_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--group",
            action="append",
            default=None,
            help="Sync group name to scope to. Repeatable.",
        )
        command_parser.add_argument(
            "--targets-only",
            action="store_true",
            help="Operate only on target folders.",
        )
        command_parser.add_argument(
            "--sources-only",
            action="store_true",
            help="Operate only on source folders.",
        )
        command_parser.add_argument(
            "--source",
            action="append",
            default=None,
            help="Specific source folder path to scope to. Repeatable.",
        )

    add_scope_arguments(subparsers.add_parser("list", parents=[common], help="List rollback points"))
    add_scope_arguments(subparsers.add_parser("restore", parents=[common], help="Restore rollback points"))
    return parser


def resolve_config_path(raw_config: Optional[str]) -> Path:
    return usb_sync.resolve_config_path(raw_config)


def load_settings(config_path: Path) -> usb_sync.Settings:
    return usb_sync.load_settings(config_path)


def select_groups(settings: usb_sync.Settings, names: Optional[Sequence[str]]) -> List[usb_sync.SyncGroup]:
    if not names:
        return list(settings.groups)

    by_name = {group.name: group for group in settings.groups}
    selected: List[usb_sync.SyncGroup] = []
    missing: List[str] = []
    for name in names:
        group = by_name.get(name)
        if group is None:
            missing.append(name)
            continue
        if group not in selected:
            selected.append(group)

    if missing:
        raise ValueError(f"Unknown sync group(s): {', '.join(missing)}")
    return selected


def resolve_relative_input(raw_value: str, config_path: Path) -> Path:
    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()
    config_dir = config_path.resolve().parent
    return usb_sync.resolve_relative_path(raw_value, script_dir=script_dir, cwd=cwd, config_dir=config_dir)


def select_sources(
    group: usb_sync.SyncGroup,
    config_path: Path,
    raw_sources: Optional[Sequence[str]],
) -> List[Path]:
    if not raw_sources:
        return list(group.sources)

    configured = {source.resolve(strict=False): source for source in group.sources}
    selected: List[Path] = []
    missing: List[str] = []
    for raw in raw_sources:
        candidate = resolve_relative_input(raw, config_path).resolve(strict=False)
        source = configured.get(candidate)
        if source is None:
            missing.append(raw)
            continue
        if source not in selected:
            selected.append(source)

    if missing:
        raise ValueError(
            f"Source path(s) not found in group [{group.name}]: {', '.join(missing)}"
        )
    return selected


def selected_endpoint_mode(args: argparse.Namespace) -> str:
    if args.targets_only and args.sources_only:
        raise ValueError("--targets-only and --sources-only cannot be used together.")
    if args.targets_only and args.source:
        raise ValueError("--targets-only cannot be combined with --source.")
    if args.targets_only:
        return "targets"
    if args.sources_only:
        return "sources"
    if args.source:
        return "sources"
    return "both"


def parse_backup_timestamp(path: Path) -> datetime:
    match = BACKUP_NAME_RE.match(path.name)
    if match is None:
        raise ValueError(f"Invalid backup file name: {path.name}")
    return datetime.strptime(match.group("ts"), "%Y%m%dT%H%M%S%f")


def parse_backup_original_path(path: Path) -> Path:
    match = BACKUP_NAME_RE.match(path.name)
    if match is None:
        raise ValueError(f"Invalid backup file name: {path.name}")
    return path.parent / match.group("stem")


def source_backup_root(source_root: Path) -> Path:
    return usb_sync.backup_root_for(source_root)


def gather_source_backups(group: usb_sync.SyncGroup, source_root: Path) -> List[SourceBackupEntry]:
    backup_root = source_backup_root(source_root)
    entries: List[SourceBackupEntry] = []
    if not backup_root.exists():
        return entries

    for backup_file in backup_root.rglob("*"):
        if not backup_file.is_file():
            continue
        match = BACKUP_NAME_RE.match(backup_file.name)
        if match is None:
            continue
        timestamp = datetime.strptime(match.group("ts"), "%Y%m%dT%H%M%S%f")
        original_rel = backup_file.relative_to(backup_root).with_name(match.group("stem"))
        entries.append(
            SourceBackupEntry(
                group_name=group.name,
                source_root=source_root,
                backup_root=backup_root,
                original_path=original_rel,
                backup_file=backup_file,
                timestamp=timestamp,
                size=backup_file.stat().st_size,
            )
        )

    entries.sort(key=lambda item: (item.original_path.as_posix(), item.timestamp), reverse=True)
    return entries


def latest_source_backups(entries: Sequence[SourceBackupEntry]) -> Dict[str, SourceBackupEntry]:
    latest: Dict[str, SourceBackupEntry] = {}
    for entry in entries:
        key = entry.original_path.as_posix()
        current = latest.get(key)
        if current is None or entry.timestamp > current.timestamp:
            latest[key] = entry
    return latest


def list_target_history(target_root: Path) -> List[TargetCommitEntry]:
    usb_sync.ensure_git_safe_directory(target_root)
    if not (target_root / ".git").exists():
        return []

    result = usb_sync.run_command(
        [
            "git",
            "-C",
            str(target_root),
            "-c",
            "core.quotepath=false",
            "log",
            "--date=format:%Y-%m-%d %H:%M:%S",
            "--format=%H%x1f%h%x1f%ad%x1f%s",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    entries: List[TargetCommitEntry] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        entries.append(
            TargetCommitEntry(
                target_root=target_root,
                commit_hash=parts[0],
                short_hash=parts[1],
                timestamp_text=parts[2],
                subject=parts[3],
            )
        )
    return entries


def print_group_header(group: usb_sync.SyncGroup) -> None:
    print(f"[{group.name}]")
    print(f"  target: {group.target}")


def print_target_list(group: usb_sync.SyncGroup) -> None:
    history = list_target_history(group.target)
    print("  target commits:")
    if not history:
        print("    (none)")
        return
    for entry in history:
        print(f"    {entry.timestamp_text} | {entry.short_hash} | {entry.subject}")


def print_source_list(group: usb_sync.SyncGroup, source_root: Path) -> None:
    entries = gather_source_backups(group, source_root)
    print(f"  source: {source_root}")
    print(f"    backup root: {source_backup_root(source_root)}")
    if not entries:
        print("    (none)")
        return
    latest = latest_source_backups(entries)
    for entry in entries:
        marker = "*" if latest.get(entry.original_path.as_posix()) == entry else " "
        print(
            f"    {marker} {entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')} | "
            f"{entry.original_path.as_posix()} | {entry.backup_file.name}"
        )


def list_backups(settings: usb_sync.Settings, args: argparse.Namespace) -> None:
    groups = select_groups(settings, args.group)
    endpoint_mode = selected_endpoint_mode(args)

    for group in groups:
        print_group_header(group)
        if endpoint_mode in {"both", "targets"}:
            print_target_list(group)
        if endpoint_mode in {"both", "sources"}:
            sources = select_sources(group, settings.config_path, args.source)
            for source_root in sources:
                print_source_list(group, source_root)


def restore_target(group: usb_sync.SyncGroup) -> None:
    target = group.target
    usb_sync.ensure_git_safe_directory(target)
    if not (target / ".git").exists():
        print(f"[{group.name}] target has no Git repository: {target}")
        return

    parent_check = usb_sync.run_command(["git", "-C", str(target), "rev-parse", "--verify", "HEAD~1"], check=False)
    if parent_check.returncode != 0:
        print(f"[{group.name}] target has no previous commit: {target}")
        return

    usb_sync.run_command(["git", "-C", str(target), "reset", "--hard", "HEAD~1"])
    usb_sync.run_command(["git", "-C", str(target), "clean", "-fd"])
    print(f"[{group.name}] target restored to previous commit: {target}")


def restore_source(source_root: Path) -> None:
    backup_root = source_backup_root(source_root)
    if not backup_root.exists():
        print(f"source has no backup directory: {source_root}")
        return

    temp_group = usb_sync.SyncGroup("", [], Path("."), None, None, 0, [])
    entries = gather_source_backups(temp_group, source_root)
    if not entries:
        print(f"source has no backup files: {source_root}")
        return

    latest = latest_source_backups(entries)
    for entry in latest.values():
        destination = source_root / entry.original_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry.backup_file, destination)
        print(f"restored {destination} from {entry.backup_file.name}")


def restore_backups(settings: usb_sync.Settings, args: argparse.Namespace) -> None:
    groups = select_groups(settings, args.group)
    endpoint_mode = selected_endpoint_mode(args)

    for group in groups:
        print_group_header(group)
        if endpoint_mode in {"both", "targets"}:
            restore_target(group)
        if endpoint_mode in {"both", "sources"}:
            sources = select_sources(group, settings.config_path, args.source)
            for source_root in sources:
                restore_source(source_root)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings(resolve_config_path(args.config))

    if args.command == "list":
        list_backups(settings, args)
        return 0
    if args.command == "restore":
        restore_backups(settings, args)
        return 0
    raise RuntimeError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
