# Tradie Connect — Operational Skill Reference

Quick reference for deploying, managing, and debugging the Tradie Connect AI Agent stack.

---

## Deploy

```bash
# Full deploy — CFN + Bedrock Agent + action group + alias (idempotent)
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc

# Staging / prod
./scripts/deploy.sh staging my-tradie-deploy-bucket tradie-poc
./scripts/deploy.sh prod    my-tradie-deploy-bucket tradie-poc
```

---

## Teardown

```bash
AGENT_ID=$(aws bedrock-agent list-agents --region ap-southeast-2 \
  --query "agentSummaries[?agentName=='TradieConnectAgent-dev'].agentId" --output text)

ALIAS_ID=$(aws bedrock-agent list-agent-aliases --agent-id "$AGENT_ID" --region ap-southeast-2 \
  --query "agentAliasSummaries[?agentAliasName=='live'].agentAliasId" --output text)

aws bedrock-agent delete-agent-alias --agent-id "$AGENT_ID" --agent-alias-id "$ALIAS_ID" --region ap-southeast-2
aws bedrock-agent delete-agent --agent-id "$AGENT_ID" --skip-resource-in-use-check --region ap-southeast-2
aws cloudformation delete-stack --stack-name tradie-connect-agent-dev --region ap-southeast-2
```

---

## Bedrock Agent

```bash
# List agents and their status
aws bedrock-agent list-agents --region ap-southeast-2 \
  --query "agentSummaries[].{id:agentId,name:agentName,status:agentStatus}"

# Check agent model and role
aws bedrock-agent get-agent --agent-id <AGENT_ID> --region ap-southeast-2 \
  --query "agent.{model:foundationModel,role:agentResourceRoleArn,status:agentStatus}"

# List action groups and functions
aws bedrock-agent list-agent-action-groups \
  --agent-id <AGENT_ID> --agent-version DRAFT --region ap-southeast-2

# Prepare agent after changes
aws bedrock-agent prepare-agent --agent-id <AGENT_ID> --region ap-southeast-2

# List aliases
aws bedrock-agent list-agent-aliases --agent-id <AGENT_ID> --region ap-southeast-2 \
  --query "agentAliasSummaries[].{id:agentAliasId,name:agentAliasName,status:agentAliasStatus}"

# Update model (use au. or apac. prefix — bare model IDs return Access Denied)
aws bedrock-agent update-agent \
  --agent-id <AGENT_ID> \
  --agent-name "TradieConnectAgent-dev" \
  --foundation-model "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
  --agent-resource-role-arn <ROLE_ARN> \
  --instruction "$(cat connect_agent_system_prompt.txt)" \
  --region ap-southeast-2
```

---

## DynamoDB

```bash
# Check a job card was created
aws dynamodb scan \
  --table-name JobsTable-dev \
  --region ap-southeast-2 \
  --query "Items[0]"

# Count all jobs
aws dynamodb scan --table-name JobsTable-dev --region ap-southeast-2 --select COUNT

# Jobs awaiting Twilio notification
aws dynamodb scan \
  --table-name JobsTable-dev \
  --filter-expression "notification.#s = :pending" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":pending":{"S":"PENDING"}}' \
  --region ap-southeast-2

# Get a specific job
aws dynamodb get-item \
  --table-name JobsTable-dev \
  --key '{"job_id":{"S":"JOB-20260421-XXXXXX"}}' \
  --region ap-southeast-2
```

---

## Lambda

```bash
# Tail Lambda logs live
aws logs tail /aws/lambda/tradie-agent-actions-dev --follow --region ap-southeast-2
aws logs tail /aws/lambda/tradie-lex-fulfillment-dev --follow --region ap-southeast-2

# Check last update status
aws lambda get-function --function-name tradie-agent-actions-dev --region ap-southeast-2 \
  --query "Configuration.{status:LastUpdateStatus,runtime:Runtime,memory:MemorySize,timeout:Timeout}"

# Force Lambda code update (without full deploy)
aws lambda update-function-code \
  --function-name tradie-agent-actions-dev \
  --s3-bucket my-tradie-deploy-bucket \
  --s3-key lambda/agent_actions.zip \
  --region ap-southeast-2
aws lambda wait function-updated --function-name tradie-agent-actions-dev --region ap-southeast-2
```

---

## IAM Role

```bash
# Check Bedrock agent role policy
aws iam get-role-policy \
  --role-name tradie-bedrock-agent-role-dev \
  --policy-name BedrockAgentExecutionPolicy

# Check Lambda execution role
aws iam get-role-policy \
  --role-name tradie-agent-action-role-dev \
  --policy-name AgentActionPolicy
```

---

## Lex Bot

```bash
# List bots
aws lexv2-models list-bots --region ap-southeast-2 \
  --query "botSummaries[].{id:botId,name:botName,status:botStatus}"

# Check Lambda is wired to live alias
aws lexv2-models describe-bot-alias \
  --bot-id 8Q4RROSCP2 --bot-alias-id RXQU1942HA \
  --region ap-southeast-2 \
  --query "botAliasLocaleSettings.en_AU.codeHookSpecification"

# Check TestBotAlias Lambda (needed for console testing)
aws lexv2-models describe-bot-alias \
  --bot-id 8Q4RROSCP2 --bot-alias-id TSTALIASID \
  --region ap-southeast-2 \
  --query "botAliasLocaleSettings.en_AU.codeHookSpecification"
```

---

## Tradie Management

```bash
python scripts/manage_tradies.py seed   --env dev   # load sample tradies
python scripts/manage_tradies.py list   --env dev   # list all
python scripts/manage_tradies.py add    --env dev   # add interactively
python scripts/manage_tradies.py update --env dev +61411000001
python scripts/manage_tradies.py toggle --env dev +61411000001   # active/inactive
python scripts/manage_tradies.py delete --env dev +61411000001
```

---

## CloudFormation

```bash
# Check stack status and outputs
aws cloudformation describe-stacks \
  --stack-name tradie-connect-agent-dev --region ap-southeast-2 \
  --query "Stacks[0].{status:StackStatus,outputs:Outputs}"

# Watch stack events during deploy
aws cloudformation describe-stack-events \
  --stack-name tradie-connect-agent-dev --region ap-southeast-2 \
  --query "StackEvents[:10].[LogicalResourceId,ResourceStatus,ResourceStatusReason]" \
  --output table
```

---

## Available Inference Profiles (ap-southeast-2)

```bash
aws bedrock list-inference-profiles --region ap-southeast-2 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'haiku')].{id:inferenceProfileId,status:status}"
```

| Profile ID | Model |
|---|---|
| `au.anthropic.claude-haiku-4-5-20251001-v1:0` | Claude Haiku 4.5 (current) |
| `apac.anthropic.claude-3-haiku-20240307-v1:0` | Claude 3 Haiku (legacy) |

---

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Access denied` on Bedrock | Bare model ID without `au.`/`apac.` prefix | Use `au.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `AccessDeniedException` on CreateAgent | Role missing `bedrock:GetInferenceProfile` | Redeploy CFN — `agent.yaml` has the fix |
| `UnknownAction` from Lambda | Function name mismatch in action group | Names must match exactly: `createJobCard`, `lookupAvailableTradie`, `assignTradieToJob`, `logJobNotification` |
| Lex test "no Lambda associated" | `TestBotAlias` not wired | Attach `tradie-lex-fulfillment-dev` to `TestBotAlias` in Lex console |
| No tradies returned | `TradiesTable-dev` empty or `active=false` | Run `python scripts/manage_tradies.py seed --env dev` |
