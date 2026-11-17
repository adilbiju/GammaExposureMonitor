from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class OptionContract(BaseModel):
    id: str
    symbol: str
    expiration: date
    strike: float
    option_type: Literal["call", "put"]
    contract_multiplier: float = 100
    gamma: float = 0
    implied_volatility: float = 0
    open_interest: int = 0
    volume: int = 0
    bid: float = 0
    ask: float = 0
    mark: float = 0


class MarketSnapshot(BaseModel):
    symbol: str
    spot: float
    previous_close: float | None = None
    as_of: datetime
    available_expirations: list[date] = Field(default_factory=list)
    contracts: list[OptionContract]
    source: str


class StrikeExposure(BaseModel):
    strike: float
    call_gex: float
    put_gex: float
    net_gex: float


class ProfilePoint(BaseModel):
    spot: float
    net_gex: float


class ContractExposure(OptionContract):
    gex: float


class GexResponse(BaseModel):
    symbol: str
    spot: float
    previous_close: float | None
    price_change_pct: float | None
    as_of: datetime
    source: str
    expirations: list[date]
    selected_expirations: list[date]
    total_call_gex: float
    total_put_gex: float
    net_gex: float
    zero_gamma: float | None
    call_wall: float | None
    put_wall: float | None
    largest_gex_strike: float | None
    strike_exposure: list[StrikeExposure]
    gamma_profile: list[ProfilePoint]
    contracts: list[ContractExposure]
    calculation_note: str = Field(
        default="Open-interest model: calls positive, puts negative; dollar gamma per 1% underlying move."
    )


class ConnectionStatus(BaseModel):
    mode: Literal["demo", "mcp"]
    connected: bool
    connecting: bool = False
    authorization_url: str | None = None
    server_url: str | None = None
    tools: list[str] = []
    detail: str | None = None

