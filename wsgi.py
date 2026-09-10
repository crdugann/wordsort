"""Production WSGI entrypoint for Word Sort Builder TYCTR."""

from __future__ import annotations

import os
from pathlib import Path

# Load .env before importing the app (local / docker)
_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    import openai_sort_cards as osc

    osc.load_dotenv(_env)

from sort_builder import WORKSPACE, app, sync_review_state  # noqa: E402
import openai_sort_cards as osc  # noqa: E402
import review_server as rs  # noqa: E402
from sort_builder import STATE  # noqa: E402

# Ensure writable dirs exist at boot
(WORKSPACE / "cards").mkdir(parents=True, exist_ok=True)
(WORKSPACE / "packs").mkdir(parents=True, exist_ok=True)
STATE["cards_dir"] = WORKSPACE / "cards"
STATE["manifest_path"] = STATE["cards_dir"] / "manifest.json"
rs.STATE["biemiller_lookup"] = osc.load_biemiller_lookup(STATE["biemiller_path"])
sync_review_state()

# Expose for gunicorn: `gunicorn wsgi:app`
application = app
