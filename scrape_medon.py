#!/usr/bin/env python3
"""Scrape product prices and availability from medononline.com with Playwright.

Product URLs come from the public sitemap. Each product page is opened in a real
browser; the authoritative price/availability values are read from the page's
JSON-LD Product block, and the rendered DOM supplies the extras (brand, category
path, pre-discount price, stock label, rating).

Results stream to a JSONL file as they are scraped, so an interrupted run can be
resumed with --resume. The Excel workbook is built from that file at the end.

Usage:
    python scrape_medon.py                      # whole catalogue
    python scrape_medon.py --limit 50           # quick sample
    python scrape_medon.py --resume             # continue an interrupted run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError

BASE_URL = "https://medononline.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
PRODUCT_URL_RE = re.compile(r"<loc>\s*(https://medononline\.com/product/[^<\s]+)\s*</loc>")
JSON_LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)

# Resources that cost bandwidth but carry no pricing data.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

AVAILABILITY_LABELS = {
    "instock": "In Stock",
    "outofstock": "Out of Stock",
    "limitedavailability": "Limited Availability",
    "soldout": "Sold Out",
    "backorder": "Back Order",
    "preorder": "Pre-Order",
    "discontinued": "Discontinued",
}

# The breadcrumb appends one crumb at a time, so there is no single "done" event.
# Treat it as complete once the crumb count has held steady across a few polls.
BREADCRUMB_READY_JS = r"""
() => {
  const navs = [...document.querySelectorAll("main nav")];
  const h1 = document.querySelector("h1");
  const name = h1 ? h1.textContent.trim() : "";
  // Uncategorised products render a breadcrumb with no /category/ links at all
  // ("All Categories > <product>"), so fall back to matching the product name
  // rather than waiting out the timeout on them.
  const nav = navs.find((n) => n.querySelector('a[href*="/category/"]'))
    || (name ? navs.find((n) => n.textContent.includes(name)) : null);
  if (!nav) return false;
  const count = nav.querySelectorAll('a[href*="/category/"]').length;
  const state = (window.__crumbState = window.__crumbState
    || { count: -1, stable: 0, t0: Date.now() });
  if (count === state.count) state.stable += 1;
  else { state.count = count; state.stable = 0; }
  // Each crumb level is fetched separately and lands roughly a second after the
  // previous one, so the window has to outlast that gap or the chain is cut short.
  if (state.stable < 8) return false;
  // A settled zero means "genuinely uncategorised", but only once the page has
  // had real time to render crumbs - under load the first level can take several
  // seconds, and escaping early silently blanks the category for every product.
  if (count === 0) return Date.now() - state.t0 > 6000;
  return true;
}
"""

# Runs inside the page. Pulls the fields that only exist in the rendered DOM.
DOM_EXTRACT_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const out = {};

  // --- Breadcrumb category path ---
  // The header mega-menu also links to /category/, so only trust a nav inside
  // <main>. The chain is rendered twice (mobile + desktop), hence the dedupe.
  const crumbNav = [...document.querySelectorAll("main nav")]
    .find((n) => n.querySelector('a[href*="/category/"]'));
  if (crumbNav) {
    const seen = new Set();
    const crumbs = [];
    for (const a of crumbNav.querySelectorAll('a[href*="/category/"]')) {
      const label = clean(a.textContent);
      if (label && !seen.has(label)) { seen.add(label); crumbs.push(label); }
    }
    out.category_path = crumbs.join(" > ");
  }

  // --- Brand (links are /brands/<slug>-<id>) ---
  const brandLink = [...document.querySelectorAll('main a[href*="/brands/"]')]
    .find((a) => clean(a.textContent));
  if (brandLink) {
    out.brand = clean(brandLink.textContent)
      .replace(/Explore all the Products.*$/i, "")
      .trim();
  }

  // --- Locate the product info column via the SKU chip, then climb to it ---
  const skuNode = document.evaluate(
    "//*[contains(normalize-space(text()),'SKU')]",
    document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
  ).singleNodeValue;

  let column = null;
  if (skuNode) {
    let n = skuNode;
    for (let i = 0; i < 8 && n; i++) {
      const cls = (n.className || "").toString();
      if (cls.includes("min-w-0") && cls.includes("flex-col")) { column = n; break; }
      n = n.parentElement;
    }
    if (!column) column = skuNode.parentElement?.parentElement || null;
  }

  const scope = column || document.querySelector("main") || document.body;
  const scopeText = clean(scope.textContent);

  // --- Pre-discount price: struck-through figure inside the product column ---
  const strike = scope.querySelector('s, del, [class*="line-through"]');
  if (strike) out.original_price_text = clean(strike.textContent);

  // --- Stock labels as shown to shoppers ---
  // Adjacent chips render without separators ("In StockLimited Stock"), so no
  // trailing \b is available; collect every distinct label instead.
  const labels = [];
  const stockRe = /(Out of Stock|Limited Stock|Low Stock|Sold Out|Pre-?Order|In Stock)/gi;
  let m;
  while ((m = stockRe.exec(scopeText)) !== null) {
    const label = m[1];
    if (!labels.some((l) => l.toLowerCase() === label.toLowerCase())) labels.push(label);
  }
  if (labels.length) out.stock_label = labels.join(", ");

  // --- Rating and review count, rendered as "0.0 (0)" ---
  const rating = scopeText.match(/(\d(?:\.\d)?)\s*\((\d+)\)/);
  if (rating) { out.rating = rating[1]; out.reviews_count = rating[2]; }

  // --- Discount badge and expiry sit beside the column in the product grid ---
  // Scope to that grid so related-product cards further down cannot leak in.
  const grid = column ? column.parentElement : null;
  if (grid) {
    const gridText = clean(grid.textContent);
    // The sidebar promo ("Up to 40% off") is inside the grid too - ignore it.
    const discRe = /(\d{1,2})\s*%\s*OFF/gi;
    let d;
    while ((d = discRe.exec(gridText)) !== null) {
      const before = gridText.slice(Math.max(0, d.index - 10), d.index);
      if (!/up\s*to\s*$/i.test(before)) { out.discount_badge_pct = d[1]; break; }
    }
    const exp = gridText.match(/EXP\s+(\d{2}\s+\d{2}\s+\d{4})/i);
    if (exp) out.expiry = exp[1].replace(/\s+/g, "/");
  }

  return out;
}
"""


@dataclass
class Product:
    """One row of the output workbook."""

    url: str
    sku: str = ""
    name: str = ""
    brand: str = ""
    category_path: str = ""
    price: float | None = None
    original_price: float | None = None
    discount_pct: float | None = None
    currency: str = ""
    availability: str = ""
    availability_schema: str = ""
    stock_label: str = ""
    rating: float | None = None
    reviews_count: int | None = None
    expiry: str = ""
    image_url: str = ""
    scraped_at: str = ""
    error: str = ""


def parse_number(value) -> float | None:
    """Pull a number out of '1,234.50', 'AED 19.00', 19.0 or None."""
    if value is None:
        return None
    match = re.search(r"\d[\d,]*\.?\d*", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def extract_product_json_ld(html: str) -> dict:
    """Return the schema.org Product block, or {} if the page has none."""
    for block in JSON_LD_RE.findall(html):
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict) and d.get("@type") == "Product"), None)
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
    return {}


def build_product(url: str, html: str, dom: dict) -> Product:
    """Merge the JSON-LD block and the DOM extras into a single row."""
    product = Product(url=url, scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    data = extract_product_json_ld(html)
    if not data:
        product.error = "no Product JSON-LD found"
        return product

    product.name = str(data.get("name") or "")
    product.sku = str(data.get("sku") or data.get("mpn") or "")

    image = data.get("image")
    if isinstance(image, list):
        product.image_url = str(image[0]) if image else ""
    elif image:
        product.image_url = str(image)

    offers = data.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        product.price = parse_number(offers.get("price"))
        product.currency = str(offers.get("priceCurrency") or "")
        schema_url = str(offers.get("availability") or "")
        product.availability_schema = schema_url
        key = schema_url.rsplit("/", 1)[-1].lower()
        product.availability = AVAILABILITY_LABELS.get(key, key or "Unknown")

    product.brand = dom.get("brand", "") or ""
    product.stock_label = dom.get("stock_label", "") or ""
    product.expiry = dom.get("expiry", "") or ""

    # The breadcrumb's last crumb is the product itself; drop it.
    crumbs = [c for c in (dom.get("category_path") or "").split(" > ") if c]
    if crumbs and product.name and crumbs[-1].lower() == product.name.lower():
        crumbs = crumbs[:-1]
    product.category_path = " > ".join(crumbs)

    product.rating = parse_number(dom.get("rating"))
    reviews = parse_number(dom.get("reviews_count"))
    product.reviews_count = int(reviews) if reviews is not None else None

    original = parse_number(dom.get("original_price_text"))
    # Only trust the struck-through figure if it is genuinely above the sale price.
    if original and product.price and original > product.price:
        product.original_price = original
        product.discount_pct = round((original - product.price) / original * 100, 2)
    elif dom.get("discount_badge_pct") and product.price:
        badge = parse_number(dom["discount_badge_pct"])
        if badge:
            product.discount_pct = badge

    return product


async def fetch_product_urls(context, limit: int | None) -> list[str]:
    """Read product URLs from the sitemap, newest entries first."""
    response = await context.request.get(SITEMAP_URL, timeout=120_000)
    if not response.ok:
        raise RuntimeError(f"sitemap request failed: HTTP {response.status}")
    xml = await response.text()

    urls, seen = [], set()
    for url in PRODUCT_URL_RE.findall(xml):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls[:limit] if limit else urls


async def scrape_one(page, url: str, retries: int, delay: float, fast: bool = False) -> Product:
    """Load one product page, with retries on transient failures."""
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            # Price and availability are server-rendered, but brand, category and
            # the struck-through price only appear once the client data lands.
            # The breadcrumb is the last piece to render, so use it as the signal.
            try:
                if fast:
                    raise PlaywrightError("fast mode: skipping hydration wait")
                # The breadcrumb fills in one crumb at a time and ends with the
                # product name, so "breadcrumb contains the <h1>" means the whole
                # chain has arrived. Polling the DOM directly also sidesteps the
                # hidden mobile/desktop duplicate that a visibility wait trips on.
                await page.wait_for_function(
                    BREADCRUMB_READY_JS, timeout=8_000, polling=250
                )
            except PlaywrightError:
                pass  # uncategorised product - fall back to whatever rendered

            html = await page.content()
            try:
                dom = await page.evaluate(DOM_EXTRACT_JS)
            except PlaywrightError:
                dom = {}

            product = build_product(url, html, dom or {})
            if product.error and attempt < retries:
                last_error = product.error
                await asyncio.sleep(1.5 * attempt)
                continue
            return product

        except PlaywrightError as exc:
            last_error = str(exc).split("\n", 1)[0][:200]
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt + random.random())

    return Product(
        url=url,
        scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        error=last_error or "failed after retries",
    )


async def worker(name, context, queue, results, out_file, lock, args, progress):
    page = await context.new_page()
    try:
        while True:
            try:
                url = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            product = await scrape_one(page, url, args.retries, args.delay, args.fast)
            async with lock:
                results.append(product)
                out_file.write(json.dumps(asdict(product), ensure_ascii=False) + "\n")
                out_file.flush()
                progress["done"] += 1
                if product.error:
                    progress["errors"] += 1
                done, total = progress["done"], progress["total"]
                if done % 25 == 0 or done == total:
                    rate = done / max(time.time() - progress["start"], 0.1)
                    eta = (total - done) / rate if rate else 0
                    print(
                        f"  [{done}/{total}] {rate:.1f} pages/s  "
                        f"ETA {eta/60:.1f} min  errors: {progress['errors']}",
                        flush=True,
                    )
            await asyncio.sleep(args.delay * random.uniform(0.5, 1.5))
    finally:
        await page.close()


async def run_scrape(args) -> list[Product]:
    jsonl_path = Path(args.jsonl)
    already_done: set[str] = set()
    existing: list[Product] = []

    if args.resume and jsonl_path.exists():
        with jsonl_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Retry rows that previously failed.
                if record.get("error"):
                    continue
                already_done.add(record["url"])
                existing.append(Product(**record))
        print(f"Resuming: {len(already_done)} products already scraped.")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=not args.headful,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-AE",
        )
        await context.route(
            "**/*",
            lambda route: asyncio.ensure_future(
                route.abort() if route.request.resource_type in BLOCKED_RESOURCE_TYPES
                else route.continue_()
            ),
        )

        print(f"Fetching product URLs from {SITEMAP_URL} ...")
        urls = await fetch_product_urls(context, args.limit)
        pending = [u for u in urls if u not in already_done]
        print(f"{len(urls)} products in sitemap; {len(pending)} to scrape "
              f"with {args.concurrency} workers.")

        if not pending:
            await browser.close()
            return existing

        queue: asyncio.Queue = asyncio.Queue()
        for url in pending:
            queue.put_nowait(url)

        results: list[Product] = []
        lock = asyncio.Lock()
        progress = {"done": 0, "total": len(pending), "errors": 0, "start": time.time()}

        mode = "a" if (args.resume and jsonl_path.exists()) else "w"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open(mode, encoding="utf-8") as out_file:
            workers = [
                asyncio.create_task(
                    worker(i, context, queue, results, out_file, lock, args, progress)
                )
                for i in range(args.concurrency)
            ]
            await asyncio.gather(*workers)

        await context.close()
        await browser.close()

    return existing + results


def write_excel(products: list[Product], path: Path) -> None:
    """Write the products sheet plus a summary sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    columns = [
        ("Product Name", "name", 46),
        ("SKU", "sku", 14),
        ("Brand", "brand", 18),
        ("Category", "category_path", 38),
        ("Price", "price", 11),
        ("Original Price", "original_price", 14),
        ("Discount %", "discount_pct", 11),
        ("Currency", "currency", 9),
        ("Availability", "availability", 15),
        ("Stock Label", "stock_label", 14),
        ("Rating", "rating", 8),
        ("Reviews", "reviews_count", 9),
        ("Expiry", "expiry", 12),
        ("Product URL", "url", 60),
        ("Image URL", "image_url", 40),
        ("Scraped At (UTC)", "scraped_at", 21),
        ("Error", "error", 26),
    ]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Products"

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for index, (title, _, width) in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = width

    ordered = sorted(products, key=lambda p: (p.name or "").lower())
    for row_index, product in enumerate(ordered, start=2):
        for col_index, (_, attr, _) in enumerate(columns, start=1):
            sheet.cell(row=row_index, column=col_index, value=getattr(product, attr))

    money_format = "#,##0.00"
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row):
        row[4].number_format = money_format   # Price
        row[5].number_format = money_format   # Original Price
        row[6].number_format = "0.00"         # Discount %
        row[10].number_format = "0.0"         # Rating

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{sheet.max_row}"

    # --- Summary sheet ---
    summary = workbook.create_sheet("Summary")
    priced = [p for p in products if p.price is not None]
    discounted = [p for p in products if p.discount_pct]
    failed = [p for p in products if p.error]

    availability_counts: dict[str, int] = {}
    for product in products:
        if not product.error:
            key = product.availability or "Unknown"
            availability_counts[key] = availability_counts.get(key, 0) + 1

    rows = [
        ("Source", BASE_URL),
        ("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        ("", ""),
        ("Products scraped", len(products)),
        ("Scraped successfully", len(products) - len(failed)),
        ("Failed", len(failed)),
        ("", ""),
        ("Products with a price", len(priced)),
        ("Currency", priced[0].currency if priced else ""),
        ("Lowest price", min((p.price for p in priced), default="")),
        ("Highest price", max((p.price for p in priced), default="")),
        ("Average price", round(sum(p.price for p in priced) / len(priced), 2) if priced else ""),
        ("", ""),
        ("Discounted products", len(discounted)),
        ("Average discount %",
         round(sum(p.discount_pct for p in discounted) / len(discounted), 2) if discounted else ""),
        ("", ""),
        ("Availability breakdown", ""),
    ]
    rows += [(f"   {label}", count) for label, count in sorted(availability_counts.items())]

    for row_index, (label, value) in enumerate(rows, start=1):
        summary.cell(row=row_index, column=1, value=label).font = Font(bold=True)
        summary.cell(row=row_index, column=2, value=value)
    summary.column_dimensions["A"].width = 26
    summary.column_dimensions["B"].width = 34

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Scrape medononline.com prices and availability.")
    parser.add_argument("--limit", type=int, default=None,
                        help="scrape only the first N products (default: whole catalogue)")
    parser.add_argument("--concurrency", type=int, default=6,
                        help="parallel browser tabs (default: 6)")
    parser.add_argument("--delay", type=float, default=0.4,
                        help="base delay in seconds between requests per tab (default: 0.4)")
    parser.add_argument("--retries", type=int, default=3, help="attempts per product (default: 3)")
    parser.add_argument("--output", default="output/medononline_prices.xlsx",
                        help="Excel output path")
    parser.add_argument("--jsonl", default="output/products.jsonl",
                        help="progressive JSONL results file")
    parser.add_argument("--resume", action="store_true",
                        help="skip products already present in the JSONL file")
    parser.add_argument("--fast", action="store_true",
                        help="skip the hydration wait: much faster, but brand, category, "
                             "pre-discount price and rating are left blank")
    parser.add_argument("--headful", action="store_true", help="show the browser window")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    started = time.time()

    products = asyncio.run(run_scrape(args))
    if not products:
        print("No products scraped.", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    write_excel(products, output_path)

    failed = sum(1 for p in products if p.error)
    in_stock = sum(1 for p in products if p.availability == "In Stock")
    print(
        f"\nDone in {(time.time() - started)/60:.1f} min\n"
        f"  Products : {len(products)} ({failed} failed)\n"
        f"  In stock : {in_stock}\n"
        f"  Excel    : {output_path.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
