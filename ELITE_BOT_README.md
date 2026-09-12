# Elite Bot Strategy Documentation

## Game Overview: Divided Oracle

**QuantStorm 2026** — The Elite Bot competes in *Divided Oracle*, a sophisticated two-player, zero-sum trading game of incomplete information.

### The Core Objective
Two bots trade contracts on a hidden score `S`, which is the sum of **40 coins** (each ±1 with equal probability). Each player receives 20 coins but only gradually learns their own hand, and never directly sees the opponent's coins except through special auction powers. The objective is to **maximize PnL** by:

1. **Pricing accurately**: Estimating `S` from incomplete information and trading only when you have an edge.
2. **Auction strategy**: Bidding optimally for special powers within a tight **24 Tactical Energy (TE)** budget per deal.

### Deal Structure
Each deal consists of **5 rounds**. Each round follows this sequence:
```
Reveal 4 coins → Blind auction for a power → Negotiation (up to 6 turns) → One contract executes
```

Five contracts settle simultaneously at the end of the deal after all 40 coins are revealed.

---

## Elite Bot Architecture

The **BayesianOracle** (Elite Bot) is a sophisticated statistical trading agent that employs seven core strategic principles:

### 1. **Bayesian S-Estimation** 

The bot estimates the hidden score `S` using:
```
E[S | information] = k_mine + E[k_opp_revealed] + 0
```

**Sources of opponent information** (in order of reliability):
- **FORESIGHT power** (direct observation): In rounds 1–4, the bot may win FORESIGHT and see up to 16 of the opponent's revealed coins with zero noise.
- **Quote midpoint**: The Maker's opening quote reveals information about their hand. The bot anchors on the midpoint when observing as a Taker.
- **Multi-round accumulation**: The best anchor from all rounds when the bot was Taker.
- **Prior**: Without information, opponent coins are assumed to sum to 0 (mean-zero i.i.d. assumption).

The bot caches these anchors across the 5 rounds of a deal to maintain a running estimate.

### 2. **Dynamic TE Budget Allocation** (Multi-Round Knapsack Approximation)

With only **~24 TE** to spend and typically 5 power slots available, the bot allocates dynamically:

```
Budget: 24 TE per deal → ~3 powers can be afforded at equilibrium bid shade
```

For each power offered:
- Compute the offered power's value `v` and the average future power value `f`.
- **Ratio-based shading**:
  - If `v / f > 1.15`: Use **premium shade (0.75)** — bid aggressively.
  - If `v / f < 0.80`: Use **discount shade (0.42)** — save TE for better slots.
  - Otherwise: Use **base shade (0.62)** — balanced bid (Bayesian Nash Equilibrium solution).

The shade factor converts tick value to TE bids:
```
fair_te = value_in_ticks / TE_SALVAGE_RATE
bid_te = int(fair_te × shade_factor)
```

### 3. **TRICK_ROOM / STEALTH_ROCK Forcing Exploit**

These "fill-shift" powers (TRICK_ROOM and STEALTH_ROCK) shift the forced midpoint fill price by 3 and 2 ticks respectively in the holder's favor.

**Key insight**: Forcing a fill on turn 6 costs a 2-tick fee, but if you hold a net fill-shift power, the exploit becomes profitable:
```
Net gain from forcing (as short) = shift_magnitude - forcing_fee
                                 = 3 - 2 = +1 tick (for TRICK_ROOM alone)
```

**Strategy**:
- **Never accept SELL** when holding an uncontested fill-shift power with `net_shift > 0`.
- **Force (counter) on the last turn** unless accepting BUY is clearly better (edge > force_pnl + threshold).
- The bot computes the net shift balancing both its own shift powers and the opponent's, then exploits asymmetries.

### 4. **Parity-Aware Width Selection** 

The residual score (unseen coins) has inherent **discrete parity structure**: the sum of coins must match the parity of the number of unseen coins. This creates a lattice effect that favors certain widths.

**Algorithm**:
The bot checks widths `[floor, floor+1, floor+2]` and selects the one maximizing expected Maker PnL:
```
net_delta(w) = 3.0 × (straddle_prob(w) - straddle_prob(floor))
             - 0.22 × (w - floor)
```

This extracts "free money" from the lattice structure by choosing widths that align with parity.

### 5. **SUBSTITUTE-Adjusted Accept Thresholds**

SUBSTITUTE caps the bot's loss on a single contract at 2 ticks (while keeping profit uncapped). This creates an asymmetric payoff.

**Effect**: When holding SUBSTITUTE, the bot lowers its acceptance thresholds by **1.0 tick**, making it more willing to trade on thinner edges since downside is protected.

### 6. **TRANSFORM — Bidirectional Game Theory**

TRANSFORM lets the holder swap their entire 20-coin hand with the opponent's (including revealed coins). The bot uses game-theoretic logic to value this power:

**Flat hand** (`|k_mine| ≤ 1`):
- My hand is near-zero information → buy TRANSFORM to **fire the swap** and hopefully get a better hand.
- Bid the full base value for TRANSFORM.

**Decisive hand** (`|k_mine| ≥ 3`):
- I have useful information → buy TRANSFORM to **VETO** (deny opponent the swap).
- Opponent appears flat (from quote anchor) → they would fire if they won → spend TE to deny them.
- Denial value = `base_value × 0.45` (fraction of swap value to pay for denial).
- If no clear read on opponent flatness → bid 0 on TRANSFORM (too uncertain).

### 7. **Posterior Variance-Aware Response**

Even when no clear edge exists, the bot adjusts its negotiation strategy based on posterior variance:
```
variance = uncertainty_from_unobserved_opponent_coins + uncertainty_from_unseen_coins
```

Higher variance → widen counters (more room to find agreement).
Lower variance → narrow counters (tight, edge-focused trading).

---

## Method Reference

### `reset(seat, config, seed)`
Initializes the bot for a new deal. Sets up:
- **self._anchors**: Caches the Maker's quote midpoint per round (read as estimate of opponent's k).
- **self._cached_v**: Caches S estimates to avoid redundant computation.
- Random seed for reproducibility.

### `bid(obs, offered) → dict[str, int]`
Blindly bids TE on the current round's power using dynamic shading:
1. Compute power value (state-dependent; TRANSFORM gets special handling).
2. Convert to TE: `fair_te = value / TE_SALVAGE`.
3. Apply shade factor based on ratio of current value to future average value.
4. Return dict of power names to TE amounts.

### `quote(obs) → tuple[int, int]`
Maker's opening quote, centered on the bot's S estimate:
1. Compute `E[S]` using Bayesian aggregation.
2. Select optimal width using parity arbitrage (checking widths near the floor).
3. Return `(bid, ask)` centered on the estimate.

### `respond(obs, quote, turn) → str or tuple`
Taker/responder: accept or counter, incorporating power economics:
1. Compute net fill-shift from TRICK_ROOM and STEALTH_ROCK balance.
2. On turn 6 with positive net shift: evaluate forcing vs. accepting.
3. Accept BUY if edge is clear and better than forcing.
4. Accept SELL only if no shift power to exploit.
5. Otherwise: counter toward the value estimate, shrinking width as required.

### `use_transform(obs) → bool`
Decide whether to fire the swap:
- **Return True** (fire) if hand is flat (`|k_mine| ≤ 1`).
- **Return False** (decline) if hand is decisive (`|k_mine| > 1`).

---

## Calibrated Power Values

The bot uses per-round tick values derived from extensive testing:

| Power | Round 1 | Round 2 | Round 3 | Round 4 | Round 5 |
|-------|---------|---------|---------|---------|---------|
| **FORESIGHT** | 0.80 | 1.25 | 1.60 | 2.05 | 2.15 |
| **TRICK_ROOM** | 1.10 | 1.05 | 1.05 | 0.95 | 0.95 |
| **SUBSTITUTE** | 1.50 | 1.20 | 0.95 | 0.58 | 0.28 |
| **STEALTH_ROCK** | 1.65 | 1.20 | 0.90 | 0.72 | 0.00 |
| **TRANSFORM** | 1.58 | 1.30 | 1.35 | 0.00 | 0.00 |

**Observations**:
- **FORESIGHT** climbs as more opponent coins are revealed (diminishing uncertainty from later rounds).
- **SUBSTITUTE** falls (fewer rounds left to protect).
- **STEALTH_ROCK** is zero in round 5 (no remaining rounds to apply the shift).
- **TRANSFORM** is unavailable after round 3.
- **TRICK_ROOM** is consistently valuable for the forcing exploit.

---

## Complexity & Performance

- **Time complexity**: O(1) per method call (no iterative loops over coin outcomes).
- **Allowed imports**: `math`, `random`, `collections` (all standard library).
- **Memory**: Minimal per-round state (anchors dict, cached values).
- **Average call time**: Well under the 2 ms design target.

---

## Key Innovations

1. **Forcing Exploit**: Mathematically proves that holding an uncontested fill-shift power makes forcing always superior to selling.
2. **Parity Arbitrage**: Leverages the discrete lattice structure of coin outcomes to select opening widths with extra EV.
3. **Denial Bidding**: Uses quote anchors to infer opponent flatness and decides whether to bid for TRANSFORM defensively.
4. **Dynamic Shading**: Adapts bid amounts to the relative value of current vs. future powers, balancing 24 TE across 5 rounds.

---

## Files & Integration

- **File**: `strategies/elite_bot.py`
- **Class**: `Bot` (required by the tournament harness)
- **Validation**: Run `python backtester.py --validate strategies/elite_bot.py`
- **Testing**: `python backtester.py --bot1 strategies/elite_bot.py --bot2 strategies/rational.py --isolate`

---

*Elite Bot — BayesianOracle Strategy | QuantStorm 2026*
