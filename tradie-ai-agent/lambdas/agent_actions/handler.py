"""
Tradie Connect — Lambda entry point
-------------------------------------
Receives Amazon Bedrock Agent action events, dispatches to MCPOrchestrator,
and returns the response in the Bedrock Agents response envelope.

Event shape from Bedrock Agent:
{
  "actionGroup": "JobManagement",
  "function": "createJobCard",
  "parameters": [{"name": "customerName", "value": "Jane Doe"}, ...]
}

Function and parameter names use camelCase to satisfy the Bedrock console
name constraint: ([0-9a-zA-Z][_-]?){1,100}
"""

import json
import logging

from orchestrator import MCPOrchestrator

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_orchestrator = MCPOrchestrator()

# Maps Bedrock Agent camelCase function names → orchestrator methods
# Parameter names are also camelCase from Bedrock; mapped to snake_case kwargs here
_HANDLERS = {
    "createJobCard":        _orchestrator.create_job_card,
    "lookupAvailableTradie": _orchestrator.lookup_available_tradie,
    "assignTradieToJob":    _orchestrator.assign_tradie_to_job,
    "logJobNotification":   _orchestrator.log_job_notification,
}

# Bedrock sends camelCase param names; orchestrator methods expect snake_case
_PARAM_MAP = {
    "customerName":      "customer_name",
    "callbackNumber":    "callback_number",
    "serviceType":       "service_type",
    "problemDescription":"problem_description",
    "preferredTime":     "preferred_time",
    "jobId":             "job_id",
    "tradieId":          "tradie_id",
    "tradiePhone":       "tradie_phone",
}


def lambda_handler(event: dict, context) -> dict:
    logger.info("Connect action event: %s", json.dumps(event))

    action_group = event.get("actionGroup", "")
    function     = event.get("function", "")
    parameters   = event.get("parameters", [])

    # Convert [{name, value}] list to snake_case kwargs for orchestrator methods
    kwargs = {_PARAM_MAP.get(p["name"], p["name"]): p["value"] for p in parameters}

    handler = _HANDLERS.get(function)
    if handler is None:
        logger.error("Unknown function: %s", function)
        return _response(action_group, function, f"Unknown action: {function}", state="FAILURE")

    try:
        result = handler(**kwargs)
        body   = json.dumps(result)
        logger.info("Action %s succeeded: %s", function, body)
        return _response(action_group, function, body)
    except TypeError as exc:
        # Missing or unexpected parameters from Connect
        logger.error("Parameter error for %s: %s", function, exc)
        return _response(action_group, function, f"Parameter error: {exc}", state="FAILURE")
    except Exception as exc:
        logger.exception("Action %s failed: %s", function, exc)
        return _response(action_group, function, f"Internal error: {exc}", state="FAILURE")


def _response(action_group: str, function: str, body: str, state: str = "SUCCESS") -> dict:
    """Returns the response envelope required by Amazon Connect / Bedrock Agents."""
    function_response = {
        "responseBody": {
            "TEXT": {"body": body}
        }
    }
    # responseState must be omitted on success — Bedrock only accepts FAILURE or REPROMPT
    if state != "SUCCESS":
        function_response["responseState"] = state

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": action_group,
            "function":    function,
            "functionResponse": function_response,
        },
    }
