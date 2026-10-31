# Engine scenario evals

Fast, offline pytest checks (marker `scenario`) that drive the real `simgold.engine.Engine` through realistic and nasty
situations with the **shipped** rules (`config/params.yaml`, read fresh, not the v1 copy `tests/conftest.py` caches).
They run in the normal suite. `pytest -q -m scenario -v` lists them; `pytest -m "not scenario"` skips them.

After **every** simulated tick the harness (`evalkit.Sim`) re-checks, independently of the engine:
cash and LIQUIDBEES are never negative; units are whole numbers; `cash + sum(qty x price x (1 - cost/2))` equals
`Engine.value()`; the live P&L payload (`livepnl.build`) is valid schema `v:1`, JSON-safe and equals the engine value;
the money summary parts add up to overall profit to the paisa and its "interest" plug never drifts.

## test_replay_real_days.py - real days
Replays the real minute prices of 21, 22, 23, 26, 27, 28, 29 Oct 2026 (`fixtures/ticks-*.csv`, real daily bars in
`fixtures/daily_bars.json`, the real portfolio of the morning of 21 Oct) with today's rules, and asserts:
the 21 Oct floor buy is about half the account in GOLDBEES at 15:10; the 22 Oct fast-lane "tighten stop 116.40" does not
sell the floor on 23 Oct; market-moving headlines never pause a floor buy; a burst of watchdog noise never turns safe mode
on; the account stays about 50% invested every minute on 26-29 Oct; no brake or account stop fires; all the per-tick
invariants above hold. By default quiet midday minutes are sampled (about 45 s); `EVAL_FULL_REPLAY=1` replays all 2,470 minutes.

## test_edge_scenarios.py - synthetic edge cases
Gap-down open below a silver stop (one sale); price flickering around the stop (only 2 consecutive fresh prices sell); stale
prices never count; bad bars (-20%, +20%, -30%, -60%, zero, NaN) never sell the gold floor; a 30 minute feed outage and a missing
symbol; weekend, holiday and early close; restart in the middle of a stop or a pending retry; the same minute run twice, the
engine lock and the run guard; the -15% brake, the -25% brake and the 75,000 account stop override floor protection while AI advice
cannot; a buy never exceeds cash including costs; a missed buy is retried once next morning and expires after 09:50; the
"allocation stuck" alert after 2 checks and its resolution; news never pauses buys; monitoring noise never turns safe mode on but a
genuine HIGH does; live-push failures (timeout, git error, no git) never raise or block a tick; the workflow still self-dispatches.
No eval is xfail any more: the single-tick account-stop bug was fixed on 2026-10-31 (`strategy.stops.account_confirm_ticks`).

## test_properties.py - random paths x random AI advice
200 derandomized hypothesis examples (a seeded loop of 200 if hypothesis is not installed; `EVAL_EXAMPLES=n` changes the count):
random trends, holdings and price paths, with random guidance and fast-lane files (bias, tighten_stop, sell_fraction, block,
halt, and forbidden keys like `buy` or `qty`). Each scenario is run with and without the AI files. Invariants: no exception, the
per-tick invariants, a BUY only ever comes from the daily trend check, no symbol ends the check above its rule target, a
fresh `block` bias means no new buy, an AI stop is never loosened, an AI sale never takes gold below its floor units.

## Funds table / other open work
Evals about features that may not be merged yet (`money_summary()["funds"]`) skip cleanly when the feature is absent.
`make_fixtures.py` rebuilds the fixtures (needs network once; the suite never does).
