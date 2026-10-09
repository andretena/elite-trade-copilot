"""Revenue ledger for Elite Trade Copilot — RevOps for your commander.

Builds a small local ledger (a plain JSON file, no database) from the journal's
MarketBuy / MarketSell events, including past journals on first run, tracks play
sessions, and links planned trade loops to what was actually realised so forecast
accuracy can be measured.
"""
import glob
import json
import os
import time
from datetime import datetime, timezone


def parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_ago(seconds):
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path, journal_dir):
        self.path, self.journal_dir = path, journal_dir
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        self.trades = data.get("trades", [])        # sells only: what counts as revenue
        self.sessions = data.get("sessions", {})    # start ts -> last event ts
        self.plans = data.get("plans", {})          # loop_id -> [{hop, commodity, station, planned, realised}]
        self.files = data.get("files", {})          # journal file -> mtime already read
        self.keys = {self._key(t) for t in self.trades}
        self.station = self.system = None
        self.session = None
        self.dirty = False

    @staticmethod
    def _key(t):
        return (t["ts"], t["commodity"], t["count"], t.get("market_id"))

    def save(self):
        if not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"trades": self.trades, "sessions": self.sessions, "plans": self.plans,
                       "files": self.files}, fh, ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written ledger
        self.dirty = False

    # ------------------------------------------------------------ ingestion
    def backfill(self):
        """Read every journal once; later runs only re-read files that changed."""
        files = sorted(glob.glob(os.path.join(self.journal_dir, "Journal.*.log")), key=os.path.getmtime)
        for path in files:
            name, mtime = os.path.basename(path), os.path.getmtime(path)
            if self.files.get(name) == mtime:
                continue
            with open(path, "rb") as fh:
                for raw in fh:
                    try:
                        self.record(json.loads(raw.decode("utf-8", errors="replace")), save=False)
                    except json.JSONDecodeError:
                        continue  # truncated last line of a crashed session; the live watcher reports bad lines
            self.files[name] = mtime
            self.dirty = True
        self.session = None  # the live journal replay opens the current session
        self.save()

    def record(self, ev, save=True):
        e, ts = ev.get("event"), ev.get("timestamp")
        if not ts:
            return None
        if e == "LoadGame":
            self.session = ts
            self.sessions.setdefault(ts, ts)
            self.dirty = True
        elif e == "Shutdown":
            self.session = None
        elif self.session:
            self.sessions[self.session] = ts
            self.dirty = True
        if e in ("Docked", "Location"):
            self.station, self.system = ev.get("StationName"), ev.get("StarSystem")
        if e != "MarketSell":
            return None
        count, avg, total = int(ev.get("Count", 0)), int(ev.get("AvgPricePaid", 0)), int(ev.get("TotalSale", 0))
        trade = {"ts": ts, "commodity": ev.get("Type_Localised") or ev.get("Type", "?").replace("_", " ").title(),
                 "count": count, "total": total, "profit": total - avg * count,
                 "market_id": ev.get("MarketID"), "station": self.station, "system": self.system}
        if self._key(trade) in self.keys:
            return None
        self.keys.add(self._key(trade))
        self.trades.append(trade)
        self.dirty = True
        if save:
            self.save()
        return trade

    # ------------------------------------------------------------ plans (forecast)
    def save_plan(self, route):
        loop_id = now_ts()
        self.plans[loop_id] = [{"hop": i, "commodity": (h.get("commodities") or [{}])[0].get("name"),
                                "station": h["destination"]["station"], "planned": int(h.get("total_profit", 0)),
                                "realised": None} for i, h in enumerate(route)]
        self.dirty = True
        self.save()
        return loop_id

    def realise(self, loop_id, hop, profit):
        stop = self.plans.get(loop_id, [])[hop]
        stop["realised"] = (stop["realised"] or 0) + int(profit)
        self.dirty = True
        self.save()

    # ------------------------------------------------------------ metrics
    def _sum(self, since):
        sold = [t for t in self.trades if t["ts"] >= since]
        return sum(t["profit"] for t in sold), len(sold)

    def _active_hours(self, since):
        hours = 0.0
        for start, last in self.sessions.items():
            if last >= since:
                hours += max(0.0, (parse_ts(last) - max(parse_ts(start), parse_ts(since))).total_seconds() / 3600)
        return hours

    def metrics(self, pipeline=0):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        week = ts_ago(7 * 86400)
        today_profit, today_sales = self._sum(today)
        week_profit, _ = self._sum(week)
        total_profit, total_sales = self._sum("")
        hours = self._active_hours(week)
        done = [s for stops in self.plans.values() for s in stops if s["realised"] is not None]
        planned = sum(s["planned"] for s in done)
        return {"today": today_profit, "today_sales": today_sales,
                "session": self._sum(self.session)[0] if self.session else 0,
                "week": week_profit, "per_hour": week_profit / hours if hours > 0.05 else 0,
                "total": total_profit, "total_sales": total_sales, "pipeline": pipeline,
                "accuracy": sum(s["realised"] for s in done) / planned if planned else None}

    def cumulative(self, days=30):
        since, acc, out = ts_ago(days * 86400), 0, []
        for t in sorted((t for t in self.trades if t["ts"] >= since), key=lambda t: t["ts"]):
            acc += t["profit"]
            out.append((parse_ts(t["ts"]), acc))
        return out

    def top_commodities(self, limit=5):
        agg = {}
        for t in self.trades:
            a = agg.setdefault(t["commodity"], [0, 0, 0])
            a[0] += t["profit"]
            a[1] += t["count"]
            a[2] += t["total"]
        rows = sorted(((name, *vals) for name, vals in agg.items()), key=lambda r: r[1], reverse=True)
        return rows[:limit]

    def recent_sales(self, limit=6):
        rows = sorted(self.trades, key=lambda t: t["ts"], reverse=True)[:limit]
        return [(t["ts"], t["commodity"], t["count"], t["profit"], t["station"]) for t in rows]
