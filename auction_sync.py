"""
auction_sync.py — keep the wk_auction / wk_end_auction tags correct on Chase Box
auction lots, WITHOUT depending on Webkul's own (unreliable) tag sync.

WHY THIS EXISTS
---------------
Webkul is supposed to write `wk_auction` to a product when its auction goes live and
`wk_end_auction` when it ends. Those tags drive our whole storefront: the Auctions smart
collection, the homepage "On the block" row, the card countdowns, and the product page's
live/ended state. But Webkul's tag write is unreliable — observed 2026-07-27, two auctions
were LIVE in Webkul (bid widget rendering) yet the tag was NEVER written, even after
clicking Webkul's own "Sync Product Tags" button. The storefront showed nothing.

So we stop trusting the tag-write path and reconcile the tags ourselves, reading from the
path that DOES work: the same Webkul auction endpoint that already powers our card
countdowns (wk_auction_category.js -> sp-auction.webkul.com ... p=wk_ajax_process). That
endpoint returns the set of products with a LIVE auction — proven reliable, because if it
broke, the countdowns would break too. We only fill the tag Webkul forgot to write.

WHAT IT DOES (idempotent; safe to run on a schedule)
----------------------------------------------------
For every product marked `custom.listing_type = auction`:
  * live now (in the endpoint's response) -> ensure `wk_auction`, drop `wk_end_auction`
  * not live but currently tagged `wk_auction` (it just ended) -> swap to `wk_end_auction`
  * otherwise (pending, or already ended) -> leave alone
Only auction-designated products are touched, so buy-now items and Webkul's throwaway
"shopify auction-<id>" checkout products are never affected.

    python auction_sync.py             # reconcile once (writes tags)
    python auction_sync.py --dry-run   # report what it WOULD change, write nothing
    python auction_sync.py --verbose   # also list products needing no change

Schedule it every ~2-3 minutes (Windows Task Scheduler) so a new auction appears on the
storefront on its own, and an ended one flips within a couple of minutes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import config

# Self-logging: append every run's outcome here, independent of how the task invokes us
# (so "did it actually run?" is always answerable without relying on a .bat redirect).
LOG_FILE = Path(__file__).resolve().parent / "logs" / "auction_sync.log"


def _log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass  # logging must never crash the sync

ADMIN = f"https://{config.SHOPIFY_STORE}/admin/api/{config.SHOPIFY_API_VERSION}"
GRAPHQL = f"{ADMIN}/graphql.json"

# Webkul's live-auction data endpoint — the same one wk_auction_category.js calls to render
# card countdowns. api_url is currentScript.src.split('js/')[0] = this host root.
WK_ENDPOINT = "https://sp-auction.webkul.com/index.php?p=wk_ajax_process"
# PH is UTC+8, so JS getTimezoneOffset() = -480. Only affects DISPLAYED times in the
# returned template, never which products are live (the server decides that by its own
# clock), but we send it to mirror the real request.
WK_TIMEDIFF = "-480"

AUCTION_TAG = "wk_auction"
ENDED_TAG = "wk_end_auction"

# Webkul's per-product bidding endpoint (the one the product page loads). Returns the bid
# widget HTML with the live figures embedded. We read it to mirror the current bid + bid
# count into metafields so the storefront cards can show "Current bid" instead of the
# (unused, ₱0) Shopify price on auction lots.
WK_BID_ENDPOINT = WK_ENDPOINT  # same index.php?p=wk_ajax_process, different callback/params


def _admin(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{ADMIN}{path}", data=data, method=method,
        headers={"Content-Type": "application/json",
                 "X-Shopify-Access-Token": config.SHOPIFY_ADMIN_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} on {method} {path}\n{e.read().decode()[:400]}")


def _graphql(query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GRAPHQL, data=body,
        headers={"Content-Type": "application/json",
                 "X-Shopify-Access-Token": config.SHOPIFY_ADMIN_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode())
    if "errors" in out:
        raise SystemExit(f"GraphQL errors: {out['errors']}")
    return out["data"]


_PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId
      title
      tags
      listingType: metafield(namespace: "custom", key: "listing_type") { value }
    }
  }
}
"""


def auction_products() -> list[dict]:
    """Every product whose custom.listing_type == 'auction'. Returns dicts with
    {id, title, tags}. id is the numeric (legacy) product id Webkul + the tag API use."""
    out: list[dict] = []
    cursor = None
    while True:
        data = _graphql(_PRODUCTS_QUERY, {"cursor": cursor})
        conn = data["products"]
        for n in conn["nodes"]:
            lt = (n.get("listingType") or {}).get("value", "")
            if (lt or "").strip().lower() == "auction":
                out.append({
                    "id": str(n["legacyResourceId"]),
                    "title": n["title"],
                    "tags": [t.strip() for t in n.get("tags", []) if t.strip()],
                })
        if conn["pageInfo"]["hasNextPage"]:
            cursor = conn["pageInfo"]["endCursor"]
        else:
            return out


def live_auction_ids(product_ids: list[str]) -> set[str]:
    """Ask Webkul which of these products have a LIVE auction right now. The endpoint
    returns a jsonp object keyed by product id (plus a shared 'theme'); a key is present
    only when that product's auction is live. Absent = not live. Fails SAFE: on any error
    it raises, so the caller aborts rather than mistagging everything as ended."""
    if not product_ids:
        return set()
    params = {
        "shop_name": config.SHOPIFY_STORE,
        "products": ",".join(product_ids) + ",",   # Webkul's JS always trails a comma
        "cust_id": "",
        "timediff": WK_TIMEDIFF,
        "active_currency": "PHP",
        "callback": "show_auction_on_category",
    }
    url = f"{WK_ENDPOINT}&{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0",
                      "Referer": f"https://{config.SHOPIFY_STORE}/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    m = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", raw, re.S)
    if not m:
        raise SystemExit(f"Unexpected Webkul response (not jsonp): {raw[:200]}")
    data = json.loads(m.group(1))
    return {k for k in data.keys() if k != "theme"}


def fetch_bid_data(prod_id: str) -> tuple[int | None, int] | None:
    """Return (current_bid_cents, bid_count) for a product's LIVE auction, or None on error.
    Reads Webkul's biddingform widget (the same data the product page shows). 'Current bid'
    is the highest bid so far; with zero bids it's the opening/start amount. Figures verified
    against live auctions 2026-07-27 (min_bid_amount var = next-min bid; 'Maximum bidding
    amount allowed' text = current highest, exactly one increment below next-min)."""
    params = {
        "shop_name": config.SHOPIFY_STORE, "prod_id": prod_id, "cust_id": "",
        "page": "product", "timediff": WK_TIMEDIFF, "callback": "biddingform",
    }
    url = f"{WK_BID_ENDPOINT}&{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0",
                      "Referer": f"https://{config.SHOPIFY_STORE}/"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
        m = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", raw, re.S)
        html = json.loads(m.group(1)) if m else ""
    except Exception:
        return None
    if not isinstance(html, str) or not html:
        return None

    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    cm = (re.search(r'data-bids-label="\s*(\d+)\s*Bid', html)
          or re.search(r"(\d+)\s*Bid\(s\)", text))
    count = int(cm.group(1)) if cm else 0

    # next-min bid (clean JS var) — equals the opening amount when there are no bids yet
    nm = re.search(r"var\s+min_bid_amount\s*=\s*([\d.]+)", html)
    next_min = float(nm.group(1)) if nm else None
    # current highest (only meaningful once someone has bid)
    hm = re.search(r"Maximum bidding amount allowed[^0-9]*([\d,]+\.?\d*)", text)
    highest = float(hm.group(1).replace(",", "")) if hm else None

    amount = highest if (count > 0 and highest is not None) else next_min
    if amount is None:
        return None
    return round(amount * 100), count


_METAFIELDS_SET = """
mutation($mf: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $mf) { userErrors { field message } }
}
"""


def set_bid_metafields(prod_id: str, current_bid_cents: int, bid_count: int) -> None:
    gid = f"gid://shopify/Product/{prod_id}"
    mf = [
        {"ownerId": gid, "namespace": "custom", "key": "current_bid",
         "type": "number_integer", "value": str(current_bid_cents)},
        {"ownerId": gid, "namespace": "custom", "key": "bid_count",
         "type": "number_integer", "value": str(bid_count)},
    ]
    res = _graphql(_METAFIELDS_SET, {"mf": mf})
    errs = res["metafieldsSet"]["userErrors"]
    if errs:
        raise SystemExit(f"metafieldsSet errors for {prod_id}: {errs}")


def desired_tags(current: list[str], is_live: bool) -> list[str]:
    """Reconcile the auction lifecycle tags, preserving every other tag as-is."""
    tags = [t for t in current if t not in (AUCTION_TAG, ENDED_TAG)]
    if is_live:
        tags.append(AUCTION_TAG)
    else:
        # Only mark ended if it WAS live (had wk_auction) or is already ended. A product
        # that was merely pending stays untagged (theme shows "bidding opens soon").
        if AUCTION_TAG in current or ENDED_TAG in current:
            tags.append(ENDED_TAG)
    return tags


def reconcile(dry_run: bool, verbose: bool) -> None:
    products = auction_products()
    if not products:
        print("No products marked listing_type=auction. Nothing to reconcile.")
        return

    live = live_auction_ids([p["id"] for p in products])
    print(f"{len(products)} auction lot(s); Webkul reports {len(live)} live: "
          f"{sorted(live) or '(none)'}")

    changed = 0
    for p in products:
        is_live = p["id"] in live

        # For live auctions, mirror the current bid + count into metafields so the cards
        # can show "Current bid ₱X" instead of the unused ₱0 Shopify price. (Lags real time
        # by at most the sync interval; the product-page widget stays live.)
        if is_live and not dry_run:
            bd = fetch_bid_data(p["id"])
            if bd is not None:
                cents, count = bd
                set_bid_metafields(p["id"], cents, count)
                if verbose:
                    print(f"  bid   {p['title'][:44]:44s} current=PHP {cents/100:.2f} ({count} bid(s))")
        elif is_live and dry_run:
            bd = fetch_bid_data(p["id"])
            if bd is not None:
                print(f"  bid   {p['title'][:44]:44s} WOULD set current=PHP {bd[0]/100:.2f} ({bd[1]} bid(s))")

        want = desired_tags(p["tags"], is_live)
        # compare as sets on the two lifecycle tags only (order-insensitive)
        now_life = {t for t in p["tags"] if t in (AUCTION_TAG, ENDED_TAG)}
        want_life = {t for t in want if t in (AUCTION_TAG, ENDED_TAG)}
        state = "LIVE" if is_live else "not-live"
        if now_life == want_life:
            if verbose:
                print(f"  ok    {p['title'][:44]:44s} [{state}] tags={sorted(now_life) or '—'}")
            continue

        action = f"{sorted(now_life) or '—'} -> {sorted(want_life) or '—'}"
        if dry_run:
            print(f"  WOULD {p['title'][:44]:44s} [{state}] {action}")
        else:
            _admin("PUT", f"/products/{p['id']}.json",
                   {"product": {"id": int(p["id"]), "tags": ", ".join(want)}})
            print(f"  set   {p['title'][:44]:44s} [{state}] {action}")
        changed += 1

    verb = "would change" if dry_run else "changed"
    print(f"Done. {changed} product(s) {verb}.")
    _log(f"OK: {len(products)} auction lot(s), {len(live)} live, {changed} {verb}"
         + (" [dry-run]" if dry_run else ""))


def main() -> None:
    # Windows console is cp1252; keep output UTF-8-safe (theme/logs may carry non-ASCII).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Reconcile Webkul auction tags on Chase Box products")
    ap.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    ap.add_argument("--verbose", action="store_true", help="also list products needing no change")
    args = ap.parse_args()
    try:
        reconcile(args.dry_run, args.verbose)
    except SystemExit as e:
        _log(f"ABORTED: {e}")
        raise
    except Exception:
        _log("CRASH: " + traceback.format_exc().replace("\n", " | "))
        raise


if __name__ == "__main__":
    main()
