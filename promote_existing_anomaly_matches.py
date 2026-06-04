#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from typing import Optional, Sequence

import pandas as pd
import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def build_local_database_url_from_env() -> Optional[str]:
    host = os.getenv("LOCAL_PG_HOST")
    port = os.getenv("LOCAL_PG_PORT")
    db = os.getenv("LOCAL_PG_DB")
    user = os.getenv("LOCAL_PG_USER")
    password = os.getenv("LOCAL_PG_PASSWORD")

    if not all([host, port, db, user, password]):
        return None

    return f"host={host} port={port} dbname={db} user={user} password={password}"


def connect(dsn: str):
    return psycopg2.connect(dsn)


def promote(args) -> None:
    df = pd.read_csv(args.input_csv)

    required = {
        "matched_field_name",
        "matched_cluster_id",
        "matched_display_name",
        "matched_run_id",
        "matched_cluster_version",
        "count",
    }

    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing required columns: {sorted(missing)}")

    df = df.copy()
    df["count"] = pd.to_numeric(df["count"], errors="coerce").fillna(0).astype(int)

    df = df[
        df["matched_field_name"].notna()
        & df["matched_cluster_id"].notna()
        & df["matched_display_name"].notna()
    ]

    grouped = (
        df.groupby(
            [
                "matched_field_name",
                "matched_cluster_id",
                "matched_display_name",
                "matched_run_id",
                "matched_cluster_version",
            ],
            dropna=False,
            as_index=False,
        )
        .agg(
            total_occurrences_from_backfill=("count", "sum"),
            source_labels=("label", lambda s: sorted(set(str(x) for x in s if str(x).strip()))[:25]),
            source_label_count=("label", "nunique"),
        )
        .sort_values("total_occurrences_from_backfill", ascending=False)
    )

    if args.min_occurrences > 1:
        grouped = grouped[grouped["total_occurrences_from_backfill"] >= args.min_occurrences]

    output_review = args.review_output
    grouped.to_csv(output_review, index=False)

    summary = {
        "input_csv": args.input_csv,
        "review_output": output_review,
        "dry_run": args.dry_run,
        "min_occurrences": args.min_occurrences,
        "input_rows": int(len(df)),
        "unique_clusters_to_promote": int(len(grouped)),
        "total_occurrences_from_backfill": int(grouped["total_occurrences_from_backfill"].sum()) if len(grouped) else 0,
        "started_at_utc": datetime.utcnow().isoformat(),
    }

    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.dry_run:
        print("\nDry run only. Review CSV written:")
        print(output_review)
        return

    conn = connect(args.local_database_url)
    conn.autocommit = False

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            promoted_names = 0
            promoted_clusters = 0

            for row in grouped.to_dict("records"):
                field_name = str(row["matched_field_name"])
                cluster_id = str(row["matched_cluster_id"])
                run_id = str(row["matched_run_id"])
                cluster_version = str(row["matched_cluster_version"])

                cur.execute(
                    """
                    UPDATE taxonomy_cluster_names
                    SET is_anomaly = FALSE,
                        updated_at = NOW()
                    WHERE field_name = %s
                      AND cluster_id = %s
                      AND COALESCE(run_id, '') = COALESCE(%s, '')
                      AND COALESCE(cluster_version, '') = COALESCE(%s, '')
                      AND is_anomaly = TRUE
                    """,
                    (field_name, cluster_id, run_id, cluster_version),
                )
                promoted_names += cur.rowcount

                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'taxonomy_clusters'
                    """
                )
                cluster_columns = {r["column_name"] for r in cur.fetchall()}

                set_parts = []

                if "is_anomaly" in cluster_columns:
                    set_parts.append("is_anomaly = FALSE")

                if "is_true_anomaly_cluster" in cluster_columns:
                    set_parts.append("is_true_anomaly_cluster = FALSE")

                if "updated_at" in cluster_columns:
                    set_parts.append("updated_at = NOW()")

                if set_parts:
                    update_cluster_sql = f"""
                        UPDATE taxonomy_clusters
                        SET {", ".join(set_parts)}
                        WHERE field_name = %s
                          AND cluster_id = %s
                          AND COALESCE(run_id, '') = COALESCE(%s, '')
                    """

                    cur.execute(
                        update_cluster_sql,
                        (field_name, cluster_id, run_id),
                    )
                    promoted_clusters += cur.rowcount

            conn.commit()

        summary["finished_at_utc"] = datetime.utcnow().isoformat()
        summary["promoted_name_rows"] = promoted_names
        summary["promoted_cluster_rows"] = promoted_clusters

        with open(args.summary_output, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\nPromotion summary written to: {args.summary_output}")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(
        description="Promote existing same-field anomaly matches to standard taxonomy clusters."
    )

    parser.add_argument(
        "--input-csv",
        default="promote_existing_anomaly_matches.csv",
    )

    parser.add_argument(
        "--review-output",
        default="promote_existing_anomaly_review.csv",
    )

    parser.add_argument(
        "--summary-output",
        default="promote_existing_anomaly_summary.json",
    )

    parser.add_argument(
        "--local-database-url",
        default=os.getenv("LOCAL_DATABASE_URL") or build_local_database_url_from_env(),
    )

    parser.add_argument(
        "--dry-run",
        default=True,
        type=lambda x: str(x).lower() in {"true", "1", "yes", "y"},
    )

    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=1,
        help="Only promote clusters with at least this many matched backfill occurrences.",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
    )

    args = parser.parse_args(argv)

    if not args.local_database_url:
        raise RuntimeError("Missing local DB connection. Set LOCAL_DATABASE_URL or LOCAL_PG_* env vars.")

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    promote(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())