# Tradie Connect — Setup Guide

## What is automated vs manual

| Step | How |
|------|-----|
| CFN stacks (Lambdas, DynamoDB, IAM roles, Lex bot) | **Automated** — deploy scripts |
| Bedrock Agent creation + action group | **Automated** — deploy scripts |
| Agent prepare + alias creation | **Automated** — deploy scripts |
| Lex Lambda env var injection | **Automated** — deploy scripts |
| Portal S3 + API Gateway + Lambda | **Automated** — `deploy_portal.sh` |
| **Wire Connect contact flows** | **Manual** — console only (one-time) |

---

## Prerequisites

- AWS CLI configured for `ap-southeast-2`
- An existing Amazon Connect instance — you only need the **instance alias**
- An S3 bucket for Lambda code:
  ```bash
  aws s3 mb s3://my-tradie-deploy-bucket --region ap-southeast-2
  ```
- `TradiesTable-dev` seeded:
  ```bash
  python scripts/manage_tradies.py seed --env dev
  ```

---

## Step 1 — Deploy customer agent

```bash
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc
```

What this does:
1. Packages and uploads both Lambda zips (`agent_actions.zip`, `lex_fulfillment.zip`)
2. **Pass 1 CFN** — creates `JobsTable-dev`, `tradie-connect-actions-dev`, `tradie-connect-customer-lex-dev`, IAM roles
3. Forces Lambda code updates
4. Creates/updates `TradieConnect-CustomerAgent-dev` Bedrock Agent with system prompt
5. Creates/updates `JobManagement` action group (4 functions)
6. Prepares the agent and creates/reuses the `Live` alias
7. **Pass 2 CFN** — injects Agent ID + Alias ID into the Lex fulfillment Lambda

Re-running is safe — existing resources are updated, not recreated.

---

## Step 2 — Deploy tradie agent

```bash
./scripts/deploy_tradie_agent.sh dev my-tradie-deploy-bucket
```

What this does:
1. Resolves the shared actions Lambda ARN from the customer stack
2. **Pass 1 CFN** (`tradie_agent.yaml`) — creates `TradieConnect-TradieBot-dev` Lex bot, fulfillment Lambda, IAM roles
3. Creates/updates `TradieConnect-TradieAgent-dev` Bedrock Agent with tradie system prompt
4. Creates/updates `TradieManagement` action group (3 functions: lookupTradieByCode, getJobsByTradie, completeJob)
5. Prepares the agent and creates/reuses the `live` alias
6. **Pass 2 CFN** — injects Tradie Agent ID + Alias ID into the tradie Lex Lambda

---

## Step 3 — Deploy portal

```bash
./scripts/deploy_portal.sh dev my-tradie-deploy-bucket
```

What this does:
1. Packages and uploads `portal_api.zip`
2. Deploys `tradie-portal-{env}` CFN stack (S3 bucket, API GW, portal Lambda)
3. Injects the API Gateway URL into `portal/index.html`
4. Uploads the HTML to the S3 static website bucket

Portal URL is printed at the end — share with tradies.

---

## Step 4 — Wire Connect contact flows (manual, one-time)

### Customer flow

In **Amazon Connect console → Routing → Contact flows** → open your inbound customer flow:

1. **Set voice** → Language: `English (AU)`, Voice: `Olivia`
2. **Get customer input**:
   - Text to speech: `Welcome to Tradie Connect.`
   - Select **Amazon Lex** → bot: `TradieConnectBot-dev`, alias: `live`
   - Under Intents → add `FallbackIntent`
3. **Disconnect** block on both outputs
4. Save and publish

### Tradie flow

Create a second contact flow (or use a separate number):

1. **Set voice** → Language: `English (AU)`, Voice: `Olivia`
2. **Get customer input**:
   - Text to speech: `Welcome to Tradie Connect.`
   - Select **Amazon Lex** → bot: `TradieConnect-TradieBot-dev`, alias: `live`
   - Under Intents → add `FallbackIntent`
3. **Disconnect** block on both outputs
4. Save and publish

---

## Smoke tests

### Customer agent

Call the customer number. Verify a job card was created:
```bash
aws dynamodb scan --table-name JobsTable-dev --region ap-southeast-2 --query "Items[0]"
```

Check appointment fields are present:
```bash
aws dynamodb scan --table-name JobsTable-dev --region ap-southeast-2 \
  --query "Items[0].job"
```

### Tradie agent

Call the tradie number. Say your 6-digit tradie code when prompted. Ask "what jobs do I have tomorrow?"

Check a tradie's code:
```bash
python scripts/manage_tradies.py show-code --env dev +61411000001
python scripts/manage_tradies.py list --env dev
```

### Portal

Open the portal URL, enter a tradie code. Jobs should appear with appointment date and time.

---

## Ongoing code updates

Re-run the deploy scripts after any Lambda or system prompt changes:
```bash
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc
./scripts/deploy_tradie_agent.sh dev my-tradie-deploy-bucket
./scripts/deploy_portal.sh dev my-tradie-deploy-bucket
```

---

## Teardown

```bash
# Customer agent
AGENT_ID=$(aws bedrock-agent list-agents --region ap-southeast-2 \
  --query "agentSummaries[?agentName=='TradieConnect-CustomerAgent-dev'].agentId" --output text)
ALIAS_ID=$(aws bedrock-agent list-agent-aliases --agent-id "$AGENT_ID" --region ap-southeast-2 \
  --query "agentAliasSummaries[?agentAliasName=='Live'].agentAliasId" --output text)
aws bedrock-agent delete-agent-alias --agent-id "$AGENT_ID" --agent-alias-id "$ALIAS_ID" --region ap-southeast-2
aws bedrock-agent delete-agent --agent-id "$AGENT_ID" --skip-resource-in-use-check --region ap-southeast-2

# Tradie agent
TAGENT_ID=$(aws bedrock-agent list-agents --region ap-southeast-2 \
  --query "agentSummaries[?agentName=='TradieConnect-TradieAgent-dev'].agentId" --output text)
TALIAS_ID=$(aws bedrock-agent list-agent-aliases --agent-id "$TAGENT_ID" --region ap-southeast-2 \
  --query "agentAliasSummaries[?agentAliasName=='live'].agentAliasId" --output text)
aws bedrock-agent delete-agent-alias --agent-id "$TAGENT_ID" --agent-alias-id "$TALIAS_ID" --region ap-southeast-2
aws bedrock-agent delete-agent --agent-id "$TAGENT_ID" --skip-resource-in-use-check --region ap-southeast-2

# CFN stacks
aws cloudformation delete-stack --stack-name tradie-connect-tradie-agent-dev --region ap-southeast-2
aws cloudformation delete-stack --stack-name tradie-portal-dev --region ap-southeast-2
aws cloudformation delete-stack --stack-name tradie-connect-agent-dev --region ap-southeast-2
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `EventStreamError: accessDeniedException` in Lex Lambda logs | Bedrock Agent alias is pinned to an old version whose execution role was deleted (happens after a CFN role rename) | Delete the agent alias and recreate it — this snapshots the current DRAFT with the correct role. Update Lambda env vars and IAM policy to the new alias ID. Then re-run CFN pass 2. |
| `no identity-based policy allows bedrock:InvokeAgent` | Lex Lambda IAM policy references a deleted alias ARN (alias was recreated but policy wasn't updated) | Run CFN pass 2 deploy with the new alias ID, or manually `aws iam put-role-policy` with the new alias ARN |
| Lex test fails: "Access denied while invoking lambda" | Lex bot alias still wired to old/deleted Lambda name after a rename | Run `aws lexv2-models update-bot-alias` for both `live` and `TSTALIASID` aliases to point to the new Lambda ARN |
| `AccessDeniedException` on Bedrock | Bedrock execution role missing `bedrock:InvokeModel` or `bedrock:GetInferenceProfile` | Verify role policy in IAM console |
| Model `Access Denied` | Not using `au.` inference profile prefix | Check `deploy.sh` `AGENT_MODEL` — must be `au.anthropic.claude-haiku-4-5-20251001-v1:0` |
| No tradies found | `TradiesTable-dev` missing active tradies | Run `python scripts/manage_tradies.py seed --env dev` |
| Tradie code not recognised | Tradie record missing `tradie_code` field | Run `python scripts/manage_tradies.py show-code --env dev <phone>` — if blank, re-seed or manually add code |
| Portal login fails silently | JS ReferenceError (historical — now fixed) | Ensure latest `portal/index.html` is deployed |
| Appointment date/time missing on portal | Job was created before appointment fields were added | Only new job cards include these fields |
