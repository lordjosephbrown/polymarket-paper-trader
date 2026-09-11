"""Tests for thetadesk.ledger: cash accounting, positions, settlement, stats, export."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from thetadesk import ledger as ledger_mod
from thetadesk.ledger import Ledger
from thetadesk.models import (
    STATUS_ASSIGNED,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    InsufficientBuyingPowerError,
    InvalidOrderError,
    NotInitializedError,
    PositionClosedError,
    PositionNotFoundError,
)

T0 = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)
T3 = T0 + timedelta(days=3)
EXPIRY = date(2026, 10, 16)
AFTER_EXPIRY = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_data_dir: Path):
    led = Ledger(tmp_data_dir)
    led.init_account(100_000.0, ts=T0)
    yield led
    led.close()


def sell(led: Ledger, **kw):
    args = dict(underlying="xyz", right="PUT", strike=90.0, expiry=EXPIRY, contracts=2, credit=1.0,
                collateral=9000.0, spot=100.0, delta=-0.2, iv=0.4, notes="thesis", ts=T0)
    args.update(kw)
    return led.sell_to_open(**args)


class TestAccount:
    def test_init_creates_db_and_account(self, tmp_data_dir: Path):
        led = Ledger(tmp_data_dir / "nested")
        assert not led.is_initialized()
        with pytest.raises(NotInitializedError):
            led.account()
        with pytest.raises(NotInitializedError):
            led.balance()
        acct = led.init_account(25_000, ts=T0)
        assert (led.data_dir / "paper.db").exists()
        assert led.is_initialized() and acct.cash == 25_000 and acct.created_at == T0.isoformat()
        assert led.account().starting_cash == 25_000
        led.close()

    @pytest.mark.parametrize("cash", [0, -5])
    def test_init_invalid_cash(self, tmp_data_dir: Path, cash):
        led = Ledger(tmp_data_dir)
        with pytest.raises(InvalidOrderError):
            led.init_account(cash)
        led.close()

    def test_reinit_wipes(self, ledger: Ledger):
        sell(ledger)
        ledger.init_account(5_000)
        assert ledger.positions() == [] and ledger.trades() == [] and ledger.account().cash == 5_000

    def test_reset(self, ledger: Ledger):
        sell(ledger)
        ledger.reset()
        assert not ledger.is_initialized() and ledger.positions() == []


class TestSellToOpen:
    def test_cash_collateral_and_journal(self, ledger: Ledger):
        p = sell(ledger)
        assert p.id == 1 and p.underlying == "XYZ" and p.right == "put" and p.expiry == EXPIRY
        assert p.status == STATUS_OPEN and p.contracts == 2 and p.credit == 1.0 and p.collateral == 9000.0
        assert (p.spot_at_open, p.delta_at_open, p.iv_at_open, p.notes) == (100.0, -0.2, 0.4, "thesis")
        assert p.opened_at == T0.isoformat() and p.rolled_from is None
        assert ledger.account().cash == pytest.approx(100_200.0)
        assert ledger.collateral_in_use() == 18_000.0
        assert ledger.buying_power() == pytest.approx(82_200.0)
        (t,) = ledger.trades()
        assert t.action == "sell_to_open" and t.price == 1.0 and t.contracts == 2
        assert t.cash_delta == 200.0 and t.ts == T0.isoformat() and t.note == "thesis" and t.position_id == 1

    def test_expiry_string_and_defaults(self, ledger: Ledger):
        p = ledger.sell_to_open("abc", "call", 110.0, "2026-10-16", 1, 0.55, 11_000.0)
        assert p.expiry == EXPIRY and p.spot_at_open is None and p.notes is None
        assert datetime.fromisoformat(p.opened_at).tzinfo is not None

    @pytest.mark.parametrize("kw", [dict(contracts=0), dict(credit=0.0), dict(strike=0.0), dict(collateral=0.0)])
    def test_validation(self, ledger: Ledger, kw):
        with pytest.raises(InvalidOrderError):
            sell(ledger, **kw)

    def test_insufficient_buying_power(self, ledger: Ledger):
        sell(ledger, contracts=9)  # 81,000 reserved
        with pytest.raises(InsufficientBuyingPowerError) as exc:
            sell(ledger, contracts=3)  # needs 27,000, only ~20,100 left
        assert exc.value.required == 27_000.0 and exc.value.available == pytest.approx(19_900.0)
        assert len(ledger.open_positions()) == 1

    def test_requires_account(self, tmp_data_dir: Path):
        led = Ledger(tmp_data_dir)
        with pytest.raises(NotInitializedError):
            sell(led)
        led.close()


class TestBuyToClose:
    def test_realized_and_cash(self, ledger: Ledger):
        p = sell(ledger)
        closed = ledger.buy_to_close(p.id, 0.4, reason="take_profit", ts=T3, note="tp")
        assert closed.status == STATUS_CLOSED and closed.close_price == 0.4 and closed.close_reason == "take_profit"
        assert closed.realized_pnl == pytest.approx(120.0) and closed.closed_at == T3.isoformat()
        assert ledger.account().cash == pytest.approx(100_120.0)
        assert ledger.collateral_in_use() == 0.0
        t = ledger.trades()[0]
        assert t.action == "buy_to_close" and t.cash_delta == -80.0 and t.note == "tp"

    def test_loss(self, ledger: Ledger):
        p = sell(ledger)
        closed = ledger.buy_to_close(p.id, 3.0)
        assert closed.realized_pnl == pytest.approx(-400.0) and closed.close_reason == "manual"

    def test_errors(self, ledger: Ledger):
        p = sell(ledger)
        with pytest.raises(InvalidOrderError):
            ledger.buy_to_close(p.id, -1.0)
        ledger.buy_to_close(p.id, 0.5)
        with pytest.raises(PositionClosedError):
            ledger.buy_to_close(p.id, 0.5)
        with pytest.raises(PositionNotFoundError):
            ledger.buy_to_close(999, 0.5)


class TestSettle:
    def test_not_expired_yet(self, ledger: Ledger):
        p = sell(ledger)
        with pytest.raises(InvalidOrderError, match="not expired"):
            ledger.settle(p.id, 95.0, ts=T3)

    def test_expire_worthless(self, ledger: Ledger):
        p = sell(ledger)
        s = ledger.settle(p.id, 95.0, ts=AFTER_EXPIRY)
        assert s.status == STATUS_EXPIRED and s.close_price == 0.0 and s.close_reason == "expired_worthless"
        assert s.realized_pnl == pytest.approx(200.0)
        assert ledger.account().cash == pytest.approx(100_200.0)
        t = ledger.trades()[0]
        assert t.action == "expire" and t.cash_delta == 0.0

    def test_assignment_cash_settled(self, ledger: Ledger):
        p = sell(ledger)
        s = ledger.settle(p.id, 85.0, ts=AFTER_EXPIRY)
        assert s.status == STATUS_ASSIGNED and s.close_price == 5.0 and s.close_reason == "assigned"
        assert s.realized_pnl == pytest.approx(-800.0)
        assert ledger.account().cash == pytest.approx(100_200.0 - 1000.0)
        t = ledger.trades()[0]
        assert t.action == "assign" and t.cash_delta == -1000.0 and "spot 85" in t.note

    def test_today_override(self, ledger: Ledger):
        p = sell(ledger)
        s = ledger.settle(p.id, 95.0, ts=T3, today=EXPIRY)
        assert s.status == STATUS_EXPIRED and s.closed_at == T3.isoformat()

    def test_call_assignment(self, ledger: Ledger):
        p = sell(ledger, right="call", strike=110.0, collateral=11_000.0)
        s = ledger.settle(p.id, 120.0, ts=AFTER_EXPIRY)
        assert s.status == STATUS_ASSIGNED and s.close_price == 10.0


class TestRoll:
    def test_roll_out(self, ledger: Ledger):
        p = sell(ledger)
        closed, opened = ledger.roll(p.id, 1.5, 90.0, "2026-11-20", 2.2, 9000.0, spot=95.0, delta=-0.3, iv=0.5, ts=T3)
        assert closed.status == STATUS_CLOSED and closed.close_reason == "roll" and closed.realized_pnl == pytest.approx(-100.0)
        assert opened.id == 2 and opened.rolled_from == 1 and opened.contracts == 2 and opened.credit == 2.2
        assert opened.expiry == date(2026, 11, 20) and opened.notes == "rolled from #1"
        assert opened.opened_at == T3.isoformat() and opened.spot_at_open == 95.0
        assert ledger.account().cash == pytest.approx(100_000 + 200 - 300 + 440)
        assert ledger.collateral_in_use() == 18_000.0
        actions = [t.action for t in ledger.trades()]
        assert actions == ["sell_to_open", "buy_to_close", "sell_to_open"]

    def test_roll_custom_notes(self, ledger: Ledger):
        p = sell(ledger)
        _, opened = ledger.roll(p.id, 1.5, 85.0, "2026-11-20", 2.2, 8500.0, notes="down and out")
        assert opened.notes == "down and out" and opened.strike == 85.0

    @pytest.mark.parametrize("kw", [dict(close_price=-1.0), dict(new_credit=0.0), dict(new_collateral=0.0)])
    def test_validation(self, ledger: Ledger, kw):
        p = sell(ledger)
        args = dict(position_id=p.id, close_price=1.0, new_strike=90.0, new_expiry="2026-11-20", new_credit=1.0, new_collateral=9000.0)
        args.update(kw)
        with pytest.raises(InvalidOrderError):
            ledger.roll(**args)

    def test_insufficient_buying_power_is_atomic(self, ledger: Ledger):
        p = sell(ledger)
        with pytest.raises(InsufficientBuyingPowerError):
            ledger.roll(p.id, 1.0, 90.0, "2026-11-20", 1.0, 60_000.0)
        assert ledger.get_position(p.id).status == STATUS_OPEN
        assert ledger.account().cash == pytest.approx(100_200.0)

    def test_roll_closed_position(self, ledger: Ledger):
        p = sell(ledger)
        ledger.buy_to_close(p.id, 0.5)
        with pytest.raises(PositionClosedError):
            ledger.roll(p.id, 1.0, 90.0, "2026-11-20", 1.0, 9000.0)


class TestQueries:
    def test_positions_filters_and_order(self, ledger: Ledger):
        a = sell(ledger)
        b = sell(ledger, underlying="abc", right="call", strike=110.0, collateral=11_000.0)
        ledger.buy_to_close(a.id, 0.5, ts=T3)
        assert [p.id for p in ledger.positions()] == [b.id, a.id]
        assert [p.id for p in ledger.positions(STATUS_OPEN)] == [b.id]
        assert [p.id for p in ledger.positions(underlying="xyz")] == [a.id]
        assert [p.id for p in ledger.positions(STATUS_CLOSED, "ABC")] == []
        assert [p.id for p in ledger.open_positions()] == [b.id]
        assert [p.id for p in ledger.closed_positions()] == [a.id]
        with pytest.raises(PositionNotFoundError):
            ledger.get_position(42)

    def test_trades_limit_and_filter(self, ledger: Ledger):
        a = sell(ledger)
        b = sell(ledger, underlying="abc")
        ledger.buy_to_close(a.id, 0.5)
        assert len(ledger.trades(limit=2)) == 2
        assert [t.position_id for t in ledger.trades(position_id=a.id)] == [a.id, a.id]
        assert [t.action for t in ledger.trades(position_id=b.id)] == ["sell_to_open"]

    def test_balance(self, ledger: Ledger):
        p = sell(ledger)
        bal = ledger.balance()
        assert bal["cash"] == pytest.approx(100_200.0) and bal["starting_cash"] == 100_000.0
        assert bal["collateral_in_use"] == 18_000.0 and bal["buying_power"] == pytest.approx(82_200.0)
        assert bal["open_positions"] == 1 and bal["premium_at_risk"] == 200.0
        assert bal["realized_pnl"] == 0.0 and bal["net_liq_if_closed_at_entry"] == pytest.approx(100_000.0)
        ledger.buy_to_close(p.id, 0.5)
        bal = ledger.balance()
        assert bal["realized_pnl"] == pytest.approx(100.0) and bal["return_pct"] == pytest.approx(0.001)
        assert bal["open_positions"] == 0 and bal["collateral_in_use"] == 0


class TestStats:
    def test_empty(self, ledger: Ledger):
        s = ledger.stats()
        assert s["closed_trades"] == 0 and s["win_rate"] == 0.0 and s["profit_factor"] is None
        assert s["max_drawdown"] == 0.0 and s["avg_days_held"] == 0.0 and s["by_underlying"] == {}
        assert s["largest_win"] == 0.0 and s["largest_loss"] == 0.0 and s["return_pct"] == 0.0

    def test_mixed_results(self, ledger: Ledger):
        a = sell(ledger)                                   # +200 when expired
        b = sell(ledger, underlying="abc")                 # -800 when closed at 5.0
        c = sell(ledger, right="call", strike=110.0, collateral=11_000.0, credit=1.5)  # +300
        d = sell(ledger, underlying="open")                # stays open
        ledger.settle(a.id, 95.0, ts=T3, today=EXPIRY)
        ledger.buy_to_close(b.id, 5.0, reason="stop_loss", ts=T3)
        ledger.buy_to_close(c.id, 0.0, reason="take_profit", ts=T3)
        s = ledger.stats()
        assert s["closed_trades"] == 3 and s["open_trades"] == 1
        assert s["wins"] == 2 and s["losses"] == 1 and s["win_rate"] == pytest.approx(2 / 3)
        assert s["realized_pnl"] == pytest.approx(-300.0) and s["return_pct"] == pytest.approx(-0.003)
        assert s["avg_pnl"] == pytest.approx(-100.0) and s["avg_credit"] == pytest.approx((1.0 + 1.0 + 1.5) / 3)
        assert s["avg_days_held"] == pytest.approx(3.0)
        assert s["profit_factor"] == pytest.approx(500 / 800)
        assert s["largest_win"] == 300.0 and s["largest_loss"] == -800.0
        assert s["max_drawdown"] == pytest.approx(800.0)
        assert s["premium_collected"] == pytest.approx(200 + 200 + 300 + 200)
        assert s["collateral_in_use"] == 18_000.0 and s["cash"] == pytest.approx(100_000 - 300 + 200)
        assert s["by_underlying"]["XYZ"] == {"trades": 2, "wins": 2, "win_rate": 1.0, "realized_pnl": 500.0, "avg_pnl": 250.0}
        assert s["by_underlying"]["ABC"]["realized_pnl"] == -800.0
        assert s["by_strategy"]["short_put"]["trades"] == 2 and s["by_strategy"]["short_call"]["wins"] == 1
        assert s["by_close_reason"] == {"expired_worthless": 1, "stop_loss": 1, "take_profit": 1}


class TestExport:
    def test_json_and_csv(self, ledger: Ledger):
        p = sell(ledger)
        ledger.buy_to_close(p.id, 0.5)
        trades = json.loads(ledger.export("trades", "json"))
        assert [t["action"] for t in trades] == ["buy_to_close", "sell_to_open"]
        positions = json.loads(ledger.export("positions", "json"))
        assert positions[0]["expiry"] == "2026-10-16" and positions[0]["status"] == "closed"
        rows = list(csv.DictReader(io.StringIO(ledger.export("trades", "csv"))))
        assert len(rows) == 2 and rows[0]["action"] == "buy_to_close"

    def test_csv_empty(self, ledger: Ledger):
        assert ledger.export("trades", "csv") == ""

    def test_invalid(self, ledger: Ledger):
        with pytest.raises(InvalidOrderError):
            ledger.export("orders", "json")
        with pytest.raises(InvalidOrderError):
            ledger.export("trades", "xml")


def test_days_between():
    assert ledger_mod._days_between(T0.isoformat(), T3.isoformat()) == pytest.approx(3.0)
