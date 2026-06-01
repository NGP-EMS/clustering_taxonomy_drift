import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from psycopg2.extras import Json
import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql


load_dotenv()


STAGING_PATH = Path(
    "outputs/generic_actor_role_reversal_triage_20260601/08_confirmed_cleanup_staging.csv"
)

BACKUP_SUFFIX = datetime.now().strftime("%Y%m%d_%H%M%S")


def get_conn():
    return psycopg2.connect(
        host=os.getenv("LOCAL_PG_HOST") or "127.0.0.1",
        port=os.getenv("LOCAL_PG_PORT") or "5432",
        dbname=os.getenv("LOCAL_PG_DB") or "taxonomy_drift_local",
        user=os.getenv("LOCAL_PG_USER") or "postgres",
        password=os.getenv("LOCAL_PG_PASSWORD") or "postgres",
    )

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


def create_backup_table(conn, source_table, backup_table):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} AS SELECT * FROM {} WHERE false")
            .format(sql.Identifier(backup_table), sql.Identifier(source_table))
        )


def backup_label_rows(conn, rows, backup_table):
    with conn.cursor() as cur:
        for r in rows:
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
                """).format(sql.Identifier(backup_table)),
                (
                    r["field_name"],
                    r["cluster_version"],
                    r["cluster_id"],
                    r["normalized_label"],
                    r["raw_label"],
                ),
            )


def backup_cluster_rows(conn, rows, backup_table):
    cluster_keys = set()

    for r in rows:
        cluster_keys.add((r["field_name"], r["cluster_version"], r["cluster_id"]))
        cluster_keys.add((r["field_name"], r["cluster_version"], r["target_cluster_id"]))

    with conn.cursor() as cur:
        for field_name, cluster_version, cluster_id in cluster_keys:
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}
                    SELECT *
                    FROM taxonomy_clusters
                    WHERE field_name = %s
                      AND cluster_version = %s
                      AND cluster_id = %s
                """).format(sql.Identifier(backup_table)),
                (field_name, cluster_version, cluster_id),
            )


def backup_name_rows(conn, rows, backup_table):
    cluster_keys = set()

    for r in rows:
        cluster_keys.add((r["field_name"], r["cluster_version"], r["cluster_id"]))
        cluster_keys.add((r["field_name"], r["cluster_version"], r["target_cluster_id"]))

    with conn.cursor() as cur:
        for field_name, cluster_version, cluster_id in cluster_keys:
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}
                    SELECT *
                    FROM taxonomy_cluster_names
                    WHERE field_name = %s
                      AND cluster_version = %s
                      AND cluster_id = %s
                """).format(sql.Identifier(backup_table)),
                (field_name, cluster_version, cluster_id),
            )


def read_staging(path):
    if not path.exists():
        raise FileNotFoundError(f"Staging file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    required = {
        "field_name",
        "cluster_version",
        "cluster_id",
        "raw_label",
        "normalized_label",
        "cleanup_decision",
        "target_cluster_id",
        "target_display_name",
    }

    missing = required - set(rows[0].keys()) if rows else required
    if missing:
        raise ValueError(f"Missing required columns in staging CSV: {sorted(missing)}")

    rows = [
        r for r in rows
        if r["cleanup_decision"] == "MOVE_TO_MANUAL_DIRECTIONAL_CLUSTER"
    ]

    if not rows:
        raise ValueError("No MOVE_TO_MANUAL_DIRECTIONAL_CLUSTER rows found.")

    return rows


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


def create_target_clusters(conn, rows):
    c_cols = table_columns(conn, "taxonomy_clusters")
    n_cols = table_columns(conn, "taxonomy_cluster_names")

    grouped = defaultdict(list)
    for r in rows:
        grouped[
            (
                r["field_name"],
                r["cluster_version"],
                r["cluster_id"],
                r["target_cluster_id"],
                r["target_display_name"],
            )
        ].append(r)

    for (field_name, cluster_version, source_cluster_id, target_cluster_id, target_display_name), group_rows in grouped.items():
        if target_cluster_exists(conn, field_name, cluster_version, target_cluster_id):
            print(f"Target cluster already exists: {field_name} / {target_cluster_id}")
        else:
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

            # Avoid copying PK/id values.
            for pk_col in ["id"]:
                insert_row.pop(pk_col, None)

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
                top_row = max(group_rows, key=lambda x: int(float(x.get("value_count") or 0)))
                insert_row["medoid_label"] = top_row["normalized_label"]

            # Force centroid rebuild after movement.
            if "centroid_embedding" in c_cols:
                insert_row["centroid_embedding"] = None

            if "medoid_similarity_to_centroid" in c_cols:
                insert_row["medoid_similarity_to_centroid"] = None

            if "representative_labels" in c_cols:
                insert_row["representative_labels"] = None

            now = datetime.now()
            if "created_at" in c_cols:
                insert_row["created_at"] = now
            if "updated_at" in c_cols:
                insert_row["updated_at"] = now

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

        # Add target cluster display name if missing.
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
                name_row["naming_reason"] = (
                    "Created during confirmed actor-target directionality cleanup."
                )
            if "created_at" in n_cols:
                name_row["created_at"] = datetime.now()
            if "updated_at" in n_cols:
                name_row["updated_at"] = datetime.now()

            cols = list(name_row.keys())
            values = [name_row[c] for c in cols]

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


def move_label_rows(conn, rows):
    m_cols = table_columns(conn, "taxonomy_label_cluster_map")

    moved_total = 0

    for r in rows:
        set_parts = [
            sql.SQL("final_cluster_id = %s")
        ]
        params = [r["target_cluster_id"]]

        if "final_cluster_source" in m_cols:
            set_parts.append(sql.SQL("final_cluster_source = %s"))
            params.append("manual_directionality_cleanup")

        if "final_is_true_anomaly" in m_cols:
            set_parts.append(sql.SQL("final_is_true_anomaly = %s"))
            params.append(False)

        if "updated_at" in m_cols:
            set_parts.append(sql.SQL("updated_at = NOW()"))

        params.extend([
            r["field_name"],
            r["cluster_version"],
            r["cluster_id"],
            r["normalized_label"],
            r["raw_label"],
        ])

        query = sql.SQL("""
            UPDATE taxonomy_label_cluster_map
            SET {}
            WHERE field_name = %s
              AND cluster_version = %s
              AND final_cluster_id = %s
              AND normalized_label = %s
              AND raw_label = %s
        """).format(sql.SQL(", ").join(set_parts))

        with conn.cursor() as cur:
            cur.execute(query, params)
            moved = cur.rowcount
            moved_total += moved

        print(
            f"Moved {moved} row(s): {r['cluster_id']} / {r['normalized_label']} "
            f"-> {r['target_cluster_id']}"
        )

    return moved_total


def refresh_basic_cluster_stats(conn, rows):
    c_cols = table_columns(conn, "taxonomy_clusters")

    affected_clusters = set()
    for r in rows:
        affected_clusters.add((r["field_name"], r["cluster_version"], r["cluster_id"]))
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

        # Clear centroid fields so the rebuild script refreshes them properly.
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


def print_validation(conn, rows):
    affected = sorted({
        (r["field_name"], r["cluster_version"], r["cluster_id"])
        for r in rows
    } | {
        (r["field_name"], r["cluster_version"], r["target_cluster_id"])
        for r in rows
    })

    print("")
    print("Post-cleanup cluster counts:")

    with conn.cursor() as cur:
        for field_name, cluster_version, cluster_id in affected:
            cur.execute(
                """
                SELECT
                    COUNT(*) AS label_rows,
                    SUM(COALESCE(value_count, 1)) AS occurrences
                FROM taxonomy_label_cluster_map
                WHERE field_name = %s
                  AND cluster_version = %s
                  AND final_cluster_id = %s
                """,
                (field_name, cluster_version, cluster_id),
            )
            label_rows, occurrences = cur.fetchone()
            print(
                f"{field_name} / {cluster_id}: "
                f"label_rows={label_rows}, occurrences={occurrences or 0}"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", default=str(STAGING_PATH))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    staging_path = Path(args.staging)
    rows = read_staging(staging_path)

    print("Confirmed cleanup staging loaded")
    print(f"Rows to move: {len(rows)}")
    print("")

    for r in rows:
        print(
            f"{r['field_name']} / {r['cluster_id']} / {r['normalized_label']} "
            f"-> {r['target_cluster_id']}"
        )

    if not args.apply:
        print("")
        print("DRY RUN ONLY. No DB changes were made.")
        print("Run with --apply to execute the cleanup.")
        return

    backup_label_map = f"backup_directionality_label_map_{BACKUP_SUFFIX}"
    backup_clusters = f"backup_directionality_clusters_{BACKUP_SUFFIX}"
    backup_names = f"backup_directionality_cluster_names_{BACKUP_SUFFIX}"

    with get_conn() as conn:
        try:
            create_backup_table(conn, "taxonomy_label_cluster_map", backup_label_map)
            create_backup_table(conn, "taxonomy_clusters", backup_clusters)
            create_backup_table(conn, "taxonomy_cluster_names", backup_names)

            backup_label_rows(conn, rows, backup_label_map)
            backup_cluster_rows(conn, rows, backup_clusters)
            backup_name_rows(conn, rows, backup_names)

            print("")
            print("Backups created:")
            print(f"- {backup_label_map}")
            print(f"- {backup_clusters}")
            print(f"- {backup_names}")
            print("")

            create_target_clusters(conn, rows)
            moved_total = move_label_rows(conn, rows)
            refresh_basic_cluster_stats(conn, rows)
            print_validation(conn, rows)

            conn.commit()

            print("")
            print("Cleanup committed successfully.")
            print(f"Total label-map rows moved: {moved_total}")

        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    main()