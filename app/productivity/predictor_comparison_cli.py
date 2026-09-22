"""
predictor_comparison_cli.py

Reproducible CLI to evaluate the median duration predictor and an
evidence-gated scikit-learn regression model against the same held-out,
chronologically split slice of real execution history, and (with
--save-model) persist the trained model + activation decision for runtime
use by app/productivity/ml_prediction.py.

All the actual work is done by app/productivity/ml_evaluation.py; this
module only parses arguments, wires up the repository, and formats output.
Kept as a separate module from app/productivity/report_cli.py because the
two tools' flags would collide in meaning (--period there means "report
window"; here the analogous concept is "how much history to train/evaluate
on", which this CLI always takes as everything in the database).

Usage:
    python -m app.productivity.predictor_comparison_cli
    python -m app.productivity.predictor_comparison_cli --format json
    python -m app.productivity.predictor_comparison_cli --save-model
    python -m app.productivity.predictor_comparison_cli --db-path path/to/executions.db
    python -m app.productivity.predictor_comparison_cli \\
        --min-total-samples 20 --min-train-samples 12 --min-test-samples 5
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from app.execution.db import get_connection
from app.execution.repository import ExecutionRepository
from app.productivity.ml_evaluation import (
    MLActivationDecision,
    MLGateConfig,
    decide_activation,
    evaluate_predictors,
)
from app.productivity.ml_persistence import save_model_artifact
from app.productivity.reporting import ProductivityService

_DEFAULT_GATE = MLGateConfig()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the original estimate, the median duration predictor, and an ML model "
            "on the same chronologically held-out slice of real execution history."
        )
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to the execution database (default: the configured local data directory).",
    )
    parser.add_argument(
        "--min-total-samples",
        type=int,
        default=_DEFAULT_GATE.min_total_samples,
        help=(
            "Minimum total completed+plausible observations required to train/evaluate ML "
            f"(default: {_DEFAULT_GATE.min_total_samples})."
        ),
    )
    parser.add_argument(
        "--min-train-samples",
        type=int,
        default=_DEFAULT_GATE.min_train_samples,
        help=f"Minimum training-split rows required (default: {_DEFAULT_GATE.min_train_samples}).",
    )
    parser.add_argument(
        "--min-test-samples",
        type=int,
        default=_DEFAULT_GATE.min_test_samples,
        help=f"Minimum held-out test-split rows required (default: {_DEFAULT_GATE.min_test_samples}).",
    )
    parser.add_argument(
        "--test-fraction",
        type=float,
        default=_DEFAULT_GATE.test_fraction,
        help=f"Fraction of chronologically-latest rows held out for testing (default: {_DEFAULT_GATE.test_fraction}).",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format (default: text).")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="File to write the report to (default: stdout).",
    )
    parser.add_argument(
        "--save-model",
        action="store_true",
        help=(
            "If the activation gate passes, persist the trained model + activation decision "
            "for runtime use. Without this flag the CLI only evaluates and reports -- it never "
            "writes a model artifact."
        ),
    )
    return parser


def render_decision_text(decision: MLActivationDecision) -> str:
    """Deterministic, human-readable rendering of an MLActivationDecision."""
    comparison = decision.comparison
    lines = ["Schedule Maxing Duration Predictor Comparison"]

    if comparison is None:
        lines.append("No comparison could be computed.")
        lines.append(f"Reason: {decision.reason}")
        return "\n".join(lines)

    lines += [
        f"Generated at: {comparison.generated_at}",
        f"Train samples: {comparison.train_sample_count}",
        f"Test samples: {comparison.test_sample_count}",
        f"Chronological cutoff: index={comparison.cutoff_index} created_at={comparison.cutoff_created_at or 'n/a'}",
        "",
    ]

    for label, metrics in (
        ("Original estimate", comparison.original_estimate),
        ("Median predictor", comparison.median_predictor),
        ("ML predictor", comparison.ml_predictor),
    ):
        lines.append(f"{label}:")
        if metrics is None:
            lines.append("  (not evaluated)")
            continue
        lines.append(
            f"  n={metrics.sample_count} mae={metrics.mae_minutes:g}m "
            f"median_ae={metrics.median_absolute_error_minutes:g}m "
            f"within_15m={metrics.within_15_min_rate:.1%} within_30m={metrics.within_30_min_rate:.1%}"
        )

    if comparison.insufficient_history_reason:
        lines.append("")
        lines.append(f"Insufficient history: {comparison.insufficient_history_reason}")

    lines += [
        "",
        f"Activation decision: {'ENABLED' if decision.is_active else 'DISABLED'}",
        f"Reason: {decision.reason}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    gate_config = MLGateConfig(
        min_total_samples=args.min_total_samples,
        min_train_samples=args.min_train_samples,
        min_test_samples=args.min_test_samples,
        test_fraction=args.test_fraction,
    )

    connection = get_connection(args.db_path)
    try:
        service = ProductivityService(ExecutionRepository(connection))
        observations = service.build_observations(period="all_time")
    finally:
        connection.close()

    comparison, pipeline = evaluate_predictors(observations, gate_config=gate_config)
    decision = decide_activation(comparison)

    if args.save_model and pipeline is not None and decision.is_active:
        save_model_artifact(pipeline, decision, trained_at=datetime.now(timezone.utc).isoformat())

    if args.format == "text":
        text = render_decision_text(decision)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        payload = decision.model_dump_json(indent=2)
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
