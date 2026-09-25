"""Normalize a legacy emote/reaction asset collection for webqueue hosting.

Usage:
    python scripts/migrate_emote_assets.py SEED_DIR DESTINATION_DIR

``SEED_DIR`` must contain ``reactions/`` and ``emotes/``. Reactions are
processed first, preserving their canonical names.  Identical collisions are
deduplicated; distinct emote collisions are numbered (``name2``, ``name3``).
The destination must not already exist.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


IMAGE_EXTENSIONS = frozenset({".gif", ".webp"})


def normalize_name(value: str) -> str:
    name = value.lstrip("#").replace("_", "").replace("-", "").lower()
    if not name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid emote filename stem: {value!r}")
    return name


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate(seed: Path, destination: Path) -> tuple[int, int, int]:
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite destination: {destination}")
    destination.mkdir(parents=True)

    used_names: set[str] = set()
    canonical: dict[str, list[str]] = {}
    copied = deduplicated = suffixed = 0
    for folder in ("reactions", "emotes"):
        source_dir = seed / folder
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Missing source directory: {source_dir}")
        for source in sorted(source_dir.iterdir()):
            if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            bare = normalize_name(source.stem)
            source_hash = _hash(source)
            hashes = canonical.setdefault(bare, [])
            if source_hash in hashes:
                deduplicated += 1
                continue
            candidate = bare
            number = 2
            while candidate in used_names:
                candidate = f"{bare}{number}"
                number += 1
            if candidate != bare:
                suffixed += 1
            used_names.add(candidate)
            hashes.append(source_hash)
            shutil.copy2(source, destination / f"{candidate}{source.suffix.lower()}")
            copied += 1
    return copied, deduplicated, suffixed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    copied, deduplicated, suffixed = migrate(args.seed, args.destination)
    print(f"copied={copied} deduplicated={deduplicated} suffixed={suffixed}")


if __name__ == "__main__":
    main()
