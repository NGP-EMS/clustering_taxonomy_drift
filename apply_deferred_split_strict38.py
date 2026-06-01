import argparse
import os
import re
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

DEFAULT_FIELD = "additional_tags"
DEFAULT_CLUSTER_VERSION = "20260513_093749"
SOURCE_CLUSTER_ID = "strict_38"
DEFAULT_OUT_DIR = Path("outputs/deferred_split_strict38_20260601")


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


def normalize_label(value):
    if value is None:
        return ""
    value = str(value).strip().lower()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"[^a-z0-9\s]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def load_cluster_members(conn, field_name, cluster_version, cluster_id):
    query = """
        SELECT
            m.field_name,
            m.cluster_version,
            m.final_cluster_id AS source_cluster_id,
            m.raw_label,
            m.normalized_label,
            COALESCE(m.value_count, 1) AS value_count,
            c.medoid_label,
            c.cluster_size,
            c.total_occurrences
        FROM taxonomy_label_cluster_map m
        JOIN taxonomy_clusters c
          ON c.field_name = m.field_name
         AND c.cluster_version = m.cluster_version
         AND c.cluster_id = m.final_cluster_id
        WHERE m.field_name = %s
          AND m.cluster_version = %s
          AND m.final_cluster_id = %s
        ORDER BY COALESCE(m.value_count, 1) DESC, m.normalized_label
    """
    return pd.read_sql_query(query, conn, params=(field_name, cluster_version, cluster_id))


def match_rule(label):
    """
    Conservative strict_38 / Broker Call Fatigue split rules.

    Only clear broker/customer business meanings are moved.
    Vague parser rows are intentionally left in strict_38.
    """
    l = normalize_label(label)

    # Customer confusion about broker role / relationship / involvement.
    if (
        l.startswith("customer confused by broker")
        or l.startswith("customer confused by other broker")
        or l.startswith("customer confused about broker")
        or l.startswith("customer confused broker")
        or l.startswith("customer confused by previous broker")
    ):
        return (
            "manual_customer_confused_by_broker",
            "Customer Confused By Broker",
            "Customer confusion about broker role, involvement, relationship, or volume.",
        )

    # Customer hostility toward broker or due to broker issue.
    if (
        l.startswith("customer hostile to broker")
        or l.startswith("customer hostile broker")
        or l.startswith("customer hostile due to previous broker")
        or l.startswith("customer hostile due to broker")
    ):
        return (
            "manual_customer_hostile_to_broker",
            "Customer Hostile To Broker",
            "Customer hostility toward broker or caused by previous broker issue.",
        )

    # Customer burnt / conned / harmed by broker.
    if (
        "burnt by broker" in l
        or "burnt by previous broker" in l
        or "burnt fingers previous broker" in l
        or "burnt by current broker" in l
        or "conned by broker" in l
        or "conned by current broker" in l
        or "conned by previous broker" in l
        or "burned by broker" in l
        or "burned by previous broker" in l
    ):
        return (
            "manual_customer_burnt_or_conn_by_broker",
            "Customer Burnt Or Conned By Broker",
            "Customer reports being burnt, conned, or harmed by broker.",
        )

    # Customer frustration caused by broker / broker volume / previous broker silence.
    if (
        l.startswith("customer frustrated by broker")
        or l.startswith("customer frustrated with broker")
        or l.startswith("customer frustrated by previous broker")
        or l.startswith("customer frustrated with previous broker")
    ):
        return (
            "manual_customer_frustrated_by_broker",
            "Customer Frustrated By Broker",
            "Customer frustration caused by broker, broker volume, or previous broker handling.",
        )

    # Customer / DM claimed existing broker, competitor broker, or broker engagement.
    if (
        l.startswith("customer claimed existing broker")
        or l.startswith("customer claimed different broker")
        or l.startswith("customer claims existing broker")
        or l.startswith("customer claims competitor broker")
        or l.startswith("customer claims broker engaged")
        or l.startswith("customer claims broker increases cost")
        or l.startswith("dm claims existing broker")
        or l.startswith("previous broker contact claimed")
    ):
        return (
            "manual_customer_claimed_existing_broker",
            "Customer Claimed Existing Broker",
            "Customer/DM claimed existing, different, competitor, or engaged broker.",
        )

    # Broker querying or acting on behalf of client/customer.
    if (
        l.startswith("broker query on behalf of client")
        or l.startswith("broker query on behalf of customer")
        or l.startswith("broker querying on behalf of client")
        or l.startswith("broker querying on behalf of customer")
        or l.startswith("broker querying client account")
        or l.startswith("broker querying client data")
        or l.startswith("broker querying customer account")
        or l.startswith("broker querying customer data")
    ):
        return (
            "manual_broker_query_on_behalf_of_customer",
            "Broker Query On Behalf Of Customer",
            "Broker queried or acted on behalf of customer/client.",
        )

    # Customer mentions existing broker / broker mentioned by customer.
    if (
        l.startswith("customer mentions existing broker")
        or l.startswith("broker mentioned by customer")
        or l.startswith("previous broker contact mentioned")
        or l.startswith("customer mentioned existing broker")
        or l.startswith("customer mentioned broker")
    ):
        return (
            "manual_customer_mentioned_existing_broker",
            "Customer Mentioned Existing Broker",
            "Customer mentioned broker or existing broker relationship.",
        )

    # Customer has broker fatigue / repeated broker contact fatigue.
    if (
        l.startswith("customer broker fatigue")
        or l.startswith("repeated broker contact fatigue")
        or l.startswith("high broker contact fatigue")
        or l.startswith("broker contact fatigue")
    ):
        return (
            "manual_customer_broker_fatigue",
            "Customer Broker Fatigue",
            "Customer fatigue caused by repeated broker contact or broker volume.",
        )

    # Threat direction: customer threatening switch/blacklist/action against broker.
    if (
        l.startswith("customer threatened to switch broker")
        or l.startswith("customer threatened broker switch")
        or l.startswith("customer threatened to blacklist broker")
        or l.startswith("customer threatened broker")
        or l.startswith("customer threatens broker")
    ):
        return (
            "manual_customer_threatened_broker_action",
            "Customer Threatened Broker Action",
            "Customer threatened broker switch, blacklist, or action.",
        )

    # Threat direction: broker threatening customer.
    if (
        l.startswith("broker threatened customer")
        or l.startswith("broker threatened owner")
        or l.startswith("broker threatened client")
        or l.startswith("broker threatened tenant")
        or l.startswith("broker threatened landlord")
    ):
        return (
            "manual_broker_threatened_customer",
            "Broker Threatened Customer",
            "Broker threatened customer/client/owner/tenant/landlord.",
        )

    # Legal issue: customer says broker sued / legal escalation.
    if (
        l.startswith("customer sued broker")
        or l.startswith("customer threatened to sue broker")
        or l.startswith("customer says broker sued")
        or l.startswith("customer claimed broker sued")
        or l.startswith("broker sued customer")
    ):
        return (
            "manual_broker_legal_dispute",
            "Broker Legal Dispute",
            "Legal dispute or sue/sued language involving broker.",
        )

    # Refusal to deal with broker / broker contact.
    # Conservative: only move explicit broker-refusal labels.
    if (
        l.startswith("customer refused broker")
        or l.startswith("customer refused to speak to broker")
        or l.startswith("customer refused broker contact")
        or l.startswith("customer refused further broker contact")
        or l.startswith("customer refused contact with broker")
        or l.startswith("dm refused broker")
        or l.startswith("contact refused broker")
    ):
        return (
            "manual_customer_refused_broker_contact",
            "Customer Refused Broker Contact",
            "Customer/DM/contact refused broker contact.",
        )

    return None


def build_plan(df):
    plan_rows = []
    review_rows = []
    seen = set()

    for r in df.to_dict("records"):
        clean_label = normalize_label(r["normalized_label"])
        matched = match_rule(clean_label)

        if matched:
            target_cluster_id, target_display_name, reason = matched

            key = (
                r["field_name"],
                r["cluster_version"],
                r["source_cluster_id"],
                r["raw_label"],
                r["normalized_label"],
                target_cluster_id,
            )

            if key not in seen:
                seen.add(key)
                plan_rows.append({
                    "field_name": r["field_name"],
                    "cluster_version": r["cluster_version"],
                    "source_cluster_id": r["source_cluster_id"],
                    "raw_label": r["raw_label"],
                    "normalized_label": r["normalized_label"],
                    "clean_label": clean_label,
                    "value_count": int(r["value_count"] or 1),
                    "target_cluster_id": target_cluster_id,
                    "target_display_name": target_display_name,
                    "cleanup_decision": "MOVE_TO_MANUAL_SPLIT_CLUSTER",
                    "reason": reason,
                })
        else:
            review_rows.append({
                "field_name": r["field_name"],
                "cluster_version": r["cluster_version"],
                "source_cluster_id": r["source_cluster_id"],
                "raw_label": r["raw_label"],
                "normalized_label": r["normalized_label"],
                "clean_label": clean_label,
                "value_count": int(r["value_count"] or 1),
                "review_decision": "LEFT_IN_STRICT_38_FOR_NOW",
                "reason": "No conservative broker split rule matched.",
            })

    return pd.DataFrame(plan_rows), pd.DataFrame(review_rows)


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
                raise ValueError(f"Source cluster not found: {source_cluster_id}")

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
                name_row["naming_method"] = "manual_deferred_split"
            if "naming_reason" in n_cols:
                name_row["naming_reason"] = "Created during deferred strict_38 broker split cleanup."
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


def move_rows(conn, plan_rows):
    m_cols = table_columns(conn, "taxonomy_label_cluster_map")
    moved_total = 0

    for r in plan_rows:
        set_parts = [sql.SQL("final_cluster_id = %s")]
        params = [r["target_cluster_id"]]

        if "final_cluster_source" in m_cols:
            set_parts.append(sql.SQL("final_cluster_source = %s"))
            params.append("manual_deferred_split_strict38")

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

        print(f"Moved {moved}: {r['normalized_label']} -> {r['target_cluster_id']}")

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

        print(f"Refreshed stats: {cluster_id} size={cluster_size}, occurrences={total_occurrences}, medoid={medoid_label}")


def write_outputs(plan_df, review_df, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_path = out_dir / "01_strict38_split_plan.csv"
    review_path = out_dir / "02_strict38_left_for_review.csv"
    summary_path = out_dir / "03_strict38_split_summary.csv"

    plan_df.to_csv(plan_path, index=False)
    review_df.to_csv(review_path, index=False)

    if not plan_df.empty:
        summary = (
            plan_df.groupby(["target_cluster_id", "target_display_name"])
            .agg(
                label_rows=("normalized_label", "count"),
                occurrences=("value_count", "sum"),
                reasons=("reason", lambda s: " | ".join(sorted(set(s)))),
            )
            .reset_index()
            .sort_values("occurrences", ascending=False)
        )
    else:
        summary = pd.DataFrame(columns=["target_cluster_id", "target_display_name", "label_rows", "occurrences", "reasons"])

    summary.to_csv(summary_path, index=False)

    print(f"Plan written: {plan_path}")
    print(f"Review leftovers written: {review_path}")
    print(f"Summary written: {summary_path}")

    if not summary.empty:
        print("")
        print(summary.to_string(index=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--field", default=DEFAULT_FIELD)
    parser.add_argument("--cluster-version", default=DEFAULT_CLUSTER_VERSION)
    parser.add_argument("--cluster-id", default=SOURCE_CLUSTER_ID)
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)

    with get_conn() as conn:
        df = load_cluster_members(
            conn,
            field_name=args.field,
            cluster_version=args.cluster_version,
            cluster_id=args.cluster_id,
        )

    if df.empty:
        raise SystemExit("No rows found for strict_38.")

    plan_df, review_df = build_plan(df)

    write_outputs(plan_df, review_df, out_dir)

    print("")
    print("Strict 38 deferred broker split dry-run")
    print(f"Source cluster rows: {len(df):,}")
    print(f"Rows selected to move: {len(plan_df):,}")
    print(f"Rows left for review: {len(review_df):,}")
    print(f"Occurrences selected to move: {int(plan_df['value_count'].sum()) if not plan_df.empty else 0:,}")

    if not args.apply:
        print("")
        print("DRY RUN ONLY. No DB changes were made.")
        print("Open 01_strict38_split_plan.csv and 03_strict38_split_summary.csv before applying.")
        return

    if plan_df.empty:
        print("No rows selected. Nothing to apply.")
        return

    plan_rows = plan_df.to_dict("records")

    backup_label_map = f"backup_strict38_split_label_map_{BACKUP_SUFFIX}"
    backup_clusters = f"backup_strict38_split_clusters_{BACKUP_SUFFIX}"
    backup_names = f"backup_strict38_split_cluster_names_{BACKUP_SUFFIX}"

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
            print("strict_38 deferred split committed successfully.")
            print(f"Total label-map rows moved: {moved_total}")

        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    main()