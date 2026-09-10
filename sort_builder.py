#!/usr/bin/env python3
"""
sort_builder.py — Phase 5 of Word Sort Builder TYCTR
====================================================
Local Sort Builder UI: pick lesson/spellings → build bank → queue card
generation → review → export pack → tutor script.

  python sort_builder.py
  # open http://127.0.0.1:5055

Review gallery is served at /review (same process).
Uses OPENAI_API_KEY from .env for generation.
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

from flask import Flask, jsonify, render_template_string, request, send_from_directory

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
    max-width: 1100px; margin: 0 auto; padding: 0 1.5rem 3rem;
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
</style>
</head>
<body>
<header>
  <h1>Word Sort Builder</h1>
  <p>Build a TYCTR picture-sort bank, generate cards, review, export an approved pack, then make the tutor script — in one place.</p>
</header>
<nav class="steps">
  <span>1 Bank</span><span>2 Generate</span><span>3 Review</span><span>4 Export</span><span>5 Tutor script</span>
</nav>
<main>
  <section class="panel" id="bankPanel">
    <h2>1. Build word bank</h2>
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
      <button type="button" class="secondary" id="btnRefreshBanks">Refresh list</button>
    </div>
    <p class="status" id="bankStatus"></p>
    <h3>Saved banks</h3>
    <div id="bankList" class="muted">Loading…</div>
  </section>

  <section class="panel" id="queuePanel">
    <h2>2. Queue card generation</h2>
    <p class="muted" id="activeBankLabel">Active bank: —</p>
    <label>Sort</label>
    <select id="sortSelect"><option value="">All sorts in bank</option></select>
    <div class="row">
      <div>
        <label>Generate next N (highest Rating.Mean first)</label>
        <input id="limit" type="number" min="1" max="50" value="4" />
      </div>
      <div>
        <label>Parallel jobs (1–6)</label>
        <input id="concurrency" type="number" min="1" max="6" value="3" />
      </div>
      <div>
        <label>&nbsp;</label>
        <label style="font-weight:500"><input type="checkbox" id="dryRun" /> Dry run (no API)</label>
      </div>
    </div>
    <div class="row" style="margin-top:0.9rem">
      <button type="button" class="secondary" id="btnPreview">Preview queue</button>
      <button type="button" id="btnGenerate">Generate cards</button>
    </div>
    <p class="status" id="genStatus"></p>
    <div id="genProgressWrap" class="hidden">
      <div class="progress"><span id="genProgressBar"></span></div>
      <p class="muted" id="genEta" style="margin:0.35rem 0 0"></p>
    </div>
    <div id="jobLog" class="log hidden"></div>
    <h3>Will generate</h3>
    <div id="pendingTable" class="muted">Preview the queue to see words.</div>
  </section>

  <section class="panel span2" id="reviewPanel">
    <h2>3. Review cards</h2>
    <p class="muted" id="reviewStats">Loading review queue…</p>
    <div class="row" style="margin-bottom:0.5rem; align-items:center; flex-wrap:wrap">
      <button type="button" class="secondary" id="btnRefreshReview">Refresh</button>
      <button type="button" class="secondary" id="btnRegenSelected">Regen selected</button>
      <button type="button" class="secondary" id="btnApproveSelected">Approve selected</button>
      <button type="button" class="secondary" id="btnRejectSelected">Reject selected</button>
      <label style="font-weight:500; margin:0">
        <input type="checkbox" id="autoAdvance" checked />
        Auto-advance after rating
      </label>
      <label style="font-weight:500; margin:0">
        <input type="checkbox" id="autoGenMissing" checked />
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

  <section class="panel span2">
    <h2>4–5. Export pack · Tutor script</h2>
    <p class="links">
      <a href="#" id="linkPack" class="hidden">Open pack preview</a>
      <a href="#" id="linkScript" class="hidden">Open tutor script</a>
    </p>
    <div class="row">
      <div>
        <label>Export sort (blank = all ready)</label>
        <input id="exportSort" type="text" placeholder="e.g. L1_m_vs_s" />
      </div>
      <div>
        <label>&nbsp;</label>
        <button type="button" id="btnExport">Export approved pack</button>
      </div>
      <div>
        <label>&nbsp;</label>
        <button type="button" class="secondary" id="btnScript">Build tutor script</button>
      </div>
    </div>
    <p class="status" id="exportStatus"></p>
    <h3>Packs</h3>
    <div id="packList" class="muted">—</div>
  </section>
</main>
<script>
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
const REVIEW_FILTERS = ["needs_attention", "all", "accepted", "needs_rework", "pending_review", "failed", "skipped"];

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

function renderBanks(banks) {
  const el = document.getElementById("bankList");
  if (!banks.length) { el.textContent = "No banks yet."; return; }
  el.innerHTML = `<table><thead><tr><th>File</th><th>Mode</th><th>Sorts</th><th></th></tr></thead><tbody>` +
    banks.map(b => `<tr>
      <td><code>${b.name}</code></td>
      <td>${b.mode || "—"} ${b.focus_lesson != null ? "(L"+b.focus_lesson+")" : ""}</td>
      <td>${b.sort_count ?? "—"}</td>
      <td><button type="button" class="secondary" data-path="${b.path}">Use</button></td>
    </tr>`).join("") + `</tbody></table>`;
  el.querySelectorAll("button[data-path]").forEach(btn => {
    btn.onclick = () => selectBank(btn.dataset.path);
  });
}

function fillSorts(summary) {
  const sel = document.getElementById("sortSelect");
  const exportSort = document.getElementById("exportSort");
  const sorts = summary?.sorts || [];
  sel.innerHTML = `<option value="">All sorts in bank</option>` +
    sorts.map(s => `<option value="${s.id}">${s.id} (${s.word_count} words) [${(s.headers||[]).join(", ")}]</option>`).join("");
  if (sorts[0]) exportSort.value = sorts[0].id;
}

async function selectBank(path) {
  const data = await api("/api/bank/select", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ path }),
  });
  activeBank = data.bank;
  document.getElementById("activeBankLabel").textContent =
    `Active bank: ${activeBank.name}`;
  fillSorts(activeBank);
  document.getElementById("bankStatus").textContent =
    `Using ${activeBank.name} · ${activeBank.sort_count} sort(s)`;
  document.getElementById("bankStatus").className = "status ok";
  refreshReview().catch(() => {});
}

async function refreshBanks() {
  const data = await api("/api/banks");
  renderBanks(data.banks || []);
  if (data.active) {
    activeBank = data.active;
    document.getElementById("activeBankLabel").textContent =
      `Active bank: ${activeBank.name}`;
    fillSorts(activeBank);
  }
  renderPacks(data.packs || []);
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
    </div>`;
  }).join("");
}

document.getElementById("btnRefreshBanks").onclick = () => refreshBanks().catch(e => {
  document.getElementById("bankStatus").textContent = e.message;
  document.getElementById("bankStatus").className = "status err";
});

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
    status.textContent = `Wrote ${data.bank.name} · ${data.bank.sort_count} sort(s)`;
    status.className = "status ok";
    activeBank = data.bank;
    fillSorts(activeBank);
    document.getElementById("activeBankLabel").textContent =
      `Active bank: ${activeBank.name}`;
    await refreshBanks();
    await refreshReview();
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

document.getElementById("btnPreview").onclick = async () => {
  const status = document.getElementById("genStatus");
  try {
    const sort = document.getElementById("sortSelect").value || null;
    const limit = Number(document.getElementById("limit").value || 0) || null;
    const data = await api(`/api/generate/preview?sort=${encodeURIComponent(sort || "")}&limit=${limit || ""}`);
    renderPending(data);
    status.textContent = `Queue ready: ${data.will_generate} will generate.`;
    status.className = "status ok";
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
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
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
    btn.disabled = false;
  }
};

document.getElementById("btnExport").onclick = async () => {
  const status = document.getElementById("exportStatus");
  status.textContent = "Exporting accepted pack…";
  status.className = "status";
  try {
    const data = await api("/api/export", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ sort: document.getElementById("exportSort").value.trim() || null }),
    });
    status.textContent = data.message;
    status.className = "status ok";
    const link = document.getElementById("linkPack");
    link.href = data.preview_url;
    link.classList.remove("hidden");
    await refreshBanks();
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
  }
};

document.getElementById("btnScript").onclick = async () => {
  const status = document.getElementById("exportStatus");
  status.textContent = "Building tutor script…";
  status.className = "status";
  try {
    const data = await api("/api/tutor-script", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        pack_id: null,
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
    return render_template_string(BUILDER_HTML)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "app": "word-sort-builder",
        "bank": STATE["bank_path"].name if STATE["bank_path"].exists() else None,
        "has_openai_key": bool(os.environ.get("OPENAI_API_KEY")),
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


@app.get("/api/generate/preview")
def api_generate_preview():
    sort_id = request.args.get("sort") or None
    limit_raw = request.args.get("limit") or ""
    limit = int(limit_raw) if str(limit_raw).isdigit() else None
    preview = queue_preview(sort_id, limit)
    if preview.get("error"):
        return jsonify(preview), 400
    return jsonify(preview)


@app.post("/api/generate")
def api_generate():
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
        ready = (pack.get("phase4_hint") or {}).get("ready_sorts") or []
        return jsonify(
            {
                "pack_id": pack_dir.name,
                "path": str(pack_dir),
                "card_count": pack.get("card_count"),
                "ready_sorts": ready,
                "preview_url": f"/packs/{pack_dir.name}/index.html",
                "message": (
                    f"Exported {pack_dir.name} · {pack.get('card_count')} cards · "
                    f"ready: {', '.join(ready) or '(none)'}"
                ),
            }
        )
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/tutor-script")
def api_tutor_script():
    body = request.get_json(silent=True) or {}
    pack_id = body.get("pack_id")
    sort_id = body.get("sort") or None
    packs_root = WORKSPACE / "packs"
    if pack_id:
        pack_dir = packs_root / pack_id
    else:
        # Prefer newest pack that has approved.json
        candidates = [
            d for d in packs_root.iterdir()
            if d.is_dir() and (d / "approved.json").exists()
        ] if packs_root.exists() else []
        if not candidates:
            return jsonify({"error": "No packs found. Export first."}), 400
        # Prefer pack matching sort name
        pack_dir = None
        if sort_id:
            slug = osc.slug(sort_id)
            for d in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
                if slug in d.name:
                    pack_dir = d
                    break
        if pack_dir is None:
            pack_dir = max(candidates, key=lambda p: p.stat().st_mtime)

    approved_path = pack_dir / "approved.json"
    if not approved_path.exists():
        return jsonify({"error": f"Missing {approved_path}"}), 400

    pack = json.loads(approved_path.read_text(encoding="utf-8"))
    cards_by_word = bts.card_lookup(pack)
    sort = bts.pick_sort(pack, sort_id)
    columns = bts.column_plan(sort, cards_by_word)

    bank_name = pack.get("bank")
    if bank_name:
        bank_path = HERE / bank_name
        if bank_path.exists():
            bank = json.loads(bank_path.read_text(encoding="utf-8"))
            by_sym = {
                s.get("symbol"): s
                for s in (bank.get("selected_spellings") or bank.get("new_spellings") or [])
            }
            for col in columns:
                meta = by_sym.get(col["spelling"]) or {}
                if meta.get("pronounced"):
                    col["pronounced"] = meta["pronounced"]

    md = bts.build_markdown(pack, sort, columns, cards_by_word)
    md_path = pack_dir / "tutor_script.md"
    html_path = pack_dir / "tutor_script.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(
        bts.markdown_to_simple_html(md, pack, sort, columns, cards_by_word),
        encoding="utf-8",
    )
    return jsonify(
        {
            "pack_id": pack_dir.name,
            "script_url": f"/packs/{pack_dir.name}/tutor_script.html",
            "message": f"Wrote tutor script for {sort.get('id')} in {pack_dir.name}",
        }
    )


@app.get("/packs/<path:subpath>")
def serve_pack(subpath: str):
    return send_from_directory(WORKSPACE / "packs", subpath)


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
