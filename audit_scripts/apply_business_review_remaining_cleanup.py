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

SOURCE_CLUSTER_IDS = {
    "strict_315",   # Agent IVR Handling Issues leftovers
    "strict_38",    # Broker Call Fatigue leftovers
    "base_542",     # Claimed Previous Contact
    "strict_304",   # Agent Claimed Previous
}

DEFAULT_OUT_DIR = Path("outputs/business_review_remaining_cleanup_20260601")


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


def starts_any(label, prefixes):
    return any(label.startswith(p) for p in prefixes)


def contains_any(label, terms):
    return any(t in label for t in terms)


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

    # Business-history / previous contact claims.
    if starts_any(l, [
        "customer claimed previous contact",
        "customer claims previous contact",
        "customer claimed agent already contacted",
        "customer claimed agent contacted",
        "customer claimed previous agent contact",
        "customer claimed already contacted",
        "customer claimed prior contact",
        "customer claimed previous call",
        "customer claimed previous conversation",
        "contact claimed previous contact",
        "dm claimed previous contact",
    ]):
        return (
            "manual_customer_claimed_previous_contact",
            "Customer Claimed Previous Contact",
            "Customer/contact/DM claimed previous contact or prior agent interaction.",
        )

    if starts_any(l, [
        "agent claimed previous contact",
        "agent claims previous contact",
        "agent claimed customer previously contacted",
        "agent claimed customer already contacted",
        "agent claimed prior contact",
        "agent claimed previous call",
        "agent claimed previous conversation",
        "agent claimed customer was looking for them",
    ]):
        return (
            "manual_agent_claimed_previous_contact",
            "Agent Claimed Previous Contact",
            "Agent claimed previous contact or prior customer interaction.",
        )

    # Challenge direction.
    if starts_any(l, [
        "customer challenged agent",
        "agent challenged by customer",
        "customer challenged previous agent",
    ]):
        return (
            "manual_customer_challenged_agent",
            "Customer Challenged Agent",
            "Customer challenged agent or agent was challenged by customer.",
        )

    if starts_any(l, [
        "agent challenged customer",
        "agent challenged client",
        "agent challenged contact",
        "agent challenged dm",
    ]):
        return (
            "manual_agent_challenged_customer",
            "Agent Challenged Customer",
            "Agent challenged customer/client/contact/DM.",
        )

    # Request / seek / callback direction.
    if starts_any(l, [
        "customer requested agent",
        "customer requested specific agent",
        "customer requested previous agent",
        "customer requested agent callback",
        "customer requested agent call back",
        "customer requested agent contact",
        "customer requested callback from agent",
        "customer asked for agent",
        "customer asked agent callback",
        "customer seeking agent",
        "customer seeks agent",
        "customer sought agent",
        "customer looking for agent",
        "customer wants agent",
    ]):
        return (
            "manual_customer_requested_agent",
            "Customer Requested Agent",
            "Customer requested, asked for, or sought an agent/contact.",
        )

    if starts_any(l, [
        "agent requested customer",
        "agent requested client",
        "agent requested contact",
        "agent requested dm",
        "agent asked customer",
        "agent asked client",
        "agent asked contact",
        "agent asked dm",
        "agent requested customer details",
        "agent requested customer information",
        "agent requested customer dob",
        "agent requested customer email",
        "agent requested customer number",
    ]):
        return (
            "manual_agent_requested_customer_action",
            "Agent Requested Customer Action",
            "Agent requested customer/client/contact/DM details, information, or action.",
        )

    # Claim / accusation direction. Business-history claims are handled above.
    if starts_any(l, [
        "customer claimed agent",
        "customer claims agent",
        "customer claimed agent lied",
        "customer claimed agent misled",
        "customer claimed agent misleading",
        "customer claimed agent pushy",
        "customer claimed agent dishonest",
        "customer claimed agent rude",
    ]):
        return (
            "manual_customer_reported_agent_misconduct",
            "Customer Reported Agent Misconduct",
            "Customer claim or accusation about agent behaviour.",
        )

    if starts_any(l, [
        "agent claimed customer",
        "agent claims customer",
        "agent claimed customer lied",
        "agent claimed customer abusive",
        "agent claimed customer hostile",
        "agent claimed customer rude",
    ]):
        return (
            "manual_agent_claimed_customer_issue",
            "Agent Claimed Customer Issue",
            "Agent claim about customer behaviour.",
        )

    # Question direction.
    if starts_any(l, [
        "customer questioned agent",
        "customer questioned previous agent",
        "agent questioned by customer",
        "agent questioned by client",
        "agent questioned by contact",
        "agent questioned by dm",
    ]):
        return (
            "manual_customer_questioned_agent",
            "Customer Questioned Agent",
            "Customer questioned agent or agent was questioned by customer/client/contact/DM.",
        )

    if starts_any(l, [
        "agent questioned customer",
        "agent questioned client",
        "agent questioned contact",
        "agent questioned dm",
    ]):
        return (
            "manual_agent_questioned_customer",
            "Agent Questioned Customer",
            "Agent questioned customer/client/contact/DM.",
        )

    # Confusion direction.
    if starts_any(l, [
        "customer confused by agent",
        "customer confused about agent",
        "customer confused by previous agent",
        "customer confused about previous agent",
        "customer confused due to agent",
        "customer confused agent identity",
        "customer confused about agent identity",
    ]):
        return (
            "manual_customer_confused_by_agent",
            "Customer Confused By Agent",
            "Customer confusion caused by current or previous agent.",
        )

    if starts_any(l, [
        "agent confused by customer",
        "agent confused about customer",
        "agent confused by client",
        "agent confused by contact",
        "agent confused by dm",
        "agent confused about contact",
        "agent confused about dm",
    ]):
        return (
            "manual_agent_confused_by_customer",
            "Agent Confused By Customer",
            "Agent confusion caused by customer/client/contact/DM.",
        )

    # Refusal direction.
    if starts_any(l, [
        "customer refused agent",
        "customer refused to speak to agent",
        "customer refused agent contact",
        "customer refused further agent contact",
        "customer refused contact with agent",
        "dm refused agent",
        "contact refused agent",
        "client refused agent",
        "customer refused agent details",
        "customer refused agent number",
    ]):
        return (
            "manual_customer_refused_agent_contact",
            "Customer Refused Agent Contact",
            "Customer/client/contact/DM refused agent contact.",
        )

    if starts_any(l, [
        "agent refused customer",
        "agent refused client",
        "agent refused contact",
        "agent refused dm",
        "agent refused to help customer",
        "agent refused customer request",
        "agent refused client request",
        "agent refused contact info",
        "agent refused contact details",
    ]):
        return (
            "manual_agent_refused_customer",
            "Agent Refused Customer",
            "Agent refused customer/client/contact/DM request or contact.",
        )

    # Dispute direction.
    if starts_any(l, [
        "customer disputed agent",
        "customer disputed agent claim",
        "customer disputed previous agent",
        "agent disputed by customer",
    ]):
        return (
            "manual_customer_disputed_agent",
            "Customer Disputed Agent",
            "Customer disputed agent, claim, or statement.",
        )

    if starts_any(l, [
        "agent disputed customer",
        "agent disputed client",
        "agent disputed contact",
        "agent disputed dm",
    ]):
        return (
            "manual_agent_disputed_customer",
            "Agent Disputed Customer",
            "Agent disputed customer/client/contact/DM claim or statement.",
        )

    # Hostility direction.
    if starts_any(l, [
        "customer hostile to agent",
        "customer hostile agent",
        "customer hostile towards agent",
        "customer hostile due to agent",
        "customer hostile to agent claims",
        "customer hostile due to agent behavior",
    ]):
        return (
            "manual_customer_hostile_to_agent",
            "Customer Hostile To Agent",
            "Customer hostility toward agent.",
        )

    if starts_any(l, [
        "agent hostile to customer",
        "agent hostile customer",
        "agent hostile towards customer",
        "agent hostile to client",
        "agent hostile to contact",
        "agent hostile to dm",
    ]):
        return (
            "manual_agent_hostile_to_customer",
            "Agent Hostile To Customer",
            "Agent hostility toward customer/client/contact/DM.",
        )

    # Complaint direction.
    if starts_any(l, [
        "customer complained about agent",
        "customer complaint about agent",
        "customer complained agent",
        "customer complained of agent",
        "customer complaint previous agent",
        "customer complained previous agent",
        "customer complaint about previous agent",
    ]):
        return (
            "manual_customer_reported_agent_misconduct",
            "Customer Reported Agent Misconduct",
            "Customer complaint or report about agent behaviour.",
        )

    if starts_any(l, [
        "agent complained about customer",
        "agent complaint about customer",
        "agent complained customer",
        "agent complained of customer",
        "agent complained about client",
        "agent complained about contact",
        "agent complained about dm",
    ]):
        return (
            "manual_agent_complained_about_customer",
            "Agent Complained About Customer",
            "Agent complaint or report about customer/client/contact/DM behaviour.",
        )

    # Dismissive / criticism / lecture direction.
    if starts_any(l, [
        "agent dismissive to customer",
        "agent dismissive toward customer",
        "agent was dismissive to customer",
        "customer felt agent dismissive",
        "customer said agent dismissive",
        "customer called agent dismissive",
        "agent criticized customer",
        "agent criticised customer",
        "agent lectured customer",
        "agent lectured client",
        "agent lectured contact",
        "agent lectured dm",
    ]):
        return (
            "manual_agent_dismissive_or_critical_to_customer",
            "Agent Dismissive Or Critical To Customer",
            "Agent was dismissive, critical, or lecturing toward customer/client/contact/DM.",
        )

    if starts_any(l, [
        "customer dismissive to agent",
        "customer dismissive toward agent",
        "customer was dismissive to agent",
        "customer criticized agent",
        "customer criticised agent",
        "customer lectured agent",
    ]):
        return (
            "manual_customer_dismissive_or_critical_to_agent",
            "Customer Dismissive Or Critical To Agent",
            "Customer was dismissive, critical, or lecturing toward agent.",
        )

    # Unable/cannot. Only exact business direction.
    if starts_any(l, [
        "agent unable to help customer",
        "agent unable to assist customer",
        "agent cannot help customer",
        "agent cannot assist customer",
        "agent unable to answer customer",
        "agent cannot answer customer",
    ]):
        return (
            "manual_agent_unable_to_help_customer",
            "Agent Unable To Help Customer",
            "Agent unable/cannot help, assist, or answer customer.",
        )

    if starts_any(l, [
        "customer unable to reach agent",
        "customer cannot reach agent",
        "customer unable to contact agent",
        "customer cannot contact agent",
        "customer unable to get agent",
    ]):
        return (
            "manual_customer_unable_to_reach_agent",
            "Customer Unable To Reach Agent",
            "Customer unable/cannot reach or contact agent.",
        )

    # Call ending direction.
    if starts_any(l, [
        "agent hung up on customer",
        "agent terminated call with customer",
        "agent ended call on customer",
        "customer hung up by agent",
        "customer call terminated by agent",
        "agent cut off customer",
        "customer cut off by agent",
    ]):
        return (
            "manual_agent_ended_call_on_customer",
            "Agent Ended Call On Customer",
            "Agent hung up, terminated, cut off, or ended call on customer.",
        )

    if starts_any(l, [
        "customer hung up on agent",
        "customer terminated call with agent",
        "customer ended call on agent",
        "agent hung up by customer",
        "agent call terminated by customer",
        "customer cut off agent",
        "agent cut off by customer",
    ]):
        return (
            "manual_customer_ended_call_on_agent",
            "Customer Ended Call On Agent",
            "Customer hung up, terminated, cut off, or ended call on agent.",
        )

    # Shouting direction.
    if starts_any(l, [
        "agent shouted at customer",
        "agent shouting at customer",
        "customer shouted at by agent",
        "agent yelled at customer",
    ]):
        return (
            "manual_agent_shouted_at_customer",
            "Agent Shouted At Customer",
            "Agent shouted/yelled at customer.",
        )

    if starts_any(l, [
        "customer shouted at agent",
        "customer shouting at agent",
        "agent shouted at by customer",
        "customer yelled at agent",
    ]):
        return (
            "manual_customer_shouted_at_agent",
            "Customer Shouted At Agent",
            "Customer shouted/yelled at agent.",
        )

    # Agent placed customer on hold / transferred / routed.
    # These are contact-handling, but not IVR if the agent is the actor.
    if starts_any(l, [
        "agent placed customer on hold",
        "agent placed client on hold",
        "agent placed contact on hold",
        "agent transferred customer",
        "agent routed customer",
    ]):
        return (
            "manual_agent_call_handling_action",
            "Agent Call Handling Action",
            "Agent placed, transferred, or routed customer/contact.",
        )
        # Agent role/status misrepresentation.
    if starts_any(l, [
        "agent claims account manager status",
        "agent claimed account manager status",
        "agent claimed account manager",
        "agent claimed manager status",
        "agent claimed to be account manager",
        "agent claimed to be supplier account manager",
        "agent claimed to be customer care",
        "agent claimed to be customer service",
        "agent claimed to be customer",
        "agent claims to be customer",
        "agent claimed to be employee",
        "agent claimed former employee",
        "agent claimed to work for customer company",
        "agent claimed familiarity with owner",
    ]):
        return (
            "manual_agent_role_misrepresentation",
            "Agent Role Misrepresentation",
            "Agent claimed or implied an incorrect role, status, identity, or relationship.",
        )

    # Customer/DM no memory or denial of agent.
    if starts_any(l, [
        "dm claims no memory of agent",
        "customer claims no memory of agent",
        "customer does not remember agent",
        "dm does not remember agent",
    ]):
        return (
            "manual_customer_no_memory_of_agent",
            "Customer No Memory Of Agent",
            "Customer/DM claimed no memory or recognition of the agent.",
        )

    # Duplicate, wrong, personal, or ownership-conflict contact by agent.
    if starts_any(l, [
        "agent called same contact twice",
        "agent called same customer twice",
        "agent called same lead twice",
        "agent called same dm twice",
        "agent called same contact twice in 24h",
        "agent called same lead twice in week",
        "agent called same lead twice same day",
        "agent called same dm twice same day",
        "agent called existing customer unaware",
        "agent called existing client managed by colleague",
        "agent called existing customer assigned to other agent",
        "agent called colleagues client",
        "agent calling existing client from previous firm",
        "agent called personal contact by mistake",
        "agent calling personal contact",
    ]):
        return (
            "manual_agent_duplicate_or_wrong_contact",
            "Agent Duplicate Or Wrong Contact",
            "Agent called duplicate, wrong, personal, existing, assigned, or colleague-managed contact.",
        )

    # Agent inappropriate wording during call.
    if starts_any(l, [
        "agent called customer darling",
        "agent called customer liar",
    ]):
        return (
            "manual_agent_inappropriate_customer_address",
            "Agent Inappropriate Customer Address",
            "Agent used inappropriate wording or address toward customer.",
        )

    # Customer/DM calling agent back.
    if starts_any(l, [
        "dm calling agent back",
        "customer calling agent back",
        "client calling agent back",
        "contact calling agent back",
    ]):
        return (
            "manual_customer_calling_agent_back",
            "Customer Calling Agent Back",
            "Customer/DM/client/contact called or attempted to call agent back.",
        )

    # Audio/hearing issue.
    if starts_any(l, [
        "agent cannot hear customer",
        "agent cannot hear caller",
        "customer cannot hear agent",
        "dm cannot hear agent",
        "caller cannot hear agent",
        "agent unable to hear customer",
        "customer unable to hear agent",
        "dm unable to hear agent",
    ]):
        return (
            "manual_call_audio_hearing_issue",
            "Call Audio Hearing Issue",
            "Call audio/hearing issue between agent and customer/caller/DM.",
        )

    # Multiple or repeated agent contact complaints.
    if starts_any(l, [
        "multiple agent contact complaint",
        "repeated agent contact complaint",
        "customer complained about multiple agent contact",
        "customer complained about repeated agent contact",
    ]):
        return (
            "manual_multiple_agent_contact_complaint",
            "Multiple Agent Contact Complaint",
            "Complaint about multiple or repeated agent contact.",
        )

    # Agent complaint about customer-side staff.
    if starts_any(l, [
        "agent complained about staff rudeness",
        "agent filing complaint against customer staff",
        "agent complained about customer staff",
    ]):
        return (
            "manual_agent_complained_about_customer_staff",
            "Agent Complained About Customer Staff",
            "Agent complaint about customer-side staff behaviour.",
        )

    # Contact/account checking workflow.
    if starts_any(l, [
        "agent checking internal account manager",
        "agent checking unregistered contact",
        "former agent contact check",
        "customer checked agent number online",
    ]):
        return (
            "manual_agent_contact_check_workflow",
            "Agent Contact Check Workflow",
            "Contact, account-manager, registration, or agent-number checking workflow.",
        )

    # Previous agent/contact confirmation.
    if starts_any(l, [
        "customer confirmed previous agent contact",
        "dm confirmed agent number",
        "agent confirmed secondary contact",
        "agent confirmed dm availability",
    ]):
        return (
            "manual_agent_contact_confirmation",
            "Agent Contact Confirmation",
            "Confirmation of previous agent contact, agent number, secondary contact, or DM availability.",
        )

    # Broker/customer identity confusion.
    if starts_any(l, [
        "customer confused by broker identity",
        "customer confused broker identity",
        "customer confused about broker identity",
        "customer confusion over broker identity",
    ]):
        return (
            "manual_customer_confused_by_broker",
            "Customer Confused By Broker",
            "Customer confusion about broker identity.",
        )

    # Agent/contact/customer data confusion.
    if starts_any(l, [
        "agent confused contact details",
        "agent confused customer identity",
        "agent confused contact history",
        "agent confusion contact data",
    ]):
        return (
            "manual_agent_confused_by_customer",
            "Agent Confused By Customer",
            "Agent confusion caused by customer/contact identity, history, or data.",
        )

    # Customer/agent confusion.
    if starts_any(l, [
        "customer confusion regarding agent",
        "customer confused agent identity",
    ]):
        return (
            "manual_customer_confused_by_agent",
            "Customer Confused By Agent",
            "Customer confusion regarding agent identity or agent involvement.",
        )

    # Advice misconduct / criticism.
    if starts_any(l, [
        "agent advised customer to lie",
    ]):
        return (
            "manual_agent_advised_customer_misconduct",
            "Agent Advised Customer Misconduct",
            "Agent advised customer to lie or take improper action.",
        )

    if starts_any(l, [
        "customer advised agent career change",
    ]):
        return (
            "manual_customer_criticized_agent",
            "Customer Criticized Agent",
            "Customer criticized or advised agent negatively.",
        )
        # Customer placed agent on hold / agent placed customer on hold.
    if starts_any(l, [
        "customer placed agent on hold",
        "customer placed agent on hold indefinitely",
        "customer placed agent on permanent hold",
        "customer placed agent on hold and never returned",
    ]):
        return (
            "manual_customer_placed_agent_on_hold",
            "Customer Placed Agent On Hold",
            "Customer placed agent on hold, permanent hold, or did not return.",
        )

    if starts_any(l, [
        "agent placed customer on long hold",
        "agent placed customer on hold",
        "agent placed on hold to call customer",
        "agent placed client on hold",
        "agent placed contact on hold",
        "agent placed dm on hold",
    ]):
        return (
            "manual_agent_call_handling_action",
            "Agent Call Handling Action",
            "Agent placed customer/contact on hold or performed a call-handling action.",
        )

    # Dismissive wording using "of" pattern.
    if starts_any(l, [
        "customer dismissive of agent",
        "customer dismissive of agent knowledge",
        "customer dismissive of agent lack of info",
        "customer dismissive of agent experience",
        "customer dismissive of agent etiquette",
        "customer dismissive of agent probing",
        "customer dismissive of agent records",
        "customer dismissive of unprepared agent",
        "customer dismissive of agent info",
        "customer dismissive of agent claim",
        "customer dismissive of agent authority",
        "customer dismissive of agent attitude",
        "customer dismissive of agent approach",
        "customer dismissive of new agent",
        "customer dismissive of agent voice",
        "dm dismissive of previous agent",
    ]):
        return (
            "manual_customer_dismissive_or_critical_to_agent",
            "Customer Dismissive Or Critical To Agent",
            "Customer/DM was dismissive or critical of agent, previous agent, or agent capability.",
        )

    if starts_any(l, [
        "agent dismissive of customer",
        "agent dismissive of large customer",
        "agent dismissive of customer empathy",
    ]):
        return (
            "manual_agent_dismissive_or_critical_to_customer",
            "Agent Dismissive Or Critical To Customer",
            "Agent was dismissive or critical of customer.",
        )

    # Seek / request / looking-for-agent workflow.
    if starts_any(l, [
        "customer seeking agent",
        "customer seeks agent",
        "customer sought agent",
        "customer looking for agent",
        "customer looking for previous agent",
        "customer looking for specific agent",
        "customer wants agent",
        "customer wanted agent",
        "customer asked for agent",
        "customer requested previous agent",
        "customer requested specific agent",
        "customer requested agent",
        "customer requested agent details",
        "customer requested agent number",
        "customer requested agent change",
        "customer requested agent callback",
        "customer requested callback from agent",
    ]):
        return (
            "manual_customer_requested_agent",
            "Customer Requested Agent",
            "Customer requested, sought, asked for, or looked for an agent.",
        )

    if starts_any(l, [
        "agent seeking customer",
        "agent seeks customer",
        "agent sought customer",
        "agent looking for customer",
        "agent requested customer",
        "agent requested customer details",
        "agent requested customer information",
        "agent requested customer dob",
        "agent asked customer",
    ]):
        return (
            "manual_agent_requested_customer_action",
            "Agent Requested Customer Action",
            "Agent requested, sought, or asked customer for contact/details/action.",
        )

    # Dispute / previous contact dispute.
    if starts_any(l, [
        "agent disputes previous contact",
        "agent disputed previous contact",
        "agent disputes colleague contact",
        "agent disputed colleague contact",
    ]):
        return (
            "manual_agent_disputed_previous_contact",
            "Agent Disputed Previous Contact",
            "Agent disputed previous or colleague contact history.",
        )

    if starts_any(l, [
        "agent disputed caller claim",
        "agent disputes customer claim",
        "agent disputed customer claim",
    ]):
        return (
            "manual_agent_disputed_customer",
            "Agent Disputed Customer",
            "Agent disputed customer/caller claim.",
        )

    if starts_any(l, [
        "customer disputed broker relationship",
    ]):
        return (
            "manual_customer_disputed_broker_relationship",
            "Customer Disputed Broker Relationship",
            "Customer disputed broker relationship or broker association.",
        )

    if starts_any(l, [
        "customer corrected agent narrative",
        "customer corrected agent familiarity",
        "customer corrected agent dates",
        "customer corrected agent terminology",
        "customer corrected agent history",
    ]):
        return (
            "manual_customer_corrected_agent",
            "Customer Corrected Agent",
            "Customer corrected agent narrative, familiarity, dates, terminology, or history.",
        )

    if starts_any(l, [
        "agent corrected customer history",
    ]):
        return (
            "manual_agent_corrected_customer",
            "Agent Corrected Customer",
            "Agent corrected customer history or statement.",
        )

    # Hostility caused by previous/former/unprepared agent.
    if starts_any(l, [
        "customer hostile due to previous agent",
        "customer hostile due to previous agent conduct",
        "customer hostile previous agent",
        "customer hostile previous agent behavior",
        "customer hostile to unprepared agent",
        "customer hostile to former agent",
    ]):
        return (
            "manual_customer_hostile_to_agent",
            "Customer Hostile To Agent",
            "Customer hostility toward agent, previous agent, former agent, or agent conduct.",
        )

    # Ghosting.
    if starts_any(l, [
        "agent ghosting customer",
        "customer ghosted by agent",
        "previous agent ghosted customer",
    ]):
        return (
            "manual_agent_ghosted_customer",
            "Agent Ghosted Customer",
            "Agent or previous agent ghosted customer.",
        )

    if starts_any(l, [
        "customer ghosting agent",
    ]):
        return (
            "manual_customer_ghosted_agent",
            "Customer Ghosted Agent",
            "Customer ghosted agent.",
        )

    # Hung up / call ended because of agent behavior or customer action.
    if starts_any(l, [
        "customer hung up due to agent silence",
        "customer hung up due to agent inattention",
        "customer hung up after criticizing agent",
        "customer hung up on previous agent",
    ]):
        return (
            "manual_customer_ended_call_on_agent",
            "Customer Ended Call On Agent",
            "Customer hung up or ended call involving agent/previous agent.",
        )

    if starts_any(l, [
    "agent terminated call due to shift end",
    "agent terminated call due to scheduling conflict",
    "agent terminated call low usage",
    "agent terminated call during hold",
    "agent terminated call for meeting",
    "agent terminated call client on other line",
    "agent terminated call coughing fit",
    "agent terminated call delivery",
    "agent terminated call on pickup",
    "agent terminated call personal reasons",
    "agent terminated call with dm",
    "agent terminated call after hold",
    "agent terminated call due to system confusion",
    ]):
        return (
            "manual_agent_call_termination",
            "Agent Call Termination",
            "Agent terminated call for operational, scheduling, hold, system, or availability reason.",
        )

    if starts_any(l, [
        "agent hung up on customer",
        "agent ended call on customer",
        "agent cut off customer",
    ]):
        return (
            "manual_agent_ended_call_on_customer",
            "Agent Ended Call On Customer",
            "Agent hung up, cut off, or ended call on customer.",
        )

    # Busy / waiting / unavailable workflow.
    if starts_any(l, [
        "customer busy with other agent",
    ]):
        return (
            "manual_customer_busy_with_other_agent",
            "Customer Busy With Other Agent",
            "Customer was busy with another agent.",
        )

    if starts_any(l, [
        "agent busy with other client",
    ]):
        return (
            "manual_agent_busy_with_other_client",
            "Agent Busy With Other Client",
            "Agent was busy with another client.",
        )

    if starts_any(l, [
        "customer waiting for agent",
        "customer waited for agent",
        "customer waiting on agent",
        "customer awaiting agent",
    ]):
        return (
            "manual_customer_waiting_for_agent",
            "Customer Waiting For Agent",
            "Customer was waiting for agent.",
        )

    if starts_any(l, [
        "agent waiting for customer",
        "agent waited for customer",
        "agent waiting on customer",
        "agent awaiting customer",
        "agent waiting for dm",
        "agent awaiting dm",
    ]):
        return (
            "manual_agent_waiting_for_customer",
            "Agent Waiting For Customer",
            "Agent was waiting for customer/DM.",
        )

    # Unable to reach/contact workflow.
    if starts_any(l, [
        "customer unable to reach agent",
        "customer unable to contact agent",
        "customer cannot reach agent",
        "customer cannot contact agent",
    ]):
        return (
            "manual_customer_unable_to_reach_agent",
            "Customer Unable To Reach Agent",
            "Customer unable/cannot reach or contact agent.",
        )

    if starts_any(l, [
        "agent unable to reach customer",
        "agent unable to contact customer",
        "agent cannot reach customer",
        "agent cannot contact customer",
        "agent unable to reach dm",
        "agent cannot reach dm",
    ]):
        return (
            "manual_agent_unable_to_reach_customer",
            "Agent Unable To Reach Customer",
            "Agent unable/cannot reach customer/DM.",
        )

    # Prank.
    if starts_any(l, [
        "agent prank customer",
        "agent pranked customer",
    ]):
        return (
            "manual_agent_pranked_customer",
            "Agent Pranked Customer",
            "Agent pranked customer.",
        )

    if starts_any(l, [
        "customer pranked agent",
        "customer prank agent",
    ]):
        return (
            "manual_customer_pranked_agent",
            "Customer Pranked Agent",
            "Customer pranked agent.",
        )

    # Feedback / report / complaints manager.
    if starts_any(l, [
        "agent manager feedback",
        "internal manager agent feedback",
    ]):
        return (
            "manual_agent_manager_feedback",
            "Agent Manager Feedback",
            "Manager/internal feedback about agent.",
        )

    if starts_any(l, [
        "agent reached complaints manager",
    ]):
        return (
            "manual_agent_reached_complaints_manager",
            "Agent Reached Complaints Manager",
            "Agent reached or contacted complaints manager.",
        )

    # Previous/internal contact reference.
    if starts_any(l, [
        "agent referenced 2017 contact",
        "agent referenced internal contact",
        "agent referenced previous contact",
        "agent referenced former contact",
    ]):
        return (
            "manual_agent_referenced_previous_contact",
            "Agent Referenced Previous Contact",
            "Agent referenced previous, old, internal, or former contact.",
        )

    if starts_any(l, [
        "customer referenced previous agent",
        "customer referenced agent",
        "customer referenced previous contact",
    ]):
        return (
            "manual_customer_referenced_agent_or_contact",
            "Customer Referenced Agent Or Contact",
            "Customer referenced previous agent/contact.",
        )

    # Mistaken identity / role mismatch.
    if starts_any(l, [
        "agent mistook contact identity",
    ]):
        return (
            "manual_agent_mistook_contact_identity",
            "Agent Mistook Contact Identity",
            "Agent mistook contact identity.",
        )

    if starts_any(l, [
        "customer mistook agent identity",
        "customer not recognizing agent",
        "customer is agent not owner",
    ]):
        return (
            "manual_customer_agent_identity_mismatch",
            "Customer Agent Identity Mismatch",
            "Customer did not recognize agent, mistook agent identity, or was agent not owner.",
        )
    return None


def match_strict38(label):
    l = normalize_label(label)

    if contains_any(l, [
        "sued broker",
        "broker sued",
        "sue broker",
        "broker legal",
        "legal claim",
        "legal dispute broker",
    ]):
        return (
            "manual_broker_legal_dispute",
            "Broker Legal Dispute",
            "Legal dispute, sue, or sued language involving broker.",
        )

    if starts_any(l, [
        "customer refused broker",
        "customer refused brokers",
        "customer refused to speak to broker",
        "customer refused contact with broker",
        "customer refused further broker",
        "dm refused broker",
        "contact refused broker",
    ]):
        return (
            "manual_customer_refused_broker_contact",
            "Customer Refused Broker Contact",
            "Customer/DM/contact refused broker contact.",
        )

    if starts_any(l, [
        "broker refused customer",
        "broker refused client",
        "broker refused owner",
        "broker refused tenant",
        "broker refused landlord",
    ]):
        return (
            "manual_broker_refused_customer",
            "Broker Refused Customer",
            "Broker refused customer/client/owner/tenant/landlord.",
        )

    if starts_any(l, [
        "customer unable to reach broker",
        "customer cannot reach broker",
        "customer unable to contact broker",
        "customer cannot contact broker",
    ]):
        return (
            "manual_customer_unable_to_reach_broker",
            "Customer Unable To Reach Broker",
            "Customer unable/cannot reach or contact broker.",
        )

    if starts_any(l, [
        "broker unable to reach customer",
        "broker cannot reach customer",
        "broker unable to contact customer",
        "broker cannot contact customer",
    ]):
        return (
            "manual_broker_unable_to_reach_customer",
            "Broker Unable To Reach Customer",
            "Broker unable/cannot reach or contact customer.",
        )

    if starts_any(l, [
        "broker verified customer",
        "broker verified client",
        "broker verified owner",
        "broker verified tenant",
        "broker verified landlord",
    ]):
        return (
            "manual_broker_verified_customer",
            "Broker Verified Customer",
            "Broker verified customer/client/owner/tenant/landlord.",
        )

    return None


def match_claimed_previous(label):
    l = normalize_label(label)

    if starts_any(l, [
        "customer claimed previous contact",
        "customer claims previous contact",
        "customer claimed already contacted",
        "customer claimed agent already contacted",
        "customer claimed prior contact",
        "customer claimed previous call",
        "customer claimed previous conversation",
        "contact claimed previous contact",
        "dm claimed previous contact",
        "client claimed previous contact",
    ]):
        return (
            "manual_customer_claimed_previous_contact",
            "Customer Claimed Previous Contact",
            "Customer/contact/DM claimed previous contact or prior interaction.",
        )

    if starts_any(l, [
        "agent claimed previous contact",
        "agent claims previous contact",
        "agent claimed customer previously contacted",
        "agent claimed customer already contacted",
        "agent claimed prior contact",
        "agent claimed previous call",
        "agent claimed previous conversation",
        "agent claimed customer was looking for them",
    ]):
        return (
            "manual_agent_claimed_previous_contact",
            "Agent Claimed Previous Contact",
            "Agent claimed previous contact or prior customer interaction.",
        )

    if contains_any(l, [
        "previous contact claimed",
        "prior contact claimed",
        "previous call claimed",
        "previous conversation claimed",
    ]):
        return (
            "manual_previous_contact_claimed",
            "Previous Contact Claimed",
            "Generic previous-contact claim where actor is unclear.",
        )

    return None


def match_rule(source_cluster_id, label):
    if source_cluster_id == "strict_315":
        return match_strict315(label)

    if source_cluster_id == "strict_38":
        return match_strict38(label)

    if source_cluster_id in {"base_542", "strict_304"}:
        return match_claimed_previous(label)

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
                    "cleanup_decision": "MOVE_TO_BUSINESS_REVIEW_CLUSTER",
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
                "review_decision": "LEFT_FOR_MANUAL_REVIEW_OR_NO_FIX",
                "reason": (
                    "No approved business-review cleanup rule matched. "
                    "Treat as workflow/status/parser/business-semantics unless manually reopened."
                ),
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
                name_row["naming_method"] = "manual_business_review_cleanup"
            if "naming_reason" in n_cols:
                name_row["naming_reason"] = "Created during business-review cleanup of remaining actor-role audit findings."
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
            params.append("manual_business_review_cleanup")

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


def write_outputs(plan_df, review_df, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_path = out_dir / "01_business_review_cleanup_plan.csv"
    review_path = out_dir / "02_business_review_no_move_or_manual_review.csv"
    summary_path = out_dir / "03_business_review_cleanup_summary.csv"

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
    print(f"No-move/manual-review file written: {review_path}")
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
        raise SystemExit("No source rows found.")

    plan_df, review_df = build_plan(df)
    write_outputs(plan_df, review_df, out_dir)

    print("")
    print("Business-review remaining cleanup dry-run")
    print(f"Source rows scanned: {len(df):,}")
    print(f"Rows selected to move: {len(plan_df):,}")
    print(f"Rows left no-move/manual-review: {len(review_df):,}")
    print(f"Occurrences selected to move: {int(plan_df['value_count'].sum()) if not plan_df.empty else 0:,}")

    if not args.apply:
        print("")
        print("DRY RUN ONLY. No DB changes were made.")
        print("Open 01_business_review_cleanup_plan.csv and 03_business_review_cleanup_summary.csv before applying.")
        return

    if plan_df.empty:
        print("No rows selected. Nothing to apply.")
        return

    plan_rows = plan_df.to_dict("records")

    backup_label_map = f"backup_business_review_label_map_{BACKUP_SUFFIX}"
    backup_clusters = f"backup_business_review_clusters_{BACKUP_SUFFIX}"
    backup_names = f"backup_business_review_cluster_names_{BACKUP_SUFFIX}"

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
            print("Business-review cleanup committed successfully.")
            print(f"Total label-map rows moved: {moved_total}")

        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    main()