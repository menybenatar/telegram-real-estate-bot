# Telegram real-estate alert bot (Yad2 → Telegram, via GitHub Actions)

Every 6 hours a GitHub Actions job searches for apartments for sale that match `config.yaml`, drops
everything you've already been told about, and sends the new ones to your Telegram chat (photo, price, rooms,
size, link). No server to run; history is kept in a small SQLite file on a dedicated git branch.

> ## ⚠️ Read this first: Yad2 access is **not verified**
> This project was built in an environment with **no internet access**, so the Yad2 endpoint, query
> parameters and JSON field names in `src/real_estate_bot/search/yad2.py` come from general knowledge of
> Yad2's public feed and have **never been tested against the live site**. Everything else (filters,
> deduplication, storage, Telegram, workflow, tests) is tested with mocked data.
>
> Before relying on it, run **`--probe`** (see [step 8](#8-trigger-a-manual-search)). It makes one request
> and tells you exactly what happened: robots.txt result, HTTP status, whether Yad2 served a bot-protection
> page, which fields were found. If Yad2 blocks the request the bot **fails loudly and does not try to get
> around it** (no CAPTCHA solving, no spoofed browser, no proxies). See [Known limitations](#12-known-limitations).

## Contents
1. [Prerequisites](#1-prerequisites) · 2. [Create the bot](#2-create-a-telegram-bot-botfather) · 3. [Chat ID](#3-get-your-chat-id) ·
4. [Configure searches](#4-configure-the-search-criteria) · 5. [Secrets](#5-github-secrets) · 6. [Enable Actions](#6-enable-github-actions) ·
7. [Run locally](#7-run-locally) · 8. [Manual run](#8-trigger-a-manual-search) · 9. [Verify](#9-verify-notifications-work) ·
10. [Persistence](#10-how-persistence-works) · 11. [Change criteria](#11-change-criteria--add-cities) · 12. [Limitations](#12-known-limitations) · 13. [Costs](#13-costs)

## 1. Prerequisites
- A GitHub account and a **private** repository (recommended - the state branch contains listing titles/URLs, see §10).
- A Telegram account.
- Python 3.12+ only for local runs/tests (GitHub runs it for you).

## 2. Create a Telegram bot (BotFather)
1. In Telegram open **@BotFather** → send `/newbot` → choose a name and a username ending in `bot`.
2. BotFather replies with a token like `123456789:AAH...`. Treat it like a password.
3. Open your new bot and press **Start** (a bot can't message you until you do).

## 3. Get your chat ID
1. Send any message to your bot.
2. Open the updates list (replace `<TOKEN>`):

   **Linux/macOS**
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   **Windows PowerShell**
   ```powershell
   Invoke-RestMethod "https://api.telegram.org/bot<TOKEN>/getUpdates" | ConvertTo-Json -Depth 6
   ```
3. Find `"chat": {"id": 123456789, ...}` - that number is your `TELEGRAM_CHAT_ID`. (For a group: add the bot
   to the group, post a message, repeat; group IDs are negative.)

## 4. Configure the search criteria
Edit `config.yaml` - no code changes needed. Each entry under `searches:` is one search; set `enabled: false`
to pause one, delete the block to remove it. Unknown/misspelled keys are rejected at startup with a clear message.

| Key | Meaning |
|---|---|
| `city` | City name in Hebrew, as Yad2 shows it |
| `city_code` | Optional Yad2 city number; built in for Holon, Bat Yam, Tel Aviv, Rishon, Ramat Gan, Petah Tikva, Netanya, Ashdod, Haifa, Jerusalem |
| `property_type` | `apartment` (or a list: `[apartment, garden_apartment]`; also `house`, `penthouse`, `duplex`) |
| `max_price_ils` / `min_price_ils` | Price bounds in ILS |
| `min/max_rooms`, `min/max_size_sqm` | Optional bounds. If set and the listing doesn't state the value, the listing is **rejected** |
| `require_image`, `require_price` | Reject listings without a usable image / a valid price |
| `exclude_brokers` | Drop brokered listings (see below) |
| `broker_uncertain` | Seller type unknown: `mark` (send with a ⚠️ note, default), `include`, or `exclude` |
| `max_pages` | Result pages inspected per run (default 3, max 20) |
| `extra_params` | Raw query parameters passed to the source (e.g. sorting) |

Global `settings:` (language `en`/`he`, per-run Telegram cap, retention, retry counts, delays, robots.txt) are
documented in `config.yaml`. Check your file any time: `python -m real_estate_bot.main --validate-config`.

**How "no broker" works** (and why it can't be perfect):
1. A structured seller field from the source is used when present (`merchant`/agency flags → broker).
2. Otherwise the title/description is scanned. Phrases like "ללא תיווך" / "בלי תיווך" / "אין צורך במתווך" are
   removed *first*, so they are not mistaken for a broker mention; any broker term left over
   (תיווך, מתווך, מתווכת, משרד תיווך, סוכנות נדל"ן, עמלת תיווך) marks the listing as brokered.
3. No evidence either way = **uncertain** (handled per `broker_uncertain`).

Keyword filtering **cannot prove** a seller is private: brokers sometimes don't write that they are brokers,
and owners sometimes write the words. Treat the ⚠️ note as "check it yourself".

## 5. GitHub Secrets
Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | the number from step 3 |

No database credentials are needed - persistence uses the repository itself (§10). Secrets are never written to
code or logs: log output is redacted for the token and anything shaped like one.

## 6. Enable GitHub Actions
1. Push this project to your repo (the default branch must contain `.github/workflows/monitor.yml`).
2. **Settings → Actions → General**: allow Actions, and under *Workflow permissions* choose **Read and write permissions**
   (needed to push the `state` branch).
3. **Actions** tab → *Monitor listings* → enable it if GitHub asks.

The schedule is `0 */6 * * *` = 00:00, 06:00, 12:00, 18:00 **UTC**:

| | UTC | Israel winter (UTC+2) | Israel summer (UTC+3, DST) |
|---|---|---|---|
| runs at | 00 / 06 / 12 / 18 | 02 / 08 / 14 / 20 | 03 / 09 / 15 / 21 |

GitHub cron is **not exact**: runs are commonly delayed by minutes (sometimes much more) under load, and
occasionally skipped. Also, GitHub disables scheduled workflows in repositories with no activity for 60 days -
if alerts stop, open the Actions tab and re-enable the workflow.

## 7. Run locally
**Linux/macOS**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then edit .env with your token and chat id
export PYTHONPATH=src
python -m real_estate_bot.main --validate-config
pytest                           # all tests use mocks; no network, no real Telegram messages
```
**Windows PowerShell**
```powershell
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env      # then edit .env
$env:PYTHONPATH = "src"
python -m real_estate_bot.main --validate-config
pytest
```
**Dry run with demo data** (prints the Telegram messages instead of sending; nothing leaves your machine):
```bash
python -m real_estate_bot.main --dry-run --provider mock --mock-file examples/mock_listings.json
python -m real_estate_bot.main --dry-run --provider mock --mock-file examples/mock_listings.json   # 2nd run: nothing sent
```
Dry runs use `dry_run.db` (git-ignored); delete it to start over. Useful flags: `--db PATH`, `--config PATH`, `LOG_LEVEL=DEBUG`, `LOG_FORMAT=json`.

## 8. Trigger a manual search
**Actions → Monitor listings → Run workflow**, pick a mode:
- **probe** - *do this first.* One request to Yad2, prints a JSON diagnostic report in the job log. Nothing is sent or saved.
- **baseline** - records everything that matches *right now* as already seen and sends nothing. Run it once before the first
  real run, otherwise the first run announces every existing match (capped at `max_notifications_per_run`, the rest follow on later runs).
- **run** - the normal run.

Locally the same modes are `--probe`, `--baseline` and no flag.

## 9. Verify notifications work
1. Quick credential test without touching Yad2:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" -d chat_id=<CHAT_ID> -d text="test"
   ```
   ```powershell
   Invoke-RestMethod "https://api.telegram.org/bot<TOKEN>/sendMessage" -Method Post -Body @{chat_id="<CHAT_ID>"; text="test"}
   ```
2. Run the workflow in **probe** mode; read the log - `verdict` should start with `OK`.
3. Run **run** mode. In the log, each search prints `fetched / matched / rejected={reason: count}` - if `matched=0` the
   rejection reasons tell you which criterion is filtering everything out (e.g. `missing_property_type` would mean the field mapping needs fixing).
4. A failed run turns the workflow red and (if `notify_on_error: true`) sends you one Telegram alert, at most once per 24 h per distinct problem.

Message example (fields that are unavailable are simply omitted):
```
🏠 New apartment for sale
<title>

📍 Holon — <neighborhood>
💰 Price: ₪1,690,000
🛏️ Rooms: 3.5
📐 Size: 82 m²
🆕 Published: 14/10/2026

🔗 View listing: https://www.yad2.co.il/...
```
If Telegram can't fetch the photo, the listing is sent as text with the link and the photo error is recorded. Delivery is
*at-least-once*: in the rare case a run is killed after Telegram accepted a message but before it was recorded, that listing can be sent again.

## 10. How persistence works
GitHub runners are wiped after every run, so history must live elsewhere. This project stores a SQLite file
(`listings.db`) on a dedicated git branch named **`state`** in the same repository:

1. Start of run: the `state` branch is checked out into `state/` (created automatically on the first run).
2. The bot reads/writes `state/listings.db` using SQLite transactions - a listing is marked *sent* only after Telegram accepted it;
   failed sends stay *pending* and are retried on later runs (up to `max_send_attempts`, then parked as *failed*).
3. End of run (`if: always()`, so even failed runs save what they already announced): the file is committed to `state`.
   The branch always holds a **single** commit (force-pushed), so the repo doesn't grow with a database revision every 6 hours.
4. A workflow-level `concurrency` group allows **one run at a time** (a second trigger waits). That is what makes this safe;
   don't remove it, and don't run the bot locally against the same database while Actions is active.

Listings are identified by Yad2's own listing id (so a changed URL isn't a new listing) and forgotten after `retention_days`
without being seen. Because the branch contains listing titles/prices/URLs, **use a private repository**.

Alternatives if you outgrow this: any free hosted database (Turso, Supabase, Neon) by replacing `storage.py` - the interface is small.
(Actions artifacts and caches were deliberately *not* used: they have no transactions or concurrency control.)

## 11. Change criteria / add cities
Edit `config.yaml` and commit. To add a city, copy a search block and change `city` (and `max_price_ils` etc.). If the city isn't in the
built-in table the run fails that search with a message asking for `city_code:` - find the number in the URL when you filter by that city on yad2.co.il.
A listing matching several searches is sent once.

## 12. Known limitations
- **Yad2 integration is unverified** (see top). Field names may differ; `--probe` shows what's actually there.
- **Bot protection.** Yad2 may serve CAPTCHA/bot-protection pages or HTTP 403 to automated clients, and GitHub's datacenter IP
  ranges are the kind that get blocked. If that happens the job fails with `ProviderBlockedError`. The bot will not work around it.
  Options: (a) use Yad2's own saved-search alerts (built into the site/app), (b) an official or licensed data source - implement
  `ListingProvider` in `src/real_estate_bot/search/base.py` and register it in `main.build_provider`, (c) run it from your own machine/network
  if that is permitted for you.
- **Terms of use.** I could not read Yad2's terms from the build environment. Automated collection may be restricted by them -
  check before running regularly. The bot honors robots.txt for the API host, identifies itself honestly and keeps volume low (≤ `max_pages` requests per search per run).
- **Recency.** Newest-first ordering isn't verified; results are sorted by publication date client-side, but only the first `max_pages` pages are inspected.
  If new listings appear beyond that window between runs they're missed - raise `max_pages` or pass a sort parameter via `extra_params`.
- Publication dates are shown only when the source provides a real publish date (not "last updated").
- Broker detection is heuristic (§4). Property type, city, rooms and size are matched on what the listing states; absent data means rejection.

## 13. Costs
- GitHub Actions: ~1 minute per run × 4 runs/day ≈ 120–150 minutes/month - inside the free tier (2,000 min/month for private repos; unlimited for public).
- Telegram Bot API: free. Storage: the repo itself. External services: none.

## Project layout
```
.github/workflows/monitor.yml   schedule (every 6h UTC), manual modes, state branch handling
config.yaml                     searches and settings
src/real_estate_bot/
  main.py                       orchestration, CLI, exit codes (0 ok, 1 critical failure, 2 bad config)
  config.py                     YAML loading + validation
  models.py                     Listing
  search/{base,yad2,mock}.py    provider interface, Yad2 provider (unverified), demo provider
  filters.py                    price/image/city/type/rooms/size/broker rules
  deduplication.py, storage.py  in-run and cross-run dedup, SQLite history
  telegram_notifier.py          photo + text messages, retries, rate-limit handling
  logging_config.py             structured logs with secret redaction
tests/                          mocked tests (no network)
examples/mock_listings.json     demo data for dry runs
```
