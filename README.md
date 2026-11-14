# NJ Distress Map

A website with an interactive satellite map of New Jersey showing every commercial property
on a county sheriff sale list. It updates itself every morning:

- **New listings and changes pulse on the map** at the property's location, with a label
  (New listing, Adjourned, New sale date, Upset changed, Bankruptcy hold, Sold, Cancelled).
- **Alerts panel** lists the last 7 days of changes; a red dot marks anything new since your last visit.
- **Click any property** to fly to it and open its file: upset price, judgment, assessed value and
  debt-to-value, sale date with countdown, owner, lender, foreclosure attorney, docket number,
  tax-record details (class, building, acreage, year built, last sale, taxes), full status history,
  the parcel boundary drawn on the imagery, and links to the sheriff listing, Google Maps and Street View.
- **Bank-owned (REO)**: commercial properties the foreclosing lender bought back at sheriff sale in
  the past 12 months show as purple diamonds, with the date, the lender that now owns it and the
  former owner. New buy-backs appear as alerts.
- **Filters**: property type (commercial, industrial, apartments, land, needs review), county,
  upcoming sale window, search by address/owner/lender/docket, and properties that came off the list.

## How it works

1. **Sheriff sale lists**: every morning a scheduled job reads the open list for each county on
   CivilView: Atlantic, Bergen, Burlington, Camden, Cape May, Cumberland, Essex, Gloucester, Hudson,
   Hunterdon, Middlesex, Monmouth, Morris, Passaic, Salem, Union.
   *Not yet covered:* Mercer, Ocean, Somerset, Sussex, Warren (Phase 2).
2. **Detail pages**: for each new listing it reads the docket number, lender, owner, judgment, upset
   price, attorney, legal description and status history.
3. **Tax records**: it takes the block/lot from the legal description and matches it to the state's
   MOD-IV tax data (NJOGIS) for property class, assessed value, building details and the parcel's
   map location. Houses and condos are dropped; vacant land (1), commercial (4A), industrial (4B) and
   apartments (4C) are kept. Unmatched listings owned by an LLC/corp show as "Needs review" and are
   placed by address. *Owner names are redacted in state data (Daniel's Law), so "Owner" is the
   foreclosure defendant.*
4. **Bank-owned (REO)**: it also reads each county's "Sold/Cancelled" list (about the past year)
   and opens each sale to see who bought it. Sales marked as bought by the plaintiff ("Purchased -
   Plaintiff", "Plaintiff Buy Back", "Sold to Plaintiff", "Assignment of Bid", ...) are added to the map
   as REO. The first pass covers ~4,000 past sales, so it is spread over the first few daily runs
   (1,200 a day); after that only new sales are checked.
5. **Changes**: on later runs it re-reads only tracked properties, records adjournments, new sale dates,
   upset changes and removals, then republishes the website.

The first run reads every listing (~1,600) and takes about an hour. Daily runs take a few minutes.

## Setup (about 15 minutes)

1. **Create a GitHub repository.** Sign in at github.com, create a new repository
   (e.g. `nj-distress-map`), and upload all of these files. Make sure the hidden `.github`
   folder is included; it holds the daily schedule.
   - *Public vs. private:* GitHub Pages is free for public repositories. A private repository
     needs a paid GitHub plan to publish a site. Either way the site address works without a
     login, so treat the link as unlisted rather than secret (search engines are told not to index it).
2. **Turn on the website.** In the repository: **Settings → Pages → Build and deployment → Source:
   GitHub Actions**.
3. **Add your contact email** (sent to the county sites as a courtesy): **Settings → Secrets and
   variables → Actions → New repository secret**, name `CONTACT_EMAIL`.
4. **Run it.** Go to **Actions → NJ distress tracker → Run workflow**. When it finishes, the site
   address appears on the run's "publish" step and under Settings → Pages
   (usually `https://<your-username>.github.io/nj-distress-map/`).

After that it runs every morning around 6:45 AM ET and republishes the map.

## Optional: daily email digest
Set `email.enabled: true` in `config.yaml` and add these secrets: `SMTP_HOST` (`smtp.gmail.com`),
`SMTP_PORT` (`587`), `SMTP_USER`, `SMTP_PASSWORD` (a Gmail App Password: turn on 2-Step
Verification, then create one under "App passwords"), and `ALERT_TO` (comma-separated recipients).

## Settings: `config.yaml`
- `counties`: which counties to scan
- `target_classes`: which tax classes count (default 1, 4A, 4B, 4C)
- `min_assessed_value`: ignore smaller properties
- `include_possible`: include unmatched LLC/corp-owned listings as "Needs review"
- `reo_enabled`, `reo_lookback_days`, `max_sold_details_per_run`: the bank-owned (REO) layer

## If the county sites block GitHub
If the site footer says counties "could not be read" day after day, run the scan from your own PC:
install Python from python.org, copy `.env.example` to `.env`, and schedule `run_local.bat` in Windows
Task Scheduler. (Publishing from a PC run needs a `git push` afterwards; ask and this can be scripted.)

## Files
- `site/index.html`: the map website (one file; opens with sample data if no live data is present)
- `data/map.json`: what the map reads, rewritten every run
- `data/leads.csv`: open commercial leads as a spreadsheet
- `data/reo.csv`: bank-owned (REO) commercial properties as a spreadsheet
- `data/tracker.db`: the full history (SQLite)

## Testing locally
```
pip install -r requirements.txt
python -m pytest -q
python run.py --counties 10 --max-new 20      # quick Hudson-only trial
python -m http.server 8000                    # then open http://localhost:8000/site/
```
(For a local preview with live data, copy `data/map.json` into `site/data/`.)

## Limits
- Figures come from county sheriff listings and state tax data; verify before relying on them.
- Assessed values are tax assessments, not market values.
- Satellite imagery: Esri World Imagery. Street map: OpenStreetMap/CARTO.

## Roadmap
- **Phase 2:** notes, status and assignments per property for the team (CRM); Mercer, Ocean,
  Somerset, Sussex and Warren sheriff lists
- **Phase 3:** lis pendens from county clerks; mortgage/assignment/complaint pulls with AI extraction
  and a written story of each property
