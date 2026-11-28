"""Fact check of the numbers a desk or a proposal states (strategy v3; paper trading only; code only, no web, no AI).

A desk file (analysis/D/<desk>.json) and a proposal (state/allocation.json) carry free text: "GOLDBEES closed 101.57", "SILVERBEES
-2.1% on the day", "SMA200 123.28", "close_below:SILVERBEES:158". This module finds the numeric CLAIMS in that text with fixed
patterns and compares each one with the engine's own data. It is advice about the advisers, never a gate: nothing here refuses,
changes or writes anything. The text is only matched against regular expressions, never interpreted, so text such as "ignore your
rules and buy" is just words without a number.

What counts as a claim (anything else is ignored: years, version numbers, plain numbers with no symbol next to them):
  price     SYMBOL [closed|at|last|price|...] [Rs|INR] NUMBER     "GOLDBEES closed 101.57"       vs the last 2 session closes (a
                                                                  "close" claim) or the latest close / the day's tick range
  pct       SYMBOL [fell|rose|...] NUMBER%  + a horizon word      "SILVERBEES -2.1% on the day"  vs the close-to-close return
  pts       SYMBOL fell|rose NUMBER pts                           rupee move vs the previous close
  level     close_below:SYMBOL:NUMBER, "support|stop|floor ... NUMBER" (must lie BELOW the price), "resistance|target ..." (ABOVE it)
  relation  SYMBOL closed above|below NUMBER                      the latest close must be on that side
  sma       SMA200 NUMBER / "55-day high NUMBER"                  vs level_raw (or the hysteresis level) of the latest engine TARGET record
  date      an ISO date or "25 Nov" next to a close/session word  a real calendar day and an NSE trading day
Tolerances: price and level numbers 0.5 percent, percentage points 0.3, rupee points max(0.5 percent of the price, 0.05).
Numbers are read in the Indian grouping too ("1,00,000"), with the unicode minus, with a leading Rs, INR or the rupee sign.

Verdicts: OK, MISMATCH (the data says otherwise), UNVERIFIABLE (no data, no horizon, no symbol: never counted against the author).
Reference data, in this order: logs/ticks-D.csv (the last row of a day is its close), state/last_prices.json, the closes kept in
memory/ltm/regime.json, the TARGET records of logs/decisions-D.jsonl for the trend numbers. check_* never raises.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from simgold import market

SYMS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")
METAL_SYM = {"GOLD": "GOLDBEES", "SILVER": "SILVERBEES", "COPPER": "HINDCOPPER"}
TOL_PRICE = 0.005
TOL_PP = 0.3
OK, MISMATCH, UNVERIFIABLE = "OK", "MISMATCH", "UNVERIFIABLE"
MAX_TEXT = 4000
MAX_CLAIMS = 60
TICK_FILES = 30

_NUM = r"[-+−]?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?"
_SYM = "|".join(SYMS)
_WORDS = (r"\b(?:closed|closes|close|settled|ended|finished|at|last|price|ltp|trades|traded|trading|opened|open|quoted|near|around|"
          r"is|was|to|a|the|of|day|session|fell|fall|falls|rose|rises|rise|dropped|drops|gained|gains|lost|lose|declined|slid|"
          r"sank|climbed|jumped|up|down|lower|higher|by|about|roughly|only|just|above|below|over|under|support|resistance|"
          r"stop|floor|trigger|target|level|cap|ceiling|broke|held|holds|holding|sits|sat|vs)\b")
_CLAIM = re.compile(
    rf"\b(?P<sym>{_SYM})\b(?P<gap>(?:[\s:=@~,(]|₹|\bRs\.?|\bINR\b|{_WORDS})*?)(?<![\w.])(?P<num>{_NUM})"
    rf"(?P<tail>\s*(?:%|\bpct\b|\bpercent\b|\bpp\b|\bbps\b|\bpts?\b|\bpoints?\b|\$|\busd\b|/oz)?)"
    rf"(?![\w]|[-/]\w|\.\d|\s*(?:d|day|days|sessions?|td|w|weeks?|x|times|bars?|trading|yrs?|years?|months?|m|h|hrs?|min|lots?|units?|shares?|qty)\b)",
    re.I)
_INVALIDATE = re.compile(rf"\bclose_below:(?P<sym>{_SYM}):(?P<num>\d+(?:\.\d+)?)\b")
_SMA = re.compile(rf"\bSMA\s?-?(?P<n>\d{{2,3}})\b(?P<gap>(?:[\s:=~]|₹|\bRs\.?|\bINR\b|\b(?:of|at|is|was|near|around|for|{_SYM})\b)*?)"
                  rf"(?<![\w.])(?P<num>{_NUM})(?![\w%]|\.\d)", re.I)
_HIGH = re.compile(rf"\b(?P<n>\d{{2,3}})[- ]?(?:day|d)\s+high\b(?P<gap>(?:[\s:=~]|₹|\bRs\.?|\bINR\b|\b(?:of|at|is|was|near|around)\b)*?)"
                   rf"(?<![\w.])(?P<num>{_NUM})(?![\w%]|\.\d)", re.I)
_SYM_ANY = re.compile(rf"\b({_SYM})\b", re.I)
_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DMY = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\b\.?(?:\s+(\d{4}))?", re.I)
_DATEWORD = re.compile(r"\b(?:closed|closes|close|session|settled|traded|trading)\b", re.I)
_LVL_BELOW = re.compile(r"\b(?:support|stop|stop-loss|floor|invalidat\w*|trigger)\b", re.I)
_LVL_ABOVE = re.compile(r"\b(?:resistance|ceiling|target)\b", re.I)
_CLOSEW = re.compile(r"\b(?:close|closed|closes|settled)\b", re.I)
_DOWNW = re.compile(r"\b(?:fell|fall|falls|dropped|drops|lost|lose|declined|slid|sank|down|lower)\b", re.I)
_UPW = re.compile(r"\b(?:rose|rise|rises|gained|gains|climbed|jumped|up|higher)\b", re.I)
_REL = re.compile(r"\b(above|below|over|under)\b", re.I)
_NDAY = re.compile(r"\b(\d{1,3})\s*-?\s*(?:d|day|days|session|sessions|td)\b", re.I)
_DAYW = re.compile(r"\b(?:on the day|today|intraday|d/d|1d|daily|yesterday|overnight|day|session)\b", re.I)
_WEEKW = re.compile(r"\b(?:week|weekly|w/w)\b", re.I)
_SPLIT = re.compile(r"[;\n]+|\.\s+|\.$")


def num(s) -> float | None:
    """'1,00,000' -> 100000.0, '−2.1' -> -2.1; None for anything else."""
    try:
        return float(str(s).replace("−", "-").replace(",", "").strip())
    except ValueError:
        return None


def _clip(s: str, n: int = 70) -> str:
    return "".join(c if c.isprintable() else " " for c in str(s))[:n].strip()


# ------------------------------------------------------------------ reference data
def _tail(path: Path, nbytes: int = 4096) -> str | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            lines = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
        return lines[-1] if lines else None
    except OSError:
        return None


def _head(path: Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readline().strip() or None
    except OSError:
        return None


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") and v > 0 else None


class Ref:
    """The engine's own numbers as of a day, read lazily and cached. Read-only; every reader tolerates a missing or broken file."""

    def __init__(self, root, day: str):
        self.root, self.day = Path(root), day
        self._series: dict[str, dict[str, float]] | None = None
        self._ranges: dict[str, tuple[float, float]] | None = None
        self._live: dict[str, float] = {}
        self._target: dict[str, dict] | None = None

    # --- daily closes {sym: {iso: close}}
    def _tick_close(self, iso: str) -> dict[str, float]:
        f = self.root / "logs" / f"ticks-{iso}.csv"
        head, last = _head(f), _tail(f)
        if not head or not last:
            return {}
        cols = [c.strip() for c in head.split(",")]
        vals = last.split(",")
        out = {}
        for s in SYMS:
            if s in cols and cols.index(s) < len(vals):
                v = _f(vals[cols.index(s)])
                if v:
                    out[s] = v
        return out

    def closes(self) -> dict[str, dict[str, float]]:
        if self._series is not None:
            return self._series
        ser: dict[str, dict[str, float]] = {s: {} for s in SYMS}
        try:
            files = sorted(p.name[6:16] for p in (self.root / "logs").glob("ticks-????-??-??.csv"))
        except OSError:
            files = []
        for iso in [x for x in files if x <= self.day][-TICK_FILES:]:
            for s, v in self._tick_close(iso).items():
                ser[s][iso] = v
        lp = _read_json(self.root / "state" / "last_prices.json")
        if isinstance(lp, dict):
            for s in SYMS:
                r = lp.get(s)
                if not isinstance(r, dict) or not _f(r.get("price")):
                    continue
                self._live[s] = float(r["price"])
                try:
                    d = date.fromisoformat(str(r.get("ts"))[:10])
                except ValueError:
                    continue
                if d.isoformat() <= self.day:
                    ser[s].setdefault(d.isoformat(), float(r["price"]))
                    if _f(r.get("prev_close")):
                        ser[s].setdefault(market.prev_trading_day(d).isoformat(), float(r["prev_close"]))
        reg = _read_json(self.root / "memory" / "ltm" / "regime.json")
        try:
            asof = date.fromisoformat(str(reg.get("asof"))[:10])
            dates = reg.get("recent_dates") or []
            for m, r in (reg.get("metals") or {}).items():
                cl = r.get("recent_closes") or []
                if m not in METAL_SYM or not cl or len(dates) < len(cl):
                    continue
                for md, c in zip(dates[-len(cl):], cl):
                    mo, dd = int(md[:2]), int(md[3:5])
                    y = asof.year if (mo, dd) <= (asof.month, asof.day) else asof.year - 1
                    iso = date(y, mo, dd).isoformat()
                    if iso <= self.day and _f(c):
                        ser[METAL_SYM[m]].setdefault(iso, float(c))
        except (AttributeError, TypeError, ValueError, IndexError):
            pass
        self._series = ser
        return ser

    def sessions(self, sym: str) -> list[tuple[str, float]]:
        return sorted(self.closes().get(sym, {}).items())

    def close_on(self, sym: str, iso: str) -> float | None:
        v = self.closes().get(sym, {}).get(iso)
        if v is None and iso <= self.day:
            v = self._tick_close(iso).get(sym)
        return v

    def latest(self, sym: str):
        s = self.sessions(sym)
        return s[-1] if s else None

    def day_range(self, sym: str) -> tuple[float, float] | None:
        """Low and high of today's ticks (the intraday prices a desk may have seen)."""
        f = self.root / "logs" / f"ticks-{self.day}.csv"
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            cols = [c.strip() for c in lines[0].split(",")]
            i = cols.index(sym)
            v = [x for x in (_f(r.split(",")[i]) for r in lines[1:] if r.count(",") >= i) if x]
            return (min(v), max(v)) if v else None
        except (OSError, ValueError, IndexError):
            return None

    def live(self, sym: str) -> float | None:
        self.closes()
        return self._live.get(sym)

    # --- trend numbers from the engine's TARGET records (latest on or before the day)
    def target(self, sym: str) -> dict | None:
        if self._target is None:
            self._target = {}
            try:
                files = sorted(p.name[10:20] for p in (self.root / "logs").glob("decisions-????-??-??.jsonl"))
            except OSError:
                files = []
            for iso in reversed([x for x in files if x <= self.day][-6:]):
                try:
                    lines = (self.root / "logs" / f"decisions-{iso}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for ln in reversed(lines):
                    if '"TARGET"' not in ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    s = r.get("symbol") if isinstance(r, dict) else None
                    if s in SYMS and s not in self._target and isinstance(r.get("level_raw"), (int, float)):
                        self._target[s] = dict(r, _date=iso)
                if len(self._target) == len(SYMS):
                    break
        return self._target.get(sym)


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


# ------------------------------------------------------------------ extraction
def _dates(chunk: str, day: str) -> list[tuple[str, str]]:
    """[(iso or 'invalid:<text>', text)] for the dates written in the chunk."""
    out = []
    for m in _ISO.finditer(chunk):
        try:
            out.append((date(int(m[1]), int(m[2]), int(m[3])).isoformat(), m[0]))
        except ValueError:
            out.append(("invalid", m[0]))
    try:
        ref = date.fromisoformat(day)
    except ValueError:
        ref = None
    for m in _DMY.finditer(chunk):
        mo = _MONTHS.index(m[2].lower()[:3]) + 1
        years = [int(m[3])] if m[3] else ([ref.year, ref.year - 1, ref.year + 1] if ref else [])
        cand = []
        for y in years:
            try:
                cand.append(date(y, mo, int(m[1])))
            except ValueError:
                pass
        if not cand:
            out.append(("invalid", m[0]))
        elif ref and not m[3]:
            out.append((min(cand, key=lambda d: abs((d - ref).days)).isoformat(), m[0]))
        else:
            out.append((cand[0].isoformat(), m[0]))
    return out


def extract(text, label: str, day: str) -> list[dict]:
    """Claims found in one text. Never raises. Each claim: {src, kind, sym, value, text, ...}."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text[:MAX_TEXT]
    claims: list[dict] = []
    for chunk in _SPLIT.split(text):
        if not chunk.strip():
            continue
        chunk_dates = _dates(chunk, day)
        found = []
        for m in _INVALIDATE.finditer(chunk):
            found.append({"kind": "level", "sym": m["sym"], "value": float(m["num"]), "side": "below", "text": m[0]})
        for m in _SMA.finditer(chunk):
            sm = _SYM_ANY.search(chunk)
            found.append({"kind": "sma", "sym": sm[1].upper() if sm else None, "n": int(m["n"]), "value": num(m["num"]),
                          "text": f"SMA{m['n']} {m['num']}"})
        for m in _HIGH.finditer(chunk):
            sm = _SYM_ANY.search(chunk)
            found.append({"kind": "donchian", "sym": sm[1].upper() if sm else None, "n": int(m["n"]), "value": num(m["num"]),
                          "text": f"{m['n']}-day high {m['num']}"})
        taken = [(m.start("num"), m.end("num")) for m in _SMA.finditer(chunk)] + [(m.start("num"), m.end("num")) for m in _HIGH.finditer(chunk)]
        for m in _CLAIM.finditer(chunk):
            if any(a <= m.start("num") < b for a, b in taken) or chunk[max(0, m.start() - 12):m.start()] == "close_below:":
                continue
            c = _classify(chunk, m, chunk_dates)
            if c:
                found.append(c)
        if chunk_dates and (found or _DATEWORD.search(chunk)):
            for iso, t in chunk_dates:
                found.append({"kind": "date", "sym": None, "value": iso, "text": t})
        for c in found:
            c["src"], c["_dates"] = label, [d for d, _ in chunk_dates if d != "invalid"]
            claims.append(c)
    return claims[:MAX_CLAIMS]


def _classify(chunk: str, m: re.Match, chunk_dates) -> dict | None:
    sym, gap, tail = m["sym"].upper(), m["gap"], m["tail"].strip().lower()
    v = num(m["num"])
    if v is None:
        return None
    ctx = chunk[max(0, m.start() - 30):m.end("gap")]
    snippet = _clip(chunk[m.start():m.end()])
    if tail in ("pp", "bps", "$", "usd", "/oz"):
        return None
    if tail in ("%", "pct", "percent"):
        if v > 0 and _DOWNW.search(gap) and not _UPW.search(gap):
            v = -v
        n = _NDAY.search(chunk)
        if n:
            hz = int(n[1])
        elif _WEEKW.search(chunk):
            hz = 5
        elif _DAYW.search(chunk):
            hz = 1
        else:
            hz = None
        return {"kind": "pct", "sym": sym, "value": v, "hz": hz, "text": snippet}
    if tail in ("pts", "pt", "point", "points"):
        sign = -1 if _DOWNW.search(gap) and not _UPW.search(gap) else (1 if _UPW.search(gap) else 0)
        return {"kind": "pts", "sym": sym, "value": abs(v) * (sign or 1), "signed": bool(sign), "text": snippet}
    cur = bool(re.search(r"₹|\bRs|\bINR", gap, re.I))
    word = bool(re.search(r"[A-Za-z]", re.sub(r"\bRs\b|\bINR\b", "", gap, flags=re.I)))
    if v < 0:
        return None
    if float(v).is_integer() and 1900 <= v <= 2100 and not cur and not word:
        return None                                                        # a year, not a price
    rel = _REL.search(gap)
    if rel:
        return {"kind": "relation", "sym": sym, "value": v, "above": rel[1].lower() in ("above", "over"), "text": snippet}
    if _LVL_ABOVE.search(ctx) or _LVL_BELOW.search(ctx):
        return {"kind": "level", "sym": sym, "value": v, "side": "above" if _LVL_ABOVE.search(ctx) and not _LVL_BELOW.search(ctx) else "below",
                "text": snippet}
    if not (cur or word or "." in m["num"] or ":" in gap):
        return None                                                        # a bare integer next to a symbol says nothing
    return {"kind": "price", "sym": sym, "value": v, "close": bool(_CLOSEW.search(ctx)), "text": snippet}


# ------------------------------------------------------------------ evaluation
def _px(x: float) -> str:
    return f"{x:,.2f}"


def _near(a: float, b: float, tol: float = TOL_PRICE) -> bool:
    return b != 0 and abs(a - b) / abs(b) <= tol


def _date_for(claim: dict, ref: Ref) -> str | None:
    ds = [d for d in claim.get("_dates", []) if d <= ref.day]
    return ds[0] if len(ds) == 1 else None


def evaluate(c: dict, ref: Ref) -> tuple[str, str]:
    k = c["kind"]
    if k == "date":
        return _eval_date(c, ref)
    if k == "sma" or k == "donchian":
        return _eval_trend(c, ref)
    sym = c.get("sym")
    ser = ref.sessions(sym)
    explicit = _date_for(c, ref)
    if explicit and k in ("price", "pct", "pts"):
        i = next((j for j, (d, _) in enumerate(ser) if d == explicit), None)
        px_ = ref.close_on(sym, explicit)
        if px_ is None:
            return UNVERIFIABLE, f"no {sym} close for {explicit}"
        ser = [x for x in ser if x[0] <= explicit] or [(explicit, px_)]
        if ser[-1][0] != explicit:
            ser.append((explicit, px_))
    if not ser:
        live = ref.live(sym)
        if live is None or k not in ("level", "relation"):
            return UNVERIFIABLE, f"no price data for {sym}"
        ser = [("live", live)]
    d0, p0 = ser[-1]
    last2 = ser[-2:]
    if k == "price":
        if explicit:
            ok = _near(c["value"], p0)
            return (OK if ok else MISMATCH), f"{sym} close {d0} {_px(p0)}"
        cands = [p for _, p in last2]
        rng = None if c["close"] else ref.day_range(sym)
        lv = None if c["close"] else ref.live(sym)
        ok = any(_near(c["value"], p) for p in cands) or (rng and rng[0] * (1 - TOL_PRICE) <= c["value"] <= rng[1] * (1 + TOL_PRICE)) \
            or (lv is not None and _near(c["value"], lv))
        return (OK if ok else MISMATCH), "; ".join(f"{sym} close {d} {_px(p)}" for d, p in reversed(last2))
    if k == "level":
        p = p0
        lv = ref.live(sym)
        if lv is not None and not explicit and d0 != "live" and ref.day == date.today().isoformat():
            p = lv
        good = c["value"] < p if c["side"] == "below" else c["value"] > p
        return (OK if good else MISMATCH), f"{sym} {_px(p)} ({d0}); a {c['side']}-price level must be {'<' if c['side'] == 'below' else '>'} it"
    if k == "relation":
        good = p0 > c["value"] if c["above"] else p0 < c["value"]
        return (OK if good else MISMATCH), f"{sym} close {d0} {_px(p0)}"
    if k == "pts":
        if len(ser) < 2:
            return UNVERIFIABLE, f"no previous {sym} close"
        ch = p0 - ser[-2][1]
        tol = max(0.005 * p0, 0.05)
        good = abs(ch - c["value"]) <= tol if c["signed"] else abs(abs(ch) - abs(c["value"])) <= tol
        return (OK if good else MISMATCH), f"{sym} {ser[-2][0]}->{d0} {ch:+.2f} pts"
    if k == "pct":
        hz = c.get("hz")
        if hz is None:
            return UNVERIFIABLE, "no horizon (say 'on the day' or '5d') so no comparison"
        if hz < 1 or len(ser) < hz + 1:
            return UNVERIFIABLE, f"fewer than {hz + 1} {sym} closes on file"
        tops = [(len(ser) - 1, d0)] if explicit or hz != 1 else [(len(ser) - 1, d0)] + ([(len(ser) - 2, ser[-2][0])] if len(ser) >= 3 else [])
        res = []
        for i, d in tops:
            if i - hz < 0:
                continue
            res.append((d, (ser[i][1] / ser[i - hz][1] - 1) * 100))
        if not res:
            return UNVERIFIABLE, "not enough closes"
        good = any(abs(r - c["value"]) <= TOL_PP for _, r in res)
        return (OK if good else MISMATCH), "; ".join(f"{sym} {hz}d to {d} {r:+.2f}%" for d, r in res)
    return UNVERIFIABLE, "unsupported claim"


def _eval_trend(c: dict, ref: Ref) -> tuple[str, str]:
    sym = c.get("sym")
    if not sym:
        return UNVERIFIABLE, "no symbol named next to it"
    t = ref.target(sym)
    if not t:
        return UNVERIFIABLE, f"no engine TARGET record for {sym}"
    want = f"sma{c['n']}" if c["kind"] == "sma" else f"donchian{c['n']}"
    if t.get("level_kind") != want:
        return UNVERIFIABLE, f"engine uses {t.get('level_kind')} for {sym}, not {want}"
    raw, lvl = t.get("level_raw"), t.get("level")
    ok = _near(c["value"], raw) or (isinstance(lvl, (int, float)) and _near(c["value"], lvl))
    return (OK if ok else MISMATCH), f"{sym} {want} {_px(raw)}" + (f" (trigger level {_px(lvl)})" if isinstance(lvl, (int, float)) else "") + f" @{t['_date']}"


def _eval_date(c: dict, ref: Ref) -> tuple[str, str]:
    iso = c["value"]
    if iso == "invalid":
        return MISMATCH, "not a real calendar date"
    d = date.fromisoformat(iso)
    if not market.holidays_known(d.year):
        return UNVERIFIABLE, f"NSE holidays for {d.year} not on file"
    ok = market.is_trading_day(d)
    return (OK if ok else MISMATCH), f"{iso} {d:%a} is {'an NSE trading day' if ok else 'not an NSE trading day'}"


# ------------------------------------------------------------------ sources of text
def _lst(x) -> list:
    return x if isinstance(x, list) else []


def desk_texts(obj) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(obj, dict):
        return out
    for i, f in enumerate(_lst(obj.get("facts")), 1):
        if isinstance(f, dict):
            out.append((f"facts[{i}]", " ".join(str(f.get(k, "")) for k in ("metric", "value", "unit", "asof")).strip()))
    view = obj.get("view")
    for m, v in (view.items() if isinstance(view, dict) else []):
        if isinstance(v, dict):
            out.append((f"view.{m}.why", str(v.get("why", ""))))
    for i, r in enumerate(_lst(obj.get("risks")), 1):
        out.append((f"risks[{i}]", str(r)))
    return out


def proposal_texts(obj) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(obj, dict):
        return out
    rat = obj.get("rationale")
    for m, t in (rat.items() if isinstance(rat, dict) else []):
        out.append((f"rationale.{m}", str(t)))
    for i, s in enumerate(_lst(obj.get("scenarios")), 1):
        if isinstance(s, dict) and s.get("why"):
            out.append((f"scenario[{i}].why", str(s["why"])))
    for i, x in enumerate(_lst(obj.get("invalidate")), 1):
        out.append((f"invalidate[{i}]", str(x)))
    if obj.get("note"):
        out.append(("note", str(obj["note"])))
    return out


def run(texts: list[tuple[str, str]], root, day: str) -> dict:
    """Check every text; the report is {date, rows, n_ok, n_mismatch, n_unverifiable, ok, factcheck:{ok,n_mismatch}}. Never raises."""
    rows: list[dict] = []
    try:
        ref = Ref(root, day)
        for label, text in texts:
            for c in extract(text, label, day):
                try:
                    st, data = evaluate(c, ref)
                except Exception as e:                                       # noqa: BLE001 - a broken data file is UNVERIFIABLE
                    st, data = UNVERIFIABLE, f"check failed ({type(e).__name__})"
                rows.append({"src": c["src"], "kind": c["kind"], "claim": _clip(c["text"]), "status": st, "data": _clip(data, 120)})
        err = None
    except Exception as e:                                                   # noqa: BLE001
        err = type(e).__name__
    n = {s: sum(1 for r in rows if r["status"] == s) for s in (OK, MISMATCH, UNVERIFIABLE)}
    rep = {"date": day, "rows": rows, "n_ok": n[OK], "n_mismatch": n[MISMATCH], "n_unverifiable": n[UNVERIFIABLE],
           "ok": n[MISMATCH] == 0, "factcheck": {"ok": n[MISMATCH] == 0, "n_mismatch": n[MISMATCH]}}
    if err:
        rep["error"] = err
    return rep


def check_desk(root, day: str, desk: str, obj) -> dict:
    rep = run([(f"{desk}.{k}", t) for k, t in desk_texts(obj)], root, day)
    rep["desk"] = desk
    return rep


def check_proposal(root, day: str, obj) -> dict:
    rep = run([(f"proposal.{k}", t) for k, t in proposal_texts(obj)], root, day)
    rep["desk"] = "proposal"
    return rep


def warning_lines(rep: dict) -> list[str]:
    """WARNING lines for the tools that print them (empty when nothing mismatches)."""
    bad = [r for r in rep.get("rows", []) if r["status"] == MISMATCH]
    if not bad:
        return []
    return [f"WARNING: factcheck: {len(bad)} stated number(s) do not match the engine's data (advice only, nothing refused):"] + [
        f"  {r['src']}: '{r['claim']}' vs {r['data']}" for r in bad[:6]]


def table(rep: dict) -> str:
    rows = rep.get("rows", [])
    L = [f"factcheck {rep.get('date')} {rep.get('desk', '')}: {rep['n_ok']} OK, {rep['n_mismatch']} MISMATCH, {rep['n_unverifiable']} UNVERIFIABLE"]
    for r in rows:
        L.append(f"  {r['status']:<12} {r['src']:<22} {r['claim']:<34} {r['data']}")
    return "\n".join(L)
