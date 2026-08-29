"""Symbol-aware distance conversions (no hardcoded gold pip)."""
from harness import Suite, use_stub_mt5
use_stub_mt5()

from types import SimpleNamespace
from price_utils import SymbolSpec, grid_index, grid_price

t = Suite("price_utils")


def spec(digits=2, point=0.01, **kw):
    d = dict(name="XAUUSD", digits=digits, point=point, tick_size=point,
             tick_value=1.0, contract_size=100.0, volume_min=0.01,
             volume_max=200.0, volume_step=0.01)
    d.update(kw)
    return SymbolSpec(**d)


t.section("PIP DERIVATION")
t.check("2-digit gold: 1 pip = 1 point", spec(2, 0.01).points_per_pip == 1)
t.check("3-digit gold: 1 pip = 10 points", spec(3, 0.001).points_per_pip == 10)
t.check("5-digit FX: 1 pip = 0.0001",
        abs(spec(5, 0.00001).pip_size - 0.0001) < 1e-12)
t.check("4-digit FX: 1 pip = 0.0001",
        abs(spec(4, 0.0001).pip_size - 0.0001) < 1e-12)
t.check("operator override wins",
        spec(2, 0.01, pip_points_override=10).pip_size == 0.1)
t.check("pips -> price", abs(spec(2, 0.01, pip_points_override=10)
                             .pips_to_price(3) - 0.30) < 1e-12)
t.check("price -> pips", abs(spec(2, 0.01, pip_points_override=10)
                             .price_to_pips(0.30) - 3.0) < 1e-12)

t.section("FROM MT5 SYMBOL INFO")
info = SimpleNamespace(name="XAUUSD.ecn", digits=2, point=0.01,
                       trade_tick_size=0.01, trade_tick_value=1.0,
                       trade_contract_size=100.0, volume_min=0.01,
                       volume_max=50.0, volume_step=0.01, trade_stops_level=30,
                       trade_freeze_level=0)
s = SymbolSpec.from_mt5(info)
t.check("reads digits/point", (s.digits, s.point) == (2, 0.01))
t.check("min stop distance from stops_level",
        abs(s.min_stop_distance - 0.30) < 1e-12, str(s.min_stop_distance))
t.check("describe() is printable", "XAUUSD.ecn" in s.describe())

t.section("NORMALIZATION")
t.check("price snapped to digits", spec().normalize_price(4010.3049) == 4010.30)
t.check("volume below min clamped", spec().normalize_volume(0.001) == 0.01)
t.check("volume above max clamped", spec().normalize_volume(999) == 200.0)
t.check("volume snapped to step", spec().normalize_volume(0.037) == 0.04)
t.check("no float dust",
        repr(spec(volume_step=0.1).normalize_volume(0.3)) == "0.3")

t.section("MONEY")
s = spec()
t.check("0.01 lot: 1.00 move = $1.00",
        abs(s.money_per_price_unit(0.01) - 1.0) < 1e-9)
t.check("BUY profit", abs(s.profit("BUY", 4010.30, 4010.60, 0.01) - 0.30) < 1e-9)
t.check("SELL profit", abs(s.profit("SELL", 4010.30, 4010.00, 0.01) - 0.30) < 1e-9)
t.check("losing SELL", abs(s.profit("SELL", 4009.76, 4010.53, 0.01) + 0.77) < 1e-9)
t.check("contract-size fallback when tick data missing",
        abs(spec(tick_size=0, tick_value=0).money_per_price_unit(0.01) - 1.0) < 1e-9)

t.section("GRID")
t.check("index of an exact level", grid_index(4010.90, 4010.00, 0.30) == 3)
t.check("index rounds to nearest", grid_index(4010.94, 4010.00, 0.30) == 3)
t.check("negative index below anchor", grid_index(4009.40, 4010.00, 0.30) == -2)
t.check("grid price round trip",
        abs(grid_price(4010.0, 0.30, 3) - 4010.90) < 1e-9)
t.check("ladder matches the reference recording",
        [round(grid_price(4010.0, 0.30, i), 2) for i in range(1, 6)] ==
        [4010.30, 4010.60, 4010.90, 4011.20, 4011.50])
t.check("sell side matches the reference recording",
        [round(grid_price(4010.0, 0.30, -i), 2) for i in range(1, 6)] ==
        [4009.70, 4009.40, 4009.10, 4008.80, 4008.50])

t.done()
