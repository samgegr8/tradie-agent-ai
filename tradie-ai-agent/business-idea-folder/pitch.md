# Tradie Connect — The Pitch

## One-liner
An AI phone receptionist for trade businesses: it answers every call 24/7, books the job, writes the job card and assigns the right tradie. Tradies call a second line to hear their schedule, close jobs and keep a travel logbook. No app to install.

## 30-second elevator pitch
> When you're under a house or up a ladder, you can't pick up the phone, and that caller rings the next plumber on Google. Tradie Connect answers for you, any hour, in a natural Aussie voice. It takes the customer's details, the problem, the address and when they want you there, then books it and assigns the right tradie by trade and suburb. The customer hangs up with a job reference. Your tradies call in to hear tomorrow's jobs, mark them done, and log their trips for tax time. It runs on AWS in Sydney for well under a dollar a call.

## The problem
1. **On the tools, off the phone.** Missed calls go to competitors.
2. **After-hours calls hit voicemail.** Emergencies happen at night and on weekends; a receptionist or answering service is expensive for a small crew.
3. **Admin eats the evening.** Writing up jobs, dispatching, and keeping a vehicle logbook all happen after a full day's work.

## The solution — three parts (all built today)

### 1. Booking line (customers)
- AI agent collects: name, callback number, trade, problem, full address, preferred date & time, urgency.
- Creates a job card, finds a tradie matching **trade + suburb**, assigns them.
- Reads back the job reference and the tradie's name.
- If no tradie matches: job is still logged; caller is told someone will call back within 2 hours.

### 2. Crew line (tradies)
- Tradie authenticates with a 6-digit code.
- "What jobs have I got tomorrow?" → reads jobs by appointment date.
- "Mark it done" → completes the job (only the assigned tradie can).
- "Heading to Bunnings Auburn" / "I've arrived" → starts / ends a logged trip (job, supplier, home, other).
- "Read me today's trips" → reads back the trip log.

### 3. Tradie portal (web)
- Log in with the same 6-digit code.
- **My Jobs**: date, time, customer, address, description.
- **Trip Log**: all trips, filterable by day.
- Phone numbers are never exposed to the browser.

## Why it wins
- **Books jobs, not messages.** The call ends with a booked, assigned job.
- **Built for Australia.** AU English voice; reads postcodes/phone numbers digit by digit; understands trade suppliers.
- **Data sovereignty.** Runs in AWS ap-southeast-2 (Sydney), can deploy into the customer's own AWS account.
- **No app for the crew.** Phone call or web page.
- **Logbook head start.** Time-stamped trips by voice.
- **Fast setup.** Scripted deploy + one-time phone number wiring.

## Economics
Running cost (from the project's AWS model, 50 calls/day × ~3 min):

| Service | AUD/month |
|---|---:|
| Connect voice channel | ~$129 |
| AU phone number | ~$57 |
| AU inbound telephony | ~$94 |
| Transcription | ~$171 |
| AI model (Claude Haiku 4.5) | ~$9 |
| DynamoDB + Lambda + S3 + API GW | ~$6 |
| **Total** | **~$466** |

≈ 10c per call minute all-in. The AI is <2% of cost.

### Suggested pricing (draft — validate)
| Tier | Price | Who | Approx. AWS cost at full use |
|---|---|---|---|
| Solo | $249/mo | 1–2 tradies, ≤300 calls | ~$140 |
| Crew | $599/mo | ≤10 tradies, ≤1,500 calls | ~$466 |
| Multi-site | Quote | Franchises, builders, facilities | Custom |

Plus an optional one-off setup fee (roster onboarding, phone number, contact flows).

### Illustrative ROI (replace with the prospect's numbers)
3 missed calls/week caught × 1-in-3 conversion × $350 average job ≈ **$1,500/month** recovered work vs $249–$599 subscription.

## Objections & answers
- **"Customers will know it's AI."** Most won't mind if the job is booked first call.
- **"Does it quote?"** No, by design. The tradie quotes.
- **"No tradie for that suburb?"** Job still logged; 2-hour callback promise.
- **"Can it transfer to me?"** Not yet. Fully automated today; transfer is a sensible add-on.
- **"Does my tradie get a text?"** Not yet. SMS (Twilio) is the next roadmap item; notifications are already tracked as pending.
- **"Is the logbook ATO-compliant?"** Records purpose, destination, times. Odometer capture is on the roadmap; position as a logbook helper for now.

## Be upfront about (current limits)
- No SMS/notifications yet (stub in place).
- No live transfer to a human.
- No quoting, payments or calendar sync.
- Tradie matching is trade + suburb only (no live availability/calendar check).
- Portal login is a fixed 6-digit code (OTP on roadmap).

## Roadmap
1. ✅ Booking line, crew line, portal, trip log
2. ⏭ SMS alerts to tradie and customer (Twilio)
3. OTP portal login
4. Odometer capture for full ATO logbook

## Target customers
- Sole-trader and small-crew plumbers, electricians, carpenters, painters, tilers
- Emergency trades with after-hours demand (plumbing, electrical, locksmith)
- Multi-trade handyman / maintenance businesses dispatching across suburbs
- Later: franchise networks, strata / facilities management
