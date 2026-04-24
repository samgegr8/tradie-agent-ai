#!/usr/bin/env python3
"""
Job seed / management CLI for developers.

Usage:
  python scripts/manage_jobs.py seed   [--env dev] [--tradie-code CODE]
  python scripts/manage_jobs.py list   [--env dev] [--tradie-code CODE] [--status STATUS] [--date YYYY-MM-DD]
  python scripts/manage_jobs.py show   [--env dev] <job_id>
  python scripts/manage_jobs.py add    [--env dev]
  python scripts/manage_jobs.py update [--env dev] <job_id>
  python scripts/manage_jobs.py status [--env dev] <job_id> <PENDING_ASSIGNMENT|ASSIGNED|COMPLETED>
  python scripts/manage_jobs.py appt   [--env dev] <job_id> <YYYY-MM-DD> [<time_slot>]

Examples:
  python scripts/manage_jobs.py seed --env dev
  python scripts/manage_jobs.py seed --env dev --tradie-code 100001
  python scripts/manage_jobs.py list --env dev --tradie-code 100001
  python scripts/manage_jobs.py list --env dev --date 2026-04-25 --status ASSIGNED
  python scripts/manage_jobs.py show --env dev JOB-20260424-A3F2B1
  python scripts/manage_jobs.py add  --env dev
  python scripts/manage_jobs.py update --env dev JOB-20260424-A3F2B1
  python scripts/manage_jobs.py status --env dev JOB-20260424-A3F2B1 COMPLETED
  python scripts/manage_jobs.py appt   --env dev JOB-20260424-A3F2B1 2026-04-25 morning
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone, timedelta

import boto3
from boto3.dynamodb.conditions import Attr

REGION   = "ap-southeast-2"
STATUSES = ["PENDING_ASSIGNMENT", "ASSIGNED", "COMPLETED"]

# Seed job templates — one per seed tradie from manage_tradies.py
# appointment_date offsets are relative to today (0=today, 1=tomorrow, etc.)
SEED_JOBS = [
    {
        "tradie_code":    "100001",  # Dave Nguyen — plumber
        "appt_offset":    0,
        "appointment_time": "morning",
        "urgency":        "STANDARD",
        "customer_name":  "Alice Brennan",
        "callback":       "0412000101",
        "address":        "14 Crown St, Surry Hills, NSW 2010",
        "service_type":   "plumber",
        "description":    "Leaking tap under kitchen sink",
    },
    {
        "tradie_code":    "100001",  # Dave Nguyen — second job tomorrow
        "appt_offset":    1,
        "appointment_time": "afternoon",
        "urgency":        "EMERGENCY",
        "customer_name":  "Ben Hartley",
        "callback":       "0412000102",
        "address":        "8 Fitzroy St, Newtown, NSW 2042",
        "service_type":   "plumber",
        "description":    "No hot water — burst pipe suspected",
    },
    {
        "tradie_code":    "100002",  # Sarah Kim — electrician
        "appt_offset":    0,
        "appointment_time": "9am",
        "urgency":        "STANDARD",
        "customer_name":  "Carol Davis",
        "callback":       "0412000103",
        "address":        "22 Church St, Parramatta, NSW 2150",
        "service_type":   "electrician",
        "description":    "Power points in kitchen not working",
    },
    {
        "tradie_code":    "100002",  # Sarah Kim — tomorrow
        "appt_offset":    1,
        "appointment_time": "after 2pm",
        "urgency":        "STANDARD",
        "customer_name":  "Dan Okafor",
        "callback":       "0412000104",
        "address":        "5 Macquarie Rd, Westmead, NSW 2145",
        "service_type":   "electrician",
        "description":    "Install new ceiling fan in living room",
    },
    {
        "tradie_code":    "100003",  # Tom Walsh — carpenter
        "appt_offset":    1,
        "appointment_time": "morning",
        "urgency":        "STANDARD",
        "customer_name":  "Emma Liu",
        "callback":       "0412000105",
        "address":        "3 Whistler St, Manly, NSW 2095",
        "service_type":   "carpenter",
        "description":    "Back deck boards need replacing, several rotten",
    },
    {
        "tradie_code":    "100004",  # Maria Santos — plumber
        "appt_offset":    2,
        "appointment_time": "morning",
        "urgency":        "STANDARD",
        "customer_name":  "Frank Russo",
        "callback":       "0412000106",
        "address":        "11 Marion St, Auburn, NSW 2144",
        "service_type":   "plumber",
        "description":    "Blocked drain in bathroom",
    },
    {
        "tradie_code":    "100006",  # Priya Patel — painter
        "appt_offset":    2,
        "appointment_time": "8am",
        "urgency":        "STANDARD",
        "customer_name":  "Grace Wong",
        "callback":       "0412000107",
        "address":        "90 Pacific Hwy, Chatswood, NSW 2067",
        "service_type":   "painter",
        "description":    "Full interior repaint — 3 bedroom unit",
    },
    {
        "tradie_code":    "100007",  # Chris O'Brien — tiler
        "appt_offset":    0,
        "appointment_time": "afternoon",
        "urgency":        "STANDARD",
        "customer_name":  "Harry Ngo",
        "callback":       "0412000108",
        "address":        "25 Hall St, Bondi, NSW 2026",
        "service_type":   "tiler",
        "description":    "Bathroom floor tiles cracked, needs re-tiling",
    },
]


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

def _jobs_table(env: str):
    return boto3.resource("dynamodb", region_name=REGION).Table(f"JobsTable-{env}")

def _tradies_table(env: str):
    return boto3.resource("dynamodb", region_name=REGION).Table(f"TradiesTable-{env}")

def _resolve_tradie(env: str, tradie_code: str) -> dict:
    tbl    = _tradies_table(env)
    result = tbl.scan(FilterExpression=Attr("tradie_code").eq(tradie_code))
    items  = result.get("Items", [])
    if not items:
        sys.exit(f"No tradie found with code {tradie_code} in TradiesTable-{env}.")
    return items[0]

def _get_job(env: str, job_id: str) -> dict:
    tbl  = _jobs_table(env)
    resp = tbl.get_item(Key={"job_id": job_id})
    item = resp.get("Item")
    if not item:
        sys.exit(f"Job {job_id} not found in JobsTable-{env}.")
    return item

def _make_job_id() -> str:
    return f"JOB-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

def _today_plus(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()

def _extract_suburb(address: str) -> str:
    parts = [p.strip() for p in address.split(",")]
    for part in reversed(parts[:-1]):
        if any(s in part.upper() for s in ["NSW","VIC","QLD","WA","SA","TAS","ACT","NT"]):
            continue
        if part and not part.isdigit():
            return part
    return parts[0] if parts else ""


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_seed(env: str, tradie_code: str = None):
    jobs_tbl   = _jobs_table(env)
    tradies_tbl = _tradies_table(env)

    # Build code → tradie map once
    all_tradies = {t["tradie_code"]: t for t in tradies_tbl.scan().get("Items", [])}

    templates = [j for j in SEED_JOBS if tradie_code is None or j["tradie_code"] == tradie_code]
    if not templates:
        sys.exit(f"No seed jobs defined for tradie code {tradie_code}.")

    created = 0
    for tmpl in templates:
        code   = tmpl["tradie_code"]
        tradie = all_tradies.get(code)
        if not tradie:
            print(f"  SKIP  code={code} — not in TradiesTable-{env} (run manage_tradies.py seed first)")
            continue

        job_id     = _make_job_id()
        created_at = datetime.now(timezone.utc).isoformat()
        appt_date  = _today_plus(tmpl["appt_offset"])
        suburb     = _extract_suburb(tmpl["address"])

        item = {
            "job_id":     job_id,
            "created_at": created_at,
            "status":     "ASSIGNED",
            "urgency":    tmpl["urgency"],
            "customer": {
                "name":            tmpl["customer_name"],
                "callback_number": tmpl["callback"],
                "address":         tmpl["address"],
                "suburb":          suburb,
            },
            "job": {
                "service_type":        tmpl["service_type"],
                "problem_description": tmpl["description"],
                "appointment_date":    appt_date,
                "appointment_time":    tmpl["appointment_time"],
            },
            "tradie": {
                "tradie_id":     tradie["phone_number"],
                "name":          tradie["name"],
                "phone":         tradie["phone_number"],
                "business_name": tradie.get("business_name", ""),
                "assigned_at":   created_at,
            },
            "notification": {
                "status":   "PENDING",
                "provider": "twilio",
                "sent_at":  None,
            },
        }

        jobs_tbl.put_item(Item=item)
        print(f"  Created  {job_id}  tradie={tradie['name']} ({code})  appt={appt_date} {tmpl['appointment_time']}  customer={tmpl['customer_name']}")
        created += 1

    print(f"\nSeeded {created} job(s) into JobsTable-{env}.")


def cmd_list(env: str, tradie_code: str = None, status: str = None, date: str = None):
    tbl  = _jobs_table(env)
    expr = None

    if tradie_code:
        tradie = _resolve_tradie(env, tradie_code)
        phone  = tradie["phone_number"]
        expr   = _and(expr, Attr("tradie.phone").eq(phone))
    if status:
        expr = _and(expr, Attr("status").eq(status.upper()))
    if date:
        expr = _and(expr, Attr("job.appointment_date").eq(date))

    kwargs = {"FilterExpression": expr} if expr else {}
    items  = tbl.scan(**kwargs).get("Items", [])
    items.sort(key=lambda j: (j.get("job", {}).get("appointment_date", ""), j.get("created_at", "")))

    if not items:
        print("No jobs found.")
        return

    hdr = "{:<26} {:<20} {:<10} {:<12} {:<12} {:<22} {}"
    print(hdr.format("JOB ID", "TRADIE", "STATUS", "APPT DATE", "APPT TIME", "CUSTOMER", "URGENCY"))
    print("-" * 115)
    for j in items:
        tradie_name = (j.get("tradie") or {}).get("name", "—")
        customer    = (j.get("customer") or {}).get("name", "—")
        job_info    = j.get("job") or {}
        print(hdr.format(
            j["job_id"],
            tradie_name[:19],
            j.get("status", "—"),
            job_info.get("appointment_date", "—"),
            job_info.get("appointment_time", "—")[:11],
            customer[:21],
            j.get("urgency", "—"),
        ))
    print(f"\n{len(items)} job(s).")


def cmd_show(env: str, job_id: str):
    job = _get_job(env, job_id)
    print(json.dumps(job, indent=2, default=str))


def cmd_add(env: str):
    print("Add new job (Ctrl-C to cancel)\n")
    tradies_tbl = _tradies_table(env)
    jobs_tbl    = _jobs_table(env)

    # Tradie lookup
    tradie_code = _prompt("Tradie code (6-digit)")
    result      = tradies_tbl.scan(FilterExpression=Attr("tradie_code").eq(tradie_code))
    tradies     = result.get("Items", [])
    if not tradies:
        sys.exit(f"No tradie with code {tradie_code}.")
    tradie = tradies[0]
    print(f"  → Tradie: {tradie['name']} ({tradie['trade_type']})")

    customer_name = _prompt("Customer name")
    callback      = _prompt("Customer callback number")
    address       = _prompt("Full address (street, suburb, state postcode)")
    service_type  = _prompt(f"Service type [{tradie['trade_type']}]") or tradie["trade_type"]
    description   = _prompt("Problem description")
    appt_date     = _prompt(f"Appointment date YYYY-MM-DD [today={_today_plus(0)}]") or _today_plus(0)
    appt_time     = _prompt("Appointment time [morning]") or "morning"
    urgency       = (_prompt("Urgency (standard/emergency) [standard]") or "standard").upper()

    _validate_date(appt_date)

    job_id     = _make_job_id()
    created_at = datetime.now(timezone.utc).isoformat()
    suburb     = _extract_suburb(address)

    item = {
        "job_id":     job_id,
        "created_at": created_at,
        "status":     "ASSIGNED",
        "urgency":    urgency,
        "customer": {
            "name":            customer_name,
            "callback_number": callback,
            "address":         address,
            "suburb":          suburb,
        },
        "job": {
            "service_type":        service_type.lower(),
            "problem_description": description,
            "appointment_date":    appt_date,
            "appointment_time":    appt_time,
        },
        "tradie": {
            "tradie_id":     tradie["phone_number"],
            "name":          tradie["name"],
            "phone":         tradie["phone_number"],
            "business_name": tradie.get("business_name", ""),
            "assigned_at":   created_at,
        },
        "notification": {
            "status":   "PENDING",
            "provider": "twilio",
            "sent_at":  None,
        },
    }

    jobs_tbl.put_item(Item=item)
    print(f"\nCreated {job_id}")
    print(f"  Tradie:      {tradie['name']}")
    print(f"  Customer:    {customer_name}")
    print(f"  Appointment: {appt_date} {appt_time}")
    print(f"  Urgency:     {urgency}")


def cmd_update(env: str, job_id: str):
    jobs_tbl = _jobs_table(env)
    job      = _get_job(env, job_id)
    job_info = job.get("job") or {}
    customer = job.get("customer") or {}

    print(f"Updating {job_id} — press Enter to keep current value.\n")

    updates      = {}
    nested_job   = {}
    nested_cust  = {}

    # Appointment
    cur_date = job_info.get("appointment_date", "")
    val = input(f"  Appointment date [{cur_date}]: ").strip()
    if val:
        _validate_date(val)
        nested_job["appointment_date"] = val

    cur_time = job_info.get("appointment_time", "")
    val = input(f"  Appointment time [{cur_time}]: ").strip()
    if val:
        nested_job["appointment_time"] = val

    # Job details
    cur_desc = job_info.get("problem_description", "")
    val = input(f"  Problem description [{cur_desc}]: ").strip()
    if val:
        nested_job["problem_description"] = val

    cur_svc = job_info.get("service_type", "")
    val = input(f"  Service type [{cur_svc}]: ").strip()
    if val:
        nested_job["service_type"] = val.lower()

    # Urgency (top-level)
    cur_urg = job.get("urgency", "")
    val = input(f"  Urgency [{cur_urg}]: ").strip()
    if val:
        updates["urgency"] = val.upper()

    # Customer details
    cur_cname = customer.get("name", "")
    val = input(f"  Customer name [{cur_cname}]: ").strip()
    if val:
        nested_cust["name"] = val

    cur_cb = customer.get("callback_number", "")
    val = input(f"  Callback number [{cur_cb}]: ").strip()
    if val:
        nested_cust["callback_number"] = val

    cur_addr = customer.get("address", "")
    val = input(f"  Address [{cur_addr}]: ").strip()
    if val:
        nested_cust["address"] = val
        nested_cust["suburb"]  = _extract_suburb(val)

    # Build update expression from collected changes
    if nested_job:
        merged = {**job_info, **nested_job}
        updates["job"] = merged
    if nested_cust:
        merged = {**customer, **nested_cust}
        updates["customer"] = merged

    if not updates:
        print("No changes.")
        return

    expr   = "SET " + ", ".join(f"#{k} = :{k}" for k in updates)
    names  = {f"#{k}": k for k in updates}
    values = {f":{k}": v for k, v in updates.items()}
    jobs_tbl.update_item(
        Key={"job_id": job_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    print(f"\nUpdated {job_id}.")


def cmd_status(env: str, job_id: str, new_status: str):
    new_status = new_status.upper()
    if new_status not in STATUSES:
        sys.exit(f"Invalid status '{new_status}'. Choose from: {', '.join(STATUSES)}")

    jobs_tbl = _jobs_table(env)
    job      = _get_job(env, job_id)
    old      = job.get("status", "—")

    extra_expr   = ""
    extra_values = {}
    if new_status == "COMPLETED":
        extra_expr              = ", completed_at = :completed_at"
        extra_values[":completed_at"] = datetime.now(timezone.utc).isoformat()

    jobs_tbl.update_item(
        Key={"job_id": job_id},
        UpdateExpression=f"SET #status = :status{extra_expr}",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": new_status, **extra_values},
    )
    print(f"{job_id}  {old} → {new_status}")


def cmd_appt(env: str, job_id: str, appt_date: str, appt_time: str = None):
    _validate_date(appt_date)
    jobs_tbl = _jobs_table(env)
    job      = _get_job(env, job_id)
    job_info = dict(job.get("job") or {})

    job_info["appointment_date"] = appt_date
    if appt_time:
        job_info["appointment_time"] = appt_time

    jobs_tbl.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #job = :job",
        ExpressionAttributeNames={"#job": "job"},
        ExpressionAttributeValues={":job": job_info},
    )
    time_str = f" {appt_time}" if appt_time else ""
    print(f"{job_id}  appointment → {appt_date}{time_str}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _and(expr, new):
    return new if expr is None else expr & new

def _prompt(label: str) -> str:
    return input(f"  {label}: ").strip()

def _validate_date(val: str):
    try:
        datetime.strptime(val, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"Invalid date '{val}' — must be YYYY-MM-DD.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Manage JobsTable entries")
    sub    = parser.add_subparsers(dest="command", required=True)

    # seed
    p = sub.add_parser("seed", help="Seed sample jobs for dev testing")
    p.add_argument("--env",         default="dev", choices=["dev","staging","prod"])
    p.add_argument("--tradie-code", help="Seed only for this tradie code")

    # list
    p = sub.add_parser("list", help="List jobs with optional filters")
    p.add_argument("--env",         default="dev", choices=["dev","staging","prod"])
    p.add_argument("--tradie-code", help="Filter by tradie code")
    p.add_argument("--status",      help="Filter by status (ASSIGNED, PENDING_ASSIGNMENT, COMPLETED)")
    p.add_argument("--date",        help="Filter by appointment date YYYY-MM-DD")

    # show
    p = sub.add_parser("show", help="Show full job details as JSON")
    p.add_argument("job_id")
    p.add_argument("--env", default="dev", choices=["dev","staging","prod"])

    # add
    p = sub.add_parser("add", help="Interactively create a job for a tradie")
    p.add_argument("--env", default="dev", choices=["dev","staging","prod"])

    # update
    p = sub.add_parser("update", help="Interactively update a job")
    p.add_argument("job_id")
    p.add_argument("--env", default="dev", choices=["dev","staging","prod"])

    # status
    p = sub.add_parser("status", help="Change job status")
    p.add_argument("job_id")
    p.add_argument("new_status", choices=STATUSES, metavar="STATUS",
                   help="PENDING_ASSIGNMENT | ASSIGNED | COMPLETED")
    p.add_argument("--env", default="dev", choices=["dev","staging","prod"])

    # appt
    p = sub.add_parser("appt", help="Change appointment date/time")
    p.add_argument("job_id")
    p.add_argument("date",      help="New appointment date YYYY-MM-DD")
    p.add_argument("time_slot", nargs="?", help="New time slot e.g. morning, after 2pm")
    p.add_argument("--env", default="dev", choices=["dev","staging","prod"])

    args = parser.parse_args()

    if args.command == "seed":
        cmd_seed(args.env, args.tradie_code)
    elif args.command == "list":
        cmd_list(args.env, args.tradie_code, args.status, args.date)
    elif args.command == "show":
        cmd_show(args.env, args.job_id)
    elif args.command == "add":
        cmd_add(args.env)
    elif args.command == "update":
        cmd_update(args.env, args.job_id)
    elif args.command == "status":
        cmd_status(args.env, args.job_id, args.new_status)
    elif args.command == "appt":
        cmd_appt(args.env, args.job_id, args.date, args.time_slot)


if __name__ == "__main__":
    main()
