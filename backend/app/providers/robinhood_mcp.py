from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Iterable
from zoneinfo import ZoneInfo

from app.models import MarketSnapshot, OptionContract
from app.providers.mcp_gateway import McpGateway

ToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]
INSTRUMENT_CACHE_SECONDS = 15 * 60
QUOTE_BATCH_SIZE = 50
NEW_YORK = ZoneInfo("America/New_York")


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


def _record_list(payload: Any, name: str) -> list[dict[str, Any]]:
    for record in _walk_dicts(payload):
        value = record.get(name)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _scalar(payload: Any, name: str) -> Any:
    for record in _walk_dicts(payload):
        value = record.get(name)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return value
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


def _active_expirations(values: Iterable[date], now: datetime) -> list[date]:
    current = now.astimezone(NEW_YORK)
    return sorted(
        {
            expiration
            for expiration in values
            if datetime(
                expiration.year,
                expiration.month,
                expiration.day,
                16,
                15,
                tzinfo=NEW_YORK,
            )
            > current
        }
    )


class RobinhoodMcpProvider:
    def __init__(self, gateway: McpGateway):
        self.gateway = gateway
        self._instrument_cache: dict[
            tuple[str, tuple[date, ...]], tuple[float, list[dict[str, Any]]]
        ] = {}

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
        for key in ("symbol", "ticker", "underlying_symbol", "chain_symbol"):
            if key in properties:
                return {key: symbol}
        for key in ("symbols", "tickers", "underlying_symbols"):
            if key in properties:
                value_type = properties[key].get("type") if isinstance(properties[key], dict) else None
                return {key: symbol if value_type == "string" else [symbol]}
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

    async def _call(
        self,
        caller: ToolCaller,
        tool: str,
        arguments: dict[str, Any],
    ) -> Any:
        return _json_payload(await caller(tool, arguments))

    async def _paginated_records(
        self,
        caller: ToolCaller,
        tool: str,
        arguments: dict[str, Any],
        collection: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(500):
            page_arguments = dict(arguments)
            if cursor:
                page_arguments["cursor"] = cursor
            payload = await self._call(caller, tool, page_arguments)
            records.extend(_record_list(payload, collection))
            next_cursor = str(_scalar(payload, "next") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise RuntimeError(f"{tool} exceeded the 500-page safety limit.")
        return records

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

    async def _index_quote(
        self,
        caller: ToolCaller,
        symbol: str,
    ) -> tuple[float, float | None]:
        indexes_tool = self._find_tool("get_indexes")
        quotes_tool = self._find_tool("get_index_quotes")
        if not indexes_tool:
            raise ValueError(f"Robinhood MCP returned no usable quote for {symbol}.")

        indexes_payload = await self._call(
            caller, indexes_tool, self._symbol_arguments(indexes_tool, symbol)
        )
        indexes = _record_list(indexes_payload, "indexes")
        index = next(
            (item for item in indexes if str(item.get("symbol", "")).upper() == symbol),
            None,
        )
        if not index:
            raise ValueError(f"Robinhood MCP returned no usable quote for {symbol}.")

        spot = _number(index.get("current_value"))
        identifier = index.get("id")
        if quotes_tool and identifier:
            quotes_payload = await self._call(
                caller, quotes_tool, self._id_arguments(quotes_tool, [str(identifier)])
            )
            quotes = _record_list(quotes_payload, "quotes")
            quote = next(
                (
                    item
                    for item in quotes
                    if str(item.get("instrument_id", "")) == str(identifier)
                ),
                None,
            )
            if quote:
                spot = _number(quote.get("value"), spot)
        if spot <= 0:
            raise ValueError(f"Robinhood MCP returned no usable quote for {symbol}.")
        return spot, None

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
            contract_multiplier=_number(
                _deep_find(record, {"trade_value_multiplier", "contract_multiplier", "multiplier"}),
                100,
            ),
            gamma=_number(_deep_find(record, {"gamma"})),
            implied_volatility=iv,
            open_interest=_integer(_deep_find(record, {"open_interest", "openinterest"})),
            volume=_integer(_deep_find(record, {"volume"})),
            bid=bid,
            ask=ask,
            mark=mark,
        )

    async def _snapshot(
        self,
        caller: ToolCaller,
        symbol: str,
        selected_expirations: list[date] | None,
    ) -> MarketSnapshot:
        symbol = symbol.upper()
        quote_tool = self._find_tool("get_equity_quotes", "market_quote", "market_current_price")
        if not quote_tool:
            raise RuntimeError("Connected MCP has no supported equity quote tool.")
        quote_payload = await self._call(
            caller, quote_tool, self._symbol_arguments(quote_tool, symbol)
        )
        try:
            spot, previous_close = self._extract_quote(quote_payload, symbol)
        except ValueError:
            spot, previous_close = await self._index_quote(caller, symbol)
        snapshot_time = datetime.now(timezone.utc)

        community_chain = self._find_tool("robinhood_options_chain", "options_chain")
        records: list[dict[str, Any]] = []
        if community_chain:
            chain_payload = await self._call(
                caller, community_chain, self._symbol_arguments(community_chain, symbol)
            )
            records = self._contract_records(chain_payload)
            available_expirations = _active_expirations(
                {
                    expiration
                    for record in records
                    if (
                        expiration := _iso_date(
                            _deep_find(
                                record,
                                {"expiration", "expiration_date", "expiry", "expiry_date"},
                            )
                        )
                    )
                },
                snapshot_time,
            )
            requested_expirations = sorted(
                set(selected_expirations or available_expirations[:1])
            )
            records = [
                record
                for record in records
                if _iso_date(
                    _deep_find(
                        record,
                        {"expiration", "expiration_date", "expiry", "expiry_date"},
                    )
                )
                in requested_expirations
            ]
        else:
            chain_tool = self._find_tool("get_option_chains")
            instruments_tool = self._find_tool("get_option_instruments")
            quotes_tool = self._find_tool("get_option_quotes")
            if not instruments_tool:
                raise RuntimeError("Connected MCP has no supported option-instruments tool.")

            chain_payload: Any = {}
            if chain_tool:
                chain_payload = await self._call(
                    caller, chain_tool, self._symbol_arguments(chain_tool, symbol)
                )

            chains = _record_list(chain_payload, "chains")
            available_expirations = _active_expirations(
                {
                    expiration
                    for chain in chains
                    for value in (chain.get("expiration_dates") or [])
                    if (expiration := _iso_date(value))
                },
                snapshot_time,
            )
            requested_expirations = sorted(
                set(selected_expirations or available_expirations[:1])
            )
            cache_key = (symbol, tuple(requested_expirations))
            cached = self._instrument_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < INSTRUMENT_CACHE_SECONDS:
                instruments = cached[1]
            else:
                instruments = []
                instrument_properties = self._schema_properties(instruments_tool)
                if chains and "chain_id" in instrument_properties:
                    for chain in chains:
                        chain_id = chain.get("id")
                        chain_expirations = {
                            expiration
                            for value in (chain.get("expiration_dates") or [])
                            if (expiration := _iso_date(value))
                        }
                        matching_expirations = [
                            expiration
                            for expiration in requested_expirations
                            if expiration in chain_expirations
                        ]
                        if not chain_id or not matching_expirations:
                            continue
                        arguments = {"chain_id": str(chain_id)}
                        if "expiration_dates" in instrument_properties:
                            arguments["expiration_dates"] = ",".join(
                                expiration.isoformat() for expiration in matching_expirations
                            )
                        if "state" in instrument_properties:
                            arguments["state"] = "active"
                        instruments.extend(
                            await self._paginated_records(
                                caller,
                                instruments_tool,
                                arguments,
                                "instruments",
                            )
                        )
                else:
                    instruments = await self._paginated_records(
                        caller,
                        instruments_tool,
                        self._symbol_arguments(instruments_tool, symbol),
                        "instruments",
                    )

                deduplicated: dict[str, dict[str, Any]] = {}
                for item in instruments:
                    identifier = self._record_id(item)
                    if identifier:
                        deduplicated[identifier] = item
                instruments = list(deduplicated.values())
                self._instrument_cache[cache_key] = (time.monotonic(), instruments)

            quote_records: list[dict[str, Any]] = []
            if quotes_tool:
                ids = [identifier for item in instruments if (identifier := self._record_id(item))]
                for start in range(0, len(ids), QUOTE_BATCH_SIZE):
                    payload = await self._call(
                        caller,
                        quotes_tool,
                        self._id_arguments(
                            quotes_tool, ids[start : start + QUOTE_BATCH_SIZE]
                        ),
                    )
                    results = _record_list(payload, "results")
                    if not results:
                        raise ValueError(
                            "Robinhood MCP returned no option quotes for a requested batch."
                        )
                    quotes = [item.get("quote") for item in results]
                    quote_records.extend(item for item in quotes if isinstance(item, dict))
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
            as_of=snapshot_time,
            available_expirations=available_expirations,
            contracts=contracts,
            source="Robinhood Trading MCP",
        )

    async def snapshot(
        self, symbol: str, selected_expirations: list[date] | None = None
    ) -> MarketSnapshot:
        if not self.gateway.connected:
            raise RuntimeError("Robinhood MCP is not connected. Use Connect Robinhood first.")

        async def operation(client):
            return await self._snapshot(client.call_tool, symbol, selected_expirations)

        return await self.gateway.with_client(operation)

