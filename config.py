"""
Minimal config for the PUBLIC Chase Box auction-sync service.

Deliberately carries ONLY the three Shopify settings that auction_sync.py needs — and
NONE of the Chase Box comps methodology (landed-cost model, matrix, scraping). That engine
stays OFF GitHub, on a private host. Secrets come from environment variables (GitHub Actions
secrets) or a local .env file; nothing secret is hard-coded here.
"""
from __future__ import annotations
import os


def _load_dotenv(path: str = ".env") -> None:
    """Tiny stdlib .env loader. No-ops when the file is absent (e.g. on CI runners),
    where the values come from the workflow's env/secrets instead."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass


_load_dotenv()

# HTTP header values must not carry a trailing newline; .strip() guards a token pasted
# into a secret/.env with a stray newline (urllib refuses such a header value).
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "").strip()
SHOPIFY_ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()  # shpat_...
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-07").strip()
