# Merchant Intelligent Scraper

A daily scraper that walks through Glovo's store listings in major Spanish cities, pulls out the relevant data for each restaurant (promotions, ratings, delivery type, open/closed status), and saves everything to CSV. It runs unattended through GitHub Actions every night and commits the results straight back to the repo.

## What it actually does

Glovo splits each city into a "primary" food category page plus a bunch of sub-lists (burgers, sushi, breakfast, etc.). The scraper:

1. Loads the primary listing page for a city.
2. Discovers every other category sub-list linked from that page and crawls those too, so it isn't limited to whatever shows up first.
3. Deduplicates store cards by link, since the same restaurant often shows up in several lists.
4. Before writing anything, it checks a sample of stores to see whether the city is actually open right now — if everything sampled is closed, it skips the city entirely instead of polluting the dataset with stale entries.
5. Walks through the remaining stores in order and keeps going until it finds **5 stores that use Glovo's own delivery fleet** (as opposed to self-delivery), checking each one's live page for its delivery type and open status.
6. Appends the result to a daily CSV — one file per day, new rows get added to the same file if the workflow runs more than once.

The whole thing is built to be polite about it: random pauses between requests, retry/backoff on failed requests, and a strict time window so it only runs late at night.

## Cities covered

```
Valencia · Málaga · Alicante · Sevilla · Madrid · Barcelona
Las Palmas de Gran Canaria · Tenerife (Santa Cruz / La Laguna)
Zaragoza · A Coruña · Granada · Palma · Albacete · Jerez de la Frontera
```

Add or remove cities by editing the `CITIES_LIST` constant in `scripts/g_script.py`.

## Output

Each run appends to `outputs/store_data_YYYY-MM-DD.csv`. Columns:

| Column | Description |
|---|---|
| Fecha de extracción | Date the row was scraped |
| Hora ejecución | Local time (Europe/Madrid) the row was scraped |
| Timestamp ejecución | Full timestamp with timezone |
| Ciudad | City slug |
| Nombre | Store name |
| Promoción (texto) | Raw promo text shown on the card, if any |
| Descuento normal (%) | Non-Prime discount, parsed from the promo text |
| Rating | Store rating shown on the card |
| Reviews Number | Number of reviews |
| Categoria | Reserved, currently unused |
| Link | Direct link to the store page |
| Prime | Whether the promo is a Glovo Prime perk |
| Descuento prime | Prime-only discount percentage, if found |
| Tipo de reparto | `Glovo` (platform delivery) or `Propio` (store's own delivery) — only `Glovo` rows get written |
| Estado | `Abierto` or `Cerrado` at scrape time |

A few sample CSVs from previous runs are already sitting in `outputs/` so you can see the shape of the data without running anything.

## Project structure

```
.
├── scripts/
│   └── g_script.py          # everything lives here: fetch, parse, dedupe, write
├── outputs/
│   └── store_data_*.csv     # one CSV per day
├── .github/workflows/
│   └── get_information.yml  # nightly automation
└── requirements.txt
```

## Running it locally

```bash
git clone https://github.com/oscar-ctrl/Merchant-Intelligent-Scraper.git
cd Merchant-Intelligent-Scraper
pip install -r requirements.txt
python scripts/g_script.py
```

By default the script only runs inside a fixed late-night window (00:00–03:00 Europe/Madrid), since that's when the original automation is scheduled. If you're testing locally outside that window, flip `FORCE_RUN` to `True` near the top of `g_script.py`.

## Automation

`.github/workflows/get_information.yml` installs dependencies, runs the script, uploads the CSV as a workflow artifact, and commits any new file straight back to `outputs/` using `github-actions[bot]`. The cron schedule is currently commented out — uncomment the lines you want (or trigger it manually via `workflow_dispatch`) to turn the nightly run back on.

## Notes

- This scrapes public store-listing pages and is rate-limited on purpose. Be reasonable if you tweak the pauses or the per-city store count.
- Selectors are matched against Glovo's current frontend class names (`StoreCardStoreWall_*`, `StorePromotion_*`, etc.). If Glovo ships a redesign, the CSS-class prefixes in `scripts/g_script.py` are the first thing to check.
- No license file yet — add one if you plan on sharing or open-sourcing this further.
