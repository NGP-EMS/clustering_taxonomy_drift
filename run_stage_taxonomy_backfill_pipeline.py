#!/usr/bin/env python3

from __future__ import annotations
import shlex
import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def format_command_for_print(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part or "\\" in part else part for part in command)


def run_command(command: list[str], cwd: Optional[str] = None) -> None:
    printable = format_command_for_print(command)

    print("\n" + "=" * 120)
    print(printable)
    print("=" * 120 + "\n")

    result = subprocess.run(
        command,
        cwd=cwd,
        shell=False,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {printable}")


def run_shell_command(command: str, cwd: Optional[str] = None) -> None:
    print("\n" + "=" * 120)
    print(command)
    print("=" * 120 + "\n")

    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {command}")


def read_summary(summary_path: Path) -> dict:
    if not summary_path.exists():
        raise FileNotFoundError(f"Summary file not found: {summary_path}")

    with summary_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def print_summary(summary: dict, label: str) -> None:
    print("\n" + "-" * 120)
    print(label)
    print("-" * 120)
    print(f"dry_run: {summary.get('dry_run')}")
    print(f"rows_scanned: {summary.get('rows_scanned')}")
    print(f"rows_changed: {summary.get('rows_changed')}")
    print(f"rows_unchanged: {summary.get('rows_unchanged')}")
    print(f"rows_error: {summary.get('rows_error')}")
    print(f"unmapped_total_count: {summary.get('unmapped_total_count')}")
    print(f"unmapped_unique_count: {summary.get('unmapped_unique_count')}")
    print(f"duration_seconds: {summary.get('duration_seconds')}")
    print("-" * 120 + "\n")

def build_backfill_command(
    backfill_script: str,
    dry_run: bool,
    workers: int,
    batch_size: int,
    update_page_size: int,
    audit_dir: str,
    log_level: str,
    limit: Optional[int] = None,
    include_already_updated: bool = False,
) -> list[str]:
    parts = [
        sys.executable,
        backfill_script,
        "--dry-run",
        "true" if dry_run else "false",
        "--workers",
        str(workers),
        "--batch-size",
        str(batch_size),
        "--update-page-size",
        str(update_page_size),
        "--audit-dir",
        audit_dir,
        "--log-level",
        log_level,
    ]

    if limit is not None:
        parts.extend(["--limit", str(limit)])

    if include_already_updated:
        parts.append("--include-already-updated")

    return parts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Orchestrate STAGE taxonomy coverage, unresolved label resolution, and final backfill."
    )

    parser.add_argument(
        "--backfill-script",
        default="backfill_clean_taxonomy_to_stage.py",
        help="Path to the existing backfill script.",
    )

    parser.add_argument(
        "--resolver-command",
        default=None,
        help=(
            "Optional command to run hourly/weekly resolver on unmapped labels. "
            "Use {unmapped_csv} and {audit_dir} placeholders if needed."
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=6,
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
    )

    parser.add_argument(
        "--update-page-size",
        type=int,
        default=500,
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for testing. Do not use this for full coverage.",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
    )

    parser.add_argument(
        "--mode",
        choices=[
            "coverage-only",
            "coverage-resolve",
            "coverage-resolve-coverage",
            "apply",
            "full",
        ],
        default="coverage-only",
        help=(
            "coverage-only: full dry-run only. "
            "coverage-resolve: dry-run then resolver. "
            "coverage-resolve-coverage: dry-run, resolver, dry-run again. "
            "apply: real backfill only. "
            "full: dry-run, resolver, dry-run again, then real backfill."
        ),
    )

    parser.add_argument(
        "--max-unmapped-unique-before-apply",
        type=int,
        default=100,
        help="Safety gate for full mode. Apply will stop if unmapped unique count is above this.",
    )

    parser.add_argument(
        "--max-errors-before-apply",
        type=int,
        default=0,
        help="Safety gate for full mode.",
    )

    args = parser.parse_args()

    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base_audit_dir = Path(f"audit_stage_taxonomy_pipeline_{run_id}")
    base_audit_dir.mkdir(parents=True, exist_ok=True)

    coverage_1_dir = base_audit_dir / "01_full_coverage_before_resolve"
    coverage_2_dir = base_audit_dir / "03_full_coverage_after_resolve"
    apply_dir = base_audit_dir / "04_apply_backfill"

    if args.mode in {"coverage-only", "coverage-resolve", "coverage-resolve-coverage", "full"}:
        command = build_backfill_command(
            backfill_script=args.backfill_script,
            dry_run=True,
            workers=args.workers,
            batch_size=args.batch_size,
            update_page_size=args.update_page_size,
            audit_dir=str(coverage_1_dir),
            log_level=args.log_level,
            limit=args.limit,
        )
        run_command(command)

        coverage_1_summary = read_summary(coverage_1_dir / "backfill_summary.json")
        print_summary(coverage_1_summary, "Coverage before resolver")

    if args.mode in {"coverage-only"}:
        print(f"Coverage audit written to: {coverage_1_dir}")
        return 0

    if args.mode in {"coverage-resolve", "coverage-resolve-coverage", "full"}:
        unmapped_csv = coverage_1_dir / "backfill_unmapped_labels.csv"

        if not args.resolver_command:
            print("\nNo resolver command supplied.")
            print("Coverage is complete. Review this file first:")
            print(unmapped_csv)
            print("\nThen rerun with --resolver-command once we plug in your hourly/weekly resolver.")
            return 0

        resolver_command = args.resolver_command.format(
            unmapped_csv=str(unmapped_csv),
            audit_dir=str(base_audit_dir / "02_resolver_output"),
        )
        run_shell_command(resolver_command)

    if args.mode in {"coverage-resolve-coverage", "full"}:
        command = build_backfill_command(
            backfill_script=args.backfill_script,
            dry_run=True,
            workers=args.workers,
            batch_size=args.batch_size,
            update_page_size=args.update_page_size,
            audit_dir=str(coverage_2_dir),
            log_level=args.log_level,
            limit=args.limit,
        )
        run_command(command)

        coverage_2_summary = read_summary(coverage_2_dir / "backfill_summary.json")
        print_summary(coverage_2_summary, "Coverage after resolver")

    if args.mode == "apply":
        command = build_backfill_command(
            backfill_script=args.backfill_script,
            dry_run=False,
            workers=args.workers,
            batch_size=args.batch_size,
            update_page_size=args.update_page_size,
            audit_dir=str(apply_dir),
            log_level=args.log_level,
            limit=args.limit,
        )
        run_command(command)

        apply_summary = read_summary(apply_dir / "backfill_summary.json")
        print_summary(apply_summary, "Apply backfill")
        return 0

    if args.mode == "full":
        coverage_2_summary = read_summary(coverage_2_dir / "backfill_summary.json")

        rows_error = int(coverage_2_summary.get("rows_error") or 0)
        unmapped_unique = int(coverage_2_summary.get("unmapped_unique_count") or 0)

        if rows_error > args.max_errors_before_apply:
            raise RuntimeError(
                f"Stopping before apply. rows_error={rows_error}, "
                f"allowed={args.max_errors_before_apply}"
            )

        if unmapped_unique > args.max_unmapped_unique_before_apply:
            raise RuntimeError(
                f"Stopping before apply. unmapped_unique_count={unmapped_unique}, "
                f"allowed={args.max_unmapped_unique_before_apply}"
            )

        command = build_backfill_command(
            backfill_script=args.backfill_script,
            dry_run=False,
            workers=args.workers,
            batch_size=args.batch_size,
            update_page_size=args.update_page_size,
            audit_dir=str(apply_dir),
            log_level=args.log_level,
            limit=args.limit,
        )
        run_command(command)

        apply_summary = read_summary(apply_dir / "backfill_summary.json")
        print_summary(apply_summary, "Apply backfill")

    print(f"Pipeline audit written to: {base_audit_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())