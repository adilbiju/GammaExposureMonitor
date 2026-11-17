from datetime import date
from typing import Protocol

from app.models import MarketSnapshot


class MarketDataProvider(Protocol):
    async def snapshot(
        self, symbol: str, selected_expirations: list[date] | None = None
    ) -> MarketSnapshot: ...

