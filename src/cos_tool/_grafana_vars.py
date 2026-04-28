"""Shared Grafana template variable handling utilities for PromQL and LogQL transforms.

This module ports the regex-based Grafana variable replacement and restoration logic
from the Go implementation in pkg/tool/promql_transform.go and logql_transform.go.
"""

from __future__ import annotations

import re
from typing import Callable


# ---------------------------------------------------------------------------
# Shared regex patterns
# ---------------------------------------------------------------------------

# Grafana template variable: $var or ${var} or ${var:option}
_VAR_PAT = r"\$(?:\w+|\{[^}]+\})"

# by(...) / without(...) grouping clause content
_GROUPING_CONTENT_RE = re.compile(r"\b((?:by|without)\s*\()([^)]*)(\))")

# Double-quoted string literal  (handles escaped quotes via \\.)
_DOUBLE_QUOTED_STR_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')

# Backtick raw string literal (LogQL supports these in filters)
_BACKTICK_STR_RE = re.compile(r"`[^`]*`")


# ---------------------------------------------------------------------------
# Placeholder counter (shared, reset per-transform call)
# ---------------------------------------------------------------------------

_COUNTER_START = 99_990_000


# ---------------------------------------------------------------------------
# Grouping-variable replacement (shared by PromQL and LogQL)
# ---------------------------------------------------------------------------

def _normalize_grouping_content(content: str) -> str:
    """Ensure comma separation between labels inside a by()/without() clause.

    Grafana users may write ``by (label $var)`` without commas because
    ``$var`` is interpolated at render time.  The PromQL/LogQL parsers
    require commas, so we normalise ``label __g0__`` → ``label, __g0__``.
    """
    tokens: list[str] = []
    for part in content.split(","):
        tokens.extend(part.split())
    return ", ".join(tokens)


def replace_variables_in_grouping(
    query: str,
    var_pat: re.Pattern[str],
    get_placeholder: Callable[[str, str], str],
) -> str:
    """Replace Grafana variables inside ``by()``/``without()`` clauses.

    Uses the ``__g%d__`` format so placeholders don't clash with the
    numeric ones used for durations/values.

    Masks string literals first so that patterns like
    ``|= "queued by ($q)"`` are not rewritten.
    """
    literals: list[str] = []

    def _mask(m: re.Match[str]) -> str:
        idx = len(literals)
        literals.append(m.group(0))
        return f'"__LIT{idx}__"'

    masked = _DOUBLE_QUOTED_STR_RE.sub(_mask, query)
    masked = _BACKTICK_STR_RE.sub(_mask, masked)

    def _replace_grouping(m: re.Match[str]) -> str:
        prefix = m.group(1)   # "by(" or "without("
        content = m.group(2)  # labels inside parens
        suffix = m.group(3)   # ")"

        if not var_pat.search(content):
            return m.group(0)

        new_content = var_pat.sub(
            lambda v: get_placeholder(v.group(0), "__g%d__"), content
        )
        new_content = _normalize_grouping_content(new_content)
        return prefix + new_content + suffix

    result = _GROUPING_CONTENT_RE.sub(_replace_grouping, masked)

    # Restore masked literals
    for i, lit in enumerate(literals):
        result = result.replace(f'"__LIT{i}__"', lit)

    return result
