import unittest
from unittest.mock import patch

from trader import KrakenTrader


class _FakeExchange:
    def __init__(self, *, price=100.0, min_amount=None, amount_precision=0.0001):
        self.price = price
        self.min_amount = min_amount
        self.amount_precision = amount_precision
        self.events = []
        self.order_args = None
        self.stop_order_id = "sl-old"

    def load_markets(self):
        return None

    def market(self, symbol):
        return {
            "contractSize": 1.0,
            "precision": {"amount": self.amount_precision, "price": 0.1},
            "limits": {"amount": {"min": self.min_amount}},
        }

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def amount_to_precision(self, symbol, amount):
        steps = int(amount / self.amount_precision)
        return str(steps * self.amount_precision)

    def fetch_open_orders(self, symbol):
        return [{
            "id": self.stop_order_id,
            "reduceOnly": True,
            "type": "stop",
        }]

    def cancel_order(self, order_id, symbol):
        self.events.append(f"cancel:{order_id}")
        self.stop_order_id = None

    def create_order(self, *args):
        self.order_args = args
        if getattr(self, "next_order_fails", False):
            self.next_order_fails = False
            raise RuntimeError("rate limited")
        self.events.append("stop")
        return {"id": "sl-new"}

    def fetch_positions(self, symbols):
        return [{"contracts": 2.0, "side": "long"}]

    def create_market_sell_order(self, symbol, amount, params=None):
        self.events.append("market")
        return {"id": "close", "average": self.price, "cost": amount * self.price}


class TraderSafetyTests(unittest.TestCase):
    def test_missing_minimum_uses_amount_precision_not_one_contract(self):
        trader = KrakenTrader()
        trader._exchange = _FakeExchange(price=67_000.0, min_amount=None, amount_precision=0.0001)

        contracts = trader.get_size_contracts("BTC/USD:USD", 35.0)

        self.assertGreater(contracts, 0)
        self.assertLess(contracts, 1)

    def test_explicit_exchange_minimum_is_enforced(self):
        trader = KrakenTrader()
        trader._exchange = _FakeExchange(price=10_000.0, min_amount=0.01, amount_precision=0.001)

        self.assertEqual(trader.get_size_contracts("X/USD:USD", 35.0), 0.0)

    def test_stop_order_has_no_limit_price_and_uses_mark_trigger(self):
        exchange = _FakeExchange()
        trader = KrakenTrader()
        trader._exchange = exchange

        with patch("trader.KRAKEN_KEY", "key"):
            success = trader.set_stop_loss("X/USD:USD", "long", 99.0, amount=2.0)

        self.assertTrue(success)
        symbol, order_type, side, amount, price, params = exchange.order_args
        self.assertEqual(order_type, "stop")
        self.assertIsNone(price)
        self.assertEqual(params["stopPrice"], 99.0)
        self.assertEqual(params["triggerSignal"], "mark")
        self.assertTrue(params["reduceOnly"])

    def test_partial_close_keeps_old_stop_until_market_close_succeeds(self):
        exchange = _FakeExchange()
        trader = KrakenTrader()
        trader._exchange = exchange

        with patch("trader.KRAKEN_KEY", "key"):
            result = trader.close_position("X/USD:USD", "long", close_pct=0.5)

        self.assertTrue(result.success)
        self.assertEqual(exchange.events, ["market"])

    def test_stop_replacement_cancels_old_only_after_new_submitted(self):
        exchange = _FakeExchange()
        trader = KrakenTrader()
        trader._exchange = exchange

        with patch("trader.KRAKEN_KEY", "key"):
            success = trader.set_stop_loss("X/USD:USD", "long", 99.0, amount=2.0)

        self.assertTrue(success)
        self.assertEqual(exchange.events, ["stop", "cancel:sl-old"])
        self.assertIsNone(exchange.stop_order_id)

    def test_failed_stop_submit_keeps_old_stop_intact(self):
        exchange = _FakeExchange()
        exchange.next_order_fails = True
        trader = KrakenTrader()
        trader._exchange = exchange

        with patch("trader.KRAKEN_KEY", "key"):
            success = trader.set_stop_loss("X/USD:USD", "long", 99.0, amount=2.0)

        self.assertFalse(success)
        self.assertEqual(exchange.events, [])
        self.assertEqual(exchange.stop_order_id, "sl-old")


if __name__ == "__main__":
    unittest.main()
