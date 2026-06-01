import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from pandas.errors import EmptyDataError

ROOT = Path.cwd()

APPLY_SCRIPT = ROOT / "apply_business_review_remaining_cleanup.py"
REBUILD_SCRIPT = ROOT / "rebuild_all_active_cluster_centroids_fixed.py"
AUDIT_SCRIPT = ROOT / "audit_generic_actor_role_reversals_all_fields.py"
TRIAGE_SCRIPT = ROOT / "triage_generic_actor_role_reversal_results.py"

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
BASE_OUT = ROOT / "outputs" / f"remaining_business_cleanup_convergence_{RUN_ID}"


ACTIONABLE_TRIAGE = {
    "REVIEW_SPLIT_BROAD_MIXED_CLUSTER",
    "REVIEW_SPLIT",
    "REVIEW_HIGH_RISK_DIRECTIONAL",
}


def run_cmd(cmd, check=True):
    print("\n" + "=" * 120)
    print("RUN:", " ".join(str(x) for x in cmd))
    print("=" * 120)

    result = subprocess.run(
        [str(x) for x in cmd],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    print(result.stdout)

    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(str(x) for x in cmd)}")

    return result


def require_file(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")


def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()

    if path.stat().st_size == 0:
        return pd.DataFrame()

    try:
        return pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame()

def validate_cleanup_script_has_latest_rules():
    text = APPLY_SCRIPT.read_text(encoding="utf-8", errors="ignore")

    required_markers = [
        "manual_customer_placed_agent_on_hold",
        "manual_customer_dismissive_or_critical_to_agent",
        "manual_agent_call_termination",
        "manual_agent_disputed_previous_contact",
        "manual_agent_ghosted_customer",
        "manual_customer_referenced_agent_or_contact",
        "manual_customer_disputed_broker_relationship",
        "manual_customer_agent_identity_mismatch",
    ]

    missing = [m for m in required_markers if m not in text]

    if missing:
        raise RuntimeError(
            "apply_business_review_remaining_cleanup.py does not contain the latest batch-3 rules. "
            "Missing markers: " + ", ".join(missing)
        )
def get_plan_count(cleanup_out: Path):
    plan_path = cleanup_out / "01_business_review_cleanup_plan.csv"
    summary_path = cleanup_out / "03_business_review_cleanup_summary.csv"

    plan_df = read_csv_if_exists(plan_path)
    summary_df = read_csv_if_exists(summary_path)

    rows = int(len(plan_df)) if not plan_df.empty else 0

    occurrences = 0
    if not summary_df.empty and "occurrences" in summary_df.columns:
        occurrences = int(pd.to_numeric(summary_df["occurrences"], errors="coerce").fillna(0).sum())
    elif not plan_df.empty and "value_count" in plan_df.columns:
        occurrences = int(pd.to_numeric(plan_df["value_count"], errors="coerce").fillna(0).sum())

    return rows, occurrences, plan_path, summary_path


def run_cleanup_dry(round_dir: Path):
    run_cmd([
        sys.executable,
        APPLY_SCRIPT,
        "--out",
        round_dir,
    ])

    rows, occurrences, plan_path, summary_path = get_plan_count(round_dir)

    print("\nCleanup dry-run result")
    print(f"Rows selected: {rows:,}")
    print(f"Occurrences selected: {occurrences:,}")
    print(f"Plan: {plan_path}")
    print(f"Summary: {summary_path}")

    return rows, occurrences


def apply_cleanup(round_dir: Path):
    run_cmd([
        sys.executable,
        APPLY_SCRIPT,
        "--out",
        round_dir,
        "--apply",
    ])


def rebuild_centroids():
    run_cmd([
        sys.executable,
        REBUILD_SCRIPT,
        "--field",
        "additional_tags",
        "--apply",
    ])


def run_audit_and_triage(round_dir: Path, round_number: int):
    audit_out = round_dir / f"generic_after_round_{round_number}"
    triage_out = round_dir / f"generic_triage_after_round_{round_number}"

    run_cmd([
        sys.executable,
        AUDIT_SCRIPT,
        "--out",
        audit_out,
    ])

    run_cmd([
        sys.executable,
        TRIAGE_SCRIPT,
        "--generic-dir",
        audit_out,
        "--out",
        triage_out,
    ])

    summary_path = triage_out / "07_triage_summary.csv"
    review_path = triage_out / "04_review_required_clusters.csv"
    labels_path = triage_out / "02_triaged_generic_labels.csv"

    summary_df = read_csv_if_exists(summary_path)

    print("\nTriage summary")
    if summary_df.empty:
        print("No triage summary found.")
    else:
        print(summary_df.to_string(index=False))

    return {
        "audit_out": audit_out,
        "triage_out": triage_out,
        "summary_path": summary_path,
        "review_path": review_path,
        "labels_path": labels_path,
        "summary_df": summary_df,
    }


def summarize_actionable(summary_df: pd.DataFrame):
    if summary_df.empty or "triage_decision" not in summary_df.columns:
        return pd.DataFrame()

    return summary_df[summary_df["triage_decision"].isin(ACTIONABLE_TRIAGE)].copy()


def export_final_unresolved(last_result: dict):
    final_dir = BASE_OUT / "final_unresolved_review"
    final_dir.mkdir(parents=True, exist_ok=True)

    summary_df = read_csv_if_exists(last_result["summary_path"])
    review_df = read_csv_if_exists(last_result["review_path"])
    labels_df = read_csv_if_exists(last_result["labels_path"])

    if not summary_df.empty:
        summary_df.to_csv(final_dir / "01_final_triage_summary.csv", index=False)

    if not review_df.empty:
        actionable_review = review_df[
            review_df.get("triage_decision", pd.Series(dtype=str)).isin(ACTIONABLE_TRIAGE)
        ].copy()
        actionable_review.to_csv(final_dir / "02_final_actionable_review_clusters.csv", index=False)

        strict315 = review_df[review_df.get("cluster_id", pd.Series(dtype=str)).eq("strict_315")].copy()
        strict315.to_csv(final_dir / "03_final_strict315_review_clusters.csv", index=False)

    if not labels_df.empty:
        actionable_labels = labels_df[
            labels_df.get("triage_decision", pd.Series(dtype=str)).isin(ACTIONABLE_TRIAGE)
        ].copy()
        actionable_labels.to_csv(final_dir / "04_final_actionable_labels.csv", index=False)

        strict315_labels = labels_df[labels_df.get("cluster_id", pd.Series(dtype=str)).eq("strict_315")].copy()
        strict315_labels.to_csv(final_dir / "05_final_strict315_labels.csv", index=False)

    report_path = final_dir / "06_final_remaining_cleanup_report.md"

    lines = [
        "# Final Remaining Cleanup Report",
        "",
        "This report was generated by `run_remaining_business_cleanup_to_convergence.py`.",
        "",
        "## Interpretation",
        "",
        "- All cleanup rules currently defined in `apply_business_review_remaining_cleanup.py` were applied until no more rule-matched rows were available.",
        "- Centroids were rebuilt after each applied pass.",
        "- Generic actor-role reversal audit and triage were rerun after each pass.",
        "",
        "## Final triage summary",
        "",
    ]

    if summary_df.empty:
        lines.append("No final triage summary found.")
    else:
        lines.append(summary_df.to_markdown(index=False))

    lines.extend([
        "",
        "## Final files",
        "",
        "- `01_final_triage_summary.csv`",
        "- `02_final_actionable_review_clusters.csv`",
        "- `03_final_strict315_review_clusters.csv`",
        "- `04_final_actionable_labels.csv`",
        "- `05_final_strict315_labels.csv`",
    ])

    report_path.write_text("\n".join(lines), encoding="utf-8")

    print("\nFinal unresolved review exported:")
    print(final_dir)
    print(report_path)

    return final_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Without this, only dry-run the first cleanup plan.")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--stop-if-no-plan", action="store_true", default=True)
    args = parser.parse_args()

    for script in [APPLY_SCRIPT, REBUILD_SCRIPT, AUDIT_SCRIPT, TRIAGE_SCRIPT]:
        require_file(script)
        validate_cleanup_script_has_latest_rules()

    BASE_OUT.mkdir(parents=True, exist_ok=True)

    print(f"Output folder: {BASE_OUT}")

    last_result = None

    for round_number in range(1, args.max_rounds + 1):
        print("\n" + "#" * 120)
        print(f"ROUND {round_number}")
        print("#" * 120)

        round_dir = BASE_OUT / f"round_{round_number}"
        cleanup_out = round_dir / "cleanup"
        cleanup_out.mkdir(parents=True, exist_ok=True)

        rows, occurrences = run_cleanup_dry(cleanup_out)

        if rows == 0:
            print("\nNo rule-matched cleanup rows remain.")
            if last_result is None:
                print("Running audit/triage once to produce final state.")
                rebuild_centroids()
                last_result = run_audit_and_triage(round_dir, round_number)
            break

        if not args.apply:
            print("\nDRY RUN ONLY.")
            print("Review the cleanup summary, then rerun:")
            print(f"python {Path(__file__).name} --apply")
            return

        apply_cleanup(cleanup_out)
        rebuild_centroids()
        last_result = run_audit_and_triage(round_dir, round_number)

        actionable = summarize_actionable(last_result["summary_df"])

        print("\nOpen actionable triage buckets after this round:")
        if actionable.empty:
            print("None.")
            break
        else:
            print(actionable.to_string(index=False))

    if last_result is not None:
        final_dir = export_final_unresolved(last_result)

        print("\nDONE.")
        print(f"Full convergence output: {BASE_OUT}")
        print(f"Final review output: {final_dir}")
    else:
        print("\nNo audit result generated.")


if __name__ == "__main__":
    main()