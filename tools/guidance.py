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

Everything is checked before anything is written. Exit 0 = written, exit 2 = refused (nothing written).
`fastlane` runs the very same filter the engine runs (guard.sanitize_fastlane): no favour, no buys, no loosening
of a stop, sell/tighten only on a position that exists. Files: state/guidance.json, state/fastlane.json,
state/events.json, state/market_today.json, state/tomorrow.txt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import brief, config, guard, memsafe, memstore, regime
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
            memsafe.check_text(n, "bias_note", one_line=False)
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


def _date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise Refused(f"--date must look like 2026-10-14, got {s!r}")


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
    write_atomic(root / "state" / "tomorrow.txt", text + "\n")
    return "tomorrow.txt written"


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
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    utf8_stdout()
    # fastlane takes repeated --symbol groups, so it parses its own arguments
    if "fastlane" in argv:
        i = argv.index("fastlane")
        head, rest = argv[:i], argv[i + 1:]
        if "-h" in rest or "--help" in rest:
            print(__doc__)
            return 0
        pre = build_parser().parse_args(head + ["fastlane"])
        runner = lambda root, now, p: cmd_fastlane(rest, root, now, p)   # noqa: E731
    else:
        pre = build_parser().parse_args(argv)
        fn = {"set": cmd_set, "touch": cmd_touch, "events": cmd_events, "market-today": cmd_market_today, "tomorrow": cmd_tomorrow}[pre.cmd]
        runner = lambda root, now, p: fn(pre, root, now, p)              # noqa: E731
    try:
        print(runner(Path(pre.root), parse_now(pre.now), config.load_params()))
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
