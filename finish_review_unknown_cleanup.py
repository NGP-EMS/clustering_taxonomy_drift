import os
import re
import sys
import subprocess
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
OUT_DIR = Path("outputs/review_unknown_final_cleanup_20260601")

LABELS_FILE = Path(
    "outputs/business_relationship_direction_cleanup_20260601/review_unknown_remaining_labels.csv"
)

REBUILD_SCRIPT = Path("rebuild_all_active_cluster_centroids_fixed.py")
AUDIT_SCRIPT = Path("audit_generic_actor_role_reversals_all_fields.py")
TRIAGE_SCRIPT = Path("triage_generic_actor_role_reversal_results.py")

KNOWN_FP_TO_ADD = [
    ('additional_tags', 'manual_agent_claimed_previous_contact'),
    ('additional_tags', 'manual_agent_confused_by_customer'),
    ('additional_tags', 'manual_agent_contact_confirmation'),
    ('additional_tags', 'manual_agent_manager_feedback'),
    ('additional_tags', 'manual_agent_role_misrepresentation'),
    ('additional_tags', 'manual_call_audio_hearing_issue'),
    ('additional_tags', 'manual_customer_agent_identity_mismatch'),
    ('additional_tags', 'manual_customer_confused_by_agent'),
    ('additional_tags', 'manual_landlord_agent_contact_issue'),
    ('additional_tags', 'manual_customer_recent_colleague_contact'),
]


def get_conn():
    return psycopg2.connect(
        host=os.getenv("LOCAL_PG_HOST") or "127.0.0.1",
        port=os.getenv("LOCAL_PG_PORT") or "5432",
        dbname=os.getenv("LOCAL_PG_DB") or "taxonomy_drift_local",
        user=os.getenv("LOCAL_PG_USER") or "postgres",
        password=os.getenv("LOCAL_PG_PASSWORD") or "postgres",
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


def map_review_unknown(source_cluster_id, label):
    l = normalize_label(label)

    if source_cluster_id == "base_2017":
        if l == "agent did not speak to dm":
            return (
                "manual_agent_did_not_speak_to_dm",
                "Agent Did Not Speak To DM",
                "Agent did not speak to DM.",
            )
        if l == "dm did not recognize agent":
            return (
                "manual_dm_did_not_recognize_agent",
                "DM Did Not Recognize Agent",
                "DM did not recognize agent.",
            )

    if source_cluster_id == "base_2172":
        if l == "agent recognized previous contact":
            return (
                "manual_agent_recognized_previous_contact",
                "Agent Recognized Previous Contact",
                "Agent recognized previous contact.",
            )
        if l == "contact recognized agent":
            return (
                "manual_contact_recognized_agent",
                "Contact Recognized Agent",
                "Contact recognized agent.",
            )

    if source_cluster_id == "base_2295":
        if starts_any(l, [
            "customer just called by colleague",
            "customer just spoke to colleague",
        ]):
            return (
                "manual_customer_recent_colleague_contact",
                "Customer Recent Colleague Contact",
                "Customer was just called by or recently spoke to colleague.",
            )

    if source_cluster_id == "base_542":
        if starts_any(l, [
            "agent claims prior contact",
            "agent claims recent contact",
            "agent claimed direct contact",
            "agent claimed business contact",
            "agent claimed current contact",
            "agent claimed false contact",
            "agent claimed false previous contact",
            "agent falsely claimed previous contact",
            "agent claimed personal contact",
            "agent claimed existing contact",
            "agent claimed direct dm contact",
            "agent claimed direct contact info",
            "agent falsely claimed prior contact",
        ]):
            return (
                "manual_agent_claimed_previous_contact",
                "Agent Claimed Previous Contact",
                "Agent claimed previous, prior, direct, current, business, personal, or false contact.",
            )

    if source_cluster_id == "strict_304":
        if starts_any(l, [
            "agent claims recent dm engagement",
            "agent claimed previous dm consent",
            "agent claimed previous dm engagement",
        ]):
            return (
                "manual_agent_claimed_previous_dm_engagement",
                "Agent Claimed Previous DM Engagement",
                "Agent claimed previous or recent DM engagement/consent.",
            )

    if source_cluster_id == "strict_126":
        if l == "agent pitched existing customer":
            return (
                "manual_agent_pitched_existing_customer",
                "Agent Pitched Existing Customer",
                "Agent pitched existing customer.",
            )
        if starts_any(l, [
            "customer pitched agent",
            "customer pitches agent",
            "customer pitched agent on job",
        ]):
            return (
                "manual_customer_pitched_agent",
                "Customer Pitched Agent",
                "Customer pitched agent or pitched agent on job.",
            )

    if source_cluster_id == "strict_340":
        if l == "agent rescheduling for dm":
            return (
                "manual_agent_rescheduled_for_dm",
                "Agent Rescheduled For DM",
                "Agent rescheduled for DM.",
            )
        if l == "customer rescheduled consultant meeting":
            return (
                "manual_customer_rescheduled_consultant_meeting",
                "Customer Rescheduled Consultant Meeting",
                "Customer rescheduled consultant meeting.",
            )

    if source_cluster_id == "strict_524":
        if starts_any(l, [
            "agent coaching customer",
            "agent coached customer",
            "agent coaching dm",
        ]):
            return (
                "manual_agent_coached_customer",
                "Agent Coached Customer",
                "Agent coached customer/DM on negotiation, gatekeeping, email, competitors, avoidance, or verification.",
            )
        if l == "customer coaching agent":
            return (
                "manual_customer_coached_agent",
                "Customer Coached Agent",
                "Customer coached agent.",
            )

    if source_cluster_id == "strict_11":
        if starts_any(l, [
            "customer awaiting information from colleague",
            "customer awaiting internal colleague",
        ]):
            return (
                "manual_customer_awaiting_internal_colleague",
                "Customer Awaiting Internal Colleague",
                "Customer awaiting information or response from internal colleague.",
            )

    if source_cluster_id == "strict_595":
        if l == "landlord agent contact issue":
            return (
                "manual_landlord_agent_contact_issue",
                "Landlord Agent Contact Issue",
                "Landlord-agent contact issue.",
            )

    return None


def build_plan(labels_df):
    rows = []

    for r in labels_df.to_dict("records"):
        source_cluster_id = str(r.get("cluster_id", ""))
        normalized_label = str(r.get("normalized_label", ""))
        raw_label = str(r.get("raw_label", normalized_label))
        field_name = str(r.get("field_name", "additional_tags"))
        cluster_version = str(r.get("cluster_version", "20260513_093749"))
        value_count = int(r.get("value_count", 1) or 1)

        mapped = map_review_unknown(source_cluster_id, normalized_label)
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
                name_row["naming_method"] = "manual_review_unknown_final_cleanup"
            if "naming_reason" in n_cols:
                name_row["naming_reason"] = "Created from final REVIEW_UNKNOWN cleanup."
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
            params.append("manual_review_unknown_final_cleanup")

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


def patch_triage_false_positive_clusters():
    path = TRIAGE_SCRIPT
    text = path.read_text(encoding="utf-8", errors="ignore")

    additions = []
    for field_name, cluster_id in KNOWN_FP_TO_ADD:
        literal = f'("{field_name}", "{cluster_id}")'
        single_literal = f"('{field_name}', '{cluster_id}')"

        if literal in text or single_literal in text:
            continue

        additions.append(
            f'    ("{field_name}", "{cluster_id}"): "LIKELY_FALSE_POSITIVE_OR_STATUS_SYMMETRY",'
        )

    if not additions:
        print("No triage false-positive entries needed.")
        return

    marker = "KNOWN_FALSE_POSITIVE_CLUSTERS"
    idx = text.find(marker)
    if idx == -1:
        raise RuntimeError("Could not find KNOWN_FALSE_POSITIVE_CLUSTERS in triage script.")

    brace_idx = text.find("{", idx)
    if brace_idx == -1:
        raise RuntimeError("Could not find opening brace for KNOWN_FALSE_POSITIVE_CLUSTERS.")

    insert_at = brace_idx + 1
    patched = text[:insert_at] + "\n" + "\n".join(additions) + text[insert_at:]

    backup = path.with_suffix(f".py.backup_review_unknown_{BACKUP_SUFFIX}")
    backup.write_text(text, encoding="utf-8")
    path.write_text(patched, encoding="utf-8")

    print(f"Patched triage false-positive clusters. Backup: {backup}")


def run_cmd(cmd):
    print("\n" + "=" * 120)
    print("RUN:", " ".join(str(x) for x in cmd))
    print("=" * 120)

    result = subprocess.run(
        [str(x) for x in cmd],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    print(result.stdout)

    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(x) for x in cmd)}")


def post_validate():
    audit_out = OUT_DIR / "generic_after_review_unknown_cleanup"
    triage_out = OUT_DIR / "generic_triage_after_review_unknown_cleanup"

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
    if not LABELS_FILE.exists():
        raise FileNotFoundError(LABELS_FILE)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    labels_df = pd.read_csv(LABELS_FILE)
    plan_df = build_plan(labels_df)

    plan_path = OUT_DIR / "01_review_unknown_move_plan.csv"
    summary_path = OUT_DIR / "02_review_unknown_move_summary.csv"
    unmapped_path = OUT_DIR / "03_review_unknown_unmapped.csv"

    plan_df.to_csv(plan_path, index=False)

    if plan_df.empty:
        summary_df = pd.DataFrame()
    else:
        summary_df = (
            plan_df.groupby(["source_cluster_id", "target_cluster_id", "target_display_name"])
            .agg(
                label_rows=("normalized_label", "count"),
                occurrences=("value_count", "sum"),
                reasons=("reason", lambda s: " | ".join(sorted(set(s)))),
            )
            .reset_index()
            .sort_values(["source_cluster_id", "occurrences"], ascending=[True, False])
        )

    summary_df.to_csv(summary_path, index=False)

    mapped_keys = set(
        (r["source_cluster_id"], r["normalized_label"])
        for r in plan_df.to_dict("records")
    )

    unmapped = labels_df[
        labels_df.apply(
            lambda r: (str(r.get("cluster_id", "")), str(r.get("normalized_label", ""))) not in mapped_keys,
            axis=1,
        )
    ].copy()
    unmapped.to_csv(unmapped_path, index=False)

    print("")
    print("REVIEW_UNKNOWN move summary:")
    if summary_df.empty:
        print("No DB moves mapped.")
    else:
        print(summary_df.to_string(index=False))

    print("")
    print(f"Rows mapped: {len(plan_df):,}")
    print(f"Occurrences mapped: {int(plan_df['value_count'].sum()) if not plan_df.empty else 0:,}")
    print(f"Rows unmapped: {len(unmapped):,}")
    print("")
    print(f"Plan: {plan_path}")
    print(f"Summary: {summary_path}")
    print(f"Unmapped: {unmapped_path}")

    if "--apply" not in sys.argv:
        print("")
        print("DRY RUN ONLY. No DB changes made.")
        print("Run with --apply after reviewing summary.")
        return

    plan_rows = plan_df.to_dict("records")

    backup_label_map = f"backup_review_unknown_label_map_{BACKUP_SUFFIX}"
    backup_clusters = f"backup_review_unknown_clusters_{BACKUP_SUFFIX}"
    backup_names = f"backup_review_unknown_cluster_names_{BACKUP_SUFFIX}"

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

        except Exception:
            conn.rollback()
            raise

    patch_triage_false_positive_clusters()
    post_validate()


if __name__ == "__main__":
    main()