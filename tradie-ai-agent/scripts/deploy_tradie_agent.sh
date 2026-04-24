#!/usr/bin/env bash
# Tradie Connect — tradie-facing agent deploy script
# Usage: ./scripts/deploy_tradie_agent.sh [dev|staging|prod] <code-bucket>
#
# What this deploys:
#   - Bedrock Agent: TradieConnectAgent-tradie-{env}
#   - Action group: TradieManagement (getJobsByTradie + completeJob)
#   - Lex fulfillment Lambda pointing at the tradie agent
#   - CFN stack: tradie-connect-tradie-agent-{env}
#
# Prereq: tradie-connect-agent-{env} stack must already be deployed
#         (the shared agent-actions Lambda must exist)

set -euo pipefail

ENV="${1:-dev}"
CODE_BUCKET="${2:-}"

REGION="ap-southeast-2"
AGENT_STACK="tradie-connect-agent-${ENV}"
TRADIE_STACK="tradie-connect-tradie-agent-${ENV}"
AGENT_NAME="TradieConnect-TradieAgent-${ENV}"
AGENT_NAME_LEGACY="TradieConnectAgent-tradie-${ENV}"
AGENT_MODEL="au.anthropic.claude-haiku-4-5-20251001-v1:0"

# ── Validate ───────────────────────────────────────────────────────────────────
if [[ -z "$CODE_BUCKET" ]]; then
  echo "Usage: $0 [dev|staging|prod] <code-bucket>"
  echo "  Example: $0 dev my-tradie-deploy-bucket"
  exit 1
fi

if [[ "$ENV" != "dev" && "$ENV" != "staging" && "$ENV" != "prod" ]]; then
  echo "ERROR: env must be dev, staging, or prod (got: $ENV)"
  exit 1
fi

log()     { echo "[$(date '+%H:%M:%S')] $*"; }
require() { command -v "$1" &>/dev/null || { echo "ERROR: '$1' not found in PATH"; exit 1; }; }

require aws

log "=== Tradie Agent deploy — env=$ENV stack=$TRADIE_STACK ==="

# ── Step 1: Resolve shared Lambda ARN from customer agent stack ───────────────
log "Step 1/5 — Resolving shared agent-actions Lambda ARN ..."

LAMBDA_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$AGENT_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AgentActionLambdaArn'].OutputValue" --output text 2>/dev/null || true)

if [[ -z "$LAMBDA_ARN" || "$LAMBDA_ARN" == "None" ]]; then
  echo "ERROR: Could not resolve AgentActionLambdaArn from stack '$AGENT_STACK'."
  echo "       Run ./scripts/deploy.sh first to deploy the customer agent stack."
  exit 1
fi

log "  AgentActionLambdaArn: ${LAMBDA_ARN}"

# ── Step 2: Pass 1 CFN deploy (no agent IDs yet) ──────────────────────────────
log "Step 2/5 — Pass 1 CloudFormation deploy ..."

STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$TRADIE_STACK" --region "$REGION" \
  --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STACK_STATUS" == *ROLLBACK_COMPLETE* ]]; then
  log "  Stack in ${STACK_STATUS} — deleting before re-create ..."
  aws cloudformation delete-stack --stack-name "$TRADIE_STACK" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$TRADIE_STACK" --region "$REGION"
fi

CFN_EXIT=0
aws cloudformation deploy \
  --template-file cloudformation/tradie_agent.yaml \
  --stack-name "$TRADIE_STACK" \
  --parameter-overrides \
      "Env=${ENV}" \
      "CodeBucket=${CODE_BUCKET}" \
      "AgentActionLambdaArn=${LAMBDA_ARN}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --region "$REGION" || CFN_EXIT=$?

if [[ $CFN_EXIT -ne 0 ]]; then
  log "  ERROR: CloudFormation deploy failed (exit ${CFN_EXIT}). Failure events:"
  aws cloudformation describe-stack-events \
    --stack-name "$TRADIE_STACK" --region "$REGION" \
    --query "StackEvents[?ResourceStatus=='CREATE_FAILED'||ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
    --output table
  exit $CFN_EXIT
fi

BEDROCK_ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$TRADIE_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='TradieBedrockAgentExecutionRoleArn'].OutputValue" --output text)

log "  TradieBedrockAgentRole: ${BEDROCK_ROLE_ARN}"

# ── Step 3: Create or reuse tradie Bedrock Agent ──────────────────────────────
log "Step 3/5 — Bedrock Agent ..."

# Check by new name first, fall back to legacy name for existing deployments
EXISTING_AGENT_ID=$(aws bedrock-agent list-agents --region "$REGION" \
  --query "agentSummaries[?agentName=='${AGENT_NAME}'].agentId" --output text 2>/dev/null || true)
if [[ -z "$EXISTING_AGENT_ID" || "$EXISTING_AGENT_ID" == "None" ]]; then
  EXISTING_AGENT_ID=$(aws bedrock-agent list-agents --region "$REGION" \
    --query "agentSummaries[?agentName=='${AGENT_NAME_LEGACY}'].agentId" --output text 2>/dev/null || true)
  [[ -n "$EXISTING_AGENT_ID" && "$EXISTING_AGENT_ID" != "None" ]] && \
    log "  Found agent under legacy name '${AGENT_NAME_LEGACY}' — will rename."
fi

INSTRUCTION=$(cat tradie_agent_system_prompt.txt)

if [[ -n "$EXISTING_AGENT_ID" && "$EXISTING_AGENT_ID" != "None" ]]; then
  log "  Agent already exists (${EXISTING_AGENT_ID}) — updating instruction ..."
  AGENT_ID="$EXISTING_AGENT_ID"
  aws bedrock-agent update-agent \
    --agent-id "$AGENT_ID" \
    --agent-name "$AGENT_NAME" \
    --foundation-model "$AGENT_MODEL" \
    --agent-resource-role-arn "$BEDROCK_ROLE_ARN" \
    --instruction "$INSTRUCTION" \
    --region "$REGION" > /dev/null
  log "  Instruction updated."
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

# Shared function schema — create or update action group so schema changes are always applied
FUNCTION_SCHEMA='{
  "functions": [
    {
      "name": "lookupTradieByCode",
      "description": "Authenticates a tradie by their 6-digit tradie code. Always call this first. Returns phone_number which must be used for all subsequent tool calls.",
      "parameters": {
        "tradieCode": {
          "type": "string",
          "required": true,
          "description": "The 6-digit tradie code spoken by the caller e.g. 482719"
        }
      }
    },
    {
      "name": "getJobsByTradie",
      "description": "Returns jobs assigned to a tradie for a given date. Use the phone_number returned by lookupTradieByCode as tradiePhone.",
      "parameters": {
        "tradiePhone": {
          "type": "string",
          "required": true,
          "description": "Tradie phone number from lookupTradieByCode result e.g. +61411000001"
        },
        "dateFilter": {
          "type": "string",
          "required": true,
          "description": "Date to filter by: today, tomorrow, day after tomorrow, a weekday name e.g. monday, or ISO date YYYY-MM-DD"
        }
      }
    },
    {
      "name": "completeJob",
      "description": "Marks a job as COMPLETED. Verifies job is assigned to the tradie. Use phone_number from lookupTradieByCode as tradiePhone.",
      "parameters": {
        "jobId": {
          "type": "string",
          "required": true,
          "description": "Job reference number e.g. JOB-20260424-A3F2B1"
        },
        "tradiePhone": {
          "type": "string",
          "required": true,
          "description": "Tradie phone number from lookupTradieByCode result — verifies job ownership"
        }
      }
    }
  ]
}'

EXISTING_AG_ID=$(aws bedrock-agent list-agent-action-groups \
  --agent-id "$AGENT_ID" --agent-version DRAFT --region "$REGION" \
  --query "actionGroupSummaries[?actionGroupName=='TradieManagement'].actionGroupId" --output text 2>/dev/null || true)

if [[ -n "$EXISTING_AG_ID" && "$EXISTING_AG_ID" != "None" ]]; then
  log "  Updating TradieManagement action group (${EXISTING_AG_ID}) ..."
  aws bedrock-agent update-agent-action-group \
    --agent-id "$AGENT_ID" \
    --agent-version DRAFT \
    --action-group-id "$EXISTING_AG_ID" \
    --action-group-name TradieManagement \
    --description "Tradie code auth, job reminders, and job completion" \
    --action-group-executor "{\"lambda\":\"${LAMBDA_ARN}\"}" \
    --function-schema "$FUNCTION_SCHEMA" \
    --region "$REGION" > /dev/null
  log "  Action group updated."
else
  log "  Creating TradieManagement action group ..."
  aws bedrock-agent create-agent-action-group \
    --agent-id "$AGENT_ID" \
    --agent-version DRAFT \
    --action-group-name TradieManagement \
    --description "Tradie code auth, job reminders, and job completion" \
    --action-group-executor "{\"lambda\":\"${LAMBDA_ARN}\"}" \
    --function-schema "$FUNCTION_SCHEMA" \
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

log "  Agent ID:  ${AGENT_ID}"
log "  Alias ID:  ${AGENT_ALIAS_ID}"

# ── Step 4: Pass 2 CFN deploy (inject agent IDs into Lex Lambda env vars) ────
log "Step 4/5 — Pass 2 CloudFormation deploy (injecting Agent + Alias IDs) ..."

aws cloudformation deploy \
  --template-file cloudformation/tradie_agent.yaml \
  --stack-name "$TRADIE_STACK" \
  --parameter-overrides \
      "Env=${ENV}" \
      "CodeBucket=${CODE_BUCKET}" \
      "AgentActionLambdaArn=${LAMBDA_ARN}" \
      "TradieBedrockAgentId=${AGENT_ID}" \
      "TradieBedrockAgentAliasId=${AGENT_ALIAS_ID}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --region "$REGION"

LEX_BOT_ID=$(aws cloudformation describe-stacks \
  --stack-name "$TRADIE_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='TradieLexBotId'].OutputValue" --output text)
LEX_ALIAS_ID=$(aws cloudformation describe-stacks \
  --stack-name "$TRADIE_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='TradieLexBotAliasId'].OutputValue" --output text)

# ── Step 5: Output ─────────────────────────────────────────────────────────────
log "Step 5/5 — Done."
echo ""
echo "Tradie Agent deployed:"
echo "  Bedrock Agent ID:  ${AGENT_ID}"
echo "  Bedrock Alias ID:  ${AGENT_ALIAS_ID}"
echo "  Lex Bot ID:        ${LEX_BOT_ID}"
echo "  Lex Alias ID:      ${LEX_ALIAS_ID}"
echo "  Model:             ${AGENT_MODEL}"
echo ""
echo "One manual step remaining:"
echo "  Wire TradieConnect-TradieBot-${ENV} into a Connect contact flow:"
echo "    - Add a 'Get customer input' block"
echo "    - Select Amazon Lex, Bot: TradieConnectBot-tradie-${ENV}, Alias: live"
echo "    - Route the flow to a tradie queue or second phone number"
