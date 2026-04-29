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
TRIPS_TABLE   = os.environ.get("TRIPS_TABLE",   "TripsTable-dev")


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
    ) -> dict:
        """
        Creates a new job card in DynamoDB.
        problemDescription is pipe-delimited: "description|YYYY-MM-DD|time slot|urgency"
        Returns job_id and confirmation details.
        """
        job_id     = f"JOB-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        created_at = datetime.now(timezone.utc).isoformat()

        suburb = _extract_suburb(address)

        # Parse pipe-delimited jobDetails: description|date|time|urgency
        parts            = [p.strip() for p in problem_description.split("|")]
        clean_desc       = parts[0] if len(parts) > 0 else problem_description
        appointment_date = parts[1] if len(parts) > 1 else ""
        appointment_time = parts[2] if len(parts) > 2 else ""
        urgency          = parts[3].lower() if len(parts) > 3 else "standard"

        item = {
            "job_id":     job_id,
            "created_at": created_at,
            "status":     "PENDING_ASSIGNMENT",
            "urgency":    urgency.upper() if urgency else "STANDARD",

            "customer": {
                "name":            customer_name,
                "callback_number": callback_number,
                "address":         address,
                "suburb":          suburb,
            },

            "job": {
                "service_type":        service_type.lower(),
                "problem_description": clean_desc,
                "appointment_date":    appointment_date,
                "appointment_time":    appointment_time,
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

    # ── Tool 1b: lookup_tradie_by_code ────────────────────────────────────────
    def lookup_tradie_by_code(self, tradie_code: str) -> dict:
        """
        Authenticates a tradie by their tradie_code.
        Returns phone_number and profile — phone_number must be used for all
        subsequent getJobsByTradie and completeJob calls.
        """
        table  = dynamodb.Table(TRADIES_TABLE)
        result = table.scan(FilterExpression=Attr("tradie_code").eq(tradie_code.strip()))
        items  = result.get("Items", [])

        if not items:
            logger.warning("lookup_tradie_by_code: code %s not found", tradie_code)
            return {
                "found":   False,
                "message": "That code wasn't recognised. Please check your tradie code and try again.",
            }

        tradie = items[0]
        logger.info("lookup_tradie_by_code: matched %s (%s)", tradie["name"], tradie["phone_number"])

        return {
            "found":         True,
            "phone_number":  tradie["phone_number"],
            "name":          tradie["name"],
            "business_name": tradie.get("business_name", ""),
            "trade_type":    tradie.get("trade_type", ""),
            "active":        tradie.get("active", False),
            "message":       f"Welcome, {tradie['name']}.",
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

    # ── Tool 5: get_jobs_by_tradie ────────────────────────────────────────────
    def get_jobs_by_tradie(self, tradie_phone: str, date_filter: str = "today") -> dict:
        """
        Returns jobs assigned to a tradie on a given date.
        date_filter: "today", "tomorrow", or ISO date string "YYYY-MM-DD".
        Filters by created_at date prefix and excludes COMPLETED jobs.
        """
        from datetime import timedelta

        today  = datetime.now(timezone.utc).date()
        phrase = date_filter.strip().lower()

        WEEKDAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

        if phrase in ("today", "this day"):
            target_date = today
        elif phrase == "tomorrow":
            target_date = today + timedelta(days=1)
        elif phrase in ("day after tomorrow", "the day after tomorrow", "overmorrow"):
            target_date = today + timedelta(days=2)
        elif phrase in WEEKDAYS:
            # next occurrence of the named weekday (could be today if it matches)
            target_dow  = WEEKDAYS.index(phrase)
            days_ahead  = (target_dow - today.weekday()) % 7 or 7
            target_date = today + timedelta(days=days_ahead)
        else:
            try:
                target_date = datetime.strptime(date_filter[:10], "%Y-%m-%d").date()
            except ValueError:
                logger.warning("get_jobs_by_tradie: unrecognised date_filter '%s', defaulting to today", date_filter)
                target_date = today

        date_str = target_date.isoformat()  # "YYYY-MM-DD"

        table  = dynamodb.Table(JOBS_TABLE)
        result = table.scan(
            FilterExpression=(
                Attr("tradie.phone").eq(tradie_phone)
                & Attr("job.appointment_date").eq(date_str)
                & Attr("status").ne("COMPLETED")
            )
        )

        jobs = sorted(result.get("Items", []), key=lambda j: j.get("created_at", ""))

        logger.info("get_jobs_by_tradie: %d jobs for %s on %s", len(jobs), tradie_phone, date_str)

        if not jobs:
            return {
                "jobs_found": False,
                "date":       date_str,
                "count":      0,
                "jobs":       [],
                "message":    f"No active jobs found for {date_str}.",
            }

        summaries = [
            {
                "job_id":              j["job_id"],
                "status":              j.get("status", ""),
                "urgency":             j.get("urgency", ""),
                "customer_name":       (j.get("customer") or {}).get("name", ""),
                "address":             (j.get("customer") or {}).get("address", ""),
                "callback_number":     (j.get("customer") or {}).get("callback_number", ""),
                "problem_description": (j.get("job") or {}).get("problem_description", ""),
                "appointment_date":    (j.get("job") or {}).get("appointment_date", ""),
                "appointment_time":    (j.get("job") or {}).get("appointment_time", ""),
            }
            for j in jobs
        ]

        return {
            "jobs_found": True,
            "date":       date_str,
            "count":      len(jobs),
            "jobs":       summaries,
            "message":    f"Found {len(jobs)} active job(s) for {date_str}.",
        }

    # ── Tool 6: complete_job ──────────────────────────────────────────────────
    def complete_job(self, job_id: str, tradie_phone: str) -> dict:
        """
        Marks a job as COMPLETED. Verifies that tradie_phone matches the assigned
        tradie on the job — prevents one tradie completing another's job.
        """
        table  = dynamodb.Table(JOBS_TABLE)
        result = table.get_item(Key={"job_id": job_id})
        job    = result.get("Item")

        if not job:
            return {"success": False, "message": f"Job {job_id} not found."}

        assigned_phone = (job.get("tradie") or {}).get("phone", "")
        if assigned_phone != tradie_phone:
            logger.warning(
                "complete_job: tradie %s tried to complete job %s assigned to %s",
                tradie_phone, job_id, assigned_phone,
            )
            return {"success": False, "message": f"Job {job_id} is not assigned to your number."}

        if job.get("status") == "COMPLETED":
            return {"success": False, "message": f"Job {job_id} is already marked as completed."}

        completed_at = datetime.now(timezone.utc).isoformat()

        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #status = :status, completed_at = :completed_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status":       "COMPLETED",
                ":completed_at": completed_at,
            },
        )

        logger.info("Job %s marked COMPLETED by tradie %s", job_id, tradie_phone)

        return {
            "success":        True,
            "job_id":         job_id,
            "completed_at":   completed_at,
            "customer_name":  (job.get("customer") or {}).get("name", ""),
            "message":        f"Job {job_id} has been marked as completed.",
        }

    # ── Tool 7: start_trip ────────────────────────────────────────────────────
    def start_trip(
        self,
        tradie_phone: str,
        trip_type: str,
        destination: str,
        related_job_id: str = "",
    ) -> dict:
        """
        Records the start of a trip in TripsTable.
        trip_type must be one of: job, supplier, home, other.
        Returns trip_id — pass this to end_trip when the trip is done.
        """
        from datetime import timedelta

        valid_types = {"job", "supplier", "home", "other"}
        clean_type  = trip_type.lower().strip()
        if clean_type not in valid_types:
            clean_type = "other"

        trip_id    = f"TRIP-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        started_at = datetime.now(timezone.utc).isoformat()
        expires_at = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())

        item = {
            "trip_id":      trip_id,
            "tradie_phone": tradie_phone,
            "trip_type":    clean_type,
            "destination":  destination.strip(),
            "started_at":   started_at,
            "ended_at":     None,
            "status":       "IN_PROGRESS",
            "expires_at":   expires_at,
        }

        if related_job_id and related_job_id.strip():
            item["related_job_id"] = related_job_id.strip()

        dynamodb.Table(TRIPS_TABLE).put_item(Item=item)

        logger.info("Started trip %s for %s → %s (%s)", trip_id, tradie_phone, destination, clean_type)

        return {
            "trip_id":    trip_id,
            "started_at": started_at,
            "trip_type":  clean_type,
            "destination": destination.strip(),
            "message":    f"Trip to {destination.strip()} started. Reference: {trip_id}",
        }

    # ── Tool 8: end_trip ──────────────────────────────────────────────────────
    def end_trip(self, trip_id: str, tradie_phone: str) -> dict:
        """
        Marks a trip COMPLETED. Verifies tradie_phone matches the trip owner
        to prevent one tradie ending another's trip.
        """
        table  = dynamodb.Table(TRIPS_TABLE)
        result = table.get_item(Key={"trip_id": trip_id})
        trip   = result.get("Item")

        if not trip:
            return {"success": False, "message": f"Trip {trip_id} not found."}

        if trip.get("tradie_phone") != tradie_phone:
            logger.warning(
                "end_trip: tradie %s tried to end trip %s owned by %s",
                tradie_phone, trip_id, trip.get("tradie_phone"),
            )
            return {"success": False, "message": f"Trip {trip_id} is not registered to your number."}

        if trip.get("status") == "COMPLETED":
            return {"success": False, "message": f"Trip {trip_id} is already completed."}

        ended_at = datetime.now(timezone.utc).isoformat()

        table.update_item(
            Key={"trip_id": trip_id},
            UpdateExpression="SET #status = :status, ended_at = :ended_at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status":   "COMPLETED",
                ":ended_at": ended_at,
            },
        )

        logger.info("Trip %s ended by tradie %s", trip_id, tradie_phone)

        return {
            "success":     True,
            "trip_id":     trip_id,
            "destination": trip.get("destination", ""),
            "started_at":  trip.get("started_at", ""),
            "ended_at":    ended_at,
            "message":     f"Trip to {trip.get('destination', '')} logged as complete.",
        }

    # ── Tool 9: get_trip_log ──────────────────────────────────────────────────
    def get_trip_log(self, tradie_phone: str, date_filter: str = "today") -> dict:
        """
        Returns trips for a tradie on a given date.
        date_filter: "today", "yesterday", or ISO date "YYYY-MM-DD".
        Filters by started_at date prefix.
        """
        from datetime import timedelta

        today  = datetime.now(timezone.utc).date()
        phrase = date_filter.strip().lower()

        if phrase in ("today", "this day"):
            target_date = today
        elif phrase == "yesterday":
            target_date = today - timedelta(days=1)
        elif phrase == "tomorrow":
            target_date = today + timedelta(days=1)
        else:
            try:
                target_date = datetime.strptime(date_filter[:10], "%Y-%m-%d").date()
            except ValueError:
                logger.warning("get_trip_log: unrecognised date_filter '%s', defaulting to today", date_filter)
                target_date = today

        date_str = target_date.isoformat()

        table  = dynamodb.Table(TRIPS_TABLE)
        result = table.scan(
            FilterExpression=(
                Attr("tradie_phone").eq(tradie_phone)
                & Attr("started_at").begins_with(date_str)
            )
        )

        trips = sorted(result.get("Items", []), key=lambda t: t.get("started_at", ""))

        logger.info("get_trip_log: %d trips for %s on %s", len(trips), tradie_phone, date_str)

        if not trips:
            return {
                "trips_found": False,
                "date":        date_str,
                "count":       0,
                "trips":       [],
                "message":     f"No trips logged for {date_str}.",
            }

        summaries = [
            {
                "trip_id":        t["trip_id"],
                "trip_type":      t.get("trip_type", ""),
                "destination":    t.get("destination", ""),
                "related_job_id": t.get("related_job_id", ""),
                "started_at":     t.get("started_at", ""),
                "ended_at":       t.get("ended_at") or "in progress",
                "status":         t.get("status", ""),
            }
            for t in trips
        ]

        return {
            "trips_found": True,
            "date":        date_str,
            "count":       len(trips),
            "trips":       summaries,
            "message":     f"Found {len(trips)} trip(s) for {date_str}.",
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
