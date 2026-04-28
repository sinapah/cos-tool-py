"""CLI entry point for cos-tool.

This module ports the command structure from cmd/root/root.go in the
Go cos-tool implementation.

Commands:
  transform       Inject label matchers into a PromQL or LogQL expression.
  validate-rules  Validate Prometheus or Loki alert rule YAML files.
  validate-config Validate a Prometheus configuration file (PromQL only).
"""

from __future__ import annotations

import sys

import click

from ._promql import transform_promql, validate_rules_promql, validate_config_promql
from ._logql import transform_logql, validate_rules_logql


def _parse_label_matchers(label_matchers: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for matcher in label_matchers:
        parts = matcher.split("=", 1)
        if len(parts) != 2:
            raise click.BadParameter(
                f"malformed label matcher: {matcher!r} (expected key=value)"
            )
        result[parts[0]] = parts[1]
    return result


@click.group()
@click.option(
    "--format",
    "-f",
    "fmt",
    default="promql",
    type=click.Choice(["promql", "logql"], case_sensitive=False),
    help="Expression format to use (promql or logql).",
    show_default=True,
)
@click.pass_context
def main(ctx: click.Context, fmt: str) -> None:
    """Validates Prometheus and Loki expressions and adds Juju Topology to label matchers."""
    ctx.ensure_object(dict)
    ctx.obj["format"] = fmt.lower()


@main.command("transform", short_help="Inject label matchers into an expression.")
@click.option(
    "--label-matcher",
    "label_matchers",
    multiple=True,
    metavar="KEY=VALUE",
    help="Label matcher to inject into all vector/stream selectors.",
)
@click.argument("expression")
@click.pass_context
def transform_cmd(ctx: click.Context, label_matchers: tuple[str, ...], expression: str) -> None:
    """Inject label matchers into EXPRESSION."""
    fmt = ctx.obj["format"]
    matchers = _parse_label_matchers(label_matchers)

    try:
        if fmt == "promql":
            output = transform_promql(expression, matchers)
        else:
            output = transform_logql(expression, matchers)
    except (ValueError, Exception) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    click.echo(output, nl=False)


@main.command("validate-rules", short_help="Validate alert rule YAML files.")
@click.argument("rule_files", nargs=-1, required=True, metavar="RULE_FILE...")
@click.pass_context
def validate_rules_cmd(ctx: click.Context, rule_files: tuple[str, ...]) -> None:
    """Validate one or more alert rule YAML files."""
    fmt = ctx.obj["format"]
    exit_code = 0

    for path in rule_files:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            click.echo(str(exc), err=True)
            exit_code = 1
            continue

        try:
            if fmt == "promql":
                validate_rules_promql(path, data)
            else:
                validate_rules_logql(path, data)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            exit_code = 1

    sys.exit(exit_code)


@main.command("validate-config", short_help="Validate a Prometheus configuration file.")
@click.argument("config_files", nargs=-1, required=True, metavar="CONFIG_FILE...")
@click.pass_context
def validate_config_cmd(ctx: click.Context, config_files: tuple[str, ...]) -> None:
    """Validate one or more Prometheus configuration files."""
    fmt = ctx.obj["format"]
    if fmt == "logql":
        click.echo("Loki not supported for validate-config", err=True)
        sys.exit(1)

    exit_code = 0
    for path in config_files:
        try:
            validate_config_promql(path)
        except (ValueError, OSError) as exc:
            click.echo(str(exc), err=True)
            exit_code = 1

    sys.exit(exit_code)


# Aliases matching the Go implementation
main.add_command(validate_rules_cmd, "validate")
main.add_command(validate_rules_cmd, "lint")
main.add_command(validate_rules_cmd, "v")
main.add_command(validate_rules_cmd, "l")
main.add_command(transform_cmd, "t")
