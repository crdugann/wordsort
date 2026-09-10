#!/usr/bin/env python3
"""
build_sort_bank.py — Phase 1 of Word Sort Builder TYCTR
=======================================================
Builds sort_word_bank JSON from TYCTR spellings + Biemiller words.

Three ways to choose what goes in the sort
------------------------------------------
1) Focus lesson (auto contrasts with previous GPCs)
     python build_sort_bank.py --lesson 37
     python build_sort_bank.py --lesson 37 --previous 3
     python build_sort_bank.py --lesson 37 --contrast g,sh

2) Custom lessons (all GPCs introduced in those lessons)
     python build_sort_bank.py --lessons 14,19,35

3) Custom spellings (exact GPC symbols)
     python build_sort_bank.py --spellings-list m,s,sh,ch
     python build_sort_bank.py --spellings-list a:,ee,i   # aliases for ā, ē, i

Words are ranked by Rating.Mean and matched at word START or END.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DESKTOP_SPELLINGS = Path(
    r"C:\Users\chris\OneDrive\Desktop\TYCTR Decodables x Midjourney"
    r"\TYCTR_GPC_Words (2).xlsx - Spellings.csv"
)
_DESKTOP_BIEMILLER = Path(
    r"C:\Users\chris\OneDrive\Desktop\TYCTR Decodables x Midjourney"
    r"\Kuperman X Biemiller - e.rated_biemiller.csv"
)


def _resolve_data(env_key: str, *candidates: Path) -> Path:
    env = os.environ.get(env_key)
    if env:
        return Path(env)
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


DEFAULT_SPELLINGS = _resolve_data(
    "SPELLINGS_CSV",
    _HERE / "data" / "spellings.csv",
    _DESKTOP_SPELLINGS,
)
DEFAULT_BIEMILLER = _resolve_data(
    "BIEMILLER_CSV",
    _HERE / "data" / "biemiller.csv",
    _DESKTOP_BIEMILLER,
)

MACRON_TO_LETTER = {"ā": "a", "ē": "e", "ī": "i", "ō": "o", "ū": "u", "ȳ": "y"}

# ASCII-friendly aliases for macron vowels / common typos
SYMBOL_ALIASES = {
    "a:": "ā",
    "aa": "ā",
    "longa": "ā",
    "long-a": "ā",
    "e:": "ē",
    "ee": "ē",
    "longe": "ē",
    "long-e": "ē",
    "i:": "ī",
    "ii": "ī",
    "longi": "ī",
    "long-i": "ī",
    "o:": "ō",
    "oo_long": "ō",
    "longo": "ō",
    "long-o": "ō",
    "u:": "ū",
    "longu": "ū",
    "long-u": "ū",
    "y:": "ȳ",
    "yy": "ȳ",
    "longy": "ȳ",
    "long-y": "ȳ",
}

EXCLUDE_START_CLUSTERS = {
    "s": ("sh", "sch", "sc", "sk", "sl", "sm", "sn", "sp", "st", "sw"),
    "t": ("th", "tch", "tr", "tw"),
    "d": ("dr", "dw"),
    "c": ("ch", "ck"),
    "w": ("wh", "wr"),
    "p": ("ph", "pr", "pl"),
    "g": ("gh", "gl", "gr"),
    "n": ("kn"),
    "h": (),
    "b": ("bl", "br"),
    "f": ("fl", "fr"),
    "l": (),
    "m": (),
    "r": (),
    "k": (),
    "v": (),
    "j": (),
    "z": (),
    "y": (),
    "x": (),
    "q": ("qu",),
}

PHONEME_CAVEATS = {
    "ē": {
        "wrong_examples": ["egg", "elephant", "end", "elbow", "exit", "engine", "edge", "every", "else"],
        "note": "TYCTR ē is long /ē/ as in eat — reject short-e pictures.",
    },
    "e": {
        "wrong_examples": ["eat", "eagle", "ear", "eel", "east", "even", "evil"],
        "note": "TYCTR e is short /ĕ/ as in end — reject long-e pictures.",
    },
    "a": {
        "wrong_examples": ["all", "about", "away", "again", "always", "alone", "ate", "age", "able"],
        "note": "TYCTR a is short /a/ as in and — prefer apple, ant, alligator.",
    },
    "ā": {
        "wrong_examples": ["apple", "ant", "animal", "and", "ask", "angry"],
        "note": "TYCTR ā is long /ā/ as in ate — reject short-a pictures.",
    },
    "i": {
        "wrong_examples": ["ice", "idea", "island", "iron", "iris", "idol", "item"],
        "note": "TYCTR i is short /i/ as in if — reject long-i pictures.",
    },
    "ī": {
        "wrong_examples": ["if", "in", "insect", "igloo", "ink", "inch", "itch"],
        "note": "TYCTR ī is long /ī/ as in ice — reject short-i pictures.",
    },
    "o": {
        "wrong_examples": ["over", "open", "old", "only", "ocean"],
        "note": "TYCTR o is short /ŏ/ as in ox.",
    },
    "ō": {
        "wrong_examples": ["ox", "on", "off", "octopus", "odd"],
        "note": "TYCTR ō is long /ō/ as in over.",
    },
    "u": {
        "wrong_examples": ["use", "unicorn", "uniform"],
        "note": "TYCTR u is short /ŭ/ as in under.",
    },
    "ū": {
        "wrong_examples": ["up", "under", "umbrella", "us"],
        "note": "TYCTR ū is long /ū/ as in use.",
    },
    "ȳ": {
        "wrong_examples": ["yellow", "yes", "yard", "yolk"],
        "note": "TYCTR ȳ is long /ī/ as in my (final y) — not initial /y/.",
    },
    "th": {"note": "TYCTR th here is voiced /th/ as in this (not thing)."},
    "oo": {"note": "TYCTR oo is /oo/ as in moon (not look)."},
}

SKIP_AS_HEADER = {"I"}

PREFERRED_POSITION = {
    "th": "start",
    "sh": "start",
    "ch": "start",
    "wh": "start",
    "qu": "start",
    "ar": "end",
    "er": "end",
    "ing": "end",
    "oo": "start",
    "ea": "start",
    "ai": "start",
    "ou": "start",
    "ȳ": "end",
}


def orthographic_form(symbol: str) -> str:
    if symbol in MACRON_TO_LETTER:
        return MACRON_TO_LETTER[symbol]
    return symbol.lower()


def sid(*parts: str) -> str:
    raw = "_".join(str(p) for p in parts)
    return re.sub(
        r"[^a-zA-Z0-9_]+",
        "",
        raw.replace("ē", "e")
        .replace("ā", "a")
        .replace("ī", "i")
        .replace("ō", "o")
        .replace("ū", "u")
        .replace("ȳ", "y"),
    )


def parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def parse_lesson_list(raw: str | None) -> list[int]:
    parts = parse_csv_list(raw)
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            raise SystemExit(f"Invalid lesson number in --lessons: {p!r}")
    return out


def normalize_symbol_token(token: str) -> str:
    t = token.strip()
    if not t:
        return t
    # Exact / case variants
    if t in MACRON_TO_LETTER or t.lower() in {
        "m", "s", "a", "t", "r", "d", "i", "th", "c", "o", "n", "f", "u", "l",
        "w", "g", "sh", "h", "k", "v", "p", "ar", "ch", "e", "ing", "y", "er",
        "b", "oo", "j", "wh", "qu", "x", "z", "ea", "ai", "ou",
    }:
        # Preserve macron symbols as-is; lower ASCII
        return t if t in MACRON_TO_LETTER else t.lower()
    alias = SYMBOL_ALIASES.get(t.lower())
    if alias:
        return alias
    return t  # may still match CSV symbol exactly (ā typed directly)


def load_all_spellings(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "symbol": r["Symbol (Spelling)"].strip(),
                    "pronounced": (r.get("Pronounced") or "").strip(),
                    "as_in": (r.get("As In") or "").strip(),
                    "voiced": (r.get("Voiced or Whispered") or "").strip(),
                    "lesson": int(r["Introduced in Lesson"]),
                    "stage": int(r["BOB Stage"])
                    if (r.get("BOB Stage") or "").strip()
                    else None,
                }
            )
    rows.sort(key=lambda x: (x["lesson"], x["symbol"]))
    return rows


def load_biemiller(path: Path) -> list[dict]:
    best: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            word = (r.get("WORD") or "").strip()
            if not word:
                continue
            key = word.lower()
            try:
                mean = float(r.get("Rating.Mean") or 0)
            except ValueError:
                mean = 0.0
            try:
                freq_pm = float(r.get("Freq_pm") or 0)
            except ValueError:
                freq_pm = 0.0
            if key not in best or mean > best[key]["mean"]:
                best[key] = {
                    "word": key,
                    "mean": mean,
                    "freq_pm": freq_pm,
                    "pos": (r.get("PART OF SPEECH") or "").strip(),
                    "meaning": (r.get("MEANING") or "").strip(),
                    "rating": (r.get("RATING") or "").strip(),
                }
    return list(best.values())


def is_noun(pos: str) -> bool:
    return bool(re.match(r"^n\b|^n\.", pos or "", re.I))


def passes_caveat(symbol: str, word: str) -> bool:
    cave = PHONEME_CAVEATS.get(symbol)
    if not cave:
        return True
    return word not in set(cave.get("wrong_examples") or [])


def starts_with_gpc(word: str, symbol: str, form: str) -> bool:
    if not word.startswith(form):
        return False
    if len(form) > 1:
        return passes_caveat(symbol, word)
    for prefix in EXCLUDE_START_CLUSTERS.get(form, ()):
        if word.startswith(prefix):
            return False
    return passes_caveat(symbol, word)


def ends_with_gpc(word: str, symbol: str, form: str) -> bool:
    if not word.endswith(form):
        return False
    if symbol == "s" and not word.endswith("ss"):
        return False
    return passes_caveat(symbol, word)


def match_positions(symbol: str) -> list[str]:
    pref = PREFERRED_POSITION.get(symbol)
    if pref:
        return [pref]
    form = orthographic_form(symbol)
    if len(form) == 1 and form in EXCLUDE_START_CLUSTERS:
        return ["start", "end"]
    if len(form) == 1:
        return ["start"]
    return ["start", "end"]


def entry_from_word(w: dict, symbol: str, position: str) -> dict:
    return {
        "word": w["word"],
        "sound": symbol,
        "spelling": symbol,
        "position": position,
        "mean": round(w["mean"], 4),
        "freq_pm": round(float(w.get("freq_pm") or 0), 4),
        "rating_mean": round(w["mean"], 4),  # alias: CSV column Rating.Mean
        "pos": w["pos"],
        "meaning": w["meaning"],
        "noun": is_noun(w["pos"]),
        "status": "pending",
        "image": None,
    }


def collect_edge_words(biemiller: list[dict], spelling: dict) -> dict:
    symbol = spelling["symbol"]
    form = orthographic_form(symbol)
    start, end = [], []
    for w in biemiller:
        word = w["word"]
        if starts_with_gpc(word, symbol, form):
            start.append(entry_from_word(w, symbol, "start"))
        if ends_with_gpc(word, symbol, form):
            end.append(entry_from_word(w, symbol, "end"))
    start.sort(key=lambda x: x["mean"], reverse=True)
    end.sort(key=lambda x: x["mean"], reverse=True)
    return {
        **spelling,
        "letter": form,
        "caveat": PHONEME_CAVEATS.get(symbol),
        "start": start,
        "end": end,
        "usable_as_header": symbol not in SKIP_AS_HEADER
        and (len(start) + len(end)) > 0,
    }


def pick_picture_words(entries: list[dict], n: int) -> list[dict]:
    nouns = [e for e in entries if e["noun"]]
    other = [e for e in entries if not e["noun"]]
    return (nouns + other)[:n]


def best_position_words(pool: dict, n: int) -> tuple[str, list[dict]]:
    positions = match_positions(pool["symbol"])
    ranked = []
    for pos in positions:
        words = pick_picture_words(pool.get(pos, []), n)
        ranked.append((len([w for w in pool.get(pos, []) if w["noun"]]), pos, words))
    ranked.sort(key=lambda x: x[0], reverse=True)
    pref = PREFERRED_POSITION.get(pool["symbol"])
    if pref:
        for _count, pos, words in ranked:
            if pos == pref and words:
                return pos, words
    for _count, pos, words in ranked:
        if words:
            return pos, words
    return positions[0], []


def immediately_previous(
    all_spellings: list[dict],
    lesson: int,
    k: int,
    pools: dict[str, dict] | None = None,
) -> list[dict]:
    if k <= 0:
        return []
    prior_sorted = sorted(
        [s for s in all_spellings if s["lesson"] < lesson],
        key=lambda s: (-s["lesson"], s["symbol"]),
    )
    picked = []
    for s in prior_sorted:
        if s["symbol"] in SKIP_AS_HEADER:
            continue
        if pools is not None:
            pool = pools.get(s["symbol"])
            if not pool or not pool.get("usable_as_header"):
                continue
        picked.append(s)
        if len(picked) >= k:
            break
    picked.reverse()
    return picked


def usable_headers(spellings: list[dict], pools: dict[str, dict]) -> list[dict]:
    out = []
    for s in spellings:
        if s["symbol"] in SKIP_AS_HEADER:
            continue
        pool = pools.get(s["symbol"])
        if not pool or not pool.get("usable_as_header"):
            continue
        out.append(s)
    return out


def resolve_symbols(
    all_spellings: list[dict], tokens: list[str]
) -> list[dict]:
    """Map user tokens to spelling rows; error on unknown."""
    by_symbol = {s["symbol"]: s for s in all_spellings}
    # also index lowercase for ASCII symbols
    by_lower = {s["symbol"].lower(): s for s in all_spellings}
    resolved = []
    missing = []
    seen = set()
    for tok in tokens:
        norm = normalize_symbol_token(tok)
        row = by_symbol.get(norm) or by_lower.get(norm.lower())
        if not row:
            missing.append(tok)
            continue
        if row["symbol"] in seen:
            continue
        seen.add(row["symbol"])
        resolved.append(row)
    if missing:
        known = ", ".join(s["symbol"] for s in all_spellings)
        raise SystemExit(
            f"Unknown spelling(s): {', '.join(missing)}\n"
            f"Known TYCTR symbols: {known}\n"
            f"Aliases: {', '.join(sorted(SYMBOL_ALIASES))}"
        )
    return resolved


def resolve_focus_lesson(all_spellings: list[dict], lesson: int) -> int:
    introduced = sorted({s["lesson"] for s in all_spellings})
    at_or_before = [n for n in introduced if n <= lesson]
    if not at_or_before:
        raise SystemExit(f"No spellings introduced at or before lesson {lesson}.")
    return at_or_before[-1]


def make_sort(
    sort_id: str,
    title: str,
    focus_lesson: int | None,
    headers: list[dict],
    pools: dict[str, dict],
    per_column: int,
    focus: str = "initial_sound",
) -> dict | None:
    words = []
    header_meta = []
    notes = []
    positions_used = set()
    for h in headers:
        pool = pools[h["symbol"]]
        pos, col_words = best_position_words(pool, per_column)
        if not col_words:
            return None
        positions_used.add(pos)
        header_meta.append(
            {
                "spelling": h["symbol"],
                "as_in": h["as_in"],
                "lesson": h["lesson"],
                "key_idea": h["as_in"].split()[0].strip("(),"),
                "position": pos,
            }
        )
        words.extend(col_words)
        if pool.get("caveat") and pool["caveat"].get("note"):
            notes.append(pool["caveat"]["note"])

    seen = set()
    unique = []
    for w in words:
        key = (w["word"], w["position"], w["spelling"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(w)

    if len(positions_used) == 1 and "end" in positions_used:
        focus = "final_sound"
    elif len(positions_used) > 1:
        focus = "mixed_edge"

    avail = focus_lesson
    if avail is None and header_meta:
        avail = max(h["lesson"] for h in header_meta)

    return {
        "id": sort_id,
        "title": title,
        "type": "closed_picture_sort",
        "focus": focus,
        "focus_lesson": focus_lesson,
        "available_after_lesson": avail,
        "headers": header_meta,
        "words": unique,
        "word_count": len(unique),
        "notes": notes or None,
    }


def build_sorts_for_lesson(
    focus_lesson: int,
    new_spellings: list[dict],
    previous_spellings: list[dict],
    pools: dict[str, dict],
    per_column: int,
) -> list[dict]:
    sorts: list[dict] = []
    new_u = usable_headers(new_spellings, pools)
    prev_u = usable_headers(previous_spellings, pools)

    if len(new_u) >= 2:
        symbols = [s["symbol"] for s in new_u]
        title = " vs ".join(f"/{s}/" for s in symbols)
        sort = make_sort(
            sid("L" + str(focus_lesson), *symbols, "new"),
            f"Lesson {focus_lesson}: {title} (new spellings)",
            focus_lesson,
            new_u,
            pools,
            per_column,
        )
        if sort:
            sorts.append(sort)
        for i, a in enumerate(new_u):
            for b in new_u[i + 1 :]:
                sort = make_sort(
                    sid("L" + str(focus_lesson), a["symbol"], "vs", b["symbol"]),
                    f"Lesson {focus_lesson}: /{a['symbol']}/ vs /{b['symbol']}/",
                    focus_lesson,
                    [a, b],
                    pools,
                    per_column,
                )
                if sort:
                    sorts.append(sort)

    for neu in new_u:
        for old in prev_u:
            sort = make_sort(
                sid("L" + str(focus_lesson), neu["symbol"], "vs", old["symbol"]),
                f"Lesson {focus_lesson}: /{neu['symbol']}/ vs /{old['symbol']}/ "
                f"(new vs L{old['lesson']})",
                focus_lesson,
                [old, neu],
                pools,
                per_column,
            )
            if sort:
                sorts.append(sort)

    if prev_u and new_u:
        for neu in new_u:
            headers = prev_u + [neu]
            if len(headers) < 2:
                continue
            prev_syms = "-".join(s["symbol"] for s in prev_u)
            sort = make_sort(
                sid("L" + str(focus_lesson), neu["symbol"], "vs", prev_syms, "window"),
                f"Lesson {focus_lesson}: /{neu['symbol']}/ vs prior "
                f"({', '.join('/' + s['symbol'] + '/' for s in prev_u)})",
                focus_lesson,
                headers,
                pools,
                per_column,
            )
            if sort:
                sorts.append(sort)

    if not sorts and len(prev_u) >= 2:
        sort = make_sort(
            sid("L" + str(focus_lesson), "review", *[s["symbol"] for s in prev_u]),
            f"Lesson {focus_lesson}: review sort of prior spellings",
            focus_lesson,
            prev_u,
            pools,
            per_column,
        )
        if sort:
            sorts.append(sort)

    return sorts


def build_sorts_from_selection(
    selected: list[dict],
    pools: dict[str, dict],
    per_column: int,
    label: str,
) -> list[dict]:
    """Pairwise + full-group closed sorts for an explicit spelling set."""
    usable = usable_headers(selected, pools)
    if len(usable) < 2:
        syms = ", ".join(s["symbol"] for s in selected) or "(none)"
        raise SystemExit(
            f"Need at least 2 usable spellings to build a sort. Got: {syms}"
        )

    sorts: list[dict] = []
    focus_lesson = max(s["lesson"] for s in usable)

    # Full multi-way
    sort = make_sort(
        sid(label, *[s["symbol"] for s in usable], "all"),
        f"{label}: " + " vs ".join(f"/{s['symbol']}/" for s in usable),
        focus_lesson,
        usable,
        pools,
        per_column,
    )
    if sort:
        sorts.append(sort)

    # Pairwise
    for i, a in enumerate(usable):
        for b in usable[i + 1 :]:
            sort = make_sort(
                sid(label, a["symbol"], "vs", b["symbol"]),
                f"{label}: /{a['symbol']}/ vs /{b['symbol']}/",
                max(a["lesson"], b["lesson"]),
                [a, b],
                pools,
                per_column,
            )
            if sort:
                sorts.append(sort)

    return sorts


def summarize_pool(s: dict, pools: dict[str, dict]) -> dict:
    p = pools[s["symbol"]]
    return {
        "symbol": s["symbol"],
        "lesson": s["lesson"],
        "as_in": s["as_in"],
        "pronounced": s["pronounced"],
        "letter": p["letter"],
        "usable_as_header": p["usable_as_header"],
        "start_count": len(p["start"]),
        "end_count": len(p["end"]),
        "top_start_nouns": [
            {"word": e["word"], "mean": e["mean"]}
            for e in pick_picture_words(p["start"], 8)
        ],
        "top_end_nouns": [
            {"word": e["word"], "mean": e["mean"]}
            for e in pick_picture_words(p["end"], 8)
        ],
        "caveat": p.get("caveat"),
    }


def build_bank(
    spellings_path: Path,
    biemiller_path: Path,
    per_column: int,
    *,
    lesson: int | None = None,
    previous: int = 2,
    contrast_tokens: list[str] | None = None,
    lessons: list[int] | None = None,
    spelling_tokens: list[str] | None = None,
) -> dict:
    all_spellings = load_all_spellings(spellings_path)
    biemiller = load_biemiller(biemiller_path)
    mode = None
    new_spellings: list[dict] = []
    previous_spellings: list[dict] = []
    selected: list[dict] = []
    focus_lesson = None
    snapped = False
    requested_lesson = lesson

    if spelling_tokens:
        mode = "custom_spellings"
        selected = resolve_symbols(all_spellings, spelling_tokens)
        relevant = selected
        pools = {s["symbol"]: collect_edge_words(biemiller, s) for s in relevant}
        sorts = build_sorts_from_selection(
            selected, pools, per_column, label="custom_spellings"
        )
        focus_lesson = max(s["lesson"] for s in selected)

    elif lessons:
        mode = "custom_lessons"
        missing = [n for n in lessons if not any(s["lesson"] == n for s in all_spellings)]
        if missing:
            available = sorted({s["lesson"] for s in all_spellings})
            raise SystemExit(
                f"No spellings introduced in lesson(s): {missing}. "
                f"Lessons with GPCs: {available}"
            )
        selected = [s for s in all_spellings if s["lesson"] in set(lessons)]
        selected.sort(key=lambda s: (s["lesson"], s["symbol"]))
        relevant = selected
        pools = {s["symbol"]: collect_edge_words(biemiller, s) for s in relevant}
        label = "lessons_" + "-".join(str(n) for n in lessons)
        sorts = build_sorts_from_selection(selected, pools, per_column, label=label)
        focus_lesson = max(lessons)

    elif lesson is not None:
        mode = "focus_lesson"
        focus_lesson = resolve_focus_lesson(all_spellings, lesson)
        snapped = focus_lesson != lesson
        new_spellings = [s for s in all_spellings if s["lesson"] == focus_lesson]
        # Need pools through focus (and contrasts if they are earlier)
        relevant = [s for s in all_spellings if s["lesson"] <= focus_lesson]
        pools = {s["symbol"]: collect_edge_words(biemiller, s) for s in relevant}

        if contrast_tokens:
            previous_spellings = resolve_symbols(all_spellings, contrast_tokens)
            # Ensure contrast GPCs are in pools even if after focus (unusual but allowed)
            for s in previous_spellings:
                if s["symbol"] not in pools:
                    pools[s["symbol"]] = collect_edge_words(biemiller, s)
                    if s not in relevant:
                        relevant.append(s)
        else:
            previous_spellings = immediately_previous(
                all_spellings, focus_lesson, previous, pools=pools
            )

        sorts = build_sorts_for_lesson(
            focus_lesson, new_spellings, previous_spellings, pools, per_column
        )
        selected = new_spellings + previous_spellings

    else:
        raise SystemExit("Provide --lesson, --lessons, or --spellings-list.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "requested_lesson": requested_lesson,
        "requested_lessons": lessons,
        "requested_spellings": spelling_tokens,
        "requested_contrast": contrast_tokens,
        "focus_lesson": focus_lesson,
        "snapped_to_nearest_spelling_lesson": snapped,
        "previous_window": previous if mode == "focus_lesson" and not contrast_tokens else None,
        "new_spellings": new_spellings,
        "previous_spellings": previous_spellings,
        "selected_spellings": selected,
        "sources": {
            "spellings": str(spellings_path),
            "biemiller": str(biemiller_path),
            "rank_field": "Rating.Mean",
            "rank_note": (
                "Cards store both Rating.Mean (as mean/rating_mean) and Freq_pm. "
                "In this Kuperman×Biemiller export, Rating.Mean is used as the "
                "familiarity/frequency proxy for ranking (same as the TYCTR analyzer)."
            ),
            "fields": {
                "rating_mean": "CSV column Rating.Mean",
                "freq_pm": "CSV column Freq_pm",
                "mean": "alias of rating_mean (backward compatible)",
            },
        },
        "pools": {s["symbol"]: summarize_pool(s, pools) for s in relevant},
        "edge_lists": {
            sym: {"start": pools[sym]["start"][:50], "end": pools[sym]["end"][:50]}
            for sym in pools
        },
        "sorts": sorts,
        "phase2_hint": {
            "next": "midjourney_sort_cards.py",
            "prompt_pattern": (
                "Children's picture-sort card: a single {word}, centered, "
                "simple object on plain background, no text or letters in the image."
            ),
            "aspect": "1:1",
        },
    }


def default_out_path(here: Path, args) -> Path:
    if args.spellings_list:
        tag = sid("spellings", *parse_csv_list(args.spellings_list))
        return here / f"sort_word_bank_{tag}.json"
    if args.lessons:
        tag = "lessons_" + "-".join(str(n) for n in parse_lesson_list(args.lessons))
        return here / f"sort_word_bank_{tag}.json"
    return here / f"sort_word_bank_L{args.lesson}.json"


def _safe_print(msg: str = "") -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write((msg + "\n").encode(enc, errors="replace"))


def main() -> int:
    here = Path(__file__).resolve().parent
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--lesson",
        type=int,
        help="Focus lesson. New GPCs at this lesson vs previous/contrast spellings.",
    )
    mode.add_argument(
        "--lessons",
        type=str,
        help="Comma-separated lesson numbers whose GPCs to include, e.g. 14,19,35",
    )
    mode.add_argument(
        "--spellings-list",
        type=str,
        help="Comma-separated GPC symbols, e.g. m,s,sh,ch or a:,ee,i",
    )

    ap.add_argument(
        "--previous",
        type=int,
        default=2,
        help="With --lesson only: how many immediately previous usable GPCs "
        "to use as contrasts (default 2). Ignored if --contrast is set.",
    )
    ap.add_argument(
        "--contrast",
        type=str,
        default=None,
        help="With --lesson only: explicit contrast spellings, e.g. g,sh "
        "(overrides --previous).",
    )
    ap.add_argument("--spellings", type=Path, default=DEFAULT_SPELLINGS,
                    help="Path to TYCTR Spellings CSV")
    ap.add_argument("--biemiller", type=Path, default=DEFAULT_BIEMILLER)
    ap.add_argument("--per-column", type=int, default=8)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if not args.spellings.exists():
        sys.exit(f"Spellings CSV not found: {args.spellings}")
    if not args.biemiller.exists():
        sys.exit(f"Biemiller CSV not found: {args.biemiller}")
    if args.previous < 0:
        sys.exit("--previous must be >= 0")
    if args.contrast and args.lesson is None:
        sys.exit("--contrast can only be used with --lesson")
    if args.contrast and args.lessons:
        sys.exit("--contrast cannot be combined with --lessons")

    lessons = parse_lesson_list(args.lessons) if args.lessons else None
    spelling_tokens = parse_csv_list(args.spellings_list) if args.spellings_list else None
    contrast_tokens = parse_csv_list(args.contrast) if args.contrast else None

    out = args.out or default_out_path(here, args)

    bank = build_bank(
        args.spellings,
        args.biemiller,
        args.per_column,
        lesson=args.lesson,
        previous=args.previous,
        contrast_tokens=contrast_tokens,
        lessons=lessons,
        spelling_tokens=spelling_tokens,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bank, indent=2, ensure_ascii=False), encoding="utf-8")

    _safe_print(f"Wrote {out}")
    _safe_print(f"  mode:             {bank['mode']}")
    if bank["mode"] == "focus_lesson":
        _safe_print(f"  requested lesson: {bank['requested_lesson']}")
        _safe_print(
            f"  focus lesson:     {bank['focus_lesson']}"
            + (
                " (snapped)"
                if bank["snapped_to_nearest_spelling_lesson"]
                else ""
            )
        )
        _safe_print(
            "  new spellings:    "
            + (", ".join(s["symbol"] for s in bank["new_spellings"]) or "(none)")
        )
        _safe_print(
            "  contrasts:        "
            + (
                ", ".join(
                    f"{s['symbol']}(L{s['lesson']})"
                    for s in bank["previous_spellings"]
                )
                or "(none)"
            )
        )
    elif bank["mode"] == "custom_lessons":
        _safe_print(f"  lessons:          {bank['requested_lessons']}")
        _safe_print(
            "  spellings:        "
            + ", ".join(
                f"{s['symbol']}(L{s['lesson']})" for s in bank["selected_spellings"]
            )
        )
    else:
        _safe_print(f"  spellings:        {bank['requested_spellings']}")
        _safe_print(
            "  resolved:         "
            + ", ".join(
                f"{s['symbol']}(L{s['lesson']})" for s in bank["selected_spellings"]
            )
        )

    _safe_print(f"  sorts:            {len(bank['sorts'])}")
    for s in bank["sorts"]:
        hdrs = ", ".join(h["spelling"] for h in s["headers"])
        _safe_print(f"    - {s['id']}: {s['word_count']} words [{hdrs}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
