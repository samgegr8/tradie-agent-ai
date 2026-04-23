# Tradie Connect — AI Agent Feature

## What This Is

An Amazon Connect AI Agent that answers inbound calls, collects job details conversationally, creates a job card in DynamoDB, and matches a tradie. Tradie notification via SMS is a future roadmap item (Twilio — not yet built).

## Repo Layout

```
lambdas/agent_actions/
  handler.py       ← Lambda entry point; routes Bedrock action events to orchestrator
  orchestrator.py  ← Tool implementations (3 active + 1 stub)
  requirements.txt

lambdas/lex_fulfillment/
  handler.py       ← Forwards Lex utterances to Bedrock Agent via bedrock-agent-runtime

cloudformation/
  agent.yaml       ← JobsTable, Lambdas, IAM roles, Bedrock execution role (no SNS)

scripts/
  deploy.sh        ← Single command — CFN + Bedrock Agent + action group + alias (fully automated)
  manage_tradies.py

connect_agent_system_prompt.txt ← Loaded into Bedrock Agent by deploy.sh (do not paste manually)
AI_AGENT_SETUP.md               ← Deployment guide — automated vs manual breakdown
```

## AWS Services in Play

| Service | Purpose |
|---------|---------|
| Amazon Connect | Inbound voice, contact flow |
| Amazon Lex V2 (`TradieConnectBot-{env}`) | Thin pass-through from Connect to Bedrock Agent |
| Amazon Bedrock Agents | Conversational AI with tool-calling (Claude Haiku 4.5) |
| AWS Lambda (Python 3.12) | Tool actions + Lex fulfillment |
| DynamoDB `JobsTable-{env}` | Stores job cards (primary key: `job_id`) |
| DynamoDB `TradiesTable-{env}` | Source of truth for tradies |
| CloudFormation | Deploys all infrastructure |

**Not in scope yet:** SNS, Twilio, SES. Tradie notification is roadmap only.

## Bedrock Model

Model: `au.anthropic.claude-haiku-4-5-20251001-v1:0` (cross-region inference profile, APAC routing)

Do not use bare model IDs like `anthropic.claude-3-haiku-20240307-v1:0` — they require the `au.` or `apac.` prefix in `ap-southeast-2` or Bedrock will return Access Denied.

## The 4 Tools

3 active + 1 stub. Called in sequence by the Bedrock Agent via Lambda.

1. `createJobCard` — writes a new record to JobsTable, returns `job_id`. Sets `notification.status=PENDING`.
2. `lookupAvailableTradie` — scans TradiesTable, prefers suburb match, returns `tradie_id`
3. `assignTradieToJob` — updates JobsTable to `ASSIGNED`, links tradie details
4. `logJobNotification` *(stub)* — logs intent only. Replace with Twilio API call when roadmap item is built.

**Never reorder tools 1–3.** The `job_id` from step 1 is required by step 3.

## What deploy.sh Does (fully automated)

```
./scripts/deploy.sh dev <code-bucket> <connect-instance-alias>
```

1. Packages + uploads both Lambda zips to S3
2. Pass 1 CFN — creates JobsTable, Lambdas, IAM roles
3. Forces Lambda code updates
4. Creates/reuses Bedrock Agent with system prompt from `connect_agent_system_prompt.txt`
5. Creates/reuses `JobManagement` action group with all 4 functions
6. Prepares agent + creates/reuses `live` alias
7. Pass 2 CFN — injects Agent ID + Alias ID into Lex Lambda env vars

Re-running is idempotent — existing agent/action group/alias are skipped.

**Only manual step:** wire the Lex bot into the Connect contact flow (console only).

## Twilio Roadmap Hook

Integration point: `orchestrator.py:log_job_notification()`. Every job card has `notification.status=PENDING | SENT | FAILED` and `provider=twilio`. A future EventBridge rule or DynamoDB Streams trigger can poll for `PENDING` records and dispatch via Twilio without changing the agent, contact flow, or Lambda handler.

Do NOT add SNS resources to `agent.yaml` as an interim workaround.

## JobsTable Schema

Primary key: `job_id` (String, format `JOB-YYYYMMDD-XXXXXX`)

Required on write: `job_id`, `created_at`, `status`, `urgency`, `customer` (object), `job` (object), `notification` (object)
Populated later: `tradie` (object), `summary` (string)
TTL: `expires_at` (Unix epoch, 30 days)

## Bedrock ↔ Lambda Event Shape

```json
{
  "actionGroup": "JobManagement",
  "function": "createJobCard",
  "parameters": [{"name": "customerName", "value": "Jane Doe"}, ...]
}
```

`handler.py` converts the parameter list to kwargs and calls the matching orchestrator method.
Response must follow the Bedrock agent response envelope — see `handler.py:_response()`.

## Key Constraints

- **Python 3.12** only
- **boto3 only** — no third-party HTTP clients; Lambda role uses IAM, not API keys
- **No SNS** in this stack — do not add SNS permissions to `agent.yaml`
- **TradiesTable scan** — acceptable at 50 calls/day; add a GSI on `trade_type` above ~500 calls/day
- **Keep this file under 200 lines** — split additions into `.claude/rules/` files

## Naming Conventions

- CloudFormation stacks: `tradie-connect-agent-{env}`
- Lambda functions: `tradie-agent-actions-{env}`, `tradie-lex-fulfillment-{env}`
- Bedrock Agent: `TradieConnectAgent-{env}`
- Lex bot: `TradieConnectBot-{env}`
- DynamoDB tables: `JobsTable-{env}`, `TradiesTable-{env}`
- IAM roles: `tradie-agent-action-role-{env}`, `tradie-bedrock-agent-role-{env}`
- Job IDs: `JOB-YYYYMMDD-{6 char hex uppercase}`

## Common Commands

```bash
# Full deploy (CFN + Bedrock Agent + action group + alias — all automated)
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc

# Smoke test — check a job card was created
aws dynamodb scan --table-name JobsTable-dev --region ap-southeast-2 --query "Items[0]"

# Check for unnotified jobs (future Twilio queue)
aws dynamodb scan \
  --table-name JobsTable-dev \
  --filter-expression "notification.#s = :pending" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":pending":{"S":"PENDING"}}' \
  --region ap-southeast-2

# Teardown
AGENT_ID=$(aws bedrock-agent list-agents --region ap-southeast-2 \
  --query "agentSummaries[?agentName=='TradieConnectAgent-dev'].agentId" --output text)
ALIAS_ID=$(aws bedrock-agent list-agent-aliases --agent-id "$AGENT_ID" --region ap-southeast-2 \
  --query "agentAliasSummaries[?agentAliasName=='live'].agentAliasId" --output text)
aws bedrock-agent delete-agent-alias --agent-id "$AGENT_ID" --agent-alias-id "$ALIAS_ID" --region ap-southeast-2
aws bedrock-agent delete-agent --agent-id "$AGENT_ID" --skip-resource-in-use-check --region ap-southeast-2
aws cloudformation delete-stack --stack-name tradie-connect-agent-dev --region ap-southeast-2
```

## What NOT to Change Without Reading First

- `handler.py:_response()` — the Bedrock agent response envelope shape is mandatory; deviating silently breaks the agent
- `connect_agent_system_prompt.txt` — tool call sequencing is enforced by the prompt; reordering breaks the tool chain
- `agent.yaml` IAM policy — SNS is intentionally absent; do not add it
- `deploy.sh` AGENT_MODEL — must use `au.` or `apac.` inference profile prefix, not bare model IDs

## How This Fits the Existing Stack

The parent stack (`tradie-connect-{env}`) already deploys `TradiesTable-{env}`, `InteractionsTable-{env}`, and S3 buckets. This stack is purely additive.

After a call ends, the existing `recordings → Transcribe → Summariser` pipeline runs independently and can write the transcript summary back to `JobsTable` by matching on `contact_id`.
