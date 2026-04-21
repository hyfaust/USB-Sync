#!/usr/bin/env python3
"""
Cross-platform folder sync and local Git versioning.

The script reads a config.ini file with a global section and multiple sync
sections, synchronizes multiple source folders with one target folder per
section, preserves older source-side versions before overwrite or deletion,
and commits target changes into Git.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


DEFAULT_CONFIG_NAME = "config.ini"
DEFAULT_BACKUP_DIR_NAME = ".sync_backups"
DEFAULT_GIT_NAME = "USB Sync"
DEFAULT_GIT_EMAIL = "usb-sync@local"


@dataclass(frozen=True)
class Endpoint:
    root: Path
    is_file: bool
    backup_root: Path


@dataclass(frozen=True)
class FileRecord:
    endpoint_index: int
    full_path: Path
    relative_path: Path
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class SyncChange:
    relative_path: Path
    action: str  # "added", "modified", or "deleted"


@dataclass(frozen=True)
class GlobalSettings:
    log_file_dir: Optional[Path]
    backup_limit: Optional[int]
    ignore_patterns: List[str]


@dataclass(frozen=True)
class SyncGroup:
    name: str
    sources: List[Path]
    target: Path
    log_file_dir: Optional[Path]
    backup_limit: Optional[int]
    ignore_patterns: List[str]


@dataclass(frozen=True)
class Settings:
    global_settings: GlobalSettings
    groups: List[SyncGroup]
    config_path: Path


@dataclass(frozen=True)
class TargetGitState:
    has_repo: bool
    has_commits: bool
    last_commit_epoch_ns: Optional[int]
    tracked_paths: Set[str]


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    message: Optional[str]
    initial_commit: bool


@dataclass(frozen=True)
class SyncOutcome:
    changes: List[SyncChange]
    target_paths: List[Path]
    skipped_sources: List[Path]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize files and commit changes to Git.")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.ini. Defaults to config.ini next to the script.",
    )
    return parser


def setup_logging(log_file: Optional[Path]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )


def run_command(args: Sequence[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            "Command failed: {}\nSTDOUT:\n{}\nSTDERR:\n{}".format(
                " ".join(args), result.stdout.strip(), result.stderr.strip()
            )
        )
    return result


def normalize_text_path(raw: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    return Path(expanded)


def resolve_relative_path(raw: str, script_dir: Path, cwd: Path, config_dir: Path) -> Path:
    candidate = normalize_text_path(raw)
    if candidate.is_absolute():
        return candidate.resolve(strict=False)

    relative_candidates = [
        config_dir / candidate,
        cwd / candidate,
        script_dir / candidate,
    ]
    for path in relative_candidates:
        if path.exists():
            return path.resolve(strict=False)

    return (cwd / candidate).resolve(strict=False)


def split_path_list(raw: str) -> List[str]:
    items: List[str] = []
    for chunk in raw.replace("\r", "\n").replace(";", "\n").split("\n"):
        for piece in chunk.split(","):
            value = piece.strip()
            if value:
                items.append(value)
    return items


def parse_ignore_patterns(raw: str) -> List[str]:
    if not raw.strip():
        return []
    return split_path_list(raw)


def parse_optional_int(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("The config value backup_limit must be an integer.") from exc
    if value <= 0:
        raise ValueError("The config value backup_limit must be greater than 0.")
    return value


def resolve_section_value(section: configparser.SectionProxy, key: str) -> Optional[str]:
    if key not in section:
        return None
    return section.get(key, fallback="").strip()


def resolve_group_setting(
    raw_value: Optional[str],
    inherited_value,
    parser_fn,
    empty_value,
):
    if raw_value is None:
        return inherited_value
    if raw_value == "":
        return empty_value
    return parser_fn(raw_value)


def load_settings(config_path: Path) -> Settings:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")

    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()
    config_dir = config_path.resolve().parent

    global_section = parser["global"] if "global" in parser else None
    global_log_dir = None
    global_backup_limit = None
    global_ignore = []
    if global_section is not None:
        raw_log_dir = resolve_section_value(global_section, "log_file_dir")
        if raw_log_dir is None:
            raw_log_dir = resolve_section_value(global_section, "log_file")
        raw_backup_limit = resolve_section_value(global_section, "backup_limit")
        raw_ignore = resolve_section_value(global_section, "ignore")

        global_log_dir = (
            resolve_relative_path(raw_log_dir, script_dir=script_dir, cwd=cwd, config_dir=config_dir)
            if raw_log_dir
            else None
        )
        global_backup_limit = parse_optional_int(raw_backup_limit)
        global_ignore = parse_ignore_patterns(raw_ignore or "")

    groups: List[SyncGroup] = []
    for section_name in parser.sections():
        if section_name == "global":
            continue

        section = parser[section_name]
        raw_sources = resolve_section_value(section, "sources")
        raw_target = resolve_section_value(section, "target")
        if raw_sources is None or raw_sources == "":
            raise ValueError(f"Section [{section_name}] must define sources.")
        if raw_target is None or raw_target == "":
            raise ValueError(f"Section [{section_name}] must define target.")

        raw_log_dir = resolve_section_value(section, "log_file_dir")
        if raw_log_dir is None:
            raw_log_dir = resolve_section_value(section, "log_file")
        raw_backup_limit = resolve_section_value(section, "backup_limit")
        raw_ignore = resolve_section_value(section, "ignore")

        sources = [
            resolve_relative_path(item, script_dir=script_dir, cwd=cwd, config_dir=config_dir)
            for item in split_path_list(raw_sources)
        ]
        if not sources:
            raise ValueError(f"No valid source paths were found in section [{section_name}].")

        target = resolve_relative_path(raw_target, script_dir=script_dir, cwd=cwd, config_dir=config_dir)
        log_file_dir = resolve_group_setting(
            raw_log_dir,
            global_log_dir,
            lambda value: resolve_relative_path(value, script_dir=script_dir, cwd=cwd, config_dir=config_dir),
            None,
        )
        backup_limit = resolve_group_setting(raw_backup_limit, global_backup_limit, parse_optional_int, None)
        ignore_patterns = resolve_group_setting(
            raw_ignore,
            global_ignore,
            parse_ignore_patterns,
            [],
        )

        groups.append(
            SyncGroup(
                name=section_name,
                sources=sources,
                target=target,
                log_file_dir=log_file_dir,
                backup_limit=backup_limit,
                ignore_patterns=ignore_patterns,
            )
        )

    if not groups:
        raise ValueError("The config must define at least one sync section.")

    return Settings(
        global_settings=GlobalSettings(
            log_file_dir=global_log_dir,
            backup_limit=global_backup_limit,
            ignore_patterns=global_ignore,
        ),
        groups=groups,
        config_path=config_path,
    )


def is_path_inside(candidate: Path, base: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def backup_root_for(endpoint_root: Path) -> Path:
    if endpoint_root.is_file():
        return endpoint_root.parent / f".{endpoint_root.name}.sync_backups"

    if endpoint_root.name:
        return endpoint_root.parent / f".{endpoint_root.name}.sync_backups"

    return endpoint_root / DEFAULT_BACKUP_DIR_NAME


def normalize_relative_path(path: Path) -> str:
    return path.as_posix()


def is_regex_ignore(pattern: str) -> bool:
    return pattern.startswith("regex:") or pattern.startswith("re:")


def parse_ignore_rule(rule: str) -> Optional[Tuple[bool, str]]:
    text = rule.strip()
    if not text:
        return None

    negated = text.startswith("!")
    if negated:
        text = text[1:].strip()
    if not text:
        return None

    return negated, text


def rule_matches(relative_path: Path, pattern: str) -> bool:
    relative_text = normalize_relative_path(relative_path)
    parts = relative_path.parts

    if is_regex_ignore(pattern):
        regex = pattern.split(":", 1)[1]
        compiled = re.compile(regex)
        return compiled.search(relative_text) is not None or any(compiled.search(part) for part in parts)

    wildcard = pattern.replace("\\", "/")
    if "/" in wildcard:
        return fnmatch.fnmatchcase(relative_text, wildcard)

    return fnmatch.fnmatchcase(relative_text, wildcard) or any(fnmatch.fnmatchcase(part, wildcard) for part in parts)


def matches_ignore_pattern(relative_path: Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False

    ignored = False
    for pattern in patterns:
        parsed = parse_ignore_rule(pattern)
        if parsed is None:
            continue

        negated, normalized_pattern = parsed
        if rule_matches(relative_path, normalized_pattern):
            ignored = not negated

    return ignored


def should_skip_relative_path(relative_path: Path, patterns: Sequence[str]) -> bool:
    excluded = {".git", DEFAULT_BACKUP_DIR_NAME}
    if any(part in excluded for part in relative_path.parts):
        return True
    return matches_ignore_pattern(relative_path, patterns)


def iter_files(root: Path, ignore_patterns: Sequence[str]) -> Iterable[Path]:
    if root.is_file():
        if not should_skip_relative_path(Path(root.name), ignore_patterns):
            yield root
        return

    for current_root, dir_names, file_names in os.walk(root):
        current_root_path = Path(current_root)
        dir_names[:] = [dir_name for dir_name in dir_names if dir_name not in {".git", DEFAULT_BACKUP_DIR_NAME}]

        for file_name in file_names:
            relative_file_path = current_root_path.relative_to(root) / file_name
            if should_skip_relative_path(relative_file_path, ignore_patterns):
                continue
            yield current_root_path / file_name


def inventory_endpoint(endpoint_index: int, endpoint: Endpoint, ignore_patterns: Sequence[str]) -> Dict[str, FileRecord]:
    inventory: Dict[str, FileRecord] = {}

    if not endpoint.root.exists():
        return inventory

    if endpoint.is_file:
        raise ValueError(f"Folder-only sync does not support file endpoints: {endpoint.root}")

    for file_path in iter_files(endpoint.root, ignore_patterns):
        stat_result = file_path.stat()
        relative_path = file_path.relative_to(endpoint.root)
        key = relative_path.as_posix()
        inventory[key] = FileRecord(
            endpoint_index=endpoint_index,
            full_path=file_path,
            relative_path=relative_path,
            mtime_ns=stat_result.st_mtime_ns,
            size=stat_result.st_size,
        )

    return inventory


def choose_winner(records: List[FileRecord], target_index: int) -> FileRecord:
    return max(
        records,
        key=lambda record: (
            record.mtime_ns,
            1 if record.endpoint_index == target_index else 0,
            -record.endpoint_index,
        ),
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def backup_timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S%f")


def backup_existing_file(
    destination: Path,
    backup_root: Path,
    relative_path: Path,
    backup_limit: Optional[int],
) -> Optional[Path]:
    if not destination.exists():
        return None

    timestamp = backup_timestamp()
    backup_path = backup_root / relative_path.parent / f"{relative_path.name}.{timestamp}.bak"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(destination, backup_path)
    prune_backups(backup_path.parent, relative_path.name, backup_limit)
    return backup_path


def prune_backups(backup_dir: Path, file_name: str, backup_limit: Optional[int]) -> None:
    if backup_limit is None:
        return

    pattern = re.compile(rf"^{re.escape(file_name)}\.(\d{{8}}T\d{{6}}\d{{6}})\.bak$")
    backups: List[Tuple[datetime, Path]] = []
    for candidate in backup_dir.iterdir():
        if not candidate.is_file():
            continue
        match = pattern.match(candidate.name)
        if not match:
            continue
        backups.append((datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f"), candidate))

    if len(backups) <= backup_limit:
        return

    backups.sort(key=lambda item: item[0])
    excess = len(backups) - backup_limit
    for _, backup_path in backups[:excess]:
        backup_path.unlink()


def delete_file_with_backup(
    destination: Path,
    backup_root: Path,
    relative_path: Path,
    backup_limit: Optional[int],
) -> Optional[Path]:
    if not destination.exists():
        return None

    backup_path = backup_root / relative_path.parent / f"{relative_path.name}.{backup_timestamp()}.bak"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(destination, backup_path)
    destination.unlink()
    prune_backups(backup_path.parent, relative_path.name, backup_limit)
    return backup_path


def to_epoch_ns(epoch_seconds: Optional[int]) -> Optional[int]:
    if epoch_seconds is None:
        return None
    return epoch_seconds * 1_000_000_000


def source_is_newer_than_commit(records: Sequence[FileRecord], last_commit_epoch_ns: Optional[int]) -> bool:
    if not records:
        return False
    if last_commit_epoch_ns is None:
        return True
    return any(record.mtime_ns > last_commit_epoch_ns for record in records)


def copy_file(source: Path, destination: Path) -> None:
    ensure_parent(destination)
    shutil.copy2(source, destination)


def ensure_git_identity(repo_path: Path) -> None:
    try:
        name_result = run_command(["git", "config", "--local", "--get", "user.name"], cwd=repo_path, check=False)
        email_result = run_command(["git", "config", "--local", "--get", "user.email"], cwd=repo_path, check=False)

        if not name_result.stdout.strip():
            run_command(["git", "config", "--local", "user.name", DEFAULT_GIT_NAME], cwd=repo_path)
        if not email_result.stdout.strip():
            run_command(["git", "config", "--local", "user.email", DEFAULT_GIT_EMAIL], cwd=repo_path)
    except RuntimeError as exc:
        raise RuntimeError(f"Failed to configure Git identity in {repo_path}: {exc}") from exc


def ensure_git_repo(target: Path) -> None:
    git_dir = target / ".git"
    if git_dir.exists():
        ensure_git_identity(target)
        return

    logging.info("Initializing Git repository in %s", target)
    run_command(["git", "init"], cwd=target)
    ensure_git_identity(target)


def ensure_backup_ignore(target: Path, backup_root: Path) -> Optional[Path]:
    try:
        backup_root.relative_to(target)
    except ValueError:
        return None

    ignore_path = target / ".gitignore"
    ignore_line = f"{backup_root.name}/"
    if ignore_path.exists():
        content = ignore_path.read_text(encoding="utf-8").splitlines()
    else:
        content = []

    if ignore_line in content:
        return None

    content.append(ignore_line)
    ignore_path.write_text("\n".join(content) + "\n", encoding="utf-8")
    return ignore_path


def ensure_git_safe_directory(target: Path) -> None:
    target_directory = str(target.resolve())
    result = run_command(["git", "config", "--global", "--get-all", "safe.directory"], check=False)
    entries = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    normalized_target = os.path.normcase(target_directory)
    for entry in entries:
        if entry == "*" or os.path.normcase(entry) == normalized_target:
            return

    run_command(["git", "config", "--global", "--add", "safe.directory", target_directory])


def git_repo_exists(target: Path) -> bool:
    return (target / ".git").exists()


def git_has_commits(target: Path) -> bool:
    if not git_repo_exists(target):
        return False

    result = run_command(["git", "rev-parse", "--verify", "HEAD"], cwd=target, check=False)
    return result.returncode == 0


def get_git_state(target: Path) -> TargetGitState:
    has_repo = git_repo_exists(target)
    if not has_repo:
        return TargetGitState(has_repo=False, has_commits=False, last_commit_epoch_ns=None, tracked_paths=set())

    has_commits = git_has_commits(target)
    if not has_commits:
        return TargetGitState(has_repo=True, has_commits=False, last_commit_epoch_ns=None, tracked_paths=set())

    commit_time = run_command(["git", "log", "-1", "--format=%ct", "HEAD"], cwd=target).stdout.strip()
    tracked_output = run_command(
        ["git", "-c", "core.quotepath=false", "ls-tree", "-r", "-z", "--name-only", "HEAD"],
        cwd=target,
    ).stdout
    tracked = tracked_output.split("\0")
    last_commit_epoch_ns = int(commit_time) * 1_000_000_000 if commit_time else None
    tracked_paths = {line.strip() for line in tracked if line.strip()}
    return TargetGitState(
        has_repo=True,
        has_commits=True,
        last_commit_epoch_ns=last_commit_epoch_ns,
        tracked_paths=tracked_paths,
    )


def synchronize(group: SyncGroup, git_state: TargetGitState) -> SyncOutcome:
    if not group.target.exists():
        group.target.mkdir(parents=True, exist_ok=True)

    endpoints: List[Endpoint] = []
    skipped_sources: List[Path] = []
    for source in group.sources:
        if not source.exists():
            skipped_sources.append(source)
            logging.warning("Skipping missing source path: %s", source)
            continue
        if source.is_file():
            raise ValueError(f"Source must be a directory path: {source}")
        endpoints.append(Endpoint(root=source, is_file=source.is_file(), backup_root=backup_root_for(source)))

    if not endpoints:
        logging.warning("No existing source paths were found; only the target repository will be checked.")

    target_endpoint = Endpoint(
        root=group.target,
        is_file=group.target.is_file(),
        backup_root=backup_root_for(group.target),
    )
    if target_endpoint.is_file:
        raise ValueError("Target must be a directory path.")

    endpoints.append(target_endpoint)
    target_index = len(endpoints) - 1

    inventories: List[Dict[str, FileRecord]] = [
        inventory_endpoint(index, endpoint, group.ignore_patterns) for index, endpoint in enumerate(endpoints)
    ]

    merged_keys = sorted((set(git_state.tracked_paths) | {key for inventory in inventories for key in inventory.keys()}))
    target_stage_paths: List[Path] = []
    target_changes: List[SyncChange] = []
    last_commit_epoch_ns = git_state.last_commit_epoch_ns

    for key in merged_keys:
        path = Path(key)
        if should_skip_relative_path(path, group.ignore_patterns):
            continue
        source_records = [inventory[key] for index, inventory in enumerate(inventories[:-1]) if key in inventory]
        target_record = inventories[target_index].get(key)
        tracked_in_target = key in git_state.tracked_paths
        target_missing = target_record is None

        if target_record is not None:
            records = source_records + [target_record]
            winner = choose_winner(records, target_index=target_index)

            for index, endpoint in enumerate(endpoints):
                destination_path = endpoint.root if endpoint.is_file else endpoint.root / path
                destination_record = inventories[index].get(key)
                if destination_record is not None:
                    needs_copy = destination_record.mtime_ns < winner.mtime_ns or (
                        destination_record.mtime_ns == winner.mtime_ns
                        and index == target_index
                        and winner.endpoint_index != target_index
                    )
                else:
                    needs_copy = True

                if not needs_copy or endpoint.root == winner.full_path:
                    continue

                logging.info("Syncing %s -> %s", winner.full_path, destination_path)
                if index != target_index:
                    backup_existing_file(destination_path, endpoint.backup_root, path, group.backup_limit)
                copy_file(winner.full_path, destination_path)

                inventories[index][key] = FileRecord(
                    endpoint_index=index,
                    full_path=destination_path,
                    relative_path=path,
                    mtime_ns=destination_path.stat().st_mtime_ns,
                    size=destination_path.stat().st_size,
                )

                if index == target_index:
                    target_stage_paths.append(path)
                    action = "added" if destination_record is None else "modified"
                    target_changes.append(SyncChange(relative_path=path, action=action))

            if key not in git_state.tracked_paths:
                target_stage_paths.append(path)
                if not any(change.relative_path == path and change.action == "added" for change in target_changes):
                    target_changes.append(SyncChange(relative_path=path, action="added"))

            continue

        if source_records and source_is_newer_than_commit(source_records, last_commit_epoch_ns):
            winner = choose_winner(source_records, target_index=-1)
            for index, endpoint in enumerate(endpoints):
                destination_path = endpoint.root if endpoint.is_file else endpoint.root / path
                destination_record = inventories[index].get(key)
                if destination_record is not None:
                    needs_copy = destination_record.mtime_ns < winner.mtime_ns or (
                        destination_record.mtime_ns == winner.mtime_ns
                        and index == target_index
                        and winner.endpoint_index != target_index
                    )
                else:
                    needs_copy = True

                if not needs_copy or endpoint.root == winner.full_path:
                    continue

                logging.info("Syncing %s -> %s", winner.full_path, destination_path)
                if index != target_index:
                    backup_existing_file(destination_path, endpoint.backup_root, path, group.backup_limit)
                copy_file(winner.full_path, destination_path)
                inventories[index][key] = FileRecord(
                    endpoint_index=index,
                    full_path=destination_path,
                    relative_path=path,
                    mtime_ns=destination_path.stat().st_mtime_ns,
                    size=destination_path.stat().st_size,
                )

                if index == target_index:
                    target_stage_paths.append(path)
                    action = "added" if destination_record is None else "modified"
                    target_changes.append(SyncChange(relative_path=path, action=action))
            continue

        if tracked_in_target:
            for source_record in source_records:
                source_endpoint = endpoints[source_record.endpoint_index]
                logging.info("Deleting %s from %s", path.as_posix(), source_endpoint.root)
                delete_file_with_backup(
                    source_record.full_path,
                    source_endpoint.backup_root,
                    path,
                    group.backup_limit,
                )

            if source_records or target_missing:
                target_stage_paths.append(path)
                target_changes.append(SyncChange(relative_path=path, action="deleted"))
            continue

    if skipped_sources:
        logging.info("Skipped %d missing source path(s).", len(skipped_sources))

    return SyncOutcome(changes=target_changes, target_paths=target_stage_paths, skipped_sources=skipped_sources)


def summarize_changes(changes: List[SyncChange]) -> str:
    counts = {"added": 0, "modified": 0, "deleted": 0}
    for change in changes:
        if change.action in counts:
            counts[change.action] += 1

    summary = (
        f"Sync Update: {counts['added']} added, "
        f"{counts['modified']} modified, {counts['deleted']} deleted"
    )
    if changes:
        sample = ", ".join(str(change.relative_path.as_posix()) for change in changes[:5])
        summary = f"{summary} | {sample}"
    return summary


def format_completion_summary(outcome: SyncOutcome, commit_result: CommitResult) -> str:
    counts = {"added": 0, "modified": 0, "deleted": 0}
    for change in outcome.changes:
        if change.action in counts:
            counts[change.action] += 1

    lines = [
        "complete",
        f"added: {counts['added']}, modified: {counts['modified']}, deleted: {counts['deleted']}",
    ]
    if outcome.skipped_sources:
        lines.append(f"skipped sources: {len(outcome.skipped_sources)}")
    if commit_result.committed and commit_result.message:
        lines.append(f"commit: {commit_result.message}")
    return "\n".join(lines)


def git_commit_if_needed(
    target: Path,
    changed_paths: List[Path],
    changes: List[SyncChange],
    has_commits_before_sync: bool,
) -> CommitResult:
    ensure_git_repo(target)
    ensure_git_safe_directory(target)

    backup_root = backup_root_for(target)
    ignore_path = ensure_backup_ignore(target, backup_root)
    if ignore_path is not None:
        changed_paths = changed_paths + [ignore_path.relative_to(target)]

    if not changed_paths:
        logging.info("No target changes detected; skipping Git commit.")
        return CommitResult(committed=False, message=None, initial_commit=False)

    unique_paths: List[str] = []
    seen = set()
    for path in changed_paths:
        key = path.as_posix()
        if key not in seen:
            seen.add(key)
            unique_paths.append(key)

    logging.info("Staging %d changed paths", len(unique_paths))
    run_command(["git", "add", "-A", "--"] + unique_paths, cwd=target)

    status = run_command(["git", "status", "--porcelain"], cwd=target).stdout.strip()
    if not status:
        logging.info("Git working tree is clean after staging; skipping commit.")
        return CommitResult(committed=False, message=None, initial_commit=False)

    initial_commit = not has_commits_before_sync
    message = "sync init" if initial_commit else summarize_changes(changes)
    logging.info("Committing with message: %s", message)
    commit = run_command(["git", "commit", "-m", message], cwd=target, check=False)
    if commit.returncode != 0:
        raise RuntimeError(
            "Git commit failed.\n"
            f"STDOUT:\n{commit.stdout.strip()}\n"
            f"STDERR:\n{commit.stderr.strip()}"
        )
    return CommitResult(committed=True, message=message, initial_commit=initial_commit)


def resolve_config_path(raw_config: Optional[str]) -> Path:
    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()
    candidates = []

    if raw_config:
        candidate = normalize_text_path(raw_config)
        if candidate.is_absolute():
            return candidate
        candidates.append(cwd / candidate)
        candidates.append(script_dir / candidate)
    else:
        candidates.append(script_dir / DEFAULT_CONFIG_NAME)
        candidates.append(cwd / DEFAULT_CONFIG_NAME)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def sanitize_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "target"


def log_file_for_group(group: SyncGroup) -> Optional[Path]:
    if group.log_file_dir is None:
        return None

    target_name = group.target.name or group.target.anchor.replace(":", "").replace("\\", "").replace("/", "")
    target_part = sanitize_filename_component(target_name)
    return group.log_file_dir / f"sync_{target_part}.log"


def run_group(group: SyncGroup) -> Tuple[SyncOutcome, CommitResult]:
    log_file = log_file_for_group(group)
    setup_logging(log_file)
    ensure_git_safe_directory(group.target)

    logging.info("Group: %s", group.name)
    logging.info("Sources: %s", ", ".join(str(path) for path in group.sources))
    logging.info("Target: %s", group.target)

    git_state = get_git_state(group.target)
    outcome = synchronize(group, git_state)
    commit_result = git_commit_if_needed(
        group.target,
        outcome.target_paths,
        outcome.changes,
        git_state.has_commits,
    )
    logging.info("Sync complete.")
    return outcome, commit_result


def pause_if_interactive() -> None:
    if not sys.stdin.isatty():
        return

    try:
        input("Press Enter to exit...")
    except EOFError:
        return


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config_path = resolve_config_path(args.config)
    settings = load_settings(config_path)

    group_summaries: List[str] = []
    total_changes = {"added": 0, "modified": 0, "deleted": 0}
    for group in settings.groups:
        outcome, commit_result = run_group(group)
        group_summary = format_completion_summary(outcome, commit_result)
        group_summaries.append(f"[{group.name}] {group_summary}")
        for change in outcome.changes:
            if change.action in total_changes:
                total_changes[change.action] += 1

    print("complete")
    print(
        "added: {added}, modified: {modified}, deleted: {deleted}".format(
            added=total_changes["added"],
            modified=total_changes["modified"],
            deleted=total_changes["deleted"],
        )
    )
    for line in group_summaries:
        print(line)

    pause_if_interactive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
