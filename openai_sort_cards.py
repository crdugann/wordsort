#!/usr/bin/env python3
"""
openai_sort_cards.py — Phase 2 of Word Sort Builder TYCTR
=========================================================
Generates one OpenAI image card per unique word from a sort_word_bank JSON.

- One card per word (global dedupe across sorts)
- Interactive accept / regenerate / revise / skip loop
- Resumable via cards/manifest.json
- Dry-run needs no API key

Setup
-----
  pip install openai requests
  set OPENAI_API_KEY=sk-...   (do NOT paste the key into chat)

Examples
--------
  python openai_sort_cards.py --bank sort_word_bank_L1.json --dry-run
  python openai_sort_cards.py --bank sort_word_bank_L1.json --sort L1_m_vs_s --limit 4
  python openai_sort_cards.py --bank sort_word_bank_L1.json --words man,sun,map,sand
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

try:
    import requests
except ImportError:
    requests = None  # type: ignore


DEFAULT_STYLE = (
    "flat-color children's picture-book illustration, simple shapes, "
    "warm and inviting, thick clean outlines, plain light background, "
    "no text, no letters, no watermark, no logo"
)

DEFAULT_MODEL = "gpt-image-1"
FALLBACK_MODEL = "dall-e-3"

_HERE = Path(__file__).resolve().parent
_DESKTOP_BIEMILLER = Path(
    r"C:\Users\chris\OneDrive\Desktop\TYCTR Decodables x Midjourney"
    r"\Kuperman X Biemiller - e.rated_biemiller.csv"
)


def _resolve_biemiller() -> Path:
    env = os.environ.get("BIEMILLER_CSV")
    if env:
        return Path(env)
    bundled = _HERE / "data" / "biemiller.csv"
    if bundled.exists():
        return bundled
    return _DESKTOP_BIEMILLER


DEFAULT_BIEMILLER = _resolve_biemiller()


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE). Does not override existing env."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def safe_print(msg: str = "") -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))


def slug(word: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", word.strip().lower()).strip("_")
    return s or "word"


def build_prompt(word: str, spelling: str | None, as_in: str | None, style: str) -> str:
    cue = ""
    if spelling and as_in:
        cue = (
            f" The pictured object should be clearly namable as '{word}' "
            f"(picture-sort card for the /{spelling}/ sound as in {as_in})."
        )
    elif spelling:
        cue = f" The pictured object should be clearly namable as '{word}'."
    return (
        f"Children's picture-sort card: a single {word}, centered, "
        f"one clear object only.{cue} {style}"
    )


# Midjourney-friendly base style (paste into web/Discord; no OpenAI call)
DEFAULT_MJ_STYLE = (
    "flat-color children's picture-book illustration, simple shapes, "
    "warm and inviting, thick clean outlines, plain light background, "
    "single centered object, no text, no letters, no watermark, no logo"
)
DEFAULT_MJ_PARAMS = "--ar 1:1 --stylize 80"


def build_midjourney_prompt(
    word: str,
    spelling: str | None,
    as_in: str | None,
    style: str | None = None,
    params: str | None = None,
) -> str:
    """Prompt line ready to paste into Midjourney (web or Discord)."""
    base = build_prompt(word, spelling, as_in, style or DEFAULT_MJ_STYLE)
    suffix = (params if params is not None else DEFAULT_MJ_PARAMS).strip()
    return f"{base} {suffix}".strip() if suffix else base


def word_from_image_filename(filename: str) -> str:
    """
    Infer word slug from an uploaded Midjourney file name.
    Accepts: man.png, man_final.png, man_v003.png, m_man.png (returns last segment).
    """
    stem = Path(filename).stem.strip().lower()
    stem = re.sub(r"_v\d+$", "", stem, flags=re.I)
    stem = re.sub(r"_final$", "", stem, flags=re.I)
    # optional spelling prefix: m_man → man
    if "_" in stem:
        parts = stem.split("_")
        if len(parts) == 2 and len(parts[0]) <= 3:
            stem = parts[1]
    return stem


def match_item_for_filename(filename: str, items_by_word: dict[str, dict]) -> dict | None:
    key = word_from_image_filename(filename)
    if key in items_by_word:
        return items_by_word[key]
    for word, item in items_by_word.items():
        if slug(word) == key or slug(word) == slug(key):
            return item
    return None


def export_midjourney_bundle(
    items: list[dict],
    style: str | None = None,
    params: str | None = None,
) -> dict:
    """Build export payload + plain-text paste sheet for Midjourney."""
    rows = []
    text_blocks = [
        "# Midjourney prompt sheet — Word Sort Builder",
        "# Paste each prompt into Midjourney. Save the U1 (or preferred) image as:",
        "#   <word>.png   e.g. man.png",
        "# Then use Import Midjourney images in the builder.",
        "",
    ]
    for item in items:
        word = item["word"]
        spelling = item.get("spelling")
        prompt = build_midjourney_prompt(
            word, spelling, item.get("as_in"), style=style, params=params
        )
        rows.append(
            {
                "word": word,
                "spelling": spelling,
                "as_in": item.get("as_in"),
                "rating_mean": item.get("rating_mean") or item.get("mean"),
                "save_as": f"{slug(word)}.png",
                "prompt": prompt,
            }
        )
        label = f"/{spelling}/" if spelling else "?"
        text_blocks.append(f"=== {word} ({label}) -> save as {slug(word)}.png ===")
        text_blocks.append(prompt)
        text_blocks.append("")
    return {
        "count": len(rows),
        "items": rows,
        "text": "\n".join(text_blocks).rstrip() + "\n",
    }


def load_biemiller_lookup(path: Path | None = None) -> dict[str, dict]:
    """
    word -> {rating_mean, freq_pm, mean, pos, meaning}
    Prefer highest Rating.Mean when duplicates exist.
    """
    import csv

    path = Path(path) if path else DEFAULT_BIEMILLER
    if not path.exists():
        return {}
    best: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            word = (r.get("WORD") or "").strip().lower()
            if not word:
                continue
            try:
                mean = float(r.get("Rating.Mean") or 0)
            except ValueError:
                mean = 0.0
            try:
                freq_pm = float(r.get("Freq_pm") or 0)
            except ValueError:
                freq_pm = 0.0
            if word not in best or mean > best[word]["rating_mean"]:
                best[word] = {
                    "rating_mean": round(mean, 4),
                    "mean": round(mean, 4),
                    "freq_pm": round(freq_pm, 4),
                    "pos": (r.get("PART OF SPEECH") or "").strip(),
                    "meaning": (r.get("MEANING") or "").strip(),
                }
    return best


def unique_words_from_bank(
    bank: dict,
    sort_id: str | None,
    only_words: set[str] | None,
    biemiller_lookup: dict[str, dict] | None = None,
) -> list[dict]:
    """
    Return unique words (first occurrence wins) with spelling metadata.
    Each item includes rating_mean / freq_pm when available.
    """
    sorts = bank.get("sorts") or []
    if sort_id:
        sorts = [s for s in sorts if s.get("id") == sort_id]
        if not sorts:
            known = ", ".join(s.get("id", "?") for s in bank.get("sorts") or [])
            raise SystemExit(f"Sort id not found: {sort_id}\nKnown: {known}")

    header_as_in = {}
    for s in sorts:
        for h in s.get("headers") or []:
            header_as_in[h.get("spelling")] = h.get("as_in")

    lookup = biemiller_lookup if biemiller_lookup is not None else {}

    by_word: dict[str, dict] = {}
    for s in sorts:
        sid = s.get("id")
        for w in s.get("words") or []:
            word = (w.get("word") or "").strip().lower()
            if not word:
                continue
            if only_words is not None and word not in only_words:
                continue
            if word in by_word:
                if sid and sid not in by_word[word]["sort_ids"]:
                    by_word[word]["sort_ids"].append(sid)
                continue
            spelling = w.get("spelling")
            biem = lookup.get(word) or {}
            mean = w.get("mean")
            if mean is None:
                mean = biem.get("rating_mean")
            freq_pm = w.get("freq_pm")
            if freq_pm is None:
                freq_pm = biem.get("freq_pm")
            rating_mean = w.get("rating_mean", mean)
            by_word[word] = {
                "word": word,
                "spelling": spelling,
                "position": w.get("position"),
                "mean": mean,
                "rating_mean": rating_mean,
                "freq_pm": freq_pm,
                "meaning": w.get("meaning") or biem.get("meaning"),
                "pos": w.get("pos") or biem.get("pos"),
                "as_in": header_as_in.get(spelling),
                "sort_ids": [sid] if sid else [],
            }
    items = list(by_word.values())
    items.sort(key=lambda x: (-(x.get("rating_mean") or x.get("mean") or 0), x["word"]))
    return items


class OpenAIImageClient:
    def __init__(self, api_key: str, model: str, size: str = "1024x1024"):
        if OpenAI is None:
            raise RuntimeError("pip install openai")
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.size = size
        self._tried_fallback = False

    def _generate_once(self, model: str, prompt: str):
        kwargs = {
            "model": model,
            "prompt": prompt,
            "size": self.size,
        }
        # dall-e-3 requires n=1; gpt-image models usually omit n
        if model.startswith("dall-e"):
            kwargs["n"] = 1
        return self.client.images.generate(**kwargs)

    def generate(self, prompt: str) -> bytes:
        """Return raw PNG/JPEG bytes for the image."""
        try:
            resp = self._generate_once(self.model, prompt)
        except Exception as e:
            msg = str(e)
            # Don't silently burn a fallback if the real issue is billing
            if "insufficient_quota" in msg or "credit_balance_exhausted" in msg:
                raise RuntimeError(
                    "OpenAI account has no credits remaining. "
                    "Add credits at https://platform.openai.com/settings/organization/billing/"
                ) from e
            if self.model != FALLBACK_MODEL and not self._tried_fallback:
                safe_print(f"Model {self.model} failed ({e}); trying {FALLBACK_MODEL}...")
                self._tried_fallback = True
                resp = self._generate_once(FALLBACK_MODEL, prompt)
                self.model = FALLBACK_MODEL
            else:
                raise

        data = resp.data[0]
        b64 = getattr(data, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(data, "url", None)
        if not url:
            raise RuntimeError("OpenAI response had neither b64_json nor url")
        if requests is None:
            raise RuntimeError("pip install requests (needed to download image URL)")
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return r.content


class DryRunImageClient:
    def generate(self, prompt: str) -> bytes:
        # Minimal valid-ish placeholder text file bytes marked as png path later
        return f"[dry-run image for prompt]\n{prompt}\n".encode("utf-8")


def load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "cards": {}}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def patch_bank_images(bank_path: Path, word: str, image_path: str, status: str) -> None:
    """Update matching word entries in the bank JSON (best-effort)."""
    try:
        bank = json.loads(bank_path.read_text(encoding="utf-8"))
    except Exception:
        return
    changed = False
    for s in bank.get("sorts") or []:
        for w in s.get("words") or []:
            if (w.get("word") or "").lower() == word.lower():
                w["status"] = status
                w["image"] = image_path if status == "accepted" else w.get("image")
                changed = True
    if changed:
        bank_path.write_text(json.dumps(bank, indent=2, ensure_ascii=False), encoding="utf-8")


def _existing_version_numbers(spelling_dir: Path, word_slug: str) -> list[int]:
    if not spelling_dir.exists():
        return []
    found = []
    for p in spelling_dir.glob(f"{word_slug}_v*.png"):
        m = re.search(r"_v(\d+)\.png$", p.name, re.I)
        if m:
            found.append(int(m.group(1)))
    return sorted(found)


def next_card_version(spelling_dir: Path, word_slug: str, entry: dict | None = None) -> int:
    """Next version number from disk archives + manifest history."""
    nums = _existing_version_numbers(spelling_dir, word_slug)
    if entry:
        if entry.get("version"):
            try:
                nums.append(int(entry["version"]))
            except (TypeError, ValueError):
                pass
        for h in entry.get("history") or []:
            try:
                nums.append(int(h.get("version")))
            except (TypeError, ValueError):
                pass
    return (max(nums) + 1) if nums else 1


def save_versioned_card(
    cards_dir: Path,
    word: str,
    spelling: str | None,
    img_bytes: bytes,
    prompt: str,
    *,
    note: str | None = None,
    entry: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Write preferred file `{word}_final.png` and immutable archive `{word}_vNNN.png`.

    Preferred image for sorts is always `*_final.png`.
    Manifest should store:
      image: path to *_final.png
      version: N
      history: [{version, archive, prompt, note, at}, ...]
    """
    word_slug = slug(word)
    spelling_key = slug(str(spelling or "misc"))
    spelling_dir = cards_dir / spelling_key
    spelling_dir.mkdir(parents=True, exist_ok=True)

    version = next_card_version(spelling_dir, word_slug, entry)
    final_path = spelling_dir / f"{word_slug}_final.png"
    archive_path = spelling_dir / f"{word_slug}_v{version:03d}.png"

    if dry_run:
        # Keep dry-run artifacts obvious and non-PNG
        dry_final = final_path.with_suffix(".dryrun.txt")
        dry_final.write_bytes(img_bytes)
        return {
            "image": str(dry_final),
            "archive": None,
            "version": version,
            "history_entry": {
                "version": version,
                "archive": None,
                "prompt": prompt,
                "note": note,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "dry_run": True,
            },
            "dry_run": True,
        }

    # If an unversioned final already exists (pre-versioning runs), archive it first
    if final_path.exists() and version == 1 and not _existing_version_numbers(spelling_dir, word_slug):
        bootstrap = spelling_dir / f"{word_slug}_v001.png"
        if not bootstrap.exists():
            bootstrap.write_bytes(final_path.read_bytes())
        version = 2
        archive_path = spelling_dir / f"{word_slug}_v{version:03d}.png"

    final_path.write_bytes(img_bytes)
    archive_path.write_bytes(img_bytes)

    history_entry = {
        "version": version,
        "archive": str(archive_path),
        "prompt": prompt,
        "note": note,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return {
        "image": str(final_path),
        "archive": str(archive_path),
        "version": version,
        "history_entry": history_entry,
        "dry_run": False,
    }


def merge_history(entry: dict | None, history_entry: dict) -> list[dict]:
    history = list((entry or {}).get("history") or [])
    # Avoid duping the same version number
    history = [h for h in history if h.get("version") != history_entry.get("version")]
    history.append(history_entry)
    history.sort(key=lambda h: int(h.get("version") or 0))
    return history


def restore_card_version(
    cards_dir: Path,
    word: str,
    spelling: str | None,
    version: int,
    entry: dict,
) -> dict:
    """Copy archived vNNN over *_final.png and mark that version preferred."""
    word_slug = slug(word)
    spelling_key = slug(str(spelling or entry.get("spelling") or "misc"))
    spelling_dir = cards_dir / spelling_key
    final_path = spelling_dir / f"{word_slug}_final.png"

    archive = None
    prompt = entry.get("prompt")
    for h in entry.get("history") or []:
        if int(h.get("version") or 0) == int(version):
            archive = h.get("archive")
            prompt = h.get("prompt") or prompt
            break
    if not archive:
        candidate = spelling_dir / f"{word_slug}_v{int(version):03d}.png"
        if candidate.exists():
            archive = str(candidate)
    if not archive or not Path(archive).exists():
        raise FileNotFoundError(f"No archive for {word} v{version}")

    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(Path(archive).read_bytes())
    return {
        "image": str(final_path),
        "version": int(version),
        "prompt": prompt,
        "restored_from": archive,
    }


def process_word(
    client,
    item: dict,
    outdir: Path,
    style: str,
    dry_run: bool,
    auto_accept: bool,
    entry: dict | None = None,
) -> dict:
    word = item["word"]
    spelling = item.get("spelling") or "misc"
    prompt = build_prompt(word, item.get("spelling"), item.get("as_in"), style)

    while True:
        safe_print(f"\n=== {word}  (/{item.get('spelling')}/ as in {item.get('as_in')}) ===")
        safe_print(f"Prompt: {prompt}")
        safe_print("Generating...")
        try:
            img_bytes = client.generate(prompt)
        except Exception as e:
            safe_print(f"Generation failed: {e}")
            if dry_run or auto_accept:
                return {
                    "word": word,
                    "spelling": item.get("spelling"),
                    "prompt": prompt,
                    "image": None,
                    "status": "failed",
                    "error": str(e),
                    "sort_ids": item.get("sort_ids"),
                    "version": (entry or {}).get("version"),
                    "history": (entry or {}).get("history") or [],
                }
            choice = input("Retry? [y/n/s=skip] ").strip().lower()
            if choice.startswith("s"):
                return {
                    "word": word,
                    "spelling": item.get("spelling"),
                    "prompt": prompt,
                    "image": None,
                    "status": "skipped",
                    "sort_ids": item.get("sort_ids"),
                    "version": (entry or {}).get("version"),
                    "history": (entry or {}).get("history") or [],
                }
            if choice.startswith("n"):
                return {
                    "word": word,
                    "spelling": item.get("spelling"),
                    "prompt": prompt,
                    "image": None,
                    "status": "failed",
                    "error": str(e),
                    "sort_ids": item.get("sort_ids"),
                    "version": (entry or {}).get("version"),
                    "history": (entry or {}).get("history") or [],
                }
            continue

        saved = save_versioned_card(
            outdir,
            word,
            spelling,
            img_bytes,
            prompt,
            entry=entry,
            dry_run=dry_run,
        )
        history = merge_history(entry, saved["history_entry"])
        safe_print(
            f"Saved candidate: {saved['image']} (v{saved['version']}"
            + (f", archive {saved['archive']}" if saved.get("archive") else "")
            + ")"
        )

        result_base = {
            "word": word,
            "spelling": item.get("spelling"),
            "prompt": prompt,
            "image": saved["image"],
            "version": saved["version"],
            "history": history,
            "sort_ids": item.get("sort_ids"),
            "rating_mean": item.get("rating_mean", item.get("mean")),
            "mean": item.get("mean", item.get("rating_mean")),
            "freq_pm": item.get("freq_pm"),
            "meaning": item.get("meaning"),
            "pos": item.get("pos"),
        }

        if dry_run or auto_accept:
            if dry_run:
                return {
                    **result_base,
                    "status": "accepted",
                    "dry_run": True,
                }
            return {
                **result_base,
                "status": "pending_review",
                "auto_accepted": True,
            }

        choice = input(
            "Keep this image? [y]es / [n] regenerate / [r]evise prompt / [s]kip: "
        ).strip().lower()
        if choice.startswith("y") or choice == "":
            return {**result_base, "status": "accepted"}
        if choice.startswith("s"):
            return {
                "word": word,
                "spelling": item.get("spelling"),
                "prompt": prompt,
                "image": saved["image"],
                "version": saved["version"],
                "history": history,
                "status": "skipped",
                "sort_ids": item.get("sort_ids"),
            }
        if choice.startswith("r"):
            revision = input("Revision note (e.g. 'make it a simple cartoon mop'):\n> ")
            if revision.strip():
                prompt = f"{prompt} Additional direction: {revision.strip()}"
            # Continue loop; next save bumps version again
            entry = {**(entry or {}), "version": saved["version"], "history": history}
            continue
        # n -> regenerate; keep history so version increments
        entry = {**(entry or {}), "version": saved["version"], "history": history}
        continue


def main() -> int:
    here = Path(__file__).resolve().parent
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_dotenv(here / ".env")

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--bank",
        type=Path,
        default=here / "sort_word_bank_L1.json",
        help="Path to sort_word_bank JSON (default: sort_word_bank_L1.json)",
    )
    ap.add_argument("--sort", default=None, help="Only words from this sort id")
    ap.add_argument("--words", default=None, help="Comma-separated word allowlist")
    ap.add_argument("--outdir", type=Path, default=here / "cards")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=None, help="Max new words to process")
    ap.add_argument("--model", default=os.environ.get("OPENAI_IMAGE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--yes",
        action="store_true",
        help="Auto-accept every generated image (no interactive prompts).",
    )
    ap.add_argument(
        "--redo-accepted",
        action="store_true",
        help="Regenerate words already marked accepted in the manifest.",
    )
    ap.add_argument(
        "--biemiller",
        type=Path,
        default=DEFAULT_BIEMILLER,
        help="Kuperman×Biemiller CSV for Rating.Mean / Freq_pm on cards",
    )
    args = ap.parse_args()

    if not args.bank.exists():
        sys.exit(f"Bank not found: {args.bank}")

    if not args.dry_run:
        if not args.api_key:
            sys.exit(
                "No OPENAI_API_KEY found. Set the env var, put it in .env, "
                "or pass --api-key (prefer env/.env — don't paste keys into chat)."
            )
        if OpenAI is None:
            sys.exit("Missing package: pip install openai")

    bank = json.loads(args.bank.read_text(encoding="utf-8"))
    only = None
    if args.words:
        only = {w.strip().lower() for w in args.words.split(",") if w.strip()}

    biem_lookup = load_biemiller_lookup(args.biemiller)
    items = unique_words_from_bank(bank, args.sort, only, biemiller_lookup=biem_lookup)
    if not items:
        sys.exit("No words to process (check --sort / --words filters).")

    outdir = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest or (outdir / "manifest.json")
    manifest = load_manifest(manifest_path)

    client = DryRunImageClient() if args.dry_run else OpenAIImageClient(
        args.api_key, args.model, args.size
    )

    pending = []
    for item in items:
        key = item["word"]
        prev = (manifest.get("cards") or {}).get(key)
        if prev and not args.redo_accepted:
            has_png = bool(prev.get("image") and str(prev.get("image")).lower().endswith(".png"))
            if prev.get("status") in ("accepted", "pending_review") and has_png:
                continue
        pending.append(item)
    if args.limit is not None:
        pending = pending[: args.limit]

    safe_print(f"Bank: {args.bank.name}")
    safe_print(f"Unique words in selection: {len(items)}")
    safe_print(f"To process now: {len(pending)}" + (" (dry-run)" if args.dry_run else ""))
    if not pending:
        safe_print("Nothing to do — all selected words already accepted.")
        return 0

    for item in pending:
        prev_entry = (manifest.get("cards") or {}).get(item["word"])
        result = process_word(
            client,
            item,
            outdir,
            args.style,
            dry_run=args.dry_run,
            auto_accept=args.yes or args.dry_run,
            entry=prev_entry,
        )
        manifest.setdefault("cards", {})[item["word"]] = {
            **result,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        save_manifest(manifest_path, manifest)
        if result.get("status") == "accepted" and result.get("image"):
            patch_bank_images(args.bank, item["word"], result["image"], "accepted")
        elif result.get("status") == "pending_review" and result.get("image"):
            patch_bank_images(args.bank, item["word"], result["image"], "pending_review")

    accepted = sum(
        1 for c in manifest.get("cards", {}).values() if c.get("status") == "accepted"
    )
    safe_print(f"\nDone. Manifest: {manifest_path}")
    safe_print(f"Accepted cards in manifest: {accepted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
