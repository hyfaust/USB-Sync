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
import sys
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
    rollback_id: str
    timestamp: datetime
    size: int


@dataclass(frozen=True)
class TargetCommitEntry:
    target_root: Path
    rollback_id: str
    timestamp: datetime
    commit_hash: str
    short_hash: str
    timestamp_text: str
    subject: str


@dataclass(frozen=True)
class RollbackSelector:
    section_name: Optional[str]
    endpoint_kind: str
    source_index: Optional[int]


@dataclass(frozen=True)
class GroupScope:
    include_target: bool
    source_roots: List[Path]
    has_target_selector: bool
    has_source_selector: bool


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
            "--scope",
            action="append",
            default=None,
            help="Select target, target.1, source, source.N, or section-scoped forms like docs.source.1.",
        )
        command_parser.add_argument(
            "--to",
            default=None,
            help="Rollback identifier from list output, or a commit hash for targets.",
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


def parse_scope_selector(raw: str) -> RollbackSelector:
    text = raw.strip()
    if not text:
        raise ValueError("Scope selector cannot be empty.")

    match = re.fullmatch(r"(?:(?P<section>.+)\.)?(?P<kind>target|source)(?:\.(?P<index>\d+))?", text)
    if not match:
        raise ValueError(
            "The --scope selector must be one of: target, target.1, source, source.1, source.N, section.target, "
            "section.source, or section.source.N."
        )

    section_name = match.group("section")
    endpoint_kind = match.group("kind")
    index_text = match.group("index")

    if endpoint_kind == "target":
        if index_text not in (None, "1"):
            raise ValueError("The target selector only supports target or target.1.")
        return RollbackSelector(section_name=section_name, endpoint_kind="target", source_index=None)

    source_index = None if index_text is None else int(index_text)
    if source_index is not None and source_index <= 0:
        raise ValueError("The source selector index must be greater than 0.")
    return RollbackSelector(section_name=section_name, endpoint_kind="source", source_index=source_index)


def select_groups(settings: usb_sync.Settings, scopes: Optional[Sequence[str]]) -> List[usb_sync.SyncGroup]:
    if not scopes:
        return list(settings.groups)

    selectors = [parse_scope_selector(scope) for scope in scopes]
    selected: List[usb_sync.SyncGroup] = []
    for group in settings.groups:
        if any(selector.section_name is None or selector.section_name == group.name for selector in selectors):
            selected.append(group)
    return selected


def resolve_source_roots(group: usb_sync.SyncGroup, selector: RollbackSelector) -> List[Path]:
    existing_sources = [source for source in group.sources if source.exists()]
    if selector.source_index is None:
        return existing_sources

    for candidate_index in range(selector.source_index, 0, -1):
        if candidate_index > len(group.sources):
            continue
        source_root = group.sources[candidate_index - 1]
        if source_root.exists():
            if candidate_index != selector.source_index:
                print(
                    f"[{group.name}] source.{selector.source_index} fell back to source.{candidate_index}: {source_root}"
                )
            return [source_root]

    raise ValueError(
        f"[{group.name}] source.{selector.source_index} is unavailable and no earlier source exists."
    )


def resolve_group_scope(group: usb_sync.SyncGroup, raw_scopes: Optional[Sequence[str]]) -> GroupScope:
    if not raw_scopes:
        return GroupScope(True, [source for source in group.sources if source.exists()], True, True)

    selectors = [parse_scope_selector(scope) for scope in raw_scopes]
    relevant = [selector for selector in selectors if selector.section_name is None or selector.section_name == group.name]
    if not relevant:
        return GroupScope(False, [], False, False)

    include_target = False
    target_selected = False
    source_roots: List[Path] = []
    source_selected = False
    for selector in relevant:
        if selector.endpoint_kind == "target":
            include_target = True
            target_selected = True
            continue
        source_selected = True
        try:
            source_roots.extend(resolve_source_roots(group, selector))
        except ValueError as exc:
            print(str(exc))

    unique_sources: List[Path] = []
    for source_root in source_roots:
        if source_root not in unique_sources:
            unique_sources.append(source_root)
    return GroupScope(include_target, unique_sources, target_selected, source_selected)


def parse_backup_timestamp(path: Path) -> datetime:
    match = BACKUP_NAME_RE.match(path.name)
    if match is None:
        raise ValueError(f"Invalid backup file name: {path.name}")
    return datetime.strptime(match.group("ts"), "%Y%m%dT%H%M%S%f")


def format_backup_rollback_id(timestamp: datetime) -> str:
    return timestamp.strftime("%Y%m%dT%H%M%S%f")


def format_commit_rollback_id(timestamp: datetime) -> str:
    normalized = timestamp.replace(microsecond=0)
    return normalized.strftime("%Y%m%dT%H%M%S%f")


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
                rollback_id=format_backup_rollback_id(timestamp),
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
        timestamp = datetime.strptime(parts[2], "%Y-%m-%d %H:%M:%S")
        entries.append(
            TargetCommitEntry(
                target_root=target_root,
                rollback_id=format_commit_rollback_id(timestamp),
                timestamp=timestamp,
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


def print_target_list(group: usb_sync.SyncGroup, detailed: bool) -> None:
    history = list_target_history(group.target)
    print("  target commits:")
    if not history:
        print("    (none)")
        return
    for entry in history:
        if detailed:
            print(f"    {entry.rollback_id} | {entry.timestamp_text} | {entry.short_hash} | {entry.commit_hash} | {entry.subject}")
        else:
            print(f"    {entry.rollback_id} | {entry.short_hash} | {entry.subject}")


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
            f"    {marker} {entry.rollback_id} | {entry.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')} | "
            f"{entry.original_path.as_posix()} | {entry.backup_file.name}"
        )


def list_backups(settings: usb_sync.Settings, args: argparse.Namespace) -> None:
    groups = select_groups(settings, args.scope)
    for group in groups:
        try:
            scope = resolve_group_scope(group, args.scope)
            print_group_header(group)
            if scope.include_target:
                print_target_list(group, detailed=not scope.has_source_selector)
            for source_root in scope.source_roots:
                print_source_list(group, source_root)
        except ValueError as exc:
            print(str(exc))


def resolve_target_revision(history: Sequence[TargetCommitEntry], to_value: Optional[str]) -> Optional[TargetCommitEntry]:
    if not history:
        return None

    if to_value is None:
        return history[1] if len(history) > 1 else None

    normalized = to_value.strip()
    for entry in history:
        if normalized in {entry.rollback_id, entry.commit_hash, entry.short_hash, entry.timestamp_text}:
            return entry
        if entry.commit_hash.startswith(normalized):
            return entry
    return None


def prompt_target_restore(group: usb_sync.SyncGroup, entry: Optional[TargetCommitEntry]) -> bool:
    if sys.stdin is not None and not sys.stdin.isatty():
        raise RuntimeError(f"Target restore for [{group.name}] requires an interactive terminal.")

    if entry is None:
        prompt = f"[{group.name}] reset target to the previous commit? [y/N]: "
    else:
        prompt = f"[{group.name}] reset target to {entry.rollback_id} ({entry.short_hash} | {entry.subject})? [y/N]: "

    response = input(prompt).strip().lower()
    return response in {"y", "yes"}


def restore_target(group: usb_sync.SyncGroup, to_value: Optional[str]) -> None:
    target = group.target
    usb_sync.ensure_git_safe_directory(target)
    if not (target / ".git").exists():
        print(f"[{group.name}] target has no Git repository: {target}")
        return

    history = list_target_history(target)
    if not history:
        print(f"[{group.name}] target has no Git history: {target}")
        return

    selected = resolve_target_revision(history, to_value)
    if to_value is None and selected is None:
        print(f"[{group.name}] target has no previous commit: {target}")
        return
    if to_value is not None and selected is None:
        print(f"[{group.name}] target revision not found: {to_value}")
        return

    if not prompt_target_restore(group, selected):
        print(f"[{group.name}] target restore cancelled.")
        return

    revision = selected.commit_hash if selected is not None else "HEAD~1"
    usb_sync.run_command(["git", "-C", str(target), "reset", "--hard", revision])
    usb_sync.run_command(["git", "-C", str(target), "clean", "-fd"])
    if selected is None:
        print(f"[{group.name}] target restored to previous commit: {target}")
    else:
        print(f"[{group.name}] target restored to {selected.rollback_id}: {target}")


def select_source_entries(entries: Sequence[SourceBackupEntry], to_value: Optional[str]) -> List[SourceBackupEntry]:
    if to_value is None:
        latest = latest_source_backups(entries)
        return list(latest.values())

    normalized = to_value.strip()
    selected = [
        entry
        for entry in entries
        if normalized in {entry.rollback_id, entry.backup_file.name, entry.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")}
    ]
    return selected


def restore_source(source_root: Path, to_value: Optional[str]) -> None:
    backup_root = source_backup_root(source_root)
    if not backup_root.exists():
        print(f"source has no backup directory: {source_root}")
        return

    temp_group = usb_sync.SyncGroup("", [], Path("."), None, None, 0, [])
    entries = gather_source_backups(temp_group, source_root)
    if not entries:
        print(f"source has no backup files: {source_root}")
        return

    selected = select_source_entries(entries, to_value)
    if not selected:
        print(f"source revision not found: {to_value}")
        return

    for entry in selected:
        destination = source_root / entry.original_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry.backup_file, destination)
        print(f"restored {destination} from {entry.backup_file.name}")


def restore_backups(settings: usb_sync.Settings, args: argparse.Namespace) -> None:
    groups = select_groups(settings, args.scope)

    for group in groups:
        try:
            scope = resolve_group_scope(group, args.scope)
            print_group_header(group)
            if scope.include_target:
                restore_target(group, args.to)
            for source_root in scope.source_roots:
                restore_source(source_root, args.to)
        except ValueError as exc:
            print(str(exc))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings(resolve_config_path(args.config))

    if args.scope:
        selector_sections = {selector.section_name for selector in (parse_scope_selector(scope) for scope in args.scope) if selector.section_name is not None}
        known_sections = {group.name for group in settings.groups}
        missing_sections = sorted(section for section in selector_sections if section not in known_sections)
        if missing_sections:
            raise ValueError(f"Unknown sync section(s): {', '.join(missing_sections)}")

    if args.command == "list":
        list_backups(settings, args)
        return 0
    if args.command == "restore":
        restore_backups(settings, args)
        return 0
    raise RuntimeError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
