# Live Demo Script (10 minutes)

**Setup before the meeting**
- Stack deployed (`deploy.sh`, `deploy_tradie_agent.sh`, `deploy_portal.sh`).
- Tradies seeded: `python scripts/manage_tradies.py seed --env dev`
- Note a tradie code: `python scripts/manage_tradies.py list --env dev`
- Have the booking number, crew number and portal URL ready. Put the call on speaker.

## Act 1 — "You're on the tools" (4 min)
Ask the prospect to play the customer, or call yourself.
1. Call the **booking line**. Describe a real problem: "My kitchen tap is leaking under the sink."
2. Give name, number, an address in a suburb a seeded tradie covers, "tomorrow at 9", "it can wait".
3. Point out: one question at a time, number read back digit by digit, no hold music.
4. The agent reads back a job reference and the tradie's name.

**Say:** "That call happened while you were under a house. You didn't touch your phone, and the job's booked."

## Act 2 — "The next morning" (3 min)
1. Call the **crew line**. Say the tradie code.
2. "What jobs have I got tomorrow?" → hears the job just booked.
3. "I'm heading to my job" → agent confirms the address, starts a trip.
4. "I've arrived" → trip ends.
5. "Mark the job done" → give the reference → completed.

**Say:** "Your crew never opens an app. They just talk to it from the ute."

## Act 3 — "The office view" (2 min)
1. Open the **portal**, log in with the same code.
2. Show **My Jobs** (now completed) and **Trip Log** with timestamps.

**Say:** "That's your job history and the start of your logbook, done by voice."

## Close (1 min)
"It costs about 10 cents a call minute to run. What would one extra job a week be worth to you?" → offer the 30-day pilot (`pilot-offer.md`).

## If something goes wrong
- Agent goes silent / access denied → see CLAUDE.md "Alias AccessDenied Recovery".
- No tradie found → that's a feature: show the 2-hour callback fallback.
