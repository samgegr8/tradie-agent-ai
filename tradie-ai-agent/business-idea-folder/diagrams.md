# Tradie Connect — Diagrams

Mermaid diagrams. They render on GitHub, Notion, Obsidian, and mermaid.live (export to PNG/SVG for slides).

## 1. System overview

```mermaid
flowchart LR
  C([Customer calls<br/>booking line]) --> AC[Amazon Connect<br/>phone number]
  T([Tradie calls<br/>crew line]) --> AC
  AC --> LX[Amazon Lex<br/>speech to text]
  LX --> CA[Customer AI agent<br/>Claude on Bedrock]
  LX --> TA[Tradie AI agent<br/>Claude on Bedrock]
  CA --> L[Actions service<br/>AWS Lambda]
  TA --> L
  L --> J[(Jobs)]
  L --> R[(Tradie roster)]
  L --> TR[(Trip log)]
  P([Tradie or office<br/>opens portal]) --> W[Portal website S3]
  W --> API[Portal API] --> J
  API --> TR
  API --> R
```

## 2. A booking, step by step

```mermaid
sequenceDiagram
  participant Cu as Customer
  participant Ag as AI agent
  participant Sy as Tradie Connect
  participant Td as Tradie
  Cu->>Ag: Calls the business number
  Ag->>Cu: Asks name, number, trade, problem, address, time, urgency
  Cu->>Ag: Answers one question at a time
  Ag->>Sy: 1. createJobCard
  Sy-->>Ag: Job reference (JOB-YYYYMMDD-XXXXXX)
  Ag->>Sy: 2. lookupAvailableTradie (trade + suburb)
  Sy-->>Ag: Matching tradie
  Ag->>Sy: 3. assignTradieToJob
  Ag->>Cu: Reads back reference + tradie name
  Td->>Sy: Next morning: "What jobs have I got today?"
  Sy-->>Td: Address, time, problem
```

## 3. A tradie's day on the crew line

```mermaid
flowchart TD
  A[Call crew line] --> B[Say 6-digit code]
  B --> C{Code valid?}
  C -- no --> B
  C -- yes --> D[Hi Dave, how can I help?]
  D --> E[Check jobs<br/>today / tomorrow / date]
  D --> F[Start trip<br/>job / supplier / home / other]
  D --> G[End trip<br/>'I've arrived']
  D --> H[Complete job]
  D --> I[Read trip log]
  E & F & G & H & I --> J[Anything else?]
  J --> D
```

## 4. Job lifecycle

```mermaid
stateDiagram-v2
  [*] --> NEW: Customer call - createJobCard
  NEW --> ASSIGNED: Tradie matched by trade + suburb
  NEW --> CALLBACK: No tradie found - 2h callback promise
  ASSIGNED --> COMPLETED: Tradie says "job done"
  COMPLETED --> [*]
```

## 5. Value map (for slides)

```mermaid
flowchart LR
  subgraph Pain
    P1[Missed calls]
    P2[After-hours voicemail]
    P3[Evening admin]
    P4[Logbook at tax time]
  end
  subgraph Tradie Connect
    S1[24/7 AI booking line]
    S2[Auto job card + assignment]
    S3[Crew voice line]
    S4[Voice trip log + portal]
  end
  subgraph Outcome
    O1[More booked jobs]
    O2[Less admin]
    O3[Tax-time records]
  end
  P1 --> S1 --> O1
  P2 --> S1
  P3 --> S2 --> O2
  P3 --> S3 --> O2
  P4 --> S4 --> O3
```
