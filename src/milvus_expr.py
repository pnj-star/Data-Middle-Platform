"""Safe construction of Milvus boolean-expression string literals.

All call sites that previously interpolated user values into Milvus ``expr``
strings (``f'source == "{name}"'`` etc.) must go through these helpers so that
special characters in file names / ids can never corrupt or inject into the
expression. Only ``source``, ``id`` and ``name`` values ever appear in these
expressions (never filesystem paths), which is exactly the surface these
helpers protect.
"""
from __future__ import annotations


def escape_literal(value: str) -> str:
    """Escape a value for use inside a double-quoted Milvus string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def escape_like(value: str) -> str:
    """Escape a value for use inside a Milvus ``like`` pattern.

    The ``%`` and ``_`` wildcards are escaped so user input is matched
    literally rather than acting as a pattern.
    """
    return (
        escape_literal(value)
        .replace("%", r"\%")
        .replace("_", r"\_")
    )


def eq(field: str, value: str) -> str:
    """Build ``field == "value"`` with the value escaped."""
    return f'{field} == "{escape_literal(value)}"'


def eq_int(field: str, value: int) -> str:
    """Build ``field == 5`` for INT fields (no escaping needed)."""
    return f"{field} == {int(value)}"


def like(field: str, value: str) -> str:
    """Build ``field like "%value%"`` with wildcards in the value escaped."""
    return f'{field} like "%{escape_like(value)}%"'


def in_expr(field: str, values: list[str]) -> str:
    """Build ``field in ["v1", "v2"]`` with every value escaped."""
    quoted = ", ".join(f'"{escape_literal(v)}"' for v in values)
    return f"{field} in [{quoted}]"


def in_int_expr(field: str, values: list[int]) -> str:
    """Build ``field in [1, 2]`` for INT fields (e.g. wiki revision_id)."""
    return f"{field} in [{', '.join(str(int(v)) for v in values)}]"


def ne_int_expr(field: str, value: int) -> str:
    """Build ``field != 5`` for INT fields."""
    return f"{field} != {int(value)}"


def lt_int_expr(field: str, value: int) -> str:
    """Build ``field < 5`` for INT fields (used for old-version vector cleanup)."""
    return f"{field} < {int(value)}"


def and_expr(*exprs: str) -> str:
    """Join non-empty boolean expr strings with `` and ``."""
    return " and ".join(e for e in exprs if e)
