"""
Symbol-aware price/distance conversions.

Nothing about XAUUSD is hardcoded. Every distance the strategy uses - ladder
spacing, take profit, stop distance, spread limits - is expressed in price
units and converted through the broker's own symbol properties (digits, point,
volume step, minimum stop distance).
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSpec:
    """Immutable snapshot of everything the engine needs about a symbol."""

    name: str
    digits: int = 2
    point: float = 0.01
    tick_size: float = 0.01
    tick_value: float = 1.0
    contract_size: float = 100.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    stops_level_points: int = 0        # broker minimum distance for stop orders
    freeze_level_points: int = 0
    pip_points_override: int = 0       # 0 = derive from digits

    # ------------------------------------------------------------- factories
    @classmethod
    def from_mt5(cls, info, pip_points_override=0):
        """Build from a MetaTrader5 SymbolInfo object."""
        point = float(getattr(info, "point", 0.01)) or 0.01
        return cls(
            name=getattr(info, "name", "?"),
            digits=int(getattr(info, "digits", 2) or 2),
            point=point,
            tick_size=float(getattr(info, "trade_tick_size", 0) or point),
            tick_value=float(getattr(info, "trade_tick_value", 0) or 1.0),
            contract_size=float(getattr(info, "trade_contract_size", 0) or 100.0),
            volume_min=float(getattr(info, "volume_min", 0.01) or 0.01),
            volume_max=float(getattr(info, "volume_max", 100.0) or 100.0),
            volume_step=float(getattr(info, "volume_step", 0.01) or 0.01),
            stops_level_points=int(getattr(info, "trade_stops_level", 0) or 0),
            freeze_level_points=int(getattr(info, "trade_freeze_level", 0) or 0),
            pip_points_override=int(pip_points_override or 0),
        )

    def with_pip_override(self, pip_points):
        return SymbolSpec(**{**self.__dict__, "pip_points_override": int(pip_points or 0)})

    # ------------------------------------------------------------ pip / point
    @property
    def points_per_pip(self):
        """
        Points in one pip, derived from the quote precision.

        A 3- or 5-digit feed quotes tenths of a pip (1 pip = 10 points); a 2- or
        4-digit feed quotes whole pips. An operator override always wins, because
        gold conventions differ between brokers.
        """
        if self.pip_points_override > 0:
            return self.pip_points_override
        return 10 if self.digits in (3, 5) else 1

    @property
    def pip_size(self):
        """One pip in price units."""
        return self.point * self.points_per_pip

    def pips_to_price(self, pips):
        return float(pips) * self.pip_size

    def price_to_pips(self, distance):
        return float(distance) / self.pip_size if self.pip_size else 0.0

    def points_to_price(self, points):
        return float(points) * self.point

    def price_to_points(self, distance):
        return float(distance) / self.point if self.point else 0.0

    # -------------------------------------------------------------- rounding
    def normalize_price(self, price):
        """Round to the symbol's tick grid and digit count."""
        price = float(price)
        if self.tick_size > 0:
            price = round(price / self.tick_size) * self.tick_size
        return round(price, self.digits)

    def normalize_volume(self, volume):
        """Clamp to volume_min/max and snap to volume_step."""
        step = self.volume_step or 0.01
        vol = max(self.volume_min, min(float(volume), self.volume_max))
        vol = round(vol / step) * step
        vol = max(self.volume_min, min(vol, self.volume_max))
        decimals = max(0, len(f"{step:.8f}".rstrip('0').split('.')[1]))
        return round(vol, decimals)

    # ------------------------------------------------------------- distances
    @property
    def min_stop_distance(self):
        """Closest a pending stop order may sit to the market, in price units."""
        return self.points_to_price(self.stops_level_points)

    def money_per_price_unit(self, volume):
        """
        Account currency gained per 1.0 of price movement on `volume` lots.

        Uses tick value/size when the broker provides them, and falls back to
        the contract size otherwise.
        """
        if self.tick_size > 0 and self.tick_value > 0:
            return (self.tick_value / self.tick_size) * float(volume)
        return self.contract_size * float(volume)

    def profit(self, side, entry, exit_price, volume):
        """Gross profit in account currency for a closed position."""
        direction = 1.0 if str(side).upper().startswith("B") else -1.0
        return direction * (float(exit_price) - float(entry)) * \
            self.money_per_price_unit(volume)

    def describe(self):
        return (f"{self.name}: digits={self.digits} point={self.point} "
                f"1 pip={self.pip_size:g} ({self.points_per_pip} pts) "
                f"min stop={self.min_stop_distance:g} "
                f"lots {self.volume_min}-{self.volume_max} step {self.volume_step}")


def grid_index(price, anchor, spacing):
    """Index of the grid slot nearest to `price` for a grid anchored at `anchor`."""
    if spacing <= 0:
        return 0
    return int(math.floor((float(price) - float(anchor)) / float(spacing) + 0.5))


def grid_price(anchor, spacing, index):
    return float(anchor) + int(index) * float(spacing)
