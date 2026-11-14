from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from app.calculations.gex import black_scholes_gamma
from app.models import MarketSnapshot, OptionContract


class DemoProvider:
    """Deterministic, plausible data for UI development; never presented as live data."""

    BASE = {"SPY": (598.42, 596.78), "QQQ": (526.18, 523.90), "SPX": (6900.0, 6874.0)}

    async def snapshot(self, symbol: str) -> MarketSnapshot:
        symbol = symbol.upper()
        spot, previous = self.BASE.get(symbol, (100.0, 99.4))
        now = datetime.now(timezone.utc)
        rng = random.Random(f"{symbol}:{now.date().isoformat()}")
        next_friday = now.date() + timedelta(days=(4 - now.weekday()) % 7 or 7)
        expirations = [next_friday, next_friday + timedelta(days=7), next_friday + timedelta(days=28)]
        step = 5 if spot > 200 else 2.5
        center = round(spot / step) * step
        strikes = [center + step * offset for offset in range(-14, 15)]
        contracts: list[OptionContract] = []

        for expiry_index, expiration in enumerate(expirations):
            years = max((expiration - now.date()).days, 1) / 365
            for strike in strikes:
                distance = abs(strike - spot) / spot
                iv = 0.18 + 0.55 * distance + expiry_index * 0.012
                gamma = black_scholes_gamma(spot, strike, iv, years, 0.045)
                bell = math.exp(-0.5 * ((strike - spot) / (spot * 0.055)) ** 2)
                for option_type in ("call", "put"):
                    wall_boost = 1.0
                    if option_type == "call" and strike == center + step * 4:
                        wall_boost = 4.3
                    if option_type == "put" and strike == center - step * 5:
                        wall_boost = 4.8
                    skew = 1.18 if option_type == "put" and strike < spot else 1.0
                    oi = int((900 + 13500 * bell) * wall_boost * skew * (1 - expiry_index * 0.12))
                    oi += rng.randint(0, 1200)
                    intrinsic = max(spot - strike, 0) if option_type == "call" else max(strike - spot, 0)
                    time_value = max(0.08, spot * iv * math.sqrt(years) * 0.16 * bell)
                    mark = intrinsic + time_value
                    spread = max(0.02, mark * 0.025)
                    contracts.append(
                        OptionContract(
                            id=f"demo-{symbol}-{expiration}-{strike}-{option_type}",
                            symbol=symbol,
                            expiration=expiration,
                            strike=strike,
                            option_type=option_type,
                            gamma=gamma,
                            implied_volatility=iv,
                            open_interest=oi,
                            volume=int(oi * rng.uniform(0.03, 0.24)),
                            bid=max(0, mark - spread / 2),
                            ask=mark + spread / 2,
                            mark=mark,
                        )
                    )
        return MarketSnapshot(
            symbol=symbol,
            spot=spot,
            previous_close=previous,
            as_of=now,
            contracts=contracts,
            source="Demo snapshot",
        )

