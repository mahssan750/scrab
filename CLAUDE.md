# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-purpose Playwright scraper: it walks the full medononline.com catalogue
(Medon Pharmacy, a MOHAP-licensed UAE online pharmacy) and exports every
product's price and availability to an Excel workbook.

There is no framework, no test suite and no build step. The whole scraper is
`scrape_medon.py`; `README.md` is the user-facing documentation and
`docs/SESSION-NOTES.md` records how the site behaves and why the code is shaped
the way it is. **Read `docs/SESSION-NOTES.md` before changing the extraction
logic** — most of the non-obvious code exists to work around a specific site
behaviour documented there, and the selectors were arrived at empirically.

## Commands

```bash
pip install -r requirements.txt
playwright install chromium

python scrape_medon.py --fast                 # full catalogue, ~2h, price+availability
python scrape_medon.py                        # full catalogue + category, 8h+
python scrape_medon.py --limit 50             # quick sample while developing
python scrape_medon.py --fast --resume        # continue an interrupted run
```

There is no linter or test runner configured. To check a change, run against a
small `--limit` and inspect `output/products.jsonl` directly — fill rates per
field are the fastest signal that an extractor has silently broken.

## Architecture

One file, four stages:

1. **URL discovery** — `fetch_product_urls()` pulls `<loc>` entries out of
   `https://medononline.com/sitemap.xml` and keeps the `/product/` ones.
2. **Per-page scrape** — `scrape_one()` opens the page in headless Chromium,
   optionally waits for hydration (`BREADCRUMB_READY_JS`), then runs
   `DOM_EXTRACT_JS` in the page and grabs `page.content()`.
3. **Row assembly** — `build_product()` merges the two sources into a `Product`
   dataclass. **JSON-LD is authoritative for price and availability**; the DOM
   only supplies fields that JSON-LD does not carry.
4. **Export** — `write_excel()` writes a formatted `Products` sheet plus a
   `Summary` sheet.

Rows stream to `output/products.jsonl` as they are scraped and the workbook is
built from that file at the end, so a crashed run loses nothing and `--resume`
picks up where it stopped. Rows with an `error` are re-tried on resume.

### Two modes, and why

The site server-renders the `schema.org/Product` JSON-LD but loads brand and
category **client-side, one breadcrumb level at a time**, via its own API.
Waiting for that is the entire cost of a run.

- `--fast` skips the hydration wait: price, availability, name, SKU, stock label
  and rating are complete; brand lands for ~2/3 of products, category is blank.
- Default (rich) waits for the breadcrumb chain to settle: category and brand are
  essentially complete, but a full run takes 8h+.

Raising `--concurrency` does **not** straightforwardly speed things up: the
site's own API is the bottleneck, so more tabs make each page hydrate slower and
the category data measurably worse. See the measurements in the session notes.

## Conventions

- Python 3.12, standard library plus `playwright` and `openpyxl` only.
- The scraper is polite by design and should stay that way: `robots.txt` allows
  everything except `/api/` and `/order-success/`, so **only public product
  pages from the sitemap are visited and the API is never called directly**.
  Images, fonts, stylesheets and media are blocked; each tab pauses between
  requests. Keep concurrency modest.
- Extraction is defensive throughout — pages ship an empty JSON-LD block, chips
  render without separators, and promo banners sit inside the product grid. New
  extractors should assume the same and degrade to a blank field, never a wrong
  one.
- `output/*.xlsx` is committed (it is the deliverable). `output/products.jsonl`
  and `output/*.log` are gitignored — the workbook carries the same fields.

## Git workflow

Private repo `https://github.com/mahssan750/scrab`, default branch **`main`**
(note: the learnone.org repos use `master`). Commit and push each change without
being asked, per the standing policy for this account.
