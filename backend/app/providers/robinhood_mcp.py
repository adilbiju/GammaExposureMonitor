from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Iterable

from app.models import MarketSnapshot, OptionContract
from app.providers.mcp_gateway import McpGateway


def _json_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    blocks = getattr(result, "content", [])
    texts = [getattr(block, "text", "") for block in blocks if getattr(block, "text", None)]
    if not texts:
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _deep_find(record: dict[str, Any], names: set[str]) -> Any:
    normalized = {name.lower().replace("-", "_") for name in names}
    for key, value in record.items():
        if key.lower().replace("-", "_") in normalized and value not in (None, ""):
            return value
    for value in record.values():
        if isinstance(value, dict):
            found = _deep_find(value, names)
            if found not in (None, ""):
                return found
    return None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _integer(value: Any) -> int:
    return int(_number(value))


def _iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class RobinhoodMcpProvider:
    def __init__(self, gateway: McpGateway):
        self.gateway = gateway

    def _find_tool(self, *candidates: str) -> str | None:
        for candidate in candidates:
            for tool in self.gateway.tools:
                normalized = tool.lower().replace(".", "_").replace("-", "_")
                if normalized == candidate or normalized.endswith(f"_{candidate}"):
                    return tool
        return None

    def _schema_properties(self, tool: str) -> dict[str, Any]:
        schema = self.gateway.tool_schemas.get(tool, {})
        return schema.get("properties", {}) if isinstance(schema, dict) else {}

    def _symbol_arguments(self, tool: str, symbol: str) -> dict[str, Any]:
        properties = self._schema_properties(tool)
        for key in ("symbol", "ticker", "underlying_symbol"):
            if key in properties:
                return {key: symbol}
        for key in ("symbols", "tickers", "underlying_symbols"):
            if key in properties:
                return {key: [symbol]}
        return {"symbol": symbol}

    def _id_arguments(self, tool: str, ids: list[str]) -> dict[str, Any]:
        properties = self._schema_properties(tool)
        for key in ("instrument_ids", "option_ids", "contract_ids", "ids", "instruments"):
            if key in properties:
                return {key: ids}
        for key in ("instrument_id", "option_id", "contract_id", "id"):
            if key in properties and len(ids) == 1:
                return {key: ids[0]}
        return {"instrument_ids": ids}

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        return _json_payload(await self.gateway.call_tool(tool, arguments))

    def _extract_quote(self, payload: Any, symbol: str) -> tuple[float, float | None]:
        records = list(_walk_dicts(payload))
        matching = [
            record
            for record in records
            if str(_deep_find(record, {"symbol", "ticker"}) or "").upper() == symbol
        ]
        candidates = matching or records
        for record in candidates:
            spot = _number(
                _deep_find(record, {"mark_price", "last_trade_price", "last_price", "price", "mid_price"})
            )
            if spot > 0:
                previous = _number(
                    _deep_find(record, {"previous_close", "previous_close_price", "prev_close"}),
                    default=0,
                )
                return spot, previous or None
        raise ValueError(f"Robinhood MCP returned no usable equity quote for {symbol}.")

    def _contract_records(self, payload: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record in _walk_dicts(payload):
            strike = _deep_find(record, {"strike", "strike_price"})
            expiry = _deep_find(record, {"expiration", "expiration_date", "expiry", "expiry_date"})
            option_type = _deep_find(record, {"option_type", "type", "chain_type"})
            if strike is not None and expiry is not None and str(option_type).lower() in {
                "call",
                "put",
                "c",
                "p",
            }:
                records.append(record)
        return records

    def _record_id(self, record: dict[str, Any]) -> str | None:
        value = _deep_find(record, {"instrument_id", "option_id", "contract_id", "id", "url"})
        return str(value) if value else None

    def _merge_records(self, instruments: list[dict[str, Any]], quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        quotes_by_id = {self._record_id(item): item for item in quotes if self._record_id(item)}
        merged: list[dict[str, Any]] = []
        for instrument in instruments:
            identifier = self._record_id(instrument)
            quote = quotes_by_id.get(identifier)
            merged.append({"instrument": instrument, "quote": quote or {}})
        return merged

    def _normalize_contract(self, record: dict[str, Any], symbol: str, index: int) -> OptionContract | None:
        strike = _number(_deep_find(record, {"strike", "strike_price"}))
        expiration = _iso_date(_deep_find(record, {"expiration", "expiration_date", "expiry", "expiry_date"}))
        raw_type = str(_deep_find(record, {"option_type", "type", "chain_type"}) or "").lower()
        option_type = "call" if raw_type in {"call", "c"} else "put" if raw_type in {"put", "p"} else None
        if strike <= 0 or not expiration or not option_type:
            return None
        identifier = self._record_id(record) or f"{symbol}-{expiration}-{strike}-{option_type}-{index}"
        bid = _number(_deep_find(record, {"bid", "bid_price"}))
        ask = _number(_deep_find(record, {"ask", "ask_price"}))
        mark = _number(_deep_find(record, {"mark", "mark_price", "mid_price"}), (bid + ask) / 2)
        iv = _number(_deep_find(record, {"implied_volatility", "implied_volatility_percentage", "iv"}))
        if iv > 3:
            iv /= 100
        return OptionContract(
            id=identifier,
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            option_type=option_type,
            gamma=_number(_deep_find(record, {"gamma"})),
            implied_volatility=iv,
            open_interest=_integer(_deep_find(record, {"open_interest", "openinterest"})),
            volume=_integer(_deep_find(record, {"volume"})),
            bid=bid,
            ask=ask,
            mark=mark,
        )

    async def snapshot(self, symbol: str) -> MarketSnapshot:
        if not self.gateway.connected:
            raise RuntimeError("Robinhood MCP is not connected. Use Connect Robinhood first.")
        symbol = symbol.upper()
        quote_tool = self._find_tool("get_equity_quotes", "market_quote", "market_current_price")
        if not quote_tool:
            raise RuntimeError("Connected MCP has no supported equity quote tool.")
        quote_payload = await self._call(quote_tool, self._symbol_arguments(quote_tool, symbol))
        spot, previous_close = self._extract_quote(quote_payload, symbol)

        community_chain = self._find_tool("robinhood_options_chain", "options_chain")
        records: list[dict[str, Any]] = []
        if community_chain:
            chain_payload = await self._call(
                community_chain, self._symbol_arguments(community_chain, symbol)
            )
            records = self._contract_records(chain_payload)
        else:
            chain_tool = self._find_tool("get_option_chains")
            instruments_tool = self._find_tool("get_option_instruments")
            quotes_tool = self._find_tool("get_option_quotes")
            if not instruments_tool:
                raise RuntimeError("Connected MCP has no supported option-instruments tool.")

            chain_payload: Any = {}
            if chain_tool:
                chain_payload = await self._call(chain_tool, self._symbol_arguments(chain_tool, symbol))

            instrument_args = self._symbol_arguments(instruments_tool, symbol)
            chain_id = next(
                (
                    _deep_find(item, {"chain_id", "id"})
                    for item in _walk_dicts(chain_payload)
                    if _deep_find(item, {"chain_id"})
                ),
                None,
            )
            if chain_id and "chain_id" in self._schema_properties(instruments_tool):
                instrument_args["chain_id"] = str(chain_id)
            instruments_payload = await self._call(instruments_tool, instrument_args)
            instruments = self._contract_records(instruments_payload)
            if not instruments:
                instruments = [
                    item
                    for item in _walk_dicts(instruments_payload)
                    if self._record_id(item) and _deep_find(item, {"strike", "strike_price"})
                ]

            quote_records: list[dict[str, Any]] = []
            if quotes_tool:
                ids = [identifier for item in instruments if (identifier := self._record_id(item))]
                for start in range(0, len(ids), 50):
                    payload = await self._call(
                        quotes_tool, self._id_arguments(quotes_tool, ids[start : start + 50])
                    )
                    quote_records.extend(list(_walk_dicts(payload)))
            records = self._merge_records(instruments, quote_records) if quote_records else instruments

        contracts = [
            contract
            for index, record in enumerate(records)
            if (contract := self._normalize_contract(record, symbol, index)) is not None
        ]
        contracts = [contract for contract in contracts if contract.gamma > 0 and contract.open_interest > 0]
        if not contracts:
            raise ValueError(
                "The MCP response contained no contracts with both gamma and open interest. "
                "Use the connection diagnostics to verify the server's option quote fields."
            )
        return MarketSnapshot(
            symbol=symbol,
            spot=spot,
            previous_close=previous_close,
            as_of=datetime.now(timezone.utc),
            contracts=contracts,
            source="Robinhood Trading MCP",
        )

