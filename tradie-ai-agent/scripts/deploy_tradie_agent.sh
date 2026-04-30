#!/usr/bin/env bash
# Tradie Connect — tradie-facing agent deploy script
# Usage: ./scripts/deploy_tradie_agent.sh [dev|staging|prod] <code-bucket>
#
# What this deploys (fully automated — no manual AWS console steps):
#   - Stores system prompt in SSM Advanced Parameter
#   - CFN stack (tradie-connect-tradie-agent-{env}):
#       - AWS::Bedrock::Agent  (TradieConnect-TradieAgent-{env})
#       - TradieManagement action group (6 tools: auth, jobs, trip log)
#       - AWS::Lex::Bot  (TradieConnect-TradieBot-{env})
#       - Lex fulfillment Lambda + IAM roles
#   - Prepares the agent (explicit — AutoPrepare is disabled in CFN)
#   - Creates or reuses the Bedrock Agent 'live' alias outside of CFN
#   - Syncs Lex Lambda env vars + IAM policy to the current alias
#
# Prereq: tradie-connect-agent-{env} stack must already be deployed.

set -euo pipefail

ENV="${1:-dev}"
CODE_BUCKET="${2:-}"

REGION="ap-southeast-2"
AGENT_STACK="tradie-connect-agent-${ENV}"
TRADIE_STACK="tradie-connect-tradie-agent-${ENV}"
SSM_INSTRUCTION_PARAM="/tradie-connect/${ENV}/tradie-agent-instruction"

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

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")

log "=== Tradie Agent deploy — env=$ENV stack=$TRADIE_STACK ==="

# ── Step 1: Resolve shared Lambda ARN + force code update ─────────────────────
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

log "  Force-updating shared actions Lambda code from S3 ..."
aws lambda update-function-code \
  --function-name "tradie-connect-actions-${ENV}" \
  --s3-bucket "$CODE_BUCKET" --s3-key "lambda/agent_actions.zip" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated \
  --function-name "tradie-connect-actions-${ENV}" --region "$REGION"
log "  Actions Lambda updated."

# ── Step 2: Store system prompt in SSM ────────────────────────────────────────
log "Step 2/5 — Storing tradie agent system prompt in SSM ..."

INSTRUCTION=$(cat tradie_agent_system_prompt.txt)
aws ssm put-parameter \
  --name  "$SSM_INSTRUCTION_PARAM" \
  --value "$INSTRUCTION" \
  --type  String \
  --tier  Advanced \
  --overwrite \
  --region "$REGION" > /dev/null
log "  Stored at: ${SSM_INSTRUCTION_PARAM}"

# ── Step 3: CFN deploy ────────────────────────────────────────────────────────
log "Step 3/5 — CloudFormation deploy ..."

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
      "TradieAgentInstruction=${SSM_INSTRUCTION_PARAM}" \
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

log "  Force-updating tradie Lex Lambda code from S3 ..."
aws lambda update-function-code \
  --function-name "tradie-connect-tradie-lex-${ENV}" \
  --s3-bucket "$CODE_BUCKET" --s3-key "lambda/lex_fulfillment.zip" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated \
  --function-name "tradie-connect-tradie-lex-${ENV}" --region "$REGION"
log "  Lex Lambda code updated."

# ── Step 4: Prepare agent + manage alias outside CFN ─────────────────────────
# AutoPrepare is disabled in CFN. We prepare explicitly so CFN never touches
# the alias, which prevents alias replacement and the resulting access errors.
log "Step 4/5 — Preparing agent and syncing alias ..."

TRADIE_AGENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "$TRADIE_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='TradieBedrockAgentId'].OutputValue" --output text)

log "  Preparing agent ${TRADIE_AGENT_ID} ..."
aws bedrock-agent prepare-agent \
  --agent-id "$TRADIE_AGENT_ID" --region "$REGION" > /dev/null

for i in $(seq 1 24); do
  STATUS=$(aws bedrock-agent get-agent \
    --agent-id "$TRADIE_AGENT_ID" --region "$REGION" \
    --query "agent.agentStatus" --output text)
  [[ "$STATUS" == "PREPARED" ]] && break
  log "  Agent status: ${STATUS} — waiting ..."
  sleep 5
done
log "  Agent prepared."

# Create the 'live' alias if it doesn't exist; reuse it if it does.
# This alias is never touched by CFN so its ID is stable across deploys.
TRADIE_ALIAS_ID=$(aws bedrock-agent list-agent-aliases \
  --agent-id "$TRADIE_AGENT_ID" --region "$REGION" \
  --query "agentAliasSummaries[?agentAliasName=='live'].agentAliasId" --output text)

if [[ -z "$TRADIE_ALIAS_ID" || "$TRADIE_ALIAS_ID" == "None" ]]; then
  log "  Creating 'live' alias ..."
  TRADIE_ALIAS_ID=$(aws bedrock-agent create-agent-alias \
    --agent-id "$TRADIE_AGENT_ID" \
    --agent-alias-name live \
    --region "$REGION" \
    --query "agentAlias.agentAliasId" --output text)
  log "  Alias created: ${TRADIE_ALIAS_ID}"
else
  log "  Reusing existing 'live' alias: ${TRADIE_ALIAS_ID}"
fi

ALIAS_ARN="arn:aws:bedrock:${REGION}:${ACCOUNT_ID}:agent-alias/${TRADIE_AGENT_ID}/${TRADIE_ALIAS_ID}"
LEX_FN="tradie-connect-tradie-lex-${ENV}"
LEX_ROLE="tradie-connect-tradie-lex-role-${ENV}"

aws lambda update-function-configuration \
  --function-name "$LEX_FN" \
  --environment "Variables={BEDROCK_AGENT_ID=${TRADIE_AGENT_ID},BEDROCK_AGENT_ALIAS_ID=${TRADIE_ALIAS_ID}}" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated --function-name "$LEX_FN" --region "$REGION"

aws iam put-role-policy \
  --role-name "$LEX_ROLE" \
  --policy-name "TradieLexFulfillmentPolicy" \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"bedrock:InvokeAgent\",\"Resource\":\"${ALIAS_ARN}\"}]}"

log "  Alias sync complete — AgentId: ${TRADIE_AGENT_ID}  AliasId: ${TRADIE_ALIAS_ID}"

# ── Step 5: Output ─────────────────────────────────────────────────────────────
log "Step 5/5 — Done."
echo ""
echo "Tradie Agent deployed:"
aws cloudformation describe-stacks \
  --stack-name "$TRADIE_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" --output table
echo ""
echo "  Bedrock Agent ID : ${TRADIE_AGENT_ID}"
echo "  Bedrock Alias ID : ${TRADIE_ALIAS_ID}"
echo ""
echo "One manual step remaining:"
echo "  Wire TradieConnect-TradieBot-${ENV} into a Connect contact flow:"
echo "    - Add a 'Get customer input' block"
echo "    - Select Amazon Lex → Bot: TradieConnect-TradieBot-${ENV}, Alias: live"
echo "    - Route the flow to the tradie queue"
