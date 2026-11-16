# Forever-AH

Price history and deal-spotting for the World of Warcraft: Forever auction
house. Built for the Horde side of the EU PvP ruleset, plus the neutral
Blackwater house.

**Dashboard:** https://ludo96.github.io/Forever-AH/

It runs entirely on GitHub's free tier. A scheduled job pulls the current
listings from Blizzard's API once an hour, boils them down to a few numbers per
item, and publishes a static dashboard. There is no server and no database.

## What the dashboard shows

- **Deals**: items listed well below their usual low for the past 7 days, with
  the margin left if you relist at the usual market price.
- **Vendor flips**: items listed for less than a vendor pays.
- **All items**: everything on sale, with the lowest price, market price,
  24-hour change, 7-day range and units on sale.
- **Watchlist**: items you star. Saved in your own browser, so each friend
  keeps their own list.
- **Item view**: price and supply charts over 24 hours, 7 days, 30 days or all
  time. The page address updates as you browse, so any view can be shared as a
  link.

## Setup

1. **Blizzard API client.** Create one at <https://develop.battle.net/access/clients>.
   The redirect URL can be `https://localhost`; it is not used.
2. **Repository secrets.** In this repository: Settings → Secrets and
   variables → Actions → New repository secret. Add `BLIZZARD_CLIENT_ID` and
   `BLIZZARD_CLIENT_SECRET`.
3. **First run.** Actions tab → Collect auction prices → Run workflow. It
   takes a minute or two and creates a `gh-pages` branch.
4. **Turn on the site.** Settings → Pages → Source: *Deploy from a branch* →
   Branch: `gh-pages`, folder `/ (root)` → Save.

After that it looks after itself. If a run fails, GitHub emails the repository
owner, and the dashboard shows a warning once prices are more than three hours
old.

## Things to know

- **Asking prices, not sales.** Blizzard only publishes current listings.
  Nothing here knows what actually sold.
- **Deals need history.** An item needs 24 hourly snapshots before it can
  appear under Deals.
- **The API is undocumented.** Forever's endpoints were worked out by the
  community and may change without notice. The collector reads every response
  defensively and fails loudly when something essential is missing.
- **Which auction house is yours.** Blizzard currently lists every house under
  every ruleset. The collector keeps the one that names ruleset 1 (EU PvP). If
  the houses do not say, it tracks all Horde and Blackwater candidates and the
  dashboard shows them side by side; compare a price you can see in game, then
  pin the right ids in `collector/config.json`:

  ```json
  "houses": [116, 120]
  ```

- **Item names arrive gradually.** Forever's item data is missing a large share
  of auctionable items. The collector falls back to Classic Era data, looks up
  at most 1,500 new items per run, and retries unknown ones every few days.
- **Random-suffix items share an id.** "of the Tiger" and "of the Bear"
  versions of the same base item are counted together.
- **Auction cut.** Resale margins assume a 5% cut (15% at Blackwater), set in
  `collector/config.json`.

## Configuration

`collector/config.json`:

| Key | Meaning |
| --- | --- |
| `region` | `eu`, `us`, `kr` or `tw` |
| `ruleset` | Ruleset id. On EU and US: 1 PvP, 2 Normal, 3 Roleplay, 15 Roleplay PvP |
| `factions` | Which houses to track in automatic mode: `horde`, `alliance`, `blackwater` |
| `houses` | `"auto"`, or a list of house ids to pin |
| `auction_cut` | Share of the sale price the auction house keeps, per faction |

## How the data is stored

Everything lives on the `gh-pages` branch under `data/`, as small JSON files:

| File | Contents |
| --- | --- |
| `meta.json` | Tracked houses and last update |
| `items.json` | Item names, quality, type, vendor price |
| `<house>/summary.json` | One row per item listed now, with 7- and 30-day ranges |
| `<house>/h/<date>/<shard>.json` | Hourly points, kept 30 days |
| `<house>/d/<month>/<shard>.json` | Daily points, kept indefinitely |

History is split into 64 shards by item id, so viewing an item downloads a few
kilobytes. The branch is replaced with a single commit each hour, which keeps
the repository from growing with every snapshot.

## Testing

```
python3 tests/simulate.py
```

runs the collector against a fake API shaped like the real one and checks the
results. To preview the dashboard with simulated data:

```
python3 tests/simulate.py --out public
cp -r site/. public/
python3 -m http.server --directory public
```

## Credits

Charts by [uPlot](https://github.com/leeoniya/uPlot) (MIT). Typeface: Alegreya
and Alegreya Sans by Huerta Tipográfica (SIL Open Font License). Game data
from Blizzard's Battle.net API. Not affiliated with or endorsed by Blizzard
Entertainment.
