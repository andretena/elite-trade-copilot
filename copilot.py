"""Elite Trade Copilot.

Reads the Elite Dangerous journal (read-only) and, every time you dock, asks Spansh's
trade planner for the most profitable multi-stop loop starting at that station. Shows it
in a small always-on-top window, speaks it aloud and copies the next system to the
clipboard. It never touches the game itself.
"""
import ctypes
import glob
import json
import locale
import os
import queue
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import font as tkfont

from revenue import Ledger

__version__ = "1.1.0"

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL_DIR = os.path.join(os.path.expanduser("~"), "Saved Games", "Frontier Developments", "Elite Dangerous")
CONFIG_PATH = os.path.join(HERE, "config.json")
LOCALES_DIR = os.path.join(HERE, "locales")
SCRIPTS_DIR = os.path.join(HERE, "scripts")
LEDGER_PATH = os.path.join(HERE, "data", "revenue.json")
IS_WINDOWS = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
SPANSH = "https://spansh.co.uk"
USER_AGENT = f"EliteTradeCopilot/{__version__} (+https://github.com/andretena/elite-trade-copilot)"
FALLBACK_LANGUAGE = "en"

DEFAULT_CONFIG = {
    "language": "auto",             # auto | en | pt-BR | es
    "max_hops": 4,                  # stops in the loop
    "max_hop_distance": 30,         # ly between consecutive stations
    "max_system_distance": 5000,    # ls from the arrival star (avoid long supercruise)
    "max_price_age_hours": 48,      # ignore stale market data
    "allow_planetary": False,
    "allow_player_owned": False,    # fleet carriers
    "speak": True,
    "copy_next_system": True,
    "always_on_top": True,
    "window": None,                 # saved automatically: [w, h, x, y]
}

LARGE_SHIPS = {"anaconda", "type9", "type9_military", "cutter", "federation_corvette", "belugaliner",
               "type7", "panthermkii", "type8"}
SHIP_NAMES = {"type9": "Type-9 Heavy", "type9_military": "Type-10 Defender", "type7": "Type-7 Transporter",
              "type6": "Type-6 Transporter", "type8": "Type-8 Transporter", "anaconda": "Anaconda",
              "cutter": "Imperial Cutter", "federation_corvette": "Federal Corvette",
              "python": "Python", "python_nx": "Python Mk II", "krait_mkii": "Krait Mk II",
              "asp": "Asp Explorer", "diamondbackxl": "Diamondback Explorer", "mandalay": "Mandalay",
              "cobramkiii": "Cobra Mk III", "cobramkv": "Cobra Mk V", "belugaliner": "Beluga Liner",
              "panthermkii": "Panther Clipper Mk II", "corsair": "Corsair", "sidewinder": "Sidewinder"}

ASSETS_DIR = os.path.join(HERE, "assets")


def draw_flag(cv, code, x, y, w, h):
    """Small vector flags so the language picker never depends on emoji fonts."""
    if code == "br":
        cv.create_rectangle(x, y, x + w, y + h, fill="#009c3b", outline="")
        cv.create_polygon(x + w * .5, y + h * .12, x + w * .92, y + h * .5, x + w * .5, y + h * .88,
                          x + w * .08, y + h * .5, fill="#ffdf00", outline="")
        r = h * .24
        cv.create_oval(x + w * .5 - r, y + h * .5 - r, x + w * .5 + r, y + h * .5 + r, fill="#002776", outline="")
        cv.create_line(x + w * .5 - r, y + h * .47, x + w * .5 + r, y + h * .55, fill="white", width=1)
    elif code == "es":
        cv.create_rectangle(x, y, x + w, y + h, fill="#aa151b", outline="")
        cv.create_rectangle(x, y + h * .25, x + w, y + h * .75, fill="#f1bf00", outline="")
    elif code == "gb":
        cv.create_rectangle(x, y, x + w, y + h, fill="#012169", outline="")
        cv.create_line(x, y, x + w, y + h, fill="white", width=5)
        cv.create_line(x, y + h, x + w, y, fill="white", width=5)
        cv.create_line(x, y, x + w, y + h, fill="#c8102e", width=2)
        cv.create_line(x, y + h, x + w, y, fill="#c8102e", width=2)
        cv.create_rectangle(x + w * .42, y, x + w * .58, y + h, fill="white", outline="")
        cv.create_rectangle(x, y + h * .36, x + w, y + h * .64, fill="white", outline="")
        cv.create_rectangle(x + w * .46, y, x + w * .54, y + h, fill="#c8102e", outline="")
        cv.create_rectangle(x, y + h * .42, x + w, y + h * .58, fill="#c8102e", outline="")
    else:
        cv.create_rectangle(x, y, x + w, y + h, fill="#555", outline="")
        cv.create_text(x + w / 2, y + h / 2, text=code[:2].upper(), fill="white")


C = {"bg": "#0a0612", "panel": "#140d24", "panel2": "#1b1230", "line": "#34265e", "teal": "#2ff3e0",
     "teal_dim": "#1c8f86", "mag": "#ff2fa8", "violet": "#8b6bff", "text": "#e9f6f5", "dim": "#9a8fc0",
     "warn": "#ffcc66"}


# ---------------------------------------------------------------- config & i18n
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)


def available_languages():
    langs = {}
    for path in sorted(glob.glob(os.path.join(LOCALES_DIR, "*.json"))):
        code = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as fh:
            langs[code] = json.load(fh)
    return langs


def system_language(codes):
    """Best match between the OS UI language and the shipped locales."""
    name = None
    if IS_WINDOWS:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(lcid)
    name = (name or locale.getlocale()[0] or "").replace("_", "-")
    if name in codes:
        return name
    prefix = name.split("-")[0].lower()
    for code in codes:
        if code.split("-")[0].lower() == prefix:
            return code
    return FALLBACK_LANGUAGE


class I18n:
    def __init__(self, langs, code):
        self.langs = langs
        self.set(code)

    def set(self, code):
        self.code = code if code in self.langs else FALLBACK_LANGUAGE
        self.strings = self.langs[self.code]
        self.fallback = self.langs[FALLBACK_LANGUAGE]

    def __call__(self, key, **kw):
        text = self.strings.get(key, self.fallback.get(key, key))
        return text.format(**kw) if kw else text

    def number(self, value, decimals=0):
        text = f"{value:,.{decimals}f}"
        if self.code.startswith(("pt", "es")):
            text = text.replace(",", "§").replace(".", ",").replace("§", ".")
        return text

    def credits(self, value):
        if abs(value) >= 1_000_000:
            return f"{self.number(value / 1_000_000, 2)} {self('unit_mi')}"
        return self.number(value)

    def spoken_credits(self, value):
        if abs(value) >= 1_000_000:
            return f"{self.number(value / 1_000_000, 1)} {self('millions')}"
        return f"{int(round(value / 1000))} {self('thousands')}"


# ---------------------------------------------------------------- game state
class Commander:
    def __init__(self):
        self.system = self.station = self.ship = self.ship_name = None
        self.docked = False
        self.cargo_capacity = 0
        self.jump_range = 0.0
        self.credits = 0

    @property
    def large_pad(self):
        return (self.ship or "").lower() in LARGE_SHIPS

    @property
    def display_ship(self):
        return self.ship_name or SHIP_NAMES.get((self.ship or "").lower(), self.ship or "?")

    def apply(self, ev):
        e = ev.get("event")
        if e == "LoadGame":
            self.ship, self.credits = ev.get("Ship"), ev.get("Credits", self.credits)
            self.ship_name = ev.get("Ship_Localised")
        elif e == "Loadout":
            if ev.get("Ship") != self.ship:
                self.ship_name = None
            self.ship = ev.get("Ship", self.ship)
            self.cargo_capacity = ev.get("CargoCapacity", self.cargo_capacity)
            self.jump_range = ev.get("MaxJumpRange", self.jump_range)
        elif e == "Location":
            self.system, self.docked = ev.get("StarSystem"), bool(ev.get("Docked"))
            self.station = ev.get("StationName") if self.docked else None
        elif e == "Docked":
            self.system, self.station, self.docked = ev.get("StarSystem"), ev.get("StationName"), True
        elif e == "Undocked":
            self.docked = False
        elif e in ("FSDJump", "CarrierJump"):
            self.system, self.station, self.docked = ev.get("StarSystem"), None, False


def read_balance():
    try:
        with open(os.path.join(JOURNAL_DIR, "Status.json"), encoding="utf-8") as fh:
            return json.load(fh).get("Balance")
    except (OSError, json.JSONDecodeError):
        return None  # the game rewrites this file constantly; a partial read just means "try later"


class JournalWatcher(threading.Thread):
    """Tails the newest Journal.*.log and pushes parsed events into a queue."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out

    def run(self):
        try:
            self.loop()
        except Exception as exc:  # shown in the window instead of dying silently
            self.out.put(("error", ("journal_stopped", f"{type(exc).__name__}: {exc}")))

    def loop(self):
        current, pos = None, 0
        while True:
            files = sorted(glob.glob(os.path.join(JOURNAL_DIR, "Journal.*.log")), key=os.path.getmtime)
            if files and files[-1] != current:
                current, pos = files[-1], 0
            if current:
                with open(current, "rb") as fh:
                    fh.seek(pos)
                    while True:
                        line = fh.readline()
                        if not line.endswith(b"\n"):
                            break  # the game is still writing this line
                        pos = fh.tell()
                        try:
                            self.out.put(("event", json.loads(line.decode("utf-8", errors="replace"))))
                        except json.JSONDecodeError as exc:
                            self.out.put(("error", ("bad_line", str(exc))))
            time.sleep(1)


# ---------------------------------------------------------------- Spansh
def _http(url, data=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as resp:
        return json.load(resp)


def spansh_trade_route(params):
    """Returns the list of hops. Falls back to Windows' HTTPS stack when Python's
    OpenSSL can't validate the certificate chain (seen with newer Let's Encrypt roots)."""
    try:
        job = _http(f"{SPANSH}/api/trade/route", params)
        if job.get("status") == "error":
            raise RuntimeError(job.get("error"))
        for _ in range(60):
            time.sleep(3)
            res = _http(f"{SPANSH}/api/results/{job['job']}")
            if res.get("status") != "queued":
                break
        if res.get("status") != "ok":
            raise RuntimeError(f"Spansh status '{res.get('status')}' {res.get('error', '')}")
        return res.get("result") or []
    except urllib.error.URLError as exc:
        if not (IS_WINDOWS and isinstance(exc.reason, ssl.SSLError)):
            raise
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           os.path.join(SCRIPTS_DIR, "spansh.ps1"), "-ParamsJson", json.dumps(params)],
                          capture_output=True, text=True, encoding="utf-8", timeout=240,
                          creationflags=CREATE_NO_WINDOW)
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if not out.get("ok"):
        raise RuntimeError(out.get("error"))
    return out.get("result") or []


# ---------------------------------------------------------------- UI
class App:
    def __init__(self, root):
        self.root, self.cfg = root, load_config()
        langs = available_languages()
        code = self.cfg["language"]
        self.t = I18n(langs, system_language(list(langs)) if code == "auto" else code)
        self.cmdr, self.q = Commander(), queue.Queue()
        self.route, self.step, self.busy, self.replaying = [], 0, False, True
        self.last_msg = ("reading_journal", {})
        self.view = self.cfg.get("view", "route")
        self.ledger, self.loop_id = None, None

        root.title("Elite Trade Copilot")
        root.configure(bg=C["bg"])
        root.minsize(560, 520)
        if self.cfg.get("window"):
            w, h, x, y = self.cfg["window"]
            root.geometry(f"{w}x{h}+{x}+{y}")
        else:
            root.geometry("760x900")
        root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        icon = os.path.join(ASSETS_DIR, "icon.png")
        if os.path.exists(icon):
            self.icon_img = tk.PhotoImage(file=icon)
            root.iconphoto(True, self.icon_img)
        if IS_WINDOWS:  # own taskbar icon instead of Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EliteTradeCopilot")

        family = "Bahnschrift" if "Bahnschrift" in tkfont.families() else "Segoe UI"
        self.f = {k: tkfont.Font(family=family, size=s, weight=wt) for k, (s, wt) in {
            "title": (19, "bold"), "big": (14, "bold"), "txt": (11, "normal"), "small": (9, "normal"),
            "huge": (24, "bold"), "badge": (13, "bold")}.items()}
        self.build()
        threading.Thread(target=self.start_ledger, daemon=True).start()
        root.after(300, self.pump)
        root.after(15000, self.tick)

    def start_ledger(self):
        """Backfill the revenue ledger from past journals, then start following the live one."""
        try:
            ledger = Ledger(LEDGER_PATH, JOURNAL_DIR)
            ledger.backfill()
        except Exception as exc:  # the app still works without revenue tracking; say why
            ledger = None
            self.q.put(("error", ("ledger_error", f"{type(exc).__name__}: {exc}")))
        self.q.put(("ledger", ledger))
        JournalWatcher(self.q).start()

    def tick(self):
        if self.view == "revenue":
            self.render_revenue()
        self.root.after(15000, self.tick)

    # -- layout
    def build(self):
        for w in self.root.winfo_children():
            w.destroy()
        t = self.t
        accent = tk.Canvas(self.root, height=4, bg=C["bg"], highlightthickness=0)
        accent.pack(fill="x")
        accent.bind("<Configure>", lambda e: self.draw_gradient(accent, e.width))

        head = tk.Frame(self.root, bg=C["bg"])
        head.pack(fill="x", padx=18, pady=(12, 4))
        left = tk.Frame(head, bg=C["bg"])
        left.pack(side="left")
        if getattr(self, "icon_img", None):
            self.logo = self.icon_img.subsample(5, 5)
            tk.Label(left, image=self.logo, bg=C["bg"]).pack(side="left", padx=(0, 10))
        titles = tk.Frame(left, bg=C["bg"])
        titles.pack(side="left")
        tk.Label(titles, text=t("title"), font=self.f["title"], fg=C["teal"], bg=C["bg"]).pack(anchor="w")
        tk.Label(titles, text=t("subtitle"), font=self.f["small"], fg=C["dim"], bg=C["bg"]).pack(anchor="w")

        right = tk.Frame(head, bg=C["bg"])
        right.pack(side="right")
        self.btn = tk.Button(right, text=t("calculating") if self.busy else t("recalculate"), font=self.f["small"],
                             fg=C["bg"], bg=C["teal"], activebackground=C["mag"], activeforeground=C["text"],
                             relief="flat", padx=16, pady=8, command=self.calculate, cursor="hand2", bd=0)
        self.btn.pack(side="right")
        self.btn.bind("<Enter>", lambda e: self.btn.config(bg=C["mag"], fg=C["text"]))
        self.btn.bind("<Leave>", lambda e: self.btn.config(bg=C["teal"], fg=C["bg"]))
        self.v_speak = tk.BooleanVar(value=self.cfg["speak"])
        tk.Checkbutton(right, text="🔊 " + t("voice"), variable=self.v_speak, command=self.toggle_speak,
                       font=self.f["small"], fg=C["dim"], bg=C["bg"], selectcolor=C["panel"],
                       activebackground=C["bg"], activeforeground=C["teal"], cursor="hand2").pack(side="right", padx=10)
        flags = tk.Frame(right, bg=C["bg"])
        flags.pack(side="right", padx=4)
        for code, strings in self.t.langs.items():
            active = code == self.t.code
            cv = tk.Canvas(flags, width=42, height=32, bg=C["bg"], highlightthickness=0, cursor="hand2")
            cv.pack(side="left", padx=3)
            draw_flag(cv, strings.get("flag", code), 5, 4, 32, 21)
            cv.create_rectangle(3, 2, 39, 27, outline=C["teal"] if active else C["line"], width=2 if active else 1)
            if active:
                cv.create_rectangle(9, 29, 33, 32, fill=C["teal"], outline="")
            cv.bind("<Button-1>", lambda e, c=code: self.change_language(c))
            cv.bind("<Enter>", lambda e, w=cv, n=strings["language_name"]: self.show_tip(n))
            cv.bind("<Leave>", lambda e: self.show_msg(*self.last_msg))

        self.chips = tk.Frame(self.root, bg=C["bg"])
        self.chips.pack(fill="x", padx=18, pady=(10, 0))

        tabs = tk.Frame(self.root, bg=C["bg"])
        tabs.pack(fill="x", padx=18, pady=(12, 0))
        self.tab_btns = {}
        for key, label in (("route", t("tab_route")), ("revenue", t("tab_revenue"))):
            b = tk.Label(tabs, text=label, font=self.f["small"], padx=18, pady=7, cursor="hand2")
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, k=key: self.set_view(k))
            self.tab_btns[key] = b
        tk.Frame(self.root, bg=C["line"], height=1).pack(fill="x", padx=18)

        self.route_view = tk.Frame(self.root, bg=C["bg"])
        summary = tk.Frame(self.route_view, bg=C["bg"])
        summary.pack(fill="x", pady=(12, 2))
        self.profit = tk.Label(summary, font=self.f["huge"], fg=C["mag"], bg=C["bg"], anchor="w")
        self.profit.pack(side="left")
        self.summary = tk.Label(summary, font=self.f["txt"], fg=C["dim"], bg=C["bg"], anchor="w", justify="left")
        self.summary.pack(side="left", padx=12, pady=(8, 0))
        self.bar = tk.Canvas(self.route_view, height=10, bg=C["bg"], highlightthickness=0)
        self.bar.pack(fill="x", pady=(4, 6))
        self.bar.bind("<Configure>", lambda e: self.draw_bar())
        self.list = tk.Frame(self.route_view, bg=C["bg"])
        self.list.pack(fill="both", expand=True, pady=4)
        self.revenue_view = tk.Frame(self.root, bg=C["bg"])

        foot = tk.Frame(self.root, bg=C["panel"])
        foot.pack(fill="x", side="bottom")
        tk.Label(foot, text=f"v{__version__}  •  {t('footer')}", font=self.f["small"], fg=C["dim"], bg=C["panel"]
                 ).pack(side="right", padx=12, pady=6)
        self.msg = tk.Label(foot, font=self.f["small"], fg=C["warn"], bg=C["panel"], anchor="w", justify="left")
        self.msg.pack(side="left", fill="x", expand=True, padx=12, pady=6)
        self.msg.bind("<Configure>", lambda e: self.msg.config(wraplength=max(200, e.width - 10)))
        self.refresh_status()
        self.render_route()
        self.set_view(self.view)
        self.show_msg(*self.last_msg)

    def set_view(self, view):
        self.view = view
        if self.cfg.get("view") != view:
            self.cfg["view"] = view
            save_config(self.cfg)
        for key, b in self.tab_btns.items():
            on = key == view
            b.config(fg=C["bg"] if on else C["dim"], bg=C["teal"] if on else C["panel"])
        self.route_view.pack_forget()
        self.revenue_view.pack_forget()
        target = self.route_view if view == "route" else self.revenue_view
        target.pack(fill="both", expand=True, padx=18, pady=4, before=self.msg.master)
        if view == "revenue":
            self.render_revenue()

    # -- revenue (RevOps) view
    def pipeline(self):
        return sum(h.get("total_profit", 0) for h in self.route[self.step:])

    def kpi(self, parent, label, value, sub="", color=None):
        box = tk.Frame(parent, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        tk.Label(box, text=label.upper(), font=self.f["small"], fg=C["dim"], bg=C["panel"]).pack(anchor="w", padx=12,
                                                                                               pady=(8, 0))
        tk.Label(box, text=value, font=self.f["big"], fg=color or C["text"], bg=C["panel"]).pack(anchor="w", padx=12)
        tk.Label(box, text=sub, font=self.f["small"], fg=C["dim"], bg=C["panel"]).pack(anchor="w", padx=12,
                                                                                     pady=(0, 8))
        return box

    def render_revenue(self):
        t = self.t
        for w in self.revenue_view.winfo_children():
            w.destroy()
        if not self.ledger:
            tk.Label(self.revenue_view, text=t("loading_ledger"), font=self.f["txt"], fg=C["dim"], bg=C["bg"]
                     ).pack(pady=40)
            return
        m = self.ledger.metrics(self.pipeline())
        grid = tk.Frame(self.revenue_view, bg=C["bg"])
        grid.pack(fill="x", pady=(12, 6))
        acc = "—" if m["accuracy"] is None else f"{t.number(m['accuracy'] * 100)}%"
        cards = [
            (t("kpi_today"), f"+{t.credits(m['today'])} cr", t("sales_count", n=m["today_sales"]), C["mag"]),
            (t("kpi_session"), f"+{t.credits(m['session'])} cr", "", C["teal"]),
            (t("kpi_per_hour"), f"{t.credits(m['per_hour'])} cr/h", t("last_7_days"), C["text"]),
            (t("kpi_pipeline"), f"{t.credits(m['pipeline'])} cr", t("pipeline_hint"), C["violet"]),
            (t("kpi_accuracy"), acc, t("accuracy_hint"), C["warn"]),
            (t("kpi_total"), f"+{t.credits(m['total'])} cr", t("sales_count", n=m["total_sales"]), C["text"]),
        ]
        for i, (label, value, sub, color) in enumerate(cards):
            self.kpi(grid, label, value, sub, color).grid(row=i // 3, column=i % 3, sticky="nsew", padx=3, pady=3)
        for col in range(3):
            grid.columnconfigure(col, weight=1)

        tk.Label(self.revenue_view, text=t("chart_title"), font=self.f["small"], fg=C["dim"], bg=C["bg"], anchor="w"
                 ).pack(fill="x", pady=(10, 2))
        chart = tk.Canvas(self.revenue_view, height=150, bg=C["panel"], highlightthickness=1,
                          highlightbackground=C["line"])
        chart.pack(fill="x")
        series = self.ledger.cumulative(30)
        chart.bind("<Configure>", lambda e: self.draw_chart(chart, series))

        cols = tk.Frame(self.revenue_view, bg=C["bg"])
        cols.pack(fill="both", expand=True, pady=(10, 0))
        left = tk.Frame(cols, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
        right = tk.Frame(cols, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        tk.Label(left, text=t("top_commodities"), font=self.f["small"], fg=C["dim"], bg=C["panel"], anchor="w"
                 ).pack(fill="x", padx=12, pady=(8, 4))
        top = self.ledger.top_commodities()
        best = max((p for _, p, _, _ in top), default=0) or 1
        for name, profit, count, total in top:
            row = tk.Frame(left, bg=C["panel"])
            row.pack(fill="x", padx=12, pady=2)
            margin = profit / total * 100 if total else 0
            tk.Label(row, text=name, font=self.f["txt"], fg=C["text"], bg=C["panel"], anchor="w").pack(fill="x")
            bar = tk.Canvas(row, height=6, bg=C["panel"], highlightthickness=0)
            bar.pack(fill="x")
            bar.bind("<Configure>", lambda e, c=bar, f=profit / best: (c.delete("all"), c.create_rectangle(
                0, 0, e.width * max(f, 0.02), 6, fill=C["teal"], outline="")))
            tk.Label(row, text=f"+{t.credits(profit)} cr · {t('margin')} {t.number(margin)}% · {count} t",
                     font=self.f["small"], fg=C["dim"], bg=C["panel"], anchor="w").pack(fill="x")
        if not top:
            tk.Label(left, text=t("no_sales"), font=self.f["small"], fg=C["dim"], bg=C["panel"]).pack(pady=10)
        tk.Label(right, text=t("recent_sales"), font=self.f["small"], fg=C["dim"], bg=C["panel"], anchor="w"
                 ).pack(fill="x", padx=12, pady=(8, 4))
        for ts, name, count, profit, station in self.ledger.recent_sales():
            when = ts[5:16].replace("T", " ")
            tk.Label(right, text=f"{when}  {count} t {name}", font=self.f["txt"], fg=C["text"], bg=C["panel"],
                     anchor="w").pack(fill="x", padx=12)
            tk.Label(right, text=f"+{t.credits(profit)} cr · {station or '?'}", font=self.f["small"],
                     fg=C["mag"] if profit >= 0 else C["warn"], bg=C["panel"], anchor="w").pack(fill="x", padx=12,
                                                                                              pady=(0, 4))

    def draw_chart(self, cv, series):
        cv.delete("all")
        w, h, pad = cv.winfo_width(), cv.winfo_height(), 14
        if len(series) < 2:
            cv.create_text(w / 2, h / 2, text=self.t("no_sales"), fill=C["dim"], font=self.f["small"])
            return
        t0, t1 = series[0][0].timestamp(), series[-1][0].timestamp()
        top = max(v for _, v in series) or 1
        low = min(0, min(v for _, v in series))
        span_t, span_v = max(t1 - t0, 1), max(top - low, 1)
        pts = [(pad + (ts.timestamp() - t0) / span_t * (w - 2 * pad),
                h - pad - (v - low) / span_v * (h - 2 * pad)) for ts, v in series]
        cv.create_polygon([(pts[0][0], h - pad)] + pts + [(pts[-1][0], h - pad)], fill="#16343a", outline="")
        cv.create_line(pts, fill=C["teal"], width=2, smooth=False)
        cv.create_oval(pts[-1][0] - 4, pts[-1][1] - 4, pts[-1][0] + 4, pts[-1][1] + 4, fill=C["mag"], outline="")
        cv.create_text(w - pad, pad, text=f"+{self.t.credits(series[-1][1])} cr", anchor="ne", fill=C["mag"],
                       font=self.f["small"])

    @staticmethod
    def draw_gradient(cv, width):
        cv.delete("all")
        a, b = (0x2f, 0xf3, 0xe0), (0xff, 0x2f, 0xa8)
        steps = max(1, width // 4)
        for i in range(steps):
            f = i / steps
            col = "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))
            cv.create_rectangle(i * 4, 0, i * 4 + 4, 4, fill=col, outline="")

    def show_tip(self, text):
        self.msg.config(text=text)

    def chip(self, label, value, accent=False):
        box = tk.Frame(self.chips, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        box.pack(side="left", padx=(0, 6), pady=3)
        tk.Label(box, text=label.upper(), font=self.f["small"], fg=C["dim"], bg=C["panel"]).pack(anchor="w", padx=10,
                                                                                               pady=(4, 0))
        tk.Label(box, text=value, font=self.f["txt"], fg=C["teal"] if accent else C["text"], bg=C["panel"]
                 ).pack(anchor="w", padx=10, pady=(0, 5))

    def show_msg(self, key, kw=None):
        self.last_msg = (key, kw or {})
        self.msg.config(text=self.t(key, **(kw or {})) if key else "")

    def refresh_status(self):
        c, t = self.cmdr, self.t
        bal = read_balance() or c.credits
        for w in self.chips.winfo_children():
            w.destroy()
        where = f"{c.station} · {c.system}" if c.docked else f"{c.system or '?'} ({t('in_flight')})"
        self.chip(t("location"), where, accent=True)
        self.chip(t("ship"), c.display_ship)
        self.chip(t("cargo"), f"{c.cargo_capacity} t")
        self.chip(t("jump"), f"{t.number(c.jump_range, 1)} ly")
        self.chip(t("credits"), f"{t.credits(bal)} cr")
        self.chip(t("large_pad"), t("yes") if c.large_pad else t("no"))

    def draw_bar(self):
        self.bar.delete("all")
        n = len(self.route)
        if not n:
            return
        w, gap = self.bar.winfo_width(), 6
        seg = (w - gap * (n - 1)) / n
        for i in range(n):
            x0 = i * (seg + gap)
            color = C["teal"] if i < self.step else (C["mag"] if i == self.step else C["line"])
            self.bar.create_rectangle(x0, 2, x0 + seg, 9, fill=color, outline="")

    def render_route(self):
        t = self.t
        for w in self.list.winfo_children():
            w.destroy()
        if not self.route:
            self.profit.config(text="")
            self.summary.config(text="")
            self.draw_bar()
            empty = tk.Frame(self.list, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
            empty.pack(fill="x", pady=20)
            cv = tk.Canvas(empty, width=70, height=70, bg=C["panel"], highlightthickness=0)
            cv.pack(pady=(22, 6))
            cv.create_oval(6, 6, 64, 64, outline=C["violet"], width=3, dash=(6, 4))
            cv.create_oval(28, 28, 42, 42, fill=C["teal"] if not self.busy else C["mag"], outline="")
            tk.Label(empty, text=t("calculating") if self.busy else t("waiting_title"), font=self.f["big"],
                     fg=C["teal"], bg=C["panel"]).pack()
            tk.Label(empty, text=t("waiting_text"), font=self.f["txt"], fg=C["dim"], bg=C["panel"],
                     wraplength=520, justify="center").pack(pady=(4, 24), padx=20)
            return
        total = self.route[-1].get("cumulative_profit", 0)
        distance = sum(h.get("distance", 0) for h in self.route)
        self.profit.config(text=f"+{t.credits(total)} cr")
        progress = t("loop_done") if self.step >= len(self.route) else \
            t("progress", current=self.step + 1, total=len(self.route))
        self.summary.config(text=f"{t('total_profit')} • {t('loop_summary', stops=len(self.route))}\n"
                                 f"{progress}   •   {t.number(distance, 1)} ly")
        self.draw_bar()
        for i, hop in enumerate(self.route):
            self.render_hop(i, hop)

    def render_hop(self, i, hop):
        t = self.t
        done, active = i < self.step, i == self.step
        edge = C["teal"] if active else C["line"]
        card = tk.Frame(self.list, bg=C["panel2"] if active else C["panel"], highlightbackground=edge,
                        highlightthickness=2 if active else 1)
        card.pack(fill="x", pady=5)
        bg = card["bg"]
        badge = tk.Canvas(card, width=44, height=44, bg=bg, highlightthickness=0)
        badge.pack(side="left", padx=(12, 6), pady=10, anchor="n")
        ring = C["teal"] if active else (C["teal_dim"] if done else C["violet"])
        badge.create_oval(4, 4, 40, 40, outline=ring, width=3, fill=C["bg"] if not done else C["teal_dim"])
        badge.create_text(22, 22, text="✓" if done else str(i + 1), fill=C["text"], font=self.f["badge"])
        body = tk.Frame(card, bg=bg)
        body.pack(side="left", fill="x", expand=True, pady=8, padx=(0, 12))
        src, dst = hop["source"], hop["destination"]
        goods = hop.get("commodities") or []
        what = ", ".join(f"{g['amount']} t {g['name']}" for g in goods) or t("nothing")
        per_t = goods[0].get("profit", 0) if goods else 0
        tk.Label(body, text=f"{t('buy')}  {what}", font=self.f["big"], anchor="w", bg=bg,
                 fg=C["teal"] if active else (C["dim"] if done else C["text"])).pack(fill="x")
        tk.Label(body, text=f"{t('at')} {src['station']} · {src['system']}", font=self.f["small"], anchor="w",
                 bg=bg, fg=C["dim"]).pack(fill="x")
        tk.Label(body, text=f"➜ {t('sell_at')} {dst['station']} · {t('system')} {dst['system']}", font=self.f["txt"],
                 anchor="w", bg=bg, fg=C["text"] if not done else C["dim"]).pack(fill="x", pady=(4, 0))
        tk.Label(body, text=f"{t.number(hop.get('distance', 0), 1)} ly   •   "
                            f"{t.number(dst.get('distance_to_arrival', 0))} ls {t('from_star')}   •   "
                            f"{t('profit')} +{t.credits(hop.get('total_profit', 0))} cr   •   "
                            f"{t.number(per_t)} cr {t('per_ton')}",
                 font=self.f["small"], anchor="w", bg=bg, fg=C["mag"] if not done else C["dim"]).pack(fill="x")

    # -- actions
    def change_language(self, code):
        if code == self.t.code:
            return
        self.t.set(code)
        self.cfg["language"] = self.t.code
        save_config(self.cfg)
        self.build()

    def toggle_speak(self):
        self.cfg["speak"] = self.v_speak.get()
        save_config(self.cfg)

    def on_close(self):
        self.cfg["window"] = [self.root.winfo_width(), self.root.winfo_height(),
                              self.root.winfo_x(), self.root.winfo_y()]
        save_config(self.cfg)
        if self.ledger:
            self.ledger.save()
        self.root.destroy()

    def say(self, key, **kw):
        if not self.cfg["speak"] or self.replaying or not IS_WINDOWS:
            return
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          os.path.join(SCRIPTS_DIR, "speak.ps1"), "-Text", self.t(key, **kw),
                          "-Culture", self.t("voice_culture")], creationflags=CREATE_NO_WINDOW)

    def copy_next(self):
        if not (self.cfg["copy_next_system"] and self.route and self.step < len(self.route)):
            return
        system = self.route[self.step]["destination"]["system"]
        self.root.clipboard_clear()
        self.root.clipboard_append(system)
        self.show_msg("next_copied", {"system": system})

    def end_replay(self):
        self.replaying = False
        self.refresh_status()
        if self.cmdr.docked and not self.route:
            self.calculate()

    def calculate(self):
        c = self.cmdr
        if self.busy:
            return
        if not (c.docked and c.station):
            self.show_msg("dock_first")
            return
        if not c.cargo_capacity:
            self.show_msg("no_cargo")
            return
        self.busy = True
        self.btn.config(text=self.t("calculating"), state="disabled")
        self.show_msg("querying", {"station": c.station})
        if not self.route:
            self.render_route()
        params = {"max_hops": self.cfg["max_hops"], "max_hop_distance": self.cfg["max_hop_distance"],
                  "system": c.system, "station": c.station,
                  "starting_capital": int(read_balance() or c.credits), "max_cargo": c.cargo_capacity,
                  "max_system_distance": self.cfg["max_system_distance"],
                  "max_price_age": int(self.cfg["max_price_age_hours"] * 3600),
                  "requires_large_pad": int(c.large_pad), "allow_prohibited": 0,
                  "allow_planetary": int(self.cfg["allow_planetary"]),
                  "allow_player_owned": int(self.cfg["allow_player_owned"]),
                  "allow_restricted_access": 0, "unique": 0, "permit": 0}
        threading.Thread(target=self.fetch, args=(params,), daemon=True).start()

    def fetch(self, params):
        try:
            self.q.put(("route", {"ok": True, "result": spansh_trade_route(params)}))
        except Exception as exc:  # reported in the window, never swallowed
            self.q.put(("route", {"ok": False, "error": f"{type(exc).__name__}: {exc}"}))

    def on_route(self, out):
        self.busy = False
        self.btn.config(text=self.t("recalculate"), state="normal")
        if not out["ok"]:
            self.show_msg("calc_error", {"error": out["error"]})
            self.say("say_calc_error")
            return
        result = out["result"]
        if isinstance(result, dict):
            result = [result]
        self.route, self.step = result, 0
        self.loop_id = self.ledger.save_plan(result) if (self.ledger and result) else None
        self.render_route()
        if not result:
            self.show_msg("no_route")
            self.say("say_no_route")
            return
        self.copy_next()
        first = result[0]
        goods = first["commodities"][0]["name"] if first.get("commodities") else self.t("cargo_word")
        self.say("say_route_ready", profit=self.t.spoken_credits(result[-1].get("cumulative_profit", 0)),
                 goods=goods, station=first["destination"]["station"], system=first["destination"]["system"])

    def on_sale(self, sale):
        """Credit a sale to the loop stop it belongs to, so forecast accuracy can be measured."""
        target = getattr(self, "credit_target", None)  # (loop_id, hop, station) of the stop we docked at
        if target and target[0] and sale["station"] == target[2]:
            self.ledger.realise(target[0], target[1], sale["profit"])
        if self.view == "revenue":
            self.render_revenue()

    def on_docked(self, ev):
        if not self.route:
            self.calculate()
            return
        hop = self.route[self.step] if self.step < len(self.route) else None
        if hop and ev.get("StationName") == hop["destination"]["station"]:
            self.credit_target = (self.loop_id, self.step, ev.get("StationName"))
            sold = ", ".join(c["name"] for c in hop.get("commodities", []))
            self.step += 1
            self.render_route()
            if self.step < len(self.route):
                nxt = self.route[self.step]
                goods = nxt["commodities"][0]["name"] if nxt.get("commodities") else self.t("cargo_word")
                self.copy_next()
                self.say("say_next", sold=sold, goods=goods, station=nxt["destination"]["station"],
                         system=nxt["destination"]["system"])
            else:
                self.say("say_loop_done", sold=sold)
                self.route = []
                self.calculate()
        else:
            self.say("say_off_route")
            self.route = []
            self.calculate()

    def pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "event":
                    self.cmdr.apply(data)
                    sale = self.ledger.record(data) if self.ledger else None
                    if sale and not self.replaying:
                        self.on_sale(sale)
                    if self.replaying:
                        continue
                    if data.get("event") == "Docked":
                        self.refresh_status()
                        self.on_docked(data)
                    elif data.get("event") in ("Undocked", "FSDJump", "Location", "Loadout", "LoadGame"):
                        self.refresh_status()
                elif kind == "route":
                    self.on_route(data)
                elif kind == "ledger":
                    self.ledger = data
                    self.root.after(4000, self.end_replay)  # the journal watcher starts replaying now
                    if self.view == "revenue":
                        self.render_revenue()
                elif kind == "error":
                    self.show_msg(data[0], {"error": data[1]})
        except queue.Empty:
            pass
        self.root.after(300, self.pump)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
