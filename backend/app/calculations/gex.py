from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.models import (
    ContractExposure,
    GexResponse,
    MarketSnapshot,
    OptionContract,
    ProfilePoint,
    StrikeExposure,
)

CONTRACT_MULTIPLIER = 100
ONE_PERCENT_MOVE = 0.01
NEW_YORK = ZoneInfo("America/New_York")


def contract_gex(contract: OptionContract, spot: float, gamma: float | None = None) -> float:
    direction = 1 if contract.option_type == "call" else -1
    return (
        (contract.gamma if gamma is None else gamma)
        * contract.open_interest
        * CONTRACT_MULTIPLIER
        * spot
        * spot
        * ONE_PERCENT_MOVE
        * direction
    )


def _years_to_expiration(expiration: date, now: datetime) -> float:
    expiry = datetime.combine(expiration, time(16, 0), tzinfo=NEW_YORK)
    localized = now.astimezone(NEW_YORK)
    seconds = max((expiry - localized).total_seconds(), 3600)
    return seconds / (365.0 * 24 * 60 * 60)


def black_scholes_gamma(spot: float, strike: float, iv: float, years: float, rate: float) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0 or years <= 0:
        return 0
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * years) / (iv * root_t)
    normal_density = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return normal_density / (spot * iv * root_t)


def _zero_crossing(points: list[ProfilePoint], reference: float) -> float | None:
    crossings: list[float] = []
    for left, right in zip(points, points[1:]):
        if left.net_gex == 0:
            crossings.append(left.spot)
        elif left.net_gex * right.net_gex < 0:
            weight = abs(left.net_gex) / (abs(left.net_gex) + abs(right.net_gex))
            crossings.append(left.spot + (right.spot - left.spot) * weight)
    return min(crossings, key=lambda value: abs(value - reference)) if crossings else None


def calculate_gex(
    snapshot: MarketSnapshot,
    selected_expirations: list[date] | None = None,
    risk_free_rate: float = 0.045,
) -> GexResponse:
    expirations = sorted({contract.expiration for contract in snapshot.contracts})
    selected = sorted(set(selected_expirations or expirations))
    contracts = [contract for contract in snapshot.contracts if contract.expiration in selected]

    strike_buckets: dict[float, dict[str, float]] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    exposed_contracts: list[ContractExposure] = []
    for contract in contracts:
        exposure = contract_gex(contract, snapshot.spot)
        strike_buckets[contract.strike][contract.option_type] += exposure
        exposed_contracts.append(ContractExposure(**contract.model_dump(), gex=exposure))

    strike_exposure = [
        StrikeExposure(
            strike=strike,
            call_gex=values["call"],
            put_gex=values["put"],
            net_gex=values["call"] + values["put"],
        )
        for strike, values in sorted(strike_buckets.items())
    ]
    total_call = sum(item.call_gex for item in strike_exposure)
    total_put = sum(item.put_gex for item in strike_exposure)

    profile: list[ProfilePoint] = []
    lower = snapshot.spot * 0.85
    upper = snapshot.spot * 1.15
    now = snapshot.as_of if snapshot.as_of.tzinfo else snapshot.as_of.replace(tzinfo=timezone.utc)
    for index in range(61):
        scenario_spot = lower + (upper - lower) * index / 60
        scenario_gex = 0.0
        for contract in contracts:
            gamma = black_scholes_gamma(
                scenario_spot,
                contract.strike,
                contract.implied_volatility,
                _years_to_expiration(contract.expiration, now),
                risk_free_rate,
            )
            scenario_gex += contract_gex(contract, scenario_spot, gamma)
        profile.append(ProfilePoint(spot=scenario_spot, net_gex=scenario_gex))

    call_candidates = [item for item in strike_exposure if item.call_gex > 0]
    put_candidates = [item for item in strike_exposure if item.put_gex < 0]
    largest_candidates = [item for item in strike_exposure if item.net_gex != 0]
    previous = snapshot.previous_close
    change_pct = ((snapshot.spot / previous) - 1) * 100 if previous else None

    return GexResponse(
        symbol=snapshot.symbol,
        spot=snapshot.spot,
        previous_close=previous,
        price_change_pct=change_pct,
        as_of=snapshot.as_of,
        source=snapshot.source,
        expirations=expirations,
        selected_expirations=selected,
        total_call_gex=total_call,
        total_put_gex=total_put,
        net_gex=total_call + total_put,
        zero_gamma=_zero_crossing(profile, snapshot.spot),
        call_wall=max(call_candidates, key=lambda item: item.call_gex).strike if call_candidates else None,
        put_wall=min(put_candidates, key=lambda item: item.put_gex).strike if put_candidates else None,
        largest_gex_strike=max(largest_candidates, key=lambda item: abs(item.net_gex)).strike
        if largest_candidates
        else None,
        strike_exposure=strike_exposure,
        gamma_profile=profile,
        contracts=sorted(exposed_contracts, key=lambda item: (item.expiration, item.strike, item.option_type)),
    )

