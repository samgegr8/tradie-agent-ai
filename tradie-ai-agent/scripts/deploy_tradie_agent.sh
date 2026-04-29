#!/usr/bin/env bash
# Tradie Connect — tradie-facing agent deploy script
# Usage: ./scripts/deploy_tradie_agent.sh [dev|staging|prod] <code-bucket>
#
# What this deploys (fully automated — no manual AWS console steps):
#   - Stores system prompt in SSM Advanced Parameter
#   - CFN stack (tradie-connect-tradie-agent-{env}):
#       - AWS::Bedrock::Agent  (TradieConnect-TradieAgent-{env})
#       - AWS::Bedrock::AgentAlias (live)
#       - TradieManagement action group (6 tools: auth, jobs, trip log)
#       - AWS::Lex::Bot  (TradieConnect-TradieBot-{env})
#       - Lex fulfillment Lambda + IAM roles
#   - Force-updates Lambda code for the shared actions Lambda
#
# Prereq: tradie-connect-agent-{env} stack must already be deployed
#         (the shared agent-actions Lambda must exist)

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

log "=== Tradie Agent deploy — env=$ENV stack=$TRADIE_STACK ==="

# ── Step 1: Resolve shared Lambda ARN + force code update ─────────────────────
log "Step 1/4 — Resolving shared agent-actions Lambda ARN ..."

LAMBDA_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$AGENT_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='AgentActionLambdaArn'].OutputValue" --output text 2>/dev/null || true)

if [[ -z "$LAMBDA_ARN" || "$LAMBDA_ARN" == "None" ]]; then
  echo "ERROR: Could not resolve AgentActionLambdaArn from stack '$AGENT_STACK'."
  echo "       Run ./scripts/deploy.sh first to deploy the customer agent stack."
  exit 1
fi

log "  AgentActionLambdaArn: ${LAMBDA_ARN}"

# Force the shared actions Lambda to the latest code already in S3.
# deploy.sh packages and uploads agent_actions.zip. This ensures the Lambda
# has the latest orchestrator (startTrip, endTrip, getTripLog etc.) before
# the Bedrock Agent is created/updated with the matching action group schema.
log "  Force-updating shared actions Lambda code from S3 ..."
aws lambda update-function-code \
  --function-name "tradie-connect-actions-${ENV}" \
  --s3-bucket "$CODE_BUCKET" --s3-key "lambda/agent_actions.zip" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated \
  --function-name "tradie-connect-actions-${ENV}" --region "$REGION"
log "  Actions Lambda updated."

# ── Step 2: Store system prompt in SSM ────────────────────────────────────────
log "Step 2/4 — Storing tradie agent system prompt in SSM ..."

INSTRUCTION=$(cat tradie_agent_system_prompt.txt)

aws ssm put-parameter \
  --name  "$SSM_INSTRUCTION_PARAM" \
  --value "$INSTRUCTION" \
  --type  String \
  --tier  Advanced \
  --overwrite \
  --region "$REGION" > /dev/null

log "  Stored at: ${SSM_INSTRUCTION_PARAM}"

# ── Step 3: CFN deploy (single pass — Lex bot + Bedrock Agent + Alias) ────────
log "Step 3/4 — CloudFormation deploy ..."

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

# Force-update the tradie Lex Lambda code (CFN won't re-push if the S3 key is unchanged)
log "  Force-updating tradie Lex Lambda code from S3 ..."
aws lambda update-function-code \
  --function-name "tradie-connect-tradie-lex-${ENV}" \
  --s3-bucket "$CODE_BUCKET" --s3-key "lambda/lex_fulfillment.zip" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated \
  --function-name "tradie-connect-tradie-lex-${ENV}" --region "$REGION"
log "  Lex Lambda updated."

# ── Step 4: Output ─────────────────────────────────────────────────────────────
log "Step 4/4 — Done."
echo ""
echo "Tradie Agent deployed:"
aws cloudformation describe-stacks \
  --stack-name "$TRADIE_STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" --output table
echo ""
echo "One manual step remaining:"
echo "  Wire TradieConnect-TradieBot-${ENV} into a Connect contact flow:"
echo "    - Add a 'Get customer input' block"
echo "    - Select Amazon Lex → Bot: TradieConnect-TradieBot-${ENV}, Alias: live"
echo "    - Route the flow to the tradie queue"
