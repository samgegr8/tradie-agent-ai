# Tradie Connect — AI Agent Setup Guide

## What is automated vs manual

| Step | How |
|------|-----|
| CFN stack (Lambdas, DynamoDB, IAM roles) | **Automated** — `deploy.sh` |
| Bedrock Agent creation | **Automated** — `deploy.sh` |
| Action group (4 job functions) | **Automated** — `deploy.sh` |
| Agent prepare + alias creation | **Automated** — `deploy.sh` |
| Lex Lambda env var injection | **Automated** — `deploy.sh` |
| Lex bot Lambda associations | **Pre-configured** — no action needed |
| **Wire Connect contact flow** | **Manual** — console only (one-time) |

---

## Prerequisites

- AWS CLI configured for `ap-southeast-2`
- An existing Amazon Connect instance — you only need the **instance alias** (visible in the Connect console URL)
- An S3 bucket for Lambda code:
  ```bash
  aws s3 mb s3://my-tradie-deploy-bucket --region ap-southeast-2
  ```
- `TradiesTable-dev` seeded:
  ```bash
  python scripts/manage_tradies.py seed --env dev
  ```

---

## Step 1 — Run deploy.sh (fully automated)

```bash
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc
```

The script handles everything in sequence:

1. Packages and uploads both Lambda zips to S3
2. **Pass 1 CFN** — creates `JobsTable-dev`, `tradie-agent-actions-dev`, `tradie-lex-fulfillment-dev`, IAM roles
3. Forces Lambda code updates
4. Creates `TradieConnectAgent-dev` Bedrock Agent (model: `au.anthropic.claude-haiku-4-5-20251001-v1:0`)
5. Creates `JobManagement` action group with all 4 functions
6. Prepares the agent and creates the `live` alias
7. **Pass 2 CFN** — injects Agent ID + Alias ID into the Lex fulfillment Lambda

The script is idempotent — re-running it on an existing stack skips creation of existing resources and updates Lambda code only.

At the end, the script prints the Agent ID, Alias ID, and stack outputs.

---

## Step 2 — Wire the Connect contact flow (manual, one-time)

This is the only step that cannot be automated — Connect flow editing requires the console.

In **Amazon Connect console → Routing → Contact flows** → open your inbound flow:

1. Drag in a **Set voice** block → Language: `English (AU)`, Voice: `Olivia`
2. Drag in a **Get customer input** block:
   - Text to speech: `Welcome to Tradie Connect.`
   - Select **Amazon Lex** → bot: `TradieConnectBot-dev`, alias: `live`
   - Under Intents → add `FallbackIntent`
3. Drag in a **Disconnect** block — connect both Success and Fallback outputs here
4. **Save and publish**

```
Entry → Set voice → Get customer input (Lex) → Disconnect
                           ↓ (fallback/error)
                        Disconnect
```

---

## Smoke test

Call the number associated with the flow. Verify a job card was created:

```bash
aws dynamodb scan \
  --table-name JobsTable-dev \
  --region ap-southeast-2 \
  --query "Items[0]"
```

---

## Ongoing code updates

Re-run the same command after any Lambda changes — the script detects the existing stack, skips resource creation, and updates Lambda code:

```bash
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc
```

---

## Teardown

```bash
# 1. Get agent and alias IDs
AGENT_ID=$(aws bedrock-agent list-agents --region ap-southeast-2 \
  --query "agentSummaries[?agentName=='TradieConnectAgent-dev'].agentId" --output text)
ALIAS_ID=$(aws bedrock-agent list-agent-aliases --agent-id "$AGENT_ID" --region ap-southeast-2 \
  --query "agentAliasSummaries[?agentAliasName=='live'].agentAliasId" --output text)

# 2. Delete alias then agent
aws bedrock-agent delete-agent-alias --agent-id "$AGENT_ID" --agent-alias-id "$ALIAS_ID" --region ap-southeast-2
aws bedrock-agent delete-agent --agent-id "$AGENT_ID" --skip-resource-in-use-check --region ap-southeast-2

# 3. Delete CFN stack (also deletes JobsTable — back up data first if needed)
aws cloudformation delete-stack --stack-name tradie-connect-agent-dev --region ap-southeast-2
```

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `AccessDeniedException` on Bedrock | Redeploy CFN — role must include `bedrock:GetInferenceProfile`. Check `BedrockAgentExecutionRoleArn` is assigned to the agent |
| `Access denied` when testing agent | Model not using `au.` inference profile prefix — check `deploy.sh` `AGENT_MODEL` value |
| Lambda not invoked | Verify action group Lambda ARN matches `tradie-agent-actions-dev` and resource policy allows `bedrock.amazonaws.com` |
| `UnknownAction` error | Function names must exactly match: `createJobCard`, `lookupAvailableTradie`, `assignTradieToJob`, `logJobNotification` |
| No tradies found | Verify `TradiesTable-dev` has items with `active=true`. Run `python scripts/manage_tradies.py list` |
| Lex test fails with "no Lambda" | Re-attach `tradie-lex-fulfillment-dev` to both `live` and `TestBotAlias` in Lex console |
| Zip build error | Run the script from the project root, not a subdirectory |
