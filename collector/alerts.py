#!/usr/bin/env python3
"""Telegram price alerts for the Forever-AH dashboard.

A listener job (.github/workflows/bot.yml) runs this all the time with
--listen: it waits on Telegram for messages, answers them within seconds,
re-reads the published prices every few minutes and messages whoever set an
alert when it fires. The collection job only calls it with --meta-only, to put
the bot's username on the dashboard.

  1. messages sent to the bot are acted on:
       /alert Thorium Bar below 18s     add an alert
       /alert 12359 above 1g 20s        by item id
       /list                            show your alerts
       /remove 2                        remove alert number 2
       /clear                           remove all of yours
       /start a_12359_b_1800            sent by the dashboard's alert button
  2. checks every alert against the newest prices and messages whoever set it.

An alert fires when the lowest price crosses its level, then stays quiet until
the price has gone back across by 3%, so a price sitting at the level does not
message every hour.

Alerts and the chats they belong to are kept in data/alerts.bin on the
"alerts" branch, encrypted with a key derived from the bot token, so chat ids
are not readable by visitors.

Needs the TELEGRAM_BOT_TOKEN secret; without it this script does nothing.
Any failure only warns, so prices keep publishing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect import read_json, write_json  # noqa: E402

MAX_PER_PERSON = 25
REARM = 0.03                # how far back across the level before an alert can fire again
HOUSE = 113                 # alerts watch the Horde auction house


# ------------------------------------------------------------- money ----

def parse_price(raw: str) -> int | None:
    """'1g 50s', '1g50s', '18s', '2500c', '1.5g' or a bare number of gold -> copper."""
    s = raw.lower().replace(",", "").replace(" ", "")
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return round(float(s) * 10000)
    parts = re.findall(r"(\d+(?:\.\d+)?)([gsc])", s)
    if not parts or "".join(n + u for n, u in parts) != s:
        return None
    total = sum(float(n) * {"g": 10000, "s": 100, "c": 1}[u] for n, u in parts)
    return round(total) if total > 0 else None


def money(copper: int) -> str:
    g, s, c = copper // 10000, copper % 10000 // 100, copper % 100
    out = []
    if g:
        out.append(f"{g:,}g")
    if s or (g and c):
        out.append(f"{s}s")
    if c or not out:
        out.append(f"{c}c")
    return " ".join(out)


# ----------------------------------------------------------- storage ----

def _keys(token: str) -> tuple[bytes, bytes]:
    root = hashlib.sha256(b"forever-ah-alerts:" + token.encode()).digest()
    return (hmac.new(root, b"enc", hashlib.sha256).digest(),
            hmac.new(root, b"mac", hashlib.sha256).digest())


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(state: dict, token: str) -> bytes:
    """Encrypt-then-MAC with an HMAC-SHA256 keystream (standard library only)."""
    enc, mac = _keys(token)
    plain = json.dumps(state, separators=(",", ":")).encode()
    nonce = secrets.token_bytes(16)
    body = bytes(a ^ b for a, b in zip(plain, _stream(enc, nonce, len(plain))))
    tag = hmac.new(mac, nonce + body, hashlib.sha256).digest()
    return base64.b64encode(nonce + tag + body)


def unseal(blob: bytes, token: str) -> dict | None:
    try:
        raw = base64.b64decode(blob)
    except ValueError:
        return None
    if len(raw) < 48:
        return None
    nonce, tag, body = raw[:16], raw[16:48], raw[48:]
    enc, mac = _keys(token)
    if not hmac.compare_digest(tag, hmac.new(mac, nonce + body, hashlib.sha256).digest()):
        return None
    plain = bytes(a ^ b for a, b in zip(body, _stream(enc, nonce, len(body))))
    return json.loads(plain)


# ---------------------------------------------------------- commands ----

HELP = ("I watch the Horde auction house and message you when a price crosses a level.\n\n"
        "/alert Thorium Bar below 18s\n"
        "/alert Arcane Crystal above 9g\n"
        "/list  shows your alerts\n"
        "/remove 2  removes alert 2\n"
        "/clear  removes all of yours\n\n"
        "Prices: 1g 50s, 18s, 2500c, or a plain number of gold. "
        "You can also use the Telegram alert button on an item's page on the dashboard. "
        "New prices arrive about once an hour, and I check your alerts each time.")


def find_item(query: str, names: dict) -> tuple[int | None, list[str]]:
    """(item id, []) on a single match, else (None, up to 5 suggestions)."""
    q = query.strip().lower()
    if q.isdigit():
        return (int(q), []) if q in names else (None, [])
    exact = [int(k) for k, n in names.items() if n.lower() == q]
    if exact:
        return min(exact), []
    hits = sorted(((n, int(k)) for k, n in names.items() if q in n.lower()), key=lambda x: (len(x[0]), x[0]))
    if len(hits) == 1:
        return hits[0][1], []
    return None, [n for n, _ in hits[:5]]


def describe(alert: dict, names: dict) -> str:
    word = "below" if alert["dir"] == "b" else "above"
    return f"{names.get(str(alert['item']), 'Item ' + str(alert['item']))} {word} {money(alert['price'])}"


def add_alert(state: dict, chat: str, item: int, direction: str, price: int, names: dict,
              lows: dict) -> str:
    mine = state["alerts"].setdefault(chat, [])
    for a in mine:
        if a["item"] == item and a["dir"] == direction:
            a["price"], a["armed"] = price, True
            return f"Updated: {describe(a, names)}." + now_text(item, lows)
    if len(mine) >= MAX_PER_PERSON:
        return f"You have {MAX_PER_PERSON} alerts, the most allowed. Remove one with /remove first."
    alert = {"item": item, "dir": direction, "price": price, "armed": True}
    mine.append(alert)
    return f"Alert set: {describe(alert, names)}." + now_text(item, lows)


def now_text(item: int, lows: dict) -> str:
    low = lows.get(item)
    return f" Lowest right now: {money(low)}." if low else " It is not on sale right now."


def handle(state: dict, chat: str, text: str, names: dict, lows: dict) -> str | None:
    text = text.strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    rest = rest.strip()

    if cmd == "/start":
        m = re.fullmatch(r"a_(\d+)_([ba])_(\d+)", rest)
        if not m:
            return "Hi! " + HELP
        item, direction, price = int(m.group(1)), m.group(2), int(m.group(3))
        if str(item) not in names or price <= 0:
            return "That alert link did not work. Try again from the dashboard, or use /alert."
        return add_alert(state, chat, item, direction, price, names, lows)

    if cmd in ("/help",):
        return HELP

    if cmd == "/alert":
        m = re.fullmatch(r"(.+?)\s+(below|under|above|over|<|>)\s+(.+)", rest, re.I)
        if not m:
            return "Write it like this: /alert Thorium Bar below 18s"
        price = parse_price(m.group(3))
        if price is None:
            return f"I could not read the price “{m.group(3)}”. Try 1g 50s, 18s or 2500c."
        item, options = find_item(m.group(1), names)
        if item is None:
            if options:
                return "Which one?\n" + "\n".join(options)
            return f"I could not find “{m.group(1)}”. Check the spelling, or use the item id."
        direction = "b" if m.group(2).lower() in ("below", "under", "<") else "a"
        return add_alert(state, chat, item, direction, price, names, lows)

    if cmd == "/list":
        mine = state["alerts"].get(chat) or []
        if not mine:
            return "You have no alerts. Add one with /alert Thorium Bar below 18s"
        return "Your alerts:\n" + "\n".join(
            f"{i}. {describe(a, names)}" + ("" if a["armed"] else " (fired, waiting to reset)")
            for i, a in enumerate(mine, 1))

    if cmd == "/remove":
        mine = state["alerts"].get(chat) or []
        if not rest.isdigit() or not 1 <= int(rest) <= len(mine):
            return "Send /list to see the numbers, then /remove with one of them."
        gone = mine.pop(int(rest) - 1)
        return f"Removed: {describe(gone, names)}."

    if cmd == "/clear":
        count = len(state["alerts"].pop(chat, []) or [])
        return f"Removed all {count} of your alerts." if count else "You had no alerts."

    if text.startswith("/"):
        return "I do not know that command. " + HELP
    return None                                     # ignore chatter


def check(state: dict, lows: dict, names: dict, link) -> list[tuple[str, str]]:
    """Messages for alerts that fired, as (chat, text). Updates armed flags."""
    out = []
    for chat, mine in state["alerts"].items():
        for a in mine:
            low = lows.get(a["item"])
            if low is None:
                continue
            hit = low <= a["price"] if a["dir"] == "b" else low >= a["price"]
            if hit and a["armed"]:
                a["armed"] = False
                word = "dropped to" if a["dir"] == "b" else "rose to"
                out.append((chat, f"{names.get(str(a['item']), 'Item')} {word} {money(low)} "
                                  f"(your alert: {'below' if a['dir'] == 'b' else 'above'} {money(a['price'])}).\n"
                                  f"{link(a['item'])}"))
            elif not hit and not a["armed"]:
                back = low > a["price"] * (1 + REARM) if a["dir"] == "b" else low < a["price"] * (1 - REARM)
                if back:
                    a["armed"] = True
    return out


# ---------------------------------------------------------- telegram ----

class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, _wait: int = 0, **params):
        req = urllib.request.Request(self.base + method, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30 + _wait) as resp:
                return json.load(resp).get("result")
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:200]
            raise TelegramError(err.code, body) from None

    def me(self) -> dict:
        return self.call("getMe")

    def updates(self, offset: int, timeout: int = 0) -> list:
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"],
                         _wait=timeout) or []

    def send(self, chat: str, text: str) -> None:
        self.call("sendMessage", chat_id=int(chat), text=text, disable_web_page_preview=True)


class TelegramError(Exception):
    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}: {body}")
        self.code = code


# --------------------------------------------------------------- run ----

def run(bot, out: Path, token: str, site: str, log=print, wait: int = 0) -> dict:
    data = Path(out) / "data"
    names = {k: v[0] for k, v in (read_json(data / "items.json") or {}).items()}
    summary = read_json(data / str(HOUSE) / "summary.json") or {"cols": [], "rows": []}
    col = {c: i for i, c in enumerate(summary["cols"])}
    lows = {r[col["id"]]: r[col["low"]] for r in summary["rows"]} if col else {}

    path = data / "alerts.bin"
    state = unseal(path.read_bytes(), token) if path.exists() else None
    if state is None:
        if path.exists():
            log("::warning::alerts.bin could not be read (bot token changed?); starting empty")
        state = {"offset": 0, "alerts": {}}

    replies = []
    for update in bot.updates(state["offset"], wait):
        state["offset"] = max(state["offset"], update["update_id"] + 1)
        msg = update.get("message") or {}
        chat, text = msg.get("chat", {}).get("id"), msg.get("text")
        if chat is None or not text:
            continue
        answer = handle(state, str(chat), text, names, lows)
        if answer:
            replies.append((str(chat), answer))

    link = lambda item: f"{site}#h={HOUSE}&t=all&i={item}"   # noqa: E731
    fired = check(state, lows, names, link)

    gone = set()
    for chat, text in replies + fired:
        if chat in gone:
            continue
        try:
            bot.send(chat, text)
        except TelegramError as err:
            if err.code in (400, 403):          # blocked the bot or deleted the chat
                gone.add(chat)
            else:
                log(f"::warning::Telegram send failed: {err}")
    for chat in gone:
        state["alerts"].pop(chat, None)
    state["alerts"] = {c: a for c, a in state["alerts"].items() if a}

    data.mkdir(parents=True, exist_ok=True)
    path.write_bytes(seal(state, token))
    total = sum(len(a) for a in state["alerts"].values())
    if replies or fired or not wait:
        log(f"alerts: {len(replies)} replies, {len(fired)} fired, {total} alerts from {len(state['alerts'])} people")
    return state


def fetch(url: str, target: Path) -> bool:
    """Download one published file; keep the old copy if that fails."""
    try:
        with urllib.request.urlopen(f"{url}?t={int(time.time())}", timeout=60) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return True


def save(workdir: Path) -> None:
    """Publish alerts.bin as the single commit of the "alerts" branch."""
    git = lambda *a: subprocess.run(["git", "-C", str(workdir), *a], check=True,  # noqa: E731
                                    capture_output=True, text=True)
    git("add", "-f", "data/alerts.bin")
    has_commit = subprocess.run(["git", "-C", str(workdir), "rev-parse", "-q", "--verify", "HEAD"],
                                capture_output=True).returncode == 0
    git("commit", "-q", *(["--amend"] if has_commit else []), "-m", "Alert state (encrypted)")
    git("push", "-q", "--force", "origin", "HEAD:alerts")


def listen(token: str, site: str, workdir: Path, seconds: int) -> None:
    """Answer the bot for `seconds`, checking alerts whenever prices change."""
    bot = Telegram(token)
    data = workdir / "data"
    if not (data / "alerts.bin").exists():          # first start: take over the old copy
        fetch(f"{site}data/alerts.bin", data / "alerts.bin")
    end = time.time() + seconds
    fetched, saved, saved_at = 0.0, None, 0.0
    state_text = lambda: json.dumps(unseal((data / "alerts.bin").read_bytes(), token)  # noqa: E731
                                    if (data / "alerts.bin").exists() else None, sort_keys=True)
    saved = state_text()
    print(f"listening for {seconds // 60} minutes", flush=True)
    while time.time() < end - 30:
        if time.time() - fetched > 180:
            fetch(f"{site}data/items.json", data / "items.json")
            fetch(f"{site}data/{HOUSE}/summary.json", data / str(HOUSE) / "summary.json")
            fetched = time.time()
        try:
            state = run(bot, workdir, token, site, log=lambda m: print(m, flush=True),
                        wait=int(min(50, max(1, end - 30 - time.time()))))
        except TelegramError as err:
            print(f"telegram: {err}", flush=True)
            time.sleep(30 if err.code == 409 else 5)    # 409: another reader is running
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            print(f"network: {err}", flush=True)
            time.sleep(5)
            continue
        now_text = json.dumps(state, sort_keys=True)
        if now_text != saved and time.time() - saved_at > 20:
            try:
                save(workdir)
                saved, saved_at = now_text, time.time()
            except subprocess.CalledProcessError as err:
                print(f"::warning::could not save alerts: {err.stderr}", flush=True)
    if state_text() != saved:
        save(workdir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="public")
    parser.add_argument("--site", default="", help="dashboard address, for links in alerts")
    parser.add_argument("--listen", type=int, default=0, metavar="SECONDS",
                        help="answer the bot continuously; --out is the alerts branch checkout")
    parser.add_argument("--meta-only", action="store_true",
                        help="only record the bot's username in meta.json")
    args = parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("alerts: no TELEGRAM_BOT_TOKEN secret, skipped")
        return 0
    if args.listen:
        listen(token, args.site, Path(args.out), args.listen)
        return 0
    try:
        bot = Telegram(token)
        name = (bot.me() or {}).get("username")
        if not args.meta_only:
            run(bot, Path(args.out), token, args.site)
        meta_path = Path(args.out) / "data" / "meta.json"
        meta = read_json(meta_path)
        if meta is not None and name:
            meta["telegram"] = name
            write_json(meta_path, meta)
    except Exception as err:                    # never block the price update
        print(f"::warning::Alerts were not processed: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
