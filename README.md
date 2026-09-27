# UAE Pharmacy Price and Availability Pipeline

A Playwright scraper that walks the full [medononline.com](https://medononline.com)
catalogue (Medon Pharmacy, UAE) and exports every product's **price** and
**availability** to an Excel workbook.

## Portfolio evidence

The committed workbook at `output/medononline_prices.xlsx` is a historical
catalogue snapshot, not a live inventory feed. A file-level review on
27 September 2026 found:

| Check | Observed result |
| --- | --- |
| Product rows / distinct product URLs | 10,188 / 10,188 |
| Currency | AED on all 10,188 rows |
| Missing prices | 0 |
| Availability | 8,629 in stock; 1,559 out of stock |
| Missing brand | 3,248 rows (31.88%) |
| Missing category | 9,704 rows (95.25%) |

This demonstrates extraction, resumable processing and a usable Excel output.
It does not establish current prices, real-time stock, sales demand, or savings.
The review checked the existing workbook and source; it did not rerun a live
collection. Brand and category completeness need improvement before using
those fields to compare segments. Availability is the storefront label, not
warehouse stock quantity.

## What it collects

| Column | Source | Notes |
| --- | --- | --- |
| Product Name | JSON-LD | |
| SKU | JSON-LD | |
| Brand | rendered DOM | partially server-rendered; see *Modes* |
| Category | rendered DOM | breadcrumb chain, rich mode only |
| Price | JSON-LD | current selling price |
| Original Price | rendered DOM | struck-through price, discounted items only |
| Discount % | derived | from original vs. current price |
| Currency | JSON-LD | AED |
| Availability | JSON-LD | `In Stock` / `Out of Stock` |
| Stock Label | rendered DOM | shopper-facing chips, e.g. `In Stock, Limited Stock` |
| Rating / Reviews | rendered DOM | |
| Expiry | rendered DOM | shown on short-dated stock |
| Product URL / Image URL | JSON-LD | |
| Scraped At (UTC) | — | per-row timestamp |

The workbook has two sheets: **Products** (one row per product, filterable and
frozen header) and **Summary** (counts, price range, availability breakdown).

## How it works

1. Product URLs come from the site's public `sitemap.xml` (~10,200 products).
2. Each page is opened in headless Chromium.
3. Price and availability are read from the page's `schema.org/Product` JSON-LD
   block, which is server-rendered and therefore reliable.
4. The rendered DOM supplies the extras that only exist after hydration.
5. Rows stream to `output/products.jsonl` as they are scraped, so an interrupted
   run can be continued with `--resume`. The workbook is built from that file.

## Usage

```bash
pip install -r requirements.txt
playwright install chromium

python scrape_medon.py --fast                 # full catalogue, price and availability
python scrape_medon.py                        # full catalogue + category, slow
python scrape_medon.py --limit 50             # quick sample
python scrape_medon.py --fast --resume        # continue an interrupted run
```

### Modes

The site loads brand and category client-side, one breadcrumb level at a time,
via its own API. Waiting for that is what makes a run slow, so there are two modes:

- **`--fast` (recommended).** Skips the hydration wait. Price, availability,
  name and SKU are collected without waiting for additional hydration. Brand
  and especially category may be missing; see the committed snapshot checks above.
  Runtime varies with site response and concurrency.
- **Default (rich).** Waits for the breadcrumb chain to settle, so category and
  brand can be more complete, but completeness must still be checked. Each product costs several seconds, and
  the site's API slows down under concurrency, so a full run takes hours. Best
  used with `--limit` on a subset, or overnight with `--resume`.

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--limit N` | all | scrape only the first N products |
| `--concurrency N` | 6 | parallel browser tabs |
| `--delay S` | 0.4 | base pause between requests per tab |
| `--retries N` | 3 | attempts per product |
| `--fast` | off | skip the hydration wait |
| `--resume` | off | skip products already in the JSONL file |
| `--output PATH` | `output/medononline_prices.xlsx` | workbook path |
| `--jsonl PATH` | `output/products.jsonl` | streaming results file |
| `--headful` | off | show the browser window |

## Collection behavior

The implementation visits public product pages listed in the sitemap and does
not call the store API directly. Review the current site terms and crawling
policy before a new collection; the development notes describe a past observation. Images, fonts, stylesheets and media are blocked to keep
bandwidth down, and each tab pauses between requests. Keep the concurrency
modest — the site's own backend is the bottleneck, and pushing it harder makes
the data worse, not the run faster.
