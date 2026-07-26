# Daily Shortlist — setup

An installable phone app that shows a ranked shortlist of Nifty 500 stocks each
morning, scored on fixed criteria you can inspect. Total cost: nothing.

**What it is:** a reproducible screener. **What it is not:** advice, or a buy list.
Rank 1 means "read this annual report first", not "buy this".

---

## Try it right now, on this laptop

Double-click `index.html`. A real ranking is already in `data.json` — generated from
130 live Nifty 500 names, so you can see exactly how it looks and works before setting
anything up.

To generate a fresh full-universe ranking yourself:

```
pip install -r requirements.txt
python screen.py
```

Takes about four minutes for all 500 names. Add `--limit 100` for a quick test, or
`--no-news` to skip the headline pass.

---

## Putting it on the phone

The app needs to be served over `https://` for the install prompt and offline caching
to work. The free route is GitHub Pages, which also gives you the daily automation in
the same step.

### 1. Create a free GitHub account and a new repository

Name it anything — say `daily-shortlist`. Make it **private** if you prefer; Pages works
on private repos on the free plan only for the owner, so if the install gives trouble,
make it public. Nothing here is sensitive: there are no API keys, no personal data, and
no holdings in this app.

### 2. Upload these files

Everything in this folder, keeping the structure:

```
index.html
data.json
manifest.webmanifest
sw.js
icon-192.png
icon-512.png
screen.py
requirements.txt
.github/workflows/daily.yml
```

The easiest way: on the repo page choose **Add file → Upload files**, then drag the
whole folder in. GitHub preserves the `.github/workflows` path.

### 3. Turn on GitHub Pages

Repository **Settings → Pages**. Under *Source* pick **Deploy from a branch**, branch
`main`, folder `/ (root)`. Save. After a minute or two your app is live at:

```
https://<your-username>.github.io/daily-shortlist/
```

### 4. Install it on the phone

Open that URL in Chrome on Android. Menu (⋮) → **Add to Home screen** → **Install**.
It gets its own icon and opens full-screen with no browser bars. On iPhone, Safari →
Share → Add to Home Screen.

### 5. Check the automation

Repository **Actions** tab → *Daily shortlist* → **Run workflow**. This runs the
screener immediately so you can confirm it works rather than waiting for tomorrow.
It takes a few minutes, then commits a fresh `data.json`.

After that it runs itself at 02:15 UTC (07:45 IST) Monday to Friday, on GitHub's
servers. Your laptop can be switched off.

---

## Keeping it running

- GitHub disables scheduled workflows in repositories with no activity for 60 days.
  It emails you first. Pushing any commit re-enables it.
- If the ranking stops updating, the app shows a warning banner telling you how many
  days stale it is. Check the Actions tab for a failed run.
- The workflow refuses to publish if it fetched fewer than 200 stocks, so a
  rate-limited run leaves yesterday's good data in place rather than overwriting it
  with a broken ranking.

---

## Changing how it ranks

Everything lives in `screen.py` and is meant to be edited.

**The weights** — near the bottom of `score()`:

```python
total = quality * 0.50 + value * 0.35 + growth * 0.15
```

**The hard filters** — `disqualify()`. Currently: profitable, market cap above
₹1,000 cr, D/E below 1.5x, P/E below 80, CFO/PAT above 0.4x, ROE above 10%,
and no lenders.

**The factor bands** — inside `score()`. `ramp(r["roe"], 10, 25)` means an ROE of 10%
scores zero on that factor and 25% or better scores full marks. Adjust the numbers to
change what "good" means.

**The news keywords** — `RED_FLAGS` removes a name outright; `WATCH_WORDS` only
annotates it. Add to these as you learn what actually matters.

Commit the change, and tomorrow's ranking uses your new rules.

---

## Known weaknesses — read this once

**The data.** Yahoo Finance is free and unofficial. It has gaps, occasional errors, and
inconsistent treatment of standalone versus consolidated figures. Every stock card links
to its financials and annual report; check anything before acting on it. If you later
want better data, the paid options are a market-data API subscription or Groww's
Trading API at ₹499/month — the scoring code stays the same, only `fetch_one()` changes.

**Value traps.** A screen tuned to "good and cheap" reliably finds businesses in slow
decline, because decline is exactly what makes a company look statistically cheap. This
is the central weakness of the approach and no amount of tuning removes it. It is why
the annual report step is not optional.

**Holding companies and commodity cyclicals** frequently top the list for reasons the
score misreads — a holding company's assets are shares in other companies, and a
commodity producer's peak-cycle earnings make it look permanently cheap. Check what a
company actually does before believing its rank.

**Small caps.** Anything under about ₹5,000 crore is flagged in the app. Thin trading
means wide spreads and real difficulty exiting.

**The score is 11 ratios.** It cannot see management honesty, accounting quality,
competitive threats, or litigation — the things that decide outcomes. It narrows 500
names to 15. You do the rest.

---

## Not advice

This is a personal research tool. It is not investment advice and makes no
recommendation to buy or sell anything. It is not produced by a SEBI-registered
investment adviser or research analyst. Don't redistribute its output — sharing stock
recommendations beyond immediate family is regulated activity in India.
