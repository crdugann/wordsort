#!/usr/bin/env python3
"""
build_tutor_script.py — Phase 4 of Word Sort Builder TYCTR
==========================================================
Builds a 3-day closed picture-sort tutor script from an approved pack.

Pedagogy follows Words Their Way gradual release: Day 1 teacher-directed
closed sort (dense script) → Days 2–3 less tutor talk, more student
agency, Check & Reflect, and Day 3 automaticity measures.

Only words/images listed in approved.json are used.

  python build_tutor_script.py --pack packs/l1_m_vs_s_approved
  python build_tutor_script.py --pack packs/l1_m_vs_s_approved --sort L1_m_vs_s

Output (written into the pack folder)
-------------------------------------
  tutor_script.md
  tutor_script.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def safe_print(msg: str = "") -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def card_lookup(pack: dict) -> dict[str, dict]:
    return {c["word"]: c for c in pack.get("cards") or []}


def pick_sort(pack: dict, sort_id: str | None) -> dict:
    sorts = pack.get("sorts") or []
    if not sorts:
        raise ValueError("Pack has no sorts. Re-export Phase 3 first.")
    ready = [s for s in sorts if s.get("ready_for_phase4")]
    if sort_id:
        for s in sorts:
            if s.get("id") == sort_id:
                if not s.get("ready_for_phase4"):
                    safe_print(
                        f"Warning: {sort_id} is not marked ready_for_phase4; "
                        "script will still use available accepted cards."
                    )
                return s
        raise ValueError(f"Sort {sort_id!r} not found in pack.")
    if len(ready) == 1:
        return ready[0]
    if len(ready) > 1:
        ids = ", ".join(s["id"] for s in ready)
        raise ValueError(f"Multiple ready sorts ({ids}). Pass --sort.")
    # Fall back to first sort
    return sorts[0]


def column_plan(sort: dict, cards_by_word: dict[str, dict]) -> list[dict]:
    """Build ordered columns with header + demo + practice words from pack only."""
    columns = []
    headers = sort.get("headers") or []
    col_map = sort.get("columns") or {}

    # Preserve header order when available
    keys = [h.get("spelling") for h in headers if h.get("spelling") in col_map]
    for k in col_map:
        if k not in keys:
            keys.append(k)

    for spelling in keys:
        col = col_map[spelling]
        words = [w for w in (col.get("words") or []) if w in cards_by_word]
        if not words:
            continue
        hdr_meta = next((h for h in headers if h.get("spelling") == spelling), {})
        as_in = hdr_meta.get("as_in") or col.get("as_in")
        # Prefer as_in picture as header if present in pack; else highest-rated card
        if as_in and as_in in words:
            header_word = as_in
        else:
            header_word = max(
                words,
                key=lambda w: float(cards_by_word[w].get("rating_mean") or 0),
            )
        rest = [w for w in words if w != header_word]
        # Keep pack order (usually rating rank) for demos
        demo = rest[:2]
        practice = rest[2:]
        columns.append(
            {
                "spelling": spelling,
                "as_in": as_in or header_word,
                "pronounced": _guess_pronunciation(spelling, hdr_meta, col),
                "header_word": header_word,
                "demo_words": demo,
                "practice_words": practice,
                "all_words": [header_word] + rest,
                "key_idea": hdr_meta.get("key_idea") or col.get("key_idea") or as_in,
                "position": hdr_meta.get("position") or col.get("position") or "start",
            }
        )
    if len(columns) < 2:
        raise ValueError(
            "Need at least two sound columns with approved cards for a closed sort."
        )
    return columns


def _guess_pronunciation(spelling: str, hdr: dict, col: dict) -> str:
    for src in (hdr, col):
        if src.get("pronounced"):
            return str(src["pronounced"])
    # Common single-letter elongations for tutor talk
    elong = {"m": "mmm", "s": "sss", "n": "nnn", "f": "fff", "r": "rrr", "l": "lll"}
    return elong.get(spelling, spelling)


def meaning_line(word: str, cards_by_word: dict[str, dict]) -> str:
    c = cards_by_word.get(word) or {}
    meaning = (c.get("meaning") or "").strip()
    if meaning:
        return f"**{word}** — {meaning}"
    return f"**{word}**"


def img_rel(word: str, spelling: str, cards_by_word: dict[str, dict], sort_id: str) -> str:
    c = cards_by_word.get(word) or {}
    paths = c.get("pack_paths") or {}
    by_sort = (paths.get("by_sort") or {}).get(sort_id)
    if by_sort:
        return by_sort
    if paths.get("by_spelling"):
        return paths["by_spelling"]
    return f"by_spelling/{spelling}/{word}.png"


def build_markdown(
    pack: dict,
    sort: dict,
    columns: list[dict],
    cards_by_word: dict[str, dict],
) -> str:
    sort_id = sort["id"]
    title = sort.get("title") or sort_id
    focus = sort.get("focus") or "initial_sound"
    lesson = sort.get("focus_lesson")
    pack_id = pack.get("pack_id") or "approved_pack"

    sounds = " vs ".join(f"/{c['spelling']}/" for c in columns)
    header_labels = " / ".join(
        f"**{c['header_word']}** (+ letter **{c['spelling']}**)" for c in columns
    )

    all_words = []
    for c in columns:
        all_words.extend(c["all_words"])

    materials_lines = []
    for c in columns:
        for w in c["all_words"]:
            rel = img_rel(w, c["spelling"], cards_by_word, sort_id)
            role = "HEADER" if w == c["header_word"] else "sort card"
            materials_lines.append(f"- `{rel}` — **{w}** (/{c['spelling']}/, {role})")

    meaning_block = "\n".join(f"- {meaning_line(w, cards_by_word)}" for w in all_words)

    # Concrete demo lines from first two columns
    c0, c1 = columns[0], columns[1]
    d0 = c0["demo_words"][0] if c0["demo_words"] else c0["header_word"]
    d1 = c1["demo_words"][0] if c1["demo_words"] else c1["header_word"]
    d0b = c0["demo_words"][1] if len(c0["demo_words"]) > 1 else None

    help_word = None
    for c in columns:
        pool = c["demo_words"][1:] + c["practice_words"]
        if pool:
            help_word = (pool[0], c)
            break
    if help_word is None:
        help_word = (c0["header_word"], c0)

    practice_pool = []
    for c in columns:
        for w in c["demo_words"] + c["practice_words"]:
            if w not in (d0, d1, d0b, help_word[0]):
                practice_pool.append((w, c))
    # If pool is thin, allow re-use of demos for You Do
    if len(practice_pool) < 2:
        practice_pool = []
        for c in columns:
            for w in c["all_words"]:
                if w != c["header_word"]:
                    practice_pool.append((w, c))

    day2_blind = [w for w, _ in practice_pool[:4]] or [d0, d1]
    day3_words = all_words[:]

    lines: list[str] = []
    a = lines.append

    a(f"# Tutor Script — {title}")
    a("")
    a(f"**Pack:** `{pack_id}`  ")
    a(f"**Sort:** `{sort_id}` · {sounds} · focus: `{focus}`  ")
    if lesson is not None:
        a(f"**Lesson:** {lesson}  ")
    a(f"**Generated:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  ")
    a("**Rule:** Use only the approved picture cards listed below. Do not add words from memory.")
    a("")
    a("---")
    a("")
    a("## Materials (print / cut these)")
    a("")
    a(f"Closed picture sort — headers given up front: {header_labels}.")
    a("")
    a("\n".join(materials_lines))
    a("")
    letter_headers = " and ".join(f"**{c['spelling']}**" for c in columns)
    header_setup = "  |  ".join(
        f"**{c['header_word']}** + **{c['spelling']}**" for c in columns
    )

    a(
        f"**Setup:** Place the header pictures at the top of the workspace with the letter cards "
        f"{letter_headers}. Shuffle the remaining picture cards face down."
    )
    a("")
    a("---")
    a("")
    a("## Word meanings (quick check — say the name, don't spell yet)")
    a("")
    a(
        "WTW: use words/pictures students can name. For English learners, spend extra time "
        "naming pictures before any sorting."
    )
    a("")
    a(meaning_block)
    a("")
    a("---")
    a("")
    a("## Gradual release across days (WTW continuum)")
    a("")
    a("| Day | Support level | Tutor talk | Student job | What you measure |")
    a("|---|---|---|---|---|")
    a(
        "| **1** | Teacher-directed **closed** sort | Dense script + think-alouds; "
        "correct immediately on first shared sort | Learn categories; finish sort; one re-sort | "
        "Accuracy with scaffolds; can name headers |"
    )
    a(
        "| **2** | Guided → student-centered closed | Tiny model; prompts only; "
        "on re-sorts guide self-correction | Blind sort; partner/solo re-sort; more student talk | "
        "Fewer prompts; self-finds misfits |"
    )
    a(
        "| **3** | Student-centered closed + light assess | Facilitator questions, almost no demo | "
        "Independent sort; teach-back; timed sort | Accuracy without help; automaticity |"
    )
    a("")
    a(
        "Lesson skeleton every day: **Demonstrate & Sort → Check & Reflect → Extend**. "
        "Stay **closed** (headers given) for a new TYCTR feature — not Guess My Category / open sort yet."
    )
    a("")
    a("---")
    a("")
    a("## Framing")
    a("")
    a("### Day 1 — full framing (say all three)")
    a("")
    a("| Piece | Tutor language |")
    a("|---|---|")
    a(
        f"| **Declarative** | Today we are doing a **closed picture sort** for beginning sounds "
        f"**/{c0['spelling']}/** and **/{c1['spelling']}/**. I will give you the categories. |"
    )
    a(
        "| **Procedural** | We will **name the picture**, stretch the **first sound**, "
        "and put the card under the matching header. Then we check our columns. |"
    )
    a(
        "| **Conditional** | When we sort by the first sound, it helps us **read and spell** "
        "new words — and we still keep the meaning of the picture in mind. |"
    )
    a("")
    a("### Days 2–3 — short framing (10–15 sec)")
    a("")
    a(
        f"*“Same closed sort: /{c0['spelling']}/ like **{c0['header_word']}**, "
        f"/{c1['spelling']}/ like **{c1['header_word']}**. "
        f"Name it → first sound → header. You do more of the talking today.”*"
    )
    a("")
    a("---")
    a("")
    a("## Day 1 — Introduce the closed picture sort (~15–20 min)")
    a("")
    a(
        "**Goal:** Students learn the categories and how to sort by **initial sound** "
        "(teacher-directed closed sort — densest tutor talk)."
    )
    a("")
    a("### Demonstrate & Sort")
    a("")
    a("#### Prep / review the pictures (1–2 min)")
    a("1. Show each picture quickly. Student names it. Clarify meanings as needed (list above).")
    a(f"2. Set headers: {header_setup}.")
    a(
        f"3. Tutor establishes categories: *“This picture is **{c0['header_word']}**. "
        f"**{c0['header_word']}** starts with /{c0['pronounced']}/ — that goes with **{c0['spelling']}**. "
        f"This picture is **{c1['header_word']}**. "
        f"**{c1['header_word']}** starts with /{c1['pronounced']}/ — that goes with **{c1['spelling']}**.”*"
    )
    a("")
    a("#### I Do, You Watch — model 2–3 cards")
    a("Student watches; does not place yet. Stretch the first sound (say it as you lay it).")
    a("")
    a(
        f"1. Hold **{d0}**: *“I say **{d0}**… /{c0['pronounced']}/… **{d0}**. "
        f"That matches **{c0['header_word']}** / **{c0['spelling']}**. "
        f"I put it under this column.”* Place under /{c0['spelling']}/."
    )
    a(
        f"2. Hold **{d1}**: *“I say **{d1}**… /{c1['pronounced']}/… **{d1}**. "
        f"That matches **{c1['header_word']}** / **{c1['spelling']}**.”* Place under /{c1['spelling']}/."
    )
    if d0b:
        a(
            f"3. Hold **{d0b}**: *“**{d0b}**… /{c0['pronounced']}/… same as **{c0['header_word']}**.”* "
            f"Place under /{c0['spelling']}/."
        )
    a("")
    a("#### I Do, You Help — turn the task over (correct immediately if wrong)")
    a(
        f"Tutor holds **{help_word[0]}**. Student says the name and first sound; student places it."
    )
    a("")
    a(
        f"- Tutor: *“Your turn to help. What picture is this?”* → student: *“{help_word[0]}.”*"
    )
    a(
        f"- Tutor: *“What’s the first sound you hear in **{help_word[0]}**?”* "
        f"→ /{help_word[1]['pronounced']}/."
    )
    a(
        f"- Tutor: *“Which header? **{c0['header_word']}** or **{c1['header_word']}**?”* "
        f"→ place under /{help_word[1]['spelling']}/."
    )
    a(
        "- If wrong on this **first** shared sort: correct now — "
        "*“Say it again slowly. Stretch the first sound. Try once more.”* Do not skip the re-say."
    )
    a("")
    a("#### You Do, I Help — finish the sort")
    a("Student sorts the remaining picture cards. Tutor prompts only as needed:")
    a("")
    a("- *“Say it.”* → *“First sound?”* → *“Which header?”*")
    a("- After all cards are down: check each column together top to bottom (name each picture).")
    a(
        f"- Big idea (tutor may still lead): *“Pictures under **{c0['spelling']}** all start with "
        f"/{c0['pronounced']}/ like **{c0['header_word']}**. Pictures under **{c1['spelling']}** "
        f"all start with /{c1['pronounced']}/ like **{c1['header_word']}**.”*"
    )
    a("")
    a("**Remaining cards for this step:**")
    for w, c in practice_pool:
        a(f"- **{w}** → /{c['spelling']}/")
    a("")
    a("### Check & Reflect")
    a("Mix the non-header cards. Student re-sorts once under supervision.")
    a("")
    a("- On this **second** pass: if something looks off, do **not** grab the card first. Ask:")
    a("  - *“Why did you put that there?”*")
    a("  - *“I see one picture in this column that doesn’t fit — can you find it?”*")
    a(
        f"- Then: *“What do you notice about the pictures under **{c0['spelling']}**? "
        f"Under **{c1['spelling']}**?”*"
    )
    a("- End: student points to each column and states the big idea in their own words.")
    a("")
    a("### Extend")
    a("Student keeps the set for seat/partner practice later (same headers). No new words.")
    a("")
    a("---")
    a("")
    a("## Day 2 — Less tutor talk, more student talk (~15 min)")
    a("")
    a(
        "**Goal:** Fluency with the same categories; strengthen sound → letter link; "
        "shift talk from tutor to student."
    )
    a("")
    a("### Framing")
    a("Use the **short** Day 2–3 framing above.")
    a("")
    a("### Demonstrate & Sort")
    a("")
    a("#### I Do, You Watch (30–60 sec only)")
    a(
        f"One quick model: **{d0}** under **{c0['header_word']}** / **{c0['spelling']}** "
        f"with a short think-aloud — then stop talking."
    )
    a("")
    a("#### You Do, I Help — student re-sort (light prompts)")
    a(
        "Student re-sorts the full set. Tutor uses only: *Say it → first sound → header*. "
        "Prefer student explanations over tutor monologue."
    )
    a("")
    a("#### You Do, I Help — Blind picture sort")
    a(
        "Headers stay visible. Tutor holds a card so the student cannot see it, says the word, "
        "student points to the correct header, then places the card."
    )
    a("")
    a("Blind set (from this pack only):")
    for w in day2_blind:
        col = next(c for c in columns if w in c["all_words"])
        a(f"- Tutor says **{w}** → student points to /{col['spelling']}/")
    a("")
    a("### Check & Reflect (student talk)")
    a("Student does an untimed re-sort alone. Tutor watches.")
    a("")
    a("Use open prompts (not right/wrong IRE):")
    a("- *“What do you notice about these pictures now?”*")
    a("- *“How are they alike in this column? How are they different from that one?”*")
    a("- *“Why did you put this one here?”*")
    a("- *“Who can add to that?”* / *“Do you agree? Why or why not?”*")
    a(
        "- If a misfit remains: *“Find the one that doesn’t fit”* — student fixes it, then re-says "
        "the first sound."
    )
    a("")
    a("### Extend")
    a(
        f"**Write link:** Student writes the letter **{c0['spelling']}** or **{c1['spelling']}** "
        "next to 2–3 pictures after sorting (sound → letter)."
    )
    a("Optional: partner sort with the same set — partners must agree and say why.")
    a("")
    a("---")
    a("")
    a("## Day 3 — Agency + automaticity (~15 min)")
    a("")
    a(
        "**Goal:** Independent accuracy, student-owned big idea, and a light automaticity check "
        "(student-centered closed sort)."
    )
    a("")
    a("### Framing")
    a("Short Day 2–3 framing only. No long Declarative/Procedural/Conditional.")
    a("")
    a("### Demonstrate & Sort — You Do, I Watch")
    a(
        "Student sorts the full approved set with headers. Tutor observes; intervenes only if stuck. "
        "**No demo** unless the student cannot start."
    )
    a("")
    a("### Check & Reflect")
    a("- Student reads each column aloud (picture names).")
    a(
        f"- Tutor (facilitator, not teller): *“How are the **{c0['spelling']}** pictures the same? "
        f"How are the **{c1['spelling']}** pictures the same?”*"
    )
    a(
        f"- Target answer (student says it): same **beginning sound** "
        f"(/{c0['pronounced']}/ vs /{c1['pronounced']}/)."
    )
    a("- *“What did we learn from this sort to be a better reader or speller?”*")
    a("- If an error remains: student finds it, re-says, re-places, restates the big idea.")
    a("")
    a("### Automaticity check (measure)")
    a("1. Mix the cards. Student re-sorts as quickly as they can while still naming each picture.")
    a("2. Tutor tallies quietly:")
    a("   - time to finish (optional)")
    a("   - number of tutor prompts needed (goal: **0–1**)")
    a("   - any misfits after self-check")
    a("3. Student teach-back (60 sec, no tutor stems): headers → one example each → big idea.")
    a("")
    a("### Extend (pick one)")
    a(
        f"1. **Sound hunt:** Find one classroom object that starts like "
        f"**{c0['header_word']}** or **{c1['header_word']}** (say it; don’t invent new pack cards)."
    )
    a(
        f"2. **Letter link:** Match each header picture to letter **{c0['spelling']}** / "
        f"**{c1['spelling']}** and write the letter three times while saying the sound."
    )
    a(
        "3. **Best-bet transfer:** Tutor says a new spoken word (not a pack card). Student points to "
        f"/{c0['spelling']}/ or /{c1['spelling']}/ and explains the best bet."
    )
    a("")
    a("---")
    a("")
    a("## Answer key (for tutor only)")
    a("")
    for c in columns:
        words = ", ".join(f"**{w}**" for w in c["all_words"])
        a(f"- /{c['spelling']}/ (as in **{c['as_in']}**): {words}")
    a("")
    a("---")
    a("")
    a("## Fidelity checklist")
    a("")
    a("- [ ] Every picture named in this script appears in `approved.json` / the materials list")
    a("- [ ] No extra words were added from memory or the larger word bank")
    a("- [ ] Headers were given first (closed sort — not Guess My Category / open sort)")
    a("- [ ] Day 1: tutor modeled with think-alouds; first shared sort errors corrected immediately")
    a("- [ ] Day 2–3: less tutor talk; student justified placements; self-found misfits on re-sorts")
    a("- [ ] Student said the picture name and first sound before placing (*say it / lay it*)")
    a("- [ ] Check & Reflect used open questions; big idea named by the **student**")
    a("- [ ] Day 3 automaticity: prompts tallied and/or teach-back completed")
    a("")
    a(f"_Pack cards used: {len(all_words)} · Sort ready_for_phase4: {bool(sort.get('ready_for_phase4'))}_")
    a("")

    return "\n".join(lines)


def markdown_to_simple_html(md: str, pack: dict, sort: dict, columns: list[dict], cards_by_word: dict) -> str:
    """Lightweight HTML wrapper with a visual card strip; body keeps markdown in <pre> plus a gallery."""
    sort_id = sort["id"]
    title = html.escape(sort.get("title") or sort_id)
    gallery = []
    for c in columns:
        cells = []
        for w in c["all_words"]:
            rel = img_rel(w, c["spelling"], cards_by_word, sort_id)
            role = "header" if w == c["header_word"] else ""
            cells.append(
                f'<figure class="{role}">'
                f'<img src="{html.escape(rel)}" alt="{html.escape(w)}" />'
                f"<figcaption><strong>{html.escape(w)}</strong> /{html.escape(c['spelling'])}/"
                f"{' · HEADER' if role else ''}</figcaption></figure>"
            )
        gallery.append(
            f'<section><h3>/{html.escape(c["spelling"])}/ — header: '
            f'{html.escape(c["header_word"])}</h3><div class="row">{"".join(cells)}</div></section>'
        )

    # Very small MD→HTML: escape and convert a few patterns for readability
    body = html.escape(md)
    body = body.replace("\n", "<br>\n")
    # bold
    import re

    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"\*(.+?)\*", r"<em>\1</em>", body)
    body = re.sub(r"`(.+?)`", r"<code>\1</code>", body)
    body = re.sub(r"^# (.+)$", r"<h1>\1</h1>", body, flags=re.M)
    body = re.sub(r"^## (.+)$", r"<h2>\1</h2>", body, flags=re.M)
    body = re.sub(r"^### (.+)$", r"<h3>\1</h3>", body, flags=re.M)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Tutor Script — {title}</title>
<style>
  :root {{ font-family: Georgia, "Times New Roman", serif; color: #1a1a1a; }}
  body {{ max-width: 920px; margin: 2rem auto; padding: 0 1.25rem 3rem; line-height: 1.45; }}
  h1,h2,h3 {{ font-family: "Segoe UI", system-ui, sans-serif; }}
  code {{ background: #f3f3f0; padding: 0.1em 0.35em; }}
  .gallery {{ margin: 1.5rem 0 2rem; padding: 1rem; background: #f7f5f1; border-radius: 8px; }}
  .row {{ display: flex; flex-wrap: wrap; gap: 0.75rem; }}
  figure {{ margin: 0; width: 140px; background: #fff; border: 1px solid #ddd; padding: 0.4rem; }}
  figure.header {{ border-color: #2a6; border-width: 2px; }}
  figure img {{ width: 100%; height: auto; display: block; }}
  figcaption {{ font-size: 0.8rem; margin-top: 0.35rem; font-family: "Segoe UI", system-ui, sans-serif; }}
  .script {{ margin-top: 1rem; }}
  hr {{ border: none; border-top: 1px solid #ccc; margin: 1.5rem 0; }}
</style>
</head>
<body>
  <p><em>Phase 4 tutor script · pack <code>{html.escape(pack.get('pack_id') or '')}</code> ·
  only approved cards below.</em></p>
  <div class="gallery">
    <h2>Approved cards for this sort</h2>
    {''.join(gallery)}
  </div>
  <div class="script">
  {body}
  </div>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4: build tutor script from approved pack")
    ap.add_argument(
        "--pack",
        type=Path,
        default=HERE / "packs" / "l1_m_vs_s_approved",
        help="Path to pack folder containing approved.json",
    )
    ap.add_argument("--sort", default=None, help="Sort id (required if multiple ready sorts)")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: same as pack)",
    )
    args = ap.parse_args()

    pack_dir = args.pack.resolve()
    approved_path = pack_dir / "approved.json"
    if not approved_path.exists():
        sys.exit(f"Missing {approved_path}")

    pack = load_json(approved_path)
    cards_by_word = card_lookup(pack)
    sort = pick_sort(pack, args.sort)
    columns = column_plan(sort, cards_by_word)

    # Attach pronunciations from bank if present next to pack reference
    bank_name = pack.get("bank")
    if bank_name:
        bank_path = HERE / bank_name
        if bank_path.exists():
            bank = load_json(bank_path)
            by_sym = {
                s.get("symbol"): s
                for s in (bank.get("selected_spellings") or bank.get("new_spellings") or [])
            }
            for col in columns:
                meta = by_sym.get(col["spelling"]) or {}
                if meta.get("pronounced"):
                    col["pronounced"] = meta["pronounced"]

    md = build_markdown(pack, sort, columns, cards_by_word)
    out_dir = (args.out_dir or pack_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "tutor_script.md"
    html_path = out_dir / "tutor_script.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(
        markdown_to_simple_html(md, pack, sort, columns, cards_by_word),
        encoding="utf-8",
    )

    # Update README pointer if present
    readme = pack_dir / "README.txt"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        if "tutor_script.md" not in text:
            text = text.rstrip() + (
                "\n\nPhase 4\n"
                "- tutor_script.md   — tutor-facing 3-day closed picture-sort script\n"
                "- tutor_script.html — same script + card gallery\n"
            )
            readme.write_text(text + "\n", encoding="utf-8")

    safe_print(f"Wrote {md_path}")
    safe_print(f"Wrote {html_path}")
    safe_print(f"Sort: {sort.get('id')} · columns: " + ", ".join(
        f"/{c['spelling']}/={len(c['all_words'])}" for c in columns
    ))
    safe_print(f"Headers: " + " | ".join(
        f"{c['header_word']} (+{c['spelling']})" for c in columns
    ))


if __name__ == "__main__":
    main()
