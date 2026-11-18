import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.calculations.gex import calculate_gex, contract_gex
from app.models import MarketSnapshot, OptionContract


class GexCalculationTests(unittest.TestCase):
    def contract(self, option_type: str, strike: float, gamma: float = 0.01, oi: int = 100):
        return OptionContract(
            id=f"{option_type}-{strike}",
            symbol="SPY",
            expiration=date(2027, 1, 15),
            strike=strike,
            option_type=option_type,
            gamma=gamma,
            implied_volatility=0.2,
            open_interest=oi,
        )

    def test_call_and_put_signs(self):
        call = self.contract("call", 600)
        put = self.contract("put", 600)
        expected = 0.01 * 100 * 100 * 600 * 600 * 0.01
        self.assertAlmostEqual(contract_gex(call, 600), expected)
        self.assertAlmostEqual(contract_gex(put, 600), -expected)

    def test_aggregation_and_walls(self):
        snapshot = MarketSnapshot(
            symbol="SPY",
            spot=600,
            previous_close=594,
            as_of=datetime(2026, 11, 14, tzinfo=timezone.utc),
            source="test",
            contracts=[
                self.contract("call", 605, oi=500),
                self.contract("call", 610, oi=100),
                self.contract("put", 590, oi=700),
                self.contract("put", 585, oi=100),
            ],
        )
        result = calculate_gex(snapshot)
        self.assertEqual(result.call_wall, 605)
        self.assertEqual(result.put_wall, 590)
        self.assertAlmostEqual(result.price_change_pct, (600 / 594 - 1) * 100)
        self.assertEqual(len(result.strike_exposure), 4)
        self.assertEqual(len(result.gamma_profile), 61)

    def test_material_gamma_outlier_is_excluded_and_impact_is_reported(self):
        suspect = self.contract("put", 600, gamma=20, oi=1_098)
        snapshot = MarketSnapshot(
            symbol="SPY",
            spot=600,
            previous_close=594,
            as_of=datetime(2026, 11, 14, tzinfo=timezone.utc),
            source="test",
            contracts=[
                suspect,
                self.contract("put", 590, gamma=0.01, oi=700),
                self.contract("call", 605, gamma=0.01, oi=500),
            ],
        )

        result = calculate_gex(snapshot)
        flagged = next(contract for contract in result.contracts if contract.id == suspect.id)

        self.assertTrue(flagged.excluded_from_aggregates)
        self.assertEqual(flagged.gex, 0)
        self.assertLess(flagged.reported_gex, -1_000_000_000)
        self.assertEqual(result.data_quality.excluded_contract_count, 1)
        self.assertEqual(result.data_quality.raw_put_wall, 600)
        self.assertEqual(result.put_wall, 590)
        self.assertEqual(result.data_quality.raw_largest_gex_strike, 600)
        self.assertNotEqual(result.largest_gex_strike, 600)


if __name__ == "__main__":
    unittest.main()
