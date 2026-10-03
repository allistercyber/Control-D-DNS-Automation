#!/usr/bin/env python3
"""
Shared configuration loader for the sync scripts.

Both stages read the same ``config.toml`` so the list of upstream files and the
Control D profile/folder targets are defined in exactly one place.

Run directly to validate a config file without touching the network:

    python scripts/controld_config.py [path/to/config.toml]

The file location defaults to ``config.toml`` in the working directory and can
be overridden with the CONTROLD_SYNC_CONFIG environment variable.  See
config.example.toml for the format and CONFIGURATION.md for the reference.
"""

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

DEFAULT_CONFIG_PATH = "config.toml"
CONFIG_ENV_VAR      = "CONTROLD_SYNC_CONFIG"

# Defaults for every optional setting.
DEFAULT_MAX_DELETE_PERCENT = 50
DEFAULT_MIRROR_FALLBACK    = True

# Upstream filenames are used to build file paths and URLs, so only a plain
# "<name>.json" is accepted -- no directory separators, no leading dot.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.json$")

_TOP_LEVEL_KEYS = {"settings", "lists"}
_SETTINGS_KEYS  = {"max_delete_percent", "mirror_fallback"}
_LIST_KEYS      = {"file", "targets"}
_TARGET_KEYS    = {"profile", "folder"}


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class ListEntry:
    """One upstream file and the (profile, folder) pairs it is pushed into."""
    file: str
    targets: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class Config:
    lists: Tuple[ListEntry, ...]
    max_delete_percent: int = DEFAULT_MAX_DELETE_PERCENT
    mirror_fallback: bool = DEFAULT_MIRROR_FALLBACK

    @property
    def target_files(self) -> List[str]:
        """Upstream filenames to download, in configuration order."""
        return [entry.file for entry in self.lists]


def _check_keys(table: Dict[str, Any], allowed: set, where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {unknown}; allowed keys are {sorted(allowed)}"
        )


def _require_str(table: Dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value


def parse_config(data: Dict[str, Any]) -> Config:
    """Validate an already-parsed TOML document and build a Config."""
    _check_keys(data, _TOP_LEVEL_KEYS, "config")

    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise ConfigError("[settings] must be a table")
    _check_keys(settings, _SETTINGS_KEYS, "[settings]")

    max_delete = settings.get("max_delete_percent", DEFAULT_MAX_DELETE_PERCENT)
    # bool is an int subclass in Python; reject it explicitly.
    if isinstance(max_delete, bool) or not isinstance(max_delete, int) \
            or not 1 <= max_delete <= 100:
        raise ConfigError("[settings] max_delete_percent must be an integer from 1 to 100")

    mirror_fallback = settings.get("mirror_fallback", DEFAULT_MIRROR_FALLBACK)
    if not isinstance(mirror_fallback, bool):
        raise ConfigError("[settings] mirror_fallback must be true or false")

    raw_lists = data.get("lists")
    if not isinstance(raw_lists, list) or not raw_lists:
        raise ConfigError("at least one [[lists]] entry is required")

    entries: List[ListEntry] = []
    seen_files: set = set()
    seen_targets: Dict[Tuple[str, str], str] = {}

    for index, raw in enumerate(raw_lists, start=1):
        where = f"[[lists]] #{index}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a table")
        _check_keys(raw, _LIST_KEYS, where)

        filename = _require_str(raw, "file", where)
        if not _FILENAME_RE.match(filename):
            raise ConfigError(
                f"{where}: 'file' must be a plain upstream filename ending in "
                f".json (got {filename!r})"
            )
        if filename in seen_files:
            raise ConfigError(f"{where}: file {filename!r} is listed more than once")
        seen_files.add(filename)

        raw_targets = raw.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ConfigError(f"{where} ({filename}): 'targets' must be a non-empty array")

        targets: List[Tuple[str, str]] = []
        for target in raw_targets:
            if not isinstance(target, dict):
                raise ConfigError(
                    f"{where} ({filename}): each target must be a table with 'profile' and 'folder'"
                )
            _check_keys(target, _TARGET_KEYS, f"{where} ({filename}) target")
            pair = (
                _require_str(target, "profile", f"{where} ({filename}) target"),
                _require_str(target, "folder",  f"{where} ({filename}) target"),
            )
            if pair in seen_targets:
                # Each push reconciles a folder to *exactly* one file's contents,
                # so two files feeding one folder would keep deleting each
                # other's domains.
                raise ConfigError(
                    f"{where} ({filename}): a profile/folder pair is targeted by more "
                    f"than one list (already used by {seen_targets[pair]!r}); a folder "
                    f"can only mirror one upstream file"
                )
            seen_targets[pair] = filename
            targets.append(pair)

        entries.append(ListEntry(file=filename, targets=tuple(targets)))

    return Config(
        lists=tuple(entries),
        max_delete_percent=max_delete,
        mirror_fallback=mirror_fallback,
    )


def load_config(path: str = "") -> Config:
    """Load and validate the config file (argument, then env var, then default)."""
    config_path = Path(path or os.environ.get(CONFIG_ENV_VAR, "") or DEFAULT_CONFIG_PATH)
    if not config_path.is_file():
        raise ConfigError(
            f"configuration file '{config_path}' not found. Copy config.example.toml "
            f"to config.toml and edit it (see CONFIGURATION.md)."
        )
    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"'{config_path}' is not valid TOML: {exc}") from None
    try:
        return parse_config(data)
    except ConfigError as exc:
        raise ConfigError(f"'{config_path}': {exc}") from None


def main() -> None:
    try:
        config = load_config(sys.argv[1] if len(sys.argv) > 1 else "")
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    n_targets = sum(len(entry.targets) for entry in config.lists)
    # Profile/folder names are deliberately not printed: Actions logs are
    # public on public repositories.
    print(
        f"Configuration OK: {len(config.lists)} list(s), {n_targets} target(s), "
        f"max_delete_percent={config.max_delete_percent}, "
        f"mirror_fallback={str(config.mirror_fallback).lower()}"
    )


if __name__ == "__main__":
    main()
