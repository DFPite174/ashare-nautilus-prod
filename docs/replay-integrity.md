# Replay integrity follow-up

- Frozen orders keep the requested execution session. The order-tape shortcut requires a preceding open session with a positive bar for that symbol; otherwise the direct frozen replay is used. Runtime errors propagate. A date outside the supplied sessions is rejected.
- Opening capacity uses explicit opening volume or the last eligible closed bar, including minute replay. A verified zero-trade bar without opening observations has no executable depth. The synthetic intrabar path remains a scenario model, not a historical queue reconstruction.
- Prepared anomalies are immutable; all replay paths inherit the prepared corporate-action policy and reject conflicts.
- Uncompleted sell quantities are an exposure diagnostic: subsequent sells offset earlier unmet reductions for the same symbol. A failed partial sale alone does not identify liquidation residue. Portfolio target drift belongs in the target-aware research diagnostics.
- The fill comparer checks aligned records and identifies observable field differences; causal attribution requires order and market evidence.
