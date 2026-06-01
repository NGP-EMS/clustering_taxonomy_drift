#!/usr/bin/env python3
"""
scan_inverse_direction_clusters.py

Read-only scan for actor-target inverse direction issues.

Finds clusters where opposite-direction labels are grouped together, e.g.
    agent_insulted_customer
    customer_insulted_agent

Run:
    python scan_inverse_direction_clusters.py --env-file .env --field-name additional_tags --with-app-counts

Full all-field scan:
    python scan_inverse_direction_clusters.py --env-file .env --with-app-counts
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Optional

import pandas as pd
import psycopg2
from dotenv import load_dotenv


AGENT_RE = r"(agent|advisor|adviser|broker|staff|rep|representative|consultant)"
CUSTOMER_RE = r"(customer|client|caller|contact|dm|decision maker|decisionmaker|recipient|prospect)"

INSULT_RE = r"(insult|insulted|insulting|offend|offended|offensive)"
SHOUT_RE = r"(shout|shouted|shouting|yell|yelled|yelling)"
RUDE_RE = r"(rude|rudeness|impolite|disrespectful|aggressive|politeness)"
REPORT_RE = r"(accused|called|reported|complaint|complained|commented|confronted|challenged|unhappy|perceived)"


def normalize_label(value: str) -> str:
    value = str(value or "")
    value = value.replace("_", " ").replace("-", " ").replace("/", " ")
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def pg_conn(prefix: str):
    if prefix == "CLUSTER_DB":
        conn_str = os.getenv("CLUSTER_DB_CONN_STR") or os.getenv("LOCAL_PG_CONN_STR") or os.getenv("PG_CONN_STR")
        if conn_str:
            print("Connecting CLUSTER_DB using connection string")
            return psycopg2.connect(conn_str)

        host = os.getenv("CLUSTER_DB_HOST") or os.getenv("LOCAL_PG_HOST") or "127.0.0.1"
        port = os.getenv("CLUSTER_DB_PORT") or os.getenv("LOCAL_PG_PORT") or "5432"
        user = os.getenv("CLUSTER_DB_USER") or os.getenv("LOCAL_PG_USER") or "postgres"
        password = os.getenv("CLUSTER_DB_PASS") or os.getenv("LOCAL_PG_PASSWORD") or "postgres"
        dbname = os.getenv("CLUSTER_DB_NAME") or os.getenv("LOCAL_PG_DB") or "taxonomy_drift_local"

    elif prefix == "APP_DB":
        conn_str = os.getenv("AI_CALL_DB_CONN_STR") or os.getenv("APP_DB_CONN_STR") or os.getenv("DB_CONN_STR")
        if conn_str:
            print("Connecting APP_DB using connection string")
            return psycopg2.connect(conn_str)

        host = os.getenv("AI_CALL_DB_HOST") or os.getenv("APP_DB_HOST") or os.getenv("DB_HOST")
        port = os.getenv("AI_CALL_DB_PORT") or os.getenv("APP_DB_PORT") or os.getenv("DB_PORT") or "5432"
        user = os.getenv("AI_CALL_DB_USER") or os.getenv("APP_DB_USER") or os.getenv("DB_USER")
        password = (
            os.getenv("AI_CALL_DB_PASS")
            or os.getenv("APP_DB_PASS")
            or os.getenv("DB_PASSWORD")
            or os.getenv("DB_PASS")
        )
        dbname = os.getenv("AI_CALL_DB_NAME") or os.getenv("APP_DB_NAME") or os.getenv("DB_NAME")
    else:
        raise ValueError(f"Unsupported prefix: {prefix}")

    missing = [k for k, v in {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "dbname": dbname,
    }.items() if not v]

    if missing:
        raise ValueError(f"Missing DB values for {prefix}: {missing}")

    print(f"Connecting {prefix} -> {host}:{port}/{dbname}")

    return psycopg2.connect(
        host=host,
        port=int(port),
        user=user,
        password=password,
        dbname=dbname,
    )


def quote_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", str(name or "")):
        raise ValueError(f"Unsafe identifier: {name}")
    return f'"{name}"'


def table_columns(conn, table_name: str, schema: str = "public") -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position;
            """,
            (schema, table_name),
        )
        return {r[0] for r in cur.fetchall()}


def classify_direction(raw_label: str, normalized_label: str) -> tuple[str, str, str]:
    """
    Returns:
        action_family, direction, match_reason

    direction:
        agent_to_customer
        customer_to_agent
        review
    """
    text = normalize_label(normalized_label or raw_label)

    passive_agent_to_customer = [
        (r"\b" + CUSTOMER_RE + r"\b.*\b(insulted|offended)\b.*\bby\b.*\b" + AGENT_RE + r"\b", "insult_or_offense", "passive_customer_by_agent"),
        (r"\b" + CUSTOMER_RE + r"\b.*\b(rude|rudeness|disrespectful|impolite|aggressive)\b.*\bby\b.*\b" + AGENT_RE + r"\b", "rudeness", "passive_customer_by_agent_rude"),
    ]

    passive_customer_to_agent = [
        (r"\b" + AGENT_RE + r"\b.*\b(insulted|offended)\b.*\bby\b.*\b" + CUSTOMER_RE + r"\b", "insult_or_offense", "passive_agent_by_customer"),
        (r"\b" + AGENT_RE + r"\b.*\b(rude|rudeness|disrespectful|impolite|aggressive)\b.*\bby\b.*\b" + CUSTOMER_RE + r"\b", "rudeness", "passive_agent_by_customer_rude"),
    ]

    for pattern, family, reason in passive_agent_to_customer:
        if re.search(pattern, text):
            return family, "agent_to_customer", reason

    for pattern, family, reason in passive_customer_to_agent:
        if re.search(pattern, text):
            return family, "customer_to_agent", reason

    if re.search(r"\b" + AGENT_RE + r"\b.*\b" + INSULT_RE + r"\b.*\b" + CUSTOMER_RE + r"\b", text):
        return "insult_or_offense", "agent_to_customer", "active_agent_insult_customer"

    if re.search(r"\b" + CUSTOMER_RE + r"\b.*\b" + INSULT_RE + r"\b.*\b" + AGENT_RE + r"\b", text):
        return "insult_or_offense", "customer_to_agent", "active_customer_insult_agent"

    if re.search(r"\b" + AGENT_RE + r"\b.*\b" + SHOUT_RE + r"\b.*\b" + CUSTOMER_RE + r"\b", text):
        return "shouting", "agent_to_customer", "active_agent_shout_customer"

    if re.search(r"\b" + CUSTOMER_RE + r"\b.*\b" + SHOUT_RE + r"\b.*\b" + AGENT_RE + r"\b", text):
        return "shouting", "customer_to_agent", "active_customer_shout_agent"

    if re.search(r"\b" + AGENT_RE + r"\b.*\b" + RUDE_RE + r"\b.*\b" + CUSTOMER_RE + r"\b", text):
        return "rudeness", "agent_to_customer", "active_agent_rude_customer"

    if re.search(r"\b" + CUSTOMER_RE + r"\b.*\b" + RUDE_RE + r"\b.*\b" + AGENT_RE + r"\b", text):
        return "rudeness", "customer_to_agent", "active_customer_rude_agent"

    if re.search(r"\b" + CUSTOMER_RE + r"\b.*\b" + REPORT_RE + r"\b.*\b" + AGENT_RE + r"\b.*\b" + RUDE_RE + r"\b", text):
        return "rudeness_report", "agent_to_customer", "customer_reported_agent_rude"

    if re.search(r"\b" + CUSTOMER_RE + r"\b.*\b" + REPORT_RE + r"\b.*\b" + RUDE_RE + r"\b.*\b" + AGENT_RE + r"\b", text):
        return "rudeness_report", "agent_to_customer", "customer_reported_rude_agent"

    if re.search(r"\b" + AGENT_RE + r"\b.*\b" + REPORT_RE + r"\b.*\b" + CUSTOMER_RE + r"\b.*\b" + RUDE_RE + r"\b", text):
        return "rudeness_report", "customer_to_agent", "agent_reported_customer_rude"

    if re.search(r"\b" + AGENT_RE + r"\b.*\b" + REPORT_RE + r"\b.*\b" + RUDE_RE + r"\b.*\b" + CUSTOMER_RE + r"\b", text):
        return "rudeness_report", "customer_to_agent", "agent_reported_rude_customer"

    return "review", "review", "no_direction_match"


def get_candidate_labels(conn, field_filter: Optional[str]) -> pd.DataFrame:
    field_clause = ""
    params = []

    if field_filter:
        field_clause = "AND m.field_name = %s"
        params.append(field_filter)

    sql = f"""
        SELECT
            m.field_name,
            m.cluster_version,
            m.final_cluster_id AS cluster_id,
            n.display_name AS cluster_display_name,
            c.medoid_label,
            c.cluster_size,
            c.total_occurrences,
            c.cluster_source,
            m.raw_label,
            m.normalized_label,
            m.value_count,
            m.final_cluster_source,
            m.base_cluster_id,
            m.strict_graph_community_id
        FROM taxonomy_label_cluster_map m
        LEFT JOIN taxonomy_clusters c
          ON c.field_name = m.field_name
         AND c.cluster_version = m.cluster_version
         AND c.cluster_id = m.final_cluster_id
        LEFT JOIN taxonomy_cluster_names n
          ON n.field_name = m.field_name
         AND n.cluster_version = m.cluster_version
         AND n.cluster_id = m.final_cluster_id
        WHERE (
                LOWER(m.normalized_label) ~ '(agent|advisor|adviser|broker|staff|rep|representative|customer|client|caller|contact|dm|decision maker|decisionmaker|recipient|prospect)'
            AND LOWER(m.normalized_label) ~ '(insult|insulted|insulting|offend|offended|offensive|shout|shouted|shouting|yell|yelled|yelling|rude|rudeness|impolite|disrespectful|aggressive|politeness|accused|called|reported|complaint|complained|commented|confronted|challenged|unhappy|perceived)'
        )
        {field_clause}
        ORDER BY
            m.field_name,
            m.final_cluster_id,
            m.value_count DESC;
    """

    return pd.read_sql_query(sql, conn, params=params)


def clean_tag_expr(column_name: str = "tag_part") -> str:
    return f"""
        LOWER(
            TRIM(
                BOTH '{{}}" '
                FROM TRIM({column_name})
            )
        )
    """


def get_app_call_counts(conn, affected_labels: pd.DataFrame, app_table: str) -> pd.DataFrame:
    if affected_labels.empty:
        return pd.DataFrame()

    cols = table_columns(conn, app_table)

    field_names = sorted(set(affected_labels["field_name"].dropna().astype(str)))
    usable_fields = [f for f in field_names if f in cols]

    if not usable_fields:
        return pd.DataFrame()

    all_rows = []

    for field_name in usable_fields:
        labels = (
            affected_labels.loc[affected_labels["field_name"] == field_name, "raw_label"]
            .dropna()
            .astype(str)
            .str.lower()
            .drop_duplicates()
            .tolist()
        )

        normalized_labels = sorted(set(normalize_label(x) for x in labels))
        match_values = sorted(set(labels + normalized_labels))

        if not match_values:
            continue

        sql = f"""
            WITH exploded AS (
                SELECT
                    call_id,
                    filename,
                    call_summary,
                    {clean_tag_expr("tag_part")} AS tag
                FROM {quote_ident(app_table)}
                CROSS JOIN LATERAL regexp_split_to_table(
                    COALESCE({quote_ident(field_name)}::text, ''),
                    '\\s*,\\s*'
                ) AS tag_part
            )
            SELECT
                %s AS field_name,
                tag,
                COUNT(DISTINCT call_id) AS distinct_call_count,
                MIN(call_id::text) AS example_call_id,
                MIN(filename::text) AS example_filename,
                MIN(call_summary::text) AS example_summary
            FROM exploded
            WHERE tag = ANY(%s)
            GROUP BY tag
            ORDER BY distinct_call_count DESC, tag;
        """

        df = pd.read_sql_query(sql, conn, params=(field_name, match_values))
        all_rows.append(df)

    if not all_rows:
        return pd.DataFrame()

    return pd.concat(all_rows, ignore_index=True)


def write_report(out_dir: Path, candidates_df: pd.DataFrame, inverse_summary_df: pd.DataFrame, inverse_members_df: pd.DataFrame):
    total_candidate_labels = len(candidates_df)
    total_bad_groups = len(inverse_summary_df)

    base_2246_found = False
    strict_315_found = False

    if not inverse_members_df.empty:
        base_2246_found = bool((inverse_members_df["cluster_id"].astype(str) == "base_2246").any())
        strict_315_found = bool((inverse_members_df["cluster_id"].astype(str) == "strict_315").any())

    lines = []
    lines.append("# Inverse Direction Cluster Scan Report")
    lines.append("")
    lines.append("## Objective")
    lines.append("")
    lines.append("This read-only scan checks whether inverse actor-target labels were grouped together anywhere else in the taxonomy. Examples include `agent_insulted_customer` vs `customer_insulted_agent`, `agent_shouting_at_customer` vs `customer_shouted_at_agent`, and similar rudeness/offense direction pairs.")
    lines.append("")
    lines.append("## Key clarification")
    lines.append("")
    lines.append("The current evidence does not support saying that normalization or comma-value rearranging is the confirmed root cause.")
    lines.append("")
    lines.append("For normal single-value labels, normalization preserves word order and only cleans formatting such as underscores, hyphens, slashes, camel case, casing, and extra spaces.")
    lines.append("")
    lines.append("The confirmed issue is more specific: embedding-based clustering grouped labels that share the same topic words, such as agent, customer, insulted, rude, shouting, and complaint, but did not always preserve who acted on whom.")
    lines.append("")
    lines.append("Therefore, the issue should be described as an actor-target directionality edge case in semantic clustering, not as a proven normalization/reordering bug.")
    lines.append("")
    lines.append("## Scan summary")
    lines.append("")
    lines.append(f"- Candidate directional labels scanned: {total_candidate_labels:,}")
    lines.append(f"- Inverse-direction cluster/action-family combinations found: {total_bad_groups:,}")
    lines.append("")

    if base_2246_found:
        lines.append("The scan confirms the main meeting example in `additional_tags / base_2246`, where labels such as `agent_insulted_customer` and `customer_insulted_agent` are present in the same cluster.")
        lines.append("")

    if strict_315_found:
        lines.append("The scan also confirms a broader related case in `additional_tags / strict_315`, where insult/shouting labels from both directions are present in a strict graph recovered cluster.")
        lines.append("")

    lines.append("## Interpretation")
    lines.append("")
    lines.append("The frontend did not create these labels. The frontend exposed the issue by showing raw cluster members after a semantic search result was selected.")
    lines.append("")
    lines.append("The actual taxonomy issue is that some clusters contain labels from both directions. These should be separated in the next taxonomy iteration, especially after moving to higher-dimensional Voyage embeddings and stronger business-context embedding text.")
    lines.append("")
    lines.append("## Recommended next steps")
    lines.append("")
    lines.append("1. Review `02_inverse_direction_clusters.csv` to identify affected clusters by field.")
    lines.append("2. Review `03_inverse_direction_cluster_members.csv` to inspect the exact labels inside each affected cluster.")
    lines.append("3. Review passive-direction labels carefully, especially labels using `by`, such as `agent_insulted_by_customer` or `customer_offended_by_agent`.")
    lines.append("4. Add this directionality scan as a recurring QA check after each taxonomy rebuild.")
    lines.append("5. Do not immediately patch production clusters before the next embedding/backfill iteration, because cluster IDs may change after Voyage re-embedding.")
    lines.append("6. Use the results to guide the next embedding design: include actor, action, and target structure in the embedding text for direction-sensitive labels.")
    lines.append("")
    lines.append("## Evidence files")
    lines.append("")
    lines.append("- `01_all_candidate_directional_labels.csv`")
    lines.append("- `02_inverse_direction_clusters.csv`")
    lines.append("- `03_inverse_direction_cluster_members.csv`")
    lines.append("- `04_inverse_direction_pairs.csv`")
    lines.append("- `05_field_level_summary.csv`")
    lines.append("- `06_app_source_call_counts.csv` if app count mode was enabled")
    lines.append("")

    path = out_dir / "07_inverse_direction_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--output-dir", default="inverse_direction_scan")
    parser.add_argument("--field-name", default=None, help="Optional field filter, e.g. additional_tags")
    parser.add_argument("--with-app-counts", action="store_true")
    parser.add_argument("--app-table", default="ngp_call_classification")
    args = parser.parse_args()

    load_dotenv(args.env_file)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cluster_conn = pg_conn("CLUSTER_DB")

    print("\nLoading candidate directional labels...")
    candidates_df = get_candidate_labels(cluster_conn, args.field_name)

    if candidates_df.empty:
        print("No candidate labels found.")
        candidates_df.to_csv(out_dir / "01_all_candidate_directional_labels.csv", index=False)
        return

    classified = candidates_df.apply(
        lambda r: classify_direction(r["raw_label"], r["normalized_label"]),
        axis=1,
        result_type="expand",
    )
    classified.columns = ["action_family", "direction", "match_reason"]

    candidates_df = pd.concat([candidates_df, classified], axis=1)

    candidates_path = out_dir / "01_all_candidate_directional_labels.csv"
    candidates_df.to_csv(candidates_path, index=False, encoding="utf-8-sig")
    print(f"Saved {candidates_path} ({len(candidates_df):,} rows)")

    directional_df = candidates_df[candidates_df["direction"].isin(["agent_to_customer", "customer_to_agent"])].copy()

    group_cols = [
        "field_name",
        "cluster_version",
        "cluster_id",
        "cluster_display_name",
        "medoid_label",
        "cluster_size",
        "total_occurrences",
        "final_cluster_source",
        "action_family",
    ]

    summary_rows = []
    member_frames = []
    pair_rows = []

    for key, g in directional_df.groupby(group_cols, dropna=False):
        directions = sorted(g["direction"].dropna().unique())

        if set(directions) != {"agent_to_customer", "customer_to_agent"}:
            continue

        group_dict = dict(zip(group_cols, key))

        agent_side = g[g["direction"] == "agent_to_customer"].copy()
        customer_side = g[g["direction"] == "customer_to_agent"].copy()

        summary_rows.append({
            **group_dict,
            "agent_to_customer_label_rows": int(agent_side["raw_label"].nunique()),
            "agent_to_customer_occurrences": int(agent_side["value_count"].sum()),
            "customer_to_agent_label_rows": int(customer_side["raw_label"].nunique()),
            "customer_to_agent_occurrences": int(customer_side["value_count"].sum()),
            "agent_to_customer_examples": " | ".join(agent_side.sort_values("value_count", ascending=False)["raw_label"].astype(str).tolist()[:20]),
            "customer_to_agent_examples": " | ".join(customer_side.sort_values("value_count", ascending=False)["raw_label"].astype(str).tolist()[:20]),
        })

        member_frames.append(g)

        top_agent = agent_side.sort_values("value_count", ascending=False).head(20)
        top_customer = customer_side.sort_values("value_count", ascending=False).head(20)

        for a in top_agent.itertuples(index=False):
            for c in top_customer.itertuples(index=False):
                pair_rows.append({
                    **group_dict,
                    "agent_to_customer_label": a.raw_label,
                    "agent_to_customer_count": a.value_count,
                    "customer_to_agent_label": c.raw_label,
                    "customer_to_agent_count": c.value_count,
                    "pair_occurrence_weight": int(a.value_count) + int(c.value_count),
                })

    inverse_summary_df = pd.DataFrame(summary_rows)
    inverse_members_df = pd.concat(member_frames, ignore_index=True) if member_frames else pd.DataFrame()
    inverse_pairs_df = pd.DataFrame(pair_rows)

    if not inverse_summary_df.empty:
        inverse_summary_df = inverse_summary_df.sort_values(
            ["field_name", "agent_to_customer_occurrences", "customer_to_agent_occurrences"],
            ascending=[True, False, False],
        )

    if not inverse_members_df.empty:
        inverse_members_df = inverse_members_df.sort_values(
            ["field_name", "cluster_id", "action_family", "direction", "value_count"],
            ascending=[True, True, True, True, False],
        )

    if not inverse_pairs_df.empty:
        inverse_pairs_df = inverse_pairs_df.sort_values(
            ["field_name", "cluster_id", "pair_occurrence_weight"],
            ascending=[True, True, False],
        )

    path = out_dir / "02_inverse_direction_clusters.csv"
    inverse_summary_df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Saved {path} ({len(inverse_summary_df):,} rows)")

    path = out_dir / "03_inverse_direction_cluster_members.csv"
    inverse_members_df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Saved {path} ({len(inverse_members_df):,} rows)")

    path = out_dir / "04_inverse_direction_pairs.csv"
    inverse_pairs_df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Saved {path} ({len(inverse_pairs_df):,} rows)")

    if inverse_summary_df.empty:
        field_summary_df = pd.DataFrame()
    else:
        field_summary_df = (
            inverse_summary_df
            .groupby("field_name", dropna=False)
            .agg(
                affected_cluster_action_groups=("cluster_id", "count"),
                affected_clusters=("cluster_id", "nunique"),
                affected_action_families=("action_family", "nunique"),
                agent_to_customer_occurrences=("agent_to_customer_occurrences", "sum"),
                customer_to_agent_occurrences=("customer_to_agent_occurrences", "sum"),
            )
            .reset_index()
            .sort_values("affected_cluster_action_groups", ascending=False)
        )

    path = out_dir / "05_field_level_summary.csv"
    field_summary_df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Saved {path} ({len(field_summary_df):,} rows)")

    app_counts_df = pd.DataFrame()

    if args.with_app_counts and not inverse_members_df.empty:
        app_conn = pg_conn("APP_DB")
        try:
            app_counts_df = get_app_call_counts(app_conn, inverse_members_df, args.app_table)
        finally:
            app_conn.close()

    path = out_dir / "06_app_source_call_counts.csv"
    app_counts_df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Saved {path} ({len(app_counts_df):,} rows)")

    write_report(
        out_dir=out_dir,
        candidates_df=candidates_df,
        inverse_summary_df=inverse_summary_df,
        inverse_members_df=inverse_members_df,
    )

    cluster_conn.close()

    print("\nDone.")
    print(f"Output folder: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
