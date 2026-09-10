#!/usr/bin/env python3
"""
export_approved_pack.py — Phase 3 of Word Sort Builder TYCTR
============================================================
Exports a clean tutor/classroom pack from accepted picture cards.

Default: only status == "accepted" + existing *_final.png preferred images.

  python export_approved_pack.py --bank sort_word_bank_L1.json
  python export_approved_pack.py --bank sort_word_bank_L1.json --sort L1_m_vs_s
  python export_approved_pack.py --statuses accepted,pending_review   # preview pack

Output
------
  packs/<pack_id>/
    approved.json          machine-readable pack for Phase 4 scripts
    index.html             browse the pack locally
    README.txt
    by_spelling/<spelling>/<word>.png
    by_sort/<sort_id>/<spelling>/<word>.png
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

import openai_sort_cards as osc

HERE = Path(__file__).resolve().parent


def safe_print(msg: str = "") -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))


def slug(s: str) -> str:
    return osc.slug(s)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def preferred_image_path(entry: dict, cards_dir: Path) -> Path | None:
    """Resolve the preferred final PNG for a card entry."""
    image = entry.get("image")
    if image:
        p = Path(image)
        if p.exists() and p.suffix.lower() == ".png":
            return p
    spelling = slug(str(entry.get("spelling") or "misc"))
    word = slug(str(entry.get("word") or ""))
    candidate = cards_dir / spelling / f"{word}_final.png"
    if candidate.exists():
        return candidate
    return None


def collect_accepted(
    bank: dict,
    manifest: dict,
    statuses: set[str],
    sort_id: str | None,
    cards_dir: Path,
    biem_lookup: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """
    Returns (cards, sort_summaries).
    cards = approved card records with pack-relative paths filled later
    """
    words_index = {
        i["word"]: i
        for i in osc.unique_words_from_bank(
            bank, sort_id=None, only_words=None, biemiller_lookup=biem_lookup
        )
    }
    sorts = bank.get("sorts") or []
    if sort_id:
        sorts = [s for s in sorts if s.get("id") == sort_id]
        if not sorts:
            known = ", ".join(s.get("id", "?") for s in bank.get("sorts") or [])
            raise SystemExit(f"Sort id not found: {sort_id}\nKnown: {known}")

    sort_ids_needed = {s.get("id") for s in sorts if s.get("id")}
    cards_out: dict[str, dict] = {}

    for word, entry in (manifest.get("cards") or {}).items():
        status = (entry.get("status") or "").lower()
        if status not in statuses:
            continue
        # If filtering to a sort, keep cards tied to that sort (or all if no sort_ids)
        entry_sorts = set(entry.get("sort_ids") or [])
        if sort_id and entry_sorts and sort_id not in entry_sorts:
            # Still allow if bank lists the word under that sort
            meta = words_index.get(word, {})
            if sort_id not in set(meta.get("sort_ids") or []):
                continue

        img = preferred_image_path(entry, cards_dir)
        if not img:
            continue

        meta = words_index.get(word, {})
        biem = biem_lookup.get(word) or {}
        spelling = entry.get("spelling") or meta.get("spelling")
        cards_out[word] = {
            "word": word,
            "spelling": spelling,
            "status": status,
            "version": entry.get("version"),
            "source_image": str(img),
            "rating_mean": entry.get("rating_mean", entry.get("mean", meta.get("rating_mean", biem.get("rating_mean")))),
            "freq_pm": entry.get("freq_pm", meta.get("freq_pm", biem.get("freq_pm"))),
            "meaning": entry.get("meaning") or meta.get("meaning") or biem.get("meaning"),
            "pos": entry.get("pos") or meta.get("pos") or biem.get("pos"),
            "as_in": meta.get("as_in"),
            "sort_ids": sorted(entry_sorts or set(meta.get("sort_ids") or [])),
            "prompt": entry.get("prompt"),
        }

    # Build per-sort readiness using bank header columns
    sort_summaries = []
    for s in sorts:
        sid = s.get("id")
        headers = s.get("headers") or []
        columns = {}
        for h in headers:
            sp = h.get("spelling")
            columns[sp] = {
                "spelling": sp,
                "as_in": h.get("as_in"),
                "key_idea": h.get("key_idea"),
                "position": h.get("position"),
                "words": [],
            }
        for word, card in cards_out.items():
            sp = card.get("spelling")
            if sp in columns and (not card["sort_ids"] or sid in card["sort_ids"] or sid in sort_ids_needed):
                # Include card in sort if it belongs to this sort or sort filter matches spelling columns
                if sid in (card.get("sort_ids") or []) or sort_id == sid or (
                    not card.get("sort_ids") and sp in columns
                ):
                    columns[sp]["words"].append(card["word"])

        # Also attach by spelling from bank membership
        bank_words_by_spelling = {}
        for w in s.get("words") or []:
            ww = (w.get("word") or "").lower()
            sp = w.get("spelling")
            bank_words_by_spelling.setdefault(sp, set()).add(ww)
        for sp, col in columns.items():
            for ww in bank_words_by_spelling.get(sp, set()):
                if ww in cards_out and ww not in col["words"]:
                    # only if card spelling matches column
                    if cards_out[ww].get("spelling") == sp:
                        col["words"].append(ww)
            col["words"] = sorted(set(col["words"]))
            col["count"] = len(col["words"])

        counts = [columns[h.get("spelling")]["count"] for h in headers]
        ready = bool(headers) and all(c >= 1 for c in counts)
        sort_summaries.append(
            {
                "id": sid,
                "title": s.get("title"),
                "focus": s.get("focus"),
                "focus_lesson": s.get("focus_lesson") or s.get("available_after_lesson"),
                "headers": headers,
                "columns": columns,
                "ready_for_phase4": ready,
                "min_column_count": min(counts) if counts else 0,
                "total_approved_in_sort": sum(counts),
            }
        )

    # Stable card list
    cards_list = sorted(cards_out.values(), key=lambda c: (-(c.get("rating_mean") or 0), c["word"]))
    return cards_list, sort_summaries


def write_index_html(pack_dir: Path, pack: dict) -> None:
    rows = []
    for c in pack.get("cards") or []:
        rel = c.get("pack_paths", {}).get("by_spelling") or ""
        rows.append(
            f"""<tr>
              <td><img src="{rel}" alt="{c['word']}" /></td>
              <td><strong>{c['word']}</strong></td>
              <td>/{c.get('spelling')}/</td>
              <td>{c.get('rating_mean') if c.get('rating_mean') is not None else '—'}</td>
              <td>{c.get('freq_pm') if c.get('freq_pm') is not None else '—'}</td>
              <td>{', '.join(c.get('sort_ids') or [])}</td>
              <td>v{c.get('version') or '—'}</td>
            </tr>"""
        )
    sort_blocks = []
    for s in pack.get("sorts") or []:
        ready = "READY" if s.get("ready_for_phase4") else "INCOMPLETE"
        cols = []
        for sp, col in (s.get("columns") or {}).items():
            words = ", ".join(col.get("words") or []) or "(none)"
            cols.append(f"<li><strong>/{sp}/</strong> ({col.get('count', 0)}): {words}</li>")
        sort_blocks.append(
            f"<section class='sort'><h2>{s.get('id')} · {ready}</h2>"
            f"<p>{s.get('title') or ''}</p><ul>{''.join(cols)}</ul></section>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>{pack.get('pack_id')} — Approved Pack</title>
<style>
  body {{ font-family: Georgia, serif; background:#FAF7F1; color:#26241F; margin:0; padding:24px; }}
  h1 {{ color:#3D6B5C; }}
  table {{ border-collapse: collapse; width:100%; background:#fff; }}
  th, td {{ border:1px solid #E7E0D3; padding:8px; vertical-align:middle; }}
  th {{ background:#E4EEE9; text-align:left; }}
  img {{ max-width:72px; max-height:72px; }}
  .sort {{ background:#fff; border:1px solid #E7E0D3; border-radius:8px; padding:12px 16px; margin:16px 0; }}
  code {{ font-family: Consolas, monospace; }}
</style>
</head>
<body>
  <h1>Approved Pack: {pack.get('pack_id')}</h1>
  <p>Bank: <code>{pack.get('bank')}</code> · Statuses: <code>{', '.join(pack.get('statuses') or [])}</code>
     · Cards: <strong>{len(pack.get('cards') or [])}</strong></p>
  {''.join(sort_blocks)}
  <h2>All approved cards</h2>
  <table>
    <thead><tr><th>Image</th><th>Word</th><th>Sound</th><th>Rating.Mean</th><th>Freq_pm</th><th>Sorts</th><th>Ver</th></tr></thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="7">No approved cards.</td></tr>'}
    </tbody>
  </table>
</body>
</html>
"""
    (pack_dir / "index.html").write_text(html, encoding="utf-8")


def export_pack(
    bank_path: Path,
    manifest_path: Path,
    cards_dir: Path,
    out_root: Path,
    statuses: set[str],
    sort_id: str | None,
    pack_id: str | None,
    biemiller_path: Path,
) -> Path:
    bank = load_json(bank_path)
    manifest = load_json(manifest_path) if manifest_path.exists() else {"cards": {}}
    biem_lookup = osc.load_biemiller_lookup(biemiller_path)

    cards, sort_summaries = collect_accepted(
        bank, manifest, statuses, sort_id, cards_dir, biem_lookup
    )

    lesson = bank.get("focus_lesson") or bank.get("requested_lesson")
    if pack_id:
        pass
    elif sort_id:
        pack_id = f"{slug(sort_id)}_approved"
    elif lesson is not None:
        pack_id = f"L{lesson}_approved"
    else:
        pack_id = "approved_pack"
    pack_dir = out_root / pack_id
    if pack_dir.exists():
        shutil.rmtree(pack_dir)
    by_spelling = pack_dir / "by_spelling"
    by_sort = pack_dir / "by_sort"
    by_spelling.mkdir(parents=True, exist_ok=True)
    by_sort.mkdir(parents=True, exist_ok=True)

    exported_cards = []
    for card in cards:
        word = card["word"]
        spelling = slug(str(card.get("spelling") or "misc"))
        src = Path(card["source_image"])
        spelling_dest_dir = by_spelling / spelling
        spelling_dest_dir.mkdir(parents=True, exist_ok=True)
        spelling_dest = spelling_dest_dir / f"{slug(word)}.png"
        shutil.copy2(src, spelling_dest)

        sort_paths = {}
        for sid in card.get("sort_ids") or []:
            if sort_id and sid != sort_id:
                continue
            dest_dir = by_sort / slug(sid) / spelling
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{slug(word)}.png"
            shutil.copy2(src, dest)
            sort_paths[sid] = str(dest.relative_to(pack_dir)).replace("\\", "/")

        # If card has no sort_ids but we're exporting a specific sort, still place it
        if sort_id and sort_id not in sort_paths:
            dest_dir = by_sort / slug(sort_id) / spelling
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{slug(word)}.png"
            shutil.copy2(src, dest)
            sort_paths[sort_id] = str(dest.relative_to(pack_dir)).replace("\\", "/")

        exported = {
            **{k: v for k, v in card.items() if k != "source_image"},
            "pack_paths": {
                "by_spelling": str(spelling_dest.relative_to(pack_dir)).replace("\\", "/"),
                "by_sort": sort_paths,
            },
        }
        exported_cards.append(exported)

    # Refresh sort column word lists to exported set only
    exported_words = {c["word"] for c in exported_cards}
    for s in sort_summaries:
        for sp, col in (s.get("columns") or {}).items():
            col["words"] = [w for w in col.get("words") or [] if w in exported_words]
            col["count"] = len(col["words"])
        counts = [col["count"] for col in (s.get("columns") or {}).values()]
        s["ready_for_phase4"] = bool(counts) and all(c >= 1 for c in counts)
        s["min_column_count"] = min(counts) if counts else 0
        s["total_approved_in_sort"] = sum(counts)

    pack = {
        "pack_id": pack_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": 3,
        "bank": str(bank_path),
        "manifest": str(manifest_path),
        "statuses": sorted(statuses),
        "sort_filter": sort_id,
        "card_count": len(exported_cards),
        "cards": exported_cards,
        "sorts": sort_summaries,
        "phase4_hint": {
            "use": "approved.json",
            "rule": "Tutor scripts should only reference cards[] words/images in this pack.",
            "ready_sorts": [s["id"] for s in sort_summaries if s.get("ready_for_phase4")],
        },
    }

    (pack_dir / "approved.json").write_text(
        json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    write_index_html(pack_dir, pack)

    ready = [s["id"] for s in sort_summaries if s.get("ready_for_phase4")]
    incomplete = [s["id"] for s in sort_summaries if not s.get("ready_for_phase4")]
    readme = f"""Approved Pack: {pack_id}
Generated: {pack['generated_at']}

Contents
- approved.json  — source of truth for Phase 4 tutor scripts
- index.html     — open in a browser to preview cards
- by_spelling/   — images grouped by sound spelling
- by_sort/       — images grouped by sort id then spelling

Statuses included: {', '.join(sorted(statuses))}
Cards exported: {len(exported_cards)}

Sorts ready for Phase 4 (every header has ≥1 approved card):
  {', '.join(ready) if ready else '(none yet)'}

Sorts incomplete:
  {', '.join(incomplete) if incomplete else '(none)'}

Preferred image rule: pack copies the approved *_final.png for each word.
"""
    (pack_dir / "README.txt").write_text(readme, encoding="utf-8")
    return pack_dir


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bank", type=Path, default=HERE / "sort_word_bank_L1.json")
    ap.add_argument("--manifest", type=Path, default=HERE / "cards" / "manifest.json")
    ap.add_argument("--cards", type=Path, default=HERE / "cards")
    ap.add_argument("--outdir", type=Path, default=HERE / "packs")
    ap.add_argument("--sort", default=None, help="Only export one sort id")
    ap.add_argument("--pack-id", default=None, help="Folder/name for the pack")
    ap.add_argument(
        "--statuses",
        default="accepted",
        help="Comma-separated statuses to include (default: accepted)",
    )
    ap.add_argument("--biemiller", type=Path, default=osc.DEFAULT_BIEMILLER)
    args = ap.parse_args()

    if not args.bank.exists():
        sys.exit(f"Bank not found: {args.bank}")
    statuses = {s.strip().lower() for s in args.statuses.split(",") if s.strip()}
    if not statuses:
        sys.exit("No statuses provided")

    pack_dir = export_pack(
        bank_path=args.bank,
        manifest_path=args.manifest,
        cards_dir=args.cards,
        out_root=args.outdir,
        statuses=statuses,
        sort_id=args.sort,
        pack_id=args.pack_id,
        biemiller_path=args.biemiller,
    )

    pack = load_json(pack_dir / "approved.json")
    safe_print(f"Wrote {pack_dir}")
    safe_print(f"  cards:  {pack.get('card_count')}")
    safe_print(f"  statuses: {', '.join(pack.get('statuses') or [])}")
    ready = pack.get("phase4_hint", {}).get("ready_sorts") or []
    safe_print(f"  Phase 4 ready sorts: {', '.join(ready) if ready else '(none)'}")
    for s in pack.get("sorts") or []:
        cols = ", ".join(
            f"/{sp}/={col.get('count', 0)}" for sp, col in (s.get("columns") or {}).items()
        )
        flag = "READY" if s.get("ready_for_phase4") else "incomplete"
        safe_print(f"    - {s.get('id')}: {flag} [{cols}]")
    safe_print(f"  Preview: {(pack_dir / 'index.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
