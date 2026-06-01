import argparse
import os
import re
from pathlib import Path
from collections import Counter

import pandas as pd
import psycopg2
from dotenv import load_dotenv


load_dotenv()


ROLE_GROUPS = {
    "agent_side": [
        "agent",
        "broker",
        "advisor",
        "consultant",
        "representative",
        "rep",
        "salesperson",
        "colleague",
        "internal colleague",
    ],
    "customer_side": [
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
        "owner",
        "dm",
        "decision maker",
        "decisionmaker",
        "inbound caller",
        "manager",
        "maintenance manager",
        "facilities manager",
        "office manager",
        "practice manager",
        "finance manager",
        "site manager",
        "estates manager",
        "business manager",
        "hr manager",
        "property manager",
        "staff",
        "employee",
    ],
}

ROLE_ALIASES = {}
for group_name, terms in ROLE_GROUPS.items():
    for term in terms:
        ROLE_ALIASES[term] = group_name

ROLE_TERMS_SORTED = sorted(ROLE_ALIASES.keys(), key=len, reverse=True)

STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "at",
    "on",
    "in",
    "with",
    "for",
    "from",
    "of",
    "by",
    "and",
    "or",
    "but",
    "about",
    "after",
    "before",
    "during",
    "due",
    "because",
    "was",
    "were",
    "is",
    "are",
    "be",
    "being",
    "been",
    "had",
    "has",
    "have",
    "having",
    "previous",
    "post",
    "pre",
}

GENERIC_RELATION_FAMILIES = {
    "call",
    "called",
    "contact",
    "contacted",
    "speak",
    "spoke",
    "talk",
    "talked",
    "ask",
    "asked",
    "request",
    "requested",
    "follow",
    "followed",
    "update",
    "updated",
    "inform",
    "informed",
    "reach",
    "reached",
}

REPORTING_VERBS = {
    "accused",
    "accuse",
    "called",
    "complained",
    "complaint",
    "reported",
    "report",
    "mentioned",
    "stated",
    "claimed",
    "said",
    "commented",
    "confronted",
    "challenged",
    "perceived",
}
PASSIVE_MARKERS = {
    "by",
}

CAUSAL_MARKERS = {
    "due to",
    "because of",
    "caused by",
    "from",
}


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


def simple_stem(token):
    token = normalize_text(token)

    replacements = {
        "insulted": "insult",
        "insulting": "insult",
        "abused": "abuse",
        "abusive": "abuse",
        "threatened": "threaten",
        "threatening": "threaten",
        "shouted": "shout",
        "shouting": "shout",
        "yelled": "yell",
        "yelling": "yell",
        "complained": "complain",
        "complaint": "complain",
        "accused": "accuse",
        "called": "call",
        "commented": "comment",
        "confronted": "confront",
        "challenged": "challenge",
        "disconnected": "disconnect",
        "terminated": "terminate",
        "rejected": "reject",
        "refused": "refuse",
        "ignored": "ignore",
        "interrupted": "interrupt",
        "criticized": "criticize",
    }

    if token in replacements:
        return replacements[token]

    for suffix in ["ing", "ed", "es", "s"]:
        if len(token) > 5 and token.endswith(suffix):
            return token[: -len(suffix)]

    return token


def normalize_relation_phrase(value):
    text = normalize_text(value)
    tokens = [simple_stem(t) for t in text.split() if t not in STOPWORDS]

    cleaned = []
    for token in tokens:
        if token in ROLE_ALIASES:
            continue
        if token in STOPWORDS:
            continue
        cleaned.append(token)

    return " ".join(cleaned).strip()


def relation_family_from_phrase(phrase):
    phrase = normalize_relation_phrase(phrase)
    if not phrase:
        return ""

    tokens = phrase.split()
    if not tokens:
        return ""

    return tokens[0]


def role_regex():
    return r"\b(" + "|".join(re.escape(t) for t in ROLE_TERMS_SORTED) + r")\b"


def find_roles(text):
    text = normalize_text(text)
    matches = []

    pattern = re.compile(role_regex())

    for m in pattern.finditer(text):
        term = m.group(1)
        group = ROLE_ALIASES.get(term)

        if not group:
            continue

        matches.append({
            "term": term,
            "group": group,
            "start": m.start(),
            "end": m.end(),
        })

    # Remove overlapping shorter matches.
    filtered = []
    for match in matches:
        overlap = False
        for existing in filtered:
            if not (match["end"] <= existing["start"] or match["start"] >= existing["end"]):
                overlap = True
                break
        if not overlap:
            filtered.append(match)

    return filtered


def contains_any(text, markers):
    text = normalize_text(text)
    return any(marker in text for marker in markers)


def first_reporting_verb(text):
    tokens = normalize_text(text).split()
    for token in tokens:
        stem = simple_stem(token)
        if token in REPORTING_VERBS or stem in REPORTING_VERBS:
            return stem
    return ""


def extract_generic_role_signals(raw_label, normalized_label):
    """
    Extracts generic role-direction signals from any label containing two role entities.

    Signal meaning:
      actor_side / actor_term = likely source, responsible party, or initiator
      target_side / target_term = likely target, affected party, or reported-against party

    This is broad discovery logic, not final truth.
    """
    text = normalize_text(normalized_label or raw_label)

    if not text:
        return []

    roles = find_roles(text)

    if len(roles) < 2:
        return []

    signals = []

    for i in range(len(roles)):
        for j in range(i + 1, len(roles)):
            left_role = roles[i]
            right_role = roles[j]

            if left_role["group"] == right_role["group"]:
                continue

            before_left = text[max(0, left_role["start"] - 60):left_role["start"]]
            between = text[left_role["end"]:right_role["start"]]
            after_right = text[right_role["end"]: min(len(text), right_role["end"] + 80)]

            full_context = f"{before_left} {left_role['term']} {between} {right_role['term']} {after_right}"
            between_norm = normalize_text(between)
            after_norm = normalize_text(after_right)
            before_norm = normalize_text(before_left)

            pattern_type = "direct_or_initiator"
            actor = left_role
            target = right_role
            relation_source = f"{between_norm} {after_norm}"

            # Passive case:
            # agent insulted by customer
            # customer rejected by broker
            if contains_any(between_norm, PASSIVE_MARKERS):
                pattern_type = "passive"
                actor = right_role
                target = left_role
                relation_source = between_norm.replace(" by ", " ")

            # Causal case:
            # customer hung up due to agent silence
            # customer complained because of broker behavior
            elif contains_any(between_norm, CAUSAL_MARKERS):
                pattern_type = "causal"
                actor = right_role
                target = left_role
                relation_source = f"{before_norm} {between_norm} {after_norm}"

            # Reporting case:
            # customer accused agent of rudeness
            # agent called customer rude
            # broker reported client behavior
            else:
                report_verb = first_reporting_verb(between_norm)

                if report_verb:
                    pattern_type = "reporting_or_accusation"
                    actor = right_role
                    target = left_role
                    relation_source = f"{report_verb} {after_norm}"

            relation_phrase = normalize_relation_phrase(relation_source)
            relation_family = relation_family_from_phrase(relation_phrase)

            if not relation_family:
                continue

            direction_key = f"{actor['group']}->{target['group']}"
            reverse_direction_key = f"{target['group']}->{actor['group']}"

            unordered_side_pair = "|".join(sorted([actor["group"], target["group"]]))
            unordered_role_pair = "|".join(sorted([actor["term"], target["term"]]))

            confidence_hint = "MEDIUM"
            if relation_family in GENERIC_RELATION_FAMILIES:
                confidence_hint = "LOW"
            if pattern_type in {"passive", "reporting_or_accusation", "causal"}:
                confidence_hint = "HIGH"

            signals.append({
                "actor_side": actor["group"],
                "actor_term": actor["term"],
                "target_side": target["group"],
                "target_term": target["term"],
                "direction_key": direction_key,
                "reverse_direction_key": reverse_direction_key,
                "unordered_side_pair": unordered_side_pair,
                "unordered_role_pair": unordered_role_pair,
                "relation_family": relation_family,
                "relation_phrase": relation_phrase,
                "pattern_type": pattern_type,
                "confidence_hint": confidence_hint,
                "evidence": normalize_text(full_context),
            })

    # Deduplicate signals for same label.
    unique = []
    seen = set()

    for signal in signals:
        key = (
            signal["actor_side"],
            signal["target_side"],
            signal["relation_family"],
            signal["pattern_type"],
            signal["evidence"],
        )

        if key not in seen:
            seen.add(key)
            unique.append(signal)

    return unique


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

    medoid_expr = "NULL AS medoid_label"
    if "medoid_label" in c_cols:
        medoid_expr = "c.medoid_label"

    cluster_size_expr = "NULL AS cluster_size"
    if "cluster_size" in c_cols:
        cluster_size_expr = "c.cluster_size"

    total_occurrences_expr = "NULL AS total_occurrences"
    if "total_occurrences" in c_cols:
        total_occurrences_expr = "c.total_occurrences"

    value_count_expr = "1 AS value_count"
    if "value_count" in m_cols:
        value_count_expr = "COALESCE(m.value_count, 1) AS value_count"

    final_cluster_source_expr = "NULL AS final_cluster_source"
    if "final_cluster_source" in m_cols:
        final_cluster_source_expr = "m.final_cluster_source"

    final_is_true_anomaly_expr = "NULL AS final_is_true_anomaly"
    if "final_is_true_anomaly" in m_cols:
        final_is_true_anomaly_expr = "m.final_is_true_anomaly"

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


def analyze(df, min_side_occurrences):
    all_signal_rows = []

    for _, row in df.iterrows():
        signals = extract_generic_role_signals(
            raw_label=row.get("raw_label"),
            normalized_label=row.get("normalized_label"),
        )

        for signal in signals:
            payload = row.to_dict()
            payload.update(signal)
            payload["label_signal_key"] = (
                str(row.get("field_name"))
                + "|"
                + str(row.get("cluster_version"))
                + "|"
                + str(row.get("cluster_id"))
                + "|"
                + str(row.get("normalized_label"))
                + "|"
                + signal["actor_side"]
                + "|"
                + signal["target_side"]
                + "|"
                + signal["relation_family"]
                + "|"
                + signal["pattern_type"]
            )
            all_signal_rows.append(payload)

    signals_df = pd.DataFrame(all_signal_rows)

    if signals_df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    signals_df = signals_df.drop_duplicates(subset=["label_signal_key"])

    affected_rows = []

    group_cols = [
        "field_name",
        "cluster_version",
        "cluster_id",
        "display_name",
        "relation_family",
        "unordered_side_pair",
    ]

    for group_key, g in signals_df.groupby(group_cols, dropna=False):
        (
            field_name,
            cluster_version,
            cluster_id,
            display_name,
            relation_family,
            unordered_side_pair,
        ) = group_key

        directions = sorted(g["direction_key"].dropna().unique().tolist())

        if len(directions) < 2:
            continue

        direction_occurrences = (
            g.groupby("direction_key")["value_count"]
            .sum()
            .sort_values(ascending=False)
            .to_dict()
        )

        # Need at least one reverse pair.
        has_reverse_pair = False

        for d in directions:
            left, right = d.split("->")
            reverse = f"{right}->{left}"
            if reverse in directions:
                has_reverse_pair = True
                break

        if not has_reverse_pair:
            continue

        smaller_side_occurrences = min(direction_occurrences.values())
        total_issue_occurrences = sum(direction_occurrences.values())

        if smaller_side_occurrences < min_side_occurrences:
            severity = "LOW"
        elif smaller_side_occurrences >= 10:
            severity = "HIGH"
        elif smaller_side_occurrences >= 3:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        confidence_values = set(g["confidence_hint"].dropna().tolist())

        if "HIGH" in confidence_values and severity == "MEDIUM":
            severity = "HIGH"

        affected_rows.append({
            "field_name": field_name,
            "cluster_version": cluster_version,
            "cluster_id": cluster_id,
            "display_name": display_name,
            "relation_family": relation_family,
            "unordered_side_pair": unordered_side_pair,
            "severity": severity,
            "direction_count": len(directions),
            "directions_found": "; ".join(directions),
            "direction_occurrences": str(direction_occurrences),
            "smaller_side_occurrences": int(smaller_side_occurrences),
            "total_issue_occurrences": int(total_issue_occurrences),
            "is_generic_relation": relation_family in GENERIC_RELATION_FAMILIES,
            "recommended_review_decision": "",
            "notes": "",
        })

    affected_df = pd.DataFrame(affected_rows)

    if affected_df.empty:
        field_summary = pd.DataFrame()
        affected_labels = pd.DataFrame()
        return affected_df, signals_df, field_summary, affected_labels

    affected_df = affected_df.sort_values(
        ["severity", "total_issue_occurrences"],
        ascending=[True, False],
    )

    affected_keys = set(
        zip(
            affected_df["field_name"],
            affected_df["cluster_version"],
            affected_df["cluster_id"],
            affected_df["relation_family"],
            affected_df["unordered_side_pair"],
        )
    )

    affected_labels = signals_df[
        signals_df.apply(
            lambda r: (
                r["field_name"],
                r["cluster_version"],
                r["cluster_id"],
                r["relation_family"],
                r["unordered_side_pair"],
            )
            in affected_keys,
            axis=1,
        )
    ].copy()

    affected_labels = affected_labels.sort_values(
        [
            "field_name",
            "cluster_id",
            "relation_family",
            "direction_key",
            "value_count",
        ],
        ascending=[True, True, True, True, False],
    )

    field_summary = (
        affected_df.groupby("field_name")
        .agg(
            affected_issue_rows=("cluster_id", "count"),
            unique_affected_clusters=("cluster_id", "nunique"),
            high=("severity", lambda s: int((s == "HIGH").sum())),
            medium=("severity", lambda s: int((s == "MEDIUM").sum())),
            low=("severity", lambda s: int((s == "LOW").sum())),
            total_issue_occurrences=("total_issue_occurrences", "sum"),
        )
        .reset_index()
        .sort_values(
            ["unique_affected_clusters", "total_issue_occurrences"],
            ascending=False,
        )
    )

    return affected_df, signals_df, field_summary, affected_labels


def write_report_section(affected_df, affected_labels, out_path):
    lines = []

    lines.append("## Generic Actor-Role Reversal Audit")
    lines.append("")
    lines.append(
        "A broader actor-role reversal audit was run without limiting the scan to predefined issue keywords. "
        "The audit looked for any taxonomy label containing two role entities, extracted the likely direction of the relationship, "
        "and flagged clusters where the same relation family appeared in both directions."
    )
    lines.append("")

    if affected_df.empty:
        lines.append("No generic actor-role reversal candidates were found.")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return

    lines.append("| Field | Cluster ID | Display Name | Relation Family | Severity | Directions Found | Occurrences |")
    lines.append("|---|---|---|---|---|---|---:|")

    for r in affected_df.itertuples():
        lines.append(
            f"| `{r.field_name}` | `{r.cluster_id}` | {str(r.display_name).replace('|', '/')} | "
            f"`{r.relation_family}` | {r.severity} | {str(r.directions_found).replace('|', '/')} | "
            f"{int(r.total_issue_occurrences)} |"
        )

    lines.append("")
    lines.append("### Label-level evidence")
    lines.append("")

    group_cols = [
        "field_name",
        "cluster_version",
        "cluster_id",
        "display_name",
        "relation_family",
    ]

    for key, g in affected_labels.groupby(group_cols, dropna=False):
        field_name, cluster_version, cluster_id, display_name, relation_family = key

        lines.append(f"#### `{field_name}` / `{cluster_id}` — {display_name} / `{relation_family}`")
        lines.append("")
        lines.append("| Direction | Label | Occurrences | Pattern | Relation Phrase | Evidence |")
        lines.append("|---|---|---:|---|---|---|")

        for row in g.itertuples():
            label = str(row.normalized_label).replace("|", "/")
            evidence = str(row.evidence).replace("|", "/")
            relation_phrase = str(row.relation_phrase).replace("|", "/")

            lines.append(
                f"| `{row.direction_key}` | `{label}` | {int(row.value_count)} | "
                f"{row.pattern_type} | `{relation_phrase}` | {evidence} |"
            )

        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--field", default=None)
    parser.add_argument("--include-anomalies", action="store_true")
    parser.add_argument("--min-side-occurrences", type=int, default=1)
    parser.add_argument(
        "--out",
        default="outputs/generic_actor_role_reversal_audit",
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

    affected_df, signals_df, field_summary_df, affected_labels_df = analyze(
        df=df,
        min_side_occurrences=args.min_side_occurrences,
    )

    affected_df.to_csv(out_dir / "01_generic_affected_clusters.csv", index=False)
    affected_labels_df.to_csv(out_dir / "02_generic_affected_labels.csv", index=False)
    signals_df.to_csv(out_dir / "03_all_generic_role_signals.csv", index=False)
    field_summary_df.to_csv(out_dir / "04_generic_field_summary.csv", index=False)

    write_report_section(
        affected_df=affected_df,
        affected_labels=affected_labels_df,
        out_path=out_dir / "05_generic_report_section.md",
    )

    unique_cluster_count = (
        affected_df[["field_name", "cluster_version", "cluster_id"]]
        .drop_duplicates()
        .shape[0]
        if not affected_df.empty
        else 0
    )

    print("Generic actor-role reversal audit complete")
    print(f"Rows scanned: {len(df):,}")
    print(f"Role-direction signals found: {len(signals_df):,}")
    print(f"Affected issue-family rows: {len(affected_df):,}")
    print(f"Unique affected clusters: {unique_cluster_count:,}")
    print(f"Output folder: {out_dir}")

    if not field_summary_df.empty:
        print("")
        print("Field summary:")
        print(field_summary_df.to_string(index=False))


if __name__ == "__main__":
    main()