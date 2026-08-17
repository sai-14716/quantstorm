# Name: SRIKANTAM SAI SRINIVAS
# College: IIT Kharagpur
# Roll Number: 22CS30053

"""
BayesianOracle — Elite Statistical Bot
=========================================
QuantStorm 2026 | Divided Oracle

Statistical Framework:
──────────────────────
1. BAYESIAN S-ESTIMATION
   E[S | info] = k_mine + E[k_opp_revealed] + 0
   k_opp_revealed sources (in reliability order):
     a) FORESIGHT coins: direct observation, zero noise
        - Rounds 1–4: min(16,4r)=4r coins seen → full opponent revealed sum known
        - Round 5: 16/20 seen, 4 unseen (E=0 each via i.i.d. prior)
     b) Quote midpoint: Maker centres on k_mine, so midpoint ≈ opponent's k
        - Anchored on first-seen quote per round, never updated (contamination)
        - Blended with FORESIGHT via inverse-variance weighting
     c) Multi-round accumulation: best anchor across all rounds where we were Taker

2. DYNAMIC TE BUDGET ALLOCATION (Multi-Round Knapsack Approximation)
   Budget = 24 TE / deal, ~3 powers at equilibrium shade.
   Each round: compare offered-power value vs E[future_power_value].
   If current > 1.15 × future_avg: shade UP to 0.75 (above-average slot)
   If current < 0.80 × future_avg: shade DOWN to 0.42 (save TE for better slot)
   Otherwise: use calibrated shade = 0.62 (BNE solution for this field)

3. TRICK_ROOM / STEALTH_ROCK FORCING EXPLOIT
   If I hold TRICK_ROOM (shift=+3) and counter on turn 6 (forcing fee=2):
     Net gain = 3 - 2 = +1 tick above midpoint outcome.
     Proof: Force PnL (as short) = mid + 3 - v - 2 = mid + 1 - v
            Accept Sell PnL       = bid - v ≤ (mid-1) - v  (since bid ≤ mid-1)
     => Force is ALWAYS ≥ 2 ticks better than Accept Sell.
     => Accept Buy preferred only when edge_buy > net_shift - 2.
   Strategy: NEVER accept sell when holding TRICK_ROOM with net shift > 0.
             Force (counter) on last turn unless accepting buy is clearly better.

4. PARITY-AWARE WIDTH SELECTION
   S residual (coins not yet seen by Maker) always has the same parity as
   the number of unseen coins → discrete lattice, not continuous.
   An odd-width window can cover more parity-compatible values than an
   even-width window of similar size. Use config.straddle_prob(r, w) exactly
   and check widths [floor, floor+1, floor+2] for the best EV:
     net_delta(w) = 3.0 × (p_w - p_floor) - 0.22 × (w - floor)
   Pick w* = argmax net_delta. This is free money from lattice structure.

5. SUBSTITUTE-ADJUSTED ACCEPT THRESHOLDS
   If holding SUBSTITUTE, loss capped at 2 ticks → lower the buy/sell
   threshold by 1.0 tick (willingness to accept thin edges).

6. TRANSFORM — BIDIRECTIONAL GAME THEORY
   Flat hand (|k_mine| ≤ 1): buy to fire the swap → get a potentially better hand.
   Decisive hand (|k_mine| ≥ 3): buy to VETO → opponent wants our good hand,
     spending the power denies them even if we decline.
   Denial value = DENIAL_WEIGHT × swap_value (calibrated via opponent flatness read).

7. POSTERIOR VARIANCE-AWARE RESPONSE
   Even when no clear edge, the variance of S from our perspective determines
   how aggressively to counter. Widen counters in high-uncertainty rounds.

All computations are O(1) per call (no iterative loops over coin outcomes).
Uses only: math, random, collections (all permitted).
"""

import math
import random
from collections import defaultdict


# ─── Calibrated power values (ticks per win, per round) ───────────────────────
# Source: per-round measurement from adaptive_bidder docstring + re-calibrated
# for TRICK_ROOM (corrected from 0.0 → 1.0: forcing is always profitable with
# net shift > 0 after paying the 2-tick forcing fee).
# FORESIGHT climbs (more opponent coins revealed in later rounds).
# SUBSTITUTE falls (fewer remaining rounds to protect).
# STEALTH_ROCK: persistent; very valuable early, zero in round 5.
POWER_VALUES = {
    "FORESIGHT":    {1: 0.80, 2: 1.25, 3: 1.60, 4: 2.05, 5: 2.15},
    "TRICK_ROOM":   {1: 1.10, 2: 1.05, 3: 1.05, 4: 0.95, 5: 0.95},
    "SUBSTITUTE":   {1: 1.50, 2: 1.20, 3: 0.95, 4: 0.58, 5: 0.28},
    "STEALTH_ROCK": {1: 1.65, 2: 1.20, 3: 0.90, 4: 0.72, 5: 0.00},
    "TRANSFORM":    {1: 1.58, 2: 1.30, 3: 1.35, 4: 0.00, 5: 0.00},
}

# First-price auction shade factor (fraction of fair value to bid).
# Solved as BNE against field using the per-round surface.
# Broad basin: 0.58–0.68 all near-optimal. Outside this range is costly.
SHADE_BASE       = 0.62
SHADE_PREMIUM    = 0.75   # for above-average-future-value powers
SHADE_DISCOUNT   = 0.42   # for below-average-future-value powers
PREMIUM_RATIO    = 1.15   # current / future_avg > this → premium shade
DISCOUNT_RATIO   = 0.80   # current / future_avg < this → discount shade

# TRANSFORM thresholds
FLAT_THRESHOLD       = 1    # |k_mine| ≤ this → hand is flat
OPP_FLAT_THRESHOLD   = 2.5  # |opp_estimate| ≤ this → opponent looks flat
DENIAL_WEIGHT        = 0.45  # fraction of swap value to pay for denial bid


# ─────────────────────────────────────────────────────────────────────────────


class Bot:
    name = "BayesianOracle"

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def reset(self, seat, config, seed):
        """Called once per deal before round 1. All per-deal state lives here."""
        self.seat   = seat
        self.config = config
        self.rng    = random.Random(seed)

        # Quote anchors: {round → float} — opponent's inferred k_theirs midpoint.
        # Set when WE are the Taker and see the Maker's opening quote.
        # Read on subsequent rounds to estimate opponent hand.
        self._anchors = {}

        # Per-round cached value estimates (to avoid redundant computation).
        self._cached_v = {}   # {round → float}

    # ── Internal: Information Aggregation ────────────────────────────────────

    def _opp_revealed_estimate(self, obs):
        """Best estimate of sum(opponent's revealed coins).

        Priority:
          1. FORESIGHT (direct observation, zero noise).
             Rounds 1–4: we see min(16, 4r) = 4r coins (all revealed ones).
             Round 5: we see 16 of 20 → 4 unobserved, E[each]=0 by i.i.d.
          2. Quote anchor from the current round (if we're taker and just got it).
          3. Best historical quote anchor from prior rounds.
          4. Prior: 0.0 (opponent coins are mean-zero).
        """
        r = obs.round
        n_opp_revealed = self.config.REVEAL_PER_ROUND * r   # e.g. 4r

        if obs.foresight:
            fs_sum = sum(obs.foresight)
            n_seen = len(obs.foresight)
            n_unseen = n_opp_revealed - n_seen   # unobserved opponent revealed coins

            if n_unseen <= 0:
                # Perfect information about all their revealed coins (rounds 1–4)
                return float(fs_sum)

            # Round 5: 4 unobserved revealed coins.  E[each] = 0 by i.i.d. prior.
            # But if we have a quote anchor for THIS round (from the respond() call
            # that set it before quote() runs?  No — quote() for maker has no quote.
            # If we're Taker in round 5, anchor gets set in respond() first call.
            # However bid() and quote() both happen BEFORE respond(), so in bid()
            # we won't have round-r anchor yet.  Use prior rounds' anchors.
            if r in self._anchors:
                # Anchor ≈ k_theirs_revealed_at_r.
                # Our foresight covers n_seen of those coins exactly.
                # Anchor implies the n_unseen unobserved coins sum to ~ anchor - fs_sum.
                # Weight by fraction of variance explained:
                w = n_unseen / n_opp_revealed
                implied_unobserved = self._anchors[r] - fs_sum
                return float(fs_sum + w * implied_unobserved)

            return float(fs_sum)   # E[4 unseen] = 0

        # No FORESIGHT: use best available quote anchor.
        available = [rd for rd in self._anchors if rd <= r]
        if available:
            return float(self._anchors[max(available)])

        return 0.0   # flat prior

    def _estimate_v(self, obs, quote=None):
        """E[S | all available information] = k_mine + E[k_opp_revealed].

        Also records the quote anchor on first observation as Taker.
        """
        r = obs.round

        # Latch the opening quote midpoint when we first see it (as Taker).
        # ONLY the opening quote is a clean read; later counters are contaminated.
        if not obs.is_maker and quote is not None and r not in self._anchors:
            self._anchors[r] = (quote[0] + quote[1]) / 2.0

        return float(obs.k_mine) + self._opp_revealed_estimate(obs)

    def _posterior_var(self, obs):
        """Approximate posterior variance of S given our current information.

        S = k_mine (known) + k_opp_revealed (estimated) + k_opp_unrevealed (unknown)
            + k_unknown_both (unknown to both).
        Variance from coins we haven't seen (or estimated with uncertainty).
        """
        r = obs.round
        n_opp_revealed  = self.config.REVEAL_PER_ROUND * r
        n_foresight     = len(obs.foresight)
        n_opp_unrevealed = self.config.N_PRIVATE - n_opp_revealed
        n_unknown_both  = obs.n_unknown_both

        # Uncertainty from unobserved opponent revealed coins:
        if obs.foresight:
            # Remaining = n_opp_revealed - n_foresight
            # Round 1-4: 0 remaining (perfect FORESIGHT); round 5: 4 remaining
            opp_rev_var = max(0, n_opp_revealed - n_foresight)
        elif self._anchors:
            # We have a quote-based estimate of k_opp_revealed.
            # The quote midpoint has noise ≈ spread/2 ≈ 2–4 ticks.
            # Residual variance on the revealed part ≈ n_opp_revealed but attenuated.
            # Conservative: treat as if we see ~half the opponent's revealed coins.
            opp_rev_var = n_opp_revealed * 0.5
        else:
            opp_rev_var = float(n_opp_revealed)

        # Variance from opponent's unrevealed coins and coins unknown to both:
        other_var = float(n_opp_unrevealed + n_unknown_both)

        return opp_rev_var + other_var

    # ── Internal: Auction Helpers ─────────────────────────────────────────────

    def _opp_flat_estimate(self, obs):
        """Latest estimate of opponent revealed sum, from pre-current-round anchors."""
        available = [rd for rd in self._anchors if rd < obs.round]
        if not available:
            return None
        return self._anchors[max(available)]

    def _avg_future_power_value(self, obs):
        """Average tick value per future round's power (expected over draw).

        Used as baseline for dynamic shading: if the current offer is
        significantly above or below this, adjust shade accordingly.
        """
        r = obs.round
        totals = []
        for fr in range(r + 1, self.config.N_ROUNDS + 1):
            pool = self.config.offered_powers(fr)
            if pool:
                vals = [POWER_VALUES.get(p, {}).get(fr, 0.5) for p in pool]
                totals.append(sum(vals) / len(vals))
        return sum(totals) / len(totals) if totals else 0.5

    def _power_value(self, obs, name):
        """Tick value of `name` in the current round (state-dependent)."""
        if name == "TRANSFORM":
            return self._transform_value(obs)
        return POWER_VALUES.get(name, {}).get(obs.round, 0.5)

    def _transform_value(self, obs):
        """Value of winning TRANSFORM this round.

        Three cases:
          flat hand     → buy to FIRE the swap (gain a potentially better hand)
          decisive hand → buy to VETO (deny opponent the swap)
          no read available on opponent → bid 0 if decisive (denial is too uncertain)
        """
        base = POWER_VALUES.get("TRANSFORM", {}).get(obs.round, 0.0)
        if base <= 0:
            return 0.0

        if abs(obs.k_mine) <= FLAT_THRESHOLD:
            # Our hand is flat (near-zero information). Swap for anything better.
            return base

        # Decisive hand. Do we need to deny?
        opp_est = self._opp_flat_estimate(obs)
        if opp_est is not None and abs(opp_est) <= OPP_FLAT_THRESHOLD:
            # Opponent appears flat → they would fire the swap → deny them.
            return base * DENIAL_WEIGHT

        # No read that suggests swap is coming.  Don't waste TE.
        return 0.0

    # ── bid() ─────────────────────────────────────────────────────────────────

    def bid(self, obs, offered):
        """Dynamic TE bidding using state-conditional valuations and future EV."""
        if not offered or obs.te_mine <= 0:
            return {}

        r = obs.round
        result = {}

        future_avg = self._avg_future_power_value(obs)
        remaining_slots = self.config.N_ROUNDS - r

        for name in offered:
            v = self._power_value(obs, name)
            if v <= 0:
                continue

            # Convert ticks → TE fair value.
            fair_te = v / self.config.TE_SALVAGE

            # Dynamic shading: bid more on above-average slots, less on weak ones.
            if future_avg > 0.01:
                ratio = v / future_avg
                if ratio >= PREMIUM_RATIO:
                    shade = SHADE_PREMIUM
                elif ratio <= DISCOUNT_RATIO:
                    shade = SHADE_DISCOUNT
                else:
                    # Linear interpolation between discount and base
                    t = (ratio - DISCOUNT_RATIO) / (PREMIUM_RATIO - DISCOUNT_RATIO)
                    shade = SHADE_DISCOUNT + t * (SHADE_BASE - SHADE_DISCOUNT)
            else:
                shade = SHADE_BASE

            # If this is the last round, bid full value (no future to save for).
            if remaining_slots == 0:
                shade = min(0.90, shade + 0.15)

            bid_te = int(fair_te * shade)
            bid_te = max(0, min(bid_te, obs.te_mine))

            if bid_te > 0:
                result[name] = bid_te

        # Safety: since SLOTS_PER_ROUND=1 we only have one power in `offered`,
        # so total bids == single bid value ≤ obs.te_mine already.
        # But guard against spec changes with a hard total check.
        total = sum(result.values())
        if total > obs.te_mine:
            # Scale down proportionally, rounding to ints.
            scale = obs.te_mine / total
            result = {k: max(1, int(v_te * scale)) for k, v_te in result.items()}
            if sum(result.values()) > obs.te_mine:
                result = {}   # safety: bid nothing rather than get zeroed

        return result

    # ── quote() ───────────────────────────────────────────────────────────────

    def _optimal_width(self, obs):
        """Choose opening quote width to maximise expected Maker PnL.

        Three components:
          a) Width premium cost: -0.22 per tick above floor.
          b) Straddle improvement: +3.0 × (p_w - p_floor) from parity effect.
          c) Fill-shift forcing: if we hold TRICK_ROOM/STEALTH_ROCK, open WIDE
             to make negotiations reach turn 6 (forced fill more likely).

        We check widths [floor, floor+1, floor+2] and pick the best net EV.
        """
        r        = obs.round
        floor    = obs.final_cap
        cap      = obs.spread_cap

        # ─ Forcing-power override ─
        mine_shifts = obs.powers_mine & {"TRICK_ROOM", "STEALTH_ROCK"}
        opp_shifts  = obs.powers_theirs & {"TRICK_ROOM", "STEALTH_ROCK"}
        if mine_shifts and not opp_shifts:
            # Holding uncontested fill-shift power → want forced fill → open wide.
            return cap

        # ─ Parity arbitrage: find best width near floor ─
        p_floor  = self.config.straddle_prob(r, floor)
        best_w   = floor
        best_net = 0.0   # delta vs floor baseline (0 at floor by construction)

        for w in range(floor + 1, min(floor + 4, cap + 1)):
            p_w   = self.config.straddle_prob(r, w)
            # Net EV change from using width w instead of floor:
            #   +3.0 × (p_w - p_floor) straddle obligation benefit
            #   -0.22 × (w - floor) width premium cost
            net = 3.0 * (p_w - p_floor) - self.config.WIDTH_PREMIUM * (w - floor)
            if net > best_net:
                best_net = net
                best_w   = w

        return best_w

    def quote(self, obs):
        """Maker: two-sided opening quote centred on our best S estimate."""
        v     = round(self._estimate_v(obs))
        width = self._optimal_width(obs)

        lo = v - width // 2
        hi = lo + width

        return (lo, hi)

    # ── respond() ─────────────────────────────────────────────────────────────

    def respond(self, obs, quote, turn):
        """Taker/Maker: accept or counter, incorporating power economics.

        Decision hierarchy:
          1. Compute net fill shift (TRICK_ROOM + STEALTH_ROCK balance).
          2. If net shift strongly positive and on last turn → force (counter).
          3. Accept BUY if positive buy edge and edge > forcing gain.
          4. If holding TRICK_ROOM (net shift > 0): never accept sell.
          5. Accept SELL if positive sell edge and no shift power to exploit.
          6. Counter toward our value estimate.
        """
        bid_p, ask_p = quote
        v  = self._estimate_v(obs, quote)
        r  = obs.round
        floor = obs.final_cap
        N  = self.config.N_TURNS

        edge_buy  = v - ask_p    # >0 → profitable to buy at ask
        edge_sell = bid_p - v    # >0 → profitable to sell at bid

        # ─ Net fill-shift in our favour (as the short seat on a forced fill) ─
        # Short seat gains: +TRICK_ROOM magnitude if we hold it,
        #                   +STEALTH_ROCK magnitude if we hold it (persistent).
        # Long seat gains same for their powers → shifts cancel.
        def _shift_mag(p):
            return self.config.POWERS[p]["magnitude"] if p in self.config.POWERS else 0

        shift_mine = sum(_shift_mag(p) for p in obs.powers_mine
                         if p in ("TRICK_ROOM", "STEALTH_ROCK"))
        shift_theirs = sum(_shift_mag(p) for p in obs.powers_theirs
                           if p in ("TRICK_ROOM", "STEALTH_ROCK"))
        net_shift = shift_mine - shift_theirs

        # When I counter on the last turn: I pay forcing fee and become SHORT.
        # Net forcing gain (as short) vs NOT forcing:
        #   force_pnl = mid + net_shift - v - FORCED_FILL_FEE
        # where mid = (bid_p + ask_p) // 2
        fee  = self.config.FORCED_FILL_FEE   # 2.0
        mid  = (bid_p + ask_p) // 2
        force_pnl = float(mid) + net_shift - v - fee   # expected PnL if I force

        # ─ Acceptance threshold adjustments ─
        thresh = 0.0
        if "SUBSTITUTE" in obs.powers_mine:
            # Loss capped at 2 ticks → willingness to buy/sell on thinner edges.
            thresh -= 1.0

        # ─ Last-turn forcing decision ─
        if turn == N and net_shift > 0:
            # Can I do better by forcing (net_shift - fee > 0) vs accepting?
            force_net = float(net_shift) - fee   # net gain from shift after fee
            if force_net > 0:
                # Force is profitable in expectation above the midpoint outcome.
                # Force beats Accept Sell when net_shift - fee > -(mid - bid):
                # i.e. net_shift - fee + (mid - bid) > 0 → always true for fee<spread
                # Force beats Accept Buy when force_pnl > edge_buy.
                if edge_buy > force_pnl + 0.5:
                    # Accepting buy is clearly better → accept
                    return "ACCEPT_BUY"
                # Otherwise force (better than accept sell, often better than accept buy)
                w = floor
                center = max(bid_p, min(round(v), ask_p - w))
                center = min(center, ask_p - w)
                return ("COUNTER", center, center + w)

        # ─ Early-turn forcing prep: raise threshold to push toward forced fill ─
        if net_shift > fee and turn < N:
            # We're better off getting a forced fill; don't accept weak edges now.
            thresh += 0.5

        # ─ Standard accept/counter logic ─
        if edge_buy > thresh and edge_buy >= edge_sell:
            return "ACCEPT_BUY"

        # With net shift > 0: forcing is ≥ 2 ticks better than selling at bid.
        # Only accept sell if no usable shift and sell edge is clear.
        if edge_sell > thresh and net_shift <= 0:
            return "ACCEPT_SELL"

        # ─ Counter toward our value, maintaining floor width ─
        cur_width = ask_p - bid_p
        # Shrink by at least MIN_REDUCTION, but never below floor.
        new_width  = max(floor, cur_width - self.config.MIN_REDUCTION)
        # Centre on our estimate, clamped inside current range.
        center = round(v)
        center = max(bid_p, min(center, ask_p - new_width))
        new_bid = center
        new_ask = center + new_width

        # Sanity clamp inside current range (engine does this too, belt-and-braces).
        new_bid = max(bid_p, new_bid)
        new_ask = min(ask_p, new_ask)
        if new_ask - new_bid < floor:
            new_ask = new_bid + floor
        if new_ask > ask_p:
            new_bid = ask_p - new_width
            new_ask = ask_p

        return ("COUNTER", new_bid, new_ask)

    # ── use_transform() ───────────────────────────────────────────────────────

    def use_transform(self, obs):
        """Fire the swap iff we hold a flat hand; veto (decline) if decisive.

        The two branches of bid() land here:
          flat hand     → bought it to swap → fire.
          decisive hand → bought it to deny/veto → decline (power consumed anyway).

        Note: k_mine at this point reflects coins revealed up to this round
        (including the current reveal) so |k_mine| is the correct flatness gauge.
        """
        return abs(obs.k_mine) <= FLAT_THRESHOLD
