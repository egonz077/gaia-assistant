# WhatsApp Real-Estate Assistant

Personal agent: meeting notes (typed or photographed) in via WhatsApp, summaries +
lead tracking + follow-up nudges out. Single-user by design.

## Architecture

```
WhatsApp ──webhook──▶ FastAPI ──▶ Claude (tools) ──▶ Postgres + pgvector
    ▲                                                    │
    └────── daily cron (follow-up nudges) ◀──────────────┘
```

- **main.py** — webhook handler + agent tool-use loop
- **tools.py** — save_meeting, search_memory, query_leads, update_lead
- **db.py** — Postgres layer, Voyage embeddings, profile merging
- **followup_cron.py** — 8am daily nudge for due leads/commitments
- **schema.sql** — contacts (with rolling profiles), leads, meetings, commitments, memory_chunks

## Deploy (Hetzner CX22 or any Ubuntu box)

1. **VM:** Ubuntu 24.04, install Docker (`curl -fsSL https://get.docker.com | sh`).
2. **DNS:** point `agent.yourdomain.com` A-record at the VM IP. Caddy auto-provisions TLS.
3. **Meta setup** (business.facebook.com → create app → WhatsApp):
   - Get a phone number (can't be one already on personal WhatsApp — a cheap
     eSIM or Twilio number works), note the Phone Number ID.
   - Generate a permanent access token (Business Settings → System Users).
   - Webhook: URL `https://agent.yourdomain.com/webhook`, verify token = your
     `WA_VERIFY_TOKEN`, subscribe to `messages`.
4. **App:**
   ```bash
   git clone <this repo> && cd wife-agent
   cp .env.example .env   # fill in
   docker compose up -d --build
   ```
5. Text the number from her phone. First message creates the thread.

## Important WhatsApp constraint

The Cloud API has a **24-hour customer service window**: the bot can only send
free-form messages within 24h of *her* last message. The daily cron breaks this
if she goes quiet for a day. Fix: register a **message template** (e.g. a
"daily digest" template) in Meta Business Manager and have the cron fall back
to the template when outside the window. Templates need one-time approval,
usually fast for utility templates.

## v2 ideas

- Google Calendar tool (propose + create events, confirm-before-send)
- Voice notes: WhatsApp audio → transcription → same pipeline
- Weekly profile consolidation pass (rewrite `contacts.profile` cleanly)
- Per-contact memory files instead of append-only profile strings
