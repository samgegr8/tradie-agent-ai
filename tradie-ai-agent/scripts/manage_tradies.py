#!/usr/bin/env python3
"""
Tradie seed / management CLI
Usage:
  python scripts/manage_tradies.py seed   [--env dev]          # load sample tradies
  python scripts/manage_tradies.py list   [--env dev]          # show all tradies
  python scripts/manage_tradies.py add    [--env dev]          # interactive add
  python scripts/manage_tradies.py update [--env dev] <phone>  # interactive update
  python scripts/manage_tradies.py delete [--env dev] <phone>  # delete a tradie
  python scripts/manage_tradies.py toggle [--env dev] <phone>  # flip active flag

TradiesTable schema (phone_number is the primary key):
  phone_number  String  PK  e.g. "+61411000001"
  name          String      full name
  business_name String      optional trading name
  trade_type    String      lowercase: plumber, electrician, carpenter, etc.
  location      String      suburb(s) served, e.g. "Surry Hills, Newtown"
  active        Boolean     true = available for new jobs
"""

import argparse
import sys
import boto3
from boto3.dynamodb.conditions import Attr

REGION = "ap-southeast-2"

SEED_TRADIES = [
    {
        "phone_number":  "+61411000001",
        "name":          "Dave Nguyen",
        "business_name": "Nguyen Plumbing",
        "trade_type":    "plumber",
        "location":      "Surry Hills, Newtown, Redfern",
        "active":        True,
    },
    {
        "phone_number":  "+61411000002",
        "name":          "Sarah Kim",
        "business_name": "Kim Electrical",
        "trade_type":    "electrician",
        "location":      "Parramatta, Westmead, Merrylands",
        "active":        True,
    },
    {
        "phone_number":  "+61411000003",
        "name":          "Tom Walsh",
        "business_name": "Walsh Carpentry",
        "trade_type":    "carpenter",
        "location":      "Manly, Dee Why, Brookvale",
        "active":        True,
    },
    {
        "phone_number":  "+61411000004",
        "name":          "Maria Santos",
        "business_name": "Santos Plumbing & Gas",
        "trade_type":    "plumber",
        "location":      "Parramatta, Auburn, Granville",
        "active":        True,
    },
    {
        "phone_number":  "+61411000005",
        "name":          "Jake Thornton",
        "business_name": "Thornton Electrical",
        "trade_type":    "electrician",
        "location":      "Surry Hills, Paddington, Randwick",
        "active":        False,  # inactive — useful for testing fallback
    },
    {
        "phone_number":  "+61411000006",
        "name":          "Priya Patel",
        "business_name": "Patel Painting",
        "trade_type":    "painter",
        "location":      "Chatswood, Lane Cove, Willoughby",
        "active":        True,
    },
    {
        "phone_number":  "+61411000007",
        "name":          "Chris O'Brien",
        "business_name": "O'Brien Tiling",
        "trade_type":    "tiler",
        "location":      "Bondi, Coogee, Maroubra",
        "active":        True,
    },
]


def _table(env: str):
    db = boto3.resource("dynamodb", region_name=REGION)
    return db.Table(f"TradiesTable-{env}")


def cmd_seed(env: str):
    tbl = _table(env)
    print(f"Seeding {len(SEED_TRADIES)} tradies into TradiesTable-{env} ...")
    with tbl.batch_writer() as batch:
        for t in SEED_TRADIES:
            batch.put_item(Item=t)
    print("Done.")


def cmd_list(env: str):
    tbl  = _table(env)
    resp = tbl.scan()
    items = sorted(resp.get("Items", []), key=lambda x: x["name"])
    if not items:
        print(f"No tradies in TradiesTable-{env}.")
        return
    fmt = "{:<15} {:<22} {:<14} {:<12} {}"
    print(fmt.format("PHONE", "NAME", "TRADE", "ACTIVE", "LOCATION"))
    print("-" * 80)
    for t in items:
        print(fmt.format(
            t["phone_number"],
            t["name"],
            t["trade_type"],
            "yes" if t.get("active") else "no",
            t.get("location", ""),
        ))


def cmd_add(env: str):
    print("Add new tradie (Ctrl-C to cancel)")
    tradie = {
        "phone_number":  _prompt("Phone (+614XXXXXXXX)"),
        "name":          _prompt("Full name"),
        "business_name": _prompt("Business name (optional)", optional=True),
        "trade_type":    _prompt("Trade type (plumber / electrician / carpenter / ...)").lower(),
        "location":      _prompt("Suburbs served (comma-separated)"),
        "active":        _prompt_bool("Active?", default=True),
    }
    _table(env).put_item(Item=tradie)
    print(f"Added {tradie['name']} ({tradie['phone_number']}).")


def cmd_update(env: str, phone: str):
    tbl  = _table(env)
    resp = tbl.get_item(Key={"phone_number": phone})
    t    = resp.get("Item")
    if not t:
        sys.exit(f"Tradie {phone} not found.")

    print(f"Updating {t['name']} ({phone}) — press Enter to keep current value.")
    updates = {}

    for field, label in [
        ("name",          "Full name"),
        ("business_name", "Business name"),
        ("trade_type",    "Trade type"),
        ("location",      "Suburbs served"),
    ]:
        current = t.get(field, "")
        val = input(f"  {label} [{current}]: ").strip()
        if val:
            updates[field] = val.lower() if field == "trade_type" else val

    active_str = input(f"  Active [{t.get('active')}] (y/n or Enter to keep): ").strip().lower()
    if active_str in ("y", "yes", "true"):
        updates["active"] = True
    elif active_str in ("n", "no", "false"):
        updates["active"] = False

    if not updates:
        print("No changes.")
        return

    expr    = "SET " + ", ".join(f"#{k} = :{k}" for k in updates)
    names   = {f"#{k}": k for k in updates}
    values  = {f":{k}": v for k, v in updates.items()}
    tbl.update_item(Key={"phone_number": phone}, UpdateExpression=expr,
                    ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    print(f"Updated {phone}.")


def cmd_delete(env: str, phone: str):
    tbl  = _table(env)
    resp = tbl.get_item(Key={"phone_number": phone})
    t    = resp.get("Item")
    if not t:
        sys.exit(f"Tradie {phone} not found.")
    confirm = input(f"Delete {t['name']} ({phone})? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return
    tbl.delete_item(Key={"phone_number": phone})
    print(f"Deleted {phone}.")


def cmd_toggle(env: str, phone: str):
    tbl    = _table(env)
    resp   = tbl.get_item(Key={"phone_number": phone})
    t      = resp.get("Item")
    if not t:
        sys.exit(f"Tradie {phone} not found.")
    new_active = not t.get("active", False)
    tbl.update_item(
        Key={"phone_number": phone},
        UpdateExpression="SET active = :a",
        ExpressionAttributeValues={":a": new_active},
    )
    status = "active" if new_active else "inactive"
    print(f"{t['name']} ({phone}) is now {status}.")


# ── helpers ────────────────────────────────────────────────────────────────────

def _prompt(label: str, optional: bool = False) -> str:
    while True:
        val = input(f"  {label}: ").strip()
        if val or optional:
            return val
        print("  (required)")


def _prompt_bool(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    val  = input(f"  {label} [{hint}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "true")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Manage TradiesTable entries")
    parser.add_argument("command", choices=["seed", "list", "add", "update", "delete", "toggle"])
    parser.add_argument("phone",   nargs="?", help="Tradie phone number (PK) for update/delete/toggle")
    parser.add_argument("--env",   default="dev", choices=["dev", "staging", "prod"])
    args = parser.parse_args()

    if args.command == "seed":
        cmd_seed(args.env)
    elif args.command == "list":
        cmd_list(args.env)
    elif args.command == "add":
        cmd_add(args.env)
    elif args.command in ("update", "delete", "toggle"):
        if not args.phone:
            sys.exit(f"'{args.command}' requires a phone number argument.")
        if args.command == "update":
            cmd_update(args.env, args.phone)
        elif args.command == "delete":
            cmd_delete(args.env, args.phone)
        elif args.command == "toggle":
            cmd_toggle(args.env, args.phone)


if __name__ == "__main__":
    main()
