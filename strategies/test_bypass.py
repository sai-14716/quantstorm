import random

class Bot:
    name = "BlufferBot"

    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config

    def bid(self, obs, offered):
        return {}  # don't care about powers for this test

    def quote(self, obs):
        r = obs.round
        cap = obs.spread_cap
        
        # Maximize bluff!
        # If we bluff UP, we set bid = 40.
        bid = self.config.N_COINS
        ask = bid + cap
        return (bid, ask)

    def respond(self, obs, quote, turn):
        bid_p, ask_p = quote
        # We know we bluffed UP. We want to ACCEPT_SELL at any high price.
        # The true score is approx obs.k_mine
        v = obs.k_mine
        
        edge_buy = v - ask_p
        edge_sell = bid_p - v
        
        if edge_sell > 5.0:
            return "ACCEPT_SELL"
            
        if edge_buy > 5.0:
            return "ACCEPT_BUY"
            
        # Taker logic (when we are not Maker)
        # Just play honestly
        floor = obs.final_cap
        cur_width = ask_p - bid_p
        new_width = max(floor, cur_width - self.config.MIN_REDUCTION)
        
        center = max(bid_p, min(round(v), ask_p - new_width))
        return ("COUNTER", center, center + new_width)

    def use_transform(self, obs):
        return False
