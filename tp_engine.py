"""
TP / RR execution engine.

This layer runs AFTER the (unmodified) signal detection. It takes the signal
the strategy produced - direction and stop loss - and decides:

    * where the take profit goes (pip target or the classic RR multiple)
    * what the resulting reward/risk ratio is
    * whether that RR clears the configured minimum

Nothing here touches sweep detection, candle/wick logic, M5 bias, candle range
or spread filtering. A rejected plan means the signal was valid but the chosen
TP/risk configuration was unsuitable.
"""

# Reward/risk comparisons use a tolerance so 1.0R vs 0.9999999R is not a reject
RR_EPSILON = 1e-9


def get_pip_size(symbol_info, pip_points=0):
    """
    Pip size in price units for this symbol, derived from MT5 symbol info.

    Nothing about gold is hardcoded: brokers quote XAUUSD with different digit
    counts, so the pip is resolved from `point`/`digits`.

      * pip_points > 0 -> the operator pinned "1 pip = N points" in settings
      * otherwise      -> fractional-pricing rule: a 3- or 5-digit feed quotes
                          tenths of a pip, so 1 pip = 10 points; on a 2- or
                          4-digit feed 1 pip = 1 point.

    Returns (pip_size_in_price, points_per_pip).
    """
    point = getattr(symbol_info, "point", 0.0) or 0.0
    digits = int(getattr(symbol_info, "digits", 0) or 0)
    if point <= 0:
        return 0.0, 0

    if pip_points and pip_points > 0:
        return point * pip_points, int(pip_points)

    points_per_pip = 10 if digits in (3, 5) else 1
    return point * points_per_pip, points_per_pip


def normalize_volume(symbol_info, volume):
    """Clamp to volume_min/volume_max and snap to volume_step."""
    try:
        vol_min = float(symbol_info.volume_min)
        vol_max = float(symbol_info.volume_max)
        step = float(symbol_info.volume_step) or 0.01
    except (AttributeError, TypeError, ValueError):
        return volume

    vol = max(vol_min, min(float(volume), vol_max))
    vol = round(vol / step) * step
    vol = max(vol_min, min(vol, vol_max))
    # kill binary dust (0.30000000000000004 -> 0.3)
    decimals = max(0, len(f"{step:.8f}".rstrip("0").split(".")[1]))
    return round(vol, decimals)


def tp_mode_pips(tp_mode):
    """Target distance in pips for a pip mode, or None for custom RR."""
    table = {"1_pip": 1.0, "2_pips": 2.0, "3_pips": 3.0, "4_pips": 4.0,
             "5_pips": 5.0}
    return table.get(tp_mode)


def build_trade_plan(signal, symbol_info, settings, is_buy, signal_rr=None):
    """
    Turn a detected signal into an executable plan.

    `signal` is the untouched dict from build_signal (type/sl/tp/sl_points).
    `signal_rr` is the RR the signal itself used; when the configured custom RR
    matches it, the signal's own TP is reused verbatim so the classic behaviour
    is bit-for-bit identical.

    Returns a dict describing the plan, including accepted / reject_reason.
    """
    point = float(symbol_info.point)
    pip_size, points_per_pip = get_pip_size(symbol_info, settings.get("pip_points", 0))

    direction = "BUY" if is_buy else "SELL"
    sign = 1.0 if is_buy else -1.0

    # ---- risk leg: the strategy's own SL, unless the operator picked FIXED --
    sl_distance = float(signal["sl_points"]) * point
    entry = float(signal["sl"]) + sign * sl_distance
    sl_price = float(signal["sl"])

    if settings.get("sl_mode") == "fixed":
        sl_distance = float(settings["sl_fixed_points"]) * point
        sl_price = entry - sign * sl_distance

    plan = {
        "direction": direction,
        "entry": entry,
        "sl": sl_price,
        "tp": None,
        "sl_distance": sl_distance,
        "tp_distance": 0.0,
        "sl_points": sl_distance / point if point else 0.0,
        "tp_points": 0.0,
        "sl_pips": sl_distance / pip_size if pip_size else 0.0,
        "tp_pips": 0.0,
        "rr": 0.0,
        "tp_mode": settings.get("tp_mode", "custom_rr"),
        "custom_rr": float(settings.get("custom_rr", 3.0)),
        "min_rr": float(settings.get("min_rr", 0.0)),
        "pip_size": pip_size,
        "digits": int(getattr(symbol_info, "digits", 2) or 2),
        "points_per_pip": points_per_pip,
        "accepted": False,
        "reject_reason": "",
    }

    if sl_distance <= 0:
        plan["reject_reason"] = "invalid SL distance"
        return plan

    # ---- reward leg -------------------------------------------------------
    pips = tp_mode_pips(plan["tp_mode"])
    if pips is not None:
        if pip_size <= 0:
            plan["reject_reason"] = "pip size unavailable for this symbol"
            return plan
        tp_distance = pips * pip_size
        tp_price = entry + sign * tp_distance
        plan["tp_pips"] = pips
    else:
        rr = plan["custom_rr"]
        tp_distance = rr * sl_distance
        # Reuse the signal's own TP when the RR matches, so the default
        # configuration reproduces the original number exactly.
        if signal_rr is not None and abs(rr - float(signal_rr)) < 1e-12 \
                and settings.get("sl_mode") != "fixed":
            tp_price = float(signal["tp"])
            tp_distance = abs(tp_price - entry)
        else:
            tp_price = entry + sign * tp_distance
        plan["tp_pips"] = tp_distance / pip_size if pip_size else 0.0

    plan["tp"] = tp_price
    plan["tp_distance"] = tp_distance
    plan["tp_points"] = tp_distance / point if point else 0.0
    plan["rr"] = tp_distance / sl_distance

    # ---- RR validation ----------------------------------------------------
    min_rr = plan["min_rr"]
    if plan["rr"] + RR_EPSILON < min_rr:
        plan["reject_reason"] = (
            f"RR {plan['rr']:.2f} below minimum {min_rr:.2f}R"
        )
        return plan

    plan["accepted"] = True
    return plan


def plan_report(plan, symbol, mode_label):
    """Multi-line console/Telegram summary of a plan (accepted or rejected)."""
    digits = plan.get("digits", 2)
    head = "SIGNAL" if plan["accepted"] else "TRADE REJECTED"
    lines = [
        f"{head}: {plan['direction']} {symbol}",
        "",
        f"Entry: {plan['entry']:.{digits}f}",
        f"SL: {plan['sl']:.{digits}f}",
        f"TP: {plan['tp']:.{digits}f}" if plan["tp"] is not None else "TP: n/a",
        "",
        f"TP Mode: {mode_label}",
        f"SL Distance: {plan['sl_pips']:.2f} pips ({plan['sl_points']:.0f} pts)",
        f"TP Distance: {plan['tp_pips']:.2f} pips ({plan['tp_points']:.0f} pts)",
        f"RR: {plan['rr']:.2f}",
    ]
    if not plan["accepted"]:
        lines += [f"Minimum: {plan['min_rr']:.2f}R",
                  f"Reason: {plan['reject_reason']}"]
    return "\n".join(lines)
