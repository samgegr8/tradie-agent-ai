# Tradie Connect — AI Agent

An Amazon Connect AI system with two conversational agents: one for **customers** booking jobs, one for **tradies** checking schedules, marking jobs complete, and logging trips. Built on Amazon Bedrock Agents + AWS Lambda + Lex V2.

---

## How it works

### Customer flow

```
Caller speaks
      │
      ▼
Amazon Connect → Lex V2 (TradieConnectBot-dev — thin pass-through)
      │
      ▼
Bedrock Agent (TradieConnect-CustomerAgent-dev)
      │  tool calls in sequence
      ▼
Lambda (agent_actions)
      ├── 1. createJobCard            → writes JobsTable (appointment date/time stored)
      ├── 2. lookupAvailableTradie    → reads TradiesTable
      ├── 3. assignTradieToJob        → updates JobsTable (status=ASSIGNED)
      └── 4. logJobNotification       → stub (Twilio roadmap)
      │
      ▼
Caller hears job reference + tradie name → call ends
```

### Tradie flow

```
Tradie calls in
      │
      ▼
Amazon Connect → Lex V2 (TradieConnect-TradieBot-dev — CFN-managed)
      │
      ▼
Bedrock Agent (TradieConnect-TradieAgent-dev)
      │  tool calls in sequence
      ▼
Lambda (agent_actions — shared with customer flow)
      ├── 1. lookupTradieByCode       → authenticates via 6-digit tradie code
      ├── 2. getJobsByTradie          → returns jobs by appointment date
      ├── 3. completeJob             → marks job COMPLETED (verifies ownership)
      ├── 4. startTrip               → creates trip entry in TripsTable (type: job|supplier|home|other)
      ├── 5. endTrip                 → marks trip COMPLETED (verifies ownership)
      └── 6. getTripLog              → returns trips by date for the logbook
```

### Tradie portal

```
Tradie opens portal URL → enters 6-digit tradie code → sees assigned jobs + trip log
Portal API (Lambda) → DynamoDB scan by tradie_code → tradie.phone → JobsTable / TripsTable
```

---

## Repository layout

```
lambdas/
  agent_actions/
    handler.py          Lambda entry point — routes Bedrock events to orchestrator
    orchestrator.py     7 tool implementations (customer + tradie)
    requirements.txt
  lex_fulfillment/
    handler.py          Forwards Lex utterances to Bedrock Agent (shared by both bots)
  portal_api/
    handler.py          REST API backing the tradie self-service portal

cloudformation/
  agent.yaml            Customer stack — JobsTable, shared actions Lambda, IAM roles, Lex Lambda
  tradie_agent.yaml     Tradie stack — tradie Lex bot (CFN-managed), Lex Lambda, IAM roles
  portal.yaml           Portal stack — S3 website bucket, API Gateway HTTP API, portal Lambda

scripts/
  deploy.sh                  Customer agent — full automated deploy
  deploy_tradie_agent.sh     Tradie agent — full automated deploy
  deploy_portal.sh           Portal website deploy
  manage_tradies.py          Seed / list / add / update / delete / show-code for tradies

portal/
  index.html            Tradie self-service portal (static HTML, injected with API URL at deploy)

connect_agent_system_prompt.txt     Customer agent system prompt
tradie_agent_system_prompt.txt      Tradie agent system prompt
AI_AGENT_SETUP.md                   Deployment guide
```

---

## Quick start

### 1. Prerequisites

- AWS CLI configured for `ap-southeast-2`
- An Amazon Connect instance (you need the instance alias)
- S3 bucket for Lambda code:
  ```bash
  aws s3 mb s3://my-tradie-deploy-bucket --region ap-southeast-2
  ```

### 2. Seed tradie data

```bash
python scripts/manage_tradies.py seed --env dev
python scripts/manage_tradies.py list --env dev   # shows tradie codes
```

### 3. Deploy customer agent

```bash
./scripts/deploy.sh dev my-tradie-deploy-bucket tradie-poc
```

### 4. Deploy tradie agent

```bash
./scripts/deploy_tradie_agent.sh dev my-tradie-deploy-bucket
```

### 5. Deploy portal

```bash
./scripts/deploy_portal.sh dev my-tradie-deploy-bucket
```

### 6. Wire Connect contact flows (one-time, manual)

**Customer flow** — Get customer input → Lex: `TradieConnectBot-dev`, alias `live`

**Tradie flow** — Get customer input → Lex: `TradieConnect-TradieBot-dev`, alias `live`

See [AI_AGENT_SETUP.md](AI_AGENT_SETUP.md) for the full guide.

---

## Lambda tools

### Customer agent — `JobManagement` action group

| # | Tool | What it does |
|---|------|-------------|
| 1 | `createJobCard` | Writes job record to `JobsTable`. `problemDescription` is pipe-delimited: `description\|YYYY-MM-DD\|time\|urgency` |
| 2 | `lookupAvailableTradie` | Scans `TradiesTable` for an active tradie matching trade type + suburb |
| 3 | `assignTradieToJob` | Links tradie to job, sets `status=ASSIGNED` |
| 4 | `logJobNotification` | Stub — logs notification intent. Twilio integration is roadmap |

Tools run in order 1 → 2 → 3 → 4. The `job_id` from tool 1 is required by tool 3.

### Tradie agent — `TradieManagement` action group

| # | Tool | What it does |
|---|------|-------------|
| 1 | `lookupTradieByCode` | Authenticates tradie by 6-digit code, returns `phone_number` |
| 2 | `getJobsByTradie` | Returns jobs by `appointment_date` for "today", "tomorrow", or ISO date |
| 3 | `completeJob` | Marks job `COMPLETED`, verifies tradie ownership |
| 4 | `startTrip` | Creates trip entry in `TripsTable` (`job`\|`supplier`\|`home`\|`other`); returns `trip_id` |
| 5 | `endTrip` | Marks trip `COMPLETED`, verifies tradie ownership |
| 6 | `getTripLog` | Returns trips for a tradie on a given date, filtered by `started_at` |

---

## Appointment scheduling

When a customer calls, the agent collects the preferred **appointment date** (converted to ISO `YYYY-MM-DD`) and **time slot** (e.g. "morning", "after 2pm"). These are packed into `problemDescription` as a pipe-delimited string to stay within Bedrock's 5-parameter limit:

```
"Leaking tap under kitchen sink|2026-04-25|morning|standard"
```

The orchestrator splits this on `|` and stores `appointment_date`, `appointment_time`, `urgency`, and `problem_description` as separate fields in DynamoDB. The tradie portal displays them, and the tradie agent filters jobs by `appointment_date` when a tradie asks "what jobs do I have tomorrow?"

---

## Tradie code authentication

Every tradie has a unique 6-digit `tradie_code` (e.g. `482719`). This code:
- Is spoken aloud when a tradie calls in → agent calls `lookupTradieByCode`
- Is entered at the portal login screen
- Never exposes the tradie's phone number to the browser

```bash
python scripts/manage_tradies.py show-code --env dev +61411000001
python scripts/manage_tradies.py list --env dev   # shows CODE column
```

---

## Tradie portal

Static website hosted on S3. Tradies log in with their 6-digit code and see:
- **My Jobs tab** — all assigned jobs including appointment date, time, customer details, address, and job description
- **Trip Log tab** — all recorded trips with destination, type, start/end time, and related job ID; filterable by date

Portal URL (dev): `http://tradie-portal-dev-{account}.s3-website-ap-southeast-2.amazonaws.com`

---

## Managing tradies

```bash
python scripts/manage_tradies.py seed      --env dev          # seed sample data
python scripts/manage_tradies.py list      --env dev          # list all (shows tradie code)
python scripts/manage_tradies.py add       --env dev          # add interactively (auto-generates code)
python scripts/manage_tradies.py update    --env dev +61411000001
python scripts/manage_tradies.py toggle    --env dev +61411000001
python scripts/manage_tradies.py delete    --env dev +61411000001
python scripts/manage_tradies.py show-code --env dev +61411000001
```

---

## Resource naming

| Resource | Name pattern |
|----------|-------------|
| CFN stack — customer | `tradie-connect-agent-{env}` |
| CFN stack — tradie agent | `tradie-connect-tradie-agent-{env}` |
| CFN stack — portal | `tradie-portal-{env}` |
| Bedrock Agent — customer | `TradieConnect-CustomerAgent-{env}` |
| Bedrock Agent — tradie | `TradieConnect-TradieAgent-{env}` |
| Lambda — shared actions | `tradie-connect-actions-{env}` |
| Lambda — customer Lex | `tradie-connect-customer-lex-{env}` |
| Lambda — tradie Lex | `tradie-connect-tradie-lex-{env}` |
| Lambda — portal API | `tradie-portal-api-{env}` |
| Lex bot — customer | `TradieConnectBot-{env}` (pre-existing, manual) |
| Lex bot — tradie | `TradieConnect-TradieBot-{env}` (CFN-managed) |
| DynamoDB | `JobsTable-{env}`, `TradiesTable-{env}`, `TripsTable-{env}` |

---

## AWS services

| Service | Purpose |
|---------|---------|
| Amazon Connect | Inbound voice, contact flows |
| Amazon Lex V2 | Thin pass-through from Connect to Bedrock Agent |
| Amazon Bedrock Agents | Conversational AI with tool-calling (Claude Haiku 4.5) |
| AWS Lambda (Python 3.12) | Tool actions + Lex fulfillment + portal API |
| DynamoDB `JobsTable-{env}` | Job cards (PK: `job_id`) |
| DynamoDB `TradiesTable-{env}` | Tradie roster (PK: `phone_number`) |
| DynamoDB `TripsTable-{env}` | Travel log entries (PK: `trip_id`) |
| S3 | Recordings, transcripts, portal website |
| API Gateway HTTP API | Portal REST API |
| CloudFormation | All infrastructure as code |

---

## Estimated monthly cost (50 calls/day, ~3 min avg)

| Service | AUD/month |
|---------|-----------|
| Connect voice channel ($0.018/min) | ~$129 |
| AU DID number (~$1.20/day) | ~$57 |
| AU inbound telephony (~$0.013/min) | ~$94 |
| Amazon Transcribe (real-time + post-call batch) | ~$171 |
| Bedrock Agents — Claude Haiku 4.5 | ~$9 |
| DynamoDB + Lambda + S3 + API GW | ~$6 |
| **Total** | **~$466 AUD/month** |

---

## Travel log

A tradie can call in to start or end a trip at any point during the day. The agent guides them through:

1. **Start a trip** — tradie says where they're heading; agent asks for trip type (job / supplier / home / other) and optionally the related job ID, then calls `startTrip` and reads back the trip reference.
2. **End a trip** — tradie gives the trip reference; agent calls `endTrip` and confirms.
3. **Check the log** — tradie asks for their trips on a given day; agent calls `getTripLog` and reads each entry.

### TripsTable schema

| Field | Type | Notes |
|-------|------|-------|
| `trip_id` | String (PK) | `TRIP-YYYYMMDD-XXXXXX` |
| `tradie_phone` | String | FK to TradiesTable |
| `trip_type` | String | `job` \| `supplier` \| `home` \| `other` |
| `destination` | String | Free text — address or supplier name |
| `related_job_id` | String | Optional — populated when `trip_type=job` |
| `started_at` | String | ISO 8601 timestamp |
| `ended_at` | String \| null | ISO 8601 timestamp; null until ended |
| `status` | String | `IN_PROGRESS` \| `COMPLETED` |
| `expires_at` | Number | Unix epoch TTL (30 days) |

GSI `TradiePhoneIndex` (PK: `tradie_phone`, SK: `started_at`) supports portal date-range queries.

The portal **Trip Log tab** shows the same data with a date picker to filter by day.

---

## Roadmap

- **Twilio SMS notification** — integration point: `orchestrator.py:log_job_notification()`. Job cards already track `notification.status=PENDING`. A future EventBridge rule or DynamoDB Stream can dispatch via Twilio without changing the agent or contact flow.
- **OTP portal login** — replace 6-digit tradie code with Twilio OTP when SMS is integrated.
- **Distance/odometer capture** — extend `startTrip`/`endTrip` with odometer readings for ATO-compliant logbooks.

---

## Key constraints

- Python 3.12 only
- `boto3` only — no third-party HTTP clients
- No SNS in this stack — use Twilio when notification is ready
- Bedrock action group schema: max 5 parameters per function
- TradiesTable uses full scan — acceptable at 50 calls/day; add a GSI on `trade_type` above ~500 calls/day
