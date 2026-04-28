"""PromQL label injection and rule/config validation.

This module ports the functionality from pkg/tool/promql_transform.go and
pkg/tool/promql_config_check_test.go in the Go cos-tool implementation.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

import yaml

from ._grafana_vars import (
    _BACKTICK_STR_RE,
    _DOUBLE_QUOTED_STR_RE,
    _VAR_PAT,
    _COUNTER_START,
    replace_variables_in_grouping,
)

try:
    import promql_parser as _pp
    _HAS_PROMQL_PARSER = True
except ImportError:  # pragma: no cover
    _HAS_PROMQL_PARSER = False


# ---------------------------------------------------------------------------
# Compiled regex patterns (PromQL-specific)
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(_VAR_PAT)

# Function name replacement: variable in function-call position
# ((?:^|[^"\w])\s*)  prefix not inside string/identifier
# (\$(?:\w+|\{[^}]+\}))  the variable
# (\s*\()  opening paren
_FUNC_NAME_REPLACE_RE = re.compile(r'((?:^|[^"\w])\s*)(' + _VAR_PAT + r')(\s*\()')

_FUNCTION_PLACEHOLDER_POOL = ["rate", "irate", "increase", "delta", "changes", "resets", "deriv", "idelta"]

_REAL_FUNC_CALL_RE = re.compile(
    r'(?:^|[^\w$])(' + "|".join(_FUNCTION_PLACEHOLDER_POOL) + r')\s*\('
)

# Metric name pattern: $var{  or ${var}{
_FULL_METRIC_NAME_RE = re.compile(r'(?:^|[,\(])\s*(' + _VAR_PAT + r')\s*\{')

# Metric name component: prefix + $var + optional-suffix + {
_METRIC_NAME_COMPONENT_RE = re.compile(r'(\w+)(' + _VAR_PAT + r')(\w*)\{')

# Range duration: [$var]
_RANGE_DURATION_RE = re.compile(r'\[(' + _VAR_PAT + r')\]')


# ---------------------------------------------------------------------------
# Prometheus duration normalisation helpers
# ---------------------------------------------------------------------------

def _seconds_to_promql_duration(seconds: int) -> str:
    """Convert seconds to a Prometheus model.Duration string (e.g. 99990000 → '1157d7h').

    Prometheus uses days/hours/minutes/seconds without sub-second parts.
    """
    d = seconds // 86400
    remaining = seconds % 86400
    h = remaining // 3600
    remaining = remaining % 3600
    m = remaining // 60
    s = remaining % 60

    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Grafana variable replacement for PromQL
# ---------------------------------------------------------------------------

def _replace_variables_in_function_names(query: str) -> tuple[str, dict[str, str]]:
    """Replace Grafana variables in function-call positions with placeholder function names."""
    literals: list[str] = []

    def _mask_lit(m: re.Match[str]) -> str:
        idx = len(literals)
        literals.append(m.group(0))
        return f'"__LIT{idx}__"'

    masked = _DOUBLE_QUOTED_STR_RE.sub(_mask_lit, query)

    used_functions: set[str] = set()
    for m in _REAL_FUNC_CALL_RE.finditer(masked):
        used_functions.add(m.group(1))

    available = [fn for fn in _FUNCTION_PLACEHOLDER_POOL if fn not in used_functions]

    placeholder_to_var: dict[str, str] = {}
    var_to_placeholder: dict[str, str] = {}
    avail_idx = [0]
    error: list[Optional[str]] = [None]

    def _replace_func(m: re.Match[str]) -> str:
        if error[0]:
            return m.group(0)
        prefix = m.group(1)
        variable = m.group(2)
        paren = m.group(3)

        func_name = var_to_placeholder.get(variable)
        if func_name is None:
            if avail_idx[0] >= len(available):
                error[0] = (
                    f"cannot safely replace function name variable {variable}: "
                    "all placeholder functions are already in use"
                )
                return m.group(0)
            func_name = available[avail_idx[0]]
            avail_idx[0] += 1
            var_to_placeholder[variable] = func_name
            placeholder_to_var[func_name] = variable
        return prefix + func_name + paren

    result = _FUNC_NAME_REPLACE_RE.sub(_replace_func, masked)

    if error[0]:
        raise ValueError(error[0])

    for i, lit in enumerate(literals):
        result = result.replace(f'"__LIT{i}__"', lit)

    return result, placeholder_to_var


def _restore_function_name_variables(query: str, placeholder_to_var: dict[str, str]) -> str:
    if not placeholder_to_var:
        return query

    func_names = sorted(placeholder_to_var.keys(), key=len, reverse=True)

    literals: list[str] = []

    def _mask_lit(m: re.Match[str]) -> str:
        idx = len(literals)
        literals.append(m.group(0))
        return f'"__LIT{idx}__"'

    result = _DOUBLE_QUOTED_STR_RE.sub(_mask_lit, query)

    for func_name in func_names:
        result = result.replace(func_name + "(", placeholder_to_var[func_name] + "(")

    for i, lit in enumerate(literals):
        result = result.replace(f'"__LIT{i}__"', lit)

    return result


def _replace_grafana_variables_promql(query: str) -> tuple[str, dict[str, str]]:
    """Replace all Grafana template variables with parseable placeholders for PromQL."""
    replacements: dict[str, str] = {}
    # (format, variable) → placeholder — same variable can get different kinds
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

    result = query
    result = replace_variables_in_grouping(result, _VAR_RE, get_placeholder)
    result = _replace_full_metric_name_variables(result, get_placeholder)
    result = _replace_metric_name_components(result, get_placeholder)
    result = _replace_variables_in_durations(result, get_placeholder)
    result = _replace_variables_in_values(result, get_placeholder)

    return result, replacements


def _replace_full_metric_name_variables(
    query: str, get_placeholder
) -> str:
    result = query
    while True:
        m = _FULL_METRIC_NAME_RE.search(result)
        if not m:
            break
        var_start = m.start(1)
        var_end = m.end(1)
        variable = result[var_start:var_end]
        placeholder = get_placeholder(variable, "__v%d__")
        prefix = result[m.start(0):var_start]
        result = result[:m.start(0)] + prefix + placeholder + "{" + result[m.end(0):]
    return result


def _replace_metric_name_components(
    query: str, get_placeholder
) -> str:
    result = query
    while True:
        m = _METRIC_NAME_COMPONENT_RE.search(result)
        if not m:
            break
        # Verify the match ends with '{' 
        if not result[m.start():m.end()].endswith("{"):
            break
        prefix_text = m.group(1)
        variable = m.group(2)
        suffix_text = m.group(3)
        placeholder = get_placeholder(variable, "__v%d__")
        replacement = prefix_text + placeholder + suffix_text + "{"
        result = result[:m.start()] + replacement + result[m.end():]
    return result


def _replace_variables_in_durations(query: str, get_placeholder) -> str:
    def _sub(m: re.Match[str]) -> str:
        variable = m.group(1)
        placeholder = get_placeholder(variable, "%d")
        return "[" + placeholder + "]"

    return _RANGE_DURATION_RE.sub(_sub, query)


def _replace_variables_in_values(query: str, get_placeholder) -> str:
    def _sub(m: re.Match[str]) -> str:
        return get_placeholder(m.group(0), "%d")

    return _VAR_RE.sub(_sub, query)


def _restore_grafana_variables_promql(query: str, replacements: dict[str, str]) -> str:
    """Restore original Grafana variables from placeholders."""
    # Build duration map: normalized promql duration string → original variable
    duration_map: dict[str, str] = {}
    for placeholder, original in replacements.items():
        try:
            counter = int(placeholder)
            normalized = _seconds_to_promql_duration(counter)
            duration_map[normalized] = original
        except (ValueError, TypeError):
            pass

    result = query
    # Restore duration placeholders first
    for normalized, original in duration_map.items():
        result = result.replace(normalized, original)

    # Sort remaining placeholders by length descending to avoid partial replacement
    other = sorted(replacements.keys(), key=lambda k: (-len(k), k))
    for placeholder in other:
        original = replacements[placeholder]
        result = result.replace(placeholder, original)

    return result


# ---------------------------------------------------------------------------
# Tree traversal and matcher injection
# ---------------------------------------------------------------------------

def _inject_matchers_into_vector_selector(
    vs,  # promql_parser.VectorSelector
    matchers_to_inject: dict[str, str],
) -> None:
    """Inject label matchers into a VectorSelector node, skipping existing labels."""
    existing_names = {m.name for m in vs.matchers.matchers}
    new_matchers = list(vs.matchers.matchers)
    for key, val in matchers_to_inject.items():
        if key not in existing_names:
            new_matchers.append(_pp.Matcher(_pp.MatchOp.Equal, key, val))
    # Sort by name for deterministic output (matches Go's prometheus parser behavior)
    new_matchers.sort(key=lambda m: m.name)
    vs.matchers = _pp.Matchers(new_matchers)


def _traverse_and_inject(node, matchers_to_inject: dict[str, str]) -> None:
    """Recursively walk the PromQL AST and inject matchers into VectorSelectors."""
    if isinstance(node, _pp.VectorSelector):
        _inject_matchers_into_vector_selector(node, matchers_to_inject)
    elif isinstance(node, _pp.MatrixSelector):
        _inject_matchers_into_vector_selector(node.vector_selector, matchers_to_inject)
    elif isinstance(node, _pp.Call):
        for arg in node.args:
            _traverse_and_inject(arg, matchers_to_inject)
    elif isinstance(node, _pp.AggregateExpr):
        _traverse_and_inject(node.expr, matchers_to_inject)
        if node.param is not None:
            _traverse_and_inject(node.param, matchers_to_inject)
    elif isinstance(node, _pp.BinaryExpr):
        _traverse_and_inject(node.lhs, matchers_to_inject)
        _traverse_and_inject(node.rhs, matchers_to_inject)
    elif isinstance(node, _pp.UnaryExpr):
        _traverse_and_inject(node.expr, matchers_to_inject)
    elif isinstance(node, _pp.ParenExpr):
        _traverse_and_inject(node.expr, matchers_to_inject)
    elif isinstance(node, _pp.SubqueryExpr):
        _traverse_and_inject(node.expr, matchers_to_inject)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transform_promql(expression: str, matchers: dict[str, str]) -> str:
    """Inject label matchers into every vector selector in a PromQL expression.

    Grafana template variables (``$var``, ``${var}``) are preserved.

    Raises:
        ValueError: If the expression cannot be parsed.
    """
    if not _HAS_PROMQL_PARSER:
        raise ImportError("promql-parser is required for PromQL transform")

    processed, func_replacements = _replace_variables_in_function_names(expression)
    processed, occurrences = _replace_grafana_variables_promql(processed)

    try:
        ast = _pp.parse(processed)
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    _traverse_and_inject(ast, matchers)
    result = str(ast)

    result = _restore_grafana_variables_promql(result, occurrences)
    result = _restore_function_name_variables(result, func_replacements)
    return result


def validate_rules_promql(filename: str, data: bytes) -> None:
    """Validate a Prometheus alert rule file.

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
            try:
                _pp.parse(str(expr))
            except Exception as exc:
                errors.append(
                    f'group "{group_name}", rule {i}, "{rule.get("alert") or rule.get("record")}": '
                    f"could not parse expression: {exc}"
                )

    if errors:
        raise ValueError(f"error validating {filename}: {errors}")


def validate_config_promql(filename: str) -> None:
    """Validate a Prometheus configuration file (syntax check only).

    Raises:
        ValueError: If the file contains validation errors.
    """
    try:
        with open(filename, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise ValueError(str(exc)) from exc

    try:
        content = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ValueError(str(exc)) from exc

    if not isinstance(content, dict):
        raise ValueError(f"{filename}: expected a YAML mapping at the top level")

    _KNOWN_TOP_LEVEL_KEYS = {
        "global", "alerting", "rule_files", "scrape_configs",
        "remote_write", "remote_read", "storage", "tracing",
    }

    for key in content:
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            raise ValueError(
                f"{filename}: unknown field \"{key}\" in configuration file"
            )

    _KNOWN_SCRAPE_JOB_KEYS = {
        "job_name", "honor_labels", "honor_timestamps", "scrape_interval",
        "scrape_timeout", "scrape_classic_histograms", "metrics_path",
        "scheme", "params", "basic_auth", "authorization", "oauth2",
        "tls_config", "proxy_url", "no_proxy", "proxy_from_environment",
        "proxy_connect_header", "follow_redirects", "enable_http2",
        "static_configs", "relabel_configs", "metric_relabel_configs",
        "sample_limit", "label_limit", "label_name_length_limit",
        "label_value_length_limit", "body_size_limit", "target_limit",
        "native_histogram_bucket_limit", "native_histogram_min_bucket_factor",
        "file_sd_configs", "http_sd_configs", "dns_sd_configs",
        "kubernetes_sd_configs", "ec2_sd_configs", "azure_sd_configs",
        "openstack_sd_configs", "gce_sd_configs", "consul_sd_configs",
        "eureka_sd_configs", "marathon_sd_configs", "nerve_sd_configs",
        "serverset_sd_configs", "triton_sd_configs", "docker_sd_configs",
        "dockerswarm_sd_configs", "hetzner_sd_configs", "ionos_sd_configs",
        "lightsail_sd_configs", "linode_sd_configs", "nomad_sd_configs",
        "ovhcloud_sd_configs", "scaleway_sd_configs", "uyuni_sd_configs",
        "vultr_sd_configs", "puppetdb_sd_configs",
    }

    _KNOWN_STATIC_CONFIG_KEYS = {"targets", "labels"}

    for job in content.get("scrape_configs") or []:
        if not isinstance(job, dict):
            continue
        for key in job:
            if key not in _KNOWN_SCRAPE_JOB_KEYS:
                raise ValueError(
                    f"{filename}: unknown field \"{key}\" in scrape_config"
                )
        for sc in job.get("static_configs") or []:
            if not isinstance(sc, dict):
                continue
            for key in sc:
                if key not in _KNOWN_STATIC_CONFIG_KEYS:
                    raise ValueError(
                        f"{filename}: unknown field \"{key}\" in static_config"
                    )
