# medononline.com price & availability scraper

A Playwright scraper that walks the full [medononline.com](https://medononline.com)
catalogue (Medon Pharmacy, UAE) and exports every product's **price** and
**availability** to an Excel workbook.

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

python scrape_medon.py --fast                 # full catalogue, ~45 min
python scrape_medon.py                        # full catalogue + category, slow
python scrape_medon.py --limit 50             # quick sample
python scrape_medon.py --fast --resume        # continue an interrupted run
```

### Modes

The site loads brand and category client-side, one breadcrumb level at a time,
via its own API. Waiting for that is what makes a run slow, so there are two modes:

- **`--fast` (recommended).** Skips the hydration wait. Price, availability,
  name, SKU, stock label and rating are complete; brand is filled in for roughly
  two thirds of products and category is left blank. ~0.5s per product.
- **Default (rich).** Waits for the breadcrumb chain to settle, so category and
  brand are essentially complete — but each product costs several seconds, and
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

## Politeness

`robots.txt` allows crawling everything except `/api/` and `/order-success/`;
this scraper only visits public product pages listed in the sitemap and never
calls the API directly. Images, fonts, stylesheets and media are blocked to keep
bandwidth down, and each tab pauses between requests. Keep the concurrency
modest — the site's own backend is the bottleneck, and pushing it harder makes
the data worse, not the run faster.
