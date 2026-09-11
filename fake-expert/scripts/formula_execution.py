#!/usr/bin/env python3
"""Restricted formula-AST validation, dimensional analysis, and code rendering.

This module deliberately accepts data, not Python expressions.  It is shared by
the execution-tier compiler and its contract validator; generated formula modules
embed the same small interpreter so the resulting Expert Skill is portable.
"""

from __future__ import annotations

import ast as python_ast
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, getcontext
from typing import Any


FORMULA_AST_SCHEMA = "tkc.formula-ast/v0.1"
DIMENSION_KEYS = ("L", "M", "T", "I", "Theta", "N", "J")
SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
UNIT_FACTOR_RE = re.compile(r"([A-Za-z]+)(?:\^(-?[0-9]+))?")

# Scale is relative to coherent SI units.  The intentionally short registry makes
# every admitted unit reviewable; expanding it is a contract change, not a model
# decision.
UNIT_TABLE: dict[str, tuple[str, tuple[int, ...]]] = {
    "1": ("1", (0, 0, 0, 0, 0, 0, 0)),
    "m": ("1", (1, 0, 0, 0, 0, 0, 0)),
    "cm": ("0.01", (1, 0, 0, 0, 0, 0, 0)),
    "mm": ("0.001", (1, 0, 0, 0, 0, 0, 0)),
    "km": ("1000", (1, 0, 0, 0, 0, 0, 0)),
    "s": ("1", (0, 0, 1, 0, 0, 0, 0)),
    "ms": ("0.001", (0, 0, 1, 0, 0, 0, 0)),
    "min": ("60", (0, 0, 1, 0, 0, 0, 0)),
    "h": ("3600", (0, 0, 1, 0, 0, 0, 0)),
    "kg": ("1", (0, 1, 0, 0, 0, 0, 0)),
    "g": ("0.001", (0, 1, 0, 0, 0, 0, 0)),
    "A": ("1", (0, 0, 0, 1, 0, 0, 0)),
    "K": ("1", (0, 0, 0, 0, 1, 0, 0)),
    "mol": ("1", (0, 0, 0, 0, 0, 1, 0)),
    "cd": ("1", (0, 0, 0, 0, 0, 0, 1)),
    "Hz": ("1", (0, 0, -1, 0, 0, 0, 0)),
    "N": ("1", (1, 1, -2, 0, 0, 0, 0)),
    "Pa": ("1", (-1, 1, -2, 0, 0, 0, 0)),
    "J": ("1", (2, 1, -2, 0, 0, 0, 0)),
    "W": ("1", (2, 1, -3, 0, 0, 0, 0)),
    "C": ("1", (0, 0, 1, 1, 0, 0, 0)),
    "V": ("1", (2, 1, -3, -1, 0, 0, 0)),
    "ohm": ("1", (2, 1, -3, -2, 0, 0, 0)),
    "eV": ("1.602176634e-19", (2, 1, -2, 0, 0, 0, 0)),
}


class FormulaContractError(ValueError):
    """Stable validation error for formula execution inputs."""


@dataclass(frozen=True)
class FormulaContract:
    ast: dict[str, Any]
    dimensions: tuple[int, ...]
    inputs: tuple[str, ...]
    output: str


def sha256_json(value: Any) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _dimension(value: Any, path: str) -> tuple[int, ...]:
    if not isinstance(value, dict) or set(value) - set(DIMENSION_KEYS):
        raise FormulaContractError(f"dimension_invalid:{path}")
    result: list[int] = []
    for key in DIMENSION_KEYS:
        exponent = value.get(key, 0)
        if not isinstance(exponent, int) or isinstance(exponent, bool) or not -16 <= exponent <= 16:
            raise FormulaContractError(f"dimension_invalid:{path}.{key}")
        result.append(exponent)
    return tuple(result)


def dimension_object(value: tuple[int, ...]) -> dict[str, int]:
    return {key: exponent for key, exponent in zip(DIMENSION_KEYS, value) if exponent}


def _add_dimensions(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a + b for a, b in zip(left, right))


def _sub_dimensions(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a - b for a, b in zip(left, right))


def parse_unit(value: Any, path: str = "unit") -> tuple[Decimal, tuple[int, ...]]:
    """Parse a deliberately small multiplicative unit grammar, e.g. ``kg*m/s^2``."""

    if not isinstance(value, str) or not value or len(value) > 80 or any(char.isspace() for char in value):
        raise FormulaContractError(f"unit_invalid:{path}")
    if value == "1":
        return Decimal(1), (0, 0, 0, 0, 0, 0, 0)
    factor = Decimal(1)
    dimensions = (0, 0, 0, 0, 0, 0, 0)
    position = 0
    operator = 1
    while position < len(value):
        if position:
            marker = value[position]
            if marker not in "*/":
                raise FormulaContractError(f"unit_invalid:{path}")
            operator = 1 if marker == "*" else -1
            position += 1
        matched = UNIT_FACTOR_RE.match(value, position)
        if not matched:
            raise FormulaContractError(f"unit_invalid:{path}")
        name, raw_power = matched.groups()
        if name not in UNIT_TABLE:
            raise FormulaContractError(f"unit_unknown:{path}:{name}")
        power = int(raw_power) if raw_power is not None else 1
        if not -16 <= power <= 16:
            raise FormulaContractError(f"unit_power_invalid:{path}")
        power *= operator
        raw_scale, unit_dimension = UNIT_TABLE[name]
        factor *= Decimal(raw_scale) ** power
        dimensions = _add_dimensions(
            dimensions, tuple(power * exponent for exponent in unit_dimension)
        )
        position = matched.end()
    return factor, dimensions


def _decimal(value: Any, path: str) -> Decimal:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise FormulaContractError(f"number_invalid:{path}")
    if isinstance(value, str) and len(value) > 256:
        raise FormulaContractError(f"number_invalid:{path}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise FormulaContractError(f"number_invalid:{path}") from error
    if not parsed.is_finite():
        raise FormulaContractError(f"number_invalid:{path}")
    return parsed


def validate_formula_ast(value: Any) -> FormulaContract:
    """Validate a formula AST without executing arbitrary source code."""

    if not isinstance(value, dict) or set(value) != {"schema_version", "output", "symbols", "expression"}:
        raise FormulaContractError("formula_ast_shape_invalid")
    if len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) > 32768:
        raise FormulaContractError("formula_ast_too_large")
    if value.get("schema_version") != FORMULA_AST_SCHEMA:
        raise FormulaContractError("formula_ast_schema_invalid")
    output = value.get("output")
    symbols = value.get("symbols")
    if not isinstance(output, str) or not SYMBOL_RE.fullmatch(output):
        raise FormulaContractError("formula_ast_output_invalid")
    if not isinstance(symbols, dict) or not symbols or len(symbols) > 32:
        raise FormulaContractError("formula_ast_symbols_invalid")
    parsed_symbols: dict[str, tuple[str, tuple[int, ...]]] = {}
    inputs: list[str] = []
    for name, specification in symbols.items():
        if not isinstance(name, str) or not SYMBOL_RE.fullmatch(name) or not isinstance(specification, dict):
            raise FormulaContractError("formula_ast_symbol_invalid")
        if set(specification) != {"role", "unit", "dimension"}:
            raise FormulaContractError(f"formula_ast_symbol_shape_invalid:{name}")
        role = specification.get("role")
        if role not in {"input", "output"}:
            raise FormulaContractError(f"formula_ast_symbol_role_invalid:{name}")
        unit = specification.get("unit")
        _, unit_dimension = parse_unit(unit, f"symbols.{name}.unit")
        declared_dimension = _dimension(specification.get("dimension"), f"symbols.{name}.dimension")
        if unit_dimension != declared_dimension:
            raise FormulaContractError(f"symbol_unit_dimension_mismatch:{name}")
        parsed_symbols[name] = (str(unit), declared_dimension)
        if role == "input":
            inputs.append(name)
        elif name != output:
            raise FormulaContractError("formula_ast_output_symbol_mismatch")
    if output not in parsed_symbols or not inputs or symbols[output]["role"] != "output":
        raise FormulaContractError("formula_ast_output_invalid")

    node_count = 0

    def walk(node: Any, path: str) -> tuple[int, ...]:
        nonlocal node_count
        node_count += 1
        if node_count > 128:
            raise FormulaContractError("formula_ast_too_deep")
        if not isinstance(node, dict) or not isinstance(node.get("kind"), str):
            raise FormulaContractError(f"formula_ast_node_invalid:{path}")
        kind = node["kind"]
        if kind == "constant":
            if set(node) != {"kind", "value", "unit"}:
                raise FormulaContractError(f"formula_ast_node_shape_invalid:{path}")
            _decimal(node["value"], f"{path}.value")
            _, constant_dimension = parse_unit(node["unit"], f"{path}.unit")
            return constant_dimension
        if kind == "symbol":
            if set(node) != {"kind", "name"} or node.get("name") not in parsed_symbols:
                raise FormulaContractError(f"formula_ast_symbol_reference_invalid:{path}")
            return parsed_symbols[node["name"]][1]
        if kind in {"add", "subtract", "multiply", "divide"}:
            if set(node) != {"kind", "left", "right"}:
                raise FormulaContractError(f"formula_ast_node_shape_invalid:{path}")
            left = walk(node["left"], f"{path}.left")
            right = walk(node["right"], f"{path}.right")
            if kind in {"add", "subtract"}:
                if left != right:
                    raise FormulaContractError(f"dimension_addition_mismatch:{path}")
                return left
            return _add_dimensions(left, right) if kind == "multiply" else _sub_dimensions(left, right)
        if kind == "negate":
            if set(node) != {"kind", "arg"}:
                raise FormulaContractError(f"formula_ast_node_shape_invalid:{path}")
            return walk(node["arg"], f"{path}.arg")
        if kind == "power":
            if set(node) != {"kind", "base", "exponent"}:
                raise FormulaContractError(f"formula_ast_node_shape_invalid:{path}")
            exponent = node.get("exponent")
            if not isinstance(exponent, int) or isinstance(exponent, bool) or not -8 <= exponent <= 8:
                raise FormulaContractError(f"formula_ast_power_invalid:{path}")
            return tuple(exponent * item for item in walk(node["base"], f"{path}.base"))
        if kind == "sqrt":
            if set(node) != {"kind", "arg"}:
                raise FormulaContractError(f"formula_ast_node_shape_invalid:{path}")
            dimension = walk(node["arg"], f"{path}.arg")
            if any(item % 2 for item in dimension):
                raise FormulaContractError(f"dimension_sqrt_invalid:{path}")
            return tuple(item // 2 for item in dimension)
        raise FormulaContractError(f"formula_ast_operation_invalid:{path}")

    result_dimension = walk(value.get("expression"), "expression")
    if result_dimension != parsed_symbols[output][1]:
        raise FormulaContractError("formula_output_dimension_mismatch")
    return FormulaContract(
        ast=value,
        dimensions=result_dimension,
        inputs=tuple(sorted(inputs)),
        output=output,
    )


def evaluate_formula_ast(contract: FormulaContract, inputs: Any) -> dict[str, Any]:
    """Evaluate validated data with Decimal arithmetic and explicit unit conversion."""

    if not isinstance(inputs, dict) or set(inputs) != set(contract.inputs):
        raise FormulaContractError("execution_inputs_invalid")
    values: dict[str, Decimal] = {}
    for name in contract.inputs:
        item = inputs[name]
        specification = contract.ast["symbols"][name]
        if not isinstance(item, dict) or set(item) != {"value", "unit"}:
            raise FormulaContractError(f"execution_input_invalid:{name}")
        input_value = _decimal(item["value"], f"inputs.{name}.value")
        scale, input_dimension = parse_unit(item["unit"], f"inputs.{name}.unit")
        _, expected_dimension = parse_unit(specification["unit"], f"symbols.{name}.unit")
        if input_dimension != expected_dimension:
            raise FormulaContractError(f"execution_input_dimension_mismatch:{name}")
        values[name] = input_value * scale

    def evaluate(node: dict[str, Any]) -> Decimal:
        kind = node["kind"]
        if kind == "constant":
            scale, _ = parse_unit(node["unit"])
            return _decimal(node["value"], "constant.value") * scale
        if kind == "symbol":
            return values[node["name"]]
        if kind == "add":
            return evaluate(node["left"]) + evaluate(node["right"])
        if kind == "subtract":
            return evaluate(node["left"]) - evaluate(node["right"])
        if kind == "multiply":
            return evaluate(node["left"]) * evaluate(node["right"])
        if kind == "divide":
            denominator = evaluate(node["right"])
            if denominator == 0:
                raise FormulaContractError("execution_divide_by_zero")
            return evaluate(node["left"]) / denominator
        if kind == "negate":
            return -evaluate(node["arg"])
        if kind == "power":
            exponent = node["exponent"]
            if exponent < 0 and evaluate(node["base"]) == 0:
                raise FormulaContractError("execution_divide_by_zero")
            return evaluate(node["base"]) ** exponent
        if kind == "sqrt":
            value = evaluate(node["arg"])
            if value < 0:
                raise FormulaContractError("execution_sqrt_negative")
            return value.sqrt()
        raise FormulaContractError("execution_operation_invalid")

    getcontext().prec = 50
    output_unit = contract.ast["symbols"][contract.output]["unit"]
    output_scale, _ = parse_unit(output_unit, "output.unit")
    result = evaluate(contract.ast["expression"]) / output_scale
    if not result.is_finite():
        raise FormulaContractError("execution_result_invalid")
    return {
        "output": contract.output,
        "value": format(result.normalize(), "f") if result != 0 else "0",
        "unit": output_unit,
        "dimension": dimension_object(contract.dimensions),
    }


def verify_generated_module(source: str) -> None:
    """Reject code-generation drift beyond a tiny, auditable Python subset."""

    try:
        tree = python_ast.parse(source)
    except SyntaxError as error:
        raise FormulaContractError("generated_module_syntax_invalid") from error
    banned_names = {"eval", "exec", "open", "compile", "__import__", "input", "breakpoint"}
    for node in python_ast.walk(tree):
        if isinstance(node, python_ast.Import):
            if any(alias.name not in {"json", "sys", "decimal", "re"} for alias in node.names):
                raise FormulaContractError("generated_module_import_invalid")
        if isinstance(node, python_ast.ImportFrom):
            if node.module not in {"decimal"} or node.level:
                raise FormulaContractError("generated_module_import_invalid")
        if isinstance(node, python_ast.Name) and node.id in banned_names:
            raise FormulaContractError("generated_module_builtin_invalid")
        if isinstance(node, python_ast.Attribute) and node.attr.startswith("__"):
            raise FormulaContractError("generated_module_dunder_invalid")


def render_formula_module(contract: FormulaContract) -> str:
    """Render a standalone evaluator module from a validated AST, never text math."""

    ast_json = json.dumps(contract.ast, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # The module has no filesystem, process, or networking APIs.  It is still run
    # under a constrained child process by executable_formula_tier.py.
    source = f'''#!/usr/bin/env python3
"""Generated by fake-expert from a reviewed formula AST; do not hand-edit."""
import json
import sys
from decimal import Decimal, InvalidOperation, getcontext

AST = json.loads({ast_json!r})
DIMENSION_KEYS = {DIMENSION_KEYS!r}
UNIT_TABLE = {UNIT_TABLE!r}

def _unit(value):
    if not isinstance(value, str) or not value or len(value) > 80 or any(c.isspace() for c in value):
        raise ValueError("unit_invalid")
    if value == "1":
        return Decimal(1), (0, 0, 0, 0, 0, 0, 0)
    factor = Decimal(1); dims = (0, 0, 0, 0, 0, 0, 0); position = 0; operator = 1
    import re
    pattern = re.compile(r"([A-Za-z]+)(?:\\^(-?[0-9]+))?")
    while position < len(value):
        if position:
            marker = value[position]
            if marker not in "*/": raise ValueError("unit_invalid")
            operator = 1 if marker == "*" else -1; position += 1
        matched = pattern.match(value, position)
        if not matched: raise ValueError("unit_invalid")
        name, raw_power = matched.groups()
        if name not in UNIT_TABLE: raise ValueError("unit_unknown")
        power = (int(raw_power) if raw_power is not None else 1) * operator
        raw_scale, unit_dims = UNIT_TABLE[name]
        factor *= Decimal(raw_scale) ** power
        dims = tuple(a + power * b for a, b in zip(dims, unit_dims))
        position = matched.end()
    return factor, dims

def _number(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool): raise ValueError("number_invalid")
    if isinstance(value, str) and len(value) > 256: raise ValueError("number_invalid")
    try: parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error: raise ValueError("number_invalid") from error
    if not parsed.is_finite(): raise ValueError("number_invalid")
    return parsed

def evaluate(inputs):
    symbols = AST["symbols"]; expected = {{name for name, spec in symbols.items() if spec["role"] == "input"}}
    if not isinstance(inputs, dict) or set(inputs) != expected: raise ValueError("inputs_invalid")
    values = {{}}
    for name in expected:
        item = inputs[name]
        if not isinstance(item, dict) or set(item) != {{"value", "unit"}}: raise ValueError("input_invalid")
        scale, dimensions = _unit(item["unit"]); _, target_dimensions = _unit(symbols[name]["unit"])
        if dimensions != target_dimensions: raise ValueError("input_dimension_mismatch")
        values[name] = _number(item["value"]) * scale
    def walk(node):
        kind = node["kind"]
        if kind == "constant": return _number(node["value"]) * _unit(node["unit"])[0]
        if kind == "symbol": return values[node["name"]]
        if kind == "add": return walk(node["left"]) + walk(node["right"])
        if kind == "subtract": return walk(node["left"]) - walk(node["right"])
        if kind == "multiply": return walk(node["left"]) * walk(node["right"])
        if kind == "divide":
            right = walk(node["right"])
            if right == 0: raise ValueError("divide_by_zero")
            return walk(node["left"]) / right
        if kind == "negate": return -walk(node["arg"])
        if kind == "power": return walk(node["base"]) ** node["exponent"]
        if kind == "sqrt":
            value = walk(node["arg"])
            if value < 0: raise ValueError("sqrt_negative")
            return value.sqrt()
        raise ValueError("operation_invalid")
    getcontext().prec = 50
    output = AST["output"]; unit = symbols[output]["unit"]; scale, dimensions = _unit(unit)
    value = walk(AST["expression"]) / scale
    return {{"output": output, "value": format(value.normalize(), "f") if value != 0 else "0", "unit": unit, "dimension": {{key: exponent for key, exponent in zip(DIMENSION_KEYS, dimensions) if exponent}}}}

def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536: raise ValueError("request_too_large")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or set(request) != {{"inputs"}}: raise ValueError("request_invalid")
        print(json.dumps({{"status": "ok", "result": evaluate(request["inputs"])}}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except Exception as error:
        print(json.dumps({{"status": "error", "code": str(error)}}, sort_keys=True, separators=(",", ":")))
        raise SystemExit(2)

if __name__ == "__main__":
    main()
'''
    verify_generated_module(source)
    return source
