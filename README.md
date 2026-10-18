# Word Sort Builder TYCTR

Online + local tool for **closed teacher-directed picture sorts** aligned to TYCTR spellings and Gradual Release tutor scripts (**I Do, You Watch → You Do, I Watch**).

## What it does

1. **Build a word bank** from TYCTR GPCs + Biemiller (`Rating.Mean`)
2. **Generate / refine** picture-sort cards — **OpenAI** in-app, **or Midjourney** via prompt export + PNG import
3. **Export** an approved pack
4. **Build a tutor script** (3-day closed picture sort, WTW gradual release) from that pack only

### Midjourney workflow (no API)

1. Build a bank and open the Generate queue.
2. Click **Export Midjourney prompts**.
3. Prefer the **BATCH PASTE** block (Midjourney `{a, b, c}` permutations) — one Imagine creates several jobs. Use Fast/Turbo. Caps: Basic 4 / Standard 10 / Pro–Mega 40 per paste.
4. Prompts include Biemiller **meanings** so the picture shows what the word means, then remains namable as that word for the sort.
5. Save preferred images as `word.png` (e.g. `man.png`).
6. Click **Import Midjourney images** and select the PNGs.
7. Review / approve / export pack as usual.

## Run locally

```powershell
cd "Word Sort Builder TYCTR"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# put OPENAI_API_KEY in .env

python sort_builder.py --port 5055
```

Open **http://127.0.0.1:5055** (Chrome blocks port 5060).

## Host online (Render — recommended)

Repo: [github.com/crdugann/wordsort](https://github.com/crdugann/wordsort)

1. Push this repo to GitHub (already configured as `origin` after first push).
2. Go to [render.com](https://render.com) → **New** → **Blueprint** (or Web Service) → connect `crdugann/wordsort`.
3. Set environment variables:
   - `OPENAI_API_KEY` — required for generate / rework
   - `APP_PASSWORD` — required so strangers can’t spend your API credits
   - `APP_USER` — default `tutor`
4. Attach a **persistent disk** at `/var/data` (or set `DATA_DIR`) so cards/packs survive restarts.
5. Deploy. Health check: `/health`

Start command (also in `Procfile` / `render.yaml`):

```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 180
```

`timeout 180` matters — OpenAI image jobs can take 10–30+ seconds each.

### Railway / Fly / other

Same idea: Python 3.12, `pip install -r requirements.txt`, run the gunicorn command, set `OPENAI_API_KEY` + `APP_PASSWORD`, mount a volume at `DATA_DIR`.

### Cloudflare

This app is a **long-running Python Flask** service (OpenAI image jobs + persistent `cards/` / `packs/`). It is **not** a fit for Cloudflare Pages or a plain Worker.

**Practical options:**

1. **Keep Render (or any host) + put Cloudflare in front** — point a Cloudflare domain/DNS at the Render URL (orange-cloud proxy). Easiest “through Cloudflare.”
2. **Cloudflare Tunnel (`cloudflared`)** — run the builder on a machine/VPS (or keep Render), install `cloudflared`, create a tunnel to `http://127.0.0.1:5055` (or the host port), map a hostname in Zero Trust. Good for private tutor access without opening firewall ports.
3. **Cloudflare Containers** — package the app as a Docker image and run it on Cloudflare’s container platform if your account has that enabled; still need persistent storage for cards/packs and the same env vars (`OPENAI_API_KEY`, `APP_PASSWORD`, `DATA_DIR`).

Do **not** deploy this repo as a static Pages site — card generation and review need the Python server.

## Security

- Never commit `.env` or API keys.
- Always set `APP_PASSWORD` on a public URL (HTTP Basic).
- Image generation bills your OpenAI account — review queue before bulk runs.

## Data included

| Path | Purpose |
|---|---|
| `data/spellings.csv` | TYCTR GPC spellings |
| `data/biemiller.csv` | Kuperman × Biemiller ratings |
| `sort_word_bank_*.json` | Sample banks (L1, L3, …) |

Generated images live under `cards/` and `packs/` (gitignored; stored on disk when hosted).

## CLI (optional)

```bash
python build_sort_bank.py --lesson 1
python openai_sort_cards.py --bank sort_word_bank_L1.json --sort L1_m_vs_s --limit 4 --yes
python export_approved_pack.py --bank sort_word_bank_L1.json --sort L1_m_vs_s
python build_tutor_script.py --pack packs/l1_m_vs_s_approved --sort L1_m_vs_s
```
