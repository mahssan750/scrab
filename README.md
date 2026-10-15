# UAE Pharmacy Catalogue Intelligence Pipeline

**A Python workflow that collected a documented snapshot of 10,188 public pharmacy product pages**

This project converts public product-page information into a structured Excel workbook for reviewing prices, availability and catalogue coverage at scale.

## Recruiter quick view

| Area | Result |
| --- | --- |
| **Business goal** | Replace manual product checks with one organized catalogue dataset. |
| **Scope** | 10,188 distinct UAE pharmacy product URLs. |
| **Core coverage** | Price, currency and availability recorded for every row in the committed snapshot. |
| **Output** | A filterable Products sheet plus a management Summary sheet. |
| **Reliability** | Retries, resume support and streaming results prevent lost progress during long runs. |

## Why this matters

Reviewing thousands of products manually is slow and inconsistent. A structured catalogue can help commercial and e-commerce teams compare assortment, identify availability gaps and prepare pricing analysis.

## Verified historical snapshot

A file-level review of the committed workbook on 27 September 2026 found:

| Check | Result |
| --- | ---: |
| Product rows | **10,188** |
| Distinct product URLs | **10,188** |
| Missing prices | **0** |
| Currency | AED on all rows |
| In-stock labels | **8,629** |
| Out-of-stock labels | **1,559** |
| Missing brand | 3,248 rows (31.88%) |
| Missing category | 9,704 rows (95.25%) |

## What the workbook contains

The Products sheet provides one row per product with fields such as:

- Product name and SKU
- Current and original price
- Calculated discount percentage
- Shopper-facing availability and stock labels
- Brand and category when available
- Rating, review count and expiry information when shown
- Product and image URLs
- Collection timestamp

The Summary sheet provides a quick overview of row counts, price range and availability.

## How the pipeline works

1. Read product URLs from the site's public sitemap.
2. Visit each public product page with headless Chromium.
3. Collect core product, price and availability details.
4. Save every completed row immediately to a streaming file.
5. Resume safely after interruption without repeating finished products.
6. Build a formatted Excel workbook for analysis.

## Reliability choices

- Multiple attempts for temporary page failures
- Resume mode for long collection runs
- Modest concurrency and request delays
- Images, fonts and other heavy assets blocked to reduce bandwidth
- Separate fast and rich modes for speed versus additional product details

## Business uses

With suitable quality checks and permission to refresh the data, this type of dataset can support:

- Assortment and availability reviews
- Product-level price comparisons
- Discount monitoring
- Catalogue completeness checks
- Prioritizing products that need manual investigation

## Important limitations

- The committed workbook is a **historical snapshot**, not a live stock feed.
- Availability is the customer-facing website label, not warehouse stock quantity.
- Brand coverage is incomplete and category coverage is very limited.
- The data does not show sales demand, profit, commercial savings or competitor performance.
- The existing workbook was reviewed without rerunning a live collection.
- Site terms and crawling policy should be reviewed before any new collection.

These limits are documented so the output is not presented as more complete or current than it is.

## Run locally

Install the requirements and the Chromium browser, then choose a small sample before a full run:

    pip install -r requirements.txt
    playwright install chromium
    python scrape_medon.py --limit 50

Use **--fast** for core fields, **--resume** to continue an interrupted run and **--output** to choose the Excel file.

## What this project demonstrates

- Python automation for large, repetitive data tasks
- Web data extraction with error handling
- Data-quality validation and transparent limitations
- Streaming and resumable processing
- Excel output designed for non-technical users

## Tools demonstrated

**Python · Playwright · JSON-LD · Pandas · Excel · Data Quality · Automation**
