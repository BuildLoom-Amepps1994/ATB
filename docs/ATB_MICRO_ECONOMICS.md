# Micro-trading mode: economics

Everything in this document is **algebra or a modeled estimate**. None of it is
an actual MEXC account fee, a recommended target, or a predicted opportunity.
The account's own fee rate needs a signed endpoint that this work never called;
the parser for its response is tested on supplied responses only and is not
used by any engine path (`docs/ATB_MICRO_ARCHITECTURE.md`).

All numbers below come from `micro_economics.py` (the same code the engine
uses). `tests/test_micro_economics.py` pins the headline break-even and target ratios and checks how the other tables behave (budget independence, the minimum fee, rounding and depth); their individual figures are not pinned.

## Units and names

| Name | Meaning | Unit |
|---|---|---|
| B | All-in entry budget: the most quote the entry may debit, fees and fee reserve included | USDT |
| A | Best executable entry **ask** (or the ask-walk VWAP for the order) | USDT per base unit |
| X | Future executable exit **bid** (or the bid-walk VWAP for the sellable quantity) | USDT per base unit |
| sb, ss | Buy / sell slippage, as fractions (0.002 = 0.2% = 20 bps) | fraction |
| fb, fs | Buy / sell taker fee, as fractions | fraction |
| g | Net profit target for one cycle | USDT |
| bps | 1 bps = 0.01% = 0.0001 as a fraction | |

Rates are stored as fractions. The dashboard shows each required move in
percent and bps (`move_units`), and every amount with its sign and enough
places that a non-zero amount never prints as zero.

## Three budget concepts, kept apart

| Concept | Where it comes from | Can it change the order? |
|---|---|---|
| **Venue minimum feasible debit** | Smallest all-in debit that passes every venue rule (cost minimum by order type, amount step, exit minimum, depth) for this pair now. Found by searching the same sizing plan. | No. Display only. A pair whose minimum is above the budget is ineligible with `BUDGET_BELOW_VENUE_MINIMUM`; it is never upsized. |
| **Strategy target budget** | `min(configured micro budget, 25)`, then reduced by available paper cash and the remaining allocation. | Yes, but only downward. |
| **Absolute maximum all-in debit** | `MICRO_MAX_ALL_IN_USDT = 25`, a code constant, checked again against a private literal. No setting, environment variable or research run can raise it. | It bounds everything. The risk gate and `account_entry` re-check it; the paper venue does not (it enforces only the venue's own rules and the paper balance). |

The legacy 12 USDT floors (`risk.MIN_ORDER_USDT`, the settings bound, the
manual ticket and the legacy entry pass) still apply to the legacy strategies.
The micro profile does not use them: its floor is the venue's own published
minimum for the order type, as read from the catalogue.

## The simple quote-fee formula

For a quote-billed fee with proportional costs only:

```
quantity          = B / [A * (1 + sb) * (1 + fb)]
estimated_exit    = quantity * X * (1 - ss) * (1 - fs)
estimated_net_pnl = estimated_exit - B
break_even X/A    = [(1 + sb)(1 + fb)] / [(1 - ss)(1 - fs)]
target X/A        = break_even X/A * (1 + g / B)
```

With ATB's current modeled defaults (fb = fs = 0.001, sb = ss = 0.002):

| Case | Required exit-bid improvement over entry ask |
|---|---|
| Break even, any budget | 0.6018042090% (60.18 bps) |
| 25 USDT aiming at 0.05 USDT net | 0.8030078174% (80.30 bps) |
| 10 USDT aiming at 0.02 USDT net | 0.8030078174% (same 0.2% net return) |
| 15 USDT aiming at 0.03 USDT net | 0.8030078174% |
| 25 USDT aiming at 0.02 USDT net | 0.6822856524% |
| 25 USDT aiming at 0.10 USDT net | 1.0042114259% |

A larger budget does **not** reduce the percentage hurdle when costs are
proportional. It only scales the absolute loss when the price does not move:

| Budget | Net result at an unchanged price (fb = fs = 0.1%, sb = ss = 0.2%) |
|---|---|
| 10 USDT | -0.0598 USDT |
| 15 USDT | -0.0897 USDT |
| 25 USDT | -0.1496 USDT |
| 50 USDT (algebra only, above the cap) | -0.2991 USDT |

The formula does not cover base-asset fees, third-asset fees, partial fills,
quantity rounding or minimum fee amounts. The engine never uses it to size or
to exit; it is for this page and the sensitivity table.

## Cost sensitivity (proportional costs, 25 USDT, target 20 bps of budget)

Both legs use the same fee and slippage. Break-even and target are the
required exit-bid improvement over the entry ask.

| Fee per leg | Slippage per leg | Break-even | Target (0.05 USDT) | Net at unchanged price |
|---|---|---|---|---|
| 0% | 0% | 0.0000% | 0.2000% | 0.0000 |
| 0% | 0.05% | 0.1001% | 0.3003% | -0.0250 |
| 0% | 0.2% | 0.4008% | 0.6016% | -0.0998 |
| 0.05% | 0% | 0.1001% | 0.3003% | -0.0250 |
| 0.05% | 0.05% | 0.2002% | 0.4006% | -0.0500 |
| 0.05% | 0.2% | 0.5013% | 0.7023% | -0.1247 |
| 0.1% | 0% | 0.2002% | 0.4006% | -0.0500 |
| 0.1% | 0.05% | 0.3005% | 0.5011% | -0.0749 |
| 0.1% | 0.2% | 0.6018% | 0.8030% | -0.1496 |
| 0.2% | 0% | 0.4008% | 0.6016% | -0.0998 |
| 0.2% | 0.05% | 0.5013% | 0.7023% | -0.1247 |
| 0.2% | 0.2% | 0.8032% | 1.0048% | -0.1992 |

The same table for 10, 15 and 50 USDT has identical percentages; only the last
column scales. `research/micro_research.py` writes a related grid (fees 0.05, 0.1 and 0.2 %, slippage 0, 0.03 and 0.2 %) to its result
file, and `micro_economics.sensitivity()` reproduces it.

## What makes a small budget different: fixed costs, rounding and depth

Proportional costs leave the hurdle independent of the budget. Three things
do not, and the sizing code models each one separately.

**1. Minimum fee amounts.** If each leg pays `max(rate * notional, minimum)`,
a small budget pays a larger share. With a 0.01 USDT minimum fee (illustrative)
at fb = fs = 0.1% and sb = ss = 0.2%:

| Budget | Break-even | Target (20 bps of budget) |
|---|---|---|
| 10 USDT | 0.6019% | 0.8031% |
| 15 USDT | 0.6018% | 0.8030% |
| 25 USDT | 0.6018% | 0.8030% |

At 10 USDT the minimum binds on one leg; above that it does not.

**2. Quantity rounding.** The venue's amount step decides how much of the
purchase can be sold back. What cannot be sold is residual inventory: it is
kept with its share of the cost basis, never deleted, and valued at zero when
the break-even bid is computed. This is the conservative choice and it can
dominate. A sizing run on a synthetic ETH/USDT book at a 2500 USDT mid, taker
fee 0.1%, no latency margin, a deep book:

| Amount step | Budget | Sellable | Residual (base) | Break-even bid | Target bid | Required move |
|---|---|---|---|---|---|---|
| 0.0001 ETH | 10 USDT | 0.0039 | 0.0000960 | 2566.67 | 2571.81 | 287.20 bps |
| 0.0001 ETH | 25 USDT | 0.0099 | 0.0000880 | 2527.79 | 2532.84 | 131.32 bps |
| 0.000001 ETH | 10 USDT | 0.003995 | 0.00000098 | 2505.64 | 2510.65 | 42.56 bps |
| 0.000001 ETH | 25 USDT | 0.009987 | 0.00000096 | 2505.76 | 2510.78 | 43.08 bps |

The required move is to the **target** bid, measured from the best ask. With
a coarse step, one step of ETH is worth about 0.25 USDT, so a 10 USDT order
strands about 2.4% of its quantity and a 25 USDT order about 0.9%. This is a
real reason a pair can be workable at 25 USDT but not at 10. The strategy's
feasibility gate sees the required move and abstains when it is out of reach.
The real MEXC steps for each pair come from the live catalogue at run time.

**3. Depth impact.** Market orders walk the book level by level, so a larger
order pays a worse average price on a thin book. On a thin synthetic book
(0.0006 ETH per level, 0.50 USDT between levels, amount step 0.000001):

| Budget | Entry VWAP | Break-even bid | Required move |
|---|---|---|---|
| 10 USDT | 2501.43 | 2506.89 | 47.60 bps |
| 25 USDT | 2503.92 | 2509.53 | 58.16 bps |

Here the 25 USDT order needs a larger move than the 10 USDT order. A deep book
(1 ETH per level) shows almost no difference between the two.

**Fee reserve and cost-step rounding.** The order's quote amount is rounded
down to the venue's cost step and the quote fee reserve is rounded up. The
reserve that is not spent stays in the basis until the fill reports the actual
fee, so the estimate before the fill is slightly pessimistic (about 0.02% at
25 USDT in the tables above). After the fill, the basis is the observed debit.

## 25 USDT on the research fixtures

The production sizing (`micro_economics.size_entry`) on each research fixture
at its middle bar, 25 USDT budget, the protocol's 0.1% taker fee, the baseline
latency margin and a deep book. Two sets of fixture rules: the research
fixture's fine amount step (0.000001), and the coarser 0.0001 step that
`research/micro_fixtures.mexc_market` uses by default. Both sets have a 1 USDT
minimum cost and a 0.0001 minimum amount. **These are fixture rules, not
MEXC's.** The real minimums, steps and fees come from the live catalogue at
run time, and none were read here.

| Pair | Mid | Amount step | Eligible | Gross quote | Fee reserve | Max debit | Sellable base | Required move | Smallest workable budget |
|---|---|---|---|---|---|---|---|---|---|
| ETH/USDT | 2499.00 | 0.000001 | yes | 24.97 | 0.03 | 25.00 | 0.009978 | 58.2 bps | 1.03 |
| ETH/USDT | 2499.00 | 0.0001 | yes | 24.97 | 0.03 | 25.00 | 0.0099 | 137.4 bps | 1.27 |
| ADA/USDT | 0.5002 | 0.000001 | yes | 24.975 | 0.025 | 25.000 | 49.855171 | 58.0 bps | 1.02 |
| ADA/USDT | 0.5002 | 0.0001 | yes | 24.975 | 0.025 | 25.000 | 49.8551 | 58.0 bps | 1.02 |
| BTC/USDT | 58015.33 | 0.000001 | yes | 24.97 | 0.03 | 25.00 | 0.000429 | 76.9 bps | 5.82 |
| BTC/USDT | 58015.33 | 0.0001 | yes | 24.97 | 0.03 | 25.00 | 0.0004 | 807.5 bps | 5.82 |
| SOL/USDT | 133.75 | 0.000001 | yes | 24.97 | 0.03 | 25.00 | 0.186435 | 58.3 bps | 1.03 |
| SOL/USDT | 133.75 | 0.0001 | yes | 24.97 | 0.03 | 25.00 | 0.1864 | 60.6 bps | 1.03 |
| XRP/USDT | 0.6000 | 0.000001 | yes | 24.975 | 0.025 | 25.000 | 41.563976 | 56.7 bps | 1.02 |
| XRP/USDT | 0.6000 | 0.0001 | yes | 24.975 | 0.025 | 25.000 | 41.5639 | 56.7 bps | 1.02 |

* **The cap holds everywhere.** Every row debits at most 25.00 USDT including
  the fee reserve; nothing is upsized.
* **The exit minimums are met.** Every sellable quantity is above the 0.0001
  minimum amount, and its value is far above the 1 USDT minimum cost.
* **A coarse step can make a pair unworkable without making it ineligible.**
  At the 0.0001 step one BTC step is worth about 5.80 USDT, so 0.0000298 BTC
  of the purchase cannot be sold back and is valued at zero. The required
  move rises to about 808 bps, which the strategy's feasibility gate refuses.
  ETH at the same step needs about 137 bps.
* The smallest workable budget is the search the sizing runs for display. It
  never changes the 25 USDT cap.

## Book-based estimates replace flat slippage

The 0.2% "slippage" in the formula is a flat stand-in. When a validated order
book is available, the engine does not use it:

- Entries walk the **asks** for the quote amount. Exits walk the **bids** for
  the sellable quantity. Neither uses last trade, mid, a candle high or an
  unlimited top of book.
- A side-specific VWAP already contains the spread, so no spread is charged
  on top of it.
- The latency and adverse-selection margin (default 3 bps per leg, setting `micro_latency_bps`) is applied
  once per leg, to every level of the walk.
- The flat fallback slippage is used only when depth is unknown (a ticker top
  of book, even one that carries quantities: those are kept for display only). Entries are refused in that case
  (`DEPTH_UNKNOWN`); exits may still be estimated, and the estimate is labeled
  `flat_fallback`.
- When the visible bids cannot absorb the sellable quantity, the exit estimate
  is still a number, priced conservatively: the visible bids for what they
  absorb and the flat fallback from the worst visible bid for the rest,
  labeled `flat_fallback` with `depth_exhausted` set. The strategy cannot call
  that a profit; a safety exit can act on it.
- Confirmed results use the actual fills and the fees the venue reported.
  Modeled slippage is never subtracted again from an observed fill.

## Modeled, estimated, observed, reconciled, unknown

| Label | Meaning | Example |
|---|---|---|
| modeled | A rate from configuration or the public schedule | Taker fee 0.1% used to size an entry |
| estimated | A computation from a book snapshot and modeled costs | Net proceeds if the holding were sold now |
| simulated | Reported by the paper venue for a paper fill | Fee and fill of a paper market order |
| observed | Reported by a real venue for a real fill | Never produced by this work |
| reconciled | Settled after an unknown outcome was resolved read-only | A paper order whose response was lost |
| unknown | Not reported and not inferable | A fee charged in a third asset with no price |

A missing fee stays unknown and blocks the cycle from settling. It never
becomes "confirmed profit" by default. The dashboard shows realized results
from confirmed fills separately from estimates on open holdings, and residual
inventory separately from both.

## Realized net P/L for a closed quantity

For the quantity actually sold:

```
realized_net = net_exit_proceeds - allocated_basis - fees not already in either
allocated_basis = basis * sold / owned          (exactly; the rest stays as residual basis)
```

The entry fee is already inside the basis when it was paid in quote, so it is
not charged again. A base-asset fee reduces the owned quantity instead. A
partial close keeps the unsold quantity and its basis; `tests/test_micro_economics.py`
checks that the sold and kept parts add back to the original exactly.

## Stops and the loss cap

The moment a position is bought, selling it into the bids already loses the
round trip: both fees, both latency margins and the spread. With the default
modeled costs (0.10% fee per side, 3 bps latency per leg, a 2 bps spread) that
is about 40 bps of the budget. The exit compares the estimated net of selling
now with the stop, so a stop must be measured from that starting point:

```
stop_net_bps = entry_cost_bps + max(2 x ATR_1m_bps, 25)
entry_cost_bps = max_debit x (1 - best_bid / break_even_bid), in bps of the budget cap
```

A cycle is entered only if `stop_net_bps <= micro_max_loss_bps`. The first
design used `clamp(2 ATR, 25, max_loss)` without the entry cost; a review
showed that such a stop sits inside the cost and fires on the first exit check
with the book unchanged. The break-even bid (and the target bid behind the
required move) walks the bids the position would be sold into, as the exit
estimate does, so a small best bid with the depth far below it shows its real
cost and is not entered. The stop is an amount for the whole cycle: after a
partial sale, the loss already realized counts against it.

The economic gate also needs a market that moves enough: the required move
(costs plus target, about 58 bps at the defaults; the spread and the exit's
uncertainty margin come on top for a take-profit) must fit within
`0.6 x ATR x sqrt(hold minutes)`, which at a 30-minute hold means an ATR of at
least about 18 bps. Together the two rules leave this window of 1-minute ATR:

| Loss cap | Largest ATR the stop allows (40 bps entry cost) | Smallest ATR the economic gate allows | Window |
|---|---|---|---|
| 80 bps (0.20 USDT) | 20 bps | about 18 bps | about 18 to 20 bps: a sliver, so the mode would almost never enter |
| 120 bps (0.30 USDT), the default | 40 bps | about 18 bps | 18 to 40 bps |
| 160 bps (0.40 USDT) | 60 bps | about 18 bps | 18 to 60 bps (1-minute volatility above 60 bps is refused anyway) |

The default was therefore raised from 80 to 120 bps, a loss of at most about
0.30 USDT of a 25 USDT cycle on an orderly exit. A gap or an outage can lose
more: a software stop cannot guarantee its price. The daily micro loss stop
(1 USDT) and the consecutive-loss stop (4) still bound the day. A lower cap is
allowed and simply makes the mode wait more (below the 25 bps floor no stop
fits, and each decision says so); with a cheaper fee schedule the
entry cost falls and the window widens. Nothing here shows that entering in
that window is profitable.

## Choosing a target

Small targets such as 0.02, 0.05 or 0.10 USDT are research examples, not
defaults or promises. The micro profile's target is set in bps of the budget
(default 20 bps, so 0.05 USDT at 25 USDT). The strategy only enters when its
modeled edge, after costs and an uncertainty margin, clears the required move;
a target above break-even does not by itself establish positive expected value.
Whether any setup has a positive expected value after costs is a research
question. See `docs/ATB_MICRO_RESEARCH.md`: on the data available to this work,
**no edge was demonstrated**.
