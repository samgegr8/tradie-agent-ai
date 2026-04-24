# Tradie Connect — AI Agent Feature

## What This Is

Two Amazon Connect AI Agents on a shared Lambda backend:
1. **Customer agent** — answers inbound calls, collects job details, creates a job card, matches a tradie
2. **Tradie agent** — lets tradies call in to check their scheduled jobs and mark jobs complete

A **tradie portal** (S3 static website) lets tradies view their jobs using the same 6-digit tradie code.

Tradie notification via SMS is a future roadmap item (Twilio — not yet built).

## Repo Layout

```
lambdas/agent_actions/
  handler.py       ← Lambda entry point; routes Bedrock action events to orchestrator
  orchestrator.py  ← 7 tool implementations (4 customer + 3 tradie)
  requirements.txt

lambdas/lex_fulfillment/
  handler.py       ← Forwards Lex utterances to Bedrock Agent (shared by both bots)

lambdas/portal_api/
  handler.py       ← Portal REST API: POST /login (tradie_code) + GET /jobs?tradie_code=

cloudformation/
  agent.yaml          ← Customer stack: JobsTable, shared actions Lambda, IAM, Lex Lambda
  tradie_agent.yaml   ← Tradie stack: TradieBot (CFN-managed Lex), Lex Lambda, IAM
  portal.yaml         ← Portal stack: S3 website, API GW HTTP API, portal Lambda

scripts/
  deploy.sh                  ← Customer agent (fully automated)
  deploy_tradie_agent.sh     ← Tradie agent (fully automated)
  deploy_portal.sh           ← Portal website deploy
  manage_tradies.py          ← Tradie CRUD + tradie_code management

portal/index.html            ← Static portal HTML (API_URL injected at deploy time)
connect_agent_system_prompt.txt
tradie_agent_system_prompt.txt
AI_AGENT_SETUP.md
```

## AWS Services in Play

| Service | Purpose |
|---------|---------|
| Amazon Connect | Inbound voice, contact flows |
| Amazon Lex V2 | Thin pass-through from Connect to Bedrock Agent |
| Amazon Bedrock Agents | Conversational AI with tool-calling (Claude Haiku 4.5) |
| AWS Lambda (Python 3.12) | Tool actions + Lex fulfillment + portal API |
| DynamoDB `JobsTable-{env}` | Job cards (PK: `job_id`) |
| DynamoDB `TradiesTable-{env}` | Tradie roster (PK: `phone_number`) |
| S3 | Portal static website + Lambda code bucket |
| API Gateway HTTP API | Portal REST API |
| CloudFormation | All infrastructure as code |

**Not in scope yet:** SNS, Twilio, SES.

## Bedrock Model

`au.anthropic.claude-haiku-4-5-20251001-v1:0` (cross-region inference profile, APAC routing)

Never use bare model IDs like `anthropic.claude-3-haiku-20240307-v1:0` — they require the `au.` prefix in `ap-southeast-2`.

## The 7 Tools

### Customer agent — JobManagement action group (4 tools)

1. `createJobCard` — writes JobsTable. `problemDescription` is pipe-delimited: `description|YYYY-MM-DD|time slot|urgency`. Orchestrator splits on `|` to store `appointment_date`, `appointment_time`, `urgency`, and `problem_description` separately.
2. `lookupAvailableTradie` — scans TradiesTable by trade_type + suburb match
3. `assignTradieToJob` — updates job to ASSIGNED, links tradie details
4. `logJobNotification` *(stub)* — logs intent only

**Never reorder tools 1–3.** The `job_id` from tool 1 is required by tool 3.

**Bedrock schema limit: 5 parameters per function.** This is why appointment date/time/urgency are embedded in `problemDescription` as a pipe-delimited string rather than separate parameters.

### Tradie agent — TradieManagement action group (3 tools)

1. `lookupTradieByCode` — authenticates by 6-digit `tradie_code`, returns `phone_number`
2. `getJobsByTradie` — filters JobsTable by `job.appointment_date` (not `created_at`) for a given date
3. `completeJob` — marks job COMPLETED, verifies `tradie.phone` ownership

## Tradie Code Authentication

Every tradie has a 6-digit numeric `tradie_code` in TradiesTable. Used for:
- Phone call auth (tradie speaks it, agent calls `lookupTradieByCode`)
- Portal login (tradie enters it, portal API resolves to phone for DynamoDB queries)

Phone numbers are never exposed to the browser. The portal uses `tradie_code` as the session identifier throughout.

## Appointment Date/Time

Job cards store `job.appointment_date` (ISO `YYYY-MM-DD`) and `job.appointment_time` (free text e.g. "morning"). The customer agent collects these and packs them into `problemDescription` pipe-delimited. The orchestrator parses them on write.

`getJobsByTradie` filters by `Attr("job.appointment_date").eq(date_str)` — so "what jobs do I have tomorrow" returns jobs *scheduled* for tomorrow, not jobs *created* tomorrow.

## Alias Recreation Pattern

When a CFN deploy renames an IAM role (delete + create), versioned Bedrock Agent aliases throw `EventStreamError: accessDeniedException` because the role ARN embedded in the version no longer exists.

**Fix:**
```bash
aws bedrock-agent update-agent --agent-id <ID> --agent-resource-role-arn <NEW_ROLE_ARN> ...
aws bedrock-agent prepare-agent --agent-id <ID> --region ap-southeast-2
aws bedrock-agent delete-agent-alias --agent-id <ID> --agent-alias-id <OLD_ALIAS>
NEW_ALIAS=$(aws bedrock-agent create-agent-alias --agent-id <ID> --agent-alias-name live ...)
# Then update Lambda env vars + IAM policy + CFN pass 2 with new alias ID
```

## What deploy.sh Does (fully automated)

```
./scripts/deploy.sh dev <code-bucket> <connect-instance-alias>
```

1. Packages + uploads both Lambda zips to S3
2. Pass 1 CFN — creates JobsTable, Lambdas, IAM roles
3. Forces Lambda code updates
4. Creates/updates Bedrock Agent (system prompt always updated)
5. Creates/updates `JobManagement` action group (schema always updated)
6. Prepares agent + creates/reuses `Live` alias
7. Pass 2 CFN — injects Agent ID + Alias ID into Lex Lambda env vars

`deploy_tradie_agent.sh` follows the same pattern for the tradie stack.

## JobsTable Schema

Primary key: `job_id` (String, format `JOB-YYYYMMDD-XXXXXX`)

Key fields: `job_id`, `created_at`, `status`, `urgency`, `customer` (obj), `job` (obj), `notification` (obj)

`job` object: `service_type`, `problem_description`, `appointment_date` (YYYY-MM-DD), `appointment_time`

Populated later: `tradie` (obj), `summary`, `assigned_at`, `completed_at`

TTL: `expires_at` (Unix epoch, 30 days)

## Portal API Routes

```
POST /login   body: {"code": "482719"}
              → validates 6-digit code, returns profile with tradie_code (not phone)

GET  /jobs    ?tradie_code=482719
              → resolves phone from TradiesTable, scans JobsTable by tradie.phone
```

## Naming Conventions

- CFN stacks: `tradie-connect-agent-{env}`, `tradie-connect-tradie-agent-{env}`, `tradie-portal-{env}`
- Lambda — shared actions: `tradie-connect-actions-{env}`
- Lambda — customer Lex: `tradie-connect-customer-lex-{env}`
- Lambda — tradie Lex: `tradie-connect-tradie-lex-{env}`
- Lambda — portal API: `tradie-portal-api-{env}`
- Bedrock Agent — customer: `TradieConnect-CustomerAgent-{env}`
- Bedrock Agent — tradie: `TradieConnect-TradieAgent-{env}`
- Lex bot — customer: `TradieConnectBot-{env}` (pre-existing, manual)
- Lex bot — tradie: `TradieConnect-TradieBot-{env}` (CFN-managed)
- DynamoDB: `JobsTable-{env}`, `TradiesTable-{env}`
- IAM roles: `tradie-connect-actions-role-{env}`, `tradie-connect-customer-bedrock-role-{env}`, `tradie-connect-tradie-bedrock-role-{env}`
- Job IDs: `JOB-YYYYMMDD-{6 char hex uppercase}`

## Common Commands

```bash
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc
./scripts/deploy_tradie_agent.sh dev my-tradie-deploy-bucket
./scripts/deploy_portal.sh dev my-tradie-deploy-bucket

aws dynamodb scan --table-name JobsTable-dev --region ap-southeast-2 --query "Items[0]"
python scripts/manage_tradies.py list --env dev
python scripts/manage_tradies.py show-code --env dev +61411000001
```

## What NOT to Change Without Reading First

- `handler.py:_response()` — Bedrock agent response envelope shape is mandatory
- `connect_agent_system_prompt.txt` — tool call sequencing is enforced by the prompt
- `tradie_agent_system_prompt.txt` — `lookupTradieByCode` must always be called first
- `agent.yaml` IAM policy — SNS is intentionally absent; do not add it
- `deploy.sh` AGENT_MODEL — must use `au.` prefix, not bare model IDs
- Pipe-delimited `problemDescription` format — orchestrator parses `|` splits; changing the format breaks appointment storage

## How This Fits the Existing Stack

The parent stack (`tradie-connect-{env}`) already deploys `TradiesTable-{env}` and S3 buckets. These stacks are purely additive. After a call ends, the existing recordings → Transcribe → Summariser pipeline can write summaries back to `JobsTable` by matching on `contact_id`.
