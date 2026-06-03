#!/usr/bin/env python3
"""
run_yesterday_hourly_then_weekly.py

Runs production_cluster_mapper_hourly.py one hour at a time for a full local day,
then runs weekly_taxonomy_maintenance.py for the same UTC window.

Default target day:
    yesterday in Asia/Dubai timezone

Default behavior:
    - Hourly mapper writes/upserts output rows.
    - Weekly maintenance runs as DRY RUN only.

Examples:
    python run_yesterday_hourly_then_weekly.py --env-file .env

    python run_yesterday_hourly_then_weekly.py --date 2026-06-02 --timezone Asia/Dubai --env-file .env

    python run_yesterday_hourly_then_weekly.py --date 2026-06-02 --timezone UTC --env-file .env

    python run_yesterday_hourly_then_weekly.py --date 2026-06-02 --weekly-apply --env-file .env

    python run_yesterday_hourly_then_weekly.py --date 2026-06-02 --hourly-no-write --skip-weekly --env-file .env
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


DEFAULT_FIELDS = (
    "call_type,call_type_sub,main_reason,main_reason_sub,outcome,outcome_sub,"
    "next_step,coaching_tags,descriptive_keywords,additional_tags"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hourly mapper 24 times for one day, then run weekly maintenance for the same window."
    )

    parser.add_argument(
        "--date",
        help="Target local date in YYYY-MM-DD. Defaults to yesterday in --timezone.",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Dubai",
        help="Timezone used to define the target day. Use UTC if your DB day should be UTC. Default: Asia/Dubai.",
    )
    parser.add_argument(
        "--scripts-dir",
        default=str(Path(__file__).resolve().parent),
        help="Directory containing production_cluster_mapper_hourly.py and weekly_taxonomy_maintenance.py.",
    )
    parser.add_argument("--env-file", default=".env")

    parser.add_argument("--input-db", choices=["app", "local"], default="app")
    parser.add_argument("--input-table", default="ngp_call_classification")
    parser.add_argument("--timestamp-column", default="created_at")
    parser.add_argument("--call-id-column", default="call_id")
    parser.add_argument("--fields", default=DEFAULT_FIELDS)
    parser.add_argument("--cluster-version", default=None)

    parser.add_argument(
        "--output-root",
        default="taxonomy_cluster_output/yesterday_hourly_weekly_run",
        help="Directory for logs, previews, and summary JSON.",
    )
    parser.add_argument(
        "--preview-csv",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write one hourly preview CSV per hour. Default: true.",
    )
    parser.add_argument(
        "--hourly-no-write",
        action="store_true",
        help="Run hourly mapper without writing/upserting output rows.",
    )
    parser.add_argument(
        "--skip-hourly",
        action="store_true",
        help="Skip the 24 hourly mapper runs.",
    )
    parser.add_argument(
        "--skip-weekly",
        action="store_true",
        help="Skip weekly maintenance after hourly runs.",
    )
    parser.add_argument(
        "--weekly-apply",
        action="store_true",
        help="Apply weekly repairs/resolutions. Default is weekly dry run only.",
    )
    parser.add_argument(
        "--persist-weekly-dry-run-logs",
        action="store_true",
        help="Persist weekly dry-run logs into weekly tables. Default weekly dry run prints summary only.",
    )
    parser.add_argument(
        "--continue-on-hourly-error",
        action="store_true",
        help="Continue remaining hourly windows even if one hour fails. Default: stop on first failure.",
    )
    parser.add_argument(
        "--dry-run-commands",
        action="store_true",
        help="Print commands only; do not execute anything.",
    )
    parser.add_argument(
        "--python-executable",
        default=sys.executable,
        help="Python executable used to run the hourly/weekly scripts. Default: current Python.",
    )
    parser.add_argument(
        "--hourly-extra-args",
        default="",
        help="Extra args appended to each hourly command, for example: '--device cpu --top-k 10'.",
    )
    parser.add_argument(
        "--weekly-extra-args",
        default="",
        help="Extra args appended to the weekly command, for example: '--standard-naming-method deterministic'.",
    )

    return parser.parse_args()


def resolve_tz(tz_name: str):
    if tz_name.upper() == "UTC":
        return timezone.utc
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is unavailable. Use Python 3.9+ or pass --timezone UTC.")
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"Invalid timezone: {tz_name}") from exc


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def target_date(args: argparse.Namespace, tz) -> date:
    if args.date:
        return datetime.strptime(args.date, "%Y-%m-%d").date()
    return (datetime.now(tz).date() - timedelta(days=1))


def hourly_windows_for_day(day: date, tz) -> list[tuple[datetime, datetime]]:
    start_local = datetime.combine(day, time(0, 0, 0), tzinfo=tz)
    return [(start_local + timedelta(hours=h), start_local + timedelta(hours=h + 1)) for h in range(24)]


def quote_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def run_logged(cmd: Sequence[str], log_path: Path, dry_run_commands: bool) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    printable = quote_cmd(cmd)

    print("\n" + "=" * 100)
    print(printable)
    print("=" * 100)

    with log_path.open("w", encoding="utf-8") as log:
        log.write(printable + "\n\n")
        log.flush()

        if dry_run_commands:
            log.write("DRY RUN COMMAND ONLY. Not executed.\n")
            return 0

        process = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log.write(line)
        return process.wait()


def base_hourly_cmd(args: argparse.Namespace, hourly_script: Path) -> list[str]:
    cmd = [
        args.python_executable,
        str(hourly_script),
        "--input-db",
        args.input_db,
        "--input-table",
        args.input_table,
        "--timestamp-column",
        args.timestamp_column,
        "--call-id-column",
        args.call_id_column,
        "--fields",
        args.fields,
        "--env-file",
        args.env_file,
        "--ensure-schema",
    ]

    if args.hourly_no_write:
        cmd.append("--no-write-output")
    else:
        cmd.append("--write-output")

    if args.cluster_version:
        cmd.extend(["--cluster-version", args.cluster_version])

    if args.hourly_extra_args.strip():
        cmd.extend(shlex.split(args.hourly_extra_args))

    return cmd


def main() -> int:
    args = parse_args()

    scripts_dir = Path(args.scripts_dir).resolve()
    hourly_script = scripts_dir / "production_cluster_mapper_hourly_ngp.py"
    weekly_script =  scripts_dir / "weekly_taxonomy_maintenance.py"

    if not hourly_script.exists():
        raise FileNotFoundError(f"Hourly script not found: {hourly_script}")
    if not weekly_script.exists():
        raise FileNotFoundError(f"Weekly script not found: {weekly_script}")

    tz = resolve_tz(args.timezone)
    day = target_date(args, tz)
    windows = hourly_windows_for_day(day, tz)
    day_start_utc = iso_utc(windows[0][0])
    day_end_utc = iso_utc(windows[-1][1])

    safe_tz = args.timezone.replace("/", "_").replace(" ", "_")
    run_dir = Path(args.output_root) / f"{day.isoformat()}_{safe_tz}"
    log_dir = run_dir / "logs"
    preview_dir = run_dir / "hourly_previews"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    print(f"Target local day: {day.isoformat()} [{args.timezone}]")
    print(f"UTC coverage: {day_start_utc} -> {day_end_utc}")
    print(f"Run directory: {run_dir}")
    print(f"Hourly write mode: {'NO WRITE' if args.hourly_no_write else 'WRITE/UPSERT'}")
    print(f"Weekly mode: {'SKIPPED' if args.skip_weekly else ('APPLY' if args.weekly_apply else 'DRY RUN')}")

    summary: dict[str, object] = {
        "target_date": day.isoformat(),
        "timezone": args.timezone,
        "day_start_utc": day_start_utc,
        "day_end_utc": day_end_utc,
        "hourly_write_output": not args.hourly_no_write,
        "weekly_apply": bool(args.weekly_apply),
        "hourly_runs": [],
        "weekly_run": None,
    }

    hourly_failures = 0
    if not args.skip_hourly:
        base_cmd = base_hourly_cmd(args, hourly_script)
        for idx, (start_local, end_local) in enumerate(windows, start=1):
            start_utc = iso_utc(start_local)
            end_utc = iso_utc(end_local)
            label = f"hour_{idx:02d}_{start_utc.replace(':', '').replace('-', '').replace('Z', 'Z')}_to_{end_utc.replace(':', '').replace('-', '').replace('Z', 'Z')}"
            log_path = log_dir / f"{label}.log"

            cmd = list(base_cmd)
            cmd.extend(["--window-start-utc", start_utc, "--window-end-utc", end_utc])
            if args.preview_csv:
                cmd.extend(["--preview-csv", str(preview_dir / f"{label}.csv")])

            return_code = run_logged(cmd, log_path, args.dry_run_commands)
            run_info = {
                "hour_index": idx,
                "local_start": start_local.isoformat(),
                "local_end": end_local.isoformat(),
                "window_start_utc": start_utc,
                "window_end_utc": end_utc,
                "return_code": return_code,
                "log_path": str(log_path),
            }
            cast_runs = summary["hourly_runs"]
            assert isinstance(cast_runs, list)
            cast_runs.append(run_info)

            summary_path = run_dir / "run_summary.json"
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

            if return_code != 0:
                hourly_failures += 1
                print(f"Hourly window failed: {start_utc} -> {end_utc} return_code={return_code}")
                if not args.continue_on_hourly_error:
                    print("Stopping because --continue-on-hourly-error was not provided.")
                    return return_code

    if hourly_failures:
        print(f"Hourly completed with failures: {hourly_failures}")
        if not args.continue_on_hourly_error:
            return 1

    if not args.skip_weekly:
        weekly_cmd = [
            args.python_executable,
            str(weekly_script),
            "--env-file",
            args.env_file,
            "--window-start",
            day_start_utc,
            "--window-end",
            day_end_utc,
        ]
        if args.weekly_apply:
            weekly_cmd.append("--apply")
        elif args.persist_weekly_dry_run_logs:
            weekly_cmd.append("--persist-dry-run-logs")

        if args.weekly_extra_args.strip():
            weekly_cmd.extend(shlex.split(args.weekly_extra_args))

        weekly_log_path = log_dir / "weekly_maintenance.log"
        weekly_return_code = run_logged(weekly_cmd, weekly_log_path, args.dry_run_commands)
        summary["weekly_run"] = {
            "window_start_utc": day_start_utc,
            "window_end_utc": day_end_utc,
            "apply": bool(args.weekly_apply),
            "return_code": weekly_return_code,
            "log_path": str(weekly_log_path),
        }
        (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        if weekly_return_code != 0:
            return weekly_return_code

    print("\nDone.")
    print(f"Summary: {run_dir / 'run_summary.json'}")
    print(f"Logs: {log_dir}")
    if args.preview_csv:
        print(f"Hourly previews: {preview_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
