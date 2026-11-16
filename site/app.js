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
  tab: "deals", q: "", sort: {}, limit: 100, discount: 20,
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
}
function writeHash() {
  const p = new URLSearchParams();
  p.set("h", S.house);
  p.set("t", S.tab);
  if (S.item != null) { p.set("i", S.item); p.set("r", S.range); }
  if (S.q) p.set("q", S.q);
  history.replaceState(null, "", "#" + p);
}
function loadWatch() {
  try { S.watch = new Set(JSON.parse(localStorage.getItem("ledger.watch") || "[]")); } catch { /* private mode */ }
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
  vprofit: { label: "Profit each", num: 1, cell: r => money(r.vprofit), val: r => r.vprofit },
};

const TABS = {
  deals: {
    label: "Deals", count: true,
    cols: ["star", "name", "low", "usual", "discount", "near", "profit"], sort: ["discount", -1],
    rows: () => S.rows.filter(r => r.n7 >= DEAL_MIN_POINTS && r.discount >= S.discount / 100 && r.profit > 0),
    blurb: () => `Listed well below the item's usual low for the past 7 days. Resale margin is what is left per unit if you relist at the usual market price and pay the ${Math.round(currentHouse().cut * 100)}% auction cut.`,
  },
  vendor: {
    label: "Vendor flips", count: true,
    cols: ["star", "name", "low", "sell", "vprofit", "near"], sort: ["vprofit", -1],
    rows: () => S.rows.filter(r => r.vprofit > 0),
    blurb: () => "Listed for less than a vendor pays. Buy it, sell it to any vendor, keep the difference.",
  },
  all: {
    label: "All items",
    cols: ["star", "name", "low", "market", "change", "range", "qty"], sort: ["qty", -1],
    rows: () => S.rows,
    blurb: () => "Everything on sale right now. Lowest is the cheapest buyout per unit; market averages the cheapest 15% of units, so one stray listing does not move it.",
  },
  watch: {
    label: "Watchlist",
    cols: ["star", "name", "low", "market", "change", "range", "qty"], sort: ["name", 1],
    rows: () => [...S.watch].map(id => S.byId.get(id) || unlisted(id)),
    blurb: () => "Items you starred. Saved in this browser only.",
  },
};

function unlisted(id) {
  const it = S.items[id];
  return { id, listed: false, name: it ? it[0] : `Item ${id}`, quality: it ? it[1] : -1, n7: 0, n30: 0,
    sell: it ? it[5] : 0, kind: kindOf(it) };
}
function kindOf(it) {
  if (!it) return "No name from Blizzard yet";
  const type = [it[3], it[2]].filter(Boolean).join(" ");
  return [QUALITY[it[1]], type.toLowerCase()].filter(Boolean).join(" ") + (it[4] ? `, item level ${it[4]}` : "");
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
      "The collector has not published any data. It runs once an hour; if this message stays, check the Actions tab of the repository for a failed run.");
    return;
  }
  S.items = await getJSON("data/items.json", FRESH).catch(() => ({}));
  if (!S.meta.houses.some(x => x.id === S.house)) {
    S.house = (S.meta.houses.find(x => x.faction === "horde") || S.meta.houses[0]).id;
  }
  bind();
  await loadHouse();
}

async function loadHouse() {
  const house = currentHouse();
  let doc = { cols: [], rows: [], updated: null };
  try { doc = await getJSON(`data/${S.house}/summary.json`, FRESH); } catch { /* no snapshot yet */ }
  const c = Object.fromEntries(doc.cols.map((name, i) => [name, i]));
  S.updated = doc.updated;
  S.rows = doc.rows.map(r => {
    const id = r[c.id], it = S.items[id];
    const low = r[c.low], m7 = r[c.low7dMed], mk7 = r[c.market7dMed], prev = r[c.low24hAgo];
    const sell = it ? it[5] : 0;
    return {
      id, listed: true, name: it ? it[0] : `Item ${id}`, quality: it ? it[1] : -1, kind: kindOf(it),
      low, market: r[c.market], qty: r[c.qty], near: r[c.near], listings: r[c.listings],
      lo7: r[c.low7dMin], m7, hi7: r[c.low7dMax], mk7, n7: r[c.points7d],
      lo30: r[c.low30dMin], m30: r[c.low30dMed], hi30: r[c.low30dMax], n30: r[c.points30d],
      sell, change: prev ? low / prev - 1 : null,
      discount: m7 ? 1 - low / m7 : 0,
      profit: Math.round(mk7 * (1 - house.cut)) - low,
      vprofit: sell ? sell - low : null,
    };
  });
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
  const rank = r => { const v = r.listed === false && key !== "name" ? null : val(r); return v == null || Number.isNaN(v) ? null : v; };
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

function render() {
  const tab = TABS[S.tab];

  $("#tabs").replaceChildren(...Object.entries(TABS).map(([key, t]) => h("button", {
    type: "button", role: "tab", "aria-selected": String(key === S.tab),
    onclick: () => { S.tab = key; S.limit = 100; writeHash(); render(); },
  }, t.label, t.count && S.rows.length ? h("span", { class: "count", text: num(t.rows().length) }) : null)));

  $("#blurb").textContent = tab.blurb();
  $("#controls").replaceChildren(...(S.tab === "deals" ? [
    h("label", { for: "discount", text: "At least" }),
    h("select", { id: "discount", onchange: e => { S.discount = +e.target.value; render(); } },
      [10, 20, 30, 50].map(v => h("option", { value: v, selected: v === S.discount, text: `${v}% below usual` }))),
  ] : []));

  const q = S.q.trim().toLowerCase();
  let rows = tab.rows();
  if (q) rows = rows.filter(r => r.name.toLowerCase().includes(q) || String(r.id) === q);
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
    const blank = row.listed === false && !["star", "name"].includes(key);
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

function verdict(row) {
  if (!row.listed) return "Not on sale right now. The chart shows its past listings.";
  if (row.n7 < DEAL_MIN_POINTS) return "Still gathering history, so there is no usual price to compare with yet.";
  const pct = Math.min(99, Math.round(row.discount * 100));
  if (pct >= 10) return `${pct}% below its usual low of ${moneyText(row.m7)}.`;
  if (pct <= -10) return `${-pct}% above its usual low of ${moneyText(row.m7)}.`;
  return `Close to its usual low of ${moneyText(row.m7)}.`;
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
    h("p", { class: "verdict", text: verdict(row) }),
    h("div", { class: "actions" },
      h("button", { class: "quiet", type: "button", "aria-pressed": String(watched),
        text: watched ? "★ Watching" : "☆ Watch", onclick: () => toggleWatch(row.id) }),
      h("a", { href: `https://www.wowhead.com/forever/item=${row.id}`, target: "_blank", rel: "noopener",
        text: "Look it up on Wowhead" })),
    row.listed ? h("dl", { class: "facts" },
      fact("Lowest now", money(row.low)),
      fact("Market now", money(row.market)),
      fact("On sale", `${num(row.qty)} in ${num(row.listings)} listing${row.listings === 1 ? "" : "s"}`),
      row.sell ? fact("Vendor pays", money(row.sell)) : null) : null,
    row.listed && row.n7 > 1 ? h("table", { class: "spread" },
      h("thead", null, h("tr", null, h("th", null, h("span", { class: "sr", text: "Period" })),
        ...["Low", "Usual", "High"].map(t => h("th", { class: "num", scope: "col", text: t })))),
      h("tbody", null,
        h("tr", null, h("th", { scope: "row", text: "Past 7 days" }),
          ...[row.lo7, row.m7, row.hi7].map(v => h("td", { class: "num" }, money(v)))),
        row.n30 > row.n7 ? h("tr", null, h("th", { scope: "row", text: "Past 30 days" }),
          ...[row.lo30, row.m30, row.hi30].map(v => h("td", { class: "num" }, money(v)))) : null)) : null,
  ];

  if (keepCharts && box.dataset.id === String(row.id) && $("#charts", box)) {
    // Only the header changed (watch toggled): keep the drawn charts in place.
    const charts = $("#charts", box);
    box.replaceChildren(...head, charts);
    return;
  }

  destroyCharts();
  box.dataset.id = row.id;
  box.replaceChildren(...head, h("div", { id: "charts" },
    h("div", { class: "chart-head" },
      h("span", { class: "chart-title", text: "Price per unit" }),
      h("div", { class: "ranges", role: "group", "aria-label": "Chart period" },
        RANGES.map(([key, label]) => h("button", { type: "button", "data-range": key,
          "aria-pressed": String(key === S.range), text: label,
          onclick: () => { S.range = key; writeHash(); drawItem(); } })))),
    h("div", { class: "legend" },
      h("span", null, h("i", { class: "key s1" }), "Lowest price"),
      h("span", null, h("i", { class: "key s2" }), "Market price")),
    h("div", { id: "priceChart", class: "chart", role: "img", "aria-label": `Price history for ${row.name}` }),
    h("p", { class: "small-title", text: "Units on sale" }),
    h("div", { id: "qtyChart", class: "chart", role: "img", "aria-label": `Units on sale for ${row.name}` })));
  box.hidden = false;
  $("#layout").classList.add("split");
  drawItem();
}

/* --------------------------------------------------------------- charts -- */

async function loadHistory(id, range) {
  const house = currentHouse();
  const shard = pad(id % S.meta.shards);
  const now = nowSec();
  const first = house.first || now;
  const out = { t: [], low: [], market: [], qty: [], daily: range === "all" };
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
      for (let k = 0; k + 5 < flat.length; k += 6) rows.push(flat.slice(k, k + 6));
      rows.sort((a, b) => a[0] - b[0]);
      for (const r of rows) {
        out.t.push(Date.UTC(year, month - 1, r[0], 12) / 1000);
        out.low.push(r[1]); out.market.push(r[3]); out.qty.push(r[4]);
      }
    });
    const cur = S.byId.get(id);                 // today so far
    if (cur && S.updated > (out.t[out.t.length - 1] || 0)) {
      out.t.push(S.updated); out.low.push(cur.low); out.market.push(cur.market); out.qty.push(cur.qty);
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
    for (let k = 0; k + 3 < flat.length; k += 4) at.set(flat[k], k);
    doc.t.forEach((ts, idx) => {
      if (ts < from) return;
      const k = at.get(idx);
      if (k == null && !seen) return;           // before its first listing
      seen = true;
      out.t.push(ts);
      out.low.push(k == null ? null : flat[k + 1]);
      out.market.push(k == null ? null : flat[k + 2]);
      out.qty.push(k == null ? 0 : flat[k + 3]);
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
  } catch {
    hist = null;
  }
  if (ticket !== S.drawId || !priceBox.isConnected) return;     // a newer request won
  destroyCharts();
  priceBox.classList.remove("loading");
  qtyBox.classList.remove("loading");
  const note = text => { priceBox.replaceChildren(h("p", { class: "chart-note", text })); qtyBox.replaceChildren(); };
  if (!hist) return note("The price history could not be loaded. Check your connection and pick the period again.");
  if (!hist.low.some(v => v != null)) {
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
      { label: "Market price", stroke: v("--series-2"), width: 2, spanGaps: true }],
    plugins: [tipPlugin(idx => [
      h("div", { class: "when", text: when(idx) }),
      tipRow("s1", moneyText(hist.low[idx]), "lowest"),
      tipRow("s2", moneyText(hist.market[idx]), "market"),
      tipRow(null, num(hist.qty[idx] || 0), hist.daily ? "units on sale, daily average" : "units on sale"),
    ])],
  }, [hist.t, hist.low, hist.market], priceBox);

  const qty = new uPlot({
    width, height: 110, legend: { show: false },
    cursor: { sync, y: false, points: { show: false } },
    scales: { y: { range: (u, min, max) => [0, Math.max(1, max) * 1.1] } },
    axes: [xAxis, { ...axis, size: 58, values: (u, vals) => vals.map(n => Number.isInteger(n) ? num(n) : "") }],
    series: [{}, { label: "Units on sale", stroke: v("--series-3"), fill: v("--series-3") + "8c", width: 1,
      paths: uPlot.paths.bars({ size: [0.7, 24, 1] }), points: { show: false } }],
  }, [hist.t, hist.qty], qtyBox);

  S.charts = [price, qty];
  S.observer = new ResizeObserver(() => {
    const w = priceBox.clientWidth;
    if (w && w !== price.width) { price.setSize({ width: w, height: 230 }); qty.setSize({ width: w, height: 110 }); }
  });
  S.observer.observe(priceBox);
}

/* --------------------------------------------------------------- events -- */

function bind() {
  const q = $("#q");
  q.value = S.q;
  q.addEventListener("input", () => { S.q = q.value; S.limit = 100; writeHash(); render(); });
  $("#more").addEventListener("click", () => { S.limit += 200; render(); });
  $("#table tbody").addEventListener("click", e => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    S.item = +tr.dataset.id;
    writeHash();
    render();
    renderDetail();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && S.item != null) closeItem(); });
  window.addEventListener("hashchange", () => { readHash(); loadHouse(); });
  setInterval(() => { if (S.meta) renderChrome(); }, 60000);     // keep "refreshed n minutes ago" honest
}

boot();
})();
