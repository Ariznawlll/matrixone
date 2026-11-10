class MotrError(Exception):
    pass


class TestAbort(MotrError):
    """Raised to abort a test file (e.g. --die, unmet --error)."""


class TestSkip(MotrError):
    """Raised when a test wants to be skipped (--skip)."""


# A small subset of MySQL error-name → code mapping, extended on demand.
ERROR_NAMES = {
    "ER_DUP_ENTRY": 1062,
    "ER_NO_SUCH_TABLE": 1146,
    "ER_BAD_DB_ERROR": 1049,
    "ER_TABLE_EXISTS_ERROR": 1050,
    "ER_PARSE_ERROR": 1064,
    "ER_UNKNOWN_TABLE": 1109,
    "ER_LOCK_WAIT_TIMEOUT": 1205,
    "ER_LOCK_DEADLOCK": 1213,
    "ER_UNKNOWN_ERROR": 1105,
    "ER_ACCESS_DENIED_ERROR": 1045,
    "ER_BAD_FIELD_ERROR": 1054,
    "ER_WRONG_TABLE_NAME": 1103,
    "ER_WRONG_VALUE_COUNT_ON_ROW": 1136,
    "ER_NON_UNIQ_ERROR": 1052,
    "ER_DATA_TOO_LONG": 1406,
    "ER_TRUNCATED_WRONG_VALUE": 1292,
    "ER_WARN_DATA_OUT_OF_RANGE": 1264,
    "ER_UNKNOWN_SYSTEM_VARIABLE": 1193,
}


def resolve_error_spec(spec: str) -> list[int]:
    """Parse --error args like `1062`, `ER_DUP_ENTRY`, `0,1062`, `1062,ER_XXX`."""
    out: list[int] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            out.append(int(tok))
        elif tok in ERROR_NAMES:
            out.append(ERROR_NAMES[tok])
        else:
            raise MotrError(f"unknown error symbol: {tok}")
    return out
