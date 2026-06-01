import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import Json


load_dotenv()


DEFAULT_LABELS_PATH = Path(
    "outputs/generic_actor_role_reversal_triage_after_confirmed_cleanup_v3_20260601/02_triaged_generic_labels.csv"
)

BACKUP_SUFFIX = datetime.now().strftime("%Y%m%d_%H%M%S")


AUTO_FIX_RULES = [
    {
        "field_name": "additional_tags",
        "cluster_id": "base_323",
        "labels": {
            "agent mocking customer",
            "agent mocked customer",
            "agent mocking previous customer",
        },
        "target_cluster_id": "manual_agent_mocked_customer",
        "target_display_name": "Agent Mocked Customer",
        "reason": "Clean directionality split from Customer Mocking Agent.",
    },
    {
        "field_name": "additional_tags",
        "cluster_id": "base_2017",
        "labels": {
            "agent interrupted dm",
        },
        "target_cluster_id": "manual_agent_interrupted_dm",
        "target_display_name": "Agent Interrupted DM",
        "reason": "Small but true directionality split from DM Contact Failure.",
    },
    {
        "field_name": "additional_tags",
        "cluster_id": "base_2017",
        "labels": {
            "agent interrupted by dm",
        },
        "target_cluster_id": "manual_dm_interrupted_agent",
        "target_display_name": "DM Interrupted Agent",
        "reason": "Small but true directionality split from DM Contact Failure.",
    },
]


FALSE_POSITIVE_OR_NO_FIX = {
    ("additional_tags", "base_2821"): "Wording variant: customer frustrated by broker calls / with broker calls.",
    ("additional_tags", "strict_361"): "Likely role-dictionary/parser issue. Manager/contact terms should not be auto-cleaned.",
}

DEFERRED_SPLIT = {
    ("additional_tags", "strict_315"): "Large broad mixed cluster. Needs dedicated split project.",
    ("additional_tags", "strict_38"): "Large broker-call-fatigue cluster. Needs dedicated split project.",
    ("additional_tags", "base_2765"): "Many complaint/correction/ignore/frustration patterns. Needs manual split review.",
}


def get_conn():
    return psycopg2.connect(
        host=os.getenv("LOCAL_PG_HOST") or "127.0.0.1",
        port=os.getenv("LOCAL_PG_PORT") or "5432",
        dbname=os.getenv("LOCAL_PG_DB") or "taxonomy_drift_local",
        user=os.getenv("LOCAL_PG_USER") or "postgres",
        password=os.getenv("LOCAL_PG_PASSWORD") or "postgres",
    )


def table_columns(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            (table_name,),
        )
        return {r[0] for r in cur.fetchall()}


def adapt_pg_value(value):
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


def create_backup_table(conn, source_table, backup_table):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} AS SELECT * FROM {} WHERE false")
            .format(sql.Identifier(backup_table), sql.Identifier(source_table))
        )


def normalize_value(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def load_label_evidence(path):
    if not path.exists():
        raise FileNotFoundError(f"Label evidence file not found: {path}")

    df = pd.read_csv(path)

    required = {
        "field_name",
        "cluster_version",
        "cluster_id",
        "display_name",
        "raw_label",
        "normalized_label",
        "value_count",
        "direction_key",
        "triage_decision",
    }

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns in label evidence CSV: {sorted(missing)}")

    for col in required:
        df[col] = df[col].apply(normalize_value)

    return df


def build_cleanup_plan(df):
    plan_rows = []
    decision_rows = []

    seen_move_keys = set()

    for _, row in df.iterrows():
        key = (row["field_name"], row["cluster_id"])
        normalized_label = row["normalized_label"]

        matched_rule = None

        for rule in AUTO_FIX_RULES:
            if (
                row["field_name"] == rule["field_name"]
                and row["cluster_id"] == rule["cluster_id"]
                and normalized_label in rule["labels"]
            ):
                matched_rule = rule
                break

        if matched_rule:
            move_key = (
                row["field_name"],
                row["cluster_version"],
                row["cluster_id"],
                row["raw_label"],
                row["normalized_label"],
                matched_rule["target_cluster_id"],
            )

            if move_key not in seen_move_keys:
                seen_move_keys.add(move_key)

                plan_rows.append({
                    "field_name": row["field_name"],
                    "cluster_version": row["cluster_version"],
                    "source_cluster_id": row["cluster_id"],
                    "source_display_name": row["display_name"],
                    "raw_label": row["raw_label"],
                    "normalized_label": row["normalized_label"],
                    "value_count": row["value_count"],
                    "target_cluster_id": matched_rule["target_cluster_id"],
                    "target_display_name": matched_rule["target_display_name"],
                    "cleanup_decision": "MOVE_TO_MANUAL_DIRECTIONAL_CLUSTER",
                    "reason": matched_rule["reason"],
                })

            decision = "AUTO_FIX"
            reason = matched_rule["reason"]

        elif key in FALSE_POSITIVE_OR_NO_FIX:
            decision = "NO_FIX_FALSE_POSITIVE"
            reason = FALSE_POSITIVE_OR_NO_FIX[key]

        elif key in DEFERRED_SPLIT:
            decision = "DEFERRED_SPLIT_PROJECT"
            reason = DEFERRED_SPLIT[key]

        else:
            decision = "NO_FIX_THIS_PASS"
            reason = "Not approved for automatic cleanup in this pass."

        decision_rows.append({
            "field_name": row["field_name"],
            "cluster_version": row["cluster_version"],
            "cluster_id": row["cluster_id"],
            "display_name": row["display_name"],
            "raw_label": row["raw_label"],
            "normalized_label": row["normalized_label"],
            "value_count": row["value_count"],
            "direction_key": row["direction_key"],
            "triage_decision": row["triage_decision"],
            "second_pass_decision": decision,
            "decision_reason": reason,
        })

    plan_df = pd.DataFrame(plan_rows)
    decisions_df = pd.DataFrame(decision_rows)

    return plan_df, decisions_df


def fetch_one_dict(conn, query, params):
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def target_cluster_exists(conn, field_name, cluster_version, cluster_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM taxonomy_clusters
            WHERE field_name = %s
              AND cluster_version = %s
              AND cluster_id = %s
            LIMIT 1
            """,
            (field_name, cluster_version, cluster_id),
        )
        return cur.fetchone() is not None


def backup_rows(conn, plan_rows, backup_label_map, backup_clusters, backup_names):
    cluster_keys = set()

    with conn.cursor() as cur:
        for r in plan_rows:
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}
                    SELECT *
                    FROM taxonomy_label_cluster_map
                    WHERE field_name = %s
                      AND cluster_version = %s
                      AND final_cluster_id = %s
                      AND normalized_label = %s
                      AND raw_label = %s
                """).format(sql.Identifier(backup_label_map)),
                (
                    r["field_name"],
                    r["cluster_version"],
                    r["source_cluster_id"],
                    r["normalized_label"],
                    r["raw_label"],
                ),
            )

            cluster_keys.add((r["field_name"], r["cluster_version"], r["source_cluster_id"]))
            cluster_keys.add((r["field_name"], r["cluster_version"], r["target_cluster_id"]))

        for field_name, cluster_version, cluster_id in cluster_keys:
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}
                    SELECT *
                    FROM taxonomy_clusters
                    WHERE field_name = %s
                      AND cluster_version = %s
                      AND cluster_id = %s
                """).format(sql.Identifier(backup_clusters)),
                (field_name, cluster_version, cluster_id),
            )

            cur.execute(
                sql.SQL("""
                    INSERT INTO {}
                    SELECT *
                    FROM taxonomy_cluster_names
                    WHERE field_name = %s
                      AND cluster_version = %s
                      AND cluster_id = %s
                """).format(sql.Identifier(backup_names)),
                (field_name, cluster_version, cluster_id),
            )


def create_target_clusters(conn, plan_rows):
    c_cols = table_columns(conn, "taxonomy_clusters")
    n_cols = table_columns(conn, "taxonomy_cluster_names")

    grouped = defaultdict(list)

    for r in plan_rows:
        grouped[
            (
                r["field_name"],
                r["cluster_version"],
                r["source_cluster_id"],
                r["target_cluster_id"],
                r["target_display_name"],
            )
        ].append(r)

    for (field_name, cluster_version, source_cluster_id, target_cluster_id, target_display_name), rows in grouped.items():
        if not target_cluster_exists(conn, field_name, cluster_version, target_cluster_id):
            source_row = fetch_one_dict(
                conn,
                """
                SELECT *
                FROM taxonomy_clusters
                WHERE field_name = %s
                  AND cluster_version = %s
                  AND cluster_id = %s
                LIMIT 1
                """,
                (field_name, cluster_version, source_cluster_id),
            )

            if source_row is None:
                raise ValueError(
                    f"Source cluster not found: {field_name} / {cluster_version} / {source_cluster_id}"
                )

            insert_row = dict(source_row)
            insert_row.pop("id", None)

            insert_row["cluster_id"] = target_cluster_id

            if "display_name" in c_cols:
                insert_row["display_name"] = target_display_name
            if "is_true_anomaly_cluster" in c_cols:
                insert_row["is_true_anomaly_cluster"] = False
            if "active" in c_cols:
                insert_row["active"] = True
            if "cluster_size" in c_cols:
                insert_row["cluster_size"] = 0
            if "total_occurrences" in c_cols:
                insert_row["total_occurrences"] = 0
            if "medoid_label" in c_cols:
                top_row = max(rows, key=lambda x: int(float(x.get("value_count") or 0)))
                insert_row["medoid_label"] = top_row["normalized_label"]
            if "centroid_embedding" in c_cols:
                insert_row["centroid_embedding"] = None
            if "medoid_similarity_to_centroid" in c_cols:
                insert_row["medoid_similarity_to_centroid"] = None
            if "representative_labels" in c_cols:
                insert_row["representative_labels"] = None
            if "created_at" in c_cols:
                insert_row["created_at"] = datetime.now()
            if "updated_at" in c_cols:
                insert_row["updated_at"] = datetime.now()

            cols = list(insert_row.keys())
            values = [adapt_pg_value(insert_row[c]) for c in cols]

            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("INSERT INTO taxonomy_clusters ({}) VALUES ({})")
                    .format(
                        sql.SQL(", ").join(map(sql.Identifier, cols)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in cols),
                    ),
                    values,
                )

            print(f"Created target cluster: {field_name} / {target_cluster_id}")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM taxonomy_cluster_names
                WHERE field_name = %s
                  AND cluster_version = %s
                  AND cluster_id = %s
                LIMIT 1
                """,
                (field_name, cluster_version, target_cluster_id),
            )
            name_exists = cur.fetchone() is not None

        if not name_exists:
            name_row = {}

            if "field_name" in n_cols:
                name_row["field_name"] = field_name
            if "run_id" in n_cols:
                name_row["run_id"] = cluster_version
            if "cluster_version" in n_cols:
                name_row["cluster_version"] = cluster_version
            if "cluster_id" in n_cols:
                name_row["cluster_id"] = target_cluster_id
            if "is_anomaly" in n_cols:
                name_row["is_anomaly"] = False
            if "display_name" in n_cols:
                name_row["display_name"] = target_display_name
            if "naming_method" in n_cols:
                name_row["naming_method"] = "manual_directionality_cleanup"
            if "naming_reason" in n_cols:
                name_row["naming_reason"] = "Created during second-pass actor-target directionality cleanup."
            if "created_at" in n_cols:
                name_row["created_at"] = datetime.now()
            if "updated_at" in n_cols:
                name_row["updated_at"] = datetime.now()

            cols = list(name_row.keys())
            values = [adapt_pg_value(name_row[c]) for c in cols]

            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("INSERT INTO taxonomy_cluster_names ({}) VALUES ({})")
                    .format(
                        sql.SQL(", ").join(map(sql.Identifier, cols)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in cols),
                    ),
                    values,
                )

            print(f"Created target cluster name: {target_cluster_id} -> {target_display_name}")


def move_label_rows(conn, plan_rows):
    m_cols = table_columns(conn, "taxonomy_label_cluster_map")
    moved_total = 0

    for r in plan_rows:
        set_parts = [sql.SQL("final_cluster_id = %s")]
        params = [r["target_cluster_id"]]

        if "final_cluster_source" in m_cols:
            set_parts.append(sql.SQL("final_cluster_source = %s"))
            params.append("manual_directionality_cleanup_second_pass")

        if "final_is_true_anomaly" in m_cols:
            set_parts.append(sql.SQL("final_is_true_anomaly = %s"))
            params.append(False)

        if "updated_at" in m_cols:
            set_parts.append(sql.SQL("updated_at = NOW()"))

        params.extend([
            r["field_name"],
            r["cluster_version"],
            r["source_cluster_id"],
            r["normalized_label"],
            r["raw_label"],
        ])

        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    UPDATE taxonomy_label_cluster_map
                    SET {}
                    WHERE field_name = %s
                      AND cluster_version = %s
                      AND final_cluster_id = %s
                      AND normalized_label = %s
                      AND raw_label = %s
                """).format(sql.SQL(", ").join(set_parts)),
                params,
            )
            moved = cur.rowcount

        moved_total += moved

        print(
            f"Moved {moved} row(s): "
            f"{r['source_cluster_id']} / {r['normalized_label']} -> {r['target_cluster_id']}"
        )

    return moved_total


def refresh_basic_cluster_stats(conn, plan_rows):
    c_cols = table_columns(conn, "taxonomy_clusters")

    affected_clusters = set()

    for r in plan_rows:
        affected_clusters.add((r["field_name"], r["cluster_version"], r["source_cluster_id"]))
        affected_clusters.add((r["field_name"], r["cluster_version"], r["target_cluster_id"]))

    for field_name, cluster_version, cluster_id in affected_clusters:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    normalized_label,
                    COALESCE(value_count, 1) AS value_count
                FROM taxonomy_label_cluster_map
                WHERE field_name = %s
                  AND cluster_version = %s
                  AND final_cluster_id = %s
                ORDER BY COALESCE(value_count, 1) DESC, normalized_label
                """,
                (field_name, cluster_version, cluster_id),
            )
            label_rows = cur.fetchall()

        cluster_size = len(label_rows)
        total_occurrences = sum(int(v or 1) for _, v in label_rows)
        medoid_label = label_rows[0][0] if label_rows else None

        set_parts = []
        params = []

        if "cluster_size" in c_cols:
            set_parts.append(sql.SQL("cluster_size = %s"))
            params.append(cluster_size)
        if "total_occurrences" in c_cols:
            set_parts.append(sql.SQL("total_occurrences = %s"))
            params.append(total_occurrences)
        if "medoid_label" in c_cols:
            set_parts.append(sql.SQL("medoid_label = %s"))
            params.append(medoid_label)
        if "centroid_embedding" in c_cols:
            set_parts.append(sql.SQL("centroid_embedding = NULL"))
        if "medoid_similarity_to_centroid" in c_cols:
            set_parts.append(sql.SQL("medoid_similarity_to_centroid = NULL"))
        if "representative_labels" in c_cols:
            set_parts.append(sql.SQL("representative_labels = NULL"))
        if "updated_at" in c_cols:
            set_parts.append(sql.SQL("updated_at = NOW()"))

        if not set_parts:
            continue

        params.extend([field_name, cluster_version, cluster_id])

        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    UPDATE taxonomy_clusters
                    SET {}
                    WHERE field_name = %s
                      AND cluster_version = %s
                      AND cluster_id = %s
                """).format(sql.SQL(", ").join(set_parts)),
                params,
            )

        print(
            f"Refreshed stats: {field_name} / {cluster_id} "
            f"size={cluster_size}, occurrences={total_occurrences}, medoid={medoid_label}"
        )


def write_outputs(plan_df, decisions_df, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_path = out_dir / "09_second_pass_cleanup_plan.csv"
    decisions_path = out_dir / "10_second_pass_all_decisions.csv"
    report_path = out_dir / "11_second_pass_cleanup_report.md"

    plan_df.to_csv(plan_path, index=False)
    decisions_df.to_csv(decisions_path, index=False)

    lines = []
    lines.append("# Second-Pass Directionality Cleanup Review")
    lines.append("")
    lines.append("## Auto-fix rows")
    lines.append("")

    if plan_df.empty:
        lines.append("No rows selected for automatic cleanup.")
    else:
        lines.append("| Source Cluster | Label | Occurrences | Target Cluster | Target Display Name |")
        lines.append("|---|---|---:|---|---|")

        for r in plan_df.itertuples():
            lines.append(
                f"| `{r.source_cluster_id}` | `{r.normalized_label}` | {r.value_count} | "
                f"`{r.target_cluster_id}` | {r.target_display_name} |"
            )

    lines.append("")
    lines.append("## Decision summary")
    lines.append("")

    summary = (
        decisions_df.groupby("second_pass_decision")
        .agg(
            rows=("normalized_label", "count"),
            clusters=("cluster_id", "nunique"),
            occurrences=("value_count", lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()),
        )
        .reset_index()
    )

    lines.append("| Decision | Rows | Clusters | Occurrences |")
    lines.append("|---|---:|---:|---:|")

    for r in summary.itertuples():
        lines.append(
            f"| {r.second_pass_decision} | {int(r.rows)} | {int(r.clusters)} | {int(r.occurrences)} |"
        )

    lines.append("")
    lines.append("## Deferred clusters")
    lines.append("")
    lines.append("- `strict_315 / Agent IVR Handling Issues`: deferred to dedicated broad split project.")
    lines.append("- `strict_38 / Broker Call Fatigue`: deferred to broker-specific split project.")
    lines.append("- `base_2765 / Previous Agent Complaint`: deferred to manual split review.")
    lines.append("- `base_2821 / Customer Frustrated By Broker Calls`: treated as wording variant, not automatic cleanup.")
    lines.append("- `strict_361 / Hostile Refusal`: treated as role-dictionary/parser issue if present.")

    report_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"Plan written: {plan_path}")
    print(f"Decision report written: {decisions_path}")
    print(f"Markdown report written: {report_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", default=str(DEFAULT_LABELS_PATH))
    parser.add_argument(
        "--out",
        default="outputs/second_pass_directionality_cleanup_20260601",
    )
    parser.add_argument("--apply", action="store_true")

    args = parser.parse_args()

    labels_path = Path(args.labels)
    out_dir = Path(args.out)

    df = load_label_evidence(labels_path)
    plan_df, decisions_df = build_cleanup_plan(df)

    write_outputs(plan_df, decisions_df, out_dir)

    print("")
    print("Second-pass cleanup plan")
    print(f"Input rows reviewed: {len(df):,}")
    print(f"Rows selected for movement: {len(plan_df):,}")

    if not plan_df.empty:
        print("")
        print(
            plan_df[
                [
                    "field_name",
                    "source_cluster_id",
                    "normalized_label",
                    "value_count",
                    "target_cluster_id",
                    "target_display_name",
                ]
            ].to_string(index=False)
        )

    if not args.apply:
        print("")
        print("DRY RUN ONLY. No DB changes were made.")
        print("Run with --apply to execute this second-pass cleanup.")
        return

    if plan_df.empty:
        print("No rows to apply.")
        return

    plan_rows = plan_df.to_dict("records")

    backup_label_map = f"backup_directionality_second_pass_label_map_{BACKUP_SUFFIX}"
    backup_clusters = f"backup_directionality_second_pass_clusters_{BACKUP_SUFFIX}"
    backup_names = f"backup_directionality_second_pass_cluster_names_{BACKUP_SUFFIX}"

    with get_conn() as conn:
        try:
            create_backup_table(conn, "taxonomy_label_cluster_map", backup_label_map)
            create_backup_table(conn, "taxonomy_clusters", backup_clusters)
            create_backup_table(conn, "taxonomy_cluster_names", backup_names)

            backup_rows(conn, plan_rows, backup_label_map, backup_clusters, backup_names)

            print("")
            print("Backups created:")
            print(f"- {backup_label_map}")
            print(f"- {backup_clusters}")
            print(f"- {backup_names}")
            print("")

            create_target_clusters(conn, plan_rows)
            moved_total = move_label_rows(conn, plan_rows)
            refresh_basic_cluster_stats(conn, plan_rows)

            conn.commit()

            print("")
            print("Second-pass cleanup committed successfully.")
            print(f"Total label-map rows moved: {moved_total}")

        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    main()