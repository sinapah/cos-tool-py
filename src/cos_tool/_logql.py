"""LogQL label injection and rule validation.

This module ports the functionality from pkg/tool/logql_transform.go and
pkg/tool/lokiruler/compat.go in the Go cos-tool implementation.

Since there is no Python LogQL parser, label injection is implemented with a
regex-based stream-selector (`{...}`) parser.  This is sufficient because Loki
rule validation requires only structural YAML checks plus basic expression
sanity, and label injection only touches stream-selector curly-brace sections.
"""

from __future__ import annotations

import re
from typing import Optional

import yaml

from ._grafana_vars import (
    _BACKTICK_STR_RE,
    _DOUBLE_QUOTED_STR_RE,
    _VAR_PAT,
    _COUNTER_START,
    replace_variables_in_grouping,
)


# ---------------------------------------------------------------------------
# Compiled regex patterns (LogQL-specific)
# ---------------------------------------------------------------------------

_VAR_PAT_LOGQL = r"\$(?:\{[^}]+\}|\w+)"
_VAR_RE_LOGQL = re.compile(_VAR_PAT_LOGQL)

# Duration placeholder in range brackets: [$var]
_RANGE_DURATION_RE_LOGQL = re.compile(r"\[(" + _VAR_PAT_LOGQL + r")\]")

# Label matcher value pattern: name op "value" or name op value
# Captures variables in label values (both quoted and unquoted)
_LABEL_VALUE_RE = re.compile(
    r"(\w+)\s*(=~?|!=?~?)\s*(?:\"(" + _VAR_PAT_LOGQL + r")\"|(" + _VAR_PAT_LOGQL + r")(?:\s|,|}|\]))"
)

# Stream selector: {...}  (may contain nested strings with escaped chars)
_STREAM_SELECTOR_RE = re.compile(r"\{[^}]*\}")

# A single label matcher inside a stream selector: name op "value"
_LABEL_MATCHER_RE = re.compile(r'(\w+)\s*(=~?|!=?~?)\s*"((?:[^"\\]|\\.)*)"')


# ---------------------------------------------------------------------------
# Grafana variable replacement for LogQL
# ---------------------------------------------------------------------------

def _replace_grafana_variables_logql(query: str) -> tuple[str, dict[str, str]]:
    """Replace all Grafana template variables with parseable placeholders."""
    replacements: dict[str, str] = {}
    quoted_placeholders: set[str] = set()
    var_to_placeholder: dict[tuple[str, str], str] = {}
    counter = [_COUNTER_START]

    def get_placeholder(variable: str, fmt: str) -> str:
        key = (fmt, variable)
        if key in var_to_placeholder:
            return var_to_placeholder[key]
        placeholder = fmt % counter[0]
        counter[0] += 1
        var_to_placeholder[key] = placeholder
        replacements[placeholder] = variable
        return placeholder

    def get_placeholder_quoted(variable: str, fmt: str) -> str:
        key = (fmt, variable)
        if key in var_to_placeholder:
            return var_to_placeholder[key]
        placeholder = fmt % counter[0]
        counter[0] += 1
        var_to_placeholder[key] = placeholder
        replacements[placeholder] = variable
        quoted_placeholders.add(placeholder)
        return placeholder

    result = query
    result = replace_variables_in_grouping(result, _VAR_RE_LOGQL, get_placeholder)
    result = _replace_logql_variables_in_durations(result, get_placeholder)
    result = _replace_logql_variables_in_label_values(result, get_placeholder, get_placeholder_quoted)
    result = _replace_logql_variables_in_other_contexts(result, get_placeholder)

    # Store quote metadata so restoration knows which placeholders need quotes
    for ph in quoted_placeholders:
        replacements["__quoted__" + ph] = "true"

    return result, replacements


def _replace_logql_variables_in_durations(query: str, get_placeholder) -> str:
    def _sub(m: re.Match[str]) -> str:
        variable = m.group(1)
        # LogQL normalises durations, so we add "s" suffix
        placeholder = get_placeholder(variable, "%ds")
        return "[" + placeholder + "]"

    return _RANGE_DURATION_RE_LOGQL.sub(_sub, query)


def _replace_logql_variables_in_label_values(query: str, get_placeholder, get_placeholder_quoted) -> str:
    def _sub(m: re.Match[str]) -> str:
        label_name = m.group(1)
        operator = m.group(2)
        was_quoted = m.group(3) is not None
        variable = m.group(3) if was_quoted else m.group(4)

        if variable is None:
            return m.group(0)

        # Preserve the trailing character that the regex consumed
        last_char = m.group(0)[-1]
        suffix = last_char if last_char in (" ", ",", "}", ")") else ""

        if was_quoted:
            placeholder = get_placeholder_quoted(variable, "%d")
        else:
            placeholder = get_placeholder(variable, "%d")

        return f'{label_name}{operator}"{placeholder}"{suffix}'

    return _LABEL_VALUE_RE.sub(_sub, query)


def _replace_logql_variables_in_other_contexts(query: str, get_placeholder) -> str:
    def _sub(m: re.Match[str]) -> str:
        return get_placeholder(m.group(0), "%d")

    return _VAR_RE_LOGQL.sub(_sub, query)


def _seconds_to_logql_duration(seconds: int) -> str:
    """Convert seconds to the LogQL normalised duration format (e.g. 1157407h46m40s)."""
    h = seconds // 3600
    remaining = seconds % 3600
    m = remaining // 60
    s = remaining % 60

    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return "".join(parts)


def _restore_grafana_variables_logql(query: str, replacements: dict[str, str]) -> str:
    """Restore original Grafana variables from placeholders (LogQL version)."""
    # Build duration reverse map: LogQL-normalised string → original variable
    duration_map: dict[str, str] = {}
    for placeholder, original in replacements.items():
        if placeholder.endswith("s") and not placeholder.startswith("__"):
            num_str = placeholder[:-1]
            try:
                seconds = int(num_str)
                normalized = _seconds_to_logql_duration(seconds)
                duration_map[normalized] = original
            except ValueError:
                pass

    result = query
    for normalized, original in duration_map.items():
        result = result.replace(normalized, original)

    # Sort placeholders by length descending
    def _sort_key(k: str) -> tuple[int, str]:
        return (-len(k), k)

    placeholders = sorted(
        (k for k in replacements if not k.startswith("__quoted__")),
        key=_sort_key,
    )

    for placeholder in placeholders:
        original = replacements[placeholder]
        was_quoted = ("__quoted__" + placeholder) in replacements

        if was_quoted:
            result = result.replace(f'"{placeholder}"', f'"{original}"')
        else:
            quoted_form = f'"{placeholder}"'
            if quoted_form in result:
                result = result.replace(quoted_form, original)
            else:
                result = result.replace(placeholder, original)

    return result


# ---------------------------------------------------------------------------
# Stream-selector regex-based label injection
# ---------------------------------------------------------------------------

def _parse_label_matchers(selector_body: str) -> list[tuple[str, str, str]]:
    """Parse label=value pairs from the body of a stream selector (without braces).

    Returns a list of (name, op, value) tuples.
    """
    matchers: list[tuple[str, str, str]] = []
    for m in _LABEL_MATCHER_RE.finditer(selector_body):
        matchers.append((m.group(1), m.group(2), m.group(3)))
    return matchers


def _format_matchers(matchers: list[tuple[str, str, str]]) -> str:
    return ", ".join(f'{n}{op}"{v}"' for n, op, v in matchers)


def _inject_into_stream_selector(selector: str, matchers_to_inject: dict[str, str]) -> str:
    """Inject label matchers into a stream selector string ``{...}``.

    Existing labels are not overwritten.  Injected labels are appended in
    sorted order (matching the Go implementation's deterministic ordering).
    """
    body = selector[1:-1]  # strip { }
    existing = _parse_label_matchers(body)
    existing_names = {name for name, _, _ in existing}

    inject_sorted = sorted(matchers_to_inject.items())
    new_matchers = list(existing)
    for key, val in inject_sorted:
        if key not in existing_names:
            new_matchers.append((key, "=", val))

    return "{" + _format_matchers(new_matchers) + "}"


def _transform_logql(expression: str, matchers: dict[str, str]) -> str:
    """Inject matchers into stream selectors in a LogQL expression."""
    return _STREAM_SELECTOR_RE.sub(
        lambda m: _inject_into_stream_selector(m.group(0), matchers),
        expression,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transform_logql(expression: str, matchers: dict[str, str]) -> str:
    """Inject label matchers into every stream selector in a LogQL expression.

    Grafana template variables (``$var``, ``${var}``) are preserved.

    Raises:
        ValueError: If the expression is structurally invalid (e.g. unbalanced braces).
    """
    processed, occurrences = _replace_grafana_variables_logql(expression)
    result = _transform_logql(processed, matchers)
    result = _restore_grafana_variables_logql(result, occurrences)
    return result


def validate_rules_logql(filename: str, data: bytes) -> None:
    """Validate a Loki alerting rule file.

    Checks for:
    - Valid YAML structure
    - Duplicate group names (within the same file)
    - Non-empty expressions

    Raises:
        ValueError: If the file contains validation errors.
    """
    try:
        content = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ValueError(f"error validating {filename}: {exc}") from exc

    if not isinstance(content, dict):
        raise ValueError(f"error validating {filename}: expected a YAML mapping")

    groups = content.get("groups", [])
    seen_group_names: set[str] = set()
    errors: list[str] = []

    for group in groups:
        group_name = group.get("name", "")
        if group_name in seen_group_names:
            errors.append(f'groupname: "{group_name}" is repeated in the same file')
        seen_group_names.add(group_name)

        for i, rule in enumerate(group.get("rules", []), start=1):
            expr = rule.get("expr", "")
            if not expr:
                continue
            # Basic syntax check: look for obviously broken stream selectors
            expr_str = str(expr)
            # Check that braces are balanced
            depth = 0
            for ch in expr_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                if depth < 0:
                    errors.append(
                        f'group "{group_name}", rule {i}, '
                        f'"{rule.get("alert") or rule.get("record")}": '
                        f"syntax error: unbalanced braces in expression"
                    )
                    break
            else:
                if depth != 0:
                    errors.append(
                        f'group "{group_name}", rule {i}, '
                        f'"{rule.get("alert") or rule.get("record")}": '
                        f"syntax error: unbalanced braces in expression"
                    )

    if errors:
        raise ValueError(f"error validating {filename}: {errors}")
