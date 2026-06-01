import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import Json


load_dotenv()

BACKUP_SUFFIX = datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

ACTIONABLE_DECISIONS = {
    "REVIEW_SPLIT_BROAD_MIXED_CLUSTER",
    "REVIEW_HIGH_RISK_DIRECTIONAL",
    "REVIEW_UNKNOWN",
    "REVIEW_BUSINESS_RELATIONSHIP_DIRECTION",
}

ROOT = Path.cwd()
DEFAULT_OUT = Path("outputs") / f"final_unresolved_actionable_pack_cleanup_{RUN_ID}"

REBUILD_SCRIPT = ROOT / "rebuild_all_active_cluster_centroids_fixed.py"
AUDIT_SCRIPT = ROOT / "audit_generic_actor_role_reversals_all_fields.py"
TRIAGE_SCRIPT = ROOT / "triage_generic_actor_role_reversal_results.py"


def get_conn():
    return psycopg2.connect(
        host=os.getenv("LOCAL_PG_HOST") or "127.0.0.1",
        port=os.getenv("LOCAL_PG_PORT") or "5432",
        dbname=os.getenv("LOCAL_PG_DB") or "taxonomy_drift_local",
        user=os.getenv("LOCAL_PG_USER") or "postgres",
        password=os.getenv("LOCAL_PG_PASSWORD") or "postgres",
    )


def latest_actionable_labels_file():
    dirs = sorted(
        Path("outputs").glob("remaining_business_cleanup_convergence_*/final_unresolved_review/business_review_pack"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for d in dirs:
        p = d / "02_remaining_actionable_labels.csv"
        if p.exists() and p.stat().st_size > 0:
            return p

    raise FileNotFoundError(
        "Could not find latest business_review_pack/02_remaining_actionable_labels.csv"
    )


def normalize_label(value):
    if value is None:
        return ""
    value = str(value).strip().lower()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"[^a-z0-9\s]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def starts_any(label, prefixes):
    return any(label.startswith(p) for p in prefixes)


def contains_any(label, terms):
    return any(t in label for t in terms)


def adapt_pg_value(value):
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


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

def map_final_label(source_cluster_id, label):
    l = normalize_label(label)

    # Split causal hang-up rows out of the broad customer-ended-call cluster.
    if source_cluster_id == "manual_customer_ended_call_on_agent":
        if starts_any(l, [
            "customer hung up due to agent silence",
            "customer hung up due to agent inattention",
        ]):
            return (
                "manual_customer_hung_up_due_to_agent_issue",
                "Customer Hung Up Due To Agent Issue",
                "Customer hung up because of agent silence, inattention, or related agent issue.",
            )

        return None

    # Final business relationship cleanup.
    # These are not actor-direction bugs; they are relationship-status / broker-role taxonomy splits.
    if source_cluster_id == "base_2766":
        if starts_any(l, [
            "existing customer managed by other agent",
        ]):
            return (
                "manual_customer_managed_by_other_agent",
                "Customer Managed By Other Agent",
                "Existing customer is already managed by another agent.",
            )

        if starts_any(l, [
            "customer is managing agent",
        ]):
            return (
                "manual_customer_is_managing_agent",
                "Customer Is Managing Agent",
                "Customer/contact is acting as managing agent.",
            )

        return None

    if source_cluster_id == "base_2831":
        if starts_any(l, [
            "customer managed by broker",
            "customer managed by another broker",
            "customer managed by internal broker",
            "customer managed by external broker",
            "customer managed by other broker",
        ]):
            return (
                "manual_customer_managed_by_broker",
                "Customer Managed By Broker",
                "Customer is managed by broker, another broker, internal broker, external broker, or other broker.",
            )

        if starts_any(l, [
            "customer broker managed",
        ]):
            return (
                "manual_customer_broker_managed_status",
                "Customer Broker Managed Status",
                "Customer has broker-managed status.",
            )

        if starts_any(l, [
            "customer uses broker as client",
        ]):
            return (
                "manual_customer_uses_broker_as_client",
                "Customer Uses Broker As Client",
                "Customer uses broker as client.",
            )

        if starts_any(l, [
            "customer uses client as broker",
        ]):
            return (
                "manual_customer_uses_client_as_broker",
                "Customer Uses Client As Broker",
                "Customer uses client as broker.",
            )

        if starts_any(l, [
            "broker not customer",
        ]):
            return (
                "manual_broker_not_customer",
                "Broker Not Customer",
                "Broker is not the customer.",
            )

        if starts_any(l, [
            "customer not using broker",
        ]):
            return (
                "manual_customer_not_using_broker",
                "Customer Not Using Broker",
                "Customer is not using broker.",
            )

        return None

    # Everything below this point is only for strict_315.
    if source_cluster_id != "strict_315":
        return None

    # Customer seeking/requesting a specific agent.
    if starts_any(l, [
        "customer seeking specific agent",
        "customer requesting specific agent",
        "customer looking for specific agent",
        "customer requested original agent",
        "dm requested agent previously",
        "customer requested agent previously",
        "customer waiting for specific agent",
    ]):
        return (
            "manual_customer_requested_agent",
            "Customer Requested Agent",
            "Customer/DM requested, sought, waited for, or asked for a specific/original agent.",
        )


def build_plan(labels_df):
    rows = []

    for r in labels_df.to_dict("records"):
        triage = str(r.get("triage_decision", ""))
        if triage not in ACTIONABLE_DECISIONS:
            continue

        source_cluster_id = str(r.get("cluster_id", ""))
        normalized_label = str(r.get("normalized_label", ""))
        raw_label = str(r.get("raw_label", normalized_label))
        field_name = str(r.get("field_name", "additional_tags"))
        cluster_version = str(r.get("cluster_version", "20260513_093749"))
        value_count = int(r.get("value_count", 1) or 1)

        mapped = map_final_label(source_cluster_id, normalized_label)

        if mapped is None:
            continue

        target_cluster_id, target_display_name, reason = mapped

        rows.append({
            "field_name": field_name,
            "cluster_version": cluster_version,
            "source_cluster_id": source_cluster_id,
            "raw_label": raw_label,
            "normalized_label": normalized_label,
            "value_count": value_count,
            "target_cluster_id": target_cluster_id,
            "target_display_name": target_display_name,
            "reason": reason,
        })

    return pd.DataFrame(rows)


def write_plan_outputs(plan_df, labels_df, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_path = out_dir / "01_final_actionable_move_plan.csv"
    summary_path = out_dir / "02_final_actionable_move_summary.csv"
    leftovers_path = out_dir / "03_unmapped_actionable_labels.csv"

    plan_df.to_csv(plan_path, index=False)

    if plan_df.empty:
        summary = pd.DataFrame(
            columns=[
                "source_cluster_id",
                "target_cluster_id",
                "target_display_name",
                "label_rows",
                "occurrences",
                "reasons",
            ]
        )
    else:
        summary = (
            plan_df.groupby(["source_cluster_id", "target_cluster_id", "target_display_name"])
            .agg(
                label_rows=("normalized_label", "count"),
                occurrences=("value_count", "sum"),
                reasons=("reason", lambda s: " | ".join(sorted(set(s)))),
            )
            .reset_index()
            .sort_values(["source_cluster_id", "occurrences"], ascending=[True, False])
        )

    summary.to_csv(summary_path, index=False)

    mapped_keys = set()
    for r in plan_df.to_dict("records"):
        mapped_keys.add((r["source_cluster_id"], r["normalized_label"]))

    leftovers = labels_df[
        labels_df.apply(
            lambda r: (
                str(r.get("triage_decision", "")) in ACTIONABLE_DECISIONS
                and (str(r.get("cluster_id", "")), str(r.get("normalized_label", ""))) not in mapped_keys
            ),
            axis=1,
        )
    ].copy()

    leftovers.to_csv(leftovers_path, index=False)

    print("")
    print("Final actionable move summary:")
    if summary.empty:
        print("No rows mapped.")
    else:
        print(summary.to_string(index=False))

    print("")
    print(f"Plan written: {plan_path}")
    print(f"Summary written: {summary_path}")
    print(f"Unmapped actionable labels written: {leftovers_path}")

    return summary, leftovers


def create_backup_table(conn, source_table, backup_table):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} AS SELECT * FROM {} WHERE false")
            .format(sql.Identifier(backup_table), sql.Identifier(source_table))
        )


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


def table_insert(conn, table_name, row):
    cols = list(row.keys())
    values = [adapt_pg_value(row[c]) for c in cols]

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})")
            .format(
                sql.Identifier(table_name),
                sql.SQL(", ").join(map(sql.Identifier, cols)),
                sql.SQL(", ").join(sql.Placeholder() for _ in cols),
            ),
            values,
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
                raise ValueError(f"Source cluster not found: {field_name}/{cluster_version}/{source_cluster_id}")

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
                top_row = max(rows, key=lambda x: int(x.get("value_count") or 0))
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

            table_insert(conn, "taxonomy_clusters", insert_row)
            print(f"Created target cluster: {target_cluster_id}")

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
                name_row["naming_method"] = "manual_final_unresolved_actionable_pack"
            if "naming_reason" in n_cols:
                name_row["naming_reason"] = "Created from final actionable unresolved business-review pack."
            if "created_at" in n_cols:
                name_row["created_at"] = datetime.now()
            if "updated_at" in n_cols:
                name_row["updated_at"] = datetime.now()

            table_insert(conn, "taxonomy_cluster_names", name_row)
            print(f"Created target cluster name: {target_cluster_id} -> {target_display_name}")


def move_rows(conn, plan_rows):
    m_cols = table_columns(conn, "taxonomy_label_cluster_map")
    moved_total = 0

    for r in plan_rows:
        set_parts = [sql.SQL("final_cluster_id = %s")]
        params = [r["target_cluster_id"]]

        if "final_cluster_source" in m_cols:
            set_parts.append(sql.SQL("final_cluster_source = %s"))
            params.append("manual_final_unresolved_actionable_pack")

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
        print(f"Moved {moved}: {r['source_cluster_id']} / {r['normalized_label']} -> {r['target_cluster_id']}")

    return moved_total


def refresh_basic_cluster_stats(conn, plan_rows):
    c_cols = table_columns(conn, "taxonomy_clusters")

    affected_clusters = set()

    for r in plan_rows:
        affected_clusters.add((r["field_name"], r["cluster_version"], r["source_cluster_id"]))
        affected_clusters.add((r["field_name"], r["cluster_version"], r["target_cluster_id"]))

    for field_name, cluster_version, cluster_id in sorted(affected_clusters):
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
            rows = cur.fetchall()

        cluster_size = len(rows)
        total_occurrences = sum(int(v or 1) for _, v in rows)
        medoid_label = rows[0][0] if rows else None

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
            f"Refreshed stats: {cluster_id} "
            f"size={cluster_size}, occurrences={total_occurrences}, medoid={medoid_label}"
        )


def apply_plan(plan_df):
    if plan_df.empty:
        print("No mapped rows to apply.")
        return 0

    plan_rows = plan_df.to_dict("records")

    backup_label_map = f"backup_final_actionable_label_map_{BACKUP_SUFFIX}"
    backup_clusters = f"backup_final_actionable_clusters_{BACKUP_SUFFIX}"
    backup_names = f"backup_final_actionable_cluster_names_{BACKUP_SUFFIX}"

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
            moved_total = move_rows(conn, plan_rows)
            refresh_basic_cluster_stats(conn, plan_rows)

            conn.commit()
            print("")
            print(f"Committed successfully. Moved rows: {moved_total}")
            return moved_total

        except Exception:
            conn.rollback()
            raise


def run_cmd(cmd):
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

    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(x) for x in cmd)}")

    return result


def post_validate(out_dir):
    for p in [REBUILD_SCRIPT, AUDIT_SCRIPT, TRIAGE_SCRIPT]:
        if not p.exists():
            raise FileNotFoundError(p)

    audit_out = out_dir / "generic_after_final_actionable_pack"
    triage_out = out_dir / "generic_triage_after_final_actionable_pack"

    run_cmd([
        sys.executable,
        REBUILD_SCRIPT,
        "--field",
        "additional_tags",
        "--apply",
    ])

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
    if summary_path.exists() and summary_path.stat().st_size > 0:
        summary = pd.read_csv(summary_path)
        print("")
        print("Final triage summary:")
        print(summary.to_string(index=False))

    print("")
    print(f"Final validation output: {triage_out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", default=None, help="Path to 02_remaining_actionable_labels.csv")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    labels_path = Path(args.labels) if args.labels else latest_actionable_labels_file()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Labels file: {labels_path}")
    print(f"Output folder: {out_dir}")

    labels_df = pd.read_csv(labels_path)
    plan_df = build_plan(labels_df)
    summary, leftovers = write_plan_outputs(plan_df, labels_df, out_dir)

    print("")
    print(f"Rows mapped for movement: {len(plan_df):,}")
    print(f"Occurrences mapped: {int(plan_df['value_count'].sum()) if not plan_df.empty else 0:,}")
    print(f"Unmapped actionable labels: {len(leftovers):,}")

    if not args.apply:
        print("")
        print("DRY RUN ONLY. No DB changes were made.")
        print("If the summary is acceptable, run:")
        print(f"python {Path(__file__).name} --apply")
        return

    apply_plan(plan_df)
    post_validate(out_dir)


if __name__ == "__main__":
    main()