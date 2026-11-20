# Replay integrity follow-up

- Frozen orders keep the requested execution session. Daily frozen orders now use the same trimmed event delivery as the order tape, but are admitted directly on their specified session. No previous-close decision is synthesized. Runtime errors propagate. A date outside the supplied sessions is rejected. Stable client IDs survive both full and trimmed replay.
- Opening capacity uses explicit opening volume or the last eligible closed bar, including minute replay. A verified zero-trade bar without opening observations has no executable depth. The synthetic intrabar path remains a scenario model, not a historical queue reconstruction.
- Prepared anomalies are immutable; all replay paths inherit the prepared corporate-action policy and reject conflicts.
- Uncompleted sell quantities are an exposure diagnostic: subsequent sells offset earlier unmet reductions for the same symbol. A failed partial sale alone does not identify liquidation residue. Portfolio target drift belongs in the target-aware research diagnostics.
- The fill comparer checks aligned records and identifies observable field differences; causal attribution requires order and market evidence.

The session-close fallback checks the last observed bar per symbol instead of searching the entire history. This relies on validated, non-overlapping bars and chronological native callbacks. On the frozen 100-stock, 727-session workload (72,587 bars, 1,800 fills), the full path measured 10.30 s median in three processes (9.97–10.70 s), versus 18.73 s in the earlier V3.1 batch. Fills, daily accounts and rejections matched in all three runs.
