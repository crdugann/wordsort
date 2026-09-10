#!/usr/bin/env python3
"""
review_server.py — Phase 2.5 Picture Card Review Gallery
========================================================
Local web UI for approving, regenerating, or reworking OpenAI sort cards.

  pip install flask openai requests
  python review_server.py
  # open http://127.0.0.1:5050

Uses OPENAI_API_KEY from .env (same as openai_sort_cards.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_from_directory

import openai_sort_cards as osc

HERE = Path(__file__).resolve().parent
DEFAULT_BANK = HERE / "sort_word_bank_L1.json"
DEFAULT_CARDS = HERE / "cards"
DEFAULT_MANIFEST = DEFAULT_CARDS / "manifest.json"

app = Flask(__name__)
STATE = {
    "bank_path": DEFAULT_BANK,
    "cards_dir": DEFAULT_CARDS,
    "manifest_path": DEFAULT_MANIFEST,
    "style": osc.DEFAULT_STYLE,
    "model": osc.DEFAULT_MODEL,
    "client": None,
    "biemiller_path": osc.DEFAULT_BIEMILLER,
    "biemiller_lookup": {},
}


REVIEW_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Word Sort Card Review</title>
<style>
  :root {
    --ink: #26241F;
    --muted: #6B6559;
    --paper: #FAF7F1;
    --card: #fff;
    --line: #E7E0D3;
    --accent: #3D6B5C;
    --accent-soft: #E4EEE9;
    --warn: #8A5A2F;
    --bad: #8B3A3A;
    --ok: #2F6B4F;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
    background: var(--paper);
    color: var(--ink);
  }
  header {
    display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline;
    justify-content: space-between;
    padding: 20px 28px;
    border-bottom: 3px solid var(--accent);
    background: var(--card);
  }
  .eyebrow {
    font-family: "Courier New", ui-monospace, monospace;
    letter-spacing: 0.1em; text-transform: uppercase;
    font-size: 12px; color: var(--accent); font-weight: 700;
  }
  h1 { margin: 4px 0 0; font-size: 28px; }
  main {
    max-width: 1100px; margin: 0 auto; padding: 24px;
    display: grid; grid-template-columns: 240px 1fr; gap: 24px;
  }
  @media (max-width: 860px) { main { grid-template-columns: 1fr; } }
  .panel {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 16px;
  }
  .queue { max-height: 70vh; overflow: auto; }
  .queue button {
    display: block; width: 100%; text-align: left;
    border: 1px solid transparent; background: transparent;
    padding: 8px 10px; margin: 0 0 4px; border-radius: 8px;
    cursor: pointer; font: inherit; color: inherit;
  }
  .queue button:hover, .queue button.active {
    background: var(--accent-soft); border-color: var(--accent);
  }
  .badge {
    display: inline-block; font-family: "Courier New", monospace;
    font-size: 11px; padding: 2px 6px; border-radius: 999px;
    border: 1px solid var(--line); color: var(--muted);
  }
  .badge.accepted { color: var(--ok); border-color: var(--ok); }
  .badge.needs_rework, .badge.pending_review { color: var(--warn); border-color: var(--warn); }
  .badge.failed, .badge.skipped { color: var(--bad); border-color: var(--bad); }
  .viewer {
    display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
  }
  @media (max-width: 860px) { .viewer { grid-template-columns: 1fr; } }
  .img-wrap {
    background: #f3efe6; border: 1px solid var(--line);
    border-radius: 10px; min-height: 320px;
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }
  .img-wrap img { max-width: 100%; max-height: 480px; display: block; }
  .meta h2 { margin: 0 0 8px; font-size: 26px; }
  .meta p { margin: 6px 0; color: var(--muted); }
  .prompt {
    font-family: "Courier New", monospace; font-size: 12px;
    background: #f7f3ea; border: 1px solid var(--line);
    border-radius: 8px; padding: 10px; white-space: pre-wrap;
    max-height: 160px; overflow: auto; color: var(--ink);
  }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .actions button, .filters button {
    font: inherit; border-radius: 8px; border: 1px solid var(--accent);
    background: var(--accent); color: #fff; padding: 8px 12px; cursor: pointer;
  }
  .actions button.secondary, .filters button {
    background: var(--card); color: var(--accent);
  }
  .actions button:disabled { opacity: 0.5; cursor: wait; }
  textarea {
    width: 100%; min-height: 72px; margin-top: 10px;
    font: inherit; padding: 8px; border-radius: 8px; border: 1px solid var(--line);
  }
  .filters { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .filters button.active { background: var(--accent); color: #fff; }
  .history {
    margin-top: 16px; border-top: 1px solid var(--line); padding-top: 12px;
  }
  .history-item {
    display: grid; grid-template-columns: 72px 1fr auto; gap: 10px;
    align-items: center; margin-bottom: 10px;
    padding: 8px; border: 1px solid var(--line); border-radius: 8px;
  }
  .history-item img {
    width: 72px; height: 72px; object-fit: cover; border-radius: 6px; background: #eee;
  }
  .history-item .meta-sm { font-size: 12px; color: var(--muted); }
  .history-item.current { border-color: var(--accent); background: var(--accent-soft); }
</style>
</head>
<body>
<header>
  <div>
    <div class="eyebrow">Word Sort Builder · Phase 2.5</div>
    <h1>Picture Card Review</h1>
  </div>
  <div class="status-line" id="stats">Loading…</div>
</header>
<main>
  <aside class="panel">
    <div class="filters" id="filters"></div>
    <div class="queue" id="queue"></div>
  </aside>
  <section class="panel" id="detail">
    <div class="empty">Select a card from the queue.</div>
  </section>
</main>
<script>
const FILTERS = ["needs_attention", "all", "accepted", "needs_rework", "pending_review", "failed", "skipped"];
let filter = "needs_attention";
let cards = [];
let current = null;
let busy = false;

function badge(status) {
  return `<span class="badge ${status || ""}">${status || "unknown"}</span>`;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function filtered() {
  if (filter === "all") return cards;
  if (filter === "needs_attention") {
    return cards.filter(c => ["needs_rework", "pending_review", "failed"].includes(c.status) || !c.has_image);
  }
  return cards.filter(c => c.status === filter);
}

function renderFilters() {
  const el = document.getElementById("filters");
  el.innerHTML = FILTERS.map(f =>
    `<button class="${f === filter ? "active" : ""}" data-f="${f}">${f}</button>`
  ).join("");
  el.querySelectorAll("button").forEach(b => b.onclick = () => {
    filter = b.dataset.f; renderFilters(); renderQueue();
  });
}

function renderQueue() {
  const list = filtered();
  const el = document.getElementById("queue");
  if (!list.length) {
    el.innerHTML = `<div class="empty">No cards in this filter.</div>`;
    return;
  }
  el.innerHTML = list.map(c => `
      <button class="${current && current.word === c.word ? "active" : ""}" data-word="${c.word}">
      <strong>${c.word}</strong> /${c.spelling || "?"}/ ${badge(c.status)}
      <div class="meta-sm">mean ${fmtNum(c.rating_mean)} · freq ${fmtNum(c.freq_pm)}</div>
    </button>
  `).join("");
  el.querySelectorAll("button").forEach(b => b.onclick = () => selectWord(b.dataset.word));
}

function renderDetail(c) {
  current = c;
  const img = c.image_url
    ? `<img src="${c.image_url}?t=${Date.now()}" alt="${c.word}" />`
    : `<div class="empty">No image yet</div>`;
  document.getElementById("detail").innerHTML = `
    <div class="viewer">
      <div class="img-wrap">${img}</div>
      <div class="meta">
        <h2>${c.word}</h2>
        <p><strong>Sound:</strong> /${c.spelling || "?"}/ as in <em>${c.as_in || "—"}</em></p>
        <p><strong>Status:</strong> ${badge(c.status)}
           · <strong>Preferred:</strong> v${c.version || "—"} → <code>*_final.png</code></p>
        <p><strong>Ratings:</strong>
           Rating.Mean <code>${fmtNum(c.rating_mean)}</code>
           · Freq_pm <code>${fmtNum(c.freq_pm)}</code>
        </p>
        <p><strong>Meaning:</strong> ${escapeHtml(c.meaning || "—")}
           ${c.pos ? `(${escapeHtml(c.pos)})` : ""}</p>
        <p><strong>Sorts:</strong> ${(c.sort_ids || []).join(", ") || "—"}</p>
        <p><strong>Prompt</strong></p>
        <div class="prompt">${escapeHtml(c.prompt || "")}</div>
        <label for="note"><strong>Rework note</strong> (appended to prompt)</label>
        <textarea id="note" placeholder="e.g. Show a calendar page for the month of May, not a tin can. No text or numbers."></textarea>
        <div class="actions">
          <button onclick="act('approve')" ${busy ? "disabled" : ""}>Approve</button>
          <button class="secondary" onclick="act('regenerate')" ${busy ? "disabled" : ""}>Regenerate</button>
          <button class="secondary" onclick="act('rework')" ${busy ? "disabled" : ""}>Rework with note</button>
          <button class="secondary" onclick="act('needs_rework')" ${busy ? "disabled" : ""}>Mark needs rework</button>
          <button class="secondary" onclick="act('skip')" ${busy ? "disabled" : ""}>Skip</button>
        </div>
        <div class="history">
          <p><strong>Version history</strong></p>
          ${renderHistory(c)}
        </div>
        <p class="status-line" id="actionStatus"></p>
      </div>
    </div>
  `;
}

function fmtNum(n) {
  if (n === null || n === undefined || n === "") return "—";
  const x = Number(n);
  if (Number.isNaN(x)) return "—";
  return x.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function renderHistory(c) {
  const hist = (c.history || []).slice().reverse();
  if (!hist.length) {
    return `<div class="empty">No archives yet. First regenerate/rework will create v001.</div>`;
  }
  return hist.map(h => {
    const isCurrent = Number(h.version) === Number(c.version);
    const thumb = h.image_url
      ? `<img src="${h.image_url}?t=${Date.now()}" alt="v${h.version}" />`
      : `<div class="empty">—</div>`;
    return `<div class="history-item ${isCurrent ? "current" : ""}">
      ${thumb}
      <div>
        <div><strong>v${h.version}</strong> ${isCurrent ? "(preferred)" : ""}</div>
        <div class="meta-sm">${escapeHtml(h.note || "no rework note")} · ${h.at || ""}</div>
      </div>
      <button class="secondary" ${busy || isCurrent ? "disabled" : ""}
        onclick="restoreVersion(${h.version})">Restore</button>
    </div>`;
  }).join("");
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, ch => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
  })[ch]);
}

async function selectWord(word) {
  const c = cards.find(x => x.word === word);
  if (!c) return;
  renderDetail(c);
  renderQueue();
}

async function refresh() {
  const data = await api("/api/cards");
  cards = data.cards;
  document.getElementById("stats").textContent =
    `${data.counts.total} cards · ${data.counts.accepted} accepted · ${data.counts.needs_attention} need attention`;
  renderFilters();
  renderQueue();
  if (current) {
    const updated = cards.find(c => c.word === current.word);
    if (updated) renderDetail(updated);
  } else if (filtered().length) {
    selectWord(filtered()[0].word);
  }
}

async function restoreVersion(version) {
  if (!current || busy) return;
  busy = true;
  document.getElementById("actionStatus").textContent = `Restoring v${version}…`;
  try {
    const data = await api(
      `/api/cards/${encodeURIComponent(current.word)}/restore/${version}`,
      { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" }
    );
    await refresh();
    if (data.card) selectWord(data.card.word);
    document.getElementById("actionStatus").textContent = data.message || "Restored.";
  } catch (e) {
    document.getElementById("actionStatus").textContent = e.message;
  } finally {
    busy = false;
    if (current) renderDetail(cards.find(c => c.word === current.word) || current);
  }
}

async function act(action) {
  if (!current || busy) return;
  const note = (document.getElementById("note")?.value || "").trim();
  if (action === "rework" && !note) {
    document.getElementById("actionStatus").textContent = "Add a rework note first.";
    return;
  }
  busy = true;
  document.getElementById("actionStatus").textContent =
    action === "regenerate" || action === "rework" ? "Generating with OpenAI…" : "Saving…";
  renderDetail(current);
  try {
    const data = await api(`/api/cards/${encodeURIComponent(current.word)}/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ note }),
    });
    await refresh();
    if (data.card) selectWord(data.card.word);
    document.getElementById("actionStatus").textContent = data.message || "Done.";
  } catch (e) {
    document.getElementById("actionStatus").textContent = e.message;
  } finally {
    busy = false;
    if (current) renderDetail(cards.find(c => c.word === current.word) || current);
  }
}

refresh().catch(e => {
  document.getElementById("stats").textContent = e.message;
});
</script>
</body>
</html>
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_manifest() -> dict:
    return osc.load_manifest(STATE["manifest_path"])


def _save_manifest(manifest: dict) -> None:
    osc.save_manifest(STATE["manifest_path"], manifest)


def _rel_image_url(image_path: str | None) -> str | None:
    if not image_path:
        return None
    p = Path(image_path)
    if not p.exists():
        return None
    try:
        rel = p.resolve().relative_to(STATE["cards_dir"].resolve())
    except ValueError:
        return None
    return "/media/" + rel.as_posix()


def _get_client():
    if STATE["client"] is not None:
        return STATE["client"]
    osc.load_dotenv(HERE / ".env")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing. Put it in .env")
    STATE["client"] = osc.OpenAIImageClient(key, STATE["model"])
    return STATE["client"]


def _bank_word_index() -> dict[str, dict]:
    bank = json.loads(STATE["bank_path"].read_text(encoding="utf-8"))
    items = osc.unique_words_from_bank(
        bank,
        sort_id=None,
        only_words=None,
        biemiller_lookup=STATE.get("biemiller_lookup") or {},
    )
    return {i["word"]: i for i in items}


def _enrich_card(word: str, entry: dict, bank_idx: dict[str, dict]) -> dict:
    meta = bank_idx.get(word, {})
    biem = (STATE.get("biemiller_lookup") or {}).get(word) or {}
    image = entry.get("image")
    has_image = bool(image and Path(image).exists() and str(image).lower().endswith(".png"))
    status = entry.get("status") or "pending_review"
    # Batch-generated cards may be stored as accepted + auto_accepted until a human
    # clicks Approve (which clears auto_accepted). Treat those as still pending.
    if status == "accepted" and entry.get("auto_accepted"):
        status = "pending_review"
    history = []
    for h in entry.get("history") or []:
        hist = dict(h)
        hist["image_url"] = _rel_image_url(h.get("archive"))
        history.append(hist)

    rating_mean = (
        entry.get("rating_mean")
        if entry.get("rating_mean") is not None
        else entry.get("mean")
        if entry.get("mean") is not None
        else meta.get("rating_mean", meta.get("mean"))
    )
    if rating_mean is None:
        rating_mean = biem.get("rating_mean")
    freq_pm = (
        entry.get("freq_pm")
        if entry.get("freq_pm") is not None
        else meta.get("freq_pm")
    )
    if freq_pm is None:
        freq_pm = biem.get("freq_pm")

    return {
        "word": word,
        "spelling": entry.get("spelling") or meta.get("spelling"),
        "as_in": meta.get("as_in"),
        "position": entry.get("position") or meta.get("position"),
        "prompt": entry.get("prompt") or osc.build_prompt(
            word, meta.get("spelling"), meta.get("as_in"), STATE["style"]
        ),
        "status": status,
        "image": image,
        "image_url": _rel_image_url(image) if has_image else None,
        "has_image": has_image,
        "version": entry.get("version"),
        "history": history,
        "sort_ids": entry.get("sort_ids") or meta.get("sort_ids") or [],
        "rating_mean": rating_mean,
        "mean": rating_mean,
        "freq_pm": freq_pm,
        "meaning": entry.get("meaning") or meta.get("meaning") or biem.get("meaning"),
        "pos": entry.get("pos") or meta.get("pos") or biem.get("pos"),
        "error": entry.get("error"),
        "updated_at": entry.get("updated_at"),
    }


def _queue_cards() -> list[dict]:
    manifest = _load_manifest()
    bank_idx = _bank_word_index()
    cards = {}
    for word, entry in (manifest.get("cards") or {}).items():
        cards[word] = _enrich_card(word, entry, bank_idx)
    # Include bank words not yet generated
    for word, meta in bank_idx.items():
        if word not in cards:
            cards[word] = {
                "word": word,
                "spelling": meta.get("spelling"),
                "as_in": meta.get("as_in"),
                "position": meta.get("position"),
                "prompt": osc.build_prompt(
                    word, meta.get("spelling"), meta.get("as_in"), STATE["style"]
                ),
                "status": "pending_review",
                "image": None,
                "image_url": None,
                "has_image": False,
                "version": None,
                "history": [],
                "sort_ids": meta.get("sort_ids") or [],
                "rating_mean": meta.get("rating_mean", meta.get("mean")),
                "mean": meta.get("mean", meta.get("rating_mean")),
                "freq_pm": meta.get("freq_pm"),
                "meaning": meta.get("meaning"),
                "pos": meta.get("pos"),
                "error": None,
                "updated_at": None,
            }
    out = list(cards.values())
    out.sort(key=lambda c: (0 if c["status"] != "accepted" else 1, c["word"]))
    return out


def _update_card(word: str, **fields) -> dict:
    manifest = _load_manifest()
    cards = manifest.setdefault("cards", {})
    entry = cards.get(word, {"word": word})
    # Keep ratings attached whenever we know them
    bank_idx = _bank_word_index()
    meta = bank_idx.get(word, {})
    biem = (STATE.get("biemiller_lookup") or {}).get(word) or {}
    if fields.get("rating_mean") is None:
        fields.setdefault(
            "rating_mean",
            entry.get("rating_mean", meta.get("rating_mean", biem.get("rating_mean"))),
        )
    if fields.get("freq_pm") is None:
        fields.setdefault(
            "freq_pm",
            entry.get("freq_pm", meta.get("freq_pm", biem.get("freq_pm"))),
        )
    if fields.get("mean") is None and fields.get("rating_mean") is not None:
        fields["mean"] = fields["rating_mean"]
    entry.update(fields)
    entry["word"] = word
    entry["updated_at"] = _now()
    cards[word] = entry
    _save_manifest(manifest)
    if fields.get("status") == "accepted" and fields.get("image"):
        osc.patch_bank_images(STATE["bank_path"], word, fields["image"], "accepted")
    return _enrich_card(word, entry, bank_idx)


def _generate(
    word: str,
    prompt: str,
    spelling: str | None,
    *,
    note: str | None = None,
    entry: dict | None = None,
) -> dict:
    client = _get_client()
    img_bytes = client.generate(prompt)
    saved = osc.save_versioned_card(
        STATE["cards_dir"],
        word,
        spelling,
        img_bytes,
        prompt,
        note=note,
        entry=entry,
        dry_run=False,
    )
    history = osc.merge_history(entry, saved["history_entry"])
    return {
        "image": saved["image"],
        "version": saved["version"],
        "history": history,
        "prompt": prompt,
        "archive": saved.get("archive"),
    }


@app.get("/")
def index():
    return render_template_string(REVIEW_HTML)


@app.get("/media/<path:subpath>")
def media(subpath: str):
    return send_from_directory(STATE["cards_dir"], subpath)


@app.get("/api/cards")
def api_cards():
    cards = _queue_cards()
    needs = [
        c for c in cards
        if c["status"] in ("needs_rework", "pending_review", "failed") or not c["has_image"]
    ]
    counts = {
        "total": len(cards),
        "accepted": sum(1 for c in cards if c["status"] == "accepted"),
        "needs_attention": len(needs),
        "needs_rework": sum(1 for c in cards if c["status"] == "needs_rework"),
        "failed": sum(1 for c in cards if c["status"] == "failed"),
    }
    return jsonify({"cards": cards, "counts": counts})


@app.post("/api/cards/<word>/approve")
def api_approve(word: str):
    word = word.lower()
    manifest = _load_manifest()
    entry = (manifest.get("cards") or {}).get(word)
    if not entry or not entry.get("image") or not Path(entry["image"]).exists():
        return jsonify({"error": "No image to approve. Generate or rework first."}), 400
    # Clear auto_accepted so the queue no longer remaps this to pending_review.
    card = _update_card(
        word,
        status="accepted",
        image=entry["image"],
        auto_accepted=False,
    )
    return jsonify({"card": card, "message": f"Approved {word}."})


@app.post("/api/cards/<word>/skip")
def api_skip(word: str):
    word = word.lower()
    card = _update_card(word, status="skipped")
    return jsonify({"card": card, "message": f"Skipped {word}."})


@app.post("/api/cards/<word>/needs_rework")
def api_needs_rework(word: str):
    word = word.lower()
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or "").strip()
    fields = {"status": "needs_rework"}
    if note:
        fields["rework_note"] = note
    card = _update_card(word, **fields)
    return jsonify({"card": card, "message": f"Marked {word} as needs rework."})


@app.post("/api/cards/<word>/regenerate")
def api_regenerate(word: str):
    word = word.lower()
    try:
        bank_idx = _bank_word_index()
        meta = bank_idx.get(word, {})
        manifest = _load_manifest()
        entry = (manifest.get("cards") or {}).get(word, {})
        prompt = entry.get("prompt") or osc.build_prompt(
            word, meta.get("spelling"), meta.get("as_in"), STATE["style"]
        )
        # Strip prior "Additional direction" on plain regenerate? Keep full prompt as-is.
        saved = _generate(
            word,
            prompt,
            meta.get("spelling") or entry.get("spelling"),
            entry=entry,
        )
        card = _update_card(
            word,
            status="pending_review",
            prompt=saved["prompt"],
            image=saved["image"],
            version=saved["version"],
            history=saved["history"],
            spelling=meta.get("spelling") or entry.get("spelling"),
            sort_ids=entry.get("sort_ids") or meta.get("sort_ids") or [],
            error=None,
        )
        return jsonify({
            "card": card,
            "message": f"Regenerated {word} as v{saved['version']}. Review and approve.",
        })
    except Exception as e:
        card = _update_card(word, status="failed", error=str(e))
        return jsonify({"error": str(e), "card": card}), 500


@app.post("/api/cards/<word>/rework")
def api_rework(word: str):
    word = word.lower()
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or "").strip()
    if not note:
        return jsonify({"error": "Rework note required."}), 400
    try:
        bank_idx = _bank_word_index()
        meta = bank_idx.get(word, {})
        manifest = _load_manifest()
        entry = (manifest.get("cards") or {}).get(word, {})
        base = entry.get("prompt") or osc.build_prompt(
            word, meta.get("spelling"), meta.get("as_in"), STATE["style"]
        )
        # Keep style consistency: append direction, don't replace style block
        prompt = f"{base} Additional direction: {note}"
        saved = _generate(
            word,
            prompt,
            meta.get("spelling") or entry.get("spelling"),
            note=note,
            entry=entry,
        )
        card = _update_card(
            word,
            status="pending_review",
            prompt=saved["prompt"],
            image=saved["image"],
            version=saved["version"],
            history=saved["history"],
            spelling=meta.get("spelling") or entry.get("spelling"),
            sort_ids=entry.get("sort_ids") or meta.get("sort_ids") or [],
            rework_note=note,
            error=None,
        )
        return jsonify({
            "card": card,
            "message": f"Reworked {word} as v{saved['version']}. Review and approve.",
        })
    except Exception as e:
        card = _update_card(word, status="failed", error=str(e))
        return jsonify({"error": str(e), "card": card}), 500


@app.post("/api/cards/<word>/restore/<int:version>")
def api_restore(word: str, version: int):
    word = word.lower()
    try:
        manifest = _load_manifest()
        entry = (manifest.get("cards") or {}).get(word)
        if not entry:
            return jsonify({"error": f"No manifest entry for {word}"}), 404
        restored = osc.restore_card_version(
            STATE["cards_dir"],
            word,
            entry.get("spelling"),
            version,
            entry,
        )
        card = _update_card(
            word,
            status="pending_review",
            image=restored["image"],
            version=restored["version"],
            prompt=restored.get("prompt") or entry.get("prompt"),
            restored_from=restored.get("restored_from"),
            error=None,
        )
        return jsonify({
            "card": card,
            "message": f"Restored {word} to v{version}. Review and approve.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    osc.load_dotenv(HERE / ".env")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    ap.add_argument("--cards", type=Path, default=DEFAULT_CARDS)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--model", default=os.environ.get("OPENAI_IMAGE_MODEL", osc.DEFAULT_MODEL))
    ap.add_argument("--style", default=osc.DEFAULT_STYLE)
    ap.add_argument(
        "--biemiller",
        type=Path,
        default=osc.DEFAULT_BIEMILLER,
        help="Kuperman×Biemiller CSV for Rating.Mean / Freq_pm",
    )
    args = ap.parse_args()

    STATE["bank_path"] = args.bank
    STATE["cards_dir"] = args.cards
    STATE["manifest_path"] = args.manifest or (args.cards / "manifest.json")
    STATE["style"] = args.style
    STATE["model"] = args.model
    STATE["biemiller_path"] = args.biemiller
    STATE["biemiller_lookup"] = osc.load_biemiller_lookup(args.biemiller)
    STATE["cards_dir"].mkdir(parents=True, exist_ok=True)

    print(f"Review gallery: http://{args.host}:{args.port}")
    print(f"Bank: {STATE['bank_path']}")
    print(f"Cards: {STATE['cards_dir']}")
    print(f"Manifest: {STATE['manifest_path']}")
    print(f"Biemiller ratings loaded: {len(STATE['biemiller_lookup'])} words")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
