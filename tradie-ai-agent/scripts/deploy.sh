#!/usr/bin/env bash
# Tradie Connect — build and deploy script
# Usage: ./scripts/deploy.sh [dev|staging|prod] <code-bucket> <connect-instance-alias>
#
# This script is fully self-contained:
#   Pass 1  — packages and deploys CFN (Lambdas, DynamoDB, IAM roles)
#   Pass 2  — creates/updates Bedrock Agent, action group, alias
#   Pass 3  — re-deploys CFN to inject Agent ID + Alias ID into Lex Lambda env vars

set -euo pipefail

# ── Args ───────────────────────────────────────────────────────────────────────
ENV="${1:-dev}"
CODE_BUCKET="${2:-}"
CONNECT_INSTANCE_ALIAS="${3:-}"

REGION="ap-southeast-2"
STACK_NAME="tradie-connect-agent-${ENV}"
AGENT_NAME="TradieConnect-CustomerAgent-${ENV}"
AGENT_NAME_LEGACY="TradieConnectAgent-${ENV}"
AGENT_MODEL="au.anthropic.claude-haiku-4-5-20251001-v1:0"
BUILD_DIR=".build"
LAMBDA_SRC="lambdas/agent_actions"
PACKAGE_DIR="${LAMBDA_SRC}/package"
LEX_SRC="lambdas/lex_fulfillment"
LEX_PACKAGE_DIR="${LEX_SRC}/package"

# ── Validate ───────────────────────────────────────────────────────────────────
if [[ -z "$CODE_BUCKET" || -z "$CONNECT_INSTANCE_ALIAS" ]]; then
  echo "Usage: $0 [dev|staging|prod] <code-bucket> <connect-instance-alias>"
  echo ""
  echo "  dev|staging|prod          Deployment environment (default: dev)"
  echo "  code-bucket               S3 bucket name for Lambda zip uploads"
  echo "  connect-instance-alias    Amazon Connect instance alias"
  echo ""
  echo "  Example:"
  echo "    $0 dev my-tradie-deploy-bucket tradie-poc"
  exit 1
fi

if [[ "$ENV" != "dev" && "$ENV" != "staging" && "$ENV" != "prod" ]]; then
  echo "ERROR: env must be dev, staging, or prod (got: $ENV)"
  exit 1
fi

# ── Helpers ────────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*"; }

require() {
  command -v "$1" &>/dev/null || { echo "ERROR: '$1' not found in PATH"; exit 1; }
}

require aws
require pip
require zip

log "=== Tradie Connect deploy — env=$ENV stack=$STACK_NAME ==="

# ── Resolve account ID ─────────────────────────────────────────────────────────
log "Resolving AWS account ID ..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log "  Account ID: ${ACCOUNT_ID}"

# ── Resolve Connect instance ARN from alias ────────────────────────────────────
log "Looking up Connect instance for alias '${CONNECT_INSTANCE_ALIAS}' ..."

CONNECT_INSTANCE_ID=$(aws connect list-instances \
  --region "$REGION" \
  --query "InstanceSummaryList[?InstanceAlias=='${CONNECT_INSTANCE_ALIAS}'].Id" \
  --output text)

if [[ -z "$CONNECT_INSTANCE_ID" || "$CONNECT_INSTANCE_ID" == "None" ]]; then
  echo "ERROR: No Connect instance found with alias '${CONNECT_INSTANCE_ALIAS}' in ${REGION}."
  echo "       Run 'aws connect list-instances --region ${REGION}' to see available instances."
  exit 1
fi

CONNECT_INSTANCE_ARN="arn:aws:connect:${REGION}:${ACCOUNT_ID}:instance/${CONNECT_INSTANCE_ID}"
log "  Instance ARN: ${CONNECT_INSTANCE_ARN}"

# ── Detect existing stack ──────────────────────────────────────────────────────
STACK_ACTION="CREATE"
STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].StackStatus" \
  --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STACK_STATUS" == "DOES_NOT_EXIST" ]]; then
  log "  Stack not found — will CREATE."
elif [[ "$STACK_STATUS" == *ROLLBACK_COMPLETE* ]]; then
  log "  Stack is in ${STACK_STATUS}. Deleting failed stack before re-creating ..."
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
  log "  Deleted. Will CREATE fresh stack."
else
  STACK_ACTION="UPDATE"
  log "  Existing stack found (status: ${STACK_STATUS}) — will UPDATE."
fi

# ── Step 1: Package Lambdas ────────────────────────────────────────────────────
log "Step 1/6 — Packaging Lambdas ..."

mkdir -p "$BUILD_DIR"

rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"
pip install --quiet --requirement "${LAMBDA_SRC}/requirements.txt" --target "$PACKAGE_DIR"
cp "${LAMBDA_SRC}/handler.py" "${LAMBDA_SRC}/orchestrator.py" "$PACKAGE_DIR/"
ACTIONS_ZIP="$(pwd)/${BUILD_DIR}/agent_actions.zip"
rm -f "$ACTIONS_ZIP"
(cd "$PACKAGE_DIR" && zip -qr "$ACTIONS_ZIP" .)
log "  agent_actions.zip — $(du -sh "$ACTIONS_ZIP" | cut -f1)"

rm -rf "$LEX_PACKAGE_DIR"
mkdir -p "$LEX_PACKAGE_DIR"
pip install --quiet --requirement "${LEX_SRC}/requirements.txt" --target "$LEX_PACKAGE_DIR"
cp "${LEX_SRC}/handler.py" "$LEX_PACKAGE_DIR/"
LEX_ZIP="$(pwd)/${BUILD_DIR}/lex_fulfillment.zip"
rm -f "$LEX_ZIP"
(cd "$LEX_PACKAGE_DIR" && zip -qr "$LEX_ZIP" .)
log "  lex_fulfillment.zip — $(du -sh "$LEX_ZIP" | cut -f1)"

# ── Step 2: Upload to S3 ───────────────────────────────────────────────────────
log "Step 2/6 — Uploading to s3://${CODE_BUCKET}/lambda/ ..."
aws s3 cp "$ACTIONS_ZIP" "s3://${CODE_BUCKET}/lambda/agent_actions.zip" --region "$REGION"
aws s3 cp "$LEX_ZIP"     "s3://${CODE_BUCKET}/lambda/lex_fulfillment.zip" --region "$REGION"
log "  Upload complete."

# ── Step 3: Pass 1 CFN deploy (no agent IDs yet) ──────────────────────────────
log "Step 3/6 — Pass 1 CloudFormation deploy ..."

CFN_EXIT=0
aws cloudformation deploy \
  --template-file cloudformation/agent.yaml \
  --stack-name "$STACK_NAME" \
  --parameter-overrides \
      "Env=${ENV}" \
      "CodeBucket=${CODE_BUCKET}" \
      "ConnectInstanceArn=${CONNECT_INSTANCE_ARN}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --region "$REGION" || CFN_EXIT=$?

if [[ $CFN_EXIT -ne 0 ]]; then
  log "  ERROR: CloudFormation deploy failed (exit ${CFN_EXIT}). Failure events:"
  aws cloudformation describe-stack-events \
    --stack-name "$STACK_NAME" --region "$REGION" \
    --query "StackEvents[?ResourceStatus=='CREATE_FAILED'||ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
    --output table
  exit $CFN_EXIT
fi

log "  Pass 1 ${STACK_ACTION} complete."

# Resolve outputs
LAMBDA_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AgentActionLambdaArn'].OutputValue" --output text)
BEDROCK_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='BedrockAgentExecutionRoleArn'].OutputValue" --output text)

log "  AgentActionLambdaArn:      ${LAMBDA_ARN}"
log "  BedrockAgentExecutionRole: ${BEDROCK_ROLE_ARN}"

# Force Lambda code updates (macOS bash 3 does not support declare -A)
log "  Forcing Lambda code updates ..."
aws lambda update-function-code \
  --function-name "tradie-connect-actions-${ENV}" \
  --s3-bucket "$CODE_BUCKET" --s3-key "lambda/agent_actions.zip" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated --function-name "tradie-connect-actions-${ENV}" --region "$REGION"

aws lambda update-function-code \
  --function-name "tradie-connect-customer-lex-${ENV}" \
  --s3-bucket "$CODE_BUCKET" --s3-key "lambda/lex_fulfillment.zip" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated --function-name "tradie-connect-customer-lex-${ENV}" --region "$REGION"
log "  Lambdas updated."

# ── Step 4: Create or reuse Bedrock Agent ─────────────────────────────────────
log "Step 4/6 — Bedrock Agent ..."

INSTRUCTION=$(cat connect_agent_system_prompt.txt)

# Check by new name first, fall back to legacy name for existing deployments
EXISTING_AGENT_ID=$(aws bedrock-agent list-agents --region "$REGION" \
  --query "agentSummaries[?agentName=='${AGENT_NAME}'].agentId" --output text 2>/dev/null || true)
if [[ -z "$EXISTING_AGENT_ID" || "$EXISTING_AGENT_ID" == "None" ]]; then
  EXISTING_AGENT_ID=$(aws bedrock-agent list-agents --region "$REGION" \
    --query "agentSummaries[?agentName=='${AGENT_NAME_LEGACY}'].agentId" --output text 2>/dev/null || true)
  [[ -n "$EXISTING_AGENT_ID" && "$EXISTING_AGENT_ID" != "None" ]] && \
    log "  Found agent under legacy name '${AGENT_NAME_LEGACY}' — will rename."
fi

if [[ -n "$EXISTING_AGENT_ID" && "$EXISTING_AGENT_ID" != "None" ]]; then
  log "  Updating agent (${EXISTING_AGENT_ID}) ..."
  AGENT_ID="$EXISTING_AGENT_ID"
  aws bedrock-agent update-agent \
    --agent-id "$AGENT_ID" \
    --agent-name "$AGENT_NAME" \
    --foundation-model "$AGENT_MODEL" \
    --agent-resource-role-arn "$BEDROCK_ROLE_ARN" \
    --instruction "$INSTRUCTION" \
    --region "$REGION" > /dev/null
  log "  Agent updated."
else
  log "  Creating agent '${AGENT_NAME}' ..."
  AGENT_ID=$(aws bedrock-agent create-agent \
    --agent-name "$AGENT_NAME" \
    --foundation-model "$AGENT_MODEL" \
    --agent-resource-role-arn "$BEDROCK_ROLE_ARN" \
    --instruction "$INSTRUCTION" \
    --idle-session-ttl-in-seconds 600 \
    --region "$REGION" \
    --query "agent.agentId" --output text)
  log "  Agent created: ${AGENT_ID}"
fi

# Action group schema — always create or update so Lambda ARN and schema stay current
CUSTOMER_AG_SCHEMA='{
  "functions": [
    {
      "name": "createJobCard",
      "description": "Creates a new job card in DynamoDB with customer and job details. Always call this first.",
      "parameters": {
        "customerName":       {"type":"string","required":true,"description":"Full name of the customer calling in."},
        "callbackNumber":     {"type":"string","required":true,"description":"Customer phone number for callback."},
        "serviceType":        {"type":"string","required":true,"description":"Type of trade required e.g. plumber, electrician, carpenter."},
        "address":            {"type":"string","required":true,"description":"Full street address including suburb, state and postcode."},
        "problemDescription": {"type":"string","required":true,"description":"Pipe-delimited: problem description|YYYY-MM-DD appointment date|time slot|urgency. e.g. Leaking tap under kitchen sink|2026-04-25|morning|standard"}
      }
    },
    {
      "name": "lookupAvailableTradie",
      "description": "Finds an available tradie matching the service type and suburb. Call this after createJobCard.",
      "parameters": {
        "serviceType": {"type":"string","required":true, "description":"Trade type to match, must match what was passed to createJobCard."},
        "suburb":      {"type":"string","required":false,"description":"Suburb from the customer address, used to prefer a nearby tradie."}
      }
    },
    {
      "name": "assignTradieToJob",
      "description": "Assigns the matched tradie to the job card. Call this after lookupAvailableTradie.",
      "parameters": {
        "jobId":    {"type":"string","required":true,"description":"Job ID returned by createJobCard."},
        "tradieId": {"type":"string","required":true,"description":"Tradie ID returned by lookupAvailableTradie."}
      }
    },
    {
      "name": "logJobNotification",
      "description": "Logs the notification intent for the assigned tradie. Call this last, after assignTradieToJob.",
      "parameters": {
        "jobId":       {"type":"string","required":true,"description":"Job ID of the assigned job."},
        "tradiePhone": {"type":"string","required":true,"description":"Phone number of the assigned tradie."}
      }
    }
  ]
}'

EXISTING_AG_ID=$(aws bedrock-agent list-agent-action-groups \
  --agent-id "$AGENT_ID" --agent-version DRAFT --region "$REGION" \
  --query "actionGroupSummaries[?actionGroupName=='JobManagement'].actionGroupId" --output text 2>/dev/null || true)

if [[ -n "$EXISTING_AG_ID" && "$EXISTING_AG_ID" != "None" ]]; then
  log "  Updating JobManagement action group (${EXISTING_AG_ID}) ..."
  aws bedrock-agent update-agent-action-group \
    --agent-id "$AGENT_ID" \
    --agent-version DRAFT \
    --action-group-id "$EXISTING_AG_ID" \
    --action-group-name JobManagement \
    --description "Job card creation, tradie lookup, assignment, and notification logging" \
    --action-group-executor "{\"lambda\":\"${LAMBDA_ARN}\"}" \
    --function-schema "$CUSTOMER_AG_SCHEMA" \
    --region "$REGION" > /dev/null
  log "  Action group updated."
else
  log "  Creating JobManagement action group ..."
  aws bedrock-agent create-agent-action-group \
    --agent-id "$AGENT_ID" \
    --agent-version DRAFT \
    --action-group-name JobManagement \
    --description "Job card creation, tradie lookup, assignment, and notification logging" \
    --action-group-executor "{\"lambda\":\"${LAMBDA_ARN}\"}" \
    --function-schema "$CUSTOMER_AG_SCHEMA" \
    --region "$REGION" \
    --query "agentActionGroup.actionGroupState" --output text
  log "  Action group created."
fi

# Prepare agent
log "  Preparing agent ..."
aws bedrock-agent prepare-agent --agent-id "$AGENT_ID" --region "$REGION" > /dev/null
sleep 15
AGENT_STATUS=$(aws bedrock-agent get-agent --agent-id "$AGENT_ID" --region "$REGION" \
  --query "agent.agentStatus" --output text)
log "  Agent status: ${AGENT_STATUS}"

# Create or reuse alias
EXISTING_ALIAS=$(aws bedrock-agent list-agent-aliases \
  --agent-id "$AGENT_ID" --region "$REGION" --output json 2>/dev/null | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
m = [a['agentAliasId'] for a in data.get('agentAliasSummaries', []) if a['agentAliasName'].lower() == 'live']
print(m[0] if m else '')
" || true)

if [[ -n "$EXISTING_ALIAS" && "$EXISTING_ALIAS" != "None" ]]; then
  log "  Alias 'live' already exists (${EXISTING_ALIAS}) — skipping create."
  AGENT_ALIAS_ID="$EXISTING_ALIAS"
else
  log "  Creating alias 'live' ..."
  AGENT_ALIAS_ID=$(aws bedrock-agent create-agent-alias \
    --agent-id "$AGENT_ID" \
    --agent-alias-name live \
    --region "$REGION" \
    --query "agentAlias.agentAliasId" --output text)
  sleep 10
  log "  Alias created: ${AGENT_ALIAS_ID}"
fi

log "  Agent ID:    ${AGENT_ID}"
log "  Alias ID:    ${AGENT_ALIAS_ID}"

# ── Step 5: Pass 2 CFN deploy (inject agent IDs) ──────────────────────────────
log "Step 5/6 — Pass 2 CloudFormation deploy (injecting Agent + Alias IDs) ..."

aws cloudformation deploy \
  --template-file cloudformation/agent.yaml \
  --stack-name "$STACK_NAME" \
  --parameter-overrides \
      "Env=${ENV}" \
      "CodeBucket=${CODE_BUCKET}" \
      "ConnectInstanceArn=${CONNECT_INSTANCE_ARN}" \
      "BedrockAgentId=${AGENT_ID}" \
      "BedrockAgentAliasId=${AGENT_ALIAS_ID}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --region "$REGION"

log "  Pass 2 complete."

# ── Step 6: Outputs ────────────────────────────────────────────────────────────
log "Step 6/6 — Done."
echo ""
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" --output table

echo ""
echo "Bedrock Agent:"
echo "  Agent ID:  ${AGENT_ID}"
echo "  Alias ID:  ${AGENT_ALIAS_ID}"
echo "  Model:     ${AGENT_MODEL}"
echo ""
echo "One manual step remaining:"
echo "  Wire the Lex bot into your Connect contact flow — see AI_AGENT_SETUP.md Step 2."
