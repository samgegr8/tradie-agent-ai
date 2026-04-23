"""
Tradie Connect — MCP Orchestrator
-----------------------------------
Implements the three active MCP tool actions invoked by the Connect AI Agent.

Tool execution order (enforced by the agent's system prompt):
  1. create_job_card          → writes JobsTable record
  2. lookup_available_tradie  → queries TradiesTable for best match
  3. assign_tradie_to_job     → links tradie to job, sets status=ASSIGNED

Notification (Tool 4) is stubbed — tradie notification via SMS is a future
roadmap item. Twilio is the planned provider. The stub logs the pending
notification to the job card so it can be picked up by a future notifier.
"""

import os
import uuid
import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients ────────────────────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))

JOBS_TABLE    = os.environ.get("JOBS_TABLE",    "JobsTable-dev")
TRADIES_TABLE = os.environ.get("TRADIES_TABLE", "TradiesTable-dev")


class MCPOrchestrator:
    """Executes MCP tool actions. Each method maps 1:1 to an agent action."""

    # ── Tool 1: create_job_card ────────────────────────────────────────────────
    def create_job_card(
        self,
        customer_name: str,
        callback_number: str,
        service_type: str,
        problem_description: str,
        address: str,
        preferred_time: str = "",
        urgency: str = "standard",
    ) -> dict:
        """
        Creates a new job card in DynamoDB.
        Returns job_id and confirmation details.
        """
        job_id     = f"JOB-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        created_at = datetime.now(timezone.utc).isoformat()

        suburb = _extract_suburb(address)

        item = {
            "job_id":     job_id,
            "created_at": created_at,
            "status":     "PENDING_ASSIGNMENT",
            "urgency":    urgency.upper(),

            "customer": {
                "name":            customer_name,
                "callback_number": callback_number,
                "address":         address,
                "suburb":          suburb,
            },

            "job": {
                "service_type":        service_type.lower(),
                "problem_description": problem_description,
                "preferred_time":      preferred_time,
            },

            # Populated by assign_tradie_to_job
            "tradie": None,

            # Populated later by the transcript summariser Lambda
            "summary": None,

            # Populated by assign_tradie_to_job; processed by future Twilio notifier
            "notification": {
                "status":   "PENDING",
                "provider": "twilio",
                "sent_at":  None,
            },
        }

        table = dynamodb.Table(JOBS_TABLE)
        table.put_item(Item=item)

        logger.info("Created job card %s for %s", job_id, customer_name)

        return {
            "job_id":     job_id,
            "created_at": created_at,
            "suburb":     suburb,
            "message":    f"Job card {job_id} created successfully.",
        }

    # ── Tool 2: lookup_available_tradie ────────────────────────────────────────
    def lookup_available_tradie(self, service_type: str, suburb: str = "") -> dict:
        """
        Queries TradiesTable for an active tradie matching the service type.
        Prefers tradies whose location matches the suburb.
        Returns tradie_id, name, and phone.
        """
        table = dynamodb.Table(TRADIES_TABLE)

        # Scan is fine at 50 calls/day — add a GSI on trade_type if volume grows
        response = table.scan(
            FilterExpression=Attr("trade_type").eq(service_type.lower()) & Attr("active").eq(True)
        )

        tradies = response.get("Items", [])

        if not tradies:
            logger.warning("No active tradies found for service_type=%s", service_type)
            return {
                "tradie_id":    "NONE",
                "tradie_found": False,
                "message":      f"No available tradies found for {service_type}. Job logged for manual assignment.",
            }

        # Prefer a tradie whose location mentions the suburb
        suburb_lower = suburb.lower()
        preferred    = next(
            (t for t in tradies if suburb_lower and suburb_lower in t.get("location", "").lower()),
            tradies[0],  # fall back to first available
        )

        logger.info("Matched tradie %s for %s in %s", preferred["phone_number"], service_type, suburb)

        return {
            "tradie_id":     preferred["phone_number"],
            "tradie_name":   preferred["name"],
            "tradie_phone":  preferred["phone_number"],
            "business_name": preferred.get("business_name", ""),
            "message":       f"Found tradie {preferred['name']} for {service_type} in {suburb or 'your area'}.",
        }

    # ── Tool 3: assign_tradie_to_job ──────────────────────────────────────────
    def assign_tradie_to_job(self, job_id: str, tradie_id: str) -> dict:
        """
        Links a tradie to a job card.
        Updates JobsTable with tradie details and status=ASSIGNED.
        Also stamps notification.status=PENDING so the future Twilio
        notifier can poll for unnotified jobs.
        """
        jobs_table    = dynamodb.Table(JOBS_TABLE)
        tradies_table = dynamodb.Table(TRADIES_TABLE)

        tradie_resp = tradies_table.get_item(Key={"phone_number": tradie_id})
        tradie      = tradie_resp.get("Item")

        if not tradie:
            return {"success": False, "message": f"Tradie {tradie_id} not found."}

        assigned_at = datetime.now(timezone.utc).isoformat()

        jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="""
                SET #status      = :status,
                    tradie       = :tradie,
                    assigned_at  = :assigned_at,
                    notification = :notification
            """,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status":      "ASSIGNED",
                ":assigned_at": assigned_at,
                ":tradie": {
                    "tradie_id":     tradie["phone_number"],
                    "name":          tradie["name"],
                    "phone":         tradie["phone_number"],
                    "business_name": tradie.get("business_name", ""),
                    "assigned_at":   assigned_at,
                },
                ":notification": {
                    "status":   "PENDING",
                    "provider": "twilio",
                    "sent_at":  None,
                },
            },
        )

        logger.info(
            "Assigned tradie %s to job %s — notification.status=PENDING (Twilio roadmap)",
            tradie_id, job_id,
        )

        return {
            "success":      True,
            "job_id":       job_id,
            "tradie_name":  tradie["name"],
            "tradie_phone": tradie["phone_number"],
            "message":      f"{tradie['name']} has been assigned to job {job_id}. They will be contacted shortly.",
        }

    # ── Tool 4 (stub): log_job_notification ───────────────────────────────────
    def log_job_notification(self, job_id: str, tradie_phone: str = "N/A") -> dict:
        """
        Stub for future Twilio SMS notification.

        Does nothing except log intent. When Twilio is integrated this method
        will call the Twilio Messages API. Until then, notification.status=PENDING
        on the job card acts as the queue — an EventBridge rule or DynamoDB
        stream trigger can poll for PENDING records and dispatch via Twilio.
        """
        logger.info(
            "Notification stub: job=%s tradie=%s — Twilio integration is roadmap",
            job_id, tradie_phone,
        )
        return {
            "success": True,
            "message": f"Job {job_id} confirmed. Tradie notification queued (Twilio — coming soon).",
        }


# ── Helpers ────────────────────────────────────────────────────────────────────
def _extract_suburb(address: str) -> str:
    """
    Best-effort suburb extraction from a free-text address string.
    Looks for the second-last comma-separated segment before the state/postcode.
    e.g. "12 George St, Surry Hills, NSW 2010" → "Surry Hills"
    """
    parts = [p.strip() for p in address.split(",")]
    # Walk from the end, skip the last segment (state + postcode), take the next
    for part in reversed(parts[:-1]):
        # Skip if it looks like a state/postcode fragment
        if any(state in part.upper() for state in ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"]):
            continue
        if part and not part.isdigit():
            return part
    return parts[0] if parts else ""
