"""
Tradie Connect — Lex V2 fulfillment Lambda
-------------------------------------------
Amazon Connect only supports Lex bots in the "Get customer input" flow block.
This Lambda sits between Connect/Lex and the Bedrock Agent:

  Connect (speech) → Lex STT → this Lambda → Bedrock Agent → Lex TTS → Connect (audio)

The Bedrock Agent handles all conversation logic and tool calls.
This Lambda is just a pass-through per turn.
"""

import os
import json
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ID       = os.environ["BEDROCK_AGENT_ID"]
AGENT_ALIAS_ID = os.environ["BEDROCK_AGENT_ALIAS_ID"]
REGION         = os.environ.get("AWS_REGION", "ap-southeast-2")

bedrock = boto3.client("bedrock-agent-runtime", region_name=REGION)

_ENDING_PHRASES = ["goodbye", "bye", "take care", "all the best", "have a good"]


def lambda_handler(event: dict, context) -> dict:
    logger.info("Lex event: %s", json.dumps(event))

    session_id        = event["sessionId"]
    user_input        = event.get("inputTranscript", "").strip()
    session_state_in  = event.get("sessionState", {})
    intent_block      = session_state_in.get("intent", {})
    intent_name       = intent_block.get("name", "FallbackIntent")
    session_attributes = session_state_in.get("sessionAttributes", {})

    # Greeting intent — treat as conversation start
    if intent_name == "Greeting" and not user_input:
        user_input = "hello"

    try:
        resp = bedrock.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=AGENT_ALIAS_ID,
            sessionId=session_id,
            inputText=user_input or "hello",
        )

        agent_reply = ""
        for chunk_event in resp.get("completion", []):
            if "chunk" in chunk_event:
                agent_reply += chunk_event["chunk"]["bytes"].decode("utf-8")

        agent_reply = agent_reply.strip() or "Sorry, I didn't catch that — could you say that again?"

    except Exception as exc:
        logger.exception("Bedrock Agent error: %s", exc)
        agent_reply = "Sorry, something went wrong. Please call back and try again."

    logger.info("Agent reply: %s", agent_reply)

    is_done = any(p in agent_reply.lower() for p in _ENDING_PHRASES)

    if is_done:
        response = {
            "sessionState": {
                "sessionAttributes": session_attributes,
                "dialogAction": {"type": "Close"},
                "intent": {
                    "name":  intent_name,
                    "state": "Fulfilled",
                },
            },
            "messages": [
                {"contentType": "PlainText", "content": agent_reply}
            ],
        }
    else:
        response = {
            "sessionState": {
                "sessionAttributes": session_attributes,
                "dialogAction": {"type": "ElicitIntent"},
            },
            "messages": [
                {"contentType": "PlainText", "content": agent_reply}
            ],
        }

    logger.info("Lex response: %s", json.dumps(response))
    return response
