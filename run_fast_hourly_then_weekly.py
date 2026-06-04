#!/usr/bin/env python3
"""
run_fast_hourly_then_weekly.py

Fast runner for validating the hourly mapper + weekly maintenance over one target day.

Default behavior:
  - Runs production_cluster_mapper_hourly.py ONCE for the full target day window.
  - Then runs weekly_taxonomy_maintenance.py as DRY RUN for the same window.

Why this exists:
  Running the hourly script 24 separate times reloads clusters, exact maps, and the
  embedding model 24 times. For validation/backfill-style testing, one full-day
  window is much faster and still writes/upserts the same output table.

Optional:
  --hourly-mode parallel-hours --max-workers 2
    Runs hourly windows in parallel. Use this only if you intentionally want to
    test hour-by-hour execution. It can be slower on GPU/OpenVINO because each
    process loads its own model and reference data.

Examples:
  python run_fast_hourly_then_weekly.py --date 2026-06-02 --timezone Asia/Dubai --env-file .env

  python run_fast_hourly_then_weekly.py --date 2026-06-02 --timezone Asia/Dubai --env-file .env --skip-weekly

  python run_fast_hourly_then_weekly.py --date 2026-06-02 --timezone Asia/Dubai --env-file .env --timestamp-column call_date_time

  python run_fast_hourly_then_weekly.py --date 2026-06-02 --timezone Asia/Dubai --env-file .env --hourly-mode parallel-hours --max-workers 2 --start-hour 15

  python run_fast_hourly_then_weekly.py --date 2026-06-02 --timezone Asia/Dubai --env-file .env --skip-hourly --weekly-apply
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Sequence

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
        description="Fast day-window runner for hourly mapper, then weekly maintenance."
    )

    parser.add_argument("--date", help="Target local date in YYYY-MM-DD. Defaults to yesterday in --timezone.")
    parser.add_argument("--timezone", default="Asia/Dubai", help="Timezone used to define the target day. Default: Asia/Dubai.")
    parser.add_argument("--scripts-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--env-file", default=".env")

    parser.add_argument("--input-db", choices=["app", "local"], default="app")
    parser.add_argument("--input-table", default="ngp_call_classification")
    parser.add_argument("--timestamp-column", default="created_at")
    parser.add_argument("--call-id-column", default="call_id")
    parser.add_argument("--fields", default=DEFAULT_FIELDS)
    parser.add_argument("--cluster-version", default=None)

    parser.add_argument(
        "--hourly-mode",
        choices=["full-day", "parallel-hours"],
        default="full-day",
        help="full-day is fastest for validation because references/model load once. Default: full-day.",
    )
    parser.add_argument("--max-workers", type=int, default=2, help="Only used with --hourly-mode parallel-hours. Default: 2.")
    parser.add_argument("--start-hour", type=int, default=1, help="1-based local hour index to start from, inclusive. Default: 1.")
    parser.add_argument("--end-hour", type=int, default=24, help="1-based local hour index to end at, inclusive. Default: 24.")

    parser.add_argument("--output-root", default="taxonomy_cluster_output/fast_hourly_weekly_run")
    parser.add_argument("--preview-csv", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--hourly-no-write", action="store_true", help="Run hourly without writing output rows.")
    parser.add_argument("--skip-hourly", action="store_true")
    parser.add_argument("--skip-weekly", action="store_true")
    parser.add_argument("--weekly-apply", action="store_true", help="Apply weekly changes. Default is dry run.")
    parser.add_argument("--persist-weekly-dry-run-logs", action="store_true")
    parser.add_argument("--continue-on-hourly-error", action="store_true")
    parser.add_argument("--dry-run-commands", action="store_true")
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument("--hourly-extra-args", default="")
    parser.add_argument("--weekly-extra-args", default="")

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


def target_date(args: argparse.Namespace, tz) -> date:
    if args.date:
        return datetime.strptime(args.date, "%Y-%m-%d").date()
    return datetime.now(tz).date() - timedelta(days=1)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def hourly_windows(day: date, tz) -> list[tuple[int, datetime, datetime]]:
    start_local = datetime.combine(day, time(0, 0, 0), tzinfo=tz)
    return [(i + 1, start_local + timedelta(hours=i), start_local + timedelta(hours=i + 1)) for i in range(24)]


def quote_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


def run_to_log(cmd: Sequence[str], log_path: Path, dry_run: bool) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    printable = quote_cmd(cmd)
    print("\n" + "=" * 100)
    print(printable)
    print("=" * 100)

    with log_path.open("w", encoding="utf-8") as log:
        log.write(printable + "\n\n")
        log.flush()
        if dry_run:
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


def run_to_log_quiet(cmd: Sequence[str], log_path: Path, dry_run: bool) -> int:
    """For parallel mode: avoid interleaved stdout by writing each process to its own log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    printable = quote_cmd(cmd)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(printable + "\n\n")
        log.flush()
        if dry_run:
            log.write("DRY RUN COMMAND ONLY. Not executed.\n")
            return 0
        completed = subprocess.run(list(cmd), stdout=log, stderr=subprocess.STDOUT, text=True)
        return int(completed.returncode)


def base_hourly_cmd(args: argparse.Namespace, hourly_script: Path) -> list[str]:
    cmd = [
        args.python_executable,
        str(hourly_script),
        "--input-db", args.input_db,
        "--input-table", args.input_table,
        "--timestamp-column", args.timestamp_column,
        "--call-id-column", args.call_id_column,
        "--fields", args.fields,
        "--env-file", args.env_file,
        "--ensure-schema",
    ]
    cmd.append("--no-write-output" if args.hourly_no_write else "--write-output")
    if args.cluster_version:
        cmd.extend(["--cluster-version", args.cluster_version])
    if args.hourly_extra_args.strip():
        cmd.extend(shlex.split(args.hourly_extra_args))
    return cmd


def hour_label(prefix: str, start_utc: str, end_utc: str) -> str:
    clean_start = start_utc.replace(":", "").replace("-", "").replace("Z", "Z")
    clean_end = end_utc.replace(":", "").replace("-", "").replace("Z", "Z")
    return f"{prefix}_{clean_start}_to_{clean_end}"


def main() -> int:
    args = parse_args()
    if args.max_workers < 1:
        raise ValueError("--max-workers must be >= 1")
    if args.start_hour < 1 or args.end_hour > 24 or args.start_hour > args.end_hour:
        raise ValueError("Use 1 <= --start-hour <= --end-hour <= 24")

    scripts_dir = Path(args.scripts_dir).resolve()
    hourly_script = scripts_dir / "production_cluster_mapper_hourly_ngp.py"
    weekly_script = scripts_dir / "weekly_taxonomy_maintenance.py"
    if not hourly_script.exists():
        raise FileNotFoundError(f"Hourly script not found: {hourly_script}")
    if not weekly_script.exists():
        raise FileNotFoundError(f"Weekly script not found: {weekly_script}")

    tz = resolve_tz(args.timezone)
    day = target_date(args, tz)
    windows = hourly_windows(day, tz)
    selected_windows = [w for w in windows if args.start_hour <= w[0] <= args.end_hour]

    full_day_start_utc = iso_utc(windows[0][1])
    full_day_end_utc = iso_utc(windows[-1][2])
    selected_start_utc = iso_utc(selected_windows[0][1])
    selected_end_utc = iso_utc(selected_windows[-1][2])

    safe_tz = args.timezone.replace("/", "_").replace(" ", "_")
    run_dir = Path(args.output_root) / f"{day.isoformat()}_{safe_tz}"
    log_dir = run_dir / "logs"
    preview_dir = run_dir / "previews"
    log_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    print(f"Target local day: {day.isoformat()} [{args.timezone}]")
    print(f"Full UTC coverage: {full_day_start_utc} -> {full_day_end_utc}")
    print(f"Selected UTC coverage: {selected_start_utc} -> {selected_end_utc}")
    print(f"Run directory: {run_dir}")
    print(f"Hourly mode: {args.hourly_mode}")
    if args.hourly_mode == "parallel-hours":
        print(f"Parallel workers: {args.max_workers}")
        print("Note: parallel GPU/OpenVINO processes may be slower or memory-heavy because each process loads its own model.")
    print(f"Hourly write mode: {'NO WRITE' if args.hourly_no_write else 'WRITE/UPSERT'}")
    print(f"Weekly mode: {'SKIPPED' if args.skip_weekly else ('APPLY' if args.weekly_apply else 'DRY RUN')}")

    summary: dict[str, object] = {
        "target_date": day.isoformat(),
        "timezone": args.timezone,
        "full_day_start_utc": full_day_start_utc,
        "full_day_end_utc": full_day_end_utc,
        "selected_start_utc": selected_start_utc,
        "selected_end_utc": selected_end_utc,
        "hourly_mode": args.hourly_mode,
        "hourly_write_output": not args.hourly_no_write,
        "weekly_apply": bool(args.weekly_apply),
        "hourly_runs": [],
        "weekly_run": None,
    }

    base_cmd = base_hourly_cmd(args, hourly_script)

    if not args.skip_hourly:
        if args.hourly_mode == "full-day":
            cmd = list(base_cmd)
            cmd.extend(["--window-start-utc", selected_start_utc, "--window-end-utc", selected_end_utc])
            if args.preview_csv:
                cmd.extend(["--preview-csv", str(preview_dir / "hourly_selected_window_preview.csv")])
            rc = run_to_log(cmd, log_dir / "hourly_selected_window.log", args.dry_run_commands)
            summary["hourly_runs"] = [{
                "mode": "full-day",
                "window_start_utc": selected_start_utc,
                "window_end_utc": selected_end_utc,
                "return_code": rc,
                "log_path": str(log_dir / "hourly_selected_window.log"),
            }]
            (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            if rc != 0:
                return rc

        else:
            futures = {}
            hourly_runs: list[dict[str, object]] = []
            with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
                for idx, start_local, end_local in selected_windows:
                    start_utc = iso_utc(start_local)
                    end_utc = iso_utc(end_local)
                    label = hour_label(f"hour_{idx:02d}", start_utc, end_utc)
                    log_path = log_dir / f"{label}.log"
                    cmd = list(base_cmd)
                    cmd.extend(["--window-start-utc", start_utc, "--window-end-utc", end_utc])
                    if args.preview_csv:
                        cmd.extend(["--preview-csv", str(preview_dir / f"{label}.csv")])
                    print(f"Queued hour {idx:02d}: {start_utc} -> {end_utc}")
                    fut = pool.submit(run_to_log_quiet, cmd, log_path, args.dry_run_commands)
                    futures[fut] = (idx, start_local, end_local, start_utc, end_utc, log_path)

                failures = 0
                for fut in as_completed(futures):
                    idx, start_local, end_local, start_utc, end_utc, log_path = futures[fut]
                    rc = fut.result()
                    print(f"Completed hour {idx:02d}: return_code={rc} log={log_path}")
                    hourly_runs.append({
                        "hour_index": idx,
                        "local_start": start_local.isoformat(),
                        "local_end": end_local.isoformat(),
                        "window_start_utc": start_utc,
                        "window_end_utc": end_utc,
                        "return_code": rc,
                        "log_path": str(log_path),
                    })
                    if rc != 0:
                        failures += 1

            hourly_runs.sort(key=lambda x: int(x["hour_index"]))
            summary["hourly_runs"] = hourly_runs
            (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            if failures and not args.continue_on_hourly_error:
                print(f"Hourly failures: {failures}. Not running weekly.")
                return 1

    if not args.skip_weekly:
        weekly_cmd = [
            args.python_executable,
            str(weekly_script),
            "--env-file", args.env_file,
            "--window-start", full_day_start_utc,
            "--window-end", full_day_end_utc,
        ]
        if args.weekly_apply:
            weekly_cmd.append("--apply")
        elif args.persist_weekly_dry_run_logs:
            weekly_cmd.append("--persist-dry-run-logs")
        if args.weekly_extra_args.strip():
            weekly_cmd.extend(shlex.split(args.weekly_extra_args))

        rc = run_to_log(weekly_cmd, log_dir / "weekly_maintenance.log", args.dry_run_commands)
        summary["weekly_run"] = {
            "window_start_utc": full_day_start_utc,
            "window_end_utc": full_day_end_utc,
            "apply": bool(args.weekly_apply),
            "return_code": rc,
            "log_path": str(log_dir / "weekly_maintenance.log"),
        }
        (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        if rc != 0:
            return rc

    print("\nDone.")
    print(f"Summary: {run_dir / 'run_summary.json'}")
    print(f"Logs: {log_dir}")
    print(f"Previews: {preview_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
