import argparse
from pathlib import Path

import pandas as pd


CONFIRMED_RISK_FAMILIES = {
    "insult",
    "mock",
    "threaten",
    "shout",
    "accuse",
    "complain",
    "challenge",
    "interrupt",
    "ignore",
    "ignor",
    "contradict",
    "correct",
    "frustrat",
    "hostile",
    "dismissive",
    "abuse",
    "refuse",
    "hung",
    "terminate",
    "disconnect",
    "cut",
    "cutoff",
}

LIKELY_STATUS_OR_SYMMETRY_FALSE_POSITIVES = {
    "already",
    "unavailable",
    "email",
    "will",
    "request",
    "detail",
    "info",
    "check",
    "confirmation",
    "identifi",
    "identification",
    "query",
    "question",
    "seek",
    "mobile",
    "sent",
    "provid",
    "reach",
    "redirect",
    "relationship",
    "friend",
    "personal",
    "supplier",
    "busy",
    "member",
    "shortage",
    "turnover",
    "requir",
    "internal",
    "call",
}

BROAD_MIXED_CLUSTER_IDS = {
    "strict_315",
}

KNOWN_CONFIRMED_CLUSTERS = {
    ("additional_tags", "base_2246"): "CONFIRMED_FIX",
    ("additional_tags", "base_1657"): "CONFIRMED_FIX",
}

KNOWN_REVIEW_CLUSTERS = {
    ("additional_tags", "strict_315"): "REVIEW_SPLIT_BROAD_MIXED_CLUSTER",
    ("additional_tags", "strict_38"): "REVIEW_SPLIT",
    ("additional_tags", "base_323"): "REVIEW_SPLIT",
    ("additional_tags", "base_2765"): "REVIEW_SPLIT",
}

KNOWN_FALSE_POSITIVE_CLUSTERS = {
    ("additional_tags", "strict_594"): "LIKELY_FALSE_POSITIVE",
}


def normalize(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def has_composite_label(value):
    value = normalize(value)
    return "{" in value or "}" in value or "," in value


def classify_cluster(row):
    field_name = normalize(row.get("field_name"))
    cluster_id = normalize(row.get("cluster_id"))
    relation_family = normalize(row.get("relation_family"))
    display_name = normalize(row.get("display_name"))

    key = (field_name, cluster_id)

    if key in KNOWN_CONFIRMED_CLUSTERS:
        return KNOWN_CONFIRMED_CLUSTERS[key]

    if key in KNOWN_FALSE_POSITIVE_CLUSTERS:
        return KNOWN_FALSE_POSITIVE_CLUSTERS[key]

    if key in KNOWN_REVIEW_CLUSTERS:
        return KNOWN_REVIEW_CLUSTERS[key]

    if cluster_id in BROAD_MIXED_CLUSTER_IDS:
        return "REVIEW_SPLIT_BROAD_MIXED_CLUSTER"

    if relation_family in CONFIRMED_RISK_FAMILIES:
        return "REVIEW_HIGH_RISK_DIRECTIONAL"

    if relation_family in LIKELY_STATUS_OR_SYMMETRY_FALSE_POSITIVES:
        return "LIKELY_FALSE_POSITIVE_OR_STATUS_SYMMETRY"

    if field_name in {"main_reason_sub", "next_step"}:
        return "REVIEW_COMPOSITE_OR_WORKFLOW_DIRECTION"

    if "existing" in display_name.lower() or "broker" in display_name.lower():
        return "REVIEW_BUSINESS_RELATIONSHIP_DIRECTION"

    return "REVIEW_UNKNOWN"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--generic-dir",
        required=True,
        help="Folder containing 01_generic_affected_clusters.csv and 02_generic_affected_labels.csv",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output folder for triaged review files",
    )

    args = parser.parse_args()

    generic_dir = Path(args.generic_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    clusters_path = generic_dir / "01_generic_affected_clusters.csv"
    labels_path = generic_dir / "02_generic_affected_labels.csv"

    clusters = pd.read_csv(clusters_path)
    labels = pd.read_csv(labels_path)

    clusters["triage_decision"] = clusters.apply(classify_cluster, axis=1)

    labels["has_composite_label"] = labels["normalized_label"].apply(has_composite_label)

    label_key_cols = [
        "field_name",
        "cluster_version",
        "cluster_id",
        "relation_family",
        "unordered_side_pair",
    ]

    labels = labels.merge(
        clusters[
            label_key_cols
            + [
                "display_name",
                "severity",
                "directions_found",
                "direction_occurrences",
                "triage_decision",
            ]
        ].drop_duplicates(),
        on=label_key_cols,
        how="left",
        suffixes=("", "_cluster"),
    )

    priority_order = {
        "CONFIRMED_FIX": 1,
        "REVIEW_SPLIT_BROAD_MIXED_CLUSTER": 2,
        "REVIEW_HIGH_RISK_DIRECTIONAL": 3,
        "REVIEW_SPLIT": 4,
        "REVIEW_BUSINESS_RELATIONSHIP_DIRECTION": 5,
        "REVIEW_COMPOSITE_OR_WORKFLOW_DIRECTION": 6,
        "REVIEW_UNKNOWN": 7,
        "LIKELY_FALSE_POSITIVE_OR_STATUS_SYMMETRY": 8,
        "LIKELY_FALSE_POSITIVE": 9,
    }

    clusters["triage_priority"] = clusters["triage_decision"].map(priority_order).fillna(99).astype(int)
    labels["triage_priority"] = labels["triage_decision"].map(priority_order).fillna(99).astype(int)

    clusters = clusters.sort_values(
        ["triage_priority", "severity", "total_issue_occurrences"],
        ascending=[True, True, False],
    )

    labels = labels.sort_values(
        ["triage_priority", "field_name", "cluster_id", "relation_family", "direction_key", "value_count"],
        ascending=[True, True, True, True, True, False],
    )

    clusters.to_csv(out_dir / "01_triaged_generic_clusters.csv", index=False)
    labels.to_csv(out_dir / "02_triaged_generic_labels.csv", index=False)

    confirmed = clusters[clusters["triage_decision"] == "CONFIRMED_FIX"]
    review = clusters[clusters["triage_decision"].str.startswith("REVIEW", na=False)]
    false_positive = clusters[clusters["triage_decision"].str.contains("FALSE_POSITIVE|STATUS_SYMMETRY", na=False)]

    confirmed.to_csv(out_dir / "03_confirmed_fix_clusters.csv", index=False)
    review.to_csv(out_dir / "04_review_required_clusters.csv", index=False)
    false_positive.to_csv(out_dir / "05_likely_false_positive_clusters.csv", index=False)

    cleanup_template = labels[
        labels["triage_decision"].isin(
            [
                "CONFIRMED_FIX",
                "REVIEW_SPLIT_BROAD_MIXED_CLUSTER",
                "REVIEW_HIGH_RISK_DIRECTIONAL",
                "REVIEW_SPLIT",
            ]
        )
    ].copy()

    cleanup_template["cleanup_decision"] = ""
    cleanup_template["target_cluster_id"] = ""
    cleanup_template["target_display_name"] = ""
    cleanup_template["review_notes"] = ""

    cleanup_template.to_csv(out_dir / "06_cleanup_staging_review_template.csv", index=False)

    summary = (
        clusters.groupby(["field_name", "triage_decision"])
        .agg(
            affected_issue_rows=("cluster_id", "count"),
            unique_clusters=("cluster_id", "nunique"),
            total_issue_occurrences=("total_issue_occurrences", "sum"),
        )
        .reset_index()
        .sort_values(["field_name", "total_issue_occurrences"], ascending=[True, False])
    )

    summary.to_csv(out_dir / "07_triage_summary.csv", index=False)

    print("Generic reversal triage complete")
    print(f"Input clusters: {len(clusters):,}")
    print(f"Input labels: {len(labels):,}")
    print("")
    print(summary.to_string(index=False))
    print("")
    print(f"Output folder: {out_dir}")


if __name__ == "__main__":
    main()