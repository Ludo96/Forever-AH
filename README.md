# Forever-AH

Price history and deal-spotting for the World of Warcraft: Forever auction
house. Built for the Horde side of the EU PvP ruleset, plus the neutral
Blackwater house.

**Dashboard:** https://ludo96.github.io/Forever-AH/

It runs entirely on GitHub's free tier. A scheduled job checks Blizzard's API every 20
minutes (Blizzard refreshes its listings about hourly), boils them down to a few numbers per
item, and publishes a static dashboard. There is no server and no database.

## What the dashboard shows

- **Home**: the highlights. Best deals, most gold traded, biggest moves in 24
  hours, most profitable crafts, busiest hours and your watchlist.
- **Deals**: items listed well below their usual low for the past 7 days, with
  the margin left if you relist at the usual market price. Items listed below
  vendor price sit behind a button here.
- **Professions**: what each craft costs in materials today, what the result
  actually sold for, the profit per craft after the auction cut, and the
  profit per day at the pace it sells. Items never seen selling sort last. Enter your skill level to see
  only what you can craft (saved per profession in your browser). Open an item
  to see its recipe priced line by line, and its crafting cost charted against
  its price over time.
- **Volume**: estimated units and value sold in the past 24 hours, and how
  long the current stock would last at that pace.
- **Timing**: which hours of the day the market is busiest and cheapest, and
  for each item the hour it tends to be cheapest, dearest and sell most. Shown
  in the viewer's local time.
- **All items**: everything on sale, with the lowest price, market price,
  24-hour change, 7-day range, a buy / hold / sell signal and units on sale.
- **Watchlist**: items you star. Saved in your own browser, so each friend
  keeps their own list.
- **Item view**: a buy / hold / sell meter, price, supply and sales charts
  over 24 hours, 7 days, 30 days or all time, and the item's price and sales
  by hour of day. The page address updates as you browse, so any view can be
  shared as a link.

- **Telegram alerts**: get a message when an item's lowest price drops below
  or rises above a level. Set one from the alert box on an item's page, or by
  messaging the bot. Each person gets their own alerts.

## Telegram alerts

1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts.
   It gives you a token.
2. Save the token as a repository secret named `TELEGRAM_BOT_TOKEN`.
3. After the next collection run, item pages show a Price alert box.

Commands the bot understands: `/alert Thorium Bar below 18s`,
`/alert Arcane Crystal above 9g`, `/list`, `/remove 2`, `/clear`, `/help`.
The bot is answered by the collection runs, so replies take up to about 20
minutes. An alert fires once when the price crosses its level, then waits
until the price has moved 3% back before it can fire again. Alerts watch the
Horde auction house. They are stored encrypted in `data/alerts.bin`, keyed to
the bot token, so changing the token starts the list afresh.

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
- **Timing needs days, not hours.** Each snapshot is compared with that day's
  median price, then averaged by hour of day. An item's best hour is named
  only once at least 18 hours of the day have been seen on 3 different days,
  and the pick is smoothed with its neighbouring hours. Under two weeks of
  history a pattern can still be coincidence.
- **Sold prices are asking prices that got taken.** When a listing counts as
  sold, its buyout per unit is recorded as the sale price. Crafting profit uses
  the average of those over the past 24 hours, or the most recent sale.
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
  versions of the same base item are counted together. Searching a full name
  such as "Thick Scale Breastplate of Magic" finds the base item. Blizzard's
  listings carry bonus-list codes, but several codes turned out to share one
  suffix in game, so they are not used to split suffixes.
- **Recipes come from Blizzard, skill levels partly do not.** Once a day
  `collector/recipes.py` reads every recipe from Blizzard's Forever profession
  data: what it makes, the materials and the amount. Blizzard does not publish
  the skill a recipe needs, so that comes from the recipe item that teaches it
  (when one has been seen on the auction house) or from original Classic.
  New trainer-taught recipes can show `?` for skill and are never hidden by
  the skill filter. Vendor prices for materials also come from Classic data.
  If Blizzard's recipes are ever unavailable, the dashboard falls back to
  `site/recipes.json`, a Wrath Classic list built by `tools/build_recipes.py`.
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
| `recipes.json` | Recipes from Blizzard's profession data, rebuilt daily |
| `recipe_items.json` | Which recipe items have been read, and what they teach |
| `<house>/summary.json` | One row per item listed now, with 7- and 30-day ranges |
| `<house>/h/<date>/<shard>.json` | Hourly points, kept 30 days |
| `<house>/d/<month>/<shard>.json` | Daily points, kept indefinitely |
| `<house>/p/<shard>.json` | Each item's price and sales by hour of day |
| `<house>/open.json.gz` | Listings of the last snapshot, for the next sales estimate |

History is split into 64 shards by item id, so viewing an item downloads a few
kilobytes. The branch is replaced with a single commit each hour, which keeps
the repository from growing with every snapshot.

## Looking at the API

Forever's endpoints are undocumented. Actions tab → Probe the API → Run
workflow prints what Blizzard returns for any paths you give it, for example
`/data/wow/recipe/55135`. It changes nothing.

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

Classic skill levels and vendor prices from
[wow-classic-items](https://github.com/nexus-devs/wow-classic-items) (MIT). Charts by [uPlot](https://github.com/leeoniya/uPlot) (MIT). Typeface: Alegreya
and Alegreya Sans by Huerta Tipográfica (SIL Open Font License). Game data
from Blizzard's Battle.net API. Not affiliated with or endorsed by Blizzard
Entertainment.
