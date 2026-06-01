from pathlib import Path
import pandas as pd


ACTIONABLE = {
    "REVIEW_SPLIT_BROAD_MIXED_CLUSTER",
    "REVIEW_HIGH_RISK_DIRECTIONAL",
    "REVIEW_UNKNOWN",
    "REVIEW_BUSINESS_RELATIONSHIP_DIRECTION",
}


def latest_convergence_dir():
    base = Path("outputs")
    dirs = sorted(
        base.glob("remaining_business_cleanup_convergence_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not dirs:
        raise FileNotFoundError("No remaining_business_cleanup_convergence_* folder found under outputs.")
    return dirs[0]


def read_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path)


def main():
    conv = latest_convergence_dir()
    final_dir = conv / "final_unresolved_review"

    summary_path = final_dir / "01_final_triage_summary.csv"
    clusters_path = final_dir / "02_final_actionable_review_clusters.csv"
    labels_path = final_dir / "04_final_actionable_labels.csv"

    summary = read_csv(summary_path)
    clusters = read_csv(clusters_path)
    labels = read_csv(labels_path)

    out_dir = final_dir / "business_review_pack"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("")
    print("Latest convergence folder:")
    print(conv)
    print("")

    print("Final triage summary:")
    if summary.empty:
        print("No summary found.")
    else:
        print(summary.to_string(index=False))

    if clusters.empty:
        print("")
        print("No actionable cluster file found or file is empty.")
    else:
        clusters = clusters[clusters["triage_decision"].isin(ACTIONABLE)].copy()
        clusters.to_csv(out_dir / "01_remaining_actionable_clusters.csv", index=False)

        print("")
        print("Remaining actionable clusters:")
        cols = [
            "field_name",
            "cluster_id",
            "display_name",
            "relation_family",
            "severity",
            "direction_occurrences",
            "total_issue_occurrences",
            "triage_decision",
        ]
        cols = [c for c in cols if c in clusters.columns]
        print(clusters[cols].to_string(index=False))

    if labels.empty:
        print("")
        print("No actionable label file found or file is empty.")
    else:
        labels = labels[labels["triage_decision"].isin(ACTIONABLE)].copy()
        labels.to_csv(out_dir / "02_remaining_actionable_labels.csv", index=False)

        print("")
        print("Top remaining actionable labels:")
        cols = [
            "cluster_id",
            "display_name",
            "raw_label",
            "normalized_label",
            "value_count",
            "relation_family",
            "direction_key",
            "triage_decision",
        ]
        cols = [c for c in cols if c in labels.columns]

        label_sort_col = "value_count" if "value_count" in labels.columns else None
        if label_sort_col:
            labels["_sort_value"] = pd.to_numeric(labels[label_sort_col], errors="coerce").fillna(0)
            labels = labels.sort_values(["triage_decision", "_sort_value"], ascending=[True, False])

        print(labels[cols].head(250).to_string(index=False))

    print("")
    print("Files written:")
    print(out_dir / "01_remaining_actionable_clusters.csv")
    print(out_dir / "02_remaining_actionable_labels.csv")


if __name__ == "__main__":
    main()