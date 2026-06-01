# scripts/audit_actor_target_directionality_all_fields.py

import argparse
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv
load_dotenv()
AGENT_SIDE_TERMS = {
    "agent",
    "broker",
    "advisor",
    "consultant",
    "representative",
    "rep",
    "salesperson",
    "staff",
}

CUSTOMER_SIDE_TERMS = {
    "customer",
    "client",
    "caller",
    "contact",
    "lead",
    "prospect",
    "buyer",
    "seller",
    "tenant",
    "landlord",
    "dm",
    "decision maker",
    "decisionmaker",
    "inbound caller",
}

DIRECT_ACTIONS = {
    "rude": "rudeness",
    "rudeness": "rudeness",
    "insult": "insult",
    "insulted": "insult",
    "insulting": "insult",
    "abuse": "abuse",
    "abused": "abuse",
    "abusive": "abuse",
    "shout": "shouting",
    "shouted": "shouting",
    "shouting": "shouting",
    "yell": "shouting",
    "yelled": "shouting",
    "yelling": "shouting",
    "aggressive": "aggression",
    "disrespect": "disrespect",
    "disrespectful": "disrespect",
    "threat": "threat",
    "threatened": "threat",
    "threatening": "threat",
    "cut off": "cutoff",
    "cutting off": "cutoff",
    "cutoff": "cutoff",
    "hang up": "hangup",
    "hung up": "hangup",
    "disconnect": "disconnect",
    "disconnected": "disconnect",
}

REPORTING_ACTIONS = {
    "accused": "reported_rudeness",
    "accuse": "reported_rudeness",
    "complaint": "reported_rudeness",
    "complained": "reported_rudeness",
    "reported": "reported_rudeness",
    "called": "reported_rudeness",
    "perceived": "reported_rudeness",
    "commented": "reported_rudeness",
    "confronted": "reported_rudeness",
    "challenged": "reported_rudeness",
    "unhappy": "reported_rudeness",
    "cited": "reported_rudeness",
}

REVIEW_KEYWORDS = {
    "rude",
    "rudeness",
    "insult",
    "insulted",
    "abuse",
    "abused",
    "abusive",
    "shout",
    "shouted",
    "yell",
    "yelled",
    "aggressive",
    "disrespect",
    "threat",
    "accused",
    "complaint",
    "complained",
    "reported",
    "called",
    "perceived",
    "confronted",
    "challenged",
    "cut off",
    "cutoff",
    "hang up",
    "hung up",
    "disconnect",
    "post hangup",
    "post call",
    "termination",
}

ROLE_ALIASES = {}
for x in AGENT_SIDE_TERMS:
    ROLE_ALIASES[x] = "agent_side"
for x in CUSTOMER_SIDE_TERMS:
    ROLE_ALIASES[x] = "customer_side"

ROLE_TERMS_SORTED = sorted(ROLE_ALIASES.keys(), key=len, reverse=True)
DIRECT_ACTIONS_SORTED = sorted(DIRECT_ACTIONS.keys(), key=len, reverse=True)
REPORTING_ACTIONS_SORTED = sorted(REPORTING_ACTIONS.keys(), key=len, reverse=True)


def get_conn():
    return psycopg2.connect(
        host=(
            os.getenv("LOCAL_PG_HOST")
            or os.getenv("PGHOST")
            or os.getenv("DB_HOST")
            or "localhost"
        ),
        port=(
            os.getenv("LOCAL_PG_PORT")
            or os.getenv("PGPORT")
            or os.getenv("DB_PORT")
            or "5432"
        ),
        dbname=(
            os.getenv("LOCAL_PG_DB")
            or os.getenv("PGDATABASE")
            or os.getenv("DB_NAME")
        ),
        user=(
            os.getenv("LOCAL_PG_USER")
            or os.getenv("PGUSER")
            or os.getenv("DB_USER")
        ),
        password=(
            os.getenv("LOCAL_PG_PASSWORD")
            or os.getenv("PGPASSWORD")
            or os.getenv("DB_PASSWORD")
        ),
    )

def table_columns(conn, table_name):
    sql = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table_name,))
        return {r[0] for r in cur.fetchall()}


def normalize_text(value):
    if value is None:
        return ""
    value = str(value)
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = value.lower()
    value = value.replace("_", " ").replace("-", " ").replace("/", " ")
    value = re.sub(r"[^a-z0-9\s]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def role_side(term):
    return ROLE_ALIASES.get(normalize_text(term))


def role_pattern():
    return r"(" + "|".join(re.escape(x) for x in ROLE_TERMS_SORTED) + r")"


def direct_action_pattern():
    return r"(" + "|".join(re.escape(x) for x in DIRECT_ACTIONS_SORTED) + r")"


def reporting_action_pattern():
    return r"(" + "|".join(re.escape(x) for x in REPORTING_ACTIONS_SORTED) + r")"


def has_review_keyword(text):
    t = normalize_text(text)
    return any(k in t for k in REVIEW_KEYWORDS)


def canonical_direct_action(action):
    return DIRECT_ACTIONS.get(normalize_text(action))


def canonical_reporting_action(action):
    return REPORTING_ACTIONS.get(normalize_text(action))

def dedupe_signals(signals):
    unique = []
    seen = set()

    for s in signals:
        key = (
            s["issue_family"],
            s["responsible_side"],
            s["affected_or_target_side"],
            s["pattern_type"],
            s["evidence"],
        )

        if key not in seen:
            seen.add(key)
            unique.append(s)

    return unique
def extract_direction_signals(raw_label, normalized_label, display_name="", medoid_label=""):
    """
    Stricter v2 detector.

    Returns direction signals:
      issue_family
      responsible_side
      affected_or_target_side
      pattern_type
      evidence

    Meaning:
      responsible_side = who caused / performed / was accused of the issue
      affected_or_target_side = who received / reported / was affected by the issue
    """
    text = normalize_text(f"{raw_label} {normalized_label}")

    if not text:
        return []

    signals = []

    rp = role_pattern()
    ap = direct_action_pattern()
    rep = reporting_action_pattern()

    # ------------------------------------------------------------
    # 1. Reporting / accusation patterns first.
    # customer accused agent of rudeness => responsible agent_side
    # agent accused customer of rudeness => responsible customer_side
    # ------------------------------------------------------------
    reporting_patterns = [
        rf"\b{rp}\b\s+\b{rep}\b\s+\b{rp}\b\s+(?:of\s+)?(?:being\s+)?(?:rude|rudeness|abusive|abuse|insulting|insult|aggressive|disrespectful|disrespect)",
        rf"\b{rp}\b.*?\b{rep}\b.*?\b{rp}\b.*?\b(?:rude|rudeness|abusive|abuse|insulting|insult|aggressive|disrespectful|disrespect)\b",
    ]

    reporting_found = False

    for pat in reporting_patterns:
        for m in re.finditer(pat, text):
            reporter_raw = m.group(1)
            report_action_raw = m.group(2)
            accused_raw = m.group(3)

            reporter = role_side(reporter_raw)
            accused = role_side(accused_raw)
            issue = canonical_reporting_action(report_action_raw)

            if reporter and accused and issue and reporter != accused:
                reporting_found = True
                signals.append({
                    "issue_family": issue,
                    "responsible_side": accused,
                    "affected_or_target_side": reporter,
                    "pattern_type": "reporting_or_accusation",
                    "evidence": m.group(0),
                })

    # Strong complaint/perception shorthand.
    # customer complaint agent rudeness => responsible agent_side
    if re.search(r"\bcustomer\b.*?\b(complaint|complained|perceived|unhappy|called|accused|referenced)\b.*?\bagent\b.*?\b(rude|rudeness|abuse|abusive|insult)", text):
        reporting_found = True
        signals.append({
            "issue_family": "reported_rudeness",
            "responsible_side": "agent_side",
            "affected_or_target_side": "customer_side",
            "pattern_type": "reporting_or_accusation_inference",
            "evidence": "customer reported/accused agent rudeness",
        })

    # agent complaint/commented/called customer rude => responsible customer_side
    if re.search(r"\bagent\b.*?\b(accused|called|commented|confronted|challenged|complained|reported)\b.*?\b(customer|client|caller|contact|lead|prospect|dm|decision maker|decisionmaker)\b.*?\b(rude|rudeness|abuse|abusive|insult)", text):
        reporting_found = True
        signals.append({
            "issue_family": "reported_rudeness",
            "responsible_side": "customer_side",
            "affected_or_target_side": "agent_side",
            "pattern_type": "reporting_or_accusation_inference",
            "evidence": "agent reported/accused customer rudeness",
        })

    # If this is clearly a reporting/accusation phrase, do not also classify it
    # as direct behavior. This avoids double-counting labels like:
    # customer_accused_agent_of_rudeness
    # agent_called_customer_rude
    if reporting_found:
        return dedupe_signals(signals)

    # ------------------------------------------------------------
    # 2. Passive behavior patterns before direct patterns.
    # agent insulted by customer => responsible customer_side
    # customer insulted by agent => responsible agent_side
    # ------------------------------------------------------------
    passive_patterns = [
        rf"\b{rp}\b\s+(?:was\s+|is\s+|being\s+)?\b{ap}\b\s+by\s+\b{rp}\b",
        rf"\b{rp}\b.*?\b{ap}\b.*?\bby\b.*?\b{rp}\b",
    ]

    passive_found = False

    for pat in passive_patterns:
        for m in re.finditer(pat, text):
            target_raw = m.group(1)
            action_raw = m.group(2)
            actor_raw = m.group(3)

            target = role_side(target_raw)
            actor = role_side(actor_raw)
            issue = canonical_direct_action(action_raw)

            if actor and target and issue and actor != target:
                passive_found = True
                signals.append({
                    "issue_family": issue,
                    "responsible_side": actor,
                    "affected_or_target_side": target,
                    "pattern_type": "passive_behavior",
                    "evidence": m.group(0),
                })

    # If passive was found, do not also treat the same phrase as direct.
    if passive_found:
        return dedupe_signals(signals)

    # ------------------------------------------------------------
    # 3. Direct behavior.
    # agent rude to customer => responsible agent_side
    # customer insulted agent => responsible customer_side
    # ------------------------------------------------------------
    direct_patterns = [
        rf"\b{rp}\b\s+(?:was\s+|is\s+|being\s+)?\b{ap}\b\s+(?:to|at|towards|with|on)?\s*\b{rp}\b",
        rf"\b{rp}\b.*?\b{ap}\b.*?\b{rp}\b",
    ]

    for pat in direct_patterns:
        for m in re.finditer(pat, text):
            actor_raw = m.group(1)
            action_raw = m.group(2)
            target_raw = m.group(3)

            actor = role_side(actor_raw)
            target = role_side(target_raw)
            issue = canonical_direct_action(action_raw)

            if actor and target and issue and actor != target:
                signals.append({
                    "issue_family": issue,
                    "responsible_side": actor,
                    "affected_or_target_side": target,
                    "pattern_type": "direct_behavior",
                    "evidence": m.group(0),
                })

    return dedupe_signals(signals)

def build_query(conn, field=None, include_anomalies=False):
    c_cols = table_columns(conn, "taxonomy_clusters")
    n_cols = table_columns(conn, "taxonomy_cluster_names")
    m_cols = table_columns(conn, "taxonomy_label_cluster_map")

    cluster_display_expr = "''"
    if "display_name" in c_cols:
        cluster_display_expr = "c.display_name"

    name_display_expr = "NULL"
    names_cte = ""
    names_join = ""

    if "display_name" in n_cols:
        order_cols = []
        if "updated_at" in n_cols:
            order_cols.append("updated_at DESC NULLS LAST")
        if "created_at" in n_cols:
            order_cols.append("created_at DESC NULLS LAST")
        order_sql = ", ".join(order_cols) if order_cols else "display_name"

        names_cte = f"""
            latest_names AS (
                SELECT DISTINCT ON (field_name, cluster_version, cluster_id)
                    field_name,
                    cluster_version,
                    cluster_id,
                    display_name
                FROM taxonomy_cluster_names
                ORDER BY field_name, cluster_version, cluster_id, {order_sql}
            ),
        """
        names_join = """
            LEFT JOIN latest_names n
              ON n.field_name = c.field_name
             AND n.cluster_version = c.cluster_version
             AND n.cluster_id = c.cluster_id
        """
        name_display_expr = "n.display_name"

    active_filter = ""
    if "active" in c_cols:
        active_filter = "AND COALESCE(c.active, true) = true"

    anomaly_filter = ""
    if not include_anomalies and "is_true_anomaly_cluster" in c_cols:
        anomaly_filter = "AND COALESCE(c.is_true_anomaly_cluster, false) = false"

    final_cluster_source_expr = "NULL AS final_cluster_source"
    if "final_cluster_source" in m_cols:
        final_cluster_source_expr = "m.final_cluster_source"

    final_is_true_anomaly_expr = "NULL AS final_is_true_anomaly"
    if "final_is_true_anomaly" in m_cols:
        final_is_true_anomaly_expr = "m.final_is_true_anomaly"

    rep_labels_expr = "NULL AS representative_labels"
    if "representative_labels" in c_cols:
        rep_labels_expr = "c.representative_labels"

    cluster_size_expr = "NULL AS cluster_size"
    if "cluster_size" in c_cols:
        cluster_size_expr = "c.cluster_size"

    total_occurrences_expr = "NULL AS total_occurrences"
    if "total_occurrences" in c_cols:
        total_occurrences_expr = "c.total_occurrences"

    medoid_expr = "NULL AS medoid_label"
    if "medoid_label" in c_cols:
        medoid_expr = "c.medoid_label"

    value_count_expr = "1 AS value_count"
    if "value_count" in m_cols:
        value_count_expr = "COALESCE(m.value_count, 1) AS value_count"

    params = []
    field_filter = ""
    if field:
        field_filter = "AND m.field_name = %s"
        params.append(field)

    sql = f"""
        WITH
        {names_cte}
        base AS (
            SELECT
                m.field_name,
                m.cluster_version,
                m.final_cluster_id AS cluster_id,
                COALESCE({name_display_expr}, {cluster_display_expr}, '') AS display_name,
                {medoid_expr},
                {cluster_size_expr},
                {total_occurrences_expr},
                {rep_labels_expr},
                m.raw_label,
                m.normalized_label,
                {value_count_expr},
                {final_cluster_source_expr},
                {final_is_true_anomaly_expr}
            FROM taxonomy_label_cluster_map m
            JOIN taxonomy_clusters c
              ON c.field_name = m.field_name
             AND c.cluster_version = m.cluster_version
             AND c.cluster_id = m.final_cluster_id
            {names_join}
            WHERE 1 = 1
              {active_filter}
              {anomaly_filter}
              {field_filter}
        )
        SELECT *
        FROM base
    """

    return sql, params


def load_rows(conn, field=None, include_anomalies=False):
    sql, params = build_query(conn, field=field, include_anomalies=include_anomalies)
    return pd.read_sql_query(sql, conn, params=params)


def signal_key(signal):
    return (
        signal["issue_family"],
        signal["responsible_side"],
        signal["affected_or_target_side"],
    )


def analyze(df):
    label_signal_rows = []
    cluster_member_rows = []

    for _, row in df.iterrows():
        raw_label = row.get("raw_label")
        normalized_label = row.get("normalized_label")
        display_name = row.get("display_name")
        medoid_label = row.get("medoid_label")

        signals = extract_direction_signals(
            raw_label=raw_label,
            normalized_label=normalized_label,
            display_name=display_name,
            medoid_label=medoid_label,
        )

        member_payload = row.to_dict()
        member_payload["scan_text"] = normalize_text(f"{raw_label} {normalized_label}")
        member_payload["has_review_keyword"] = has_review_keyword(member_payload["scan_text"])
        member_payload["direction_signal_count"] = len(signals)
        member_payload["direction_signals"] = "; ".join(
            f"{s['issue_family']}|{s['responsible_side']}->{s['affected_or_target_side']}|{s['pattern_type']}"
            for s in signals
        )
        cluster_member_rows.append(member_payload)

        for s in signals:
            label_signal_rows.append({
                **row.to_dict(),
                "issue_family": s["issue_family"],
                "responsible_side": s["responsible_side"],
                "affected_or_target_side": s["affected_or_target_side"],
                "pattern_type": s["pattern_type"],
                "evidence": s["evidence"],
                "label_direction_key": (
                    str(row.get("field_name"))
                    + "|"
                    + str(row.get("cluster_version"))
                    + "|"
                    + str(row.get("cluster_id"))
                    + "|"
                    + str(row.get("normalized_label"))
                    + "|"
                    + s["issue_family"]
                    + "|"
                    + s["responsible_side"]
                    + "|"
                    + s["affected_or_target_side"]
                ),
            })

    signals_df = pd.DataFrame(label_signal_rows)
    if not signals_df.empty:
        signals_df = signals_df.drop_duplicates(subset=["label_direction_key"])
    members_df = pd.DataFrame(cluster_member_rows)

    affected_clusters = []

    if signals_df.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(),
            members_df,
            pd.DataFrame(),
            pd.DataFrame(),
        )

    grouped = signals_df.groupby(
        ["field_name", "cluster_version", "cluster_id", "issue_family"],
        dropna=False,
    )

    for (field_name, cluster_version, cluster_id, issue_family), g in grouped:
        side_counts = (
            g.groupby("responsible_side")["value_count"]
            .sum()
            .to_dict()
        )

        responsible_sides = set(side_counts.keys())

        has_agent_side = "agent_side" in responsible_sides
        has_customer_side = "customer_side" in responsible_sides

        if not (has_agent_side and has_customer_side):
            continue

        cluster_rows = df[
            (df["field_name"] == field_name)
            & (df["cluster_version"] == cluster_version)
            & (df["cluster_id"] == cluster_id)
        ]

        display_name = str(cluster_rows["display_name"].iloc[0]) if len(cluster_rows) else ""
        medoid_label = str(cluster_rows["medoid_label"].iloc[0]) if "medoid_label" in cluster_rows else ""

        total_cluster_occurrences = int(cluster_rows["value_count"].fillna(1).sum())
        issue_occurrences = int(g["value_count"].fillna(1).sum())
        agent_side_occurrences = int(side_counts.get("agent_side", 0))
        customer_side_occurrences = int(side_counts.get("customer_side", 0))

        smaller_side_occurrences = min(agent_side_occurrences, customer_side_occurrences)
        conflict_ratio = (
            smaller_side_occurrences / issue_occurrences
            if issue_occurrences
            else 0
        )

        if smaller_side_occurrences >= 10 or conflict_ratio >= 0.20:
            severity = "HIGH"
        elif smaller_side_occurrences >= 3 or conflict_ratio >= 0.05:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        labels_by_side = {}
        for side in ["agent_side", "customer_side"]:
            side_labels = (
                g[g["responsible_side"] == side]
                .sort_values("value_count", ascending=False)
                [["normalized_label", "value_count", "pattern_type", "evidence"]]
                .drop_duplicates()
            )
            labels_by_side[side] = "; ".join(
                f"{r.normalized_label} ({int(r.value_count)})"
                for r in side_labels.itertuples()
            )

        affected_clusters.append({
            "field_name": field_name,
            "cluster_version": cluster_version,
            "cluster_id": cluster_id,
            "display_name": display_name,
            "medoid_label": medoid_label,
            "issue_family": issue_family,
            "severity": severity,
            "cluster_label_rows": int(len(cluster_rows)),
            "cluster_occurrences_from_map": total_cluster_occurrences,
            "issue_occurrences": issue_occurrences,
            "agent_side_issue_occurrences": agent_side_occurrences,
            "customer_side_issue_occurrences": customer_side_occurrences,
            "smaller_side_occurrences": smaller_side_occurrences,
            "conflict_ratio": round(conflict_ratio, 4),
            "agent_side_labels": labels_by_side.get("agent_side", ""),
            "customer_side_labels": labels_by_side.get("customer_side", ""),
            "review_decision": "",
            "target_fix_cluster_name": "",
            "notes": "",
        })

    affected_df = pd.DataFrame(affected_clusters)

    if affected_df.empty:
        field_summary = pd.DataFrame(columns=[
            "field_name",
            "affected_clusters",
            "high",
            "medium",
            "low",
            "issue_occurrences",
        ])
        review_template = pd.DataFrame()
        return affected_df, signals_df, members_df, field_summary, review_template

    affected_df = affected_df.sort_values(
        ["severity", "issue_occurrences", "field_name"],
        ascending=[True, False, True],
    )

    affected_keys = set(
        zip(
            affected_df["field_name"],
            affected_df["cluster_version"],
            affected_df["cluster_id"],
            affected_df["issue_family"],
        )
    )

    conflicting_labels = signals_df[
        signals_df.apply(
            lambda r: (
                r["field_name"],
                r["cluster_version"],
                r["cluster_id"],
                r["issue_family"],
            )
            in affected_keys,
            axis=1,
        )
    ].copy()

    conflicting_labels = conflicting_labels.sort_values(
        ["field_name", "cluster_id", "issue_family", "responsible_side", "value_count"],
        ascending=[True, True, True, True, False],
    )

    field_summary = (
        affected_df.groupby("field_name")
        .agg(
            affected_clusters=("cluster_id", "nunique"),
            high=("severity", lambda s: int((s == "HIGH").sum())),
            medium=("severity", lambda s: int((s == "MEDIUM").sum())),
            low=("severity", lambda s: int((s == "LOW").sum())),
            issue_occurrences=("issue_occurrences", "sum"),
        )
        .reset_index()
        .sort_values(["affected_clusters", "issue_occurrences"], ascending=False)
    )

    review_template = conflicting_labels[[
        "field_name",
        "cluster_version",
        "cluster_id",
        "display_name",
        "medoid_label",
        "issue_family",
        "responsible_side",
        "affected_or_target_side",
        "raw_label",
        "normalized_label",
        "value_count",
        "pattern_type",
        "evidence",
    ]].copy()

    review_template["review_decision"] = ""
    review_template["new_cluster_id"] = ""
    review_template["new_display_name"] = ""
    review_template["review_notes"] = ""

    return affected_df, conflicting_labels, members_df, field_summary, review_template


def write_report_section(affected_df, conflicting_labels, out_path):
    lines = []
    lines.append("## Affected Clusters and Labels")
    lines.append("")
    lines.append(
        "The actor-target directionality audit scanned all taxonomy fields for clusters where the same issue family contained both agent-side and customer-side responsibility labels."
    )
    lines.append("")
    lines.append(
        "The table below lists clusters that require review before cleanup. A cluster is flagged when labels inside the same cluster point to opposite responsibility directions, such as agent-to-customer rudeness and customer-to-agent rudeness."
    )
    lines.append("")

    if affected_df.empty:
        lines.append("No affected clusters were found by the audit.")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return

    lines.append("| Field | Cluster ID | Display Name | Issue Family | Severity | Agent-side Occurrences | Customer-side Occurrences |")
    lines.append("|---|---|---|---|---|---:|---:|")

    for r in affected_df.itertuples():
        lines.append(
            f"| `{r.field_name}` | `{r.cluster_id}` | {str(r.display_name).replace('|', '/')} | "
            f"{r.issue_family} | {r.severity} | {int(r.agent_side_issue_occurrences)} | {int(r.customer_side_issue_occurrences)} |"
        )

    lines.append("")
    lines.append("### Label-level evidence")
    lines.append("")

    for cluster_key, g in conflicting_labels.groupby(
        ["field_name", "cluster_version", "cluster_id", "display_name", "issue_family"],
        dropna=False,
    ):
        field_name, cluster_version, cluster_id, display_name, issue_family = cluster_key
        lines.append(f"#### `{field_name}` / `{cluster_id}` — {display_name} / {issue_family}")
        lines.append("")
        lines.append("| Responsible Side | Label | Occurrences | Pattern | Evidence |")
        lines.append("|---|---|---:|---|---|")

        g2 = g.sort_values(["responsible_side", "value_count"], ascending=[True, False])
        for r in g2.itertuples():
            label = str(r.normalized_label).replace("|", "/")
            evidence = str(r.evidence).replace("|", "/")
            lines.append(
                f"| {r.responsible_side} | `{label}` | {int(r.value_count)} | {r.pattern_type} | {evidence} |"
            )

        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--field",
        default=None,
        help="Optional field_name filter. Omit this to scan all fields.",
    )
    parser.add_argument(
        "--include-anomalies",
        action="store_true",
        help="Include true anomaly clusters. Default scans standard clusters only.",
    )
    parser.add_argument(
        "--out",
        default="outputs/directionality_all_fields_audit",
        help="Output folder.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        df = load_rows(
            conn,
            field=args.field,
            include_anomalies=args.include_anomalies,
        )

    if df.empty:
        raise SystemExit("No taxonomy rows found.")

    affected_df, conflicting_labels_df, members_df, field_summary_df, review_template_df = analyze(df)

    affected_df.to_csv(out_dir / "01_affected_clusters.csv", index=False)
    conflicting_labels_df.to_csv(out_dir / "02_conflicting_labels.csv", index=False)
    members_df.to_csv(out_dir / "03_all_scanned_cluster_members.csv", index=False)
    field_summary_df.to_csv(out_dir / "04_field_summary.csv", index=False)
    review_template_df.to_csv(out_dir / "05_cleanup_review_template.csv", index=False)

    write_report_section(
        affected_df=affected_df,
        conflicting_labels=conflicting_labels_df,
        out_path=out_dir / "06_report_section_affected_clusters.md",
    )
    unique_cluster_count = (
        affected_df[["field_name", "cluster_version", "cluster_id"]]
        .drop_duplicates()
        .shape[0]
        if not affected_df.empty
        else 0
    )

    print(f"Affected issue-family rows: {len(affected_df):,}")
    print(f"Unique affected clusters: {unique_cluster_count:,}")
    print("Actor-target directionality audit complete")
    print(f"Rows scanned: {len(df):,}")
    print(f"Conflicting labels: {len(conflicting_labels_df):,}")
    print(f"Output folder: {out_dir}")

    if not field_summary_df.empty:
        print("")
        print("Field summary:")
        print(field_summary_df.to_string(index=False))


if __name__ == "__main__":
    main()