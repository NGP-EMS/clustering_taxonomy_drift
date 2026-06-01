#!/usr/bin/env python3
"""
Check all taxonomy fields for role/direction conflicts inside the same cluster.

Purpose:
- Detect cases like agent->customer rudeness and customer->agent rudeness being mixed in one cluster.
- This is an investigation script only. It does not update, rename, merge, or delete anything.

Outputs:
- 00_field_direction_conflicts.csv
- 01_direction_summary.csv
- 02_conflict_cluster_members.csv
- 03_exact_label_hits.csv
- 04_direction_conflict_report.md

Usage examples:
  python check_direction_issues_all_fields.py --out-dir direction_check_all_fields
  python check_direction_issues_all_fields.py --fields additional_tags call_type call_type_sub main_reason main_reason_sub outcome outcome_sub next_step coaching_tags descriptive_keywords
  python check_direction_issues_all_fields.py --input-csv exported_all_taxonomy_members.csv --out-dir direction_check_from_csv

DB connection:
- Uses --database-url, or DATABASE_URL env var, or PGHOST/PGDATABASE/PGUSER/PGPASSWORD/PGPORT env vars.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


AGENT_TERMS = r"(?:agent|advisor|adviser|broker|consultant|representative|rep|caller|salesperson|employee|staff|account manager|sales agent)"
CUSTOMER_TERMS = r"(?:customer|client|prospect|lead|contact|tenant|buyer|seller|landlord|vendor|counterparty)"
ACTION_TERMS = r"(?:rude|rudeness|insult|insulted|insulting|abuse|abused|abusive|shout|shouted|shouting|yell|yelled|yelling|scream|screamed|screaming|aggressive|aggression|hostile|hostility|swear|swore|swearing|profanity|threat|threaten|threatened|disrespect|disrespectful)"
REPORT_TERMS = r"(?:accused|accuse|reported|report|called|call|complained|complaint|claimed|claim|said|says|stated|flagged|mentioned)"

EXACT_LABEL_TERMS = {
    "agent_rude_to_customer",
    "agent_was_rude_to_customer",
    "customer_rude_to_agent",
    "customer_rude_to_agents",
    "customer_was_rude_to_agent",
    "customer_was_rude_to_agents",
    "agent_insulted_customer",
    "agent_insulted_by_customer",
    "customer_insulted_agent",
    "customer_insulted_by_agent",
    "agent_accused_customer_of_rudeness",
    "customer_accused_agent_of_rudeness",
    "agent_called_customer_rude",
    "customer_called_agent_rude",
}

OUTPUT_CONFLICTS = "00_field_direction_conflicts.csv"
OUTPUT_SUMMARY = "01_direction_summary.csv"
OUTPUT_MEMBERS = "02_conflict_cluster_members.csv"
OUTPUT_EXACT = "03_exact_label_hits.csv"
OUTPUT_REPORT = "04_direction_conflict_report.md"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    s = s.replace("\ufeff", "")
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"[_\-/|]+", " ", s)
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def label_key(value: Any) -> str:
    return normalize_text(value).replace(" ", "_")


def has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def issue_family(text: str) -> str:
    if has(r"\b(?:rude|rudeness|disrespect|disrespectful)\b", text):
        return "rudeness"
    if has(r"\b(?:shout|shouted|shouting|yell|yelled|yelling|scream|screamed|screaming)\b", text):
        return "shouting_yelling"
    if has(r"\b(?:insult|insulted|insulting|abuse|abused|abusive|swear|swore|swearing|profanity)\b", text):
        return "insult_abuse_profanity"
    if has(r"\b(?:aggressive|aggression|hostile|hostility|threat|threaten|threatened)\b", text):
        return "aggression_threat"
    return "other"


def classify_direction(raw_label: Any, normalized_label: Any = None) -> Tuple[str, str, str, str, str]:
    """
    Returns: analyzed_text, family, direction, confidence, matched_rule

    direction values:
    - agent_to_customer: label says/strongly implies agent behavior against customer/client.
    - customer_to_agent: label says/strongly implies customer/client behavior against agent/advisor.
    - review: direction cannot be safely inferred.
    """
    raw_text = normalize_text(raw_label)
    norm_text = normalize_text(normalized_label)
    text = raw_text or norm_text
    if raw_text and norm_text and raw_text != norm_text:
        text = f"{raw_text} {norm_text}"

    family = issue_family(text)
    if family == "other":
        return text, family, "review", "none", "no_directional_issue_terms"

    # Report/accusation wording. These rules intentionally handle the known problem pattern:
    # customer_accused_agent_of_rudeness vs agent_accused_customer_of_rudeness.
    if has(rf"\b{CUSTOMER_TERMS}\b.*\b{REPORT_TERMS}\b.*\b{AGENT_TERMS}\b.*\b{ACTION_TERMS}\b", text):
        return text, family, "agent_to_customer", "high", "customer_reported_agent_issue"
    if has(rf"\b{AGENT_TERMS}\b.*\b{REPORT_TERMS}\b.*\b{CUSTOMER_TERMS}\b.*\b{ACTION_TERMS}\b", text):
        return text, family, "customer_to_agent", "high", "agent_reported_customer_issue"

    # Direct active wording.
    if has(rf"\b{AGENT_TERMS}\b.*\b{ACTION_TERMS}\b.*\b{CUSTOMER_TERMS}\b", text):
        return text, family, "agent_to_customer", "high", "agent_action_customer_target"
    if has(rf"\b{CUSTOMER_TERMS}\b.*\b{ACTION_TERMS}\b.*\b{AGENT_TERMS}\b", text):
        return text, family, "customer_to_agent", "high", "customer_action_agent_target"

    # Passive wording.
    if has(rf"\b{CUSTOMER_TERMS}\b.*\b{ACTION_TERMS}\b.*\bby\b.*\b{AGENT_TERMS}\b", text):
        return text, family, "agent_to_customer", "high", "customer_action_by_agent"
    if has(rf"\b{AGENT_TERMS}\b.*\b{ACTION_TERMS}\b.*\bby\b.*\b{CUSTOMER_TERMS}\b", text):
        return text, family, "customer_to_agent", "high", "agent_action_by_customer"

    # Compact labels like rude_agent, abusive_customer, agent_rude, customer_rude.
    if has(rf"\b(?:rude|abusive|aggressive|hostile|insulting|disrespectful)\b\s+\b{AGENT_TERMS}\b", text):
        return text, family, "agent_to_customer", "medium", "role_adjective_agent"
    if has(rf"\b{AGENT_TERMS}\b\s+\b(?:rude|abusive|aggressive|hostile|insulting|disrespectful)\b", text):
        return text, family, "agent_to_customer", "medium", "agent_role_adjective"
    if has(rf"\b(?:rude|abusive|aggressive|hostile|insulting|disrespectful)\b\s+\b{CUSTOMER_TERMS}\b", text):
        return text, family, "customer_to_agent", "medium", "role_adjective_customer"
    if has(rf"\b{CUSTOMER_TERMS}\b\s+\b(?:rude|abusive|aggressive|hostile|insulting|disrespectful)\b", text):
        return text, family, "customer_to_agent", "medium", "customer_role_adjective"

    return text, family, "review", "none", "direction_unclear"


def load_dotenv_if_present(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def connect_db(database_url: Optional[str]):
    try:
        import psycopg2  # type: ignore
        driver = "psycopg2"
    except Exception:
        try:
            import psycopg  # type: ignore
            driver = "psycopg"
        except Exception as exc:
            raise RuntimeError(
                "Install a PostgreSQL driver first: pip install psycopg2-binary"
            ) from exc

    dsn = database_url or os.getenv("DATABASE_URL")
    if driver == "psycopg2":
        import psycopg2  # type: ignore
        if dsn:
            return psycopg2.connect(dsn)
        return psycopg2.connect(
            host=os.getenv("PGHOST", "localhost"),
            port=os.getenv("PGPORT", "5432"),
            dbname=os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB"),
            user=os.getenv("PGUSER") or os.getenv("POSTGRES_USER"),
            password=os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD"),
        )

    import psycopg  # type: ignore
    if dsn:
        return psycopg.connect(dsn)
    return psycopg.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB"),
        user=os.getenv("PGUSER") or os.getenv("POSTGRES_USER"),
        password=os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD"),
    )


def fetch_dicts(conn, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def table_exists(conn, table_name: str) -> bool:
    rows = fetch_dicts(
        conn,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = ANY (current_schemas(false))
              AND table_name = %s
        ) AS exists
        """,
        [table_name],
    )
    return bool(rows and rows[0]["exists"])


def table_columns(conn, table_name: str) -> set:
    rows = fetch_dicts(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = ANY (current_schemas(false))
          AND table_name = %s
        """,
        [table_name],
    )
    return {r["column_name"] for r in rows}


def sql_literal_null(alias: str) -> str:
    return f"NULL::text AS {alias}"


def build_member_query(conn, fields: Optional[List[str]], include_anomalies: bool) -> Tuple[str, List[Any]]:
    if not table_exists(conn, "taxonomy_label_cluster_map"):
        raise RuntimeError("Missing required table: taxonomy_label_cluster_map")
    if not table_exists(conn, "taxonomy_clusters"):
        raise RuntimeError("Missing required table: taxonomy_clusters")

    lm_cols = table_columns(conn, "taxonomy_label_cluster_map")
    c_cols = table_columns(conn, "taxonomy_clusters")
    has_names = table_exists(conn, "taxonomy_cluster_names")
    n_cols = table_columns(conn, "taxonomy_cluster_names") if has_names else set()

    required_lm = {"field_name", "cluster_version", "final_cluster_id", "raw_label", "normalized_label"}
    required_c = {"field_name", "cluster_version", "cluster_id"}
    missing_lm = required_lm - lm_cols
    missing_c = required_c - c_cols
    if missing_lm:
        raise RuntimeError(f"taxonomy_label_cluster_map missing required columns: {sorted(missing_lm)}")
    if missing_c:
        raise RuntimeError(f"taxonomy_clusters missing required columns: {sorted(missing_c)}")

    value_count_expr = "COALESCE(lm.value_count, 1) AS value_count" if "value_count" in lm_cols else "1 AS value_count"
    final_cluster_source_expr = "lm.final_cluster_source" if "final_cluster_source" in lm_cols else sql_literal_null("final_cluster_source")
    base_cluster_id_expr = "lm.base_cluster_id" if "base_cluster_id" in lm_cols else sql_literal_null("base_cluster_id")
    strict_graph_expr = "lm.strict_graph_community_id" if "strict_graph_community_id" in lm_cols else sql_literal_null("strict_graph_community_id")

    medoid_expr = "c.medoid_label" if "medoid_label" in c_cols else sql_literal_null("medoid_label")
    cluster_size_expr = "c.cluster_size" if "cluster_size" in c_cols else "NULL::integer AS cluster_size"
    total_occ_expr = "c.total_occurrences" if "total_occurrences" in c_cols else "NULL::integer AS total_occurrences"

    names_join = ""
    display_expr = "c.cluster_id::text AS cluster_display_name"
    if has_names and "display_name" in n_cols:
        name_filters = ["n.field_name = c.field_name", "n.cluster_id::text = c.cluster_id::text"]
        if "cluster_version" in n_cols:
            name_filters.append("n.cluster_version::text = c.cluster_version::text")
        if "active" in n_cols:
            name_filters.append("COALESCE(n.active, true) = true")
        order_cols = []
        if "updated_at" in n_cols:
            order_cols.append("n.updated_at DESC NULLS LAST")
        if "created_at" in n_cols:
            order_cols.append("n.created_at DESC NULLS LAST")
        order_sql = ", ".join(order_cols) if order_cols else "n.display_name ASC"
        names_join = f"""
        LEFT JOIN LATERAL (
            SELECT n.display_name
            FROM taxonomy_cluster_names n
            WHERE {' AND '.join(name_filters)}
            ORDER BY {order_sql}
            LIMIT 1
        ) n ON TRUE
        """
        display_expr = "COALESCE(n.display_name, c.cluster_id::text) AS cluster_display_name"

    where_parts = [
        "lm.final_cluster_id IS NOT NULL",
        "lm.final_cluster_id::text = c.cluster_id::text",
        "lm.field_name = c.field_name",
        "lm.cluster_version::text = c.cluster_version::text",
    ]
    params: List[Any] = []

    if "active" in c_cols:
        where_parts.append("COALESCE(c.active, true) = true")
    if not include_anomalies and "is_true_anomaly_cluster" in c_cols:
        where_parts.append("COALESCE(c.is_true_anomaly_cluster, false) = false")
    if fields:
        placeholders = ", ".join(["%s"] * len(fields))
        where_parts.append(f"lm.field_name IN ({placeholders})")
        params.extend(fields)

    sql = f"""
    SELECT
        lm.field_name,
        lm.cluster_version,
        lm.final_cluster_id::text AS cluster_id,
        {display_expr},
        {medoid_expr},
        {cluster_size_expr},
        {total_occ_expr},
        lm.raw_label,
        lm.normalized_label,
        {value_count_expr},
        {final_cluster_source_expr},
        {base_cluster_id_expr},
        {strict_graph_expr}
    FROM taxonomy_label_cluster_map lm
    JOIN taxonomy_clusters c
      ON lm.field_name = c.field_name
     AND lm.cluster_version::text = c.cluster_version::text
     AND lm.final_cluster_id::text = c.cluster_id::text
    {names_join}
    WHERE {' AND '.join(where_parts)}
    ORDER BY lm.field_name, lm.cluster_version, lm.final_cluster_id::text, COALESCE(lm.value_count, 1) DESC NULLS LAST, lm.raw_label
    """
    return sql, params


def load_rows_from_db(args) -> List[Dict[str, Any]]:
    load_dotenv_if_present(args.env_file)
    conn = connect_db(args.database_url)
    try:
        sql, params = build_member_query(conn, args.fields, args.include_anomalies)
        return fetch_dicts(conn, sql, params)
    finally:
        conn.close()


def load_rows_from_csv(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize common column aliases from previous investigation exports.
            if "cluster_display_name" not in row and "display_name" in row:
                row["cluster_display_name"] = row.get("display_name", "")
            if "cluster_id" not in row and "final_cluster_id" in row:
                row["cluster_id"] = row.get("final_cluster_id", "")
            if "value_count" not in row:
                row["value_count"] = "1"
            rows.append(row)
    return rows


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def csv_examples(values: Iterable[str], limit: int = 8) -> str:
    seen = []
    seen_set = set()
    for value in values:
        if not value:
            continue
        if value not in seen_set:
            seen.append(value)
            seen_set.add(value)
        if len(seen) >= limit:
            break
    return " | ".join(seen)


def analyze_rows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    annotated: List[Dict[str, Any]] = []
    exact_hits: List[Dict[str, Any]] = []

    for row in rows:
        analyzed_text, family, direction, confidence, rule = classify_direction(
            row.get("raw_label"), row.get("normalized_label")
        )
        out = dict(row)
        out["analyzed_text"] = analyzed_text
        out["issue_family"] = family
        out["direction"] = direction
        out["direction_confidence"] = confidence
        out["matched_rule"] = rule
        out["value_count_int"] = safe_int(row.get("value_count"), 1)
        annotated.append(out)

        raw_key = label_key(row.get("raw_label"))
        norm_key = label_key(row.get("normalized_label"))
        if raw_key in EXACT_LABEL_TERMS or norm_key in EXACT_LABEL_TERMS:
            hit = dict(out)
            hit["matched_exact_term"] = raw_key if raw_key in EXACT_LABEL_TERMS else norm_key
            exact_hits.append(hit)

    summary_map: Dict[Tuple[str, str, str, str, str, str, str], Dict[str, Any]] = {}
    examples_map: Dict[Tuple[str, str, str, str, str, str, str], List[str]] = defaultdict(list)

    for row in annotated:
        if row["direction"] == "review" or row["issue_family"] == "other":
            continue
        key = (
            str(row.get("field_name", "")),
            str(row.get("cluster_version", "")),
            str(row.get("cluster_id", "")),
            str(row.get("cluster_display_name", "")),
            str(row.get("medoid_label", "")),
            row["issue_family"],
            row["direction"],
        )
        if key not in summary_map:
            summary_map[key] = {
                "field_name": key[0],
                "cluster_version": key[1],
                "cluster_id": key[2],
                "cluster_display_name": key[3],
                "medoid_label": key[4],
                "issue_family": key[5],
                "direction": key[6],
                "label_rows": 0,
                "total_occurrences": 0,
                "high_confidence_rows": 0,
                "medium_confidence_rows": 0,
            }
        summary_map[key]["label_rows"] += 1
        summary_map[key]["total_occurrences"] += row["value_count_int"]
        if row["direction_confidence"] == "high":
            summary_map[key]["high_confidence_rows"] += 1
        if row["direction_confidence"] == "medium":
            summary_map[key]["medium_confidence_rows"] += 1
        examples_map[key].append(str(row.get("raw_label") or row.get("normalized_label") or ""))

    summary_rows = list(summary_map.values())
    for key, summary in summary_map.items():
        summary["example_labels"] = csv_examples(examples_map[key], 10)

    by_cluster_family: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in summary_rows:
        by_cluster_family[(row["field_name"], row["cluster_version"], row["cluster_id"], row["issue_family"])].append(row)

    conflict_rows: List[Dict[str, Any]] = []
    conflict_keys = set()
    for (field, version, cluster_id, family), group in by_cluster_family.items():
        dirs = {r["direction"] for r in group}
        if not {"agent_to_customer", "customer_to_agent"}.issubset(dirs):
            continue
        agent_side = next(r for r in group if r["direction"] == "agent_to_customer")
        customer_side = next(r for r in group if r["direction"] == "customer_to_agent")
        conflict_keys.add((field, version, cluster_id, family))
        conflict_rows.append({
            "field_name": field,
            "cluster_version": version,
            "cluster_id": cluster_id,
            "cluster_display_name": agent_side.get("cluster_display_name") or customer_side.get("cluster_display_name"),
            "medoid_label": agent_side.get("medoid_label") or customer_side.get("medoid_label"),
            "issue_family": family,
            "agent_to_customer_label_rows": agent_side["label_rows"],
            "agent_to_customer_occurrences": agent_side["total_occurrences"],
            "agent_to_customer_examples": agent_side["example_labels"],
            "customer_to_agent_label_rows": customer_side["label_rows"],
            "customer_to_agent_occurrences": customer_side["total_occurrences"],
            "customer_to_agent_examples": customer_side["example_labels"],
            "min_opposing_occurrences": min(agent_side["total_occurrences"], customer_side["total_occurrences"]),
            "total_directional_occurrences": agent_side["total_occurrences"] + customer_side["total_occurrences"],
            "suggested_action": "REVIEW_FOR_SPLIT_OR_RENAME",
        })

    conflict_rows.sort(
        key=lambda r: (
            r["field_name"],
            -safe_int(r["min_opposing_occurrences"]),
            -safe_int(r["total_directional_occurrences"]),
            r["cluster_id"],
        )
    )

    conflict_member_rows: List[Dict[str, Any]] = []
    flagged_cluster_keys = {(r["field_name"], r["cluster_version"], r["cluster_id"], r["issue_family"]) for r in conflict_rows}
    for row in annotated:
        key = (
            str(row.get("field_name", "")),
            str(row.get("cluster_version", "")),
            str(row.get("cluster_id", "")),
            row.get("issue_family", ""),
        )
        if key in flagged_cluster_keys:
            conflict_member_rows.append(row)

    summary_rows.sort(
        key=lambda r: (
            r["field_name"],
            r["cluster_version"],
            r["cluster_id"],
            r["issue_family"],
            r["direction"],
        )
    )

    return conflict_rows, summary_rows, conflict_member_rows, exact_hits


def write_csv(path: Path, rows: List[Dict[str, Any]], preferred_columns: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        columns = preferred_columns or ["status"]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
        return

    columns: List[str] = []
    if preferred_columns:
        columns.extend([c for c in preferred_columns if c not in columns])
    for row in rows:
        for key in row.keys():
            if key not in columns and key != "value_count_int":
                columns.append(key)

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, rows_scanned: int, conflict_rows: List[Dict[str, Any]], exact_hits: List[Dict[str, Any]]) -> None:
    fields = sorted({r["field_name"] for r in conflict_rows})
    exact_fields = sorted({str(r.get("field_name", "")) for r in exact_hits})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# Direction Conflict Check - All Fields",
        "",
        f"Generated: {now}",
        "",
        "## Scope",
        "",
        f"Total taxonomy label rows scanned: {rows_scanned:,}",
        "",
        "This script checks whether opposite role/direction labels appear inside the same taxonomy cluster.",
        "It is read-only and does not update taxonomy tables.",
        "",
        "## Important interpretation",
        "",
        "A flagged row is not evidence that normalization rearranged words. The check is designed to find semantic/member placement issues where labels with opposite speaker roles were grouped together.",
        "",
        "## Result summary",
        "",
        f"Flagged cluster/family conflicts: {len(conflict_rows):,}",
        f"Fields with flagged conflicts: {', '.join(fields) if fields else 'None'}",
        f"Exact label hits found across scanned fields: {len(exact_hits):,}",
        f"Fields with exact label hits: {', '.join(exact_fields) if exact_fields else 'None'}",
        "",
        "## Top flagged clusters",
        "",
    ]

    if not conflict_rows:
        lines.append("No opposite-direction cluster conflicts were detected by the current rule set.")
    else:
        lines.append("| field_name | cluster_id | display_name | family | agent_to_customer_occ | customer_to_agent_occ | action |")
        lines.append("|---|---|---|---|---:|---:|---|")
        for row in conflict_rows[:50]:
            display = str(row.get("cluster_display_name", "")).replace("|", "-")
            lines.append(
                f"| {row.get('field_name')} | {row.get('cluster_id')} | {display} | {row.get('issue_family')} | "
                f"{row.get('agent_to_customer_occurrences')} | {row.get('customer_to_agent_occurrences')} | {row.get('suggested_action')} |"
            )

    lines.extend([
        "",
        "## Output files",
        "",
        f"- `{OUTPUT_CONFLICTS}`: one row per flagged cluster/family conflict.",
        f"- `{OUTPUT_SUMMARY}`: grouped direction counts for all directional labels found.",
        f"- `{OUTPUT_MEMBERS}`: member labels for the flagged clusters only.",
        f"- `{OUTPUT_EXACT}`: exact hits for known labels such as `customer_rude_to_agent` and `agent_accused_customer_of_rudeness`.",
        "",
        "## Recommended next step",
        "",
        "Open `00_field_direction_conflicts.csv` first. If only `additional_tags / base_1657` appears, the issue is localized. If other fields appear, review their rows in `02_conflict_cluster_members.csv` before deciding whether to split, rename, or leave them as-is.",
        "",
    ])

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check all taxonomy fields for role/direction conflicts.")
    parser.add_argument("--database-url", default=None, help="PostgreSQL DATABASE_URL. Defaults to env DATABASE_URL or PG* env vars.")
    parser.add_argument("--env-file", default=".env", help="Optional .env file to load before connecting.")
    parser.add_argument("--input-csv", default=None, help="Analyze an already-exported taxonomy member CSV instead of querying DB.")
    parser.add_argument("--out-dir", default="direction_check_all_fields", help="Output directory.")
    parser.add_argument("--fields", nargs="*", default=None, help="Optional field_name filter. Default scans all fields.")
    parser.add_argument("--include-anomalies", action="store_true", help="Include true anomaly clusters. Default scans standard clusters only if the column exists.")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.input_csv:
        rows = load_rows_from_csv(args.input_csv)
    else:
        rows = load_rows_from_db(args)

    conflict_rows, summary_rows, conflict_member_rows, exact_hits = analyze_rows(rows)

    write_csv(
        out_dir / OUTPUT_CONFLICTS,
        conflict_rows,
        [
            "field_name", "cluster_version", "cluster_id", "cluster_display_name", "medoid_label", "issue_family",
            "agent_to_customer_label_rows", "agent_to_customer_occurrences", "agent_to_customer_examples",
            "customer_to_agent_label_rows", "customer_to_agent_occurrences", "customer_to_agent_examples",
            "min_opposing_occurrences", "total_directional_occurrences", "suggested_action",
        ],
    )
    write_csv(
        out_dir / OUTPUT_SUMMARY,
        summary_rows,
        [
            "field_name", "cluster_version", "cluster_id", "cluster_display_name", "medoid_label", "issue_family",
            "direction", "label_rows", "total_occurrences", "high_confidence_rows", "medium_confidence_rows", "example_labels",
        ],
    )
    write_csv(
        out_dir / OUTPUT_MEMBERS,
        conflict_member_rows,
        [
            "field_name", "cluster_version", "cluster_id", "cluster_display_name", "medoid_label", "cluster_size", "total_occurrences",
            "raw_label", "normalized_label", "value_count", "final_cluster_source", "base_cluster_id", "strict_graph_community_id",
            "issue_family", "direction", "direction_confidence", "matched_rule", "analyzed_text",
        ],
    )
    write_csv(
        out_dir / OUTPUT_EXACT,
        exact_hits,
        [
            "field_name", "cluster_version", "cluster_id", "cluster_display_name", "medoid_label", "raw_label", "normalized_label",
            "value_count", "matched_exact_term", "issue_family", "direction", "direction_confidence", "matched_rule",
        ],
    )
    write_report(out_dir / OUTPUT_REPORT, len(rows), conflict_rows, exact_hits)

    print(f"Rows scanned: {len(rows):,}")
    print(f"Flagged cluster/family conflicts: {len(conflict_rows):,}")
    print(f"Exact label hits: {len(exact_hits):,}")
    print(f"Output directory: {out_dir.resolve()}")
    print(f"Open first: {out_dir / OUTPUT_CONFLICTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
