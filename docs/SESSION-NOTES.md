# Session notes — building the medononline.com scraper

Recorded 2026-07-26. This is the reasoning and evidence behind `scrape_medon.py`,
kept so the next person (or the next Claude session, on any machine) does not
have to re-derive it. Everything below was measured against the live site.

---

## 1. The target

**medononline.com** — Medon Pharmacy, a MOHAP-licensed UAE online pharmacy
(licence Q4G78781-210426), operating in AED across all seven emirates. The
storefront is a **Next.js app** (React Server Component payload in `self.__next_f`;
no `__NEXT_DATA__`).

### Permission to scrape

`robots.txt` is permissive:

```
User-Agent: *
Allow: /
Disallow: /api/
Disallow: /order-success/
Sitemap: https://medononline.com/sitemap.xml
```

The site also publishes an `llms.txt` whose AI-usage policy explicitly allows
public catalogue content to be cited and summarised.

**The scraper therefore only visits public product pages listed in the sitemap
and never calls `/api/` directly**, even though the browser itself does so while
rendering. Keep it that way.

### Scale

`sitemap.xml` is 3.6 MB, 10,718 `<loc>` entries, of which **10,188 are
`/product/` URLs**. Product URLs are `/product/<slug>-<numeric-id>` — the numeric
suffix is required; guessing a slug without it returns a 200 soft-404 with no
Product JSON-LD.

---

## 2. Where the data actually lives

### Server-rendered: JSON-LD (reliable)

Every product page ships ~10 `application/ld+json` blocks with types
`Organization`, `WebSite`, `Pharmacy`, `Product`, `BreadcrumbList`. The `Product`
block is the authoritative source for the fields we care about:

```json
{
  "@type": "Product",
  "name": "Panda Strong 1000Mg 12S",
  "sku": "22253708", "mpn": "22253708",
  "image": ["https://medononline.fra1.cdn.digitaloceanspaces.com/product/…webp"],
  "offers": {
    "priceCurrency": "AED",
    "price": "11.00",
    "availability": "https://schema.org/InStock"
  }
}
```

Gotchas found:

- **At least one JSON-LD block is empty/whitespace** and throws on `json.loads`.
  Parse every block defensively and skip failures.
- `availability` is only ever `InStock` or `OutOfStock` in practice, but the code
  maps the full schema.org vocabulary in case that changes.
- **Out-of-stock products still carry a price.** Do not treat a missing price as
  the out-of-stock signal.
- The `BreadcrumbList` JSON-LD is useless for categories — it only ever contains
  `Home > Products > <product name>`. The real category chain is DOM-only.

### Client-rendered: everything else

Brand, category, the struck-through original price, stock chips, rating and
expiry only exist after hydration. Server-side rendering is *streamed and
inconsistent* — a clearance page's raw HTML contained `38.00`, `Limited Stock`
and `Baby Diapers`, while a plain product's raw HTML contained none of its
category. So raw HTML cannot be relied on for these; the browser is required.

---

## 3. Site behaviours that shaped the code

Each of these cost a debugging round. They are the reason the extraction code
looks fussier than it "should".

### 3.1 The breadcrumb loads one level at a time

Measured by polling crumb count every 500 ms:

| Time | Panda Strong | Abrir Diapers |
| --- | --- | --- |
| 500 ms | — | `Mother & Baby > Baby Diapering` |
| 1000 ms | — | `… > Baby Diapers` (final) |
| 1500 ms | `Medicine` | |
| 2500 ms | `… > OTC (Over the Counter)` | |
| 3500 ms | `… > Pain` (final) | |

Each level is a separate API fetch landing ~1 s after the previous one. There is
no "breadcrumb complete" event. `BREADCRUMB_READY_JS` therefore waits for the
crumb count to **hold steady across 8 polls at 250 ms (2 s)** — a shorter window
fires in the gap between levels and silently truncates the chain. An early
version used 750 ms and produced categories that were 1 level deep instead of 3,
which looks like valid data and is easy to miss.

### 3.2 A visibility wait hangs forever

The breadcrumb renders **both a mobile and a desktop copy, and the first is
hidden**. `wait_for_selector(...)` defaults to `state="visible"`, resolves to the
hidden copy, and burns the full timeout on *every* page — 12 s each, while the
data had actually arrived in ~2 s. Use `state="attached"` or, better,
`wait_for_function`, which polls the DOM directly.

### 3.3 The chain is rendered 4× — dedupe it

The breadcrumb nav contains 4 copies of the same `/category/` links (12 links for
a 3-level chain). Dedupe by label, preserving order.

### 3.4 The header mega-menu also links to `/category/`

Selecting "the nav with the most `/category/` links" picks the site-wide
mega-menu and yields nonsense like `Medicine > Medicine > Medicine`. **Scope to
`main nav`** — the breadcrumb is inside `<main>`, the mega-menu is not.

### 3.5 Uncategorised products exist

Some products (e.g. Tobrex Eye Drops) render `All Categories > <product>` with
**zero** `/category/` links. A blank category is correct data for these, not a
bug. The readiness check has to distinguish "genuinely uncategorised" from "not
loaded yet", which it does with a minimum elapsed time (6 s) before accepting a
settled zero. Setting that bound too low (2.5 s) blanks the category for
*everything* under concurrency.

### 3.6 Brand links are `/brands/`, not `/brand/`

Plural. A `href*="/brand/"` selector silently matches nothing. There are usually
two brand anchors, one with empty text (a logo link) — take the first with text,
and strip the trailing `Explore all the Products` that shares the anchor.

### 3.7 Stock chips render with no separator

Adjacent chips produce the literal text `In StockLimited Stock`, so a
`\b(In Stock|…)\b` regex **fails** — there is no word boundary between `Stock`
and `Limited`. Collect all matches with a global regex instead. The scraper
reports e.g. `In Stock, Limited Stock`.

### 3.8 The flash-sale banner sits inside the product grid

The right-hand sidebar carries a site-wide `FLASH SALE — Up to 40% off` promo,
*inside* the same grid container as the product info. A naive `(\d+)% OFF` match
tagged every single product as 40% off. The fix is to skip matches preceded by
"up to". Related-product cards further down the page carry their own prices and
`EXP` dates too, hence scoping discount/expiry extraction to the product grid
and the strike-through price to the product column.

The product column is located by finding the `SKU` text node and climbing to the
ancestor whose class contains both `min-w-0` and `flex-col`.

### 3.9 A discount is only trusted when it is arithmetically real

`build_product()` accepts the struck-through figure only if it is strictly
greater than the current price, then derives the percentage itself rather than
trusting the badge. Verified: 6,714 discounted rows, **0 inconsistent**.

---

## 4. Performance, and why concurrency is not the answer

The site's own API is the bottleneck. More tabs means slower hydration per page,
which means the category wait times out more often — **throughput up, data
quality down**:

| Mode | Concurrency | Speed | Category fill |
| --- | --- | --- | --- |
| Rich | 6 | 2.9 s/product (~8 h) | **40/40** |
| Rich | 12 | 1.7 s/product (~4.8 h) | 45/60 |
| Fast | 10 | 0.5 s/product (~1.5 h) | 0 (by design) |

**`networkidle` was tried and rejected.** The site holds persistent connections,
so it rarely fires: 12 of 30 pages timed out and the projection was 12.9 h.

Blocking `image`, `media`, `font` and `stylesheet` resource types is safe —
extraction is text/DOM based and never depends on layout or computed style.

Sustained load also degrades: the production run started at 3.8 pages/s and
settled to ~1.3 pages/s over two hours. Budget accordingly.

---

## 5. The production run

`python scrape_medon.py --fast --concurrency 10 --delay 0.15`, 2026-07-26,
**127 minutes, 10,188 products, 0 failures.**

| Metric | Value |
| --- | --- |
| In stock | 8,629 |
| Out of stock | 1,559 |
| Discounted | 6,714 (avg 17.4%, range 2–80%) |
| Price range | AED 0.40 – 1,734 (avg 69.53) |

Field coverage: price, availability, name, SKU, currency, image, URL — 10,188/10,188.
Stock label and rating — 10,187. Brand — 6,940. Original price — 6,714 (only
discounted products have one). No duplicate URLs.

The 80% maximum discount matches the site's own "up to 80% off" claim, and
spot-checked rows (Abrir clearance diapers at 19.00 from 38.00) match the live
pages — two independent signals that the discount logic is right.

### Known limitation

**Category is blank in this dataset**, because the run used `--fast`. Filling it
means a rich-mode run at concurrency ~6, which takes 8h+; `--resume` makes that
restartable. This was a deliberate trade: the request was for prices and
availability, and those are complete and reliable.

---

## 6. Environment notes (this VPS)

- Python 3.12.3; `playwright` 1.61.0 and `openpyxl` installed system-wide with
  `pip --break-system-packages`.
- Chromium was already cached at `/root/.cache/ms-playwright/chromium-1228`, so
  `playwright install` was not needed here. It **is** needed on a fresh machine.
- Chromium needs `--no-sandbox` as root, which the script passes.
- Two self-inflicted shell traps worth remembering: `pgrep -f "scrape_medon.py --fast"`
  in a waiter loop **matches the waiter's own command line**, so the loop never
  exits; and `pkill -f "until ! pgrep"` matches and kills its own shell.
