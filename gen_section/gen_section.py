#!/usr/bin/env python3
"""
Generate one INI section from a list of file paths or file names.

Input rules:
- Each line is one path or one plain file name.
- Quoted lines are accepted.
- All absolute paths must share the same parent directory.
- Relative names are treated as files under that parent directory.
"""

from __future__ import annotations

import argparse
import ntpath
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def normalize_line(raw: str) -> str:
    line = raw.strip()
    if len(line) >= 2 and line[0] == line[-1] == '"':
        line = line[1:-1].strip()
    return line


def is_absolute_path(text: str) -> bool:
    return bool(ABSOLUTE_RE.match(text))


def read_lines(path: Optional[Path]) -> List[str]:
    if path is None:
        return [normalize_line(line) for line in sys.stdin.read().splitlines()]
    return [normalize_line(line) for line in path.read_text(encoding="utf-8").splitlines()]


def dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def resolve_base_path(items: Sequence[str]) -> Tuple[str, str]:
    base_parent: Optional[str] = None
    base_name: Optional[str] = None
    for item in items:
        if not is_absolute_path(item):
            continue
        parent = ntpath.dirname(item)
        if not parent:
            continue
        normalized_parent = ntpath.normcase(ntpath.normpath(parent))
        if base_parent is None:
            base_parent = parent
            base_name = ntpath.basename(ntpath.normpath(parent))
            continue
        if normalized_parent != ntpath.normcase(ntpath.normpath(base_parent)):
            raise ValueError("All absolute paths must share the same parent directory.")
    if base_parent is None or base_name is None:
        raise ValueError("No absolute path found; cannot determine the common parent directory.")
    return base_parent, base_name


def filename_for_item(item: str, base_parent: str) -> str:
    if is_absolute_path(item):
        normalized = ntpath.normpath(item)
        if ntpath.dirname(normalized) != ntpath.normpath(base_parent):
            raise ValueError("All absolute paths must share the same parent directory.")
        return ntpath.basename(normalized)
    return ntpath.basename(item)


def render_section(items: Sequence[str]) -> str:
    deduped = dedupe_preserve_order(items)
    base_parent, base_name = resolve_base_path(deduped)
    bak_names = dedupe_preserve_order(filename_for_item(item, base_parent) for item in deduped)
    bak_list = ",".join(f"!{name}" for name in bak_names)
    lines = [
        f"[{base_name}]",
        f"sources = {base_parent}",
        f"target = .\\target_{base_name}",
        f"ignore=*,{bak_list}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a sync section from path list.")
    parser.add_argument("input", nargs="?", help="Input txt file. Reads stdin when omitted.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input) if args.input else None
    items = read_lines(input_path)
    print(render_section(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
