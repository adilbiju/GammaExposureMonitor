from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.models import (
    ContractExposure,
    DataQualityImpact,
    GexResponse,
    MarketSnapshot,
    OptionContract,
    ProfilePoint,
    StrikeExposure,
)

ONE_PERCENT_MOVE = 0.01
GAMMA_DISCREPANCY_MULTIPLE = 10.0
MIN_MATERIAL_GEX_DIFFERENCE = 1_000_000.0
MODEL_MATERIALITY_FRACTION = 0.01
NEW_YORK = ZoneInfo("America/New_York")


def contract_gex(contract: OptionContract, spot: float, gamma: float | None = None) -> float:
    direction = 1 if contract.option_type == "call" else -1
    return (
        (contract.gamma if gamma is None else gamma)
        * contract.open_interest
        * contract.contract_multiplier
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
    contract_expirations = sorted({contract.expiration for contract in snapshot.contracts})
    expirations = sorted(set(snapshot.available_expirations) | set(contract_expirations))
    selected = sorted(set(selected_expirations or contract_expirations))
    contracts = [contract for contract in snapshot.contracts if contract.expiration in selected]

    now = snapshot.as_of if snapshot.as_of.tzinfo else snapshot.as_of.replace(tzinfo=timezone.utc)
    observations: list[tuple[OptionContract, float, float, float]] = []
    for contract in contracts:
        model_gamma = black_scholes_gamma(
            snapshot.spot,
            contract.strike,
            contract.implied_volatility,
            _years_to_expiration(contract.expiration, now),
            risk_free_rate,
        )
        observations.append(
            (
                contract,
                model_gamma,
                contract_gex(contract, snapshot.spot),
                contract_gex(contract, snapshot.spot, model_gamma),
            )
        )

    materiality_threshold = max(
        MIN_MATERIAL_GEX_DIFFERENCE,
        sum(abs(modeled_gex) for _, _, _, modeled_gex in observations)
        * MODEL_MATERIALITY_FRACTION,
    )
    strike_buckets: dict[float, dict[str, float]] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    raw_strike_buckets: dict[float, dict[str, float]] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    exposed_contracts: list[ContractExposure] = []
    trusted_contracts: list[OptionContract] = []
    excluded_gex = 0.0
    for contract, model_gamma, reported_gex, modeled_gex in observations:
        smaller_gamma = min(contract.gamma, model_gamma)
        larger_gamma = max(contract.gamma, model_gamma)
        discrepancy_ratio = larger_gamma / smaller_gamma if smaller_gamma > 1e-12 else None
        materially_different = abs(reported_gex - modeled_gex) >= materiality_threshold
        gamma_inconsistent = (
            discrepancy_ratio is not None and discrepancy_ratio >= GAMMA_DISCREPANCY_MULTIPLE
        ) or (model_gamma <= 1e-12 and contract.gamma > 1)
        excluded = materially_different and gamma_inconsistent

        raw_strike_buckets[contract.strike][contract.option_type] += reported_gex
        if excluded:
            exposure = 0.0
            excluded_gex += reported_gex
        else:
            exposure = reported_gex
            strike_buckets[contract.strike][contract.option_type] += exposure
            trusted_contracts.append(contract)
        exposed_contracts.append(
            ContractExposure(
                **contract.model_dump(),
                gex=exposure,
                reported_gex=reported_gex,
                model_gamma=model_gamma,
                gamma_discrepancy_ratio=discrepancy_ratio,
                excluded_from_aggregates=excluded,
            )
        )

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

    raw_strike_exposure = [
        StrikeExposure(
            strike=strike,
            call_gex=values["call"],
            put_gex=values["put"],
            net_gex=values["call"] + values["put"],
        )
        for strike, values in sorted(raw_strike_buckets.items())
    ]
    raw_total_call = sum(item.call_gex for item in raw_strike_exposure)
    raw_total_put = sum(item.put_gex for item in raw_strike_exposure)

    profile: list[ProfilePoint] = []
    lower = snapshot.spot * 0.85
    upper = snapshot.spot * 1.15
    for index in range(61):
        scenario_spot = lower + (upper - lower) * index / 60
        scenario_gex = 0.0
        for contract in trusted_contracts:
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
    raw_call_candidates = [item for item in raw_strike_exposure if item.call_gex > 0]
    raw_put_candidates = [item for item in raw_strike_exposure if item.put_gex < 0]
    raw_largest_candidates = [item for item in raw_strike_exposure if item.net_gex != 0]
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
        data_quality=DataQualityImpact(
            excluded_contract_count=sum(
                contract.excluded_from_aggregates for contract in exposed_contracts
            ),
            excluded_gex=excluded_gex,
            raw_total_call_gex=raw_total_call,
            raw_total_put_gex=raw_total_put,
            raw_net_gex=raw_total_call + raw_total_put,
            raw_call_wall=max(raw_call_candidates, key=lambda item: item.call_gex).strike
            if raw_call_candidates
            else None,
            raw_put_wall=min(raw_put_candidates, key=lambda item: item.put_gex).strike
            if raw_put_candidates
            else None,
            raw_largest_gex_strike=max(
                raw_largest_candidates, key=lambda item: abs(item.net_gex)
            ).strike
            if raw_largest_candidates
            else None,
        ),
    )

