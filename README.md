# cos-tool (Python)

[![PyPI](https://img.shields.io/pypi/v/cos-tool)](https://pypi.org/project/cos-tool/)

Python reimplementation of [cos-tool](https://github.com/canonical/cos-tool).

Transforms PromQL/LogQL expressions on the fly, and validates that Alert rules
can be loaded successfully by either Prometheus/Mimir or Loki.

## Installation

```bash
pip install cos-tool
```

## Usage

### PromQL transform

```bash
$ cos-tool --format promql transform \
    --label-matcher juju_model=cos \
    --label-matcher juju_model_uuid=12345 \
    --label-matcher juju_application=proxy \
    --label-matcher juju_unit=proxy/1 \
    -- 'rate(http_requests_total{job="myjob"}[5m]) > 0.5'
```

Outputs:

```
rate(http_requests_total{job="myjob",juju_application="proxy",juju_model="cos",juju_model_uuid="12345",juju_unit="proxy/1"}[5m]) > 0.5
```

### LogQL transform

```bash
$ cos-tool --format logql transform \
    --label-matcher juju_model=cos \
    --label-matcher juju_model_uuid=12345 \
    --label-matcher juju_application=proxy \
    --label-matcher juju_unit=proxy/1 \
    -- 'rate({filename="myfile"}[1m])'
```

### Alert rule validation

```bash
$ cos-tool [-f logql] validate-rules rule_file.yaml [rule_file2.yaml ...]
```

### Prometheus config validation

```bash
$ cos-tool validate-config prometheus.yml
```

## Development

```bash
pip install -e ".[dev]"
pytest
```
