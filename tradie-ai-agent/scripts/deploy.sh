#!/usr/bin/env bash
# Tradie Connect — customer agent deploy script
# Usage: ./scripts/deploy.sh [dev|staging|prod] <code-bucket> <connect-instance-alias>
#
# What this deploys (fully automated — no manual AWS console steps):
#   - Packages and uploads both Lambda zips to S3
#   - Stores system prompt in SSM Advanced Parameter
#   - CFN stack (tradie-connect-agent-{env}):
#       - DynamoDB JobsTable + TripsTable
#       - Shared actions Lambda + IAM roles
#       - AWS::Bedrock::Agent  (TradieConnect-CustomerAgent-{env})
#       - AWS::Bedrock::AgentAlias (live)
#       - JobManagement action group (4 tools)
#       - AWS::Lex::Bot  (TradieConnect-CustomerBot-{env})
#       - Customer Lex fulfillment Lambda + IAM roles
#   - Force-updates both Lambda functions to the uploaded zip

set -euo pipefail

# ── Args ───────────────────────────────────────────────────────────────────────
ENV="${1:-dev}"
CODE_BUCKET="${2:-}"
CONNECT_INSTANCE_ALIAS="${3:-}"

REGION="ap-southeast-2"
STACK_NAME="tradie-connect-agent-${ENV}"
SSM_INSTRUCTION_PARAM="/tradie-connect/${ENV}/customer-agent-instruction"
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
log()     { echo "[$(date '+%H:%M:%S')] $*"; }
require() { command -v "$1" &>/dev/null || { echo "ERROR: '$1' not found in PATH"; exit 1; }; }

require aws
require pip
require zip

log "=== Tradie Connect deploy — env=$ENV stack=$STACK_NAME ==="

# ── Resolve AWS account ID ─────────────────────────────────────────────────────
log "Resolving AWS account ID ..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log "  Account ID: ${ACCOUNT_ID}"

# ── Resolve Connect instance ARN ───────────────────────────────────────────────
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

# ── Step 1: Package Lambdas ────────────────────────────────────────────────────
log "Step 1/4 — Packaging Lambdas ..."

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

# ── Step 2: Upload to S3 + store system prompt in SSM ─────────────────────────
log "Step 2/4 — Uploading Lambda zips and storing system prompt ..."

aws s3 cp "$ACTIONS_ZIP" "s3://${CODE_BUCKET}/lambda/agent_actions.zip" --region "$REGION"
aws s3 cp "$LEX_ZIP"     "s3://${CODE_BUCKET}/lambda/lex_fulfillment.zip" --region "$REGION"
log "  S3 upload complete."

INSTRUCTION=$(cat connect_agent_system_prompt.txt)
aws ssm put-parameter \
  --name  "$SSM_INSTRUCTION_PARAM" \
  --value "$INSTRUCTION" \
  --type  String \
  --tier  Advanced \
  --overwrite \
  --region "$REGION" > /dev/null
log "  System prompt stored at: ${SSM_INSTRUCTION_PARAM}"

# ── Step 3: CFN deploy (single pass — full stack including Bedrock Agent + Lex) ─
log "Step 3/4 — CloudFormation deploy ..."

STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STACK_STATUS" == *ROLLBACK_COMPLETE* ]]; then
  log "  Stack in ${STACK_STATUS} — deleting before re-create ..."
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
fi

CFN_EXIT=0
aws cloudformation deploy \
  --template-file cloudformation/agent.yaml \
  --stack-name "$STACK_NAME" \
  --parameter-overrides \
      "Env=${ENV}" \
      "CodeBucket=${CODE_BUCKET}" \
      "ConnectInstanceArn=${CONNECT_INSTANCE_ARN}" \
      "CustomerAgentInstruction=${SSM_INSTRUCTION_PARAM}" \
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

# Force Lambda code updates (CFN won't re-push if S3 key is unchanged)
log "  Force-updating Lambda code from S3 ..."
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

# ── Step 4: Outputs ────────────────────────────────────────────────────────────
log "Step 4/4 — Done."
echo ""
echo "Stack outputs:"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" --output table

echo ""
echo "One manual step remaining:"
echo "  Wire TradieConnect-CustomerBot-${ENV} into a Connect contact flow:"
echo "    - Add a 'Get customer input' block"
echo "    - Select Amazon Lex → Bot: TradieConnect-CustomerBot-${ENV}, Alias: live"
echo "    - Under Intents → add FallbackIntent"
echo "    - Save and publish"
