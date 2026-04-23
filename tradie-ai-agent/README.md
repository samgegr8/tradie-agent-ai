# Tradie Connect — AI Agent

An Amazon Connect AI agent that answers inbound calls, collects job details conversationally, creates a job card in DynamoDB, and matches an available tradie. Built on Amazon Bedrock Agents + AWS Lambda.

---

## How it works

```
Caller speaks
      │
      ▼ (managed by Connect)
Connect STT  [Transcribe Streaming]
      │  text utterance
      ▼
Amazon Lex V2  (TradieConnectBot-dev — thin pass-through)
      │  forwards to
      ▼
Amazon Bedrock Agent  ←── connect_agent_system_prompt.txt (enforces tool order)
      │
      │  tool calls in sequence
      ▼
AWS Lambda  (handler.py → orchestrator.py)
      ├── 1. createJobCard            → writes JobsTable
      ├── 2. lookupAvailableTradie    ← reads TradiesTable
      ├── 3. assignTradieToJob        → updates JobsTable (status=ASSIGNED)
      └── 4. logJobNotification       → stub (Twilio roadmap)
      │
      │  text response
      ▼
Connect TTS  [Amazon Polly, managed by Connect]
      │
      ▼
Caller hears job reference + tradie name  →  call ends

─── Post-call (runs independently) ────────────────────────────────────────────
Recording → S3 → Transcribe batch → Summariser Lambda → JobsTable.summary
```

---

## Repository layout

```
lambdas/agent_actions/
  handler.py          Lambda entry point — routes Bedrock action events
  orchestrator.py     Tool implementations (3 active + 1 stub)
  requirements.txt

lambdas/lex_fulfillment/
  handler.py          Forwards Lex utterances to Bedrock Agent

cloudformation/
  agent.yaml          JobsTable, Lambdas, IAM roles, Bedrock execution role

scripts/
  deploy.sh           Single command — builds, deploys CFN, creates Bedrock Agent + action group + alias
  manage_tradies.py   Seed / list / add / update / delete tradie records

connect_agent_system_prompt.txt  System prompt loaded into Bedrock Agent by deploy.sh
AI_AGENT_SETUP.md                Deployment guide — what is automated vs manual
```

---

## Quick start

### 1. Prerequisites

- AWS CLI configured for `ap-southeast-2`
- An Amazon Connect instance with a Lex V2 bot (`TradieConnectBot-dev`) already created
- An S3 bucket for Lambda code:
  ```bash
  aws s3 mb s3://my-tradie-deploy-bucket --region ap-southeast-2
  ```

### 2. Seed tradie data

```bash
python scripts/manage_tradies.py seed --env dev
python scripts/manage_tradies.py list --env dev
```

### 3. Deploy everything

```bash
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc
```

This single command:
- Packages and uploads both Lambda zips
- Deploys the CloudFormation stack (Lambdas, DynamoDB, IAM roles)
- Creates the Bedrock Agent with the correct model and system prompt
- Creates the `JobManagement` action group with all 4 functions
- Prepares the agent and creates the `live` alias
- Re-deploys CFN to inject Agent ID + Alias ID into the Lex Lambda

Re-running is safe — existing resources are skipped, only Lambda code is updated.

### 4. Wire the Connect contact flow (one-time, manual)

In **Amazon Connect console → Routing → Contact flows** → open your inbound flow:

1. **Set voice** → `English (AU)`, Voice: `Olivia`
2. **Get customer input** → Lex bot: `TradieConnectBot-dev`, alias: `live`, intent: `FallbackIntent`
3. **Disconnect** block on both outputs
4. Save and publish

See [AI_AGENT_SETUP.md](AI_AGENT_SETUP.md) for the full guide.

---

## The 4 Lambda tools

| # | Tool | What it does |
|---|------|-------------|
| 1 | `createJobCard` | Writes a new job record to `JobsTable-{env}`. Returns `job_id`. |
| 2 | `lookupAvailableTradie` | Scans `TradiesTable-{env}` for an active tradie matching trade type and suburb. |
| 3 | `assignTradieToJob` | Links the tradie to the job card, sets `status=ASSIGNED`. |
| 4 | `logJobNotification` | Stub — logs notification intent. Twilio SMS integration is roadmap. |

Tools must run in order 1 → 2 → 3 → 4. The `job_id` from tool 1 is required by tool 3.

---

## Managing tradies

```bash
python scripts/manage_tradies.py seed   --env dev   # seed sample data
python scripts/manage_tradies.py list   --env dev   # list all tradies
python scripts/manage_tradies.py add    --env dev   # add interactively
python scripts/manage_tradies.py update --env dev +61411000001
python scripts/manage_tradies.py toggle --env dev +61411000001
python scripts/manage_tradies.py delete --env dev +61411000001
```

---

## Estimated monthly cost (50 calls/day, ~3 min avg)

| Service | AUD/month |
|---------|-----------|
| Connect voice channel ($0.018/min) | ~$129 |
| AU DID number (~$1.20/day) | ~$57 |
| AU inbound telephony (~$0.013/min) | ~$94 |
| Amazon Transcribe (real-time STT + post-call batch) | ~$171 |
| Bedrock Agents — Claude Haiku 4.5 (orchestration) | ~$9 |
| Bedrock — Claude Haiku 4.5 (transcript summariser) | ~$3 |
| DynamoDB + Lambda + S3 | ~$6 |
| **Total** | **~$469 AUD/month** |

> Bedrock Agents (token-based, ~$9/month) is used instead of Connect's AI self-service add-on (~$57/month), saving ~$48 AUD/month at this volume.

---

## AWS services

| Service | Purpose |
|---------|---------|
| Amazon Connect | Inbound voice, contact flow |
| Amazon Lex V2 | Thin pass-through from Connect to Bedrock Agent |
| Amazon Bedrock Agents | Conversational AI with tool-calling |
| AWS Lambda (Python 3.12) | Executes tool actions + Lex fulfillment |
| DynamoDB `JobsTable-{env}` | Job cards (PK: `job_id`) |
| DynamoDB `TradiesTable-{env}` | Tradie roster (PK: `phone`) |
| Amazon Transcribe | Call recording transcription |
| S3 | Recordings and transcripts (30-day TTL) |
| CloudFormation | Infrastructure as code |

---

## Roadmap

- **Twilio SMS notification** — integration point is `orchestrator.py:log_job_notification()`. Job cards already track `notification.status=PENDING`. A future EventBridge rule or DynamoDB Stream can poll for pending records and dispatch via Twilio without changing the agent or contact flow.

---

## Key constraints

- Python 3.12 only
- `boto3` only — no third-party HTTP clients
- No SNS in this stack — use Twilio when notification is ready
- TradiesTable uses a full scan — acceptable at 50 calls/day; add a GSI on `trade_type` above ~500 calls/day
