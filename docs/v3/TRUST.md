# AI trust recommendation (`tools/trust.py`, `simgold/trust.py`)

Paper trading only. The tool RECOMMENDS `allocation.ai_trust` in {0, 0.25, 0.5, 0.75, 1.0} from the calibration ledger; it never writes
config and never applies trust. The owner commits `allocation.ai_trust`.

| level | scored days | P&L mean CI lower bound | Brier skill vs base rates | validity | misquote rate |
|---|---|---|---|---|---|
| 0.25 | >= 20 | > 0 | > 0 | >= 95% | <= 5% |
| 0.50 | >= 40 | > 0 | > 0 **and CI lower bound > 0** | >= 95% | <= 5% |
| 0.75 | >= 60 | > 0 | > 0 and CI lower bound > 0 | >= 95% | <= 5% |
| 1.00 | >= 120 | > 0 | > 0 and CI lower bound > 0 | >= 95% | <= 5% |

Rules the code fixes: the Brier-skill CI lower bound must be above 0 from 0.5 upward (0.25 needs only a positive point estimate);
hold-default days (AI weights equal the default) are not scored days; bootstrap blocks are `max(5, n // 5)` dates (they cover the 5-day
window overlap) and intervals below 25 dates are advisory; the synthesis scores are kept for 140 dates (desk scores 40 each); unknown
evidence (no ALLOC records, no fact-check field) does not count as passing; a month with the tilt losing more than 2 pp, or 5 invalid
proposals in 31 days, recommends 0; a ledger that cannot be read reports "ledger corrupt", a missing one "no calibration ledger".

Known leniencies (by design, so a morning desk is not punished for quoting yesterday): without an explicit date a stated price or day move may
match EITHER of the last two closes. Below 25 dates the bootstrap interval is advisory but it still gates the 0.25 level (which needs 20 scored
days): read a 0.25 recommendation at 20-24 days as a first look. The synthesis scores, the pairs and the `oc` outcomes are all kept for 130
dates, so the 120-day level is reachable; the ledger size cap is 48 KB (a full-retention 5-desk ledger fits with no row dropped).
Fact-check skips conditional text (if/could/needs/unless/until/breakout/bull/bear, relation and level claims in desk `risks` and scenario lines).
