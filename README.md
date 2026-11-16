# Forever-AH

Price history and deal-spotting for the World of Warcraft: Forever auction
house. Built for the Horde side of the EU PvP ruleset, plus the neutral
Blackwater house.

**Dashboard:** https://ludo96.github.io/Forever-AH/

It runs entirely on GitHub's free tier. A scheduled job checks Blizzard's API every 20
minutes (Blizzard refreshes its listings about hourly), boils them down to a few numbers per
item, and publishes a static dashboard. There is no server and no database.

## What the dashboard shows

- **Deals**: items listed well below their usual low for the past 7 days, with
  the margin left if you relist at the usual market price.
- **Vendor flips**: items listed for less than a vendor pays.
- **Buy / hold / sell signal**: a meter on every item page and a sortable
  column, comparing today's lowest price with the item's past 7 days.
- **Volume**: estimated units and value sold in the past 24 hours, and how
  long the current stock would last at that pace.
- **Professions**: what each craft costs in materials, what the result sells
  for, and what is left after the auction cut. Open an item to see its recipe
  priced line by line, and its crafting cost charted against its price over
  time.
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

After that it looks after itself. The schedule asks GitHub for a run every 20
minutes, and as a backup each run also starts the next one 18 minutes after it
finishes, because GitHub's scheduler can be late or skip runs. If the chain
ever stops, start it again with Run workflow. If a run fails, GitHub emails the repository
owner, and the dashboard shows a warning once prices are more than three hours
old.

## Things to know

- **Asking prices, not sales.** Blizzard only publishes current listings.
  Nothing here knows what actually sold.
- **Sales are estimates.** Each snapshot is compared with the one before. A
  listing whose quantity dropped counts as partly sold. A listing that vanished
  counts as sold only if it could not have expired and no cheaper listing of
  the same item is still there, since buyers take the cheapest first. For gear
  and other one-off items, reposted listings are subtracted. Cancelling the
  cheapest listing still looks like a sale, so the figures are an upper
  estimate.
- **The signal is not a forecast.** It scores today's price against the past
  week: 60% distance from the usual low (30% away is the full swing), 40%
  position between the week's low and high. Above +0.5 reads Buy, below -0.5
  Sell. It needs 24 hourly snapshots, like Deals.
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
  "houses": [113, 116]
  ```

  This is the current setting: 113 is the Horde house for EU PvP (confirmed
  against in-game prices) and 116 is the Blackwater house in the same position
  of Blizzard's list.

- **Item names arrive gradually.** Forever's item data is missing a large share
  of auctionable items. The collector falls back to Classic Era data, looks up
  at most 1,500 new items per run, and retries unknown ones every few days.
- **Random-suffix items share an id.** "of the Tiger" and "of the Bear"
  versions of the same base item are counted together.
- **Recipes are borrowed.** Blizzard's Forever API has no recipe data, so the
  Professions tab uses the open `wow-classic-items` dataset, which describes
  recipes as they stood in Wrath Classic. Most old-world recipes match, a few
  (flasks, for one) used different amounts originally, and anything Forever
  added or changed is missing. Rebuild with `tools/build_recipes.py`.
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
| `<house>/open.json.gz` | Listings of the last snapshot, for the next sales estimate |

History is split into 64 shards by item id, so viewing an item downloads a few
kilobytes. The branch is replaced with a single commit each hour, which keeps
the repository from growing with every snapshot.

## Changing the dashboard

Browsers cache `app.js` and `style.css`. After editing either, raise the
`?v=` number on both links in `site/index.html` so visitors get the new files.

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

Recipes from [wow-classic-items](https://github.com/nexus-devs/wow-classic-items)
(MIT). Charts by [uPlot](https://github.com/leeoniya/uPlot) (MIT). Typeface: Alegreya
and Alegreya Sans by Huerta Tipográfica (SIL Open Font License). Game data
from Blizzard's Battle.net API. Not affiliated with or endorsed by Blizzard
Entertainment.
