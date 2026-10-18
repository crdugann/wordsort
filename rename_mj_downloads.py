#!/usr/bin/env python3
"""
rename_mj_downloads.py — Rename Midjourney downloads to word.png for import
==========================================================================
Midjourney exports usually have random names. This tool renames (or copies)
them into the Word Sort Builder import format: man.png, sat.png, …

Recommended workflow
--------------------
1. Export Midjourney prompts from the builder (or use data/mj_discord_L1-7.txt).
2. Generate batches in Discord in the **same order** as the word list.
3. Download the preferred upscales into one folder (keep download order / times).
4. Run:

   python rename_mj_downloads.py --words data/mj_words_L1-7_order.txt ^
       --inbox \"%USERPROFILE%\\Downloads\\mj_raw\" ^
       --out cards_inbox --by mtime --dry-run

5. Check the dry-run mapping, then drop --dry-run.
6. In the builder: Import Midjourney images → select the renamed folder files.

Modes
-----
  --by mtime   Sort inbox images by modified time (oldest first) and zip with words
  --by name    Sort inbox images by filename, then zip with words
  --map FILE   CSV with columns: source,word  (explicit mapping; ignores order)

Examples
--------
  python rename_mj_downloads.py --words data/mj_words_L1-7_order.txt --inbox ./mj_raw --out ./mj_named --by mtime
  python rename_mj_downloads.py --map my_map.csv --inbox ./mj_raw --out ./mj_named
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def slug(word: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", word.strip().lower()).strip("_")
    return s or "word"


def load_words(path: Path) -> list[str]:
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # allow "man" or "man.png" or "1. man"
        line = re.sub(r"^\d+[\.)]\s*", "", line)
        line = Path(line).stem if "." in line else line
        words.append(line.strip().lower())
    return words


def load_map(path: Path) -> list[tuple[str, str]]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = {*(reader.fieldnames or [])}
        src_key = "source" if "source" in fields else ("filename" if "filename" in fields else None)
        word_key = "word" if "word" in fields else None
        if not src_key or not word_key:
            raise SystemExit("Map CSV needs columns: source,word (or filename,word)")
        for r in reader:
            src = (r.get(src_key) or "").strip()
            word = (r.get(word_key) or "").strip().lower()
            if src and word:
                rows.append((src, word))
    return rows


def list_images(inbox: Path) -> list[Path]:
    files = [p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="Rename Midjourney downloads to word.png")
    ap.add_argument("--inbox", type=Path, required=True, help="Folder of MJ downloads")
    ap.add_argument("--out", type=Path, required=True, help="Output folder for word.png files")
    ap.add_argument("--words", type=Path, help="Ordered word list (one per line)")
    ap.add_argument("--map", type=Path, dest="map_csv", help="CSV mapping source,word")
    ap.add_argument("--by", choices=("mtime", "name"), default="mtime")
    ap.add_argument("--copy", action="store_true", help="Copy instead of move")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.inbox.exists():
        sys.exit(f"Inbox not found: {args.inbox}")

    pairs: list[tuple[Path, str]] = []

    if args.map_csv:
        mapping = load_map(args.map_csv)
        by_name = {p.name: p for p in list_images(args.inbox)}
        by_stem = {p.stem: p for p in list_images(args.inbox)}
        for src, word in mapping:
            src_path = Path(src)
            hit = by_name.get(src_path.name) or by_stem.get(src_path.stem)
            if hit is None and (args.inbox / src_path.name).exists():
                hit = args.inbox / src_path.name
            if hit is None:
                print(f"WARN: no inbox file for {src}")
                continue
            pairs.append((hit, word))
    else:
        if not args.words:
            sys.exit("Provide --words (ordered list) or --map CSV")
        words = load_words(args.words)
        images = list_images(args.inbox)
        if args.by == "mtime":
            images.sort(key=lambda p: p.stat().st_mtime)
        else:
            images.sort(key=lambda p: p.name.lower())
        if len(images) < len(words):
            sys.exit(
                f"Need {len(words)} images for {len(words)} words; "
                f"found {len(images)} in {args.inbox}"
            )
        if len(images) > len(words):
            print(
                f"NOTE: {len(images)} images vs {len(words)} words — "
                f"using the first {len(words)} by {args.by}."
            )
            images = images[: len(words)]
        pairs = list(zip(images, words))

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{'DRY-RUN ' if args.dry_run else ''}Mapping ({len(pairs)}):")
    used_out = set()
    for src, word in pairs:
        dest_name = f"{slug(word)}.png"
        if dest_name in used_out:
            print(f"ERROR: duplicate target {dest_name}")
            return 1
        used_out.add(dest_name)
        dest = args.out / dest_name
        # keep original extension if not png? Builder accepts jpeg too, but prefer .png name
        if src.suffix.lower() != ".png":
            dest = args.out / f"{slug(word)}{src.suffix.lower()}"
        print(f"  {src.name}  ->  {dest.name}")
        if args.dry_run:
            continue
        if args.copy:
            shutil.copy2(src, dest)
        else:
            shutil.move(str(src), str(dest))

    if not args.dry_run:
        print(f"Wrote {len(pairs)} file(s) to {args.out}")
        print("Next: builder → Import Midjourney images → select these files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
