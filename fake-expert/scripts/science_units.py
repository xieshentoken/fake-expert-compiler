#!/usr/bin/env python3
"""Small explicit science-unit mappings, separate from the frozen legacy table."""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation, localcontext
import re

import science_contract as sc
from compiler_version import SCIENCE_UNITS_PROTOCOL

# Each mapping is (coherent unit, scale, offset). Absolute temperature alone has
# an offset. Quantity kinds are never inferred from matching dimensions or glyphs.
UNITS = {
    "length": ("m", {"m": "1", "mm": ".001", "cm": ".01", "km": "1000"}),
    "area": ("m^2", {"m^2": "1", "mm^2": ".000001", "cm^2": ".0001"}),
    "time": ("s", {"s": "1", "ms": ".001", "min": "60", "h": "3600"}),
    "pressure": ("Pa", {"Pa": "1", "kPa": "1000", "MPa": "1000000"}),
    "temperature": ("K", {"K": "1", "degC": "1"}),
    "temperature-difference": ("K", {"K": "1", "delta_K": "1", "delta_degC": "1"}),
    "thermal-conductivity": ("W/m/K", {"W/m/K": "1"}),
    "thermal-resistance": ("K/W", {"K/W": "1"}),
    "power": ("W", {"W": "1", "mW": ".001"}),
    "speed": ("m/s", {"m/s": "1", "cm/s": ".01"}),
    "extinction-coefficient": ("1", {"1": "1"}),
    "mass-fraction": ("1", {"1": "1", "%": ".01", "mass%": ".01"}),
    "volume-fraction": ("1", {"1": "1", "%": ".01", "vol%": ".01"}),
    "mole-fraction": ("1", {"1": "1", "%": ".01", "mol%": ".01"}),
}
SPELLINGS = {"mm²": "mm^2", "cm²": "cm^2", "m²": "m^2", "°C": "degC", "Δ°C": "delta_degC",
             "ΔK": "delta_K", "W/(m·K)": "W/m/K", "W/(m*K)": "W/m/K"}


def decimal_number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("science_number_invalid")
    if isinstance(value, int) and abs(value) > 10**100:
        raise ValueError("science_number_out_of_bounds")
    text = str(value)
    if len(text) > 128 or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?", text) is None:
        raise ValueError("science_number_invalid")
    try:
        result = Decimal(text)
    except InvalidOperation as error:
        raise ValueError("science_number_invalid") from error
    if not result.is_finite() or result.copy_abs() > Decimal("1e100") or (result and result.copy_abs() < Decimal("1e-100")):
        raise ValueError("science_number_out_of_bounds")
    return result


def decimal_text(value):
    value = decimal_number(value)
    with localcontext() as context:
        context.prec = 50
        return format(value.normalize(), "f") if value else "0"


def _mapping(kind, unit):
    normalized = SPELLINGS.get(unit, unit)
    if kind not in UNITS or normalized not in UNITS[kind][1]:
        raise ValueError("science_unit_or_quantity_unsupported")
    offset = Decimal("273.15") if kind == "temperature" and normalized == "degC" else Decimal(0)
    return normalized, Decimal(UNITS[kind][1][normalized]), offset


def normalize_quantity(quantity, *, expected_kind, target_unit, expected_frame=None, basis="not-applicable"):
    sc.validate_shape(quantity, "quantity")
    if sc._unknown(quantity):
        raise ValueError("science_quantity_unknown")
    if quantity["quantity_kind"] != expected_kind:
        raise ValueError("science_quantity_kind_mismatch")
    if expected_frame is not None and quantity["frame"] != expected_frame:
        raise ValueError("science_coordinate_frame_mismatch")
    if expected_kind.endswith("-fraction"):
        if basis != expected_kind.removesuffix("-fraction"):
            raise ValueError("science_fraction_basis_required")
    elif basis != "not-applicable":
        raise ValueError("science_unexpected_fraction_basis")
    if type(quantity["value"]) not in (int, float):
        raise ValueError("science_numeric_quantity_required")
    source_unit, scale, offset = _mapping(expected_kind, quantity["unit"])
    target, target_scale, target_offset = _mapping(expected_kind, target_unit)
    value = decimal_number(quantity["value"])
    with localcontext() as context:
        context.prec = 50
        coherent = value * scale + offset
        if expected_kind == "temperature" and coherent < 0:
            raise ValueError("science_temperature_below_absolute_zero")
        if expected_kind.endswith("-fraction") and not 0 <= coherent <= 1:
            raise ValueError("science_fraction_out_of_range")
        converted = decimal_number(str((coherent - target_offset) / target_scale))
        effective_scale, effective_offset = scale / target_scale, (offset - target_offset) / target_scale
        result = {**quantity, "value": int(converted) if converted == converted.to_integral() else float(converted), "unit": target}
        conversion = {"protocol": SCIENCE_UNITS_PROTOCOL, "source_unit": quantity["unit"], "normalized_source_unit": source_unit,
                      "target_unit": target, "quantity_kind": expected_kind, "basis": basis,
                      "scale": decimal_text(str(effective_scale)), "offset": decimal_text(str(effective_offset)),
                      "input_decimal": decimal_text(str(value)), "output_decimal": decimal_text(str(converted))}
    return {"quantity": result, "decimal_value": decimal_text(str(converted)), "conversion": conversion}


def canonical_quantity(value):
    sc.validate_shape(value, "quantity")
    if sc._unknown(value):
        return copy.deepcopy(value)
    if value["quantity_kind"] == "flag" and value["unit"] == "1":
        return copy.deepcopy(value)
    kind = value["quantity_kind"]
    if kind not in UNITS or kind.endswith("-fraction"):
        raise ValueError("science_condition_quantity_unsupported")
    converted = normalize_quantity(value, expected_kind=kind, target_unit=UNITS[kind][0])
    if decimal_text(str(converted["quantity"]["value"])) != converted["decimal_value"]:
        raise ValueError("science_condition_precision_loss")
    return converted["quantity"]
