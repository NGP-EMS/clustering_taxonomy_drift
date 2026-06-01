# build_confirmed_directionality_cleanup_staging.py

from pathlib import Path
import pandas as pd


INPUT_PATH = Path("outputs/generic_actor_role_reversal_triage_20260601/06_cleanup_staging_review_template.csv")
OUT_PATH = Path("outputs/generic_actor_role_reversal_triage_20260601/08_confirmed_cleanup_staging.csv")


def main():
    df = pd.read_csv(INPUT_PATH)

    confirmed = df[df["triage_decision"] == "CONFIRMED_FIX"].copy()

    move_rows = confirmed[
        (
            (confirmed["field_name"] == "additional_tags")
            & (confirmed["cluster_id"] == "base_2246")
            & (confirmed["direction_key"] == "customer_side->agent_side")
        )
        |
        (
            (confirmed["field_name"] == "additional_tags")
            & (confirmed["cluster_id"] == "base_1657")
            & (confirmed["direction_key"] == "customer_side->agent_side")
        )
    ].copy()

    # Force text columns to object/string-safe columns before assignment.
    for col in [
        "cleanup_decision",
        "target_cluster_id",
        "target_display_name",
        "review_notes",
    ]:
        if col not in move_rows.columns:
            move_rows[col] = ""
        move_rows[col] = move_rows[col].fillna("").astype("object")

    move_rows["cleanup_decision"] = "MOVE_TO_MANUAL_DIRECTIONAL_CLUSTER"

    mask_2246 = move_rows["cluster_id"] == "base_2246"
    mask_1657 = move_rows["cluster_id"] == "base_1657"

    move_rows.loc[mask_2246, "target_cluster_id"] = "manual_customer_insulted_agent"
    move_rows.loc[mask_2246, "target_display_name"] = "Customer Insulted Agent"

    move_rows.loc[mask_1657, "target_cluster_id"] = "manual_agent_reported_customer_rudeness"
    move_rows.loc[mask_1657, "target_display_name"] = "Agent Reported Customer Rudeness"

    move_rows["review_notes"] = (
        "Confirmed actor-target directionality cleanup. "
        "Moved reversed responsibility labels out of the original standard cluster."
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    move_rows.to_csv(OUT_PATH, index=False)

    print("Confirmed cleanup staging created")
    print(f"Input rows: {len(df):,}")
    print(f"Confirmed rows: {len(confirmed):,}")
    print(f"Rows to move: {len(move_rows):,}")
    print(f"Output: {OUT_PATH}")

    if not move_rows.empty:
        print("")
        print(
            move_rows[
                [
                    "field_name",
                    "cluster_id",
                    "display_name",
                    "normalized_label",
                    "value_count",
                    "direction_key",
                    "target_cluster_id",
                    "target_display_name",
                ]
            ].to_string(index=False)
        )


if __name__ == "__main__":
    main()