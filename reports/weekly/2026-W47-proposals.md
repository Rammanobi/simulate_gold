# 2026-W47 proposals (for the owner; nothing is changed by the routine)

## 1. Lower the min_invested floor while every trend is OFF (safer)
- Parameter: `strategy.min_invested` in `config/params.yaml` (D5 "stay invested", held as GOLDBEES even with trend OFF), 0.50 → 0.25.
- Safer: yes (less exposure when the trend signal says OFF).
- Evidence: trend has been OFF on all three metals since late October. The floor holding (437 GOLDBEES bought 2 Nov at ₹113.00) is now about -₹5,042 unrealised and caused almost all of the -₹6,062 month (W46 -1.85%, W47 -4.60%). Defensive stops cannot sell it (refused 13 Nov ₹115/₹111 and 20 Nov ₹101). Counter-evidence: on 4 Nov a +3% gold day lifted the floor holding. Three weeks in one falling market is still weak evidence.
- Test: replay Oct 2025 – Oct 2026 with `run_backtest.py` at 0.50, 0.25 and 0. Pass if 0.25 cuts max drawdown by ≥ 25% and gives up ≤ 1 pp a year against 0.50.
- Main risk: if gold turns up before trend turns ON, the book catches less of the rebound and falls further behind GOLDBEES buy-and-hold.
