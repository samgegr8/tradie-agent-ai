"""
Tradie Connect — Portal API Lambda
------------------------------------
HTTP API backing the tradie self-service portal.

Routes (HTTP API v2 payload format):
  POST /login   body: {"code": "482719"}
                → looks up tradie by tradie_code, returns profile or 404
  GET  /jobs    ?phone=+61411000001
                → returns jobs assigned to that tradie (phone from login response)

Auth: tradie_code lookup (6-digit numeric code assigned per tradie).
Upgrade path: replace with OTP when Twilio is integrated.
"""

import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb      = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-southeast-2"))
TRADIES_TABLE = os.environ.get("TRADIES_TABLE", "TradiesTable-dev")
JOBS_TABLE    = os.environ.get("JOBS_TABLE",    "JobsTable-dev")


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    logger.info("Portal route: %s", route)

    if route == "POST /login":
        return _handle_login(event)
    if route == "GET /jobs":
        return _handle_jobs(event)
    return _respond(404, {"error": "Not found"})


# ── POST /login ────────────────────────────────────────────────────────────────

def _handle_login(event):
    body = json.loads(event.get("body") or "{}")
    code = (body.get("code") or "").strip()

    if not code:
        return _respond(400, {"error": "code is required"})

    if not code.isdigit() or len(code) != 6:
        return _respond(400, {"error": "Tradie code must be 6 digits."})

    table  = dynamodb.Table(TRADIES_TABLE)
    result = table.scan(FilterExpression=Attr("tradie_code").eq(code))
    items  = result.get("Items", [])

    if not items:
        return _respond(404, {"error": "Code not recognised. Please check your tradie code."})

    tradie = items[0]
    logger.info("Portal login: %s (%s)", tradie["name"], tradie["phone_number"])

    return _respond(200, {
        "tradie_code":   tradie["tradie_code"],
        "name":          tradie["name"],
        "business_name": tradie.get("business_name", ""),
        "trade_type":    tradie.get("trade_type", ""),
        "location":      tradie.get("location", ""),
        "active":        tradie.get("active", False),
    })


# ── GET /jobs ──────────────────────────────────────────────────────────────────

def _handle_jobs(event):
    params      = event.get("queryStringParameters") or {}
    tradie_code = (params.get("tradie_code") or "").strip()

    if not tradie_code:
        return _respond(400, {"error": "tradie_code query param is required"})

    # Resolve phone number from tradie_code (phone is the jobs table FK)
    tradies_table = dynamodb.Table(TRADIES_TABLE)
    tradie_result = tradies_table.scan(FilterExpression=Attr("tradie_code").eq(tradie_code))
    tradies       = tradie_result.get("Items", [])
    if not tradies:
        return _respond(404, {"error": "Tradie not found."})
    phone = tradies[0]["phone_number"]

    table  = dynamodb.Table(JOBS_TABLE)
    result = table.scan(FilterExpression=Attr("tradie.phone").eq(phone))
    jobs   = result.get("Items", [])

    # Newest first
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)

    logger.info("Portal jobs: %d jobs for tradie_code=%s", len(jobs), tradie_code)

    return _respond(200, {"jobs": jobs})


# ── helpers ────────────────────────────────────────────────────────────────────

def _respond(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body, default=str),
    }
