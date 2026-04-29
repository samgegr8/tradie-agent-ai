#!/usr/bin/env bash
# Tradie Connect — portal deploy script
# Usage: ./scripts/deploy_portal.sh [dev|staging|prod] <code-bucket>
#
# First run   — creates CFN infrastructure (S3 bucket, API GW, Lambda, IAM)
# Subsequent  — skips CFN, pushes Lambda code and HTML directly (no drift correction)

set -euo pipefail

ENV="${1:-dev}"
CODE_BUCKET="${2:-}"

REGION="ap-southeast-2"
STACK_NAME="tradie-portal-${ENV}"
BUILD_DIR=".build"
PORTAL_SRC="lambdas/portal_api"
PORTAL_PACKAGE_DIR="${PORTAL_SRC}/package"

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
require pip
require zip

log "=== Tradie Portal deploy — env=$ENV stack=$STACK_NAME ==="

# ── Step 1: Package portal Lambda ─────────────────────────────────────────────
log "Step 1/4 — Packaging portal Lambda ..."

mkdir -p "$BUILD_DIR"
rm -rf "$PORTAL_PACKAGE_DIR"
mkdir -p "$PORTAL_PACKAGE_DIR"

cp "${PORTAL_SRC}/handler.py" "$PORTAL_PACKAGE_DIR/"
PORTAL_ZIP="$(pwd)/${BUILD_DIR}/portal_api.zip"
rm -f "$PORTAL_ZIP"
(cd "$PORTAL_PACKAGE_DIR" && zip -qr "$PORTAL_ZIP" .)
log "  portal_api.zip — $(du -sh "$PORTAL_ZIP" | cut -f1)"

# ── Step 2: Upload Lambda zip to S3 ───────────────────────────────────────────
log "Step 2/4 — Uploading Lambda to s3://${CODE_BUCKET}/lambda/ ..."
aws s3 cp "$PORTAL_ZIP" "s3://${CODE_BUCKET}/lambda/portal_api.zip" --region "$REGION"
log "  Upload complete."

# ── Step 3: CFN (first time only) or skip ─────────────────────────────────────
STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STACK_STATUS" == "DOES_NOT_EXIST" || "$STACK_STATUS" == *ROLLBACK_COMPLETE* ]]; then
  if [[ "$STACK_STATUS" == *ROLLBACK_COMPLETE* ]]; then
    log "Step 3/4 — Stack in ${STACK_STATUS} — deleting before re-create ..."
    aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
  fi

  log "Step 3/4 — Stack not found — running CFN create (one-time infrastructure setup) ..."
  CFN_EXIT=0
  aws cloudformation deploy \
    --template-file cloudformation/portal.yaml \
    --stack-name "$STACK_NAME" \
    --parameter-overrides \
        "Env=${ENV}" \
        "CodeBucket=${CODE_BUCKET}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$REGION" || CFN_EXIT=$?

  if [[ $CFN_EXIT -ne 0 ]]; then
    log "  ERROR: CloudFormation create failed (exit ${CFN_EXIT}). Failure events:"
    aws cloudformation describe-stack-events \
      --stack-name "$STACK_NAME" --region "$REGION" \
      --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
      --output table
    exit $CFN_EXIT
  fi
  log "  Infrastructure created."
else
  log "Step 3/4 — Stack already exists (${STACK_STATUS}) — skipping CFN, updating code directly."
fi

# ── Force Lambda code update (always — CFN is skipped on re-deploy) ────────────
log "  Pushing latest Lambda code ..."
aws lambda update-function-code \
  --function-name "tradie-portal-api-${ENV}" \
  --s3-bucket "$CODE_BUCKET" --s3-key "lambda/portal_api.zip" \
  --region "$REGION" --output text --query "LastUpdateStatus"
aws lambda wait function-updated --function-name "tradie-portal-api-${ENV}" --region "$REGION"
log "  Lambda updated."

# ── Step 4: Upload index.html with API URL injected ───────────────────────────
log "Step 4/4 — Uploading portal static site ..."

API_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='PortalApiUrl'].OutputValue" --output text)

PORTAL_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='PortalBucketName'].OutputValue" --output text)

WEBSITE_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='PortalWebsiteUrl'].OutputValue" --output text)

TMP_HTML="${BUILD_DIR}/index.html"
sed "s|__API_URL__|${API_URL}|g" portal/index.html > "$TMP_HTML"

aws s3 cp "$TMP_HTML" "s3://${PORTAL_BUCKET}/index.html" \
  --content-type "text/html" \
  --region "$REGION"

log "  Portal site uploaded."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "Portal deployed successfully."
echo ""
echo "  Website URL : ${WEBSITE_URL}"
echo "  API URL     : ${API_URL}"
