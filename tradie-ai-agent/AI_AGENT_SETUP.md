# Tradie Connect — Setup Guide

## What is automated vs manual

| Step | How |
|------|-----|
| CFN stacks (Lambdas, DynamoDB, IAM roles, Lex bots) | **Automated** — deploy scripts |
| Bedrock Agent + action group + alias | **Automated** — CFN (`AWS::Bedrock::Agent` + `AWS::Bedrock::AgentAlias`) |
| System prompt storage | **Automated** — deploy scripts store prompt in SSM before CFN deploy |
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
2. Stores system prompt in SSM (`/tradie-connect/dev/customer-agent-instruction`)
3. **Single CFN deploy** — creates `JobsTable-dev`, `TripsTable-dev`, `tradie-connect-actions-dev`, `tradie-connect-customer-lex-dev`, IAM roles, `TradieConnect-CustomerAgent-dev` Bedrock Agent + alias, `TradieConnect-CustomerBot-dev` Lex bot
4. Force-updates both Lambda functions to the latest uploaded code

Re-running is safe — existing resources are updated, not recreated.

---

## Step 2 — Deploy tradie agent

```bash
./scripts/deploy_tradie_agent.sh dev my-tradie-deploy-bucket
```

What this does:
1. Resolves the shared actions Lambda ARN from the customer stack and force-updates its code
2. Stores tradie system prompt in SSM (`/tradie-connect/dev/tradie-agent-instruction`)
3. **Single CFN deploy** (`tradie_agent.yaml`) — creates `TradieConnect-TradieBot-dev` Lex bot, fulfillment Lambda, IAM roles, `TradieConnect-TradieAgent-dev` Bedrock Agent + alias with TradieManagement action group (6 functions: lookupTradieByCode, getJobsByTradie, completeJob, startTrip, endTrip, getTripLog)
4. Force-updates the tradie Lex Lambda to the latest uploaded code

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
   - Select **Amazon Lex** → bot: `TradieConnect-CustomerBot-dev`, alias: `live`
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

### Tradie agent — jobs

Call the tradie number. Say your 6-digit tradie code when prompted. Ask "what jobs do I have tomorrow?"

Check a tradie's code:
```bash
python scripts/manage_tradies.py show-code --env dev +61411000001
python scripts/manage_tradies.py list --env dev
```

### Tradie agent — travel log

The following examples show exactly how the voice conversation flows for each travel log scenario.
No trip reference numbers are required from the tradie in normal use.

---

#### Example 1 — Starting a job trip (one job today)

```
Agent:  "Welcome to Tradie Connect. Please tell me your 6-digit tradie code."
Tradie: "4 8 2 7 1 9"
Agent:  "I heard 4 8 2 7 1 9 — is that right?"
Tradie: "Yes"
        [lookupTradieByCode → returns phone_number, name "Dave"]
Agent:  "No worries Dave, how can I help you today?"

Tradie: "I'm heading to my job"
        [Agent infers: tripType = job]
        [getJobsByTradie → dateFilter=today → 1 result: JOB-20260429-A3F2B1 at 32 King St Newtown]
Agent:  "Is this for your job at 32 King Street Newtown?"
Tradie: "Yeah that's it"
        [startTrip → tripType=job, destination="32 King Street Newtown",
                     relatedJobId="JOB-20260429-A3F2B1"]
Agent:  "Done, I have started your trip log to 32 King Street Newtown. Safe travels!"
Agent:  "Is there anything else I can help you with?"
Tradie: "No that's all"
Agent:  "Cheers, have a great day!"
```

---

#### Example 2 — Ending a trip (one trip in progress)

```
Agent:  [auth as above — "No worries Dave, how can I help you today?"]

Tradie: "I've just arrived, can you stop my trip"
        [getTripLog → dateFilter=today → 1 IN_PROGRESS trip:
         TRIP-20260429-B7F9C2, destination="32 King Street Newtown"]
Agent:  "I can see your trip to 32 King Street Newtown is still running.
         Shall I end that one?"
Tradie: "Yes please"
        [endTrip → trip_id=TRIP-20260429-B7F9C2]
Agent:  "Done, I have logged the end of your trip to 32 King Street Newtown. Safe work!"
```

---

#### Example 3 — Ending a trip (multiple trips in progress)

```
Tradie: "I've arrived, end my trip"
        [getTripLog → dateFilter=today → 2 IN_PROGRESS trips]
Agent:  "I can see 2 trips still running today. Which one would you like to end?
         1 — trip to 32 King Street Newtown, started 8:15 this morning
         2 — trip to Bunnings Auburn, started 11 o'clock"
Tradie: "The Bunnings one"
        [endTrip → TRIP-20260429-D4A1E8]
Agent:  "Done, I have logged the end of your trip to Bunnings Auburn. Safe work!"
```

---

#### Example 4 — Heading to a supplier

```
Tradie: "I'm popping out to pick up some supplies"
Agent:  "Sure. Where are you heading?"
Tradie: "Reece Plumbing in Parramatta"
        [Agent infers: tripType = supplier — no job lookup needed]
        [startTrip → tripType=supplier, destination="Reece Plumbing Parramatta"]
Agent:  "Done, I have started your trip log to Reece Plumbing Parramatta."
```

---

#### Example 5 — Starting a job trip (multiple jobs today)

```
Tradie: "Starting a trip to a job"
        [getJobsByTradie → today → 2 results]
Agent:  "I can see 2 jobs on today's schedule. Which one is this trip for?
         1 — 32 King Street Newtown
         2 — 18 George Street Redfern"
Tradie: "The Redfern one"
        [startTrip → destination="18 George Street Redfern",
                     relatedJobId="JOB-20260429-C9D2E7"]
Agent:  "Done, I have started your trip log to 18 George Street Redfern."
```

---

#### Example 6 — Heading home

```
Tradie: "I'm done for the day, heading home"
        [Agent infers: tripType = home, destination = "home"]
        [startTrip → tripType=home, destination="home"]
Agent:  "Done, I have started your trip log home. Have a good evening!"
```

---

#### Example 7 — Checking today's trip log

```
Tradie: "What trips have I logged today?"
        [getTripLog → dateFilter=today → 3 trips]
Agent:  "You have 3 trips logged for today.
         First — job trip to 32 King Street Newtown. Started 8:15 am, ended 9:30 am.
         Second — supplier trip to Reece Plumbing Parramatta. Started 10:45 am, ended 11:20 am.
         Third — home trip. Started 4:30 pm, still in progress."
```

---

Verify trips were written to DynamoDB:
```bash
aws dynamodb scan --table-name TripsTable-dev --region ap-southeast-2 \
  --query "Items[*].[trip_id.S,trip_type.S,destination.S,status.S]" --output table
```

### Portal

Open the portal URL, enter a tradie code. Jobs should appear on the **My Jobs** tab with
appointment date and time. Switch to the **Trip Log** tab to see logged trips — use the
date picker to filter by day.

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
| `EventStreamError: accessDeniedException` in Lex Lambda logs | Bedrock Agent alias snapshots an old IAM role ARN (role was renamed by a CFN update) | Delete the old alias manually (`aws bedrock-agent delete-agent-alias`), then re-run the deploy script — CFN will recreate the alias with the current role |
| `no identity-based policy allows bedrock:InvokeAgent` | IAM policy on the Lex Lambda role still references the deleted alias ARN | Re-run the deploy script — CFN will update the IAM policy with the new alias ARN via `!GetAtt` |
| Lex test fails: "Access denied while invoking lambda" | Lex bot alias still wired to old/deleted Lambda name after a rename | Run `aws lexv2-models update-bot-alias` for both `live` and `TSTALIASID` aliases to point to the new Lambda ARN |
| `AccessDeniedException` on Bedrock | Bedrock execution role missing `bedrock:InvokeModel` or `bedrock:GetInferenceProfile` | Verify role policy in IAM console |
| Model `Access Denied` | Not using `au.` inference profile prefix | Check `deploy.sh` `AGENT_MODEL` — must be `au.anthropic.claude-haiku-4-5-20251001-v1:0` |
| No tradies found | `TradiesTable-dev` missing active tradies | Run `python scripts/manage_tradies.py seed --env dev` |
| Tradie code not recognised | Tradie record missing `tradie_code` field | Run `python scripts/manage_tradies.py show-code --env dev <phone>` — if blank, re-seed or manually add code |
| Portal login fails silently | JS ReferenceError (historical — now fixed) | Ensure latest `portal/index.html` is deployed |
| Appointment date/time missing on portal | Job was created before appointment fields were added | Only new job cards include these fields |
