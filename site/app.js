/* Auction ledger: reads the static files written by collector/collect.py. */
(() => {
"use strict";

const DEAL_MIN_POINTS = 24;          // hourly points an item needs before it can be a "deal"
const RANGES = [["24h", "24 hours", 1], ["7d", "7 days", 7], ["30d", "30 days", 30], ["all", "All", 0]];
const QUALITY = ["Poor", "Common", "Uncommon", "Rare", "Epic", "Legendary", "Artifact"];
const FRESH = { cache: "no-cache" };

const S = {
  meta: null, items: {}, house: null, updated: null,
  rows: [], byId: new Map(),
  tab: "home", q: "", hours: null, sort: {}, limit: 100, discount: 20,
  recipes: null, prof: "Alchemy", skill: {}, spec: {},
  item: null, range: "7d", watch: new Set(), charts: [], observer: null, drawId: 0,
};

/* ------------------------------------------------------------ helpers -- */

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(props || {})) {
    if (val == null || val === false) continue;
    if (key === "class") node.className = val;
    else if (key === "text") node.textContent = val;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), val);
    else node.setAttribute(key, val === true ? "" : val);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
  return node;
}

const num = n => n.toLocaleString("en-GB");
const pad = n => String(n).padStart(2, "0");
const dayStr = ts => new Date(ts * 1000).toISOString().slice(0, 10);
const nowSec = () => Math.floor(Date.now() / 1000);

async function getJSON(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

/* Prices are copper per unit. 1 gold = 100 silver = 10,000 copper. */
function moneyParts(copper) {
  const c = Math.max(0, Math.round(copper));
  const g = Math.floor(c / 10000), s = Math.floor(c % 10000 / 100), k = c % 100;
  if (g >= 1) return s ? [[g, "g"], [s, "s"]] : [[g, "g"]];      // copper is noise above 1g
  if (s >= 1) return [[s, "s"], ...(k ? [[k, "c"]] : [])];
  return [[k, "c"]];
}
function moneyText(copper) {
  if (copper == null) return "–";
  return (copper < 0 ? "−" : "") + moneyParts(Math.abs(copper)).map(([n, u]) => num(n) + u).join(" ");
}
function money(copper) {
  if (copper == null) return h("span", { class: "dim", text: "–" });
  const wrap = h("span", { class: "money", role: "img", "aria-label": moneyText(copper) });
  if (copper < 0) wrap.append("−");
  for (const [n, unit] of moneyParts(Math.abs(copper))) wrap.append(h("span", { class: `coin ${unit}`, text: num(n) }));
  return wrap;
}
function axisMoney(v) {
  if (v == null) return "";
  const short = n => num(+n.toPrecision(3));
  if (Math.abs(v) >= 10000) return short(v / 10000) + "g";
  if (Math.abs(v) >= 100) return short(v / 100) + "s";
  return Math.round(v) + "c";
}

function ago(ts) {
  const mins = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.round(hours / 24)} days ago`;
}

const currentHouse = () => S.meta.houses.find(x => x.id === S.house);

function houseLabel(house) {
  const base = { horde: "Horde", alliance: "Alliance", blackwater: "Blackwater" }[house.faction] || house.name;
  const twins = S.meta.houses.filter(x => x.faction === house.faction).length;
  return twins > 1 ? `${base} ${house.id}` : base;
}

/* -------------------------------------------- saved state: URL + watch -- */

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.get("h")) S.house = +p.get("h");
  if (p.get("t") in TABS) S.tab = p.get("t");
  S.item = p.get("i") ? +p.get("i") : null;
  if (RANGES.some(r => r[0] === p.get("r"))) S.range = p.get("r");
  S.q = p.get("q") || "";
  if (p.get("p")) S.prof = p.get("p");
}
function writeHash() {
  const p = new URLSearchParams();
  p.set("h", S.house);
  p.set("t", S.tab);
  if (S.tab === "prof") p.set("p", S.prof);
  if (S.item != null) { p.set("i", S.item); p.set("r", S.range); }
  if (S.q) p.set("q", S.q);
  history.replaceState(null, "", "#" + p);
}
function loadWatch() {
  try { S.watch = new Set(JSON.parse(localStorage.getItem("ledger.watch") || "[]")); } catch { /* private mode */ }
  try { S.skill = JSON.parse(localStorage.getItem("ledger.skill") || "{}") || {}; } catch { /* private mode */ }
  try { S.spec = JSON.parse(localStorage.getItem("ledger.spec") || "{}") || {}; } catch { /* private mode */ }
}
/* Your skill level per profession (1 to 300), saved in this browser. */
function setSkill(text) {
  const n = Math.min(300, parseInt(text, 10));
  if (n >= 1) S.skill[S.prof] = n; else delete S.skill[S.prof];
  try { localStorage.setItem("ledger.skill", JSON.stringify(S.skill)); } catch { /* not saved */ }
  S.limit = 100;
  render();
  const input = $("#skill");                       // the controls were rebuilt: keep typing
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
/* Specialization per profession, saved in this browser: "" shows every recipe,
   "none" only recipes anyone can learn, a name those plus that specialization's. */
function setSpec(value) {
  if (value) S.spec[S.prof] = value; else delete S.spec[S.prof];
  try { localStorage.setItem("ledger.spec", JSON.stringify(S.spec)); } catch { /* not saved */ }
  S.limit = 100;
  render();
}
function specsOf(prof) {
  const list = (S.recipes && S.recipes.professions[prof]) || [];
  return [...new Set(list.map(rec => rec[5]).filter(Boolean))].sort();
}
const specLabel = (spec, prof) => spec.replace(` ${prof}`, "");      // "Dragonscale Leatherworking" -> "Dragonscale"
function pickProf(name) {
  S.prof = name;
  S.limit = 100;
  writeHash();
  render();
}
function profChips() {
  if (!S.recipes) return [];
  const chip = (label, on, click, title) => h("button", { type: "button", class: "chip", "aria-pressed": String(on), title, onclick: click, text: label });
  const specs = specsOf(S.prof);
  const mine = S.spec[S.prof] || "";
  return [
    h("div", { class: "chips", role: "group", "aria-label": "Profession" },
      Object.keys(S.recipes.professions).map(name => chip(name, name === S.prof, () => pickProf(name)))),
    specs.length ? h("div", { class: "chips spec", role: "group", "aria-label": "Specialization" },
      h("span", { class: "chips-label", text: "Specialization" }),
      chip("Any", !mine, () => setSpec(""), "Show every recipe"),
      chip("None", mine === "none", () => setSpec("none"), "Only recipes that need no specialization"),
      specs.map(s => chip(specLabel(s, S.prof), mine === s, () => setSpec(s), `Recipes anyone can learn, plus ${s}`))) : null,
  ];
}

function toggleWatch(id) {
  S.watch.has(id) ? S.watch.delete(id) : S.watch.add(id);
  try { localStorage.setItem("ledger.watch", JSON.stringify([...S.watch])); } catch { /* not saved */ }
  render();
  renderDetail(true);
}

/* ------------------------------------------------------------- columns -- */

function starBtn(row) {
  const on = S.watch.has(row.id);
  return h("button", {
    class: "star", type: "button", "aria-pressed": String(on), text: on ? "★" : "☆",
    "aria-label": `${on ? "Stop watching" : "Watch"} ${row.name}`,
    onclick: e => { e.stopPropagation(); toggleWatch(row.id); },
  });
}
function nameCell(row) {
  return h("button", { class: "item", type: "button" },
    h("span", { class: `q q${row.quality}`, title: QUALITY[row.quality] || "" }),
    h("span", { text: row.name }),
    row.makes && row.makes !== 1 ? h("span", { class: "sub", text: `makes ${row.makes}` }) : null,
    row.needs ? h("span", { class: "sub spec-tag", text: row.needs }) : null,
    row.listed ? null : h("span", { class: "sub", text: "not listed" }));
}
function changeCell(row) {
  if (row.change == null) return h("span", { class: "dim", text: "–" });
  const pct = Math.round(row.change * 100);
  if (!pct) return h("span", { class: "dim", text: "0%" });
  return h("span", { class: pct < 0 ? "down" : "up", text: `${Math.abs(pct)}%` });
}
function rangeCell(row) {
  if (!row.listed || row.n7 < 2) return h("span", { class: "dim", text: "–" });
  const span = row.hi7 - row.lo7;
  const at = v => (span > 0 ? Math.min(100, Math.max(0, (v - row.lo7) / span * 100)) : 50) + "%";
  return h("span", {
    class: "range", role: "img",
    title: `7-day low ${moneyText(row.lo7)}, usual ${moneyText(row.m7)}, high ${moneyText(row.hi7)}`,
    "aria-label": `7-day low ${moneyText(row.lo7)}, usual ${moneyText(row.m7)}, high ${moneyText(row.hi7)}`,
  }, h("i", { style: `left:${at(row.m7)}` }), h("b", { style: `left:${at(row.low)}` }));
}

const COLS = {
  star: { label: "", cls: "tight", cell: starBtn },
  name: { label: "Item", cls: "name", cell: nameCell, val: r => r.name.toLowerCase(), first: 1 },
  low: { label: "Lowest", num: 1, cell: r => money(r.low), val: r => r.low },
  market: { label: "Market", num: 1, cls: "hide-sm", cell: r => money(r.market), val: r => r.market },
  change: { label: "24 hours", num: 1, cell: changeCell, val: r => r.change },
  range: { label: "7-day range", cls: "hide-sm", cell: rangeCell },
  qty: { label: "On sale", num: 1, cell: r => r.listed ? num(r.qty) : "–", val: r => r.qty },
  usual: { label: "Usual low", num: 1, cls: "hide-sm", cell: r => money(r.m7), val: r => r.m7 },
  discount: { label: "Below usual", num: 1, val: r => r.discount,
    cell: r => h("span", { class: "pill", text: `${Math.min(99, Math.round(r.discount * 100))}%` }) },
  near: { label: "At this price", num: 1, cls: "hide-sm", cell: r => num(r.near), val: r => r.near },
  profit: { label: "Resale margin", num: 1, cell: r => money(r.profit), val: r => r.profit },
  sell: { label: "Vendor pays", num: 1, cls: "hide-sm", cell: r => money(r.sell), val: r => r.sell },
  signal: { label: "Signal", cls: "hide-sm", val: r => r.sig ? r.sig.score : null,
    cell: r => r.sig ? h("span", { class: `sig ${r.sig.tone}`, text: r.sig.label }) : h("span", { class: "dim", text: "–" }) },
  sold: { label: "Sold", num: 1, cell: r => num(r.sold1), val: r => r.sold1 },
  value: { label: "Value sold", num: 1, cell: r => money(r.value), val: r => r.value },
  stock: { label: "Stock lasts", num: 1, cls: "hide-sm", val: r => r.stock, first: 1,
    cell: r => r.stock == null ? h("span", { class: "dim", text: "–" })
      : r.stock < 1 ? (Math.round(r.stock * 24) <= 1 ? "1 hour" : `${Math.round(r.stock * 24)} hours`)
        : `${r.stock < 10 ? r.stock.toFixed(1) : Math.round(r.stock)} days` },
  buyAt: { label: "Cheapest at", num: 1, val: r => r.buyPct,
    cell: r => h("span", null, clock(toLocal(r.buyHour)), h("span", { class: "sub", text: ` −${r.buyPct.toFixed(1)}%` })) },
  sellAt: { label: "Dearest at", num: 1, val: r => r.sellPct,
    cell: r => h("span", null, clock(toLocal(r.sellHour)), h("span", { class: "sub", text: ` +${r.sellPct.toFixed(1)}%` })) },
  swing: { label: "Daily swing", num: 1, val: r => r.swing, cell: r => `${r.swing.toFixed(1)}%` },
  peakAt: { label: "Sells most at", num: 1, cls: "hide-sm", val: r => r.peakHour == null ? null : toLocal(r.peakHour), first: 1,
    cell: r => r.peakHour == null ? h("span", { class: "dim", text: "–" }) : clock(toLocal(r.peakHour)) },
  sold7: { label: "Sold in 7 days", num: 1, cls: "hide-sm", cell: r => num(r.sold7), val: r => r.sold7 },
  skill: { label: "Skill", num: 1, cls: "hide-sm", always: 1, val: r => r.skill, first: 1,
    cell: r => r.skill == null ? h("span", { class: "dim", title: "Blizzard does not publish the skill for this recipe", text: "?" }) : String(r.skill) },
  cost: { label: "Materials", num: 1, always: 1, cell: r => money(r.cost), val: r => r.cost },
  soldFor: { label: "Sold for", num: 1, cls: "hide-sm", always: 1, val: r => r.sale,
    cell: r => r.sale == null ? h("span", { class: "dim", text: "no sales seen" })
      : h("span", null, money(r.sale), r.saleNote ? h("span", { class: "sub", text: ` ${r.saleNote}` }) : null) },
  sold24: { label: "Sold in 24 hours", num: 1, always: 1, val: r => r.sold1 || null,
    cell: r => r.sold1 ? num(r.sold1) : h("span", { class: "dim", text: "–" }) },
  craft: { label: "Profit per craft", num: 1, always: 1, val: r => r.craft,
    cell: r => r.craft == null ? h("span", { class: "dim", text: "–" }) : money(r.craft) },
  perDay: { label: "Profit per day", num: 1, cls: "hide-sm", always: 1, val: r => r.perDay,
    cell: r => r.perDay == null ? h("span", { class: "dim", text: "–" }) : money(r.perDay) },
  vprofit: { label: "Profit each", num: 1, cell: r => money(r.vprofit), val: r => r.vprofit },
};

const TABS = {
  home: { label: "Home", cols: [], sort: ["name", 1], rows: () => [], blurb: () => "" },
  deals: {
    label: "Deals", count: true,
    cols: ["star", "name", "low", "usual", "discount", "near", "profit"], sort: ["discount", -1],
    rows: () => S.rows.filter(r => r.n7 >= DEAL_MIN_POINTS && r.discount >= S.discount / 100 && r.profit > 0),
    blurb: () => `Listed well below the item's usual low for the past 7 days. Resale margin is what is left per unit if you relist at the usual market price and pay the ${Math.round(currentHouse().cut * 100)}% auction cut.`,
  },
  vendor: {
    label: "Vendor flips", hidden: true,
    cols: ["star", "name", "low", "sell", "vprofit", "near"], sort: ["vprofit", -1],
    rows: () => S.rows.filter(r => r.vprofit > 0),
    blurb: () => "Listed for less than a vendor pays. Buy it, sell it to any vendor, keep the difference.",
  },
  prof: {
    label: "Professions",
    cols: ["star", "name", "skill", "cost", "soldFor", "sold24", "craft", "perDay"], sort: ["craft", -1],
    rows: () => craftRows(),
    top: profChips,
    blurb: () => {
      const cut = Math.round(currentHouse().cut * 100);
      const source = S.recipes && S.recipes.official
        ? "Recipes come from Blizzard's Forever data. Skill levels come from the items that teach each recipe or from original Classic, so a few new recipes show ? and are never filtered out."
        : "Recipes come from Wrath Classic data, so a few differ in Forever. Check the breakdown against the recipe in game before you buy.";
      const mine = S.skill[S.prof] ? ` Showing recipes that need ${S.prof} skill ${S.skill[S.prof]} or lower.` : "";
      const specs = specsOf(S.prof).length ? " Specializations come from the plans and patterns that teach each recipe, so a specialization recipe taught only by a trainer may show as needing none." : "";
      return `Profit is the price the item actually sold for in the past 24 hours (its most recent sale if none), less the ${cut}% auction cut and today's material cost at market price, or vendor price when cheaper. Items never seen selling have no profit and sort last. Profit per day is that margin times the units sold in a day. ${source}${mine}${specs}`;
    },
  },
  volume: {
    label: "Volume",
    cols: ["star", "name", "sold", "value", "qty", "stock", "low"], sort: ["value", -1],
    rows: () => S.rows.filter(r => r.sold1 > 0),
    blurb: () => {
      const span = S.salesHours >= 24 ? "the past 24 hours" : `the past ${Math.round(S.salesHours)} hour${Math.round(S.salesHours) === 1 ? "" : "s"} (counting started recently)`;
      return `Estimated sales over ${span}. Blizzard does not publish sales, so these count listings that vanished with plenty of time left or shrank between hourly snapshots. Cancelled auctions look the same as sales, so read the figures as an upper estimate. Value is units sold at the current market price; stock lasts is how long today's supply would cover that pace.`;
    },
  },
  timing: {
    label: "Timing",
    cols: ["star", "name", "buyAt", "sellAt", "swing", "peakAt", "sold7"], sort: ["swing", -1],
    rows: () => S.rows.filter(r => r.buyHour != null && r.sold7 >= 10),
    top: marketHours,
    blurb: () => `When each item tends to be cheapest and dearest across the day, as a percentage against its average for the day. Daily swing is the gap between the two. An hour counts once it has been seen on 3 days, and a pattern from under two weeks of history can still be coincidence. ${TZ_NOTE}`,
  },
  all: {
    label: "All items",
    cols: ["star", "name", "low", "market", "change", "range", "signal", "qty"], sort: ["qty", -1],
    rows: () => S.rows,
    blurb: () => "Everything on sale right now. Lowest is the cheapest buyout per unit; market averages the cheapest 15% of units, so one stray listing does not move it.",
  },
  watch: {
    label: "Watchlist",
    cols: ["star", "name", "low", "market", "change", "range", "signal", "qty"], sort: ["name", 1],
    rows: () => [...S.watch].map(id => S.byId.get(id) || unlisted(id)),
    blurb: () => "Items you starred. Saved in this browser only.",
  },
};

function nameOf(id) {
  const it = S.items[id];
  return it ? it[0] : (S.recipes && S.recipes.names[id]) || `Item ${id}`;
}
function qualityOf(id) {
  const it = S.items[id];
  if (it) return it[1];
  return S.recipes && id in S.recipes.quality ? S.recipes.quality[id] : -1;
}
function unlisted(id) {
  const it = S.items[id];
  return { id, listed: false, name: nameOf(id), quality: qualityOf(id), n7: 0, n30: 0,
    sell: it ? it[5] : 0, kind: kindOf(it) };
}

/* Professions: what a craft costs in materials and what it leaves. */
function matPrice(id, field) {
  const row = S.byId.get(id);
  const ah = row ? row[field] : null;
  const vendor = S.recipes.vendor[id];
  if (ah == null) return vendor == null ? null : vendor;
  return vendor == null ? ah : Math.min(ah, vendor);
}
function craftRows(prof = S.prof) {
  if (!S.recipes) return [];
  const cut = currentHouse().cut;
  const mine = S.skill[prof] || 300;
  const spec = S.spec[prof] || "";
  const allowed = rec => !rec[5] || (spec ? spec !== "none" && rec[5] === spec : true);
  return (S.recipes.professions[prof] || []).filter(rec => rec[1] <= mine && allowed(rec)).map(([id, skill, lo, hi, flat, needs]) => {
    const base = S.byId.get(id) || unlisted(id);
    const makes = (lo + hi) / 2;
    let cost = 0, complete = true;
    for (let k = 0; k < flat.length; k += 2) {
      const now = matPrice(flat[k], "market");
      if (now == null) { complete = false; break; }
      cost += now * flat[k + 1];
    }
    if (!complete && !base.listed) return null;       // nothing known about it in Forever
    // Profit is what the item actually sold for, less today's material cost.
    // Sales seen before prices were recorded fall back to the lowest listing.
    const sale = base.soldPrice || (base.sold1 > 0 ? base.low : null);
    const age = base.soldAt ? S.updated - base.soldAt : 0;
    const craft = complete && sale != null ? Math.round(sale * makes * (1 - cut)) - cost : null;
    return { ...base, skill, makes, sale, needs: needs ? specLabel(needs, prof) : null,
      saleNote: !base.soldPrice && sale != null ? "listing" : age > 86400 ? `${Math.round(age / 86400)}d ago` : null,
      cost: complete ? cost : null, craft,
      // what crafting everything that sold in a day would have made
      perDay: craft != null && base.sold1 > 0 && S.salesHours ? Math.round(craft * base.sold1 * 24 / S.salesHours / makes) : null };
  }).filter(Boolean);
}
function recipesFor(id) {
  const found = [];
  if (!S.recipes) return found;
  for (const [prof, list] of Object.entries(S.recipes.professions)) {
    for (const rec of list) if (rec[0] === id) found.push([prof, rec]);
  }
  return found;
}
function kindOf(it) {
  if (!it) return "Blizzard has not published details for this item yet";
  const type = [it[3], it[2]].filter(Boolean).join(" ");
  return [QUALITY[it[1]], type.toLowerCase()].filter(Boolean).join(" ") + (it[4] ? `, item level ${it[4]}` : "");
}

/* --------------------------------------------------------- hour of day -- */

/* The collector files everything by UTC hour; people read local time. */
const OFF = Math.round(new Date().getTimezoneOffset() / 60);          // local hour + OFF = UTC hour

/* Which clock the times are on: "Dhaka time (UTC+6), 5 hours ahead of EU server time". */
const TZ_NOTE = (() => {
  const offsetIn = zone => {                       // minutes ahead of UTC, now, in a zone
    const now = new Date();
    const local = new Date(now.toLocaleString("en-US", { timeZone: zone }));
    const utc = new Date(now.toLocaleString("en-US", { timeZone: "UTC" }));
    return Math.round((local - utc) / 60000);
  };
  const fmt = mins => {
    const sign = mins < 0 ? "−" : "+", a = Math.abs(mins);
    return `UTC${sign}${Math.floor(a / 60)}${a % 60 ? ":" + pad(a % 60) : ""}`;
  };
  let zone = "", mine = -new Date().getTimezoneOffset(), server = 60;
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    server = offsetIn("Europe/Paris");             // EU realms run on Central European Time
  } catch { /* older browser: fall back to the offset alone */ }
  const place = zone.includes("/") ? zone.split("/").pop().replace(/_/g, " ") + " time" : "Your local time";
  const diff = (mine - server) / 60;
  const hours = n => `${Number.isInteger(n) ? n : n.toFixed(1)} hour${Math.abs(n) === 1 ? "" : "s"}`;
  const vs = diff === 0 ? "the same as EU server time"
    : `${hours(Math.abs(diff))} ${diff > 0 ? "ahead of" : "behind"} EU server time`;
  return `Times are in ${place} (${fmt(mine)}), ${vs}.`;
})();
const tzLine = () => h("p", { class: "tz", text: TZ_NOTE });
const toLocal = utcHour => ((utcHour - OFF) % 24 + 24) % 24;
const clock = hour => `${pad(hour)}:00`;
const localOrder = utcValues => Array.from({ length: 24 }, (_, l) => utcValues[((l + OFF) % 24 + 24) % 24]);

/* 24 bars, one per local hour. `level` draws values around 1.0 as above or
   below the day's average; otherwise bars rise from zero. Hovering or tapping
   a bar puts its value in the line under the chart. */
function hourBars(utcValues, { level, fmt, idle }) {
  const vals = localOrder(utcValues);
  const read = h("p", { class: "hours-read", text: idle });
  const scale = level ? Math.max(0.02, ...vals.map(v => v == null ? 0 : Math.abs(v - 1)))
    : Math.max(1e-9, ...vals.map(v => v || 0));
  const plot = h("div", { class: `hours${level ? " level" : ""}`, role: "img", "aria-label": idle }, vals.map((v, l) => {
    let bar = null;
    if (v != null && level) {
      const d = (v - 1) / scale;
      bar = h("i", { class: d >= 0 ? "hi" : "lo", style: `height:${Math.abs(d) * 50}%;${d >= 0 ? "bottom" : "top"}:50%` });
    } else if (v != null) {
      bar = h("i", { style: `height:${v > 0 ? Math.max(2, v / scale * 100) : 0}%;bottom:0` });
    }
    const text = `${clock(l)} to ${clock((l + 1) % 24)}: ${v == null ? "no data yet" : fmt(v)}`;
    const col = h("div", { class: "hb" }, bar);
    const show = () => { read.textContent = text; };
    col.addEventListener("mouseenter", show);
    col.addEventListener("click", show);
    return col;
  }));
  plot.addEventListener("mouseleave", () => { read.textContent = idle; });
  return h("div", { class: "hourchart" }, plot,
    h("div", { class: "hours-axis", "aria-hidden": "true" }, [0, 6, 12, 18].map(x => h("span", { text: clock(x) }))), read);
}

const levelText = v => Math.abs(v - 1) < 0.0005 ? "at the day's average"
  : `${Math.abs((v - 1) * 100).toFixed(1)}% ${v > 1 ? "above" : "below"} the day's average`;
const peak = (vals, sign = 1) => vals.reduce((best, v, i) => v != null && (best < 0 || sign * v > sign * vals[best]) ? i : best, -1);

/* Market-wide: gold traded and price level for each hour of the day. */
function marketHours(compact) {
  const hrs = S.hours;
  const snaps = hrs ? hrs.snapshots.reduce((a, b) => a + b, 0) : 0;
  if (!hrs || snaps < 2) {
    return [h("p", { class: "hours-wait", text: "The by-hour picture appears once sales have been counted for a few hours." })];
  }
  const days = snaps / 24;
  const tradedBy = hrs.traded.map((v, i) => hrs.snapshots[i] ? v : null);       // hours not seen yet stay blank
  const busy = peak(tradedBy), cheap = peak(hrs.price, -1), dear = peak(hrs.price);
  const basis = days < 1 ? `Based on ${Math.round(snaps)} hours so far, so this is one day's shape, not a pattern yet.`
    : days < 7 ? `Based on ${days.toFixed(1)} days so far. Give it a week before trusting the pattern.`
    : `Averaged over ${Math.round(days)} days.`;
  const trade = hourBars(tradedBy, { fmt: v => `about ${moneyText(v)} traded`,
    idle: `Most gold changes hands around ${clock(toLocal(busy))}. ${basis}` });
  if (compact) return [trade, tzLine()];
  const out = [h("div", { class: "hourpair" },
    h("div", null, h("p", { class: "chart-title", text: "Gold traded by hour" }), trade),
    cheap < 0 ? null : h("div", null, h("p", { class: "chart-title", text: "Price level by hour" }),
      hourBars(hrs.price, { level: true, fmt: levelText,
        idle: `Across the market, prices are lowest around ${clock(toLocal(cheap))} and highest around ${clock(toLocal(dear))}.` })))];
  return out;
}

/* One item's profile, from p/<shard>.json: [24 prices x1000, 24 sold x10, days]. */
async function itemHours(id) {
  const box = $("#hoursBox");
  if (!box) return;
  let prof = null;
  try {
    const doc = await getJSON(`data/${S.house}/p/${pad(id % S.meta.shards)}.json`, FRESH);
    prof = doc.i[id];
  } catch { /* no profiles published yet */ }
  if (S.item !== id || !box.isConnected || !prof) return;
  const price = prof.slice(0, 24).map(v => v == null ? null : v / 1000);
  const sold = prof.slice(24, 48).map(v => v / 10);
  const days = prof[48];
  const row = S.byId.get(id);
  const parts = [h("p", { class: "chart-title hours-title", text: "By hour of day" }), tzLine()];
  if (days >= 1) {
    const cheap = peak(price, -1), dear = peak(price);
    const sure = row && row.buyHour != null;
    parts.push(h("p", { class: "small-title", text: "Price against the day's average" }),
      hourBars(price, { level: true, fmt: levelText, idle: sure
        ? `Usually cheapest around ${clock(toLocal(row.buyHour))} and dearest around ${clock(toLocal(row.sellHour))}, over ${days} days.`
        : `Lowest around ${clock(toLocal(cheap))} and highest around ${clock(toLocal(dear))} so far. With ${days} day${days === 1 ? "" : "s"} of history this is not a pattern yet.` }));
  }
  if (sold.some(v => v > 0)) {
    parts.push(h("p", { class: "small-title", text: "Estimated units sold per hour" }),
      hourBars(sold, { fmt: v => `about ${v < 10 ? v.toFixed(1) : num(Math.round(v))} sold`,
        idle: `Sells most around ${clock(toLocal(peak(sold)))}.` }));
  }
  if (parts.length > 1) box.replaceChildren(...parts);
}

/* ----------------------------------------------------------------- home -- */

function panel(title, tab, rows, emptyText, extra) {
  return h("section", { class: "panel" },
    h("div", { class: "panel-head" },
      h("h2", { text: title }),
      tab ? h("button", { class: "linkish", type: "button", onclick: () => goTab(tab), text: "See all" }) : null),
    extra || null,
    rows.length ? h("ul", { class: "hl" }, rows) : (emptyText ? h("p", { class: "panel-empty", text: emptyText }) : null));
}
function hlRow(row, note, value) {
  return h("li", null, h("button", { type: "button", class: row.id === S.item ? "on" : null, onclick: () => openItem(row.id) },
    h("span", { class: "hl-name" }, h("span", { class: `q q${row.quality}` }), h("span", { text: row.name }),
      note ? h("span", { class: "sub", text: note }) : null),
    h("span", { class: "hl-val" }, value)));
}

function renderHome() {
  if (!S.rows.length) {
    return [h("div", { class: "empty" }, h("strong", { text: "No prices for this auction house yet" }),
      "The collector has not saved a snapshot for it. It checks every 20 minutes.")];
  }
  const house = currentHouse();
  const snaps = house.snapshots || 0;
  const top = (rows, key, n = 6) => rows.slice().sort((a, b) => b[key] - a[key]).slice(0, n);
  const pct = (v, cap = 999) => `${Math.min(cap, Math.abs(Math.round(v * 100)))}%`;

  const deals = top(TABS.deals.rows(), "discount");
  const traded = top(S.rows.filter(r => r.value), "value");
  const moved = S.rows.filter(r => r.change != null && r.sold1 > 0 && r.n7 >= DEAL_MIN_POINTS && r.low >= 100);
  const fell = top(moved.filter(r => r.change < -0.05).map(r => ({ ...r, drop: -r.change })), "drop", 3);
  const rose = top(moved.filter(r => r.change > 0.05), "change", 3);

  const saved = S.recipes ? Object.keys(S.recipes.professions).filter(p => S.skill[p]) : [];
  const profs = S.recipes ? (saved.length ? saved : Object.keys(S.recipes.professions)) : [];
  const crafts = top(profs.flatMap(p => craftRows(p).filter(r => r.craft > 0 && r.sold1 > 0).map(r => ({ ...r, prof: p }))), "craft");

  const watched = [...S.watch].map(id => S.byId.get(id) || unlisted(id)).sort((a, b) => a.name.localeCompare(b.name)).slice(0, 8);
  const total = S.rows.reduce((sum, r) => sum + (r.value || 0), 0);
  const span = S.salesHours >= 24 ? "24 hours" : `${Math.round(S.salesHours)} hour${Math.round(S.salesHours) === 1 ? "" : "s"}`;
  const stat = (label, value) => h("div", null, h("dt", { text: label }), h("dd", null, value));
  const wait = `Needs ${DEAL_MIN_POINTS} hourly snapshots to know what is usual. ${Math.min(snaps, DEAL_MIN_POINTS)} collected so far.`;

  return [
    h("dl", { class: "pulse" },
      stat("Items on sale", num(S.rows.length)),
      stat("Units on sale", num(S.rows.reduce((sum, r) => sum + r.qty, 0))),
      S.salesHours ? stat(`Traded in ${span}, estimated`, money(total)) : null,
      stat("History collected", snaps >= 48 ? `${Math.floor(snaps / 24)} days` : `${snaps} hour${snaps === 1 ? "" : "s"}`)),
    h("div", { class: "panels" },
      panel("Best deals", "deals", deals.map(r => hlRow(r, null,
        h("span", null, money(r.low), " ", h("span", { class: "pill", text: `−${pct(r.discount, 99)}` })))),
        snaps < DEAL_MIN_POINTS ? wait : "Nothing is listed well below its usual price right now."),
      panel("Most gold traded", "volume", traded.map(r => hlRow(r, `about ${num(r.sold1)} sold`, money(r.value))),
        "Sales are counted by comparing hourly snapshots. The first figures appear after the next refresh."),
      panel("Biggest moves in 24 hours", "all", [
        ...fell.map(r => hlRow(r, null, h("span", null, money(r.low), " ", h("span", { class: "down", text: pct(r.change) })))),
        ...rose.map(r => hlRow(r, null, h("span", null, money(r.low), " ", h("span", { class: "up", text: pct(r.change) })))),
      ], snaps < DEAL_MIN_POINTS + 1 ? "Needs a full day of history to compare against." : "No item that sells has moved more than 5% since yesterday."),
      panel("Most profitable crafts", "prof", crafts.map(r => hlRow(r, r.skill == null ? r.prof : `${r.prof} ${r.skill}`, money(r.craft))),
        saved.length ? "Nothing profitable at your saved skill levels right now." : "No craft is profitable at current prices.",
        h("p", { class: "panel-note", text: saved.length
          ? `At your saved skill in ${saved.join(", ")}.`
          : "All skill levels. Set your skill under Professions to see only what you can make." })),
      panel("Busiest hours", "timing", [], null, h("div", null, marketHours(true))),
      panel("Your watchlist", "watch", watched.map(r => hlRow(r, r.listed ? null : "not listed",
        r.listed ? h("span", null, money(r.low), r.sig ? h("span", { class: `sig ${r.sig.tone} hl-sig`, text: r.sig.label }) : null) : h("span", { class: "dim", text: "–" }))),
        "Star any item to pin it here. Saved in this browser only.")),
  ];
}

/* -------------------------------------------------------------- loading -- */

async function boot() {
  loadWatch();
  readHash();
  try {
    S.meta = await getJSON("data/meta.json", FRESH);
  } catch {
    $("#status").textContent = "No prices yet.";
    showEmpty("Waiting for the first snapshot",
      "The collector has not published any data. It checks for new prices every 20 minutes; if this message stays, check the Actions tab of the repository for a failed run.");
    return;
  }
  [S.items, S.recipes] = await Promise.all([
    getJSON("data/items.json", FRESH).catch(() => ({})),
    getJSON("data/recipes.json", FRESH).catch(() => getJSON("recipes.json")).catch(() => null)]);
  if (S.recipes && !(S.prof in S.recipes.professions)) S.prof = Object.keys(S.recipes.professions)[0];
  if (!S.meta.houses.some(x => x.id === S.house)) {
    S.house = (S.meta.houses.find(x => x.faction === "horde") || S.meta.houses[0]).id;
  }
  bind();
  await loadHouse();
}

/* Fetch the newest published prices. A manual refresh adds a one-off query
   string so a copy cached along the way cannot be served instead. */
async function refresh(manual) {
  const bust = manual ? `?t=${Date.now()}` : "";
  const before = S.updated;
  S.meta = await getJSON("data/meta.json" + bust, FRESH);
  S.items = await getJSON("data/items.json" + bust, FRESH).catch(() => S.items);
  S.bust = bust;
  if (!S.meta.houses.some(x => x.id === S.house)) {
    S.house = (S.meta.houses.find(x => x.faction === "horde") || S.meta.houses[0]).id;
  }
  await loadHouse();
  return S.updated !== before;
}

async function refreshClicked() {
  const button = $("#refresh"), note = $("#refresh-note");
  button.disabled = true;
  button.textContent = "Refreshing…";
  try {
    const changed = await refresh(true);
    note.textContent = changed ? "New prices loaded." : "Already the latest. Blizzard updates about once an hour.";
  } catch {
    note.textContent = "Could not load prices. Check your connection and try again.";
  }
  button.disabled = false;
  button.textContent = "Refresh";
  clearTimeout(refreshClicked.timer);
  refreshClicked.timer = setTimeout(() => { note.textContent = ""; }, 6000);
}

async function loadHouse() {
  const house = currentHouse();
  let doc = { cols: [], rows: [], updated: null };
  try { doc = await getJSON(`data/${S.house}/summary.json${S.bust || ""}`, FRESH); } catch { /* no snapshot yet */ }
  const c = Object.fromEntries(doc.cols.map((name, i) => [name, i]));
  S.updated = doc.updated;
  S.hours = doc.hours || null;
  S.rows = doc.rows.map(r => {
    const id = r[c.id], it = S.items[id];
    const low = r[c.low], m7 = r[c.low7dMed], mk7 = r[c.market7dMed], prev = r[c.low24hAgo];
    const sell = it ? it[5] : 0;
    return {
      id, listed: true, name: nameOf(id), quality: qualityOf(id), kind: kindOf(it),
      low, market: r[c.market], qty: r[c.qty], near: r[c.near], listings: r[c.listings],
      lo7: r[c.low7dMin], m7, hi7: r[c.low7dMax], mk7, n7: r[c.points7d],
      lo30: r[c.low30dMin], m30: r[c.low30dMed], hi30: r[c.low30dMax], n30: r[c.points30d],
      sell, change: prev ? low / prev - 1 : null,
      sold1: c.sold1d == null ? null : r[c.sold1d], sold7: c.sold7d == null ? null : r[c.sold7d],
      discount: m7 ? 1 - low / m7 : 0,
      profit: Math.round(mk7 * (1 - house.cut)) - low,
      vprofit: sell ? sell - low : null,
      buyHour: r[c.buyHour], buyPct: r[c.buyPct], sellHour: r[c.sellHour], sellPct: r[c.sellPct],
      soldPrice: r[c.soldPrice] || null, soldAt: r[c.soldAt] || null,
      peakHour: r[c.peakHour], swing: r[c.buyHour] == null ? null : r[c.buyPct] + r[c.sellPct],
    };
  });
  // Sales are only counted since the collector started comparing snapshots,
  // so scale to a daily rate while there is less than a day of them.
  const hours = house.sales_since ? Math.min(24, Math.max(1, (S.updated - house.sales_since) / 3600 + 1)) : 0;
  S.salesHours = hours;
  for (const r of S.rows) {
    r.sig = signal(r);
    r.value = r.sold1 ? r.sold1 * r.market : null;
    r.stock = r.sold1 && hours ? r.qty / (r.sold1 * 24 / hours) : null;       // days of supply
  }
  S.byId = new Map(S.rows.map(r => [r.id, r]));
  renderChrome();
  render();
  renderDetail();
}

/* ------------------------------------------------------------ rendering -- */

function renderChrome() {
  const house = currentHouse(), meta = S.meta;
  const where = [meta.region.toUpperCase(), meta.ruleset.name].filter(Boolean).join(" ");
  $("#status").textContent = S.updated
    ? `${houseLabel(house)} auction house on ${where}. Prices refreshed ${ago(S.updated)}.`
    : `${houseLabel(house)} auction house on ${where}. No snapshot yet.`;
  document.title = `Auction ledger: ${houseLabel(house)}, ${where}`;

  $("#houses").replaceChildren(...meta.houses.map(x => h("button", {
    type: "button", "aria-pressed": String(x.id === S.house),
    onclick: () => { S.house = x.id; S.limit = 100; writeHash(); loadHouse(); },
  }, h("span", { class: `flag ${x.faction}` }), houseLabel(x))));

  const notes = [];
  if (S.updated && nowSec() - S.updated > 3 * 3600) {
    notes.push(`These prices are ${ago(S.updated).replace(" ago", "")} old. The hourly collector may have stopped; the repository's Actions tab shows why.`);
  }
  const twins = meta.houses.filter(x => x.faction === house.faction).length;
  if (twins > 1) {
    notes.push(`Blizzard lists ${twins} ${houseLabel(house).split(" ")[0]} auction houses without saying which server each belongs to, so all of them are tracked for now. Compare a price you can see in game to find yours.`);
  }
  const notice = $("#notice");
  notice.hidden = !notes.length;
  notice.textContent = notes.join(" ");

  const missing = meta.names ? meta.names.missing : 0;
  $("#footnote").textContent = missing
    ? `${num(missing)} listed items have no name yet. Blizzard's item data for Forever is incomplete, so some names arrive late or not at all.`
    : "";
}

function sortRows(rows, tab) {
  const [key, dir] = S.sort[S.tab] || tab.sort;
  const val = COLS[key].val;
  const rank = r => { const v = r.listed === false && key !== "name" && !COLS[key].always ? null : val(r); return v == null || Number.isNaN(v) ? null : v; };
  return rows.slice().sort((a, b) => {
    const x = rank(a), y = rank(b);
    if (x == null || y == null) return (x == null) - (y == null);     // blanks last
    return (x < y ? -1 : x > y ? 1 : 0) * dir || a.name.localeCompare(b.name);
  });
}

function showEmpty(title, body) {
  $("#empty").replaceChildren(h("strong", { text: title }), body);
  $("#empty").hidden = false;
}

function emptyMessage(q) {
  const house = currentHouse();
  if (q) return [`Nothing matches “${q}”`, "Check the spelling, or try the All items view."];
  if (S.tab === "timing") {
    const days = Math.floor((currentHouse().snapshots || 0) / 24);
    return ["Item timing needs 3 days of history", `An hour of the day only counts once it has been seen on 3 different days. ${days} full day${days === 1 ? "" : "s"} collected so far.`];
  }
  if (S.tab === "volume") return ["No sales counted yet", "Sales are estimated by comparing each hourly snapshot with the one before, so the first figures appear after the next refresh."];
  if (S.tab === "prof" && !S.recipes) return ["Recipes could not be loaded", "Reload the page to try again."];
  if (S.tab === "prof" && S.skill[S.prof]) return [`Nothing to craft at ${S.prof} skill ${S.skill[S.prof]} yet`, "No recipe at or below that level has its materials or result on sale right now. Raise the level or clear the box to see everything."];
  if (S.tab === "prof") return ["No priced recipes yet", "None of this profession's materials or results are on sale right now."];
  if (S.tab === "watch") return ["Your watchlist is empty", "Star any item to keep it here. The list is saved in this browser only."];
  if (!S.rows.length) return ["No prices for this auction house yet", "The collector has not saved a snapshot for it. It runs about once an hour."];
  if (S.tab === "deals" && (house.snapshots || 0) < DEAL_MIN_POINTS) {
    return ["Deals need about a day of history",
      `An item needs ${DEAL_MIN_POINTS} hourly snapshots before there is a usual price to compare with. ${house.snapshots || 0} collected so far.`];
  }
  if (S.tab === "deals") return ["No deals right now", `Nothing is listed ${S.discount}% or more below its usual low. Lower the threshold or look again after the next refresh.`];
  if (S.tab === "vendor") return ["Nothing below vendor price right now", "These are rare and go fast. Vendor prices are only known for items Blizzard's data names."];
  return ["Nothing to show", ""];
}

function goTab(key) {
  S.tab = key;
  S.limit = 100;
  writeHash();
  render();
}

function render() {
  const tab = TABS[S.tab];
  const shown = S.tab === "vendor" ? "deals" : S.tab;       // vendor flips live under Deals

  $("#tabs").replaceChildren(...Object.entries(TABS).filter(([, t]) => !t.hidden).map(([key, t]) => h("button", {
    type: "button", role: "tab", "aria-selected": String(key === shown),
    onclick: () => goTab(key),
  }, t.label, t.count && S.rows.length ? h("span", { class: "count", text: num(t.rows().length) }) : null)));

  const isHome = S.tab === "home";
  $("#home").hidden = !isHome;
  $(".list-head").hidden = isHome;
  $("#top").replaceChildren(...(tab.top ? tab.top() : []));
  if (isHome) {
    $("#home").replaceChildren(...renderHome());
    $("#table").hidden = true;
    $("#empty").hidden = true;
    $("#more").hidden = true;
    return;
  }

  $("#blurb").textContent = tab.blurb();
  $("#controls").replaceChildren(...(S.tab === "deals" ? [
    h("label", { for: "discount", text: "At least" }),
    h("select", { id: "discount", onchange: e => { S.discount = +e.target.value; render(); } },
      [10, 20, 30, 50].map(v => h("option", { value: v, selected: v === S.discount, text: `${v}% below usual` }))),
    h("button", { class: "quiet", type: "button", onclick: () => goTab("vendor"),
      text: `Below vendor price (${num(TABS.vendor.rows().length)})` }),
  ] : S.tab === "vendor" ? [
    h("button", { class: "quiet", type: "button", onclick: () => goTab("deals"), text: "Back to deals" }),
  ] : S.tab === "prof" && S.recipes ? [
    h("label", { for: "skill", text: `My ${S.prof} skill` }),
    h("input", { id: "skill", type: "text", inputmode: "numeric", maxlength: "3", placeholder: "Any", autocomplete: "off",
      value: S.skill[S.prof] || "", title: "Show only recipes you can craft at this skill level",
      oninput: e => setSkill(e.target.value.replace(/\D/g, "")) }),
  ] : []));

  const q = S.q.trim().toLowerCase();
  let rows = tab.rows();
  if (q) {
    const all = rows;
    rows = all.filter(r => r.name.toLowerCase().includes(q) || String(r.id) === q);
    // "Thick Scale Breastplate of Magic": random suffixes are listed under the
    // base item, so when the full name finds nothing, look for the base name.
    const cut = q.lastIndexOf(" of ");
    if (!rows.length && cut > 0) {
      const base = q.slice(0, cut).trim();
      rows = all.filter(r => r.name.toLowerCase() === base);
      if (rows.length) {
        $("#blurb").textContent = `Random suffixes such as “${S.q.trim().slice(cut + 1)}” are priced together under ${rows[0].name}, so its prices cover every suffix on sale.`;
      }
    }
  }
  rows = sortRows(rows, tab);

  const [sortKey, sortDir] = S.sort[S.tab] || tab.sort;
  $("#table thead").replaceChildren(h("tr", null, tab.cols.map(key => {
    const col = COLS[key];
    const th = h("th", { class: [col.num && "num", col.cls].filter(Boolean).join(" "), scope: "col" });
    if (!col.val) { th.append(col.label || h("span", { class: "sr", text: "Watch" })); return th; }
    if (key === sortKey) th.setAttribute("aria-sort", sortDir > 0 ? "ascending" : "descending");
    th.append(h("button", { type: "button", text: col.label, onclick: () => {
      S.sort[S.tab] = [key, key === sortKey ? -sortDir : (col.first || -1)];
      render();
    } }));
    return th;
  })));

  $("#table tbody").replaceChildren(...rows.slice(0, S.limit).map(row => h("tr", {
    class: row.id === S.item ? "on" : null, "data-id": row.id,
  }, tab.cols.map(key => {
    const col = COLS[key];
    const blank = row.listed === false && !["star", "name"].includes(key) && !col.always;
    return h("td", { class: [col.num && "num", col.cls].filter(Boolean).join(" ") },
      blank ? h("span", { class: "dim", text: "–" }) : col.cell(row));
  }))));

  $("#table").hidden = !rows.length;
  $("#empty").hidden = true;
  if (!rows.length) showEmpty(...emptyMessage(S.q.trim()));
  $("#more").hidden = rows.length <= S.limit;
  $("#more").textContent = `Show more (${num(rows.length - S.limit)} left)`;
}

/* --------------------------------------------------------------- detail -- */

/* Buy / hold / sell signal: how today's lowest price compares with the item's
   past 7 days. 60% is the distance from its usual low (30% away is the full
   swing), 40% is where it sits between the week's low and high. It describes
   the price now; it does not predict where the price goes next. */
function signal(row) {
  if (!row.listed || row.n7 < DEAL_MIN_POINTS || !row.m7) return null;
  const versusUsual = Math.max(-1, Math.min(1, row.discount / 0.3));
  const span = row.hi7 - row.lo7;
  const inRange = span > 0 ? 1 - 2 * (row.low - row.lo7) / span : 0;
  const score = 0.6 * versusUsual + 0.4 * inRange;
  const [label, tone] = score >= 0.5 ? ["Buy", "buy"] : score >= 0.2 ? ["Lean buy", "buy"]
    : score > -0.2 ? ["Hold", "hold"] : score > -0.5 ? ["Lean sell", "sell"] : ["Sell", "sell"];
  const pct = Math.min(99, Math.abs(Math.round(row.discount * 100)));
  const versus = pct < 5 ? `close to its usual low of ${moneyText(row.m7)}`
    : `${pct}% ${row.discount > 0 ? "below" : "above"} its usual low of ${moneyText(row.m7)}`;
  const where = span <= 0 ? "" : inRange > 0.5 ? ", near the bottom of its 7-day range"
    : inRange < -0.5 ? ", near the top of its 7-day range" : ", mid-range for the week";
  return { score, label, tone, why: `${versus[0].toUpperCase()}${versus.slice(1)}${where}.` };
}

function meter(row) {
  const svg = (tag, attrs) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };
  const sig = row.sig === undefined ? signal(row) : row.sig;
  const cx = 110, cy = 104, r = 84;
  const at = (deg, rad) => [cx + rad * Math.cos(deg * Math.PI / 180), cy - rad * Math.sin(deg * Math.PI / 180)];
  const dial = svg("svg", { viewBox: "0 0 220 122", class: "dial", "aria-hidden": "true" });
  ["sell", "sell soft", "hold", "buy soft", "buy"].forEach((tone, i) => {
    const [x1, y1] = at(180 - i * 36 - 1.5, r), [x2, y2] = at(180 - (i + 1) * 36 + 1.5, r);
    dial.append(svg("path", { d: `M${x1} ${y1} A${r} ${r} 0 0 1 ${x2} ${y2}`, class: `arc ${tone}` }));
  });
  if (sig) {
    const [nx, ny] = at(90 - sig.score * 90, r - 24);
    dial.append(svg("line", { x1: cx, y1: cy, x2: nx, y2: ny, class: "needle" }), svg("circle", { cx, cy, r: 6, class: "hub" }));
  }
  const ends = h("div", { class: "dial-ends" }, h("span", { text: "Sell" }), h("span", { text: "Hold" }), h("span", { text: "Buy" }));
  return h("div", { class: "meter", role: "img", "aria-label": sig ? `Signal: ${sig.label}. ${sig.why}` : "Signal: not enough history yet" },
    h("div", { class: "dial-wrap" }, dial, ends),
    h("div", { class: "meter-text" },
      h("p", { class: `meter-label ${sig ? sig.tone : ""}`, text: sig ? sig.label : "No signal yet" }),
      h("p", { class: "meter-why", text: sig ? sig.why : verdict(row) }),
      h("p", { class: "meter-note", text: "Compares today's price with the past 7 days. It is not a forecast." })));
}

function verdict(row) {
  if (!row.listed) return "Not on sale right now. The chart shows its past listings.";
  if (row.n7 < DEAL_MIN_POINTS) return "Still gathering history, so there is no usual price to compare with yet.";
  const pct = Math.min(99, Math.round(row.discount * 100));
  if (pct >= 10) return `${pct}% below its usual low of ${moneyText(row.m7)}.`;
  if (pct <= -10) return `${-pct}% above its usual low of ${moneyText(row.m7)}.`;
  return `Close to its usual low of ${moneyText(row.m7)}.`;
}

function openItem(id) {
  S.item = id;
  writeHash();
  render();
  renderDetail();
}

function recipeBlock([prof, [, skill, lo, hi, flat, needs]]) {
  const makes = lo === hi ? (lo > 1 ? `, makes ${lo}` : "") : `, makes ${lo} to ${hi}`;
  const mats = [];
  let total = 0, complete = true;
  for (let k = 0; k < flat.length; k += 2) {
    const id = flat[k], count = flat[k + 1], price = matPrice(id, "market");
    const row = S.byId.get(id), vendor = S.recipes.vendor[id];
    const fromVendor = price != null && vendor != null && (!row || vendor <= row.market);
    if (price == null) complete = false; else total += price * count;
    mats.push(h("li", null,
      h("button", { class: "item", type: "button", onclick: () => openItem(id) },
        h("span", { text: `${count} × ${nameOf(id)}` }),
        fromVendor ? h("span", { class: "sub", text: "vendor" }) : null),
      price == null ? h("span", { class: "dim", text: "not on sale" }) : money(price * count)));
  }
  return h("div", { class: "recipe" },
    h("p", { class: "chart-title", text: `${prof} recipe` }),
    h("p", { class: "kind", text: `${skill == null ? "Skill level not published" : `Skill ${skill}`}${makes}${needs ? `, requires ${needs}` : ""}` }),
    h("ul", { class: "mats" }, mats,
      h("li", { class: "sum" }, h("span", { text: complete ? "Materials" : "Materials on sale" }), money(total))));
}

/* "1g 50s", "18s", "2500c", "1.5g" or a plain number of gold -> copper. */
function parsePrice(raw) {
  const s = raw.toLowerCase().replace(/[,\s]/g, "");
  if (!s) return null;
  if (/^\d+(\.\d+)?$/.test(s)) return Math.round(parseFloat(s) * 10000);
  const parts = [...s.matchAll(/(\d+(?:\.\d+)?)([gsc])/g)];
  if (!parts.length || parts.map(m => m[0]).join("") !== s) return null;
  const total = parts.reduce((sum, m) => sum + parseFloat(m[1]) * { g: 10000, s: 100, c: 1 }[m[2]], 0);
  return total > 0 ? Math.round(total) : null;
}
const plainMoney = c => moneyText(c).replace(/,/g, "");

/* Telegram alert: builds a link that opens the bot with the alert filled in. */
function alertBox(row) {
  const bot = S.meta && S.meta.telegram;
  if (!bot || currentHouse().faction !== "horde") return null;
  const dir = h("select", { "aria-label": "Alert when the lowest price is" },
    h("option", { value: "b", text: "drops below" }), h("option", { value: "a", text: "rises above" }));
  const price = h("input", { type: "text", class: "alert-price", "aria-label": "Price",
    value: row.listed ? plainMoney(row.low) : "", placeholder: "1g 50s" });
  const go = h("a", { class: "quiet alert-go", target: "_blank", rel: "noopener", text: "Set in Telegram" });
  const note = h("p", { class: "alert-note" });
  const update = () => {
    const copper = parsePrice(price.value);
    if (copper == null) {
      go.removeAttribute("href");
      go.setAttribute("aria-disabled", "true");
      note.textContent = price.value.trim() ? "Write the price like 1g 50s, 18s or 2500c." : "Enter a price.";
      return;
    }
    go.href = `https://t.me/${bot}?start=a_${row.id}_${dir.value}_${copper}`;
    go.removeAttribute("aria-disabled");
    note.textContent = `Telegram opens the bot; tap Start to save the alert for ${moneyText(copper)}. You can also type /alert to it.`;
  };
  dir.addEventListener("change", update);
  price.addEventListener("input", update);
  update();
  return h("div", { class: "alert-box" },
    h("p", { class: "chart-title", text: "Price alert" }),
    h("div", { class: "alert-row" }, h("span", { text: "Message me when the lowest price" }), dir, price, go),
    note);
}

function closeItem() {
  S.item = null;
  writeHash();
  render();
  renderDetail();
}

function renderDetail(keepCharts) {
  const box = $("#detail");
  if (S.item == null) {
    destroyCharts();
    box.hidden = true;
    $("#layout").classList.remove("split");
    return;
  }
  const row = S.byId.get(S.item) || unlisted(S.item);
  const watched = S.watch.has(row.id);
  const fact = (label, value) => h("div", null, h("dt", { text: label }), h("dd", null, value));

  const head = [
    h("div", { class: "detail-head" },
      h("div", null, h("h2", { text: row.name }), h("p", { class: "kind", text: row.kind })),
      h("button", { class: "close", type: "button", "aria-label": "Close item details", text: "×", onclick: closeItem })),
    meter(row),
    h("div", { class: "actions" },
      h("button", { class: "quiet", type: "button", "aria-pressed": String(watched),
        text: watched ? "★ Watching" : "☆ Watch", onclick: () => toggleWatch(row.id) }),
      h("a", { href: `https://www.wowhead.com/forever/item=${row.id}`, target: "_blank", rel: "noopener",
        text: "Look it up on Wowhead" })),
    alertBox(row),
    row.listed ? h("dl", { class: "facts" },
      fact("Lowest now", money(row.low)),
      fact("Market now", money(row.market)),
      fact("On sale", `${num(row.qty)} in ${num(row.listings)} listing${row.listings === 1 ? "" : "s"}`),
      row.sold1 != null && S.salesHours ? fact(S.salesHours >= 24 ? "Sold in 24 hours" : `Sold in ${Math.round(S.salesHours)} hours`, `about ${num(row.sold1)}`) : null,
      row.soldPrice ? fact("Last sold for", money(row.soldPrice)) : null,
      row.sell ? fact("Vendor pays", money(row.sell)) : null) : null,
    ...recipesFor(row.id).map(recipeBlock),
    row.listed && row.n7 > 1 ? h("table", { class: "spread" },
      h("thead", null, h("tr", null, h("th", null, h("span", { class: "sr", text: "Period" })),
        ...["Low", "Usual", "High"].map(t => h("th", { class: "num", scope: "col", text: t })))),
      h("tbody", null,
        h("tr", null, h("th", { scope: "row", text: "Past 7 days" }),
          ...[row.lo7, row.m7, row.hi7].map(v => h("td", { class: "num" }, money(v)))),
        row.n30 > row.n7 ? h("tr", null, h("th", { scope: "row", text: "Past 30 days" }),
          ...[row.lo30, row.m30, row.hi30].map(v => h("td", { class: "num" }, money(v)))) : null)) : null,
  ];

  const parts = head.filter(Boolean);
  if (keepCharts && box.dataset.id === String(row.id) && $("#charts", box)) {
    // Only the header changed (watch toggled): keep the drawn charts in place.
    const charts = $("#charts", box);
    box.replaceChildren(...parts, charts);
    return;
  }

  destroyCharts();
  box.dataset.id = row.id;
  box.replaceChildren(...parts, h("div", { id: "charts" },
    h("div", { class: "chart-head" },
      h("span", { class: "chart-title", text: "Price per unit" }),
      h("div", { class: "ranges", role: "group", "aria-label": "Chart period" },
        RANGES.map(([key, label]) => h("button", { type: "button", "data-range": key,
          "aria-pressed": String(key === S.range), text: label,
          onclick: () => { S.range = key; writeHash(); drawItem(); } })))),
    h("div", { class: "legend" },
      h("span", null, h("i", { class: "key s1" }), "Lowest price"),
      h("span", null, h("i", { class: "key s2" }), "Market price"),
      recipesFor(row.id).length ? h("span", null, h("i", { class: "key s3" }), "Cost to craft") : null),
    tzLine(),
    h("div", { id: "priceChart", class: "chart", role: "img", "aria-label": `Price history for ${row.name}` }),
    h("p", { class: "small-title", text: "Units on sale" }),
    h("div", { id: "qtyChart", class: "chart", role: "img", "aria-label": `Units on sale for ${row.name}` }),
    h("p", { class: "small-title", text: "Estimated units sold" }),
    h("div", { id: "soldChart", class: "chart", role: "img", "aria-label": `Estimated units sold for ${row.name}` }),
    h("div", { id: "hoursBox" })));
  itemHours(row.id);
  box.hidden = false;
  $("#layout").classList.add("split");
  drawItem();
}

/* --------------------------------------------------------------- charts -- */

/* What one unit cost to craft at each point of `hist`: every material at its
   market price then (or the vendor price when cheaper), divided by the yield.
   A material keeps its last known price while it is off the market. */
async function craftCost([, , lo, hi, flat], hist, range) {
  const makes = (lo + hi) / 2;
  const mats = [];
  for (let k = 0; k < flat.length; k += 2) mats.push({ id: flat[k], count: flat[k + 1] });
  await Promise.all(mats.map(async m => {
    m.vendor = S.recipes.vendor[m.id];
    m.hist = await loadHistory(m.id, range).catch(() => null);
    m.at = 0;
    m.last = null;
  }));
  if (!hist.t.length) {
    // The crafted item was never on sale in this period: use the materials' timeline.
    const all = new Set();
    for (const m of mats) if (m.hist) m.hist.t.forEach(ts => all.add(ts));
    hist.t = [...all].sort((a, b) => a - b);
    for (const key of ["low", "market", "sold"]) hist[key] = hist.t.map(() => null);
    hist.qty = hist.t.map(() => 0);
  }
  hist.cost = hist.t.map(ts => {
    let total = 0;
    for (const m of mats) {
      while (m.hist && m.at < m.hist.t.length && m.hist.t[m.at] <= ts) {
        if (m.hist.market[m.at] != null) m.last = m.hist.market[m.at];
        m.at++;
      }
      const price = m.vendor == null ? m.last : m.last == null ? m.vendor : Math.min(m.last, m.vendor);
      if (price == null) return null;
      total += price * m.count;
    }
    return Math.round(total / makes);
  });
}

async function loadHistory(id, range) {
  const house = currentHouse();
  const shard = pad(id % S.meta.shards);
  const now = nowSec();
  const first = house.first || now;
  const out = { t: [], low: [], market: [], qty: [], sold: [], daily: range === "all" };
  const key = String(id);

  if (range === "all") {
    const months = [];
    const start = new Date(first * 1000);
    for (let d = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), 1)); d.getTime() <= Date.now();
      d.setUTCMonth(d.getUTCMonth() + 1)) months.push(d.toISOString().slice(0, 7));
    const docs = await Promise.all(months.map(m =>
      getJSON(`data/${S.house}/d/${m}/${shard}.json`, FRESH).catch(() => null)));
    docs.forEach((doc, i) => {
      const flat = doc && doc.i[key];
      if (!flat) return;
      const [year, month] = months[i].split("-").map(Number);
      const rows = [];
      const n = doc.n || 6;                       // older files have no sales figure
      for (let k = 0; k + n - 1 < flat.length; k += n) rows.push(flat.slice(k, k + n));
      rows.sort((a, b) => a[0] - b[0]);
      for (const r of rows) {
        out.t.push(Date.UTC(year, month - 1, r[0], 12) / 1000);
        out.low.push(r[1]); out.market.push(r[3]); out.qty.push(r[4]); out.sold.push(r[6] || 0);
      }
    });
    const cur = S.byId.get(id);                 // today so far
    if (cur && S.updated > (out.t[out.t.length - 1] || 0)) {
      out.t.push(S.updated); out.low.push(cur.low); out.market.push(cur.market); out.qty.push(cur.qty);
      out.sold.push(null);                        // today is not finished
    }
    return out;
  }

  const days = RANGES.find(r => r[0] === range)[2];
  const from = now - days * 86400;
  const list = [];
  for (let ts = Math.max(from, first); dayStr(ts) < dayStr(now); ts += 86400) list.push(dayStr(ts));
  list.push(dayStr(now));
  const docs = await Promise.all(list.map((day, i) =>
    getJSON(`data/${S.house}/h/${day}/${shard}.json`, i === list.length - 1 ? FRESH : undefined).catch(() => null)));
  let seen = false;
  for (const doc of docs) {
    if (!doc) continue;
    const flat = doc.i[key] || [];
    const at = new Map();
    const n = doc.n || 4;
    for (let k = 0; k + n - 1 < flat.length; k += n) at.set(flat[k], k);
    doc.t.forEach((ts, idx) => {
      if (ts < from) return;
      const k = at.get(idx);
      if (k == null && !seen) return;           // before its first listing
      seen = true;
      out.t.push(ts);
      out.low.push(k == null ? null : flat[k + 1]);
      out.market.push(k == null ? null : flat[k + 2]);
      out.qty.push(k == null ? 0 : flat[k + 3]);
      out.sold.push(k == null || n < 5 ? 0 : flat[k + 4]);
    });
  }
  return out;
}

function destroyCharts() {
  S.charts.forEach(c => c.destroy());
  S.charts = [];
  if (S.observer) { S.observer.disconnect(); S.observer = null; }
}

function tipPlugin(build) {
  let tip;
  return { hooks: {
    init: u => { tip = h("div", { class: "tip", hidden: true }); u.over.append(tip); },
    setCursor: u => {
      const { idx, left } = u.cursor;
      if (idx == null || left == null || left < 0) { tip.hidden = true; return; }
      tip.replaceChildren(...build(idx));
      tip.hidden = false;
      const width = tip.offsetWidth, room = u.over.clientWidth;
      tip.style.left = (left + 14 + width > room ? Math.max(-50, left - 14 - width) : left + 14) + "px";
      tip.style.top = "4px";
    },
  } };
}

async function drawItem() {
  const id = S.item, range = S.range, ticket = ++S.drawId;
  const priceBox = $("#priceChart"), qtyBox = $("#qtyChart");
  if (!priceBox) return;
  document.querySelectorAll(".ranges button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.range === range)));
  priceBox.classList.add("loading");
  qtyBox.classList.add("loading");
  let hist;
  try {
    [hist] = await Promise.all([loadHistory(id, range), document.fonts ? document.fonts.ready : null]);
    const recipe = recipesFor(id)[0];
    if (recipe) await craftCost(recipe[1], hist, range);
  } catch {
    hist = null;
  }
  if (ticket !== S.drawId || !priceBox.isConnected) return;     // a newer request won
  destroyCharts();
  priceBox.classList.remove("loading");
  qtyBox.classList.remove("loading");
  const note = text => {
    priceBox.replaceChildren(h("p", { class: "chart-note", text }));
    qtyBox.replaceChildren();
    $("#soldChart").replaceChildren();
  };
  if (!hist) return note("The price history could not be loaded. Check your connection and pick the period again.");
  if (![...hist.low, ...(hist.cost || [])].some(v => v != null)) {
    return note(range === "all" ? "No history yet. The first daily point appears after a full day of collecting."
      : "No listings in this period. Try a longer one.");
  }
  priceBox.replaceChildren();
  qtyBox.replaceChildren();

  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  const axis = { stroke: v("--muted"), font: `13px ${v("--sans")}`,
    grid: { stroke: v("--line"), width: 1 }, ticks: { stroke: v("--line"), width: 1, size: 4 } };
  const width = priceBox.clientWidth;
  const sync = { key: "ledger" };
  const when = idx => new Date(hist.t[idx] * 1000).toLocaleString("en-GB", hist.daily
    ? { weekday: "short", day: "numeric", month: "short" }
    : { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const span = hist.t[hist.t.length - 1] - hist.t[0];
  const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
  const hourFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
  const xAxis = { ...axis, space: 64, values: (u, splits) => splits.map(s => {
    const d = new Date(s * 1000);
    if (d.getHours() === 0 && d.getMinutes() === 0) return dayFmt.format(d);
    return span > 2 * 86400 ? "" : hourFmt.format(d);
  }) };
  const tipRow = (cls, value, label) => h("div", { class: "row" },
    cls ? h("i", { class: `key ${cls}` }) : null, h("strong", { text: value }), h("em", { text: label }));

  const price = new uPlot({
    width, height: 230, legend: { show: false },
    cursor: { sync, y: false, points: { size: 8, width: 2 } },
    axes: [xAxis, { ...axis, size: 58, values: (u, vals) => vals.map(axisMoney) }],
    series: [{},
      { label: "Lowest price", stroke: v("--series-1"), width: 2, spanGaps: true },
      { label: "Market price", stroke: v("--series-2"), width: 2, spanGaps: true },
      ...(hist.cost ? [{ label: "Cost to craft", stroke: v("--series-3"), width: 2, dash: [6, 4], spanGaps: true }] : [])],
    plugins: [tipPlugin(idx => [
      h("div", { class: "when", text: when(idx) }),
      tipRow("s1", moneyText(hist.low[idx]), "lowest"),
      tipRow("s2", moneyText(hist.market[idx]), "market"),
      hist.cost ? tipRow("s3", moneyText(hist.cost[idx]), "cost to craft") : null,
      tipRow(null, num(hist.qty[idx] || 0), hist.daily ? "units on sale, daily average" : "units on sale"),
      hist.sold[idx] == null ? null : tipRow(null, num(hist.sold[idx]), hist.daily ? "sold that day, estimated" : "sold in the hour before, estimated"),
    ])],
  }, [hist.t, hist.low, hist.market, ...(hist.cost ? [hist.cost] : [])], priceBox);

  const bars = (box, label, values, color) => new uPlot({
    width, height: 110, legend: { show: false },
    cursor: { sync, y: false, points: { show: false } },
    scales: { y: { range: (u, min, max) => [0, Math.max(1, max) * 1.1] } },
    axes: [xAxis, { ...axis, size: 58, values: (u, vals) => vals.map(n => Number.isInteger(n) ? num(n) : "") }],
    series: [{}, { label, stroke: v(color), fill: v(color) + "8c", width: 1,
      paths: uPlot.paths.bars({ size: [0.7, 24, 1] }), points: { show: false } }],
  }, [hist.t, values], box);
  const qty = bars(qtyBox, "Units on sale", hist.qty, "--bar");
  const soldBox = $("#soldChart");
  soldBox.replaceChildren();
  const sold = bars(soldBox, "Estimated units sold", hist.sold, "--series-4");

  S.charts = [price, qty, sold];
  S.observer = new ResizeObserver(() => {
    const w = priceBox.clientWidth;
    if (!w || w === price.width) return;
    price.setSize({ width: w, height: 230 });
    qty.setSize({ width: w, height: 110 });
    sold.setSize({ width: w, height: 110 });
  });
  S.observer.observe(priceBox);
}

/* --------------------------------------------------------------- events -- */

function bind() {
  const q = $("#q");
  q.value = S.q;
  q.addEventListener("input", () => {
    S.q = q.value;
    S.limit = 100;
    if (S.tab === "home" && S.q.trim()) S.tab = "all";       // a search from Home looks through everything
    writeHash();
    render();
  });
  // Created here so an older cached page still gets them.
  const list = $(".list");
  if (!$("#home")) list.insertBefore(h("div", { id: "home", class: "home", hidden: true }), list.firstChild);
  if (!$("#top")) list.insertBefore(h("div", { id: "top" }), $(".tablewrap"));
  $("#more").addEventListener("click", () => { S.limit += 200; render(); });
  $("#table tbody").addEventListener("click", e => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    openItem(+tr.dataset.id);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && S.item != null) closeItem(); });
  window.addEventListener("hashchange", () => { readHash(); loadHouse(); });
  setInterval(() => { if (S.meta) renderChrome(); }, 60000);     // keep "refreshed n minutes ago" honest
  // Guarded: a browser can briefly pair a cached older page with this script.
  if ($("#refresh")) $("#refresh").addEventListener("click", refreshClicked);
  // Pick up new prices without a reload while the page is left open.
  setInterval(() => { if (!document.hidden) refresh(false).catch(() => {}); }, 5 * 60000);
}

boot();
})();
