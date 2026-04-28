"""Tests for PromQL and LogQL transform and validation — mirroring the Go test suite."""

from __future__ import annotations

import os
import pytest

from cos_tool._promql import transform_promql, validate_rules_promql, validate_config_promql
from cos_tool._logql import transform_logql, validate_rules_logql

TESTDATA = os.path.join(os.path.dirname(__file__), "testdata")


# ---------------------------------------------------------------------------
# PromQL transform
# ---------------------------------------------------------------------------

class TestPromQLTransformLabelInjection:
    def test_rate_expression(self):
        out = transform_promql('rate(metric[5m]) > 0.5', {"bar": "baz"})
        assert out == 'rate(metric{bar="baz"}[5m]) > 0.5'

    def test_bare_metric(self):
        out = transform_promql("metric", {"bar": "baz"})
        assert out == 'metric{bar="baz"}'

    def test_multiple_matchers_sorted(self):
        out = transform_promql("up == 0", {"cool": "breeze", "hot": "sunrays"})
        assert out == 'up{cool="breeze",hot="sunrays"} == 0'

    def test_absent(self):
        out = transform_promql('absent(up{job="prometheus"})', {"model": "lma"})
        assert out == 'absent(up{job="prometheus",model="lma"})'

    def test_aggregation(self):
        out = transform_promql(
            "sum by(consumergroup) (kafka_consumergroup_lag) > 50",
            {"firstname": "Franz"},
        )
        assert 'kafka_consumergroup_lag{firstname="Franz"}' in out

    def test_existing_label_not_overwritten(self):
        out = transform_promql('up{cool="breeze",hot="sunrays"} == 0', {"cool": "stuff"})
        assert out == 'up{cool="breeze",hot="sunrays"} == 0'

    def test_existing_label_partial_injection(self):
        out = transform_promql(
            'up{cool="breeze",hot="sunrays"} == 0',
            {"cool": "stuff", "dance": "macarena"},
        )
        assert 'dance="macarena"' in out
        assert 'cool="breeze"' in out


class TestPromQLTransformGrafanaVariables:
    def test_label_value_variable(self):
        out = transform_promql('up{job="$job"}', {"env": "prod"})
        assert out == 'up{env="prod",job="$job"}'

    def test_range_duration_variable(self):
        out = transform_promql('rate(up{job="test"}[$__rate_interval])', {"env": "prod"})
        assert out == 'rate(up{env="prod",job="test"}[$__rate_interval])'

    def test_full_metric_name_variable_braces_syntax(self):
        out = transform_promql('${metric_name}{job="test"}', {"env": "prod"})
        assert out == '${metric_name}{env="prod",job="test"}'

    def test_full_metric_name_variable_dollar_syntax(self):
        out = transform_promql('$metric_name{job="test"}', {"env": "prod"})
        assert out == '$metric_name{env="prod",job="test"}'

    def test_metric_name_suffix_variable(self):
        out = transform_promql('otelcol_receiver_${suffix_total}{job="test"}', {"env": "prod"})
        assert out == 'otelcol_receiver_${suffix_total}{env="prod",job="test"}'

    def test_complex_real_world_query(self):
        out = transform_promql(
            'sum(rate(otelcol_receiver_accepted${suffix_total}{receiver=~"$receiver",job="$job"}[$__rate_interval])) by (receiver)',
            {"cluster": "prod"},
        )
        assert 'cluster="prod"' in out
        assert '$__rate_interval' in out
        assert '$receiver' in out
        assert '$job' in out

    def test_grouping_variable_in_by_clause(self):
        out = transform_promql('sum(rate(up[5m])) by ($grouping)', {"env": "prod"})
        assert '$grouping' in out
        assert 'env="prod"' in out

    def test_variable_as_prefix_raises(self):
        with pytest.raises(ValueError):
            transform_promql('${prefix}_metric{job="test"}', {"env": "prod"})

    def test_no_variables(self):
        out = transform_promql('up{job="test"}', {"env": "prod"})
        assert out == 'up{env="prod",job="test"}'

    def test_variable_in_regex(self):
        out = transform_promql('up{job=~"$job.*"}', {"env": "prod"})
        assert 'env="prod"' in out
        assert '$job' in out

    def test_aggregation_parameter_variable(self):
        out = transform_promql('topk($limit, up)', {"env": "prod"})
        assert '$limit' in out
        assert 'env="prod"' in out


# ---------------------------------------------------------------------------
# PromQL validate-rules
# ---------------------------------------------------------------------------

class TestPromQLValidateRules:
    def _read(self, name: str) -> bytes:
        with open(os.path.join(TESTDATA, "prom_alerts", name), "rb") as f:
            return f.read()

    def test_valid_file(self):
        # No exception expected
        validate_rules_promql("basic.yaml", self._read("basic.yaml"))

    def test_duplicate_group(self):
        with pytest.raises(ValueError, match='groupname: "yolo" is repeated'):
            validate_rules_promql("duplicate_group.yaml", self._read("duplicate_group.yaml"))

    def test_bad_expression(self):
        with pytest.raises(ValueError, match="could not parse expression"):
            validate_rules_promql("bad_expr.yaml", self._read("bad_expr.yaml"))


# ---------------------------------------------------------------------------
# PromQL validate-config
# ---------------------------------------------------------------------------

class TestPromQLValidateConfig:
    def _path(self, name: str) -> str:
        return os.path.join(TESTDATA, "prom_configs", name)

    def test_good_config(self):
        validate_config_promql(self._path("good_config.yml"))

    def test_bad_yaml(self):
        with pytest.raises(ValueError):
            validate_config_promql(self._path("bad_yaml.yml"))

    def test_bad_key(self):
        with pytest.raises(ValueError):
            validate_config_promql(self._path("bad_key.yml"))


# ---------------------------------------------------------------------------
# LogQL transform
# ---------------------------------------------------------------------------

class TestLogQLTransformLabelInjection:
    def test_stream_selector_with_filter(self):
        out = transform_logql(
            'sum(rate({app="foo", env="production"} |= "error" [5m])) by (job)',
            {"bar": "baz"},
        )
        assert 'bar="baz"' in out

    def test_rate_with_filename(self):
        out = transform_logql('rate({filename="test"}[1m])', {"bar": "baz"})
        assert out == 'rate({filename="test", bar="baz"}[1m])'

    def test_multiple_matchers_sorted(self):
        out = transform_logql('{cool="breeze"} |= "weather"', {"hot": "sunrays", "dance": "macarena"})
        assert 'dance="macarena"' in out
        assert 'hot="sunrays"' in out

    def test_existing_label_not_overwritten(self):
        out = transform_logql('rate({job="test", env="existing"}[5m])', {"env": "prod"})
        assert 'env="existing"' in out
        assert 'env="prod"' not in out

    def test_empty_matchers(self):
        out = transform_logql('{job="test"}', {})
        assert out == '{job="test"}'


class TestLogQLTransformGrafanaVariables:
    def test_grouping_variable_preserved(self):
        out = transform_logql(
            'sum by ($grouping) (rate({job="$job"}[5m]))',
            {"juju_model": "cos"},
        )
        assert '$grouping' in out
        assert '$job' in out
        assert 'juju_model="cos"' in out

    def test_same_grouping_variable_two_clauses(self):
        matchers = {"cluster": "prod"}
        inp = 'sum by ($grouping) (rate({app="svc1"}[5m])) / sum by ($grouping) (rate({app="svc2"}[5m]))'
        out = transform_logql(inp, matchers)
        assert out.count("$grouping") == 2
        assert 'cluster="prod"' in out


# ---------------------------------------------------------------------------
# LogQL validate-rules
# ---------------------------------------------------------------------------

class TestLogQLValidateRules:
    def _read(self, name: str) -> bytes:
        with open(os.path.join(TESTDATA, "loki_alerts", name), "rb") as f:
            return f.read()

    def test_valid_file(self):
        validate_rules_logql("basic.yaml", self._read("basic.yaml"))

    def test_duplicate_group(self):
        with pytest.raises(ValueError, match='"testgroup" is repeated in the same file'):
            validate_rules_logql("duplicate_group.yaml", self._read("duplicate_group.yaml"))

    def test_bad_expression(self):
        with pytest.raises(ValueError, match="syntax error"):
            validate_rules_logql("bad_expr.yaml", self._read("bad_expr.yaml"))
