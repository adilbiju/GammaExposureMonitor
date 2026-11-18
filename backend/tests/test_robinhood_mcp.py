import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.providers.robinhood_mcp import RobinhoodMcpProvider, _active_expirations


class FakeGateway:
    def __init__(self):
        self.connected = True
        self.tools = [
            "get_equity_quotes",
            "get_indexes",
            "get_index_quotes",
            "get_option_chains",
            "get_option_instruments",
            "get_option_quotes",
        ]
        self.tool_schemas = {
            "get_equity_quotes": {"properties": {"symbols": {"type": "array"}}},
            "get_indexes": {"properties": {"symbols": {"type": "string"}}},
            "get_index_quotes": {"properties": {"instrument_ids": {"type": "array"}}},
            "get_option_chains": {"properties": {"underlying_symbol": {"type": "string"}}},
            "get_option_instruments": {
                "properties": {
                    "chain_id": {"type": "string"},
                    "chain_symbol": {"type": "string"},
                    "expiration_dates": {"type": "string"},
                    "state": {"type": "string"},
                    "cursor": {"type": "string"},
                }
            },
            "get_option_quotes": {"properties": {"instrument_ids": {"type": "array"}}},
        }
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_equity_quotes":
            payload = {
                "data": {
                    "results": [
                        {
                            "quote": {
                                "symbol": "SPY",
                                "mark_price": "740.00",
                                "previous_close_price": "735.00",
                            }
                        }
                    ]
                }
            }
        elif name == "get_option_chains":
            payload = {
                "data": {
                    "chains": [
                        {
                            "id": "chain-1",
                            "symbol": "SPY",
                            "expiration_dates": ["2026-12-18"],
                        }
                    ]
                }
            }
        elif name == "get_option_instruments" and not arguments.get("cursor"):
            payload = {
                "data": {
                    "instruments": [
                        {
                            "id": "put-1",
                            "chain_id": "chain-1",
                            "chain_symbol": "SPY",
                            "expiration_date": "2026-12-18",
                            "strike_price": "700",
                            "type": "put",
                            "state": "active",
                            "trade_value_multiplier": "100",
                        }
                    ],
                    "next": "page-2",
                }
            }
        elif name == "get_option_instruments":
            payload = {
                "data": {
                    "instruments": [
                        {
                            "id": "call-1",
                            "chain_id": "chain-1",
                            "chain_symbol": "SPY",
                            "expiration_date": "2026-12-18",
                            "strike_price": "750",
                            "type": "call",
                            "state": "active",
                            "trade_value_multiplier": "50",
                        }
                    ],
                    "next": "",
                }
            }
        elif name == "get_option_quotes":
            payload = {
                "data": {
                    "results": [
                        {
                            "quote": {
                                "instrument_id": identifier,
                                "bid_price": "2.00",
                                "ask_price": "2.20",
                                "mark_price": "2.10",
                                "implied_volatility": "0.20",
                                "gamma": "0.01",
                                "open_interest": 10,
                                "volume": 3,
                            },
                            "close": {
                                "instrument_id": identifier,
                                "price": "1.90",
                            },
                        }
                        for identifier in arguments["instrument_ids"]
                    ]
                }
            }
        elif name == "get_indexes":
            payload = {
                "data": {
                    "indexes": [
                        {"id": "spx-index", "symbol": "SPX", "current_value": "6999.00"}
                    ]
                }
            }
        elif name == "get_index_quotes":
            payload = {
                "data": {
                    "quotes": [
                        {"instrument_id": "spx-index", "symbol": "SPX", "value": "7001.25"}
                    ]
                }
            }
        else:
            raise AssertionError(f"Unexpected tool call: {name} {arguments}")
        return SimpleNamespace(structuredContent=payload, content=[])

    async def with_client(self, operation):
        client = SimpleNamespace(call_tool=self.call_tool)
        return await operation(client)


class RobinhoodMcpProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = FakeGateway()
        self.provider = RobinhoodMcpProvider(self.gateway)

    async def test_paginates_instruments_and_merges_only_live_quote_records(self):
        snapshot = await self.provider.snapshot("SPY")

        self.assertEqual(snapshot.spot, 740)
        self.assertEqual(snapshot.previous_close, 735)
        self.assertEqual({contract.option_type for contract in snapshot.contracts}, {"call", "put"})
        self.assertEqual({contract.id for contract in snapshot.contracts}, {"call-1", "put-1"})
        self.assertTrue(all(contract.gamma == 0.01 for contract in snapshot.contracts))
        self.assertTrue(all(contract.open_interest == 10 for contract in snapshot.contracts))
        self.assertEqual(
            next(contract for contract in snapshot.contracts if contract.id == "call-1").contract_multiplier,
            50,
        )

        instrument_calls = [arguments for name, arguments in self.gateway.calls if name == "get_option_instruments"]
        self.assertEqual(
            instrument_calls[0],
            {
                "chain_id": "chain-1",
                "expiration_dates": "2026-12-18",
                "state": "active",
            },
        )
        self.assertEqual(
            instrument_calls[1],
            {
                "chain_id": "chain-1",
                "expiration_dates": "2026-12-18",
                "state": "active",
                "cursor": "page-2",
            },
        )

    async def test_index_quote_uses_index_id_and_live_value(self):
        spot, previous_close = await self.provider._index_quote(
            self.gateway.call_tool, "SPX"
        )

        self.assertEqual(spot, 7001.25)
        self.assertIsNone(previous_close)
        self.assertIn(("get_indexes", {"symbols": "SPX"}), self.gateway.calls)
        self.assertIn(
            ("get_index_quotes", {"instrument_ids": ["spx-index"]}),
            self.gateway.calls,
        )

    def test_instrument_symbol_argument_uses_chain_symbol(self):
        self.assertEqual(
            self.provider._symbol_arguments("get_option_instruments", "SPY"),
            {"chain_symbol": "SPY"},
        )

    def test_decimal_iv_above_three_is_not_divided_by_one_hundred(self):
        contract = self.provider._normalize_contract(
            {
                "id": "high-iv",
                "expiration_date": "2026-12-18",
                "strike_price": "600",
                "type": "put",
                "quote": {
                    "implied_volatility": "3.660396",
                    "gamma": "0.002",
                    "open_interest": 10,
                },
            },
            "SPY",
            0,
        )

        self.assertIsNotNone(contract)
        self.assertAlmostEqual(contract.implied_volatility, 3.660396)

    def test_explicit_percentage_iv_is_converted_to_decimal(self):
        contract = self.provider._normalize_contract(
            {
                "id": "percentage-iv",
                "expiration_date": "2026-12-18",
                "strike_price": "600",
                "type": "put",
                "quote": {
                    "implied_volatility_percentage": "366.0396",
                    "gamma": "0.002",
                    "open_interest": 10,
                },
            },
            "SPY",
            0,
        )

        self.assertIsNotNone(contract)
        self.assertAlmostEqual(contract.implied_volatility, 3.660396)

    def test_expired_same_day_contract_is_not_selected_by_default(self):
        now = datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc)

        self.assertEqual(
            _active_expirations(
                [date(2026, 11, 16), date(2026, 11, 17)],
                now,
            ),
            [date(2026, 11, 17)],
        )


if __name__ == "__main__":
    unittest.main()
