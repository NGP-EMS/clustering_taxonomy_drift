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
SOURCE_CLUSTER_IDS = {"strict_315", "strict_38"}

DEFAULT_OUT_DIR = Path("outputs/final_residual_directionality_cleanup_20260601")


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


def load_cluster_members(conn, field_name, cluster_version, cluster_ids):
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
          AND m.final_cluster_id = ANY(%s)
        ORDER BY m.final_cluster_id, COALESCE(m.value_count, 1) DESC, m.normalized_label
    """
    return pd.read_sql_query(
        query,
        conn,
        params=(field_name, cluster_version, list(cluster_ids)),
    )


def match_strict315(label):
    l = normalize_label(label)

    # Challenge direction.
    if (
        l.startswith("customer challenged agent")
        or l.startswith("agent challenged by customer")
        or l.startswith("customer challenged previous agent")
    ):
        return (
            "manual_customer_challenged_agent",
            "Customer Challenged Agent",
            "Customer challenged agent or agent was challenged by customer.",
        )

    if (
        l.startswith("agent challenged customer")
        or l.startswith("agent challenged client")
        or l.startswith("agent challenged contact")
        or l.startswith("agent challenged dm")
    ):
        return (
            "manual_agent_challenged_customer",
            "Agent Challenged Customer",
            "Agent challenged customer/client/contact/DM.",
        )

    # Request direction. Only exact actor-target request labels.
    if (
        l.startswith("customer requested agent")
        or l.startswith("customer requested specific agent")
        or l.startswith("customer requested previous agent")
        or l.startswith("customer requested agent callback")
        or l.startswith("customer requested agent call back")
        or l.startswith("customer requested agent contact")
    ):
        return (
            "manual_customer_requested_agent",
            "Customer Requested Agent",
            "Customer requested agent, specific agent, callback, or contact.",
        )

    if (
        l.startswith("agent requested customer")
        or l.startswith("agent requested client")
        or l.startswith("agent requested contact")
        or l.startswith("agent requested dm")
        or l.startswith("agent requested customer details")
        or l.startswith("agent requested customer information")
    ):
        return (
            "manual_agent_requested_customer_action",
            "Agent Requested Customer Action",
            "Agent requested customer/client/contact/DM details, information, or action.",
        )

    # Claim / accusation direction. Exclude business-history claims.
    if (
        l.startswith("customer claimed agent")
        or l.startswith("customer claims agent")
        or l.startswith("customer claimed agent lied")
        or l.startswith("customer claimed agent misled")
        or l.startswith("customer claimed agent misleading")
        or l.startswith("customer claimed agent pushy")
    ):
        return (
            "manual_customer_reported_agent_misconduct",
            "Customer Reported Agent Misconduct",
            "Customer claim or accusation about agent behaviour.",
        )

    if (
        l.startswith("agent claimed customer")
        or l.startswith("agent claims customer")
        or l.startswith("agent claimed customer lied")
        or l.startswith("agent claimed customer abusive")
        or l.startswith("agent claimed customer hostile")
    ):
        return (
            "manual_agent_claimed_customer_issue",
            "Agent Claimed Customer Issue",
            "Agent claim about customer behaviour.",
        )

    # Question direction.
    if (
        l.startswith("customer questioned agent")
        or l.startswith("customer questioned previous agent")
        or l.startswith("agent questioned by customer")
    ):
        return (
            "manual_customer_questioned_agent",
            "Customer Questioned Agent",
            "Customer questioned agent or agent was questioned by customer.",
        )

    if (
        l.startswith("agent questioned customer")
        or l.startswith("agent questioned client")
        or l.startswith("agent questioned contact")
        or l.startswith("agent questioned dm")
    ):
        return (
            "manual_agent_questioned_customer",
            "Agent Questioned Customer",
            "Agent questioned customer/client/contact/DM.",
        )

    # Confusion caused by actor.
    if (
        l.startswith("customer confused by agent")
        or l.startswith("customer confused about agent")
        or l.startswith("customer confused by previous agent")
        or l.startswith("customer confused about previous agent")
        or l.startswith("customer confused due to agent")
    ):
        return (
            "manual_customer_confused_by_agent",
            "Customer Confused By Agent",
            "Customer confusion caused by current or previous agent.",
        )

    if (
        l.startswith("agent confused by customer")
        or l.startswith("agent confused about customer")
        or l.startswith("agent confused by client")
        or l.startswith("agent confused by contact")
        or l.startswith("agent confused by dm")
    ):
        return (
            "manual_agent_confused_by_customer",
            "Agent Confused By Customer",
            "Agent confusion caused by customer/client/contact/DM.",
        )

    # Refusal direction.
    if (
        l.startswith("customer refused agent")
        or l.startswith("customer refused to speak to agent")
        or l.startswith("customer refused agent contact")
        or l.startswith("customer refused further agent contact")
        or l.startswith("customer refused contact with agent")
        or l.startswith("dm refused agent")
        or l.startswith("contact refused agent")
        or l.startswith("client refused agent")
    ):
        return (
            "manual_customer_refused_agent_contact",
            "Customer Refused Agent Contact",
            "Customer/client/contact/DM refused agent contact.",
        )

    if (
        l.startswith("agent refused customer")
        or l.startswith("agent refused client")
        or l.startswith("agent refused contact")
        or l.startswith("agent refused dm")
        or l.startswith("agent refused to help customer")
        or l.startswith("agent refused customer request")
        or l.startswith("agent refused client request")
    ):
        return (
            "manual_agent_refused_customer",
            "Agent Refused Customer",
            "Agent refused customer/client/contact/DM request or contact.",
        )

    # Dispute direction.
    if (
        l.startswith("customer disputed agent")
        or l.startswith("customer disputed agent claim")
        or l.startswith("customer disputed previous agent")
        or l.startswith("agent disputed by customer")
    ):
        return (
            "manual_customer_disputed_agent",
            "Customer Disputed Agent",
            "Customer disputed agent, claim, or statement.",
        )

    if (
        l.startswith("agent disputed customer")
        or l.startswith("agent disputed client")
        or l.startswith("agent disputed contact")
        or l.startswith("agent disputed dm")
    ):
        return (
            "manual_agent_disputed_customer",
            "Agent Disputed Customer",
            "Agent disputed customer/client/contact/DM claim or statement.",
        )

    # Hostility direction.
    if (
        l.startswith("customer hostile to agent")
        or l.startswith("customer hostile agent")
        or l.startswith("customer hostile towards agent")
        or l.startswith("customer hostile due to agent")
    ):
        return (
            "manual_customer_hostile_to_agent",
            "Customer Hostile To Agent",
            "Customer hostility toward agent.",
        )

    if (
        l.startswith("agent hostile to customer")
        or l.startswith("agent hostile customer")
        or l.startswith("agent hostile towards customer")
        or l.startswith("agent hostile to client")
        or l.startswith("agent hostile to contact")
        or l.startswith("agent hostile to dm")
    ):
        return (
            "manual_agent_hostile_to_customer",
            "Agent Hostile To Customer",
            "Agent hostility toward customer/client/contact/DM.",
        )

    # Complaint direction.
    if (
        l.startswith("customer complained about agent")
        or l.startswith("customer complaint about agent")
        or l.startswith("customer complained agent")
        or l.startswith("customer complained of agent")
        or l.startswith("customer complaint previous agent")
        or l.startswith("customer complained previous agent")
    ):
        return (
            "manual_customer_reported_agent_misconduct",
            "Customer Reported Agent Misconduct",
            "Customer complaint or report about agent behaviour.",
        )

    if (
        l.startswith("agent complained about customer")
        or l.startswith("agent complaint about customer")
        or l.startswith("agent complained customer")
        or l.startswith("agent complained of customer")
        or l.startswith("agent complained about client")
        or l.startswith("agent complained about contact")
        or l.startswith("agent complained about dm")
    ):
        return (
            "manual_agent_complained_about_customer",
            "Agent Complained About Customer",
            "Agent complaint or report about customer/client/contact/DM behaviour.",
        )

    # Dismissive direction.
    if (
        l.startswith("agent dismissive to customer")
        or l.startswith("agent dismissive toward customer")
        or l.startswith("agent was dismissive to customer")
        or l.startswith("customer felt agent dismissive")
        or l.startswith("customer said agent dismissive")
        or l.startswith("customer called agent dismissive")
    ):
        return (
            "manual_agent_dismissive_to_customer",
            "Agent Dismissive To Customer",
            "Agent was dismissive toward customer.",
        )

    if (
        l.startswith("customer dismissive to agent")
        or l.startswith("customer dismissive toward agent")
        or l.startswith("customer was dismissive to agent")
    ):
        return (
            "manual_customer_dismissive_to_agent",
            "Customer Dismissive To Agent",
            "Customer was dismissive toward agent.",
        )

    # Shouting direction.
    if (
        l.startswith("agent shouted at customer")
        or l.startswith("agent shouting at customer")
        or l.startswith("customer shouted at by agent")
        or l.startswith("agent yelled at customer")
    ):
        return (
            "manual_agent_shouted_at_customer",
            "Agent Shouted At Customer",
            "Agent shouted/yelled at customer.",
        )

    if (
        l.startswith("customer shouted at agent")
        or l.startswith("customer shouting at agent")
        or l.startswith("agent shouted at by customer")
        or l.startswith("customer yelled at agent")
    ):
        return (
            "manual_customer_shouted_at_agent",
            "Customer Shouted At Agent",
            "Customer shouted/yelled at agent.",
        )

    # Unable/cannot. Only exact business direction.
    if (
        l.startswith("agent unable to help customer")
        or l.startswith("agent unable to assist customer")
        or l.startswith("agent cannot help customer")
        or l.startswith("agent cannot assist customer")
    ):
        return (
            "manual_agent_unable_to_help_customer",
            "Agent Unable To Help Customer",
            "Agent unable/cannot help or assist customer.",
        )

    if (
        l.startswith("customer unable to reach agent")
        or l.startswith("customer cannot reach agent")
        or l.startswith("customer unable to contact agent")
        or l.startswith("customer cannot contact agent")
    ):
        return (
            "manual_customer_unable_to_reach_agent",
            "Customer Unable To Reach Agent",
            "Customer unable/cannot reach or contact agent.",
        )

    # Hangup / termination direction.
    if (
        l.startswith("agent hung up on customer")
        or l.startswith("agent terminated call with customer")
        or l.startswith("agent ended call on customer")
        or l.startswith("customer hung up by agent")
        or l.startswith("customer call terminated by agent")
    ):
        return (
            "manual_agent_ended_call_on_customer",
            "Agent Ended Call On Customer",
            "Agent hung up, terminated, or ended call on customer.",
        )

    if (
        l.startswith("customer hung up on agent")
        or l.startswith("customer terminated call with agent")
        or l.startswith("customer ended call on agent")
        or l.startswith("agent hung up by customer")
        or l.startswith("agent call terminated by customer")
    ):
        return (
            "manual_customer_ended_call_on_agent",
            "Customer Ended Call On Agent",
            "Customer hung up, terminated, or ended call on agent.",
        )

    return None


def match_strict38(label):
    l = normalize_label(label)

    # Legal dispute residue.
    if (
        "sued broker" in l
        or "broker sued" in l
        or "sue broker" in l
        or "broker legal" in l
    ):
        return (
            "manual_broker_legal_dispute",
            "Broker Legal Dispute",
            "Legal dispute, sue, or sued language involving broker.",
        )

    # Broker refusal residue.
    if (
        l.startswith("customer refused broker")
        or l.startswith("customer refused brokers")
        or l.startswith("customer refused to speak to broker")
        or l.startswith("customer refused contact with broker")
        or l.startswith("customer refused further broker")
        or l.startswith("dm refused broker")
        or l.startswith("contact refused broker")
    ):
        return (
            "manual_customer_refused_broker_contact",
            "Customer Refused Broker Contact",
            "Customer/DM/contact refused broker contact.",
        )

    if (
        l.startswith("broker refused customer")
        or l.startswith("broker refused client")
        or l.startswith("broker refused owner")
        or l.startswith("broker refused tenant")
        or l.startswith("broker refused landlord")
    ):
        return (
            "manual_broker_refused_customer",
            "Broker Refused Customer",
            "Broker refused customer/client/owner/tenant/landlord.",
        )

    return None


def match_rule(source_cluster_id, label):
    if source_cluster_id == "strict_315":
        return match_strict315(label)

    if source_cluster_id == "strict_38":
        return match_strict38(label)

    return None


def build_plan(df):
    plan_rows = []
    review_rows = []
    seen = set()

    for r in df.to_dict("records"):
        clean_label = normalize_label(r["normalized_label"])
        matched = match_rule(r["source_cluster_id"], clean_label)

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
                "review_decision": "REVIEWED_NO_FIX_OR_DEFERRED",
                "reason": "No approved final residual split rule matched. Treat as workflow/status/parser/business-review unless manually reopened.",
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
                name_row["naming_method"] = "manual_final_residual_split"
            if "naming_reason" in n_cols:
                name_row["naming_reason"] = "Created during final residual directionality cleanup."
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
            params.append("manual_final_residual_directionality_cleanup")

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

        print(
            f"Refreshed stats: {cluster_id} "
            f"size={cluster_size}, occurrences={total_occurrences}, medoid={medoid_label}"
        )


def write_outputs(plan_df, review_df, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_path = out_dir / "01_final_residual_split_plan.csv"
    review_path = out_dir / "02_final_residual_left_for_review.csv"
    summary_path = out_dir / "03_final_residual_split_summary.csv"

    plan_df.to_csv(plan_path, index=False)
    review_df.to_csv(review_path, index=False)

    if not plan_df.empty:
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
    else:
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

    summary.to_csv(summary_path, index=False)

    print(f"Plan written: {plan_path}")
    print(f"Review/no-fix written: {review_path}")
    print(f"Summary written: {summary_path}")

    if not summary.empty:
        print("")
        print(summary.to_string(index=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--field", default=DEFAULT_FIELD)
    parser.add_argument("--cluster-version", default=DEFAULT_CLUSTER_VERSION)
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)

    with get_conn() as conn:
        df = load_cluster_members(
            conn,
            field_name=args.field,
            cluster_version=args.cluster_version,
            cluster_ids=SOURCE_CLUSTER_IDS,
        )

    if df.empty:
        raise SystemExit("No rows found for final residual cleanup.")

    plan_df, review_df = build_plan(df)
    write_outputs(plan_df, review_df, out_dir)

    print("")
    print("Final residual directionality cleanup dry-run")
    print(f"Source rows scanned: {len(df):,}")
    print(f"Rows selected to move: {len(plan_df):,}")
    print(f"Rows left for review/no-fix: {len(review_df):,}")
    print(f"Occurrences selected to move: {int(plan_df['value_count'].sum()) if not plan_df.empty else 0:,}")

    if not args.apply:
        print("")
        print("DRY RUN ONLY. No DB changes were made.")
        print("Open 01_final_residual_split_plan.csv and 03_final_residual_split_summary.csv before applying.")
        return

    if plan_df.empty:
        print("No rows selected. Nothing to apply.")
        return

    plan_rows = plan_df.to_dict("records")

    backup_label_map = f"backup_final_residual_label_map_{BACKUP_SUFFIX}"
    backup_clusters = f"backup_final_residual_clusters_{BACKUP_SUFFIX}"
    backup_names = f"backup_final_residual_cluster_names_{BACKUP_SUFFIX}"

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
            print("Final residual directionality cleanup committed successfully.")
            print(f"Total label-map rows moved: {moved_total}")

        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    main()