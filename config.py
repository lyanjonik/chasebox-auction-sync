"""
Chase Box comps — configuration & tunables.

Secrets come from environment variables (or a local .env file, which this module
loads automatically). Nothing secret is hard-coded. Copy .env.example -> .env.
"""

from __future__ import annotations
import os

# ---------------------------------------------------------------------------
# .env loader (tiny, stdlib-only — no python-dotenv dependency)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    full = os.path.join(here, path)
    if not os.path.exists(full):
        return
    with open(full, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("COMPS_DB_PATH", os.path.join(os.path.dirname(__file__), "comps.db"))

# ---------------------------------------------------------------------------
# Credentials (all optional — engine runs in SIM mode when absent)
# ---------------------------------------------------------------------------

SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "chase-box.myshopify.com")
SHOPIFY_ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")   # shpat_...
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-07")

PRICECHARTING_TOKEN = os.environ.get("PRICECHARTING_TOKEN", "")   # TCG
SPORTSCARDSPRO_TOKEN = os.environ.get("SPORTSCARDSPRO_TOKEN", "")  # sports cards

FX_API_URL = os.environ.get("FX_API_URL", "https://open.er-api.com/v6/latest/USD")

# ---------------------------------------------------------------------------
# Pricing model — labeled PLACEHOLDER premiums (global -> PH). PRIORS, not truth.
# Shown to buyers as "estimated" and overwritten by real sales over time.
# ---------------------------------------------------------------------------

PLACEHOLDER_FACTORS = {
    "tcg_single":    1.30,
    "tcg_sealed":    1.25,
    "tcg_graded":    1.12,
    "sports_single": 1.15,
    "sports_graded": 1.10,
}

# Blend / calibration knobs
FACTOR_BLEND_K = 5        # higher = trust the placeholder factor longer
CARD_BLEND_K = 3         # higher = trust the category baseline longer per card
CARD_BLEND_CAP = 0.85    # never let one card's own median be 100% of the value
FACTOR_BLEND_CAP = 0.90
SANITY_JUMP = 0.25       # day-over-day move beyond this is quarantined for review

# ---------------------------------------------------------------------------
# Pricing-category derivation
# Storefront category (Sports Cards / TCG) + grade -> a key in PLACEHOLDER_FACTORS.
# A product may override this with a `custom.comps_category` metafield.
# ---------------------------------------------------------------------------

def pricing_category(storefront_category: str | None, grade: str | None,
                     sealed: bool = False) -> str:
    cat = (storefront_category or "").lower()
    g = (grade or "").upper()
    is_graded = any(k in g for k in ("PSA", "BGS", "SGC", "CGC", "GRADED"))
    if "tcg" in cat or "pok" in cat:
        if sealed:
            return "tcg_sealed"
        return "tcg_graded" if is_graded else "tcg_single"
    # default to sports
    return "sports_graded" if is_graded else "sports_single"


def has_live_shopify() -> bool:
    return bool(SHOPIFY_ADMIN_TOKEN)
