# Chase Box — Auction Sync

Keeps the Webkul auction tags (`wk_auction` / `wk_end_auction`) and the
`custom.current_bid` / `custom.bid_count` metafields correct on Chase Box auction lots,
so the storefront's collections and card countdowns stay right 24/7 — independent of any
one machine being on. Runs on GitHub Actions every ~5 minutes.

**This repo intentionally contains only the auction sync.** The Chase Box comps / pricing
engine is a separate, private system and is not part of this repository.

## What's here
- `auction_sync.py` — the reconcile script (standard library only)
- `config.py` — minimal Shopify settings loader (no secrets committed)
- `.github/workflows/auction-sync.yml` — the every-5-minute schedule

## Setup (one time)
Add one repository secret in **Settings → Secrets and variables → Actions**:
- `SHOPIFY_ADMIN_TOKEN` — a Shopify Admin API access token (`shpat_…`) with
  `read_products` + `write_products`.

`SHOPIFY_STORE` and `SHOPIFY_API_VERSION` are set as plain env values in the workflow.

## Run it by hand
**Actions** tab → **Chase Box Auction Sync** → **Run workflow**. A green check means it
worked; the log prints something like `4 auction lot(s); Webkul reports 3 live … Done.`
