#!/usr/bin/env python3
"""
sort_builder.py — Phase 5 of Word Sort Builder TYCTR
====================================================
Local Sort Builder UI: pick lesson/spellings → build bank → queue card
generation → review → export pack → tutor script.

  python sort_builder.py
  # open http://127.0.0.1:5055

Review gallery is served at /review (same process).
Uses OPENAI_API_KEY from .env for OpenAI generation, or Midjourney
prompt export + image import (no Midjourney API).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory

import build_sort_bank as bsb
import export_approved_pack as eap
import openai_sort_cards as osc
import review_server as rs
import build_tutor_script as bts

HERE = Path(__file__).resolve().parent


def _workspace_root() -> Path:
    """Writable root for cards/packs (cloud hosts may set DATA_DIR)."""
    raw = os.environ.get("DATA_DIR") or os.environ.get("RENDER_DISK_PATH")
    if raw:
        root = Path(raw)
        root.mkdir(parents=True, exist_ok=True)
        return root
    return HERE


WORKSPACE = _workspace_root()
DEFAULT_CARDS = WORKSPACE / "cards"
SELECTIONS_DIR = WORKSPACE / "selections"

app = Flask(__name__)

STATE = {
    "bank_path": HERE / "sort_word_bank_L1.json",
    "cards_dir": DEFAULT_CARDS,
    "manifest_path": DEFAULT_CARDS / "manifest.json",
    "style": osc.DEFAULT_STYLE,
    "model": osc.DEFAULT_MODEL,
    "biemiller_path": osc.DEFAULT_BIEMILLER,
    "spellings_csv": bsb.DEFAULT_SPELLINGS,
    "biemiller_csv": bsb.DEFAULT_BIEMILLER,
    "per_column": 8,
}


def feature_image_create() -> bool:
    """
    When false (default): UI focuses on choosing words + uploading/displaying images.
    When true: also show OpenAI generate / Midjourney prompt export / auto-gen.
    Set FEATURE_IMAGE_CREATE=1 to enable creation tools.
    """
    raw = (os.environ.get("FEATURE_IMAGE_CREATE") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def selection_path_for_bank(bank_path: Path | None = None) -> Path:
    p = bank_path or STATE["bank_path"]
    SELECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SELECTIONS_DIR / f"{p.stem}.selection.json"


def load_selection(bank_path: Path | None = None) -> dict:
    path = selection_path_for_bank(bank_path)
    if not path.exists():
        return {"bank": (bank_path or STATE["bank_path"]).name, "sorts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"bank": (bank_path or STATE["bank_path"]).name, "sorts": {}}
    data.setdefault("sorts", {})
    return data


def save_selection(data: dict, bank_path: Path | None = None) -> Path:
    path = selection_path_for_bank(bank_path)
    data = {
        "bank": (bank_path or STATE["bank_path"]).name,
        "updated_at": _now(),
        "sorts": data.get("sorts") or {},
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def selected_words_for_sort(sort_id: str | None, bank_path: Path | None = None) -> list[str] | None:
    """Return selected words for a sort, or None if no selection saved yet."""
    if not sort_id:
        return None
    sorts = load_selection(bank_path).get("sorts") or {}
    if sort_id not in sorts:
        return None
    words = sorts.get(sort_id) or []
    return [str(w).strip().lower() for w in words if str(w).strip()]


def list_sort_word_candidates(sort_id: str | None) -> dict:
    """Words available for a sort with metrics + selection + image status."""
    bank = load_bank()
    if not bank:
        return {"error": "No bank loaded", "items": []}
    sorts = bank.get("sorts") or []
    if sort_id:
        sorts = [s for s in sorts if s.get("id") == sort_id]
        if not sorts:
            return {"error": f"Sort not found: {sort_id}", "items": []}
    biem = osc.load_biemiller_lookup(STATE["biemiller_path"])
    manifest = osc.load_manifest(STATE["manifest_path"])
    cards = manifest.get("cards") or {}
    selected = set(selected_words_for_sort(sort_id) or [])
    has_explicit = selected_words_for_sort(sort_id) is not None

    by_word: dict[str, dict] = {}
    for s in sorts:
        sid = s.get("id")
        for w in s.get("words") or []:
            word = (w.get("word") or "").strip().lower()
            if not word:
                continue
            if word in by_word:
                if sid and sid not in by_word[word]["sort_ids"]:
                    by_word[word]["sort_ids"].append(sid)
                continue
            bi = biem.get(word) or {}
            entry = cards.get(word) or {}
            img = entry.get("image")
            has_png = bool(
                img and str(img).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                and Path(str(img)).exists()
            )
            mean = w.get("rating_mean")
            if mean is None:
                mean = w.get("mean")
            if mean is None:
                mean = bi.get("rating_mean")
            by_word[word] = {
                "word": word,
                "spelling": w.get("spelling"),
                "position": w.get("position"),
                "rating_mean": mean,
                "freq_pm": w.get("freq_pm") if w.get("freq_pm") is not None else bi.get("freq_pm"),
                "pos": w.get("pos") or bi.get("pos"),
                "meaning": w.get("meaning") or bi.get("meaning"),
                "noun": bool(w.get("noun") if w.get("noun") is not None else str(w.get("pos") or bi.get("pos") or "").lower().startswith("n")),
                "sort_ids": [sid] if sid else [],
                "selected": (word in selected) if has_explicit else True,
                "has_image": has_png,
                "image_status": entry.get("status") if entry else None,
            }
    items = list(by_word.values())
    items.sort(key=lambda x: (-(x.get("rating_mean") or 0), x["word"]))
    return {
        "sort_id": sort_id,
        "has_selection": has_explicit,
        "selected_count": sum(1 for i in items if i["selected"]),
        "items": items,
        "image_create": feature_image_create(),
    }

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MANIFEST_LOCK = threading.Lock()


@app.before_request
def _optional_password_gate():
    """If APP_PASSWORD is set, require HTTP Basic auth (protects OpenAI spend)."""
    from flask import Response

    expected = os.environ.get("APP_PASSWORD") or os.environ.get("BASIC_AUTH_PASSWORD")
    if not expected:
        return None
    if request.path in ("/health",):
        return None
    user = os.environ.get("APP_USER") or os.environ.get("BASIC_AUTH_USER") or "tutor"
    auth = request.authorization
    if auth and auth.username == user and auth.password == expected:
        return None
    return Response(
        "Password required. Set APP_PASSWORD on the host and sign in with HTTP Basic.",
        401,
        {"WWW-Authenticate": 'Basic realm="Word Sort Builder"'},
    )



def sync_review_state() -> None:
    rs.STATE["bank_path"] = STATE["bank_path"]
    rs.STATE["cards_dir"] = STATE["cards_dir"]
    rs.STATE["manifest_path"] = STATE["manifest_path"]
    rs.STATE["style"] = STATE["style"]
    rs.STATE["model"] = STATE["model"]
    rs.STATE["biemiller_path"] = STATE["biemiller_path"]
    if not rs.STATE.get("biemiller_lookup"):
        rs.STATE["biemiller_lookup"] = osc.load_biemiller_lookup(STATE["biemiller_path"])


def load_bank(path: Path | None = None) -> dict | None:
    p = path or STATE["bank_path"]
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def bank_summary(bank: dict, path: Path) -> dict:
    sorts = []
    for s in bank.get("sorts") or []:
        sorts.append(
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "word_count": s.get("word_count"),
                "headers": [h.get("spelling") for h in (s.get("headers") or [])],
                "focus": s.get("focus"),
            }
        )
    spellings = bank.get("selected_spellings") or bank.get("new_spellings") or []
    return {
        "path": str(path),
        "name": path.name,
        "mode": bank.get("mode"),
        "focus_lesson": bank.get("focus_lesson"),
        "requested_lesson": bank.get("requested_lesson"),
        "requested_lessons": bank.get("requested_lessons"),
        "requested_spellings": bank.get("requested_spellings"),
        "spellings": [
            {
                "symbol": s.get("symbol"),
                "as_in": s.get("as_in"),
                "pronounced": s.get("pronounced"),
                "lesson": s.get("lesson"),
            }
            for s in spellings
        ],
        "sort_count": len(sorts),
        "sorts": sorts,
    }


def list_bank_files() -> list[dict]:
    files = sorted(HERE.glob("sort_word_bank*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files:
        try:
            bank = json.loads(p.read_text(encoding="utf-8"))
            out.append(bank_summary(bank, p))
        except Exception as e:
            out.append({"path": str(p), "name": p.name, "error": str(e)})
    return out


def list_packs() -> list[dict]:
    packs_dir = WORKSPACE / "packs"
    if not packs_dir.exists():
        return []
    out = []
    for d in sorted(packs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        approved = d / "approved.json"
        info = {"id": d.name, "path": str(d), "has_script": (d / "tutor_script.html").exists()}
        if approved.exists():
            try:
                pack = json.loads(approved.read_text(encoding="utf-8"))
                info["card_count"] = pack.get("card_count")
                info["ready_sorts"] = (pack.get("phase4_hint") or {}).get("ready_sorts") or []
            except Exception:
                pass
        out.append(info)
    return out


def queue_preview(sort_id: str | None, limit: int | None) -> dict:
    bank = load_bank()
    if not bank:
        return {"error": "No bank loaded", "items": [], "pending": []}
    biem = osc.load_biemiller_lookup(STATE["biemiller_path"])
    items = osc.unique_words_from_bank(bank, sort_id, None, biemiller_lookup=biem)
    manifest = osc.load_manifest(STATE["manifest_path"])
    pending = []
    already = []
    for item in items:
        prev = (manifest.get("cards") or {}).get(item["word"])
        has_png = bool(
            prev
            and prev.get("image")
            and str(prev.get("image")).lower().endswith(".png")
            and Path(prev["image"]).exists()
        )
        status = (prev or {}).get("status")
        if prev and status in ("accepted", "pending_review") and has_png:
            already.append({**item, "status": status})
        else:
            pending.append({**item, "status": status or "new"})
    capped = pending[:limit] if limit is not None else pending
    return {
        "total_unique": len(items),
        "already_have": len(already),
        "pending_count": len(pending),
        "will_generate": len(capped),
        "pending": capped,
        "already": already[:40],
    }


def run_generate_job(
    job_id: str,
    sort_id: str | None,
    limit: int | None,
    dry_run: bool,
    concurrency: int = 3,
    words: list[str] | None = None,
    mode: str = "generate",
) -> None:
    def set_fields(**kwargs):
        with JOBS_LOCK:
            JOBS[job_id].update(kwargs)

    def append_log(line: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id].setdefault("log", []).append(line)

    def bump_done(word: str, status: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id]["done"] = int(JOBS[job_id].get("done") or 0) + 1
            active = list(JOBS[job_id].get("active") or [])
            if word in active:
                active.remove(word)
            JOBS[job_id]["active"] = active
            JOBS[job_id]["current"] = ", ".join(active) if active else None
            JOBS[job_id].setdefault("log", []).append(f"{word}: {status}")
            JOBS[job_id].setdefault("results", []).append({"word": word, "status": status})

    def mark_active(word: str) -> None:
        with JOBS_LOCK:
            active = list(JOBS[job_id].get("active") or [])
            if word not in active:
                active.append(word)
            JOBS[job_id]["active"] = active
            JOBS[job_id]["current"] = ", ".join(active)

    try:
        osc.load_dotenv(HERE / ".env")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not dry_run and not api_key:
            set_fields(status="error", error="OPENAI_API_KEY missing in .env", finished_at=_now())
            return

        bank = load_bank()
        if not bank:
            set_fields(status="error", error="Bank not found", finished_at=_now())
            return

        biem = osc.load_biemiller_lookup(STATE["biemiller_path"])
        outdir = STATE["cards_dir"]
        outdir.mkdir(parents=True, exist_ok=True)
        manifest_path = STATE["manifest_path"]

        if words:
            only = {w.strip().lower() for w in words if w and w.strip()}
            items = osc.unique_words_from_bank(
                bank, sort_id, only, biemiller_lookup=biem
            )
            found = {i["word"] for i in items}
            # Fill gaps from manifest so regenerate works even if bank filter differs
            with MANIFEST_LOCK:
                manifest = osc.load_manifest(manifest_path)
            for w in only:
                if w in found:
                    continue
                entry = (manifest.get("cards") or {}).get(w) or {}
                bi = biem.get(w) or {}
                items.append(
                    {
                        "word": w,
                        "spelling": entry.get("spelling") or bi.get("spelling"),
                        "as_in": entry.get("as_in"),
                        "prompt": entry.get("prompt"),
                        "sort_ids": entry.get("sort_ids") or [],
                        "rating_mean": entry.get("rating_mean", bi.get("rating_mean")),
                        "freq_pm": entry.get("freq_pm", bi.get("freq_pm")),
                        "meaning": entry.get("meaning") or bi.get("meaning"),
                        "pos": entry.get("pos") or bi.get("pos"),
                    }
                )
            pending = items
        else:
            items = osc.unique_words_from_bank(bank, sort_id, None, biemiller_lookup=biem)
            with MANIFEST_LOCK:
                manifest = osc.load_manifest(manifest_path)
            pending = []
            for item in items:
                prev = (manifest.get("cards") or {}).get(item["word"])
                has_png = bool(
                    prev
                    and prev.get("image")
                    and str(prev.get("image")).lower().endswith(".png")
                    and Path(prev["image"]).exists()
                )
                if prev and prev.get("status") in ("accepted", "pending_review") and has_png:
                    continue
                pending.append(item)
            if limit is not None:
                pending = pending[:limit]

        concurrency = max(1, min(int(concurrency or 3), 6))
        est_each = 20  # rough seconds per OpenAI image
        est_total = int((len(pending) * est_each) / concurrency) if pending else 0

        set_fields(
            status="running",
            mode=mode,
            total=len(pending),
            done=0,
            current=None,
            active=[],
            results=[],
            concurrency=concurrency,
            eta_seconds=est_total,
            log=[
                f"{'Regenerating' if mode == 'regenerate' else 'Generating'} "
                f"{len(pending)} card(s) · concurrency {concurrency} · "
                f"~{est_total}s estimated"
            ],
        )

        if not pending:
            set_fields(
                status="done",
                message="Nothing to do — no matching words.",
                finished_at=_now(),
            )
            return

        client = (
            osc.DryRunImageClient()
            if dry_run
            else osc.OpenAIImageClient(api_key, STATE["model"])
        )

        def process_one(item: dict) -> tuple[str, str]:
            word = item["word"]
            mark_active(word)
            try:
                with MANIFEST_LOCK:
                    manifest = osc.load_manifest(manifest_path)
                    prev_entry = (manifest.get("cards") or {}).get(word)
                result = osc.process_word(
                    client,
                    item,
                    outdir,
                    STATE["style"],
                    dry_run=dry_run,
                    auto_accept=True,
                    entry=prev_entry,
                )
                with MANIFEST_LOCK:
                    manifest = osc.load_manifest(manifest_path)
                    manifest.setdefault("cards", {})[word] = {
                        **result,
                        "updated_at": _now(),
                        "auto_accepted": True if result.get("status") == "pending_review" else result.get("auto_accepted"),
                    }
                    # For regenerate mode, clear human-accepted flag so it needs review again
                    if mode == "regenerate":
                        manifest["cards"][word]["status"] = "pending_review"
                        manifest["cards"][word]["auto_accepted"] = False
                    osc.save_manifest(manifest_path, manifest)
                if result.get("image"):
                    osc.patch_bank_images(
                        STATE["bank_path"],
                        word,
                        result["image"],
                        "pending_review",
                    )
                status = result.get("status") or "pending_review"
                bump_done(word, status)
                return word, status
            except Exception as e:
                bump_done(word, f"failed: {e}")
                with MANIFEST_LOCK:
                    manifest = osc.load_manifest(manifest_path)
                    entry = (manifest.get("cards") or {}).get(word, {"word": word})
                    entry.update({"status": "failed", "error": str(e), "updated_at": _now()})
                    manifest.setdefault("cards", {})[word] = entry
                    osc.save_manifest(manifest_path, manifest)
                return word, f"failed: {e}"

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(process_one, item) for item in pending]
            for fut in as_completed(futures):
                fut.result()

        with JOBS_LOCK:
            results = list(JOBS[job_id].get("results") or [])
        set_fields(
            status="done",
            done=len(pending),
            current=None,
            active=[],
            results=results,
            message=(
                f"Finished {len(pending)} card(s). "
                "Images are ready in Review — approve or rework."
            ),
            finished_at=_now(),
        )
    except Exception as e:
        set_fields(
            status="error",
            error=str(e),
            traceback=traceback.format_exc(),
            finished_at=_now(),
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Builder UI
# ---------------------------------------------------------------------------

BUILDER_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Word Sort Builder</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Outfit:wght@400;500;600&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg: #e8efe9;
    --bg2: #d5e3d9;
    --ink: #14201a;
    --muted: #4a5c52;
    --panel: rgba(255,252,248,0.92);
    --line: #b7c9bc;
    --accent: #0f6b5c;
    --accent2: #c45c26;
    --ok: #1b7a45;
    --warn: #9a6b00;
    --err: #a12828;
    --shadow: 0 12px 40px rgba(20,32,26,0.08);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    color: var(--ink);
    font-family: Outfit, system-ui, sans-serif;
    background:
      radial-gradient(1200px 600px at 10% -10%, #f4f7e8 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #cfe4dc 0%, transparent 50%),
      linear-gradient(165deg, var(--bg), var(--bg2));
  }
  header {
    padding: 1.75rem 1.5rem 1rem;
    max-width: 1100px;
    margin: 0 auto;
  }
  header h1 {
    font-family: Fraunces, Georgia, serif;
    font-weight: 700;
    font-size: clamp(1.8rem, 3vw, 2.4rem);
    margin: 0 0 0.35rem;
    letter-spacing: -0.02em;
  }
  header p { margin: 0; color: var(--muted); max-width: 40rem; }
  nav.steps {
    display: flex; flex-wrap: wrap; gap: 0.4rem;
    max-width: 1100px; margin: 0 auto 1rem; padding: 0 1.5rem;
  }
  nav.steps span {
    font-size: 0.78rem; font-weight: 600; letter-spacing: 0.04em;
    text-transform: uppercase; color: var(--muted);
    padding: 0.35rem 0.7rem; border: 1px solid var(--line); border-radius: 999px;
    background: rgba(255,255,255,0.45);
  }
  main {
    max-width: 1280px; margin: 0 auto; padding: 0 1.5rem 3rem;
    display: grid; gap: 1rem;
  }
  @media (min-width: 900px) {
    main { grid-template-columns: 1.1fr 0.9fr; align-items: start; }
    .span2 { grid-column: 1 / -1; }
  }
  .panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 16px; padding: 1.15rem 1.25rem; box-shadow: var(--shadow);
  }
  .panel h2 {
    font-family: Fraunces, Georgia, serif;
    font-size: 1.25rem; margin: 0 0 0.75rem; font-weight: 600;
  }
  .panel h3 { font-size: 0.95rem; margin: 1rem 0 0.4rem; color: var(--muted); }
  label { display: block; font-size: 0.85rem; font-weight: 600; margin: 0.65rem 0 0.25rem; }
  input, select, textarea {
    width: 100%; padding: 0.55rem 0.7rem; border: 1px solid var(--line);
    border-radius: 8px; font: inherit; background: #fff; color: var(--ink);
  }
  .row { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: end; }
  .row > * { flex: 1; min-width: 120px; }
  .modes { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.5rem; }
  .modes button {
    flex: 1; min-width: 110px;
  }
  button, .btn {
    appearance: none; border: none; cursor: pointer;
    background: var(--accent); color: #fff; font: inherit; font-weight: 600;
    padding: 0.6rem 1rem; border-radius: 8px;
  }
  button.secondary, .btn.secondary {
    background: transparent; color: var(--ink); border: 1px solid var(--line);
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.active { outline: 2px solid var(--accent); outline-offset: 2px; }
  .muted { color: var(--muted); font-size: 0.9rem; }
  .ok { color: var(--ok); }
  .warn { color: var(--warn); }
  .err { color: var(--err); }
  .status { margin-top: 0.75rem; font-size: 0.9rem; white-space: pre-wrap; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th, td { text-align: left; padding: 0.4rem 0.35rem; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; }
  .chip {
    display: inline-block; padding: 0.15rem 0.45rem; border-radius: 999px;
    border: 1px solid var(--line); font-size: 0.75rem; margin: 0.1rem;
  }
  .log {
    max-height: 180px; overflow: auto; background: #14201a; color: #d7ebe1;
    font-family: ui-monospace, Consolas, monospace; font-size: 0.78rem;
    padding: 0.75rem; border-radius: 8px; margin-top: 0.5rem;
  }
  .links a { color: var(--accent); font-weight: 600; margin-right: 1rem; }
  .hidden { display: none !important; }
  .review-layout {
    display: grid; grid-template-columns: 220px 1fr; gap: 1rem; margin-top: 0.5rem;
  }
  @media (max-width: 860px) { .review-layout { grid-template-columns: 1fr; } }
  .review-queue { max-height: 520px; overflow: auto; }
  .review-queue button {
    display: block; width: 100%; text-align: left; margin: 0 0 4px;
    background: #fff; color: var(--ink); border: 1px solid var(--line);
    padding: 0.5rem 0.65rem; border-radius: 8px; font: inherit; cursor: pointer;
  }
  .review-queue button.active, .review-queue button:hover {
    border-color: var(--accent); background: #e7f3ef;
  }
  .badge {
    display: inline-block; font-size: 0.72rem; padding: 0.1rem 0.4rem;
    border-radius: 999px; border: 1px solid var(--line); color: var(--muted);
  }
  .badge.accepted { color: var(--ok); border-color: var(--ok); }
  .badge.needs_rework, .badge.pending_review { color: var(--warn); border-color: var(--warn); }
  .badge.failed, .badge.skipped { color: var(--err); border-color: var(--err); }
  .viewer { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 860px) { .viewer { grid-template-columns: 1fr; } }
  .img-wrap {
    background: #eef4ef; border: 1px solid var(--line); border-radius: 10px;
    min-height: 280px; display: flex; align-items: center; justify-content: center; overflow: hidden;
  }
  .img-wrap img { max-width: 100%; max-height: 420px; display: block; }
  .prompt {
    font-family: ui-monospace, Consolas, monospace; font-size: 0.75rem;
    background: #f4f7f4; border: 1px solid var(--line); border-radius: 8px;
    padding: 0.6rem; white-space: pre-wrap; max-height: 120px; overflow: auto;
  }
  .actions { display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.75rem; }
  .filters { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.6rem; }
  .filters button { padding: 0.35rem 0.65rem; font-size: 0.8rem; }
  .filters button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .history { margin-top: 0.75rem; border-top: 1px solid var(--line); padding-top: 0.6rem; }
  .history-item {
    display: grid; grid-template-columns: 64px 1fr auto; gap: 0.5rem;
    align-items: center; margin-bottom: 0.45rem; padding: 0.4rem;
    border: 1px solid var(--line); border-radius: 8px; background: #fff;
  }
  .history-item img { width: 64px; height: 64px; object-fit: cover; border-radius: 6px; }
  .history-item.current { border-color: var(--accent); background: #e7f3ef; }
  .meta-sm { font-size: 0.78rem; color: var(--muted); }
  .progress {
    height: 10px; background: #d7e4dc; border-radius: 999px; overflow: hidden; margin-top: 0.55rem;
  }
  .progress > span {
    display: block; height: 100%; width: 0%; background: var(--accent);
    transition: width 0.35s ease;
  }
  .img-wrap.loading {
    position: relative; flex-direction: column; gap: 0.75rem; color: var(--muted);
    min-height: 320px;
  }
  .spinner {
    width: 42px; height: 42px; border-radius: 50%;
    border: 3px solid #c5d9ce; border-top-color: var(--accent);
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .review-queue label.pick {
    display: flex; gap: 0.45rem; align-items: flex-start;
    margin: 0 0 4px; padding: 0.35rem 0.4rem; border: 1px solid var(--line);
    border-radius: 8px; background: #fff; cursor: pointer;
  }
  .review-queue label.pick.active { border-color: var(--accent); background: #e7f3ef; }
  .review-queue label.pick input { margin-top: 0.25rem; }
  .review-layout { grid-template-columns: 200px minmax(0, 1fr); }
  .card-stage-wrap { max-width: 520px; margin: 0 auto; }
  .card-stage {
    position: relative; touch-action: none; user-select: none; cursor: grab;
    border-radius: 14px; overflow: hidden; border: 2px solid var(--line);
    background: #eef4ef; min-height: 300px;
  }
  .card-stage.dragging { cursor: grabbing; }
  .card-stage .img-wrap { border: none; border-radius: 0; min-height: 300px; }
  .swipe-overlay {
    position: absolute; inset: 0; pointer-events: none; opacity: 0;
    display: flex; align-items: center; justify-content: center;
    font-family: Fraunces, Georgia, serif; font-size: 2rem; font-weight: 700;
    transition: opacity 0.12s ease;
  }
  .swipe-overlay.approve { background: rgba(27, 122, 69, 0.35); color: #0d4a28; }
  .swipe-overlay.reject { background: rgba(161, 40, 40, 0.35); color: #6b1515; }
  .swipe-overlay.show { opacity: 1; }
  .rating-bar {
    display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.5rem;
    margin-top: 0.65rem;
  }
  .rating-bar button {
    padding: 0.85rem 0.5rem; font-size: 1rem; border-radius: 10px;
    min-height: 52px;
  }
  .rate-reject { background: #a12828; color: #fff; }
  .rate-reject:hover { background: #8a2020; }
  .rate-approve { background: #1b7a45; color: #fff; }
  .rate-approve:hover { background: #156338; }
  .rate-mid { background: #fff; color: var(--ink); border: 2px solid var(--line); }
  .rating-hints {
    text-align: center; font-size: 0.78rem; color: var(--muted); margin-top: 0.4rem;
  }
  .card-nav {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 0.5rem; gap: 0.5rem;
  }
  .card-nav button { padding: 0.4rem 0.75rem; font-size: 0.85rem; }
  .card-meta-compact { margin-top: 0.75rem; font-size: 0.88rem; }
  .card-meta-compact details { margin-top: 0.5rem; }
  .queue-tools {
    display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.5rem;
  }
  .queue-tools button { padding: 0.35rem 0.55rem; font-size: 0.78rem; }
  .review-queue label.pick .word-hit {
    flex: 1; cursor: pointer; padding: 0.15rem 0;
  }
  .drawer {
    border: 1px solid var(--line); border-radius: 10px; margin-top: 0.75rem;
    background: rgba(255,255,255,0.55);
  }
  .drawer > summary {
    cursor: pointer; list-style: none; padding: 0.65rem 0.85rem;
    font-weight: 600; color: var(--muted); font-size: 0.9rem;
    display: flex; justify-content: space-between; align-items: center;
  }
  .drawer > summary::-webkit-details-marker { display: none; }
  .drawer > summary::after { content: "▸"; color: var(--accent); }
  .drawer[open] > summary::after { content: "▾"; }
  .drawer-body { padding: 0 0.85rem 0.85rem; }
  .active-chip {
    display: inline-block; margin-left: 0.35rem; padding: 0.1rem 0.45rem;
    border-radius: 999px; background: #e7f3ef; border: 1px solid var(--accent);
    color: var(--accent); font-size: 0.75rem; font-weight: 600;
  }
  .script-editor {
    width: 100%; min-height: 360px; font-family: ui-monospace, Consolas, monospace;
    font-size: 0.82rem; line-height: 1.4; margin-top: 0.5rem;
  }
  .flow-note { font-size: 0.85rem; color: var(--muted); margin: 0 0 0.75rem; }
  .word-pick-tools { display:flex; flex-wrap:wrap; gap:0.4rem; margin:0.5rem 0 0.75rem; }
  .word-pick-tools button { padding:0.35rem 0.65rem; font-size:0.8rem; }
  #wordPickTable table { font-size:0.85rem; }
  #wordPickTable td.meaning { max-width:220px; color:var(--muted); }
  #wordPickTable tr.unselected { opacity:0.45; }
  .word-pick-layout {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.85rem;
    align-items: stretch;
    margin-top: 0.35rem;
  }
  @media (min-width: 980px) {
    .word-pick-layout {
      grid-template-columns: minmax(0, 1.05fr) minmax(300px, 0.95fr);
    }
    .word-pick-layout.scope-wide {
      grid-template-columns: minmax(0, 0.55fr) minmax(0, 1.45fr);
    }
  }
  .word-pick-list {
    max-height: 560px; overflow: auto;
    border: 1px solid var(--line); border-radius: 10px;
    background: #fff; padding: 0.35rem 0.5rem;
  }
  .scope-pane {
    display: flex; flex-direction: column; min-height: 420px;
    border: 1px solid var(--line); border-radius: 10px;
    background: rgba(255,255,255,0.7); overflow: hidden;
  }
  .scope-toolbar {
    display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center;
    padding: 0.5rem 0.65rem; border-bottom: 1px solid var(--line);
    background: #f4f8f5;
  }
  .scope-toolbar strong { font-size: 0.82rem; margin-right: 0.25rem; }
  .scope-toolbar button { padding: 0.3rem 0.55rem; font-size: 0.78rem; }
  .scope-zoom-label {
    min-width: 3.2rem; text-align: center; font-size: 0.78rem;
    color: var(--muted); font-variant-numeric: tabular-nums;
  }
  .scope-frame-wrap {
    position: relative; flex: 1; min-height: 380px; overflow: auto;
    background: #e8efe9;
  }
  .scope-frame-inner {
    transform-origin: 0 0; width: 100%; height: 100%; min-height: 380px;
  }
  .scope-frame-inner iframe, .scope-frame-inner embed {
    width: 100%; height: 100%; min-height: 380px; border: 0; display: block;
    background: #fff;
  }
  .scope-modal {
    position: fixed; inset: 0; z-index: 80;
    background: rgba(18, 28, 22, 0.55);
    display: flex; align-items: stretch; justify-content: center;
    padding: 1rem;
  }
  .scope-modal.hidden { display: none !important; }
  .scope-modal-panel {
    width: min(1200px, 100%); background: var(--panel);
    border-radius: 14px; border: 1px solid var(--line);
    box-shadow: var(--shadow); display: flex; flex-direction: column;
    overflow: hidden;
  }
  .scope-modal .scope-frame-wrap { min-height: calc(100vh - 7rem); }
  .scope-modal .scope-frame-inner,
  .scope-modal .scope-frame-inner iframe,
  .scope-modal .scope-frame-inner embed { min-height: calc(100vh - 7rem); }
  .feat-off { display:none !important; }
</style>
</head>
<body>
<header>
  <h1>Word Sort Builder</h1>
  <p>Build a bank → choose words → upload pictures → review → export pack &amp; edit the tutor script.</p>
</header>
<nav class="steps">
  <span>1 Bank</span><span>2 Choose words</span><span>3 Pictures</span><span>4 Pack &amp; script</span>
</nav>
<main>
  <section class="panel" id="bankPanel">
    <h2>1. Build word bank</h2>
    <p class="flow-note">Build a bank for a lesson. Generation (step 2) automatically uses the <strong>most recent</strong> bank.</p>
    <div class="modes">
      <button type="button" class="secondary active" data-mode="lesson">Focus lesson</button>
      <button type="button" class="secondary" data-mode="lessons">Lessons list</button>
      <button type="button" class="secondary" data-mode="spellings">Spellings</button>
    </div>

    <div id="mode-lesson">
      <label>Lesson number</label>
      <input id="lesson" type="number" min="1" value="1" />
      <div class="row">
        <div>
          <label>Previous contrasts</label>
          <input id="previous" type="number" min="0" value="2" />
        </div>
        <div>
          <label>Or explicit contrast (e.g. g,sh)</label>
          <input id="contrast" type="text" placeholder="optional" />
        </div>
      </div>
    </div>
    <div id="mode-lessons" class="hidden">
      <label>Lessons (comma-separated)</label>
      <input id="lessons" type="text" placeholder="14,19,35" />
    </div>
    <div id="mode-spellings" class="hidden">
      <label>Spellings (comma-separated; aliases ok: a:, ee)</label>
      <input id="spellingsList" type="text" placeholder="m,s,sh,ch" />
    </div>
    <div class="row">
      <div>
        <label>Words per column</label>
        <input id="perColumn" type="number" min="2" max="40" value="8" />
      </div>
    </div>
    <div class="row" style="margin-top:0.9rem">
      <button type="button" id="btnBuild">Build bank</button>
    </div>
    <p class="status" id="bankStatus"></p>
    <details class="drawer" id="banksDrawer">
      <summary>
        <span>Saved banks <span class="active-chip" id="activeBankChip">—</span></span>
      </summary>
      <div class="drawer-body">
        <p class="muted" style="margin:0 0 0.5rem">Optional: pick an older bank. Default is the newest.</p>
        <div class="row" style="margin-bottom:0.5rem">
          <button type="button" class="secondary" id="btnRefreshBanks">Refresh list</button>
        </div>
        <div id="bankList" class="muted">Loading…</div>
      </div>
    </details>
  </section>

  <section class="panel span2" id="queuePanel">
    <h2>2. Choose words for the sort</h2>
    <p class="flow-note" id="activeBankLabel">Using newest bank: —</p>
    <p class="flow-note">Check the words you want in this closed sort. Tutor scripts use only these words (and their pictures once uploaded). Default bank list is ranked by Rating.Mean. The TYCTR scope &amp; sequence PDF sits beside the list — zoom, widen, or expand it while you choose.</p>
    <label>Sort</label>
    <select id="sortSelect"><option value="">Pick a sort…</option></select>
    <div class="word-pick-tools">
      <button type="button" class="secondary" id="btnSelectAllWords">Select all</button>
      <button type="button" class="secondary" id="btnSelectNouns">Nouns only</button>
      <button type="button" class="secondary" id="btnSelectTop8">Top 8 by Mean</button>
      <button type="button" class="secondary" id="btnClearWords">Clear</button>
      <button type="button" id="btnSaveWords">Save word selection</button>
    </div>
    <p class="status" id="wordStatus"></p>
    <div class="word-pick-layout" id="wordPickLayout">
      <div class="word-pick-list muted" id="wordPickTable">Pick a sort to load candidates.</div>
      <aside class="scope-pane" id="scopePane" aria-label="TYCTR scope and sequence">
        <div class="scope-toolbar">
          <strong>TYCTR scope &amp; sequence</strong>
          <button type="button" class="secondary" id="btnScopeZoomOut" title="Zoom out">−</button>
          <span class="scope-zoom-label" id="scopeZoomLabel">100%</span>
          <button type="button" class="secondary" id="btnScopeZoomIn" title="Zoom in">+</button>
          <button type="button" class="secondary" id="btnScopeZoomReset">Fit</button>
          <button type="button" class="secondary" id="btnScopeWiden">Widen</button>
          <button type="button" class="secondary" id="btnScopeExpand">Expand</button>
          <a class="secondary" id="linkScopePdf" href="/docs/tyctr-scope-sequence.pdf" target="_blank" rel="noopener"
             style="display:inline-flex;align-items:center;padding:0.3rem 0.55rem;font-size:0.78rem;text-decoration:none;border-radius:8px;border:1px solid var(--line);font-weight:600;color:var(--ink)">Open PDF</a>
        </div>
        <div class="scope-frame-wrap" id="scopeFrameWrap">
          <div class="scope-frame-inner" id="scopeFrameInner">
            <iframe id="scopePdfFrame" title="TYCTR scope and sequence PDF"
              src="/docs/tyctr-scope-sequence.pdf#toolbar=1&amp;navpanes=0"></iframe>
          </div>
        </div>
      </aside>
    </div>

    <div id="scopeModal" class="scope-modal hidden" role="dialog" aria-modal="true" aria-label="Expanded TYCTR scope PDF">
      <div class="scope-modal-panel">
        <div class="scope-toolbar">
          <strong>TYCTR scope &amp; sequence</strong>
          <button type="button" class="secondary" id="btnScopeZoomOutModal">−</button>
          <span class="scope-zoom-label" id="scopeZoomLabelModal">100%</span>
          <button type="button" class="secondary" id="btnScopeZoomInModal">+</button>
          <button type="button" class="secondary" id="btnScopeZoomResetModal">Fit</button>
          <a class="secondary" href="/docs/tyctr-scope-sequence.pdf" target="_blank" rel="noopener"
             style="display:inline-flex;align-items:center;padding:0.3rem 0.55rem;font-size:0.78rem;text-decoration:none;border-radius:8px;border:1px solid var(--line);font-weight:600;color:var(--ink)">Open PDF</a>
          <button type="button" id="btnScopeCloseModal" style="margin-left:auto">Close</button>
        </div>
        <div class="scope-frame-wrap">
          <div class="scope-frame-inner" id="scopeFrameInnerModal">
            <iframe id="scopePdfFrameModal" title="TYCTR scope and sequence PDF (expanded)"
              src="/docs/tyctr-scope-sequence.pdf#toolbar=1&amp;navpanes=0"></iframe>
          </div>
        </div>
      </div>
    </div>

    <h3 style="margin-top:1.1rem">Upload pictures</h3>
    <p class="flow-note">Save files as <code>word.png</code> (e.g. <code>man.png</code>), then import. Review/approve in step 3. Scripts still gradually release the same selected words across the week.</p>
    <div class="row">
      <label class="secondary" style="display:inline-flex;align-items:center;gap:0.4rem;padding:0.55rem 0.9rem;border-radius:8px;border:1px solid var(--line);cursor:pointer;font-weight:600">
        Import images
        <input type="file" id="mjImportFiles" accept="image/png,image/jpeg,.png,.jpg,.jpeg" multiple hidden />
      </label>
    </div>
    <p class="status" id="mjStatus"></p>

    <details class="drawer feat-image-create" id="imageCreateDrawer" style="margin-top:1rem">
      <summary><span>Image creation tools <span class="active-chip">feature flag</span></span></summary>
      <div class="drawer-body">
        <p class="flow-note">Enabled with <code>FEATURE_IMAGE_CREATE=1</code>. Kept for OpenAI generate / Midjourney prompt export.</p>
        <div class="row">
          <div>
            <label>Next N words</label>
            <input id="limit" type="number" min="1" max="50" value="4" />
          </div>
          <div>
            <label>Parallel jobs (1–6)</label>
            <input id="concurrency" type="number" min="1" max="6" value="3" />
          </div>
          <div>
            <label>&nbsp;</label>
            <label style="font-weight:500"><input type="checkbox" id="dryRun" /> Dry run</label>
          </div>
        </div>
        <div class="row" style="margin-top:0.75rem">
          <button type="button" class="secondary" id="btnPreview">Preview queue</button>
          <button type="button" id="btnGenerate">Generate with OpenAI</button>
          <button type="button" class="secondary" id="btnMjExport">Export Midjourney prompts</button>
        </div>
        <p class="status" id="genStatus"></p>
        <div id="genProgressWrap" class="hidden">
          <div class="progress"><span id="genProgressBar"></span></div>
          <p class="muted" id="genEta" style="margin:0.35rem 0 0"></p>
        </div>
        <div id="jobLog" class="log hidden"></div>
        <div id="pendingTable" class="muted" style="margin-top:0.5rem"></div>
      </div>
    </details>
  </section>

  <section class="panel span2" id="reviewPanel">
    <h2>3. Review pictures</h2>
    <p class="muted" id="reviewStats">Loading review queue…</p>
    <div class="row" style="margin-bottom:0.5rem; align-items:center; flex-wrap:wrap">
      <button type="button" class="secondary" id="btnRefreshReview">Refresh</button>
      <button type="button" class="secondary feat-image-create" id="btnRegenSelected">Regen selected</button>
      <button type="button" class="secondary" id="btnApproveSelected">Approve selected</button>
      <button type="button" class="secondary" id="btnRejectSelected">Reject selected</button>
      <label style="font-weight:500; margin:0">
        <input type="checkbox" id="autoAdvance" checked />
        Auto-advance after rating
      </label>
      <label class="feat-image-create" style="font-weight:500; margin:0">
        <input type="checkbox" id="autoGenMissing" />
        Auto-generate missing images
      </label>
    </div>
    <p class="muted" style="margin:0 0 0.5rem">Swipe image right = approve · left = reject · keys: <kbd>A</kbd> approve · <kbd>X</kbd> reject · <kbd>←</kbd><kbd>→</kbd> navigate</p>
    <p class="status" id="batchStatus"></p>
    <div id="batchProgressWrap" class="hidden">
      <div class="progress"><span id="batchProgressBar"></span></div>
      <p class="muted" id="batchEta" style="margin:0.35rem 0 0"></p>
    </div>
    <div class="review-layout">
      <aside>
        <div class="filters" id="reviewFilters"></div>
        <div class="queue-tools">
          <button type="button" class="secondary" id="btnSelectAllVisible">Select all</button>
          <button type="button" class="secondary" id="btnClearSelection">Clear</button>
        </div>
        <div class="review-queue" id="reviewQueue"></div>
      </aside>
      <div id="reviewDetail"><div class="muted">Select a card from the queue.</div></div>
    </div>
  </section>

  <section class="panel span2" id="packScriptPanel">
    <h2>4. Pack &amp; tutor script</h2>
    <p class="flow-note">Export accepted cards into a pack, then edit the tutor script on this same screen.</p>
    <div class="row">
      <div>
        <label>Sort for pack / script</label>
        <input id="exportSort" type="text" placeholder="e.g. L1_m_vs_s" />
      </div>
      <div>
        <label>&nbsp;</label>
        <button type="button" id="btnExportScript">Export pack &amp; build script</button>
      </div>
      <div>
        <label>&nbsp;</label>
        <button type="button" class="secondary" id="btnSaveScript">Save script edits</button>
      </div>
    </div>
    <p class="links" style="margin-top:0.75rem">
      <a href="#" id="linkPack" class="hidden">Open pack preview</a>
      <a href="#" id="linkScript" class="hidden">Open script HTML</a>
    </p>
    <p class="status" id="exportStatus"></p>
    <label for="scriptEditor"><strong>Tutor script</strong> (edit Markdown, then Save)</label>
    <textarea id="scriptEditor" class="script-editor" placeholder="Export a pack to load the tutor script here…"></textarea>
    <input type="hidden" id="scriptPackId" value="" />
    <h3>Recent packs</h3>
    <div id="packList" class="muted">—</div>
  </section>
</main>
<script>
const FEATURES = __FEATURES_JSON__;
let mode = "lesson";
let activeBank = null;
let pollTimer = null;
let reviewFilter = "needs_attention";
let reviewCards = [];
let reviewCurrent = null;
let reviewBusy = false;
let selectedWords = new Set();
let loadingWords = new Set();
let autoGenStarted = new Set();
let swipeBound = false;
let preferNewestBank = true;
let wordCandidates = [];
const REVIEW_FILTERS = ["needs_attention", "all", "accepted", "needs_rework", "pending_review", "failed", "skipped"];

function applyFeatureFlags() {
  const on = !!(FEATURES && FEATURES.image_create);
  document.querySelectorAll(".feat-image-create").forEach(el => {
    el.classList.toggle("feat-off", !on);
  });
  const autoGen = document.getElementById("autoGenMissing");
  if (autoGen && !on) autoGen.checked = false;
}
applyFeatureFlags();

function reviewList() {
  return filteredReview();
}

function reviewIndex(word) {
  return reviewList().findIndex(c => c.word === word);
}

function pickReviewWordAt(index) {
  const list = reviewList();
  if (!list.length) return null;
  const i = Math.max(0, Math.min(index, list.length - 1));
  return list[i]?.word || null;
}

function advanceReviewWord(fromWord) {
  if (!document.getElementById("autoAdvance")?.checked) return;
  const list = reviewList();
  const idx = list.findIndex(c => c.word === fromWord);
  const next = list[idx + 1] || list[idx - 1] || list[0];
  if (next && next.word !== fromWord) {
    selectReviewWord(next.word, { skipAutoGen: true });
  } else if (next) {
    selectReviewWord(next.word, { skipAutoGen: true });
  }
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function setMode(m) {
  mode = m;
  document.querySelectorAll(".modes button").forEach(b => {
    b.classList.toggle("active", b.dataset.mode === m);
  });
  ["lesson","lessons","spellings"].forEach(id => {
    document.getElementById("mode-" + id).classList.toggle("hidden", id !== m);
  });
}

document.querySelectorAll(".modes button").forEach(b => {
  b.onclick = () => setMode(b.dataset.mode);
});

function fmtNum(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  return Number.isNaN(x) ? "—" : x.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, ch => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
  })[ch]);
}

function badge(status) {
  return `<span class="badge ${status || ""}">${status || "unknown"}</span>`;
}

function filteredReview() {
  if (reviewFilter === "all") return reviewCards;
  if (reviewFilter === "needs_attention") {
    return reviewCards.filter(c =>
      ["needs_rework", "pending_review", "failed"].includes(c.status) || !c.has_image
    );
  }
  return reviewCards.filter(c => c.status === reviewFilter);
}

function renderReviewFilters() {
  const el = document.getElementById("reviewFilters");
  el.innerHTML = REVIEW_FILTERS.map(f =>
    `<button type="button" class="secondary ${f === reviewFilter ? "active" : ""}" data-f="${f}">${f}</button>`
  ).join("");
  el.querySelectorAll("button").forEach(b => b.onclick = () => {
    reviewFilter = b.dataset.f;
    renderReviewFilters();
    renderReviewQueue();
  });
}

function renderReviewQueue() {
  const list = filteredReview();
  const el = document.getElementById("reviewQueue");
  if (!list.length) {
    el.innerHTML = `<div class="muted">No cards in this filter.</div>`;
    return;
  }
  el.innerHTML = list.map(c => {
    const active = reviewCurrent && reviewCurrent.word === c.word;
    const checked = selectedWords.has(c.word) ? "checked" : "";
    const loading = loadingWords.has(c.word)
      ? `<div class="meta-sm warn">generating…</div>` : "";
    return `<label class="pick ${active ? "active" : ""}">
      <input type="checkbox" data-select="${c.word}" ${checked} onclick="event.stopPropagation()" />
      <span class="word-hit" data-word="${c.word}">
        <strong>${c.word}</strong> /${c.spelling || "?"}/ ${badge(c.status)}
        <div class="meta-sm">mean ${fmtNum(c.rating_mean)} · freq ${fmtNum(c.freq_pm)}</div>
        ${loading}
      </span>
    </label>`;
  }).join("");
  el.querySelectorAll("input[data-select]").forEach(inp => {
    inp.onchange = () => {
      if (inp.checked) selectedWords.add(inp.dataset.select);
      else selectedWords.delete(inp.dataset.select);
    };
  });
  el.querySelectorAll(".word-hit").forEach(hit => {
    hit.onclick = (e) => {
      e.preventDefault();
      selectReviewWord(hit.dataset.word);
    };
  });
  el.querySelectorAll("label.pick").forEach(lbl => {
    lbl.onclick = (e) => {
      if (e.target.matches("input[type=checkbox]")) return;
      const word = lbl.querySelector(".word-hit")?.dataset.word;
      if (word) selectReviewWord(word);
    };
  });
}

function cardImageHtml(c, isLoading, loadingMsg) {
  if (isLoading) return loadingMarkup(loadingMsg || `Generating “${c.word}”…`);
  if (c.image_url) {
    return `<div class="img-wrap"><img draggable="false" src="${c.image_url}?t=${Date.now()}" alt="${c.word}" /></div>`;
  }
  return `<div class="img-wrap"><div class="muted">No image yet</div></div>`;
}

function loadingMarkup(message) {
  return `<div class="img-wrap loading">
    <div class="spinner" aria-hidden="true"></div>
    <div><strong>${escapeHtml(message || "Generating image…")}</strong></div>
    <div class="meta-sm">Usually 10–30 seconds per image.</div>
  </div>`;
}

function bindSwipeHandlers() {
  if (swipeBound) return;
  swipeBound = true;
  document.addEventListener("keydown", (e) => {
    if (!reviewCurrent || reviewBusy) return;
    const tag = (e.target?.tagName || "").toLowerCase();
    if (tag === "textarea" || tag === "input" || tag === "select") return;
    if (e.key === "a" || e.key === "A") { e.preventDefault(); reviewAct("approve"); }
    if (e.key === "x" || e.key === "X") { e.preventDefault(); reviewAct("needs_rework"); }
    if (e.key === "s" || e.key === "S") { e.preventDefault(); reviewAct("skip"); }
    if (e.key === "ArrowRight") { e.preventDefault(); navReview(1); }
    if (e.key === "ArrowLeft") { e.preventDefault(); navReview(-1); }
  });

  document.addEventListener("pointerdown", (e) => {
    const stage = e.target.closest?.(".card-stage");
    if (!stage || reviewBusy || loadingWords.has(reviewCurrent?.word)) return;
    let startX = e.clientX;
    let dx = 0;
    const approveOv = stage.querySelector(".swipe-overlay.approve");
    const rejectOv = stage.querySelector(".swipe-overlay.reject");
    stage.classList.add("dragging");
    stage.setPointerCapture?.(e.pointerId);

    const move = (ev) => {
      dx = ev.clientX - startX;
      if (approveOv) approveOv.classList.toggle("show", dx > 40);
      if (rejectOv) rejectOv.classList.toggle("show", dx < -40);
      stage.style.transform = `translateX(${dx * 0.35}px) rotate(${dx * 0.02}deg)`;
    };
    const up = () => {
      stage.classList.remove("dragging");
      stage.style.transform = "";
      if (approveOv) approveOv.classList.remove("show");
      if (rejectOv) rejectOv.classList.remove("show");
      stage.releasePointerCapture?.(e.pointerId);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      if (dx > 90) reviewAct("approve");
      else if (dx < -90) reviewAct("needs_rework");
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  });
}

function navReview(delta) {
  if (!reviewCurrent) return;
  const idx = reviewIndex(reviewCurrent.word);
  const word = pickReviewWordAt(idx + delta);
  if (word) selectReviewWord(word);
}

function renderReviewDetail(c, { loading = false, loadingMsg = null, savedNote = "" } = {}) {
  reviewCurrent = c;
  const isLoading = loading || loadingWords.has(c.word);
  const idx = reviewIndex(c.word);
  const total = reviewList().length;
  const pos = idx >= 0 ? `${idx + 1} / ${total}` : "";
  const disabled = isLoading || reviewBusy ? "disabled" : "";
  const statusHtml = isLoading
    ? `<p class="status warn" id="reviewActionStatus">${escapeHtml(loadingMsg || "Generating with OpenAI… (usually 10–30 seconds)")}</p>`
    : `<p class="status" id="reviewActionStatus"></p>`;

  document.getElementById("reviewDetail").innerHTML = `
    <div class="card-stage-wrap">
      <div class="card-nav">
        <button type="button" class="secondary" onclick="navReview(-1)" ${disabled}>← Prev</button>
        <span class="muted"><strong>${c.word}</strong> · ${pos}</span>
        <button type="button" class="secondary" onclick="navReview(1)" ${disabled}>Next →</button>
      </div>
      <div class="card-stage" id="cardStage">
        <div class="swipe-overlay approve">✓ Approve</div>
        <div class="swipe-overlay reject">✗ Reject</div>
        ${cardImageHtml(c, isLoading, loadingMsg)}
      </div>
      <div class="rating-bar">
        <button type="button" class="rate-reject" onclick="reviewAct('needs_rework')" ${disabled}>✗ Reject</button>
        <button type="button" class="rate-mid" onclick="reviewAct('regenerate')" ${disabled}>↻ Regen</button>
        <button type="button" class="rate-approve" onclick="reviewAct('approve')" ${disabled}>✓ Approve</button>
      </div>
      ${statusHtml}
      <p class="rating-hints">${isLoading ? "Please wait — image is generating…" : "Click a button right under the image — or swipe right/left on the card"}</p>
      <div class="card-meta-compact">
        <p class="muted" style="margin:0.35rem 0">
          /${c.spelling || "?"}/ as in <em>${c.as_in || "—"}</em>
          · ${badge(c.status)} · mean ${fmtNum(c.rating_mean)}
          · ${escapeHtml(c.meaning || "—")}
        </p>
        <details ${isLoading ? "open" : ""}>
          <summary>Prompt, rework note, history</summary>
          <div class="prompt" style="margin-top:0.4rem">${escapeHtml(c.prompt || "")}</div>
          <label for="reworkNote"><strong>Rework note</strong></label>
          <textarea id="reworkNote" placeholder="Describe what to fix, e.g. show a calendar for May, not a tin can." ${isLoading ? "disabled" : ""}>${escapeHtml(savedNote)}</textarea>
          <div class="actions">
            <button type="button" class="secondary" onclick="reviewAct('rework')" ${disabled}>Rework with note</button>
            <button type="button" class="secondary" onclick="reviewAct('skip')" ${disabled}>Skip</button>
          </div>
          <div class="history"><p><strong>Version history</strong></p>${renderHistory(c)}</div>
        </details>
      </div>
    </div>
  `;
  bindSwipeHandlers();
  renderReviewQueue();
}

async function selectReviewWord(word, { skipAutoGen = false } = {}) {
  const c = reviewCards.find(x => x.word === word);
  if (!c) return;
  renderReviewDetail(c);
  const auto = !skipAutoGen && document.getElementById("autoGenMissing")?.checked;
  if (auto && !c.has_image && !loadingWords.has(word) && !autoGenStarted.has(word)) {
    autoGenStarted.add(word);
    startWordGeneration(word, "regenerate", "No image yet — generating automatically…");
  }
}

function renderHistory(c) {
  const hist = (c.history || []).slice().reverse();
  if (!hist.length) return `<div class="muted">No archives yet.</div>`;
  return hist.map(h => {
    const isCurrent = Number(h.version) === Number(c.version);
    const thumb = h.image_url
      ? `<img src="${h.image_url}?t=${Date.now()}" alt="v${h.version}" />`
      : `<div class="muted">—</div>`;
    return `<div class="history-item ${isCurrent ? "current" : ""}">
      ${thumb}
      <div>
        <div><strong>v${h.version}</strong> ${isCurrent ? "(preferred)" : ""}</div>
        <div class="meta-sm">${escapeHtml(h.note || "no rework note")} · ${h.at || ""}</div>
      </div>
      <button type="button" class="secondary" ${reviewBusy || isCurrent || loadingWords.has(c.word) ? "disabled" : ""}
        onclick="restoreReviewVersion(${h.version})">Restore</button>
    </div>`;
  }).join("");
}

function updateJobProgress(data, barId, etaId, wrapId) {
  const wrap = document.getElementById(wrapId);
  const bar = document.getElementById(barId);
  const eta = document.getElementById(etaId);
  if (!wrap || !bar) return;
  wrap.classList.remove("hidden");
  const total = data.total || 0;
  const done = data.done || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  bar.style.width = pct + "%";
  const remaining = Math.max(total - done, 0);
  const conc = data.concurrency || 1;
  const est = remaining ? Math.ceil((remaining * 20) / conc) : 0;
  if (eta) {
    const active = (data.active || []).join(", ") || data.current || "—";
    eta.textContent = data.status === "running"
      ? `${done}/${total} done · in flight: ${active} · ~${est}s left (est.)`
      : (data.message || data.status || "");
  }
}

async function startWordGeneration(word, action, loadingMsg, note = "") {
  reviewBusy = true;
  loadingWords.add(word);
  if (reviewCurrent && reviewCurrent.word === word) {
    renderReviewDetail(reviewCurrent, { loading: true, loadingMsg, savedNote: note });
  } else {
    renderReviewQueue();
  }
  try {
    const data = await api(`/api/cards/${encodeURIComponent(word)}/${action}`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ note }),
    });
    loadingWords.delete(word);
    await refreshReview();
    if (data.card) {
      selectReviewWord(data.card.word, { skipAutoGen: true });
      const st2 = document.getElementById("reviewActionStatus");
      if (st2) {
        st2.textContent = data.message || `New version ready (v${data.card.version || "?"}).`;
        st2.className = "status ok";
      }
    }
  } catch (e) {
    loadingWords.delete(word);
    await refreshReview();
    if (reviewCurrent && reviewCurrent.word === word) {
      renderReviewDetail(reviewCurrent, { savedNote: note });
    }
    const st2 = document.getElementById("reviewActionStatus");
    if (st2) { st2.textContent = e.message; st2.className = "status err"; }
  } finally {
    reviewBusy = false;
  }
}

async function startBatchJob({ words = null, mode = "generate", statusEl = "genStatus", progressWrap = "genProgressWrap", progressBar = "genProgressBar", etaEl = "genEta", disableBtn = null } = {}) {
  const body = {
    sort: document.getElementById("sortSelect").value || null,
    limit: words ? null : (Number(document.getElementById("limit").value || 0) || null),
    dry_run: document.getElementById("dryRun").checked,
    concurrency: Number(document.getElementById("concurrency").value || 3),
    words,
    mode,
  };
  const data = await api("/api/generate", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body),
  });
  if (words) words.forEach(w => loadingWords.add(w));
  renderReviewQueue();
  if (reviewCurrent && loadingWords.has(reviewCurrent.word)) {
    renderReviewDetail(reviewCurrent, { loading: true, loadingMsg: `Generating “${reviewCurrent.word}”…` });
  }
  if (pollTimer) clearInterval(pollTimer);
  const poll = async () => {
    const job = await api(`/api/generate/jobs/${data.job_id}`);
    const status = document.getElementById(statusEl);
    if (status) {
      status.textContent = `${job.status} · ${job.done || 0}/${job.total || 0}` +
        (job.current ? ` · ${job.current}` : "");
      status.className = "status " + (job.status === "error" ? "err" : job.status === "done" ? "ok" : "");
    }
    const log = document.getElementById("jobLog");
    if (log && statusEl === "genStatus") {
      log.classList.remove("hidden");
      log.textContent = (job.log || []).join("\n");
    }
    updateJobProgress(job, progressBar, etaEl, progressWrap);
    if (job.status === "done" || job.status === "error") {
      clearInterval(pollTimer);
      pollTimer = null;
      if (disableBtn) disableBtn.disabled = false;
      (job.results || []).forEach(r => loadingWords.delete(r.word));
      if (words) words.forEach(w => loadingWords.delete(w));
      if (job.message && status) status.textContent = job.message;
      if (job.error && status) status.textContent = job.error;
      if (statusEl === "genStatus") {
        document.getElementById("btnPreview")?.click();
      }
      await refreshReview();
      document.getElementById("reviewPanel")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };
  pollTimer = setInterval(() => poll().catch(console.error), 1200);
  await poll();
  return data.job_id;
}

async function refreshReview() {
  const data = await api("/api/cards");
  reviewCards = data.cards || [];
  document.getElementById("reviewStats").textContent =
    `${data.counts.total} cards · ${data.counts.accepted} accepted · ${data.counts.needs_attention} need attention`;
  renderReviewFilters();
  renderReviewQueue();
  if (reviewCurrent) {
    const updated = reviewCards.find(c => c.word === reviewCurrent.word);
    if (updated) {
      renderReviewDetail(updated, {
        loading: loadingWords.has(updated.word),
        loadingMsg: loadingWords.has(updated.word) ? `Generating “${updated.word}”…` : null,
      });
    }
  } else if (filteredReview().length) {
    selectReviewWord(filteredReview()[0].word);
  }
}

async function restoreReviewVersion(version) {
  if (!reviewCurrent || reviewBusy) return;
  reviewBusy = true;
  const st = document.getElementById("reviewActionStatus");
  if (st) st.textContent = `Restoring v${version}…`;
  try {
    const data = await api(
      `/api/cards/${encodeURIComponent(reviewCurrent.word)}/restore/${version}`,
      { method: "POST", headers: {"Content-Type":"application/json"}, body: "{}" }
    );
    await refreshReview();
    if (data.card) selectReviewWord(data.card.word);
    const st2 = document.getElementById("reviewActionStatus");
    if (st2) st2.textContent = data.message || "Restored.";
  } catch (e) {
    const st2 = document.getElementById("reviewActionStatus");
    if (st2) { st2.textContent = e.message; st2.className = "status err"; }
  } finally {
    reviewBusy = false;
    if (reviewCurrent) {
      const updated = reviewCards.find(c => c.word === reviewCurrent.word) || reviewCurrent;
      renderReviewDetail(updated);
    }
  }
}

async function reviewAct(action) {
  if (!reviewCurrent || reviewBusy) return;
  const prevWord = reviewCurrent.word;
  const prevIdx = reviewIndex(prevWord);
  const note = (document.getElementById("reworkNote")?.value || "").trim();
  if (action === "rework" && !note) {
    const st = document.getElementById("reviewActionStatus");
    if (st) st.textContent = "Add a rework note first (expand details below).";
    return;
  }
  if (action === "regenerate" || action === "rework") {
    await startWordGeneration(
      prevWord,
      action,
      action === "rework"
        ? `Reworking “${prevWord}” with OpenAI… (10–30 seconds)`
        : `Regenerating “${prevWord}” with OpenAI… (10–30 seconds)`,
      note
    );
    return;
  }
  reviewBusy = true;
  const st = document.getElementById("reviewActionStatus");
  if (st) st.textContent = "Saving…";
  try {
    const data = await api(`/api/cards/${encodeURIComponent(prevWord)}/${action}`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ note }),
    });
    await refreshReview();
    const list = reviewList();
    const nextWord = list[prevIdx]?.word || list[prevIdx - 1]?.word || list[0]?.word;
    if (document.getElementById("autoAdvance")?.checked && nextWord && nextWord !== prevWord) {
      selectReviewWord(nextWord, { skipAutoGen: true });
    } else if (data.card) {
      selectReviewWord(data.card.word, { skipAutoGen: true });
    }
    const st2 = document.getElementById("reviewActionStatus");
    if (st2) { st2.textContent = data.message || "Done."; st2.className = "status ok"; }
  } catch (e) {
    const st2 = document.getElementById("reviewActionStatus");
    if (st2) { st2.textContent = e.message; st2.className = "status err"; }
  } finally {
    reviewBusy = false;
  }
}

async function bulkReviewAct(action) {
  let words = [...selectedWords];
  if (!words.length) {
    words = reviewList().map(c => c.word);
  }
  if (!words.length) return;
  const label = action === "approve" ? "approve" : action === "needs_rework" ? "reject" : action;
  if (!confirm(`${label} ${words.length} card(s)?`)) return;
  reviewBusy = true;
  const status = document.getElementById("batchStatus");
  if (status) {
    status.textContent = `Bulk ${label}…`;
    status.className = "status";
  }
  try {
    const data = await api("/api/cards/bulk", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ words, action }),
    });
    selectedWords.clear();
    await refreshReview();
    const list = reviewList();
    if (list.length) selectReviewWord(list[0].word, { skipAutoGen: true });
    if (status) {
      status.textContent = data.message || `Bulk ${label} done (${data.ok}/${data.total}).`;
      status.className = "status ok";
    }
  } catch (e) {
    if (status) { status.textContent = e.message; status.className = "status err"; }
  } finally {
    reviewBusy = false;
  }
}

document.getElementById("btnSelectAllVisible").onclick = () => {
  reviewList().forEach(c => selectedWords.add(c.word));
  renderReviewQueue();
};
document.getElementById("btnClearSelection").onclick = () => {
  selectedWords.clear();
  renderReviewQueue();
};
document.getElementById("btnApproveSelected").onclick = () => bulkReviewAct("approve");
document.getElementById("btnRejectSelected").onclick = () => bulkReviewAct("needs_rework");

document.getElementById("btnRefreshReview").onclick = () => refreshReview().catch(e => {
  document.getElementById("reviewStats").textContent = e.message;
});

document.getElementById("btnRegenSelected").onclick = async () => {
  const words = [...selectedWords];
  const status = document.getElementById("batchStatus");
  if (!words.length) {
    status.textContent = "Select one or more cards in the review queue first.";
    status.className = "status warn";
    return;
  }
  status.textContent = `Starting parallel regenerate for ${words.length} card(s)…`;
  status.className = "status";
  const btn = document.getElementById("btnRegenSelected");
  btn.disabled = true;
  try {
    await startBatchJob({
      words,
      mode: "regenerate",
      statusEl: "batchStatus",
      progressWrap: "batchProgressWrap",
      progressBar: "batchProgressBar",
      etaEl: "batchEta",
      disableBtn: btn,
    });
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
    btn.disabled = false;
    words.forEach(w => loadingWords.delete(w));
  }
};

function setActiveBankUi(bank) {
  activeBank = bank;
  const name = bank?.name || "—";
  const label = document.getElementById("activeBankLabel");
  if (label) label.textContent = `Using newest bank: ${name}`;
  const chip = document.getElementById("activeBankChip");
  if (chip) chip.textContent = name;
  fillSorts(bank);
}

async function previewQueue() {
  const status = document.getElementById("genStatus");
  try {
    const sort = document.getElementById("sortSelect").value || null;
    const limit = Number(document.getElementById("limit").value || 0) || null;
    const data = await api(`/api/generate/preview?sort=${encodeURIComponent(sort || "")}&limit=${limit || ""}`);
    renderPending(data);
    if (status) {
      status.textContent = `Queue ready: ${data.will_generate} will generate.`;
      status.className = "status ok";
    }
  } catch (e) {
    if (status) {
      status.textContent = e.message;
      status.className = "status err";
    }
  }
}

function renderBanks(banks) {
  const el = document.getElementById("bankList");
  if (!banks.length) { el.textContent = "No banks yet."; return; }
  el.innerHTML = `<table><thead><tr><th>File</th><th>Mode</th><th>Sorts</th><th></th></tr></thead><tbody>` +
    banks.map((b, i) => `<tr>
      <td><code>${b.name}</code>${i === 0 ? ' <span class="active-chip">newest</span>' : ""}</td>
      <td>${b.mode || "—"} ${b.focus_lesson != null ? "(L"+b.focus_lesson+")" : ""}</td>
      <td>${b.sort_count ?? "—"}</td>
      <td><button type="button" class="secondary" data-path="${b.path}">Use</button></td>
    </tr>`).join("") + `</tbody></table>`;
  el.querySelectorAll("button[data-path]").forEach(btn => {
    btn.onclick = async () => {
      preferNewestBank = false;
      await selectBank(btn.dataset.path);
      await previewQueue();
    };
  });
}

function fillSorts(summary) {
  const sel = document.getElementById("sortSelect");
  const exportSort = document.getElementById("exportSort");
  const sorts = summary?.sorts || [];
  const prev = sel.value;
  sel.innerHTML = `<option value="">Pick a sort…</option>` +
    sorts.map(s => `<option value="${s.id}">${s.id} (${s.word_count} words) [${(s.headers||[]).join(", ")}]</option>`).join("");
  if (prev && sorts.some(s => s.id === prev)) sel.value = prev;
  else if (sorts[0]) sel.value = sorts[0].id;
  if (sorts[0] && exportSort) exportSort.value = sel.value || sorts[0].id;
  loadWordPick().catch(() => {});
}

function fmtMean(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  return Number.isNaN(x) ? "—" : x.toFixed(1);
}

function renderWordPick(data) {
  const el = document.getElementById("wordPickTable");
  const status = document.getElementById("wordStatus");
  wordCandidates = data.items || [];
  if (!wordCandidates.length) {
    el.textContent = "No words in this sort.";
    return;
  }
  const selectedN = wordCandidates.filter(w => w.selected).length;
  el.innerHTML = `<p class="muted">${selectedN} selected · ${wordCandidates.length} candidates`
    + (data.has_selection ? " · saved selection" : " · default: all shown as selected until you save")
    + `</p>
    <table><thead><tr>
      <th></th><th>Word</th><th>Sound</th><th>Mean</th><th>Freq</th><th>POS</th><th>Meaning</th><th>Image</th>
    </tr></thead><tbody>` +
    wordCandidates.map(w => `<tr class="${w.selected ? "" : "unselected"}" data-word="${w.word}">
      <td><input type="checkbox" class="word-check" data-word="${w.word}" ${w.selected ? "checked" : ""} /></td>
      <td><strong>${w.word}</strong></td>
      <td>/${w.spelling || "?"}/</td>
      <td>${fmtMean(w.rating_mean)}</td>
      <td>${fmtMean(w.freq_pm)}</td>
      <td>${w.pos || "—"}</td>
      <td class="meaning">${w.meaning || "—"}</td>
      <td>${w.has_image ? (w.image_status || "yes") : "—"}</td>
    </tr>`).join("") + `</tbody></table>`;
  el.querySelectorAll(".word-check").forEach(cb => {
    cb.onchange = () => {
      const row = cb.closest("tr");
      if (row) row.classList.toggle("unselected", !cb.checked);
      const item = wordCandidates.find(x => x.word === cb.dataset.word);
      if (item) item.selected = cb.checked;
    };
  });
  if (status) {
    status.textContent = `${selectedN} of ${wordCandidates.length} words checked`;
    status.className = "status ok";
  }
}

async function loadWordPick() {
  const sort = document.getElementById("sortSelect").value;
  const el = document.getElementById("wordPickTable");
  const status = document.getElementById("wordStatus");
  if (!sort) {
    el.textContent = "Pick a sort to load candidates.";
    return;
  }
  el.textContent = "Loading words…";
  try {
    const data = await api(`/api/words?sort=${encodeURIComponent(sort)}`);
    renderWordPick(data);
    const exportSort = document.getElementById("exportSort");
    if (exportSort) exportSort.value = sort;
  } catch (e) {
    el.textContent = e.message;
    if (status) {
      status.textContent = e.message;
      status.className = "status err";
    }
  }
}

function checkedWords() {
  return [...document.querySelectorAll(".word-check:checked")].map(cb => cb.dataset.word);
}

function setWordChecks(pred) {
  document.querySelectorAll(".word-check").forEach(cb => {
    const item = wordCandidates.find(x => x.word === cb.dataset.word);
    const on = pred(item, cb.dataset.word);
    cb.checked = on;
    if (item) item.selected = on;
    cb.closest("tr")?.classList.toggle("unselected", !on);
  });
}

document.getElementById("btnSelectAllWords").onclick = () => setWordChecks(() => true);
document.getElementById("btnClearWords").onclick = () => setWordChecks(() => false);
document.getElementById("btnSelectNouns").onclick = () => setWordChecks(item => !!(item && item.noun));
document.getElementById("btnSelectTop8").onclick = () => {
  const ranked = [...wordCandidates].sort((a, b) => (b.rating_mean || 0) - (a.rating_mean || 0));
  const top = new Set(ranked.slice(0, 8).map(w => w.word));
  setWordChecks((_item, word) => top.has(word));
};

document.getElementById("btnSaveWords").onclick = async () => {
  const status = document.getElementById("wordStatus");
  const sort = document.getElementById("sortSelect").value;
  if (!sort) {
    status.textContent = "Pick a sort first.";
    status.className = "status err";
    return;
  }
  const words = checkedWords();
  status.textContent = "Saving selection…";
  status.className = "status";
  try {
    const data = await api("/api/words/selection", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ sort, words }),
    });
    status.textContent = data.message;
    status.className = "status ok";
    await loadWordPick();
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
  }
};

let scopeZoom = 1;
const SCOPE_ZOOM_MIN = 0.6;
const SCOPE_ZOOM_MAX = 2.5;
const SCOPE_ZOOM_STEP = 0.15;

function applyScopeZoom() {
  const pct = Math.round(scopeZoom * 100) + "%";
  ["scopeZoomLabel", "scopeZoomLabelModal"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = pct;
  });
  [
    ["scopeFrameInner", "scopeFrameWrap"],
    ["scopeFrameInnerModal", null],
  ].forEach(([innerId, wrapId]) => {
    const inner = document.getElementById(innerId);
    if (!inner) return;
    inner.style.transform = `scale(${scopeZoom})`;
    inner.style.width = (100 / scopeZoom) + "%";
    const wrap = wrapId ? document.getElementById(wrapId) : inner.parentElement;
    if (wrap) {
      const base = wrap.classList.contains("scope-frame-wrap")
        ? Math.max(380, wrap.clientHeight || 380)
        : 380;
      inner.style.minHeight = base + "px";
      const frame = inner.querySelector("iframe");
      if (frame) frame.style.minHeight = base + "px";
    }
  });
}

function bumpScopeZoom(delta) {
  scopeZoom = Math.min(SCOPE_ZOOM_MAX, Math.max(SCOPE_ZOOM_MIN, +(scopeZoom + delta).toFixed(2)));
  applyScopeZoom();
}

document.getElementById("btnScopeZoomIn").onclick = () => bumpScopeZoom(SCOPE_ZOOM_STEP);
document.getElementById("btnScopeZoomOut").onclick = () => bumpScopeZoom(-SCOPE_ZOOM_STEP);
document.getElementById("btnScopeZoomReset").onclick = () => { scopeZoom = 1; applyScopeZoom(); };
document.getElementById("btnScopeZoomInModal").onclick = () => bumpScopeZoom(SCOPE_ZOOM_STEP);
document.getElementById("btnScopeZoomOutModal").onclick = () => bumpScopeZoom(-SCOPE_ZOOM_STEP);
document.getElementById("btnScopeZoomResetModal").onclick = () => { scopeZoom = 1; applyScopeZoom(); };

document.getElementById("btnScopeWiden").onclick = () => {
  const layout = document.getElementById("wordPickLayout");
  const btn = document.getElementById("btnScopeWiden");
  const on = layout.classList.toggle("scope-wide");
  btn.textContent = on ? "Narrow" : "Widen";
};

document.getElementById("btnScopeExpand").onclick = () => {
  document.getElementById("scopeModal").classList.remove("hidden");
  applyScopeZoom();
};
document.getElementById("btnScopeCloseModal").onclick = () => {
  document.getElementById("scopeModal").classList.add("hidden");
};
document.getElementById("scopeModal").addEventListener("click", (e) => {
  if (e.target.id === "scopeModal") e.currentTarget.classList.add("hidden");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.getElementById("scopeModal")?.classList.add("hidden");
});
applyScopeZoom();


async function selectBank(path) {
  const data = await api("/api/bank/select", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ path }),
  });
  setActiveBankUi(data.bank);
  document.getElementById("bankStatus").textContent =
    `Using ${data.bank.name} · ${data.bank.sort_count} sort(s)`;
  document.getElementById("bankStatus").className = "status ok";
  refreshReview().catch(() => {});
}

async function refreshBanks() {
  const data = await api("/api/banks");
  const banks = data.banks || [];
  renderBanks(banks);
  // Prefer newest bank for the generate queue unless user picked an older one
  if (preferNewestBank && banks.length && banks[0].path) {
    await selectBank(banks[0].path);
  } else if (data.active) {
    setActiveBankUi(data.active);
  }
  renderPacks(data.packs || []);
  await loadWordPick();
  if (FEATURES.image_create) await previewQueue();
}

function renderPacks(packs) {
  const el = document.getElementById("packList");
  if (!packs.length) { el.textContent = "No packs exported yet."; return; }
  el.innerHTML = packs.map(p => {
    const ready = (p.ready_sorts || []).join(", ") || "—";
    return `<div style="margin:0.4rem 0">
      <strong>${p.id}</strong> · ${p.card_count ?? "?"} cards · ready: ${ready}
      ${p.has_script ? " · script ready" : ""}
      · <a href="/packs/${encodeURIComponent(p.id)}/index.html" target="_blank">pack</a>
      ${p.has_script ? ` · <a href="/packs/${encodeURIComponent(p.id)}/tutor_script.html" target="_blank">script</a>` : ""}
      · <button type="button" class="secondary" data-load-script="${p.id}" style="padding:0.2rem 0.5rem;font-size:0.75rem">Edit script</button>
    </div>`;
  }).join("");
  el.querySelectorAll("button[data-load-script]").forEach(btn => {
    btn.onclick = () => loadScriptIntoEditor(btn.dataset.loadScript);
  });
}

async function loadScriptIntoEditor(packId) {
  const status = document.getElementById("exportStatus");
  try {
    const data = await api(`/api/tutor-script?pack_id=${encodeURIComponent(packId)}`);
    document.getElementById("scriptEditor").value = data.markdown || "";
    document.getElementById("scriptPackId").value = data.pack_id || packId;
    const link = document.getElementById("linkScript");
    link.href = data.script_url;
    link.classList.remove("hidden");
    if (status) {
      status.textContent = `Loaded script for ${data.pack_id}`;
      status.className = "status ok";
    }
  } catch (e) {
    if (status) {
      status.textContent = e.message;
      status.className = "status err";
    }
  }
}

document.getElementById("btnRefreshBanks").onclick = () => {
  preferNewestBank = true;
  refreshBanks().catch(e => {
    document.getElementById("bankStatus").textContent = e.message;
    document.getElementById("bankStatus").className = "status err";
  });
};

document.getElementById("btnBuild").onclick = async () => {
  const status = document.getElementById("bankStatus");
  status.textContent = "Building bank…";
  status.className = "status";
  const body = {
    mode,
    lesson: Number(document.getElementById("lesson").value || 0) || null,
    previous: Number(document.getElementById("previous").value || 0),
    contrast: document.getElementById("contrast").value.trim() || null,
    lessons: document.getElementById("lessons").value.trim() || null,
    spellings_list: document.getElementById("spellingsList").value.trim() || null,
    per_column: Number(document.getElementById("perColumn").value || 8),
  };
  try {
    const data = await api("/api/bank/build", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(body),
    });
    preferNewestBank = true;
    status.textContent = `Wrote ${data.bank.name} · ${data.bank.sort_count} sort(s) — now active for generation`;
    status.className = "status ok";
    setActiveBankUi(data.bank);
    await refreshBanks();
    await loadWordPick();
    await refreshReview();
    document.getElementById("queuePanel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
  }
};

function renderPending(preview) {
  const el = document.getElementById("pendingTable");
  const rows = preview.pending || [];
  if (!rows.length) {
    el.innerHTML = `<p class="muted">No new words to generate (${preview.already_have || 0} already have cards).</p>`;
    return;
  }
  el.innerHTML = `<p class="muted">${preview.will_generate} of ${preview.pending_count} pending
    · ${preview.already_have} already have images</p>
    <table><thead><tr><th>Word</th><th>Sound</th><th>Mean</th><th>Freq</th></tr></thead><tbody>` +
    rows.map(r => `<tr>
      <td><strong>${r.word}</strong></td>
      <td>/${r.spelling || "?"}/</td>
      <td>${fmtNum(r.rating_mean ?? r.mean)}</td>
      <td>${fmtNum(r.freq_pm)}</td>
    </tr>`).join("") + `</tbody></table>`;
}

document.getElementById("btnPreview").onclick = () => previewQueue();
document.getElementById("sortSelect").onchange = () => {
  loadWordPick().catch(() => {});
  if (FEATURES.image_create) previewQueue().catch(() => {});
};
document.getElementById("limit").onchange = () => {
  if (FEATURES.image_create) previewQueue().catch(() => {});
};

document.getElementById("btnMjExport").onclick = async () => {
  const status = document.getElementById("genStatus") || document.getElementById("mjStatus");
  status.textContent = "Building Midjourney prompt sheet…";
  status.className = "status";
  try {
    const sort = document.getElementById("sortSelect").value || "";
    const limit = Number(document.getElementById("limit").value || 0) || "";
    const selected = checkedWords();
    const selParam = selected.length ? `&words=${encodeURIComponent(selected.join(","))}` : "";
    const url = `/api/midjourney/export?sort=${encodeURIComponent(sort)}&limit=${limit}&download=1${selParam}`;
    const res = await fetch(url);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || res.statusText);
    }
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = res.headers.get("X-Filename") || "midjourney_prompts.txt";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    status.textContent = "Downloaded Midjourney prompt sheet. Generate in MJ, save as word.png, then Import.";
    status.className = "status ok";
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
  }
};

document.getElementById("mjImportFiles").onchange = async (ev) => {
  const status = document.getElementById("mjStatus");
  const files = ev.target.files;
  if (!files || !files.length) return;
  status.textContent = `Importing ${files.length} image(s)…`;
  status.className = "status";
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  const sort = document.getElementById("sortSelect").value || "";
  if (sort) fd.append("sort", sort);
  try {
    const res = await fetch("/api/midjourney/import", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    const unmatched = (data.unmatched || []).join(", ");
    status.textContent = data.message + (unmatched ? ` · unmatched: ${unmatched}` : "");
    status.className = unmatched ? "status err" : "status ok";
    await previewQueue();
    await refreshReview();
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
  } finally {
    ev.target.value = "";
  }
};

document.getElementById("btnGenerate").onclick = async () => {
  const status = document.getElementById("genStatus");
  const btn = document.getElementById("btnGenerate");
  btn.disabled = true;
  status.textContent = "Starting parallel generation…";
  status.className = "status";
  try {
    await startBatchJob({
      mode: "generate",
      statusEl: "genStatus",
      progressWrap: "genProgressWrap",
      progressBar: "genProgressBar",
      etaEl: "genEta",
      disableBtn: btn,
    });
    await previewQueue();
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
    btn.disabled = false;
  }
};

document.getElementById("btnExportScript").onclick = async () => {
  const status = document.getElementById("exportStatus");
  status.textContent = "Exporting pack & building tutor script…";
  status.className = "status";
  const sort = document.getElementById("exportSort").value.trim() || null;
  try {
    const exported = await api("/api/export", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ sort }),
    });
    const linkPack = document.getElementById("linkPack");
    linkPack.href = exported.preview_url;
    linkPack.classList.remove("hidden");

    const scripted = await api("/api/tutor-script", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ pack_id: exported.pack_id, sort }),
    });
    document.getElementById("scriptEditor").value = scripted.markdown || "";
    document.getElementById("scriptPackId").value = scripted.pack_id || exported.pack_id;
    const linkScript = document.getElementById("linkScript");
    linkScript.href = scripted.script_url;
    linkScript.classList.remove("hidden");

    status.textContent = `${exported.message} · ${scripted.message}`;
    status.className = "status ok";
    await refreshBanks();
    document.getElementById("scriptEditor")?.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
  }
};

document.getElementById("btnSaveScript").onclick = async () => {
  const status = document.getElementById("exportStatus");
  const packId = document.getElementById("scriptPackId").value.trim();
  const markdown = document.getElementById("scriptEditor").value;
  if (!packId) {
    status.textContent = "Export a pack first (or load a script from Recent packs).";
    status.className = "status err";
    return;
  }
  status.textContent = "Saving script edits…";
  status.className = "status";
  try {
    const data = await api("/api/tutor-script", {
      method: "PUT",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        pack_id: packId,
        markdown,
        sort: document.getElementById("exportSort").value.trim() || null,
      }),
    });
    status.textContent = data.message;
    status.className = "status ok";
    const link = document.getElementById("linkScript");
    link.href = data.script_url;
    link.classList.remove("hidden");
    await refreshBanks();
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
  }
};

refreshBanks().catch(e => {
  document.getElementById("bankStatus").textContent = e.message;
  document.getElementById("bankStatus").className = "status err";
});
refreshReview().catch(e => {
  document.getElementById("reviewStats").textContent = e.message;
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes — builder
# ---------------------------------------------------------------------------

@app.get("/")
def builder_index():
    features = {"image_create": feature_image_create()}
    html = BUILDER_HTML.replace("__FEATURES_JSON__", json.dumps(features))
    return render_template_string(html)


@app.get("/health")
def health():
    git_commit = (
        (os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT") or "").strip()
        or None
    )
    if git_commit and len(git_commit) > 7:
        git_commit = git_commit[:7]
    scope_pdf = (HERE / "data" / "tyctr_scope_sequence.pdf").exists()
    return jsonify({
        "ok": True,
        "app": "word-sort-builder",
        "bank": STATE["bank_path"].name if STATE["bank_path"].exists() else None,
        "has_openai_key": bool(os.environ.get("OPENAI_API_KEY")),
        "image_create": feature_image_create(),
        "git_commit": git_commit,
        "scope_pdf": scope_pdf,
        "build": "scope-pdf-v1",
    })


@app.get("/api/banks")
def api_banks():
    banks = list_bank_files()
    active = None
    if STATE["bank_path"].exists():
        try:
            active = bank_summary(load_bank(), STATE["bank_path"])
        except Exception:
            active = None
    return jsonify({"banks": banks, "active": active, "packs": list_packs()})


@app.post("/api/bank/select")
def api_bank_select():
    body = request.get_json(silent=True) or {}
    path = Path(body.get("path") or "")
    if not path.is_absolute():
        path = HERE / path
    if not path.exists():
        return jsonify({"error": f"Bank not found: {path}"}), 404
    STATE["bank_path"] = path
    sync_review_state()
    bank = load_bank(path)
    return jsonify({"bank": bank_summary(bank, path)})


@app.post("/api/bank/build")
def api_bank_build():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode") or "lesson"
    per_column = int(body.get("per_column") or STATE["per_column"])
    spellings = Path(STATE["spellings_csv"])
    biemiller = Path(STATE["biemiller_csv"])
    if not spellings.exists():
        return jsonify({"error": f"Spellings CSV not found: {spellings}"}), 400
    if not biemiller.exists():
        return jsonify({"error": f"Biemiller CSV not found: {biemiller}"}), 400

    lesson = None
    lessons = None
    spelling_tokens = None
    contrast_tokens = None
    previous = int(body.get("previous") or 2)

    if mode == "lesson":
        lesson = body.get("lesson")
        if lesson is None:
            return jsonify({"error": "lesson required"}), 400
        lesson = int(lesson)
        if body.get("contrast"):
            contrast_tokens = bsb.parse_csv_list(body["contrast"])
    elif mode == "lessons":
        if not body.get("lessons"):
            return jsonify({"error": "lessons required"}), 400
        lessons = bsb.parse_lesson_list(body["lessons"])
    elif mode == "spellings":
        if not body.get("spellings_list"):
            return jsonify({"error": "spellings_list required"}), 400
        spelling_tokens = bsb.parse_csv_list(body["spellings_list"])
    else:
        return jsonify({"error": f"Unknown mode {mode}"}), 400

    try:
        bank = bsb.build_bank(
            spellings,
            biemiller,
            per_column,
            lesson=lesson,
            previous=previous,
            contrast_tokens=contrast_tokens,
            lessons=lessons,
            spelling_tokens=spelling_tokens,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # Match CLI default naming
    class _Args:
        pass

    args = _Args()
    args.lesson = lesson
    args.lessons = body.get("lessons")
    args.spellings_list = body.get("spellings_list")
    out = bsb.default_out_path(HERE, args)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bank, indent=2, ensure_ascii=False), encoding="utf-8")
    STATE["bank_path"] = out
    sync_review_state()
    return jsonify({"bank": bank_summary(bank, out), "path": str(out)})


@app.get("/api/config")
def api_config():
    return jsonify({"image_create": feature_image_create()})


@app.get("/api/words")
def api_words():
    sort_id = request.args.get("sort") or None
    data = list_sort_word_candidates(sort_id)
    if data.get("error"):
        return jsonify(data), 400
    return jsonify(data)


@app.post("/api/words/selection")
def api_words_selection():
    body = request.get_json(silent=True) or {}
    sort_id = (body.get("sort") or "").strip()
    words = body.get("words") or []
    if not sort_id:
        return jsonify({"error": "sort required"}), 400
    if not isinstance(words, list):
        return jsonify({"error": "words must be a list"}), 400
    cleaned = []
    seen = set()
    for w in words:
        ww = str(w).strip().lower()
        if not ww or ww in seen:
            continue
        seen.add(ww)
        cleaned.append(ww)
    data = load_selection()
    data.setdefault("sorts", {})[sort_id] = cleaned
    path = save_selection(data)
    return jsonify(
        {
            "sort": sort_id,
            "words": cleaned,
            "count": len(cleaned),
            "path": str(path),
            "message": f"Saved {len(cleaned)} word(s) for {sort_id}",
        }
    )


@app.get("/api/generate/preview")
def api_generate_preview():
    if not feature_image_create():
        return jsonify({"error": "Image creation disabled (FEATURE_IMAGE_CREATE off)"}), 403
    sort_id = request.args.get("sort") or None
    limit_raw = request.args.get("limit") or ""
    limit = int(limit_raw) if str(limit_raw).isdigit() else None
    preview = queue_preview(sort_id, limit)
    if preview.get("error"):
        return jsonify(preview), 400
    # Prefer saved selection when present
    selected = selected_words_for_sort(sort_id)
    if selected is not None:
        preview["pending"] = [p for p in (preview.get("pending") or []) if p["word"] in selected]
        preview["will_generate"] = len(preview["pending"][:limit] if limit else preview["pending"])
        if limit is not None:
            preview["pending"] = preview["pending"][:limit]
    return jsonify(preview)


@app.get("/api/midjourney/export")
def api_midjourney_export():
    """Export words as Midjourney paste prompts (no API call)."""
    if not feature_image_create():
        return jsonify({"error": "Image creation disabled (FEATURE_IMAGE_CREATE off)"}), 403
    sort_id = request.args.get("sort") or None
    limit_raw = request.args.get("limit") or ""
    limit = int(limit_raw) if str(limit_raw).isdigit() else None
    words_raw = request.args.get("words") or ""
    only = {w.strip().lower() for w in words_raw.split(",") if w.strip()} if words_raw else None
    if only is None:
        selected = selected_words_for_sort(sort_id)
        if selected is not None:
            only = set(selected)
    preview = queue_preview(sort_id, None)
    if preview.get("error"):
        return jsonify(preview), 400
    items = preview.get("pending") or []
    # Also include already-have if explicitly selected and user wants prompts for all selected
    if only is not None:
        bank = load_bank()
        biem = osc.load_biemiller_lookup(STATE["biemiller_path"])
        all_items = osc.unique_words_from_bank(bank, sort_id, only, biemiller_lookup=biem) if bank else []
        items = all_items
    if limit is not None:
        items = items[:limit]
    if not items:
        return jsonify({"error": "No words to export. Save a word selection first."}), 400
    chunk_raw = request.args.get("chunk") or ""
    chunk_size = int(chunk_raw) if str(chunk_raw).isdigit() else osc.MJ_PERM_CHUNK
    chunk_size = max(1, min(chunk_size, 40))
    bundle = osc.export_midjourney_bundle(items, chunk_size=chunk_size)
    if request.args.get("download") in ("1", "true", "yes"):
        bank_name = STATE["bank_path"].stem if STATE["bank_path"] else "bank"
        sort_part = osc.slug(sort_id) if sort_id else "all"
        filename = f"midjourney_prompts_{bank_name}_{sort_part}.txt"
        return Response(
            bundle["text"],
            mimetype="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Filename": filename,
            },
        )
    return jsonify(bundle)


@app.post("/api/midjourney/import")
def api_midjourney_import():
    """
    Import Midjourney PNGs into cards/ + manifest as pending_review.
    Filenames should be word.png (or word_final.png). Optional form field: sort.
    """
    files = request.files.getlist("files") or []
    if not files:
        one = request.files.get("file")
        if one:
            files = [one]
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    sort_id = request.form.get("sort") or request.args.get("sort") or None
    bank = load_bank()
    if not bank:
        return jsonify({"error": "No bank loaded"}), 400

    biem = osc.load_biemiller_lookup(STATE["biemiller_path"])
    # Match against full bank for this sort (not only pending) so re-imports work
    items = osc.unique_words_from_bank(bank, sort_id, None, biemiller_lookup=biem)
    by_word = {i["word"]: i for i in items}

    imported = []
    unmatched = []
    errors = []

    for f in files:
        name = f.filename or ""
        if not name:
            continue
        item = osc.match_item_for_filename(name, by_word)
        if not item:
            unmatched.append(name)
            continue
        word = item["word"]
        spelling = item.get("spelling")
        try:
            img_bytes = f.read()
            if not img_bytes:
                errors.append(f"{name}: empty file")
                continue
            # Normalize JPEG uploads to .png path naming via save_versioned_card (.png)
            prompt = osc.build_midjourney_prompt(
                word,
                spelling,
                item.get("as_in"),
                meaning=item.get("meaning"),
                pos=item.get("pos"),
            )
            with MANIFEST_LOCK:
                manifest = osc.load_manifest(STATE["manifest_path"])
                prev = (manifest.get("cards") or {}).get(word)
                saved = osc.save_versioned_card(
                    STATE["cards_dir"],
                    word,
                    spelling,
                    img_bytes,
                    prompt,
                    note="midjourney import",
                    entry=prev,
                )
                entry = {
                    "word": word,
                    "spelling": spelling,
                    "as_in": item.get("as_in"),
                    "image": saved["image"],
                    "version": saved["version"],
                    "prompt": prompt,
                    "status": "pending_review",
                    "source": "midjourney",
                    "auto_accepted": False,
                    "rating_mean": item.get("rating_mean") or item.get("mean"),
                    "freq_pm": item.get("freq_pm"),
                    "meaning": item.get("meaning"),
                    "history": osc.merge_history(prev, saved["history_entry"]),
                    "imported_at": _now(),
                    "imported_from": name,
                }
                if prev and prev.get("sort_ids"):
                    entry["sort_ids"] = prev["sort_ids"]
                elif item.get("sort_ids"):
                    entry["sort_ids"] = item["sort_ids"]
                manifest.setdefault("cards", {})[word] = entry
                osc.save_manifest(STATE["manifest_path"], manifest)
            imported.append({"word": word, "image": saved["image"], "from": name})
        except Exception as e:
            errors.append(f"{name}: {e}")

    sync_review_state()
    msg_parts = [f"Imported {len(imported)} card(s) for review"]
    if unmatched:
        msg_parts.append(f"{len(unmatched)} unmatched filename(s)")
    if errors:
        msg_parts.append(f"{len(errors)} error(s)")
    return jsonify(
        {
            "imported": imported,
            "unmatched": unmatched,
            "errors": errors,
            "message": " · ".join(msg_parts),
        }
    )


@app.post("/api/generate")
def api_generate():
    if not feature_image_create():
        return jsonify({"error": "Image creation disabled (FEATURE_IMAGE_CREATE off)"}), 403
    body = request.get_json(silent=True) or {}
    sort_id = body.get("sort") or None
    limit = body.get("limit")
    if limit is not None:
        limit = int(limit)
    dry_run = bool(body.get("dry_run"))
    concurrency = int(body.get("concurrency") or 3)
    words = body.get("words")
    if isinstance(words, str):
        words = [w.strip() for w in words.split(",") if w.strip()]
    if not words:
        selected = selected_words_for_sort(sort_id)
        if selected is not None:
            words = selected
    mode = body.get("mode") or ("regenerate" if words else "generate")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": _now(),
            "sort": sort_id,
            "limit": limit,
            "dry_run": dry_run,
            "concurrency": concurrency,
            "mode": mode,
            "done": 0,
            "total": 0,
            "active": [],
            "log": [],
            "results": [],
        }
    t = threading.Thread(
        target=run_generate_job,
        args=(job_id, sort_id, limit, dry_run, concurrency, words, mode),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.get("/api/generate/jobs/<job_id>")
def api_generate_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


@app.post("/api/export")
def api_export():
    body = request.get_json(silent=True) or {}
    sort_id = body.get("sort") or None
    bank_path = STATE["bank_path"]
    if not bank_path.exists():
        return jsonify({"error": "No active bank"}), 400
    selected = selected_words_for_sort(sort_id) if sort_id else None
    try:
        pack_dir = eap.export_pack(
            bank_path=bank_path,
            manifest_path=STATE["manifest_path"],
            cards_dir=STATE["cards_dir"],
            out_root=WORKSPACE / "packs",
            statuses={"accepted"},
            sort_id=sort_id,
            pack_id=None,
            biemiller_path=STATE["biemiller_path"],
        )
        pack = json.loads((pack_dir / "approved.json").read_text(encoding="utf-8"))
        # Keep pack aligned to saved word selection when present
        if selected is not None:
            allow = set(selected)
            before = len(pack.get("cards") or [])
            pack["cards"] = [c for c in (pack.get("cards") or []) if c.get("word") in allow]
            pack["card_count"] = len(pack["cards"])
            pack["selected_words"] = selected
            for s in pack.get("sorts") or []:
                for col in (s.get("columns") or {}).values():
                    col["words"] = [w for w in (col.get("words") or []) if w in allow]
                    col["count"] = len(col["words"])
            (pack_dir / "approved.json").write_text(
                json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            # refresh HTML index if helper exists
            try:
                if hasattr(eap, "write_pack_index"):
                    eap.write_pack_index(pack_dir, pack)
            except Exception:
                pass
            trimmed = before - pack["card_count"]
        else:
            trimmed = 0
        ready = (pack.get("phase4_hint") or {}).get("ready_sorts") or []
        msg = (
            f"Exported {pack_dir.name} · {pack.get('card_count')} cards · "
            f"ready: {', '.join(ready) or '(none)'}"
        )
        if selected is not None:
            msg += f" · filtered to {len(selected)} selected word(s)"
            if trimmed:
                msg += f" (dropped {trimmed} non-selected)"
        return jsonify(
            {
                "pack_id": pack_dir.name,
                "path": str(pack_dir),
                "card_count": pack.get("card_count"),
                "ready_sorts": ready,
                "selected_words": selected,
                "preview_url": f"/packs/{pack_dir.name}/index.html",
                "message": msg,
            }
        )
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def resolve_pack_dir(pack_id: str | None, sort_id: str | None) -> Path:
    packs_root = WORKSPACE / "packs"
    if pack_id:
        pack_dir = packs_root / pack_id
        if not (pack_dir / "approved.json").exists():
            raise ValueError(f"Pack not found: {pack_id}")
        return pack_dir

    candidates = [
        d for d in packs_root.iterdir()
        if d.is_dir() and (d / "approved.json").exists()
    ] if packs_root.exists() else []
    if not candidates:
        raise ValueError("No packs found. Export first.")

    if sort_id:
        slug = osc.slug(sort_id)
        for d in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
            if slug in d.name:
                return d
    return max(candidates, key=lambda p: p.stat().st_mtime)


def enrich_columns_from_bank(pack: dict, columns: list[dict]) -> None:
    bank_name = pack.get("bank")
    if not bank_name:
        return
    bank_path = HERE / bank_name
    if not bank_path.exists():
        return
    bank = json.loads(bank_path.read_text(encoding="utf-8"))
    by_sym = {
        s.get("symbol"): s
        for s in (bank.get("selected_spellings") or bank.get("new_spellings") or [])
    }
    for col in columns:
        meta = by_sym.get(col["spelling"]) or {}
        if meta.get("pronounced"):
            col["pronounced"] = meta["pronounced"]


def write_tutor_script(
    pack_dir: Path,
    sort_id: str | None = None,
    markdown: str | None = None,
) -> dict:
    approved_path = pack_dir / "approved.json"
    if not approved_path.exists():
        raise ValueError(f"Missing {approved_path}")

    pack = json.loads(approved_path.read_text(encoding="utf-8"))
    cards_by_word = bts.card_lookup(pack)
    sort = bts.pick_sort(pack, sort_id)
    columns = bts.column_plan(sort, cards_by_word)
    enrich_columns_from_bank(pack, columns)

    md_path = pack_dir / "tutor_script.md"
    html_path = pack_dir / "tutor_script.html"

    if markdown is None:
        md = bts.build_markdown(pack, sort, columns, cards_by_word)
    else:
        md = markdown

    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(
        bts.markdown_to_simple_html(md, pack, sort, columns, cards_by_word),
        encoding="utf-8",
    )
    return {
        "pack_id": pack_dir.name,
        "markdown": md,
        "script_url": f"/packs/{pack_dir.name}/tutor_script.html",
        "sort_id": sort.get("id"),
        "message": f"Wrote tutor script for {sort.get('id')} in {pack_dir.name}",
    }


@app.get("/api/tutor-script")
def api_tutor_script_get():
    pack_id = request.args.get("pack_id") or None
    sort_id = request.args.get("sort") or None
    try:
        pack_dir = resolve_pack_dir(pack_id, sort_id)
        md_path = pack_dir / "tutor_script.md"
        if not md_path.exists():
            # Generate once so the editor can open older packs
            result = write_tutor_script(pack_dir, sort_id=sort_id)
            return jsonify(result)
        pack = json.loads((pack_dir / "approved.json").read_text(encoding="utf-8"))
        sort = bts.pick_sort(pack, sort_id)
        return jsonify(
            {
                "pack_id": pack_dir.name,
                "markdown": md_path.read_text(encoding="utf-8"),
                "script_url": f"/packs/{pack_dir.name}/tutor_script.html",
                "sort_id": sort.get("id"),
                "message": f"Loaded script for {pack_dir.name}",
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.put("/api/tutor-script")
def api_tutor_script_put():
    body = request.get_json(silent=True) or {}
    pack_id = body.get("pack_id")
    markdown = body.get("markdown")
    sort_id = body.get("sort") or None
    if not pack_id:
        return jsonify({"error": "pack_id required"}), 400
    if markdown is None:
        return jsonify({"error": "markdown required"}), 400
    try:
        pack_dir = resolve_pack_dir(pack_id, sort_id)
        result = write_tutor_script(pack_dir, sort_id=sort_id, markdown=markdown)
        result["message"] = f"Saved script edits for {result['pack_id']}"
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/tutor-script")
def api_tutor_script():
    body = request.get_json(silent=True) or {}
    pack_id = body.get("pack_id")
    sort_id = body.get("sort") or None
    try:
        pack_dir = resolve_pack_dir(pack_id, sort_id)
        return jsonify(write_tutor_script(pack_dir, sort_id=sort_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.get("/packs/<path:subpath>")
def serve_pack(subpath: str):
    return send_from_directory(WORKSPACE / "packs", subpath)


@app.get("/docs/tyctr-scope-sequence.pdf")
def serve_tyctr_scope_pdf():
    """TYCTR GPC scope & sequence PDF for word-choice reference beside the picker."""
    docs_dir = HERE / "data"
    name = "tyctr_scope_sequence.pdf"
    path = docs_dir / name
    if not path.exists():
        try:
            import build_scope_pdf as bsp

            bsp.main()
        except Exception as e:
            return jsonify({"error": f"Scope PDF missing and rebuild failed: {e}"}), 404
    return send_from_directory(docs_dir, name, mimetype="application/pdf")


# ---------------------------------------------------------------------------
# Review gallery (same process)
# ---------------------------------------------------------------------------

@app.get("/review")
@app.get("/review/")
def review_index():
    sync_review_state()
    html = rs.REVIEW_HTML.replace(
        "<body>",
        '<body>\n<p style="margin:0;padding:10px 16px;font-family:system-ui">'
        '<a href="/" style="color:#0f6b5c;font-weight:600">← Sort Builder</a></p>\n',
        1,
    )
    return render_template_string(html)


@app.get("/media/<path:subpath>")
def media(subpath: str):
    sync_review_state()
    return rs.media(subpath)


@app.get("/api/cards")
def api_cards():
    sync_review_state()
    return rs.api_cards()


@app.post("/api/cards/bulk")
def api_cards_bulk():
    sync_review_state()
    body = request.get_json(silent=True) or {}
    words = [str(w).lower() for w in (body.get("words") or []) if w]
    action = (body.get("action") or "").strip().lower()
    if action not in ("approve", "skip", "needs_rework"):
        return jsonify({"error": "action must be approve, skip, or needs_rework"}), 400
    if not words:
        return jsonify({"error": "No words provided"}), 400

    results = []
    ok = 0
    for word in words:
        try:
            if action == "approve":
                manifest = rs._load_manifest()
                entry = (manifest.get("cards") or {}).get(word)
                if not entry or not entry.get("image") or not Path(entry["image"]).exists():
                    results.append({"word": word, "status": "error", "error": "No image"})
                    continue
                rs._update_card(
                    word,
                    status="accepted",
                    image=entry["image"],
                    auto_accepted=False,
                )
            elif action == "skip":
                rs._update_card(word, status="skipped")
            else:
                rs._update_card(word, status="needs_rework")
            ok += 1
            results.append({"word": word, "status": "ok"})
        except Exception as e:
            results.append({"word": word, "status": "error", "error": str(e)})

    return jsonify({
        "ok": ok,
        "total": len(words),
        "results": results,
        "message": f"Bulk {action}: {ok}/{len(words)} succeeded.",
    })


@app.post("/api/cards/<word>/approve")
def api_approve(word: str):
    sync_review_state()
    return rs.api_approve(word)


@app.post("/api/cards/<word>/skip")
def api_skip(word: str):
    sync_review_state()
    return rs.api_skip(word)


@app.post("/api/cards/<word>/needs_rework")
def api_needs_rework(word: str):
    sync_review_state()
    return rs.api_needs_rework(word)


@app.post("/api/cards/<word>/regenerate")
def api_regenerate(word: str):
    sync_review_state()
    return rs.api_regenerate(word)


@app.post("/api/cards/<word>/rework")
def api_rework(word: str):
    sync_review_state()
    return rs.api_rework(word)


@app.post("/api/cards/<word>/restore/<int:version>")
def api_restore(word: str, version: int):
    sync_review_state()
    return rs.api_restore(word, version)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    osc.load_dotenv(HERE / ".env")

    ap = argparse.ArgumentParser(description=__doc__)
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    default_port = int(os.environ.get("PORT") or 5055)
    ap.add_argument("--host", default=os.environ.get("HOST", default_host))
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--bank", type=Path, default=HERE / "sort_word_bank_L1.json")
    ap.add_argument("--cards", type=Path, default=DEFAULT_CARDS)
    ap.add_argument("--manifest", type=Path, default=None)
    args = ap.parse_args()

    STATE["bank_path"] = args.bank
    STATE["cards_dir"] = args.cards
    STATE["manifest_path"] = args.manifest or (args.cards / "manifest.json")
    STATE["cards_dir"].mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "packs").mkdir(parents=True, exist_ok=True)

    rs.STATE["biemiller_lookup"] = osc.load_biemiller_lookup(STATE["biemiller_path"])
    sync_review_state()

    print(f"Sort Builder: http://{args.host}:{args.port}")
    print(f"Review gallery: http://{args.host}:{args.port}/review")
    print(f"Active bank: {STATE['bank_path']}")
    print(f"Workspace (cards/packs): {WORKSPACE}")
    print(f"Spellings CSV: {STATE['spellings_csv']}")
    print(f"Biemiller CSV: {STATE['biemiller_csv']}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
