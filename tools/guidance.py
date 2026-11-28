#!/usr/bin/env python3
"""Validated writers for the files the engine and the daily report read, so routines never hand-edit JSON.

  python tools/guidance.py set --bias GOLD=favour SILVER=neutral COPPER=block --note "why"
  python tools/guidance.py touch --note "checked, nothing changed"      # keep the bias, refresh the time, append the note
  python tools/guidance.py fastlane --symbol SILVERBEES --block-minutes 60 --sell-fraction 0.5 --tighten-stop 205.1 --note "why"
  python tools/guidance.py fastlane --symbol ALL --halt-buys-today --note "why"   # ALL also takes --block-minutes / --bias
  python tools/guidance.py events add --date 2026-10-14 --type CPI [--time 18:00]
  python tools/guidance.py events list | events remove --date D --type T
  python tools/guidance.py market-today --item "Gold (global):+0.8:Weaker dollar" --item "USD/INR:0.1:Rupee a bit weaker"
  python tools/guidance.py tomorrow --text "US CPI after the close..."
  python tools/guidance.py allocation set --phase provisional|final --weights GOLDBEES=0.70 SILVERBEES=0.20 HINDCOPPER=0.10 --confidence 0.55 \
      --scenario "base:0.50:GOLD=flat,SILVER=flat,COPPER=down:DXY 104" (2 to 4 times) --rationale GOLD="..." SILVER="..." COPPER="..." \
      [--invalidate close_below:SILVERBEES:158 ...] [--horizon-days 5] [--for-date D] [--desks macro=ok ...] [--note "..."]
  python tools/guidance.py allocation explain [--file PATH] | show | hold-default --note "why"      (strategy v3, dormant)

Everything is checked before anything is written. Exit 0 = written, exit 2 = refused (nothing written).
`allocation` (strategy v3, dormant) writes state/allocation.json through simgold.allocation.sanitize_allocation(for_write=True), the
same filter the engine applies on read: weights are SHARES of the invested book (they sum to 1.00), there is no cash, size or quantity
option and no symbol outside GOLDBEES, SILVERBEES, HINDCOPPER. Weights total 1.00 within 0.02 (refused below 0.98 and above 1.02; aim for
exactly 1.00; float rounding at the boundary is tolerated). --invalidate takes only close_below:SYMBOL:price (up to 8). for_date defaults to
today before 15:30 on a trading day, else the next trading day, and is at most 5 calendar days ahead.
SHADOW RULE: while the proposal is only logged and scored (strategy.mode not v3_alloc, allocation.shadow true, phase shadow or ai_trust 0)
this tool never touches state/guidance.json, so a proposal cannot change what v2 / D5-FI trades (the only bias is an explicit `set`).
When the proposal trades live it also writes a derived `bias` for the v2 rollback: never favour, an existing block is kept (never lowered or
lifted), SILVER/COPPER weights more than 0.10 below default add a block, GOLD is never blocked (GOLDBEES is the sink).
It records the scenario set in the calibration ledger (tools/calibration.py). The preview it prints never refuses.
`fastlane` runs the very same filter the engine runs (guard.sanitize_fastlane): no favour, no buys, no loosening
of a stop, sell/tighten only on a position that exists. Files: state/guidance.json, state/fastlane.json,
state/events.json, state/market_today.json, state/tomorrow.txt.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import ROOT, iso_day, parse_now, utf8_stdout

from simgold import allocation, brief, config, guard, market, memsafe, memstore, regime
from simgold.broker import Portfolio

KEYS = ("GOLD", "SILVER", "COPPER")
VALUES = ("favour", "neutral", "block")
EVENT_TYPES = ("CPI", "FOMC", "JOBS", "RBI", "HINDCOPPER_RESULTS", "BUDGET")
MAX_NOTE = 300
MAX_BLOCK_MIN = 600


class Refused(Exception):
    pass


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, obj) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def clean_note(note: str | None) -> str:
    n = " ".join((note or "").split())
    if len(n) > MAX_NOTE:
        raise Refused(f"--note is {len(n)} characters; keep it under {MAX_NOTE}")
    if n:
        try:
            memsafe.check_text(note, "bias_note", one_line=False)      # the raw text: a newline cannot hide a "system:" line start
        except memsafe.Refused as e:
            raise Refused(f"--note rejected: {e}")
    return n


def symbol(name: str, p: dict) -> str:
    s = config.symbol_of(name)
    if s not in p["instruments"]:
        raise Refused(f"unknown symbol {name!r}; use one of {', '.join(p['instruments'])}")
    return s


# ------------------------------------------------------------------ set (guidance.json)
def _current_bias(root: Path, p: dict) -> dict:
    old = brief.load_json(root / "state" / "guidance.json", {})
    prev = {config.symbol_of(k): guard.normalise_bias(v) for k, v in (old.get("bias") or {}).items()} \
        if isinstance(old, dict) and isinstance(old.get("bias"), dict) else {}
    return {k: prev.get(p["engine"]["aliases"][k], "neutral") for k in KEYS}


def _alignment_states(root: Path, now: datetime) -> dict:
    try:
        return {m: r["state"] for m, r in memstore.current_alignment(root, now).items()}
    except Exception:
        return {}


def _write_guidance(root: Path, now: datetime, bias: dict, note: str) -> None:
    prices = {}
    lp = brief.load_json(root / "state" / "last_prices.json", {})
    for s, row in (lp.items() if isinstance(lp, dict) else []):
        if isinstance(row, dict) and row.get("price"):
            prices[s] = row["price"]
    write_json(root / "state" / "guidance.json",
               {"updated": now.isoformat(timespec="seconds"), "bias": bias, "note": note, "prices_at_update": prices,
                "alignment": _alignment_states(root, now)})


def enforce_ceiling(root: Path, now: datetime, p: dict, bias: dict) -> list[str]:
    """Lower any bias above the alignment ceiling, in place. Only ever lowers. Returns the printed downgrade lines.
    Missing/stale memory means LTM FLAT, so the ceiling is neutral at most (the safe default)."""
    try:
        al = memstore.current_alignment(root, now, p)
    except Exception:       # advisory: if alignment cannot be computed, fall back to the neutral ceiling
        al = {k: {"ceiling": "neutral", "state": "UNKNOWN"} for k in KEYS}
    down = []
    for k in KEYS:
        new, lowered = regime.apply_ceiling(bias[k], al.get(k, {}).get("ceiling", "neutral"))
        if lowered:
            down.append(f"downgraded {k} {bias[k]}->{new}: {al[k]['state']}")
            bias[k] = new
    return down


def cmd_set(a, root: Path, now: datetime, p: dict) -> str:
    out = _current_bias(root, p)          # keys not mentioned keep their current value (neutral if there is no file)
    for item in a.bias:
        k, sep, v = item.partition("=")
        k, v = k.strip().upper(), v.strip().lower()
        if not sep or k not in KEYS:
            raise Refused(f"bad --bias item {item!r}: use KEY=VALUE with KEY in {'/'.join(KEYS)}")
        if v not in VALUES:
            raise Refused(f"bad value {v!r} for {k}: use one of {'/'.join(VALUES)}")
        out[k] = v
    note = clean_note(a.note)
    down = enforce_ceiling(root, now, p, out)
    if down:
        tail = " | CEILING: " + "; ".join(down)
        note = (note[:max(0, MAX_NOTE - len(tail))].rstrip() + tail)[:MAX_NOTE]
    _write_guidance(root, now, out, note)
    msg = "guidance.json written: " + ", ".join(f"{k}={v}" for k, v in out.items())
    return msg + ("".join("\n" + d for d in down) if down else "")


def cmd_touch(a, root: Path, now: datetime, p: dict) -> str:
    """Keep the bias, refresh `updated` (so the engine does not treat it as stale), append the note.
    With no guidance.json yet: all neutral."""
    old = brief.load_json(root / "state" / "guidance.json", {})
    prev_note = " ".join(str(old.get("note", "")).split()) if isinstance(old, dict) else ""
    add = clean_note(a.note)
    note = " | ".join(x for x in (prev_note, f"{now:%H:%M} {add}" if add else "") if x)
    if len(note) > MAX_NOTE:
        note = "..." + note[-(MAX_NOTE - 3):]
    bias = _current_bias(root, p)
    _write_guidance(root, now, bias, note)
    return "guidance.json refreshed: " + ", ".join(f"{k}={v}" for k, v in bias.items())


# ------------------------------------------------------------------ fastlane (fastlane.json)
def _split_groups(argv: list[str]) -> list[list[str]]:
    """Split on each --symbol so one call can cover several instruments."""
    groups, cur = [], []
    for tok in argv:
        if tok == "--symbol" and cur:
            groups.append(cur)
            cur = []
        cur.append(tok)
    if cur:
        groups.append(cur)
    return groups


def _group_parser() -> argparse.ArgumentParser:
    g = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    g.add_argument("--symbol")
    g.add_argument("--block-minutes", type=float)
    g.add_argument("--sell-fraction", type=float)
    g.add_argument("--tighten-stop", type=float)
    g.add_argument("--bias")
    g.add_argument("--halt-buys-today", action="store_true")
    g.add_argument("--note")
    return g


def cmd_fastlane(argv: list[str], root: Path, now: datetime, p: dict) -> str:
    raw: dict = {"updated": now.isoformat(timespec="seconds")}
    notes: list[str] = []
    for grp in _split_groups(argv):
        try:
            g, extra = _group_parser().parse_known_args(grp)
        except (argparse.ArgumentError, SystemExit) as e:
            raise Refused(f"bad fastlane arguments: {e}")
        if extra:
            raise Refused(f"unknown fastlane option(s): {' '.join(extra)} "
                          f"(the fast lane is defence only: block, sell fraction, tighten stop, neutral/block bias, halt)")
        if g.note:
            notes.append(clean_note(g.note))
        if g.halt_buys_today:
            raw["halt_buys_today"] = True
        spec: dict = {}
        if g.block_minutes is not None:
            if not (0 < g.block_minutes <= MAX_BLOCK_MIN):
                raise Refused(f"--block-minutes must be between 0 and {MAX_BLOCK_MIN}")
            spec["block_until"] = (now + timedelta(minutes=g.block_minutes)).isoformat(timespec="seconds")
        if g.sell_fraction is not None:
            spec["sell_fraction"] = g.sell_fraction
        if g.tighten_stop is not None:
            spec["tighten_stop"] = g.tighten_stop
        if g.bias is not None:
            spec["bias"] = g.bias.strip().lower()
        if spec and not g.symbol:
            raise Refused("--block-minutes / --sell-fraction / --tighten-stop / --bias need --symbol")
        all_syms = bool(g.symbol) and g.symbol.strip().upper() == "ALL"
        if all_syms:
            if "sell_fraction" in spec or "tighten_stop" in spec:
                raise Refused("--symbol ALL only takes --block-minutes, --bias and --halt-buys-today; "
                              "sell and stop changes need a real symbol")
            if not spec and not g.halt_buys_today:
                raise Refused("nothing to do for ALL: give --halt-buys-today, --block-minutes or --bias")
            for sym in p["instruments"]:
                if sym in raw:
                    raise Refused(f"{sym} given twice in one call")
                if spec:
                    raw[sym] = dict(spec)
            continue
        if g.symbol and not spec and not g.halt_buys_today:
            raise Refused(f"nothing to do for {g.symbol}: give --block-minutes, --sell-fraction, --tighten-stop or --bias")
        if spec:
            sym = symbol(g.symbol, p)
            if sym in raw:
                raise Refused(f"{sym} given twice in one call")
            raw[sym] = spec
    if len(raw) == 1:
        raise Refused("nothing to write: give --symbol with an action, or --halt-buys-today")
    # the engine's own filter, with the live positions: anything it would drop is refused here
    pf = Portfolio.load(root / "state" / "portfolio.json") if (root / "state" / "portfolio.json").exists() else Portfolio()
    pos = {s: {"qty": q.qty, "price": q.last_price or q.avg_cost, "stop": q.stop} for s, q in pf.positions.items()}
    acc, rej = guard.sanitize_fastlane(raw, now, pos, p)
    if rej:
        raise Refused("the engine would refuse this: " + "; ".join(rej))
    if not acc:
        raise Refused("the engine would ignore everything in this request")
    if notes:
        raw["note"] = " | ".join(notes)[:MAX_NOTE]
    write_json(root / "state" / "fastlane.json", raw)
    return "fastlane.json written (replaces the previous one): " + "; ".join(_describe(x) for x in acc)


def _describe(a: dict) -> str:
    t = a["type"]
    if t == "block_until":
        return f"{a['sym']} no new buys until {a['until']:%H:%M}"
    if t == "sell_fraction":
        return f"{a['sym']} sell {a['fraction'] * 100:.0f}%"
    if t == "tighten_stop":
        return f"{a['sym']} stop up to {a['stop']:.2f}"
    if t == "set_bias":
        return f"{a['sym']} bias {a['bias']}"
    return "halt all new buys today"


# ------------------------------------------------------------------ events.json
def _read_events(root: Path) -> list[dict]:
    raw = brief.load_json(root / "state" / "events.json", [])
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    return [e for e in raw if isinstance(e, dict) and e.get("date") and e.get("type")] if isinstance(raw, list) else []


def strict_day(s, what: str, example: str = "2026-10-14") -> date:
    """YYYY-MM-DD only (date.fromisoformat also takes 20261118 and week dates on Python 3.11)."""
    try:
        return iso_day(s)
    except ValueError:
        raise Refused(f"{what} must look like {example} (YYYY-MM-DD) and be a real date, got {str(s)[:20]!r}")


def _date(s: str) -> date:
    return strict_day(s, "--date")


def cmd_events(a, root: Path, now: datetime, p: dict) -> str:
    path = root / "state" / "events.json"
    ev = _read_events(root)
    if a.action == "list":
        return "\n".join(f"{e['date']} {e['type']}" + (f" {e['time']}" if e.get("time") else "") for e in ev) or "no events"
    if not a.date or not a.type:
        raise Refused("events add/remove need --date and --type")
    d, typ = _date(a.date), a.type.upper()
    if typ not in EVENT_TYPES:
        raise Refused(f"--type must be one of {'/'.join(EVENT_TYPES)}")
    ev = [e for e in ev if not (str(e["date"])[:10] == d.isoformat() and str(e["type"]).upper() == typ)]
    if a.action == "remove":
        write_json(path, ev)
        return f"removed {typ} {d}"
    if d < now.date():
        raise Refused(f"{d} is in the past")
    if d > now.date() + timedelta(days=400):
        raise Refused(f"{d} is more than a year away; check the date")
    row = {"date": d.isoformat(), "type": typ}
    if a.time:
        try:
            datetime.strptime(a.time, "%H:%M")
        except ValueError:
            raise Refused("--time must look like 18:00 (IST)")
        row["time"] = a.time
    ev.append(row)
    ev.sort(key=lambda e: (str(e["date"]), str(e["type"])))
    write_json(path, ev)
    return f"events.json: added {typ} on {d}" + (f" at {a.time}" if a.time else "")


# ------------------------------------------------------------------ market_today.json and tomorrow.txt
def cmd_market_today(a, root: Path, now: datetime, p: dict) -> str:
    rows = []
    for item in a.item:
        parts = item.split(":", 2)
        if len(parts) < 2 or not parts[0].strip():
            raise Refused(f"bad --item {item!r}: use NAME:MOVE_PCT[:NOTE]")
        try:
            mv = float(parts[1])
        except ValueError:
            raise Refused(f"bad move {parts[1]!r} in {item!r}: MOVE_PCT must be a number such as -0.4")
        if abs(mv) > 40:
            raise Refused(f"{mv}% looks wrong for {parts[0].strip()}; MOVE_PCT is a percent, not a ratio")
        note = " ".join(parts[2].split()) if len(parts) > 2 else ""
        if len(note) > 200:
            raise Refused(f"note for {parts[0].strip()} is over 200 characters")
        if note:
            try:                            # shown in the owner's email: facts only, no links, no instructions
                memsafe.check_text(note, "why")
            except memsafe.Refused as e:
                raise Refused(f"note for {parts[0].strip()} rejected: {e}")
        rows.append({"name": parts[0].strip(), "move_pct": mv, "note": note})
    if not rows:
        raise Refused("give at least one --item")
    write_json(root / "state" / "market_today.json", {"date": (a.date or now.date().isoformat()), "market": rows})
    return f"market_today.json written with {len(rows)} item(s)"


def cmd_tomorrow(a, root: Path, now: datetime, p: dict) -> str:
    text = " ".join(a.text.split())
    if not text:
        raise Refused("--text is empty")
    if len(text) > 600:
        raise Refused(f"--text is {len(text)} characters; keep it under 600 (plain sentences, no markup)")
    if "<" in text:
        raise Refused("--text must be plain text (no HTML)")
    try:                                    # it goes into the owner's email: facts only, no links, no instructions
        memsafe.check_text(text, "report_text", one_line=False)
    except memsafe.Refused as e:
        raise Refused(f"--text rejected: {e}")
    write_atomic(root / "state" / "tomorrow.txt", text + "\n")
    return "tomorrow.txt written"


# ------------------------------------------------------------------ allocation (state/allocation.json, strategy v3, dormant)
ALLOC_SYMS = allocation.SYMS
ALLOC_METALS = ("GOLD", "SILVER", "COPPER")
DIRS = ("up", "flat", "down")
DESK_STATES = ("ok", "missing", "abstain", "invalid", "degraded")
REFUSED_FLAGS = ("--cash", "--qty", "--quantity", "--size", "--leverage", "--shares", "--units", "--amount", "--buy", "--sell",
                 "--order", "--stop", "--floor", "--cap", "--lots")


def _fraction(v, what: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        raise Refused(f"{what}: {str(v)[:20]!r} is not a number (weights are shares such as 0.70, not 70 or 70%)")
    if not math.isfinite(x):
        raise Refused(f"{what}: {str(v)[:20]!r} is not a number in [0, 1] (not a finite number)")
    return x


WEIGHT_LO, WEIGHT_HI, WEIGHT_EPS = 0.98, 1.02, 1e-9


def settle_weights(w: dict) -> dict:
    """The tool's own total check, tolerant of float rounding (0.70 + 0.20 + 0.08 is 0.9799999999999999 in floating point and must
    pass at the 0.98 boundary). Total 1.00 within 0.02: refused below 0.98 and above 1.02. A total that is not exactly 1.00 is
    normalised here (the engine would do the same), so the filter never sees a rounding artefact. Aim for exactly 1.00."""
    if set(w) != set(ALLOC_SYMS):
        raise Refused(f"--weights must name all three of {', '.join(ALLOC_SYMS)} (shares that sum to 1.00)")
    for k, v in w.items():
        if v < 0 or v > 1:
            raise Refused(f"weight of {k} is not a number in [0, 1] (got {v:g}; weights are shares such as 0.70, not 70)")
    tot = sum(w.values())
    if tot < WEIGHT_LO - WEIGHT_EPS:
        raise Refused(f"weights sum to {tot:.3f}: shares must sum to 1.00 (refused below {WEIGHT_LO:.2f}: a shortfall would be cash, "
                      f"which is not a choice; aim for exactly 1.00)")
    if tot > WEIGHT_HI + WEIGHT_EPS:
        raise Refused(f"weights sum to {tot:.3f}: shares must sum to 1.00 (refused above {WEIGHT_HI:.2f}: more than 100 percent is "
                      f"leverage; aim for exactly 1.00)")
    if abs(tot - 1.0) <= WEIGHT_EPS:
        return dict(w)
    out = {s: round(w[s] / tot, 6) for s in ALLOC_SYMS}
    out["GOLDBEES"] = round(out["GOLDBEES"] + (1.0 - sum(out.values())), 6)
    return out


def parse_weights(items) -> dict:
    if not items:
        raise Refused("--weights is required: GOLDBEES=0.70 SILVERBEES=0.20 HINDCOPPER=0.10 (shares that sum to 1.00)")
    out: dict = {}
    for it in items:
        k, sep, v = str(it).partition("=")
        k = k.strip().upper()
        if not sep:
            raise Refused(f"bad --weights item {it!r}: use SYMBOL=share")
        if k not in ALLOC_SYMS:
            raise Refused(f"{k or it!r} is not allowed in --weights: only {', '.join(ALLOC_SYMS)} "
                          f"(no cash, no LIQUIDBEES, no other symbol; the engine reinvests rounding dust itself)")
        if k in out:
            raise Refused(f"{k} given twice in --weights")
        out[k] = _fraction(v, f"weight of {k}")
    return out


def raw_check(label: str, raw) -> None:
    """Refuse a chat-role line start ("system:", "assistant:", "user:") in the raw text, before whitespace is collapsed."""
    rp = memsafe.role_prefix(raw)
    if rp:
        raise Refused(f"{label} starts a line with a chat-role prefix ({rp!r}); proposals hold facts and numbers only")


def parse_scenario(item: str) -> dict:
    raw_check("--scenario", item)
    parts = str(item).split(":", 3)
    if len(parts) < 3 or not parts[0].strip():
        raise Refused(f"bad --scenario {item!r}: use name:p:GOLD=up,SILVER=flat,COPPER=down:why")
    sc: dict = {"name": " ".join(parts[0].split()), "p": _fraction(parts[1], f"probability of scenario {parts[0].strip()!r}")}
    for tok in [t for t in parts[2].split(",") if t.strip()]:
        m, sep, d = tok.partition("=")
        m, d = m.strip().upper(), d.strip().lower()
        if not sep or m not in ALLOC_METALS or d not in DIRS:
            raise Refused(f"bad direction {tok!r} in scenario {sc['name']!r}: use GOLD|SILVER|COPPER=up|flat|down")
        if m in sc:
            raise Refused(f"{m} given twice in scenario {sc['name']!r}")
        sc[m] = d
    if len(parts) > 3 and parts[3].strip():
        raw_check(f"scenario {sc['name']!r} why", parts[3])
        sc["why"] = " ".join(parts[3].split())
    return sc


def parse_rationale(items) -> dict:
    out: dict = {}
    for it in items or []:
        k, sep, v = str(it).partition("=")
        k = k.strip().upper()
        if not sep or k not in ALLOC_METALS:
            raise Refused(f"bad --rationale item {it!r}: use GOLD=\"text with a number\" (keys GOLD, SILVER, COPPER; quote the text)")
        if k in out:
            raise Refused(f"{k} given twice in --rationale")
        raw_check(f"rationale {k}", v)
        out[k] = " ".join(v.split())
    return out


def parse_invalidate(groups) -> list:
    """--invalidate takes ONLY close_below:SYMBOL:price (up to 8, symbol one of the three, price a plain positive decimal). The other
    forms the weekly thesis tool knows (close_above, regime_is, driver_*, date) are refused here: the engine enforces one form."""
    flat = [x for grp in (groups or []) for x in grp]
    if len(flat) > 8:
        raise Refused(f"--invalidate has {len(flat)} entries; at most 8")
    for x in flat:
        m = allocation._RE_INVALIDATE.match(x) if isinstance(x, str) else None            # noqa: SLF001
        if not m:
            raise Refused(f"--invalidate {str(x)[:40]!r} is refused: the only form is close_below:SYMBOL:price with SYMBOL one of "
                          f"{', '.join(ALLOC_SYMS)} (for example close_below:SILVERBEES:158); close_above, regime_is and date forms are not accepted")
        if not 0 < float(m.group(2)) < 1e7:
            raise Refused(f"--invalidate {x!r}: the price must be a positive number")
    return flat


def parse_desks(items) -> dict:
    out: dict = {}
    for it in items or []:
        k, sep, v = str(it).partition("=")
        k, v = k.strip().lower(), v.strip().lower()
        if not sep or k not in ("macro", "gold", "silver", "copper", "risk", "memory") or v not in DESK_STATES:
            raise Refused(f"bad --desks item {it!r}: use macro|gold|silver|copper|risk|memory={'|'.join(DESK_STATES)}")
        out[k] = v
    return out


def default_for_date(now: datetime) -> date:
    d = now.date()
    return d if market.is_trading_day(d) and now.time() < market.CLOSE else market.next_trading_day(d)


def _for_date(a, now: datetime) -> str:
    if not getattr(a, "for_date", None):
        return default_for_date(now).isoformat()
    return strict_day(a.for_date, "--for-date", "2026-11-18").isoformat()


def _snapshot(root: Path, now: datetime, p: dict):
    import alloc_ctx
    try:
        return alloc_ctx.snapshot(root, now, p, fetch=True)
    except Exception as e:                      # noqa: BLE001 - the default is only used for derived fields and the preview
        return {"default": None, "warnings": [f"state unreadable ({type(e).__name__})"], "prices": {}, "cur": {}}


def _read_alloc(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        return "unreadable"


def proposal_is_live(p: dict, cfg: dict) -> bool:
    """True only when the engine trades the AI tilt: strategy.mode v3_alloc, allocation.shadow false, phase live, ai_trust above 0.
    In every other state (v2_trend or v1_scores, shadow true, phase shadow, trust 0) a proposal is logged and scored, never traded."""
    return bool(p["strategy"]["mode"] == "v3_alloc" and not cfg["shadow"] and cfg["phase"] == "live" and cfg["ai_trust"] > 0)


def derived_bias(clean: dict, snap: dict, root: Path, p: dict) -> dict:
    """The v2-rollback bias written ONLY while the proposal trades live (see proposal_is_live). Rules:
      * never favour (an existing favour is lowered to neutral);
      * an existing `block` is kept: the derived bias never lowers or lifts one (the stricter of existing and derived wins);
      * a SILVER or COPPER weight more than 0.10 below its default adds a block; GOLD is never blocked (GOLDBEES is the sink: a gold
        weight below default means the book rotates out of gold, which a bias block would only make worse on a rollback);
      * without a default (no history) or with hold-default nothing is derived: favour becomes neutral, a block stays."""
    existing = _current_bias(root, p)
    d, w = snap.get("default"), clean["weights"]
    out = {}
    for m in ALLOC_METALS:
        sym = {"GOLD": "GOLDBEES", "SILVER": "SILVERBEES", "COPPER": "HINDCOPPER"}[m]
        derived = "neutral"
        if m != "GOLD" and d is not None and w != "default" and w[sym] < d[sym] - 0.10:
            derived = "block"
        out[m] = "block" if existing.get(m) == "block" or derived == "block" else "neutral"
    return out


def _fmt(m: dict) -> str:
    return "/".join(f"{m[s] * 100:.0f}" for s in ALLOC_SYMS)


def _preview_lines(snap: dict, obj: dict | None, now: datetime, p: dict) -> list[str]:
    """The clamp preview (never refuses): what the engine would do with this proposal at the configured trust and at full trust."""
    import alloc_ctx
    try:
        r1, _n = alloc_ctx.preview(snap, obj, now, p)
        r2, _n = alloc_ctx.preview(snap, obj, now, p, trust=1.0)
        return alloc_ctx.render_preview(snap, r1, r2)
    except Exception as e:                      # noqa: BLE001
        why = (snap.get("warnings") or [str(e)])[0] if snap.get("default") is None else f"{type(e).__name__}: {e}"
        return [f"preview unavailable: {why}"]


def _write_alloc(root: Path, clean: dict) -> None:
    write_json(root / "state" / "allocation.json", clean)


def cmd_allocation(a, root: Path, now: datetime, p: dict) -> str:
    act = a.action
    path = root / "state" / "allocation.json"
    cfg = allocation.settings(p)
    if act == "show":
        return _alloc_show(path, now, cfg)
    if act == "explain":
        src = Path(a.file) if a.file else path
        obj = _read_alloc(src)
        snap = _snapshot(root, now, p)
        head = f"explain {src.name}: " + ("no proposal: the default runs" if obj is None else
                                          "file unreadable: the default runs" if obj == "unreadable" else
                                          f"{obj.get('phase')} for {obj.get('for_date')}" if isinstance(obj, dict) else "not an object")
        return "\n".join([head] + _preview_lines(snap, obj if isinstance(obj, dict) else None, now, p) +
                         [f"warning: {w}" for w in snap.get("warnings", [])])
    # ---- writes: build, filter, then write
    phase = a.phase
    obj: dict = {"for_date": _for_date(a, now), "written": now.isoformat(timespec="seconds"),
                 "by": a.by or ("premarket" if phase == "provisional" else "allocation"), "phase": phase}
    if act == "hold-default":
        if not a.note:
            raise Refused("hold-default needs --note \"why\" (an explicit no-view is a decision: say what was missing)")
        obj.update(weights="default", confidence=0.0)
    else:
        obj["weights"] = settle_weights(parse_weights(a.weights))
        if a.confidence is None:
            raise Refused("--confidence is required (0 to 1)")
        obj["confidence"] = a.confidence
        sc = [parse_scenario(x) for x in a.scenario or []]
        obj["scenarios"] = sc
        rat = parse_rationale(a.rationale)
        if rat:
            obj["rationale"] = rat
        obj["invalidate"] = parse_invalidate(a.invalidate)
        hd = a.horizon_days if a.horizon_days is not None else 5.0
        obj["horizon_days"] = int(hd) if float(hd).is_integer() else hd
    if a.desks:
        obj["desks"] = parse_desks(a.desks)
    if a.note:
        raw_check("--note", a.note)
        obj["note"] = " ".join(a.note.split())
    if a.by:
        raw_check("--by", a.by)
    snap = _snapshot(root, now, p)
    if "desks" not in obj:
        try:
            import alloc_ctx
            obj["desks"] = alloc_ctx.desk_statuses(root, obj["for_date"], p)
        except Exception:                       # noqa: BLE001 - informational
            pass
    if snap.get("default"):
        obj["default_at_write"] = {s: round(snap["default"][s], 4) for s in ALLOC_SYMS}
    if snap.get("prices"):
        obj["prices_at_update"] = {s: round(v, 4) for s, v in snap["prices"].items() if s in ALLOC_SYMS}
    b = brief.load_json(root / "state" / "budget.json", {})
    if isinstance(b, dict) and b.get("mode") in ("NORMAL", "THRIFT"):
        obj["budget"] = b["mode"]
    clean, reasons = allocation.sanitize_allocation(obj, cfg, now, for_write=True)
    if clean is None:
        raise Refused("the engine's filter would refuse this: " + "; ".join(reasons))
    warns = [f"warning: {w}" for w in snap.get("warnings", [])]
    if clean["weights"] != "default":
        for sym, lvl in (allocation._RE_INVALIDATE.match(x).groups() for x in clean["invalidate"]):        # noqa: SLF001
            px = snap.get("prices", {}).get(sym)
            if px and float(lvl) >= px:
                warns.append(f"close_below:{sym}:{lvl} is at or above the last price {px:g}: it would trigger at once")
        d, on = snap.get("default"), snap.get("on") or {}
        if d:
            for sym in ALLOC_SYMS:
                if sym in on and not on[sym] and clean["weights"][sym] > d[sym] + 0.02 and not any(
                        x.split(":")[1] == sym for x in clean["invalidate"]):
                    warns.append(f"{sym} is above its default while its trend is OFF: add --invalidate close_below:{sym}:<price>")
    _write_alloc(root, clean)
    out = [f"allocation.json written: {clean['phase']} for {clean['for_date']} " +
           ("hold-default (no view)" if clean["weights"] == "default" else
            f"G/S/C {_fmt(clean['weights'])} conf {clean['confidence']:g}") +
           (f" (default {_fmt(snap['default'])})" if snap.get("default") else "")]
    if clean["for_date"] == now.date().isoformat():
        if not proposal_is_live(p, cfg):
            out.append("guidance.json not touched: the proposal is logged and scored only (v3 shadow or dormant), so it must not change "
                       "what v2/D5-FI trades; only an explicit `guidance.py set` writes a bias")
        elif act == "hold-default":
            note_args = argparse.Namespace(note="allocation hold-default")
            out.append(cmd_touch(note_args, root, now, p))
        else:
            bias = derived_bias(clean, snap, root, p)
            down = enforce_ceiling(root, now, p, bias)
            note = clean_note(f"allocation {clean['phase']} G/S/C {_fmt(clean['weights'])} conf {clean['confidence']:g}")
            _write_guidance(root, now, bias, note)
            out.append("guidance.json bias (derived for v2 rollback): " + ", ".join(f"{k}={v}" for k, v in bias.items())
                       + "".join("\n" + d for d in down))
    else:
        out.append("guidance.json not touched (the proposal is for a later day)")
    try:
        import calibration
        out.append(calibration.record_proposal(root, now, clean))
    except Exception as e:                      # noqa: BLE001 - the proposal is written; the ledger is a bonus
        out.append(f"ledger not updated ({type(e).__name__})")
    if clean["weights"] != "default":
        out += _preview_lines(snap, clean, now, p)
        try:                                    # advice only: misquoted numbers are a WARNING, never a refusal
            from simgold import factcheck
            warns += factcheck.warning_lines(factcheck.check_proposal(root, now.date().isoformat(), clean))
        except Exception:                       # noqa: BLE001
            pass
    return "\n".join(out + warns)


def _alloc_show(path: Path, now: datetime, cfg: dict) -> str:
    raw = _read_alloc(path)
    if raw is None:
        return "no state/allocation.json: the engine runs its default"
    tdays = lambda x, y: market.trading_days_between(x + timedelta(days=1), y)          # noqa: E731
    adv, notes = allocation.load_proposal(path, now, cfg, tdays)
    L = []
    if adv is None:
        L.append("IGNORED by the engine (the default runs): " + "; ".join(str(n.get("why") or n.get("rule")) for n in notes))
    else:
        L.append(f"USED by the engine as {adv.source} (confidence {adv.confidence:g}; the file says {adv.raw_confidence:g})"
                 + ("" if cfg["ai_trust"] > 0 and not cfg["shadow"] else f" - logged only today (shadow {cfg['shadow']}, ai_trust {cfg['ai_trust']:g})")
                 + "".join(f"; {n['why']}" for n in notes))
    if isinstance(raw, dict):
        w = raw.get("weights")
        L.append(f"{raw.get('phase')} for {raw.get('for_date')} written {raw.get('written')} by {raw.get('by')}: " +
                 ("hold-default" if w == "default" else f"G/S/C {_fmt(w)}" if isinstance(w, dict) and set(w) == set(ALLOC_SYMS) else "weights ?")
                 + f" conf {raw.get('confidence')} horizon {raw.get('horizon_days')}d")
        for s in raw.get("scenarios") or []:
            if isinstance(s, dict):
                L.append(f"  scenario {s.get('name')} p {s.get('p')} " + " ".join(f"{m}={s[m]}" for m in ALLOC_METALS if m in s)
                         + (f" - {s['why']}" if s.get("why") else ""))
        for m, t in (raw.get("rationale") or {}).items():
            L.append(f"  {m}: {t}")
        for k in ("invalidate", "desks", "default_at_write", "prices_at_update", "budget", "note"):
            if raw.get(k):
                L.append(f"  {k}: {raw[k]}")
    return "\n".join(L)


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="write state/guidance.json")
    s.add_argument("--bias", nargs="+", required=True, metavar="KEY=VALUE")
    s.add_argument("--note")
    tc = sub.add_parser("touch", help="keep the bias, refresh the time, append a note (works without a guidance.json)")
    tc.add_argument("--note")
    sub.add_parser("fastlane", help="write state/fastlane.json (see the module help for options)", add_help=False)
    e = sub.add_parser("events", help="edit state/events.json")
    e.add_argument("action", choices=["add", "remove", "list"])
    e.add_argument("--date")
    e.add_argument("--type")
    e.add_argument("--time")
    m = sub.add_parser(
        "market-today", help="write state/market_today.json (the Market section of the daily report)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Write state/market_today.json. Repeat --item once per market line.\n\n"
                    "  --item NAME:MOVE_PCT[:NOTE]   NAME is the label shown in the report (for example 'Gold (global)'),\n"
                    "                                MOVE_PCT is the day's move in percent as a number (+0.8, -0.4; not 0.008),\n"
                    "                                NOTE is one short plain-English sentence (max 200 characters, optional).\n"
                    "  --date YYYY-MM-DD             the trading date (default: today IST).\n\n"
                    "Example:\n  python tools/guidance.py market-today --item \"Gold (global):+0.8:Weaker dollar\" "
                    "--item \"USD/INR:0.1:Rupee slightly weaker\"\n"
                    "Include the dollar index and USD/INR when known; they are shown as context, not good or bad news.")
    m.add_argument("--item", action="append", default=[], metavar="NAME:MOVE_PCT[:NOTE]",
                   help="one market line; repeat for each market (at least one)")
    m.add_argument("--date", help="trading date YYYY-MM-DD (default today IST)")
    t = sub.add_parser("tomorrow", help="write state/tomorrow.txt")
    t.add_argument("--text", required=True)
    al = sub.add_parser("allocation", help="strategy v3 (dormant): state/allocation.json set|explain|show|hold-default", allow_abbrev=False)
    al.add_argument("action", choices=["set", "explain", "show", "hold-default"])
    al.add_argument("--weights", nargs="+", metavar="SYMBOL=SHARE", help="GOLDBEES=0.70 SILVERBEES=0.20 HINDCOPPER=0.10 (sum 1.00)")
    al.add_argument("--confidence", type=float)
    al.add_argument("--scenario", action="append", metavar="NAME:P:GOLD=up,SILVER=flat,COPPER=down:WHY", help="2 to 4 times")
    al.add_argument("--rationale", nargs="+", metavar="METAL=TEXT", help="GOLD=\"text with a number\" SILVER=... COPPER=...")
    al.add_argument("--invalidate", action="append", nargs="+", metavar="close_below:SYMBOL:PRICE")
    al.add_argument("--horizon-days", type=float)
    al.add_argument("--for-date", help="default: today (before 15:30 on a trading day), else the next trading day")
    al.add_argument("--phase", choices=["provisional", "final"], default="final")
    al.add_argument("--desks", nargs="+", metavar="DESK=STATE", help="default: read from analysis/D/")
    al.add_argument("--by", help="who wrote it (default premarket for provisional, allocation for final)")
    al.add_argument("--note")
    al.add_argument("--file", help="explain only: a proposal file to preview instead of state/allocation.json")
    return ap


def _guard_allocation_flags(argv: list[str]) -> str | None:
    """`allocation` has no cash, size, quantity or order option: say so plainly (exit 2) before argparse sees the flag."""
    if "allocation" in argv:
        for t in argv[argv.index("allocation"):]:
            if t.startswith("--") and t.split("=")[0].lower() in REFUSED_FLAGS:
                return (f"allocation has no {t.split('=')[0]} option: you propose shares of GOLDBEES, SILVERBEES, HINDCOPPER that "
                        f"sum to 1.00; the engine decides sizes, orders and cash")
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    utf8_stdout()
    refused = _guard_allocation_flags(argv)
    if refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return 2
    # fastlane takes repeated --symbol groups, so it parses its own arguments
    fl = [i for i, t in enumerate(argv) if t == "fastlane" and (i == 0 or argv[i - 1] not in ("--root", "--now"))]
    if fl and not any(t == "allocation" for t in argv[:fl[0]]):
        i = fl[0]
        head, rest = argv[:i], argv[i + 1:]
        if "-h" in rest or "--help" in rest:
            print(__doc__)
            return 0
        pre = build_parser().parse_args(head + ["fastlane"])
        runner = lambda root, now, p: cmd_fastlane(rest, root, now, p)   # noqa: E731
    else:
        pre = build_parser().parse_args(argv)
        fn = {"set": cmd_set, "touch": cmd_touch, "events": cmd_events, "market-today": cmd_market_today, "tomorrow": cmd_tomorrow,
              "allocation": cmd_allocation}[pre.cmd]
        runner = lambda root, now, p: fn(pre, root, now, p)              # noqa: E731
    try:
        print(runner(Path(pre.root), parse_now(pre.now), config.load_params()))
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
