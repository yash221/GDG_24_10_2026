# Civic Complaint Agent

Report a pothole, garbage, a broken streetlight or waterlogging in Ahmedabad with one photo.
The agent works out what the problem is, finds the AMC ward and department, files the complaint
and gives you a tracking ID. Later you just ask "what's the status?".

Built for a GDG hackathon (Agents track) with Google ADK and Gemini.

> The AMC system is a mock. Wards, departments and tickets live in a local SQLite file
> (or Firestore when deployed). Nothing is sent to a real municipal portal.

## How it works

```
photo (+ optional area name or device location)
   │
   ▼
ADK agent (Gemini) ──calls──►  analyze_photo            Gemini vision, structured JSON
                               get_photo_location       GPS from the photo or the phone
                               find_ward_by_coordinates nearest AMC ward
                               find_ward_by_area        alias match, then Maps Geocoding
                               get_department           category → department + SLA
                               file_complaint           creates the ticket, merges duplicates
                               get_complaint_status     read-only status lookup
                               draft_social_post        X / Instagram post with AMC tags
                               get_my_rewards           points, badges, recent awards
```

A few things the tools enforce on their own, whatever the model says:

- A ticket's category comes from the stored photo analysis, and the department comes from our own table.
- A ward has to come from a lookup in the same conversation. No guessed wards.
- If the photo isn't a civic issue or the model isn't confident, the user is asked to confirm first.
- Reports of the same issue within about 60 m (or the same typed area) in 72 hours are merged into one ticket.
- There is no tool that changes a ticket's status. Only AMC staff can, through a token-protected endpoint.

## Photo trust score

No single check proves a photo is real, so each report gets a 0-100 score from several signals:

| Signal | Effect |
|---|---|
| Camera make/model in the photo's metadata | +20 |
| Taken in the last 2 days / more than a month old | +15 / -10 |
| Photo has its own GPS | +15 |
| Photo GPS more than 2 km from the phone's current location | -25 |
| Metadata names an AI image tool (Midjourney, Stable Diffusion, ...) | -60 |
| Gemini: real street photo / screenshot or photo of a screen / AI-looking | +10 / -30 / -50 |
| Same picture already used in another report (image fingerprint) | -40 |

Starting from 40, a score of 50 or more is **trusted**. Lower scores are still filed but marked
**needs verification** and earn no points, so genuine reports from older phones aren't blocked.

## Civic points

Points reward outcomes, not uploads, so fake photos don't pay:

| Action | Points |
|---|---|
| First to report an issue (trusted photo) | 20 |
| Confirming someone else's report (trusted photo) | 5 |
| AMC marks your reported issue resolved | 40 |

Each award is given once per person, ticket and reason, with a cap of 100 points a day. Badges
(First Report, Road Guardian, Swachh Nagrik, Community Voice, Problem Solver) and an anonymous
leaderboard keep people coming back. Points are only granted by server-side checks; the agent has
no tool that hands them out. Real rewards would need phone OTP login, since a browser session can
be reset.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add your Gemini API key
uvicorn app.main:get_app --factory --reload
```

Open http://localhost:8000.

Move a ticket along during a demo:

```bash
curl -X POST localhost:8000/api/admin/tickets/AMC-XXXXXX/status \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"status": "In Progress", "note": "Road team assigned"}'
```

Statuses: `Filed`, `Assigned`, `In Progress`, `Resolved`.

## Tests

```bash
pytest
```

Gemini is replaced with a fake in tests, so no API key is needed. The suite covers the tools,
the API flow and the security checks below.

## Security

| Risk | What we do |
|---|---|
| Guessing other people's tickets | Random IDs (`AMC-` + 6 chars, ~1 billion combinations), public view hides location and session |
| Prompt injection (in chat or text inside a photo) | Model output is never trusted for category, department or ward; no status-changing tool |
| Malicious uploads | 8 MB cap, real image check by content, JPEG/PNG/WEBP only, decompression-bomb guard |
| Location privacy | GPS is read once, then the photo is re-encoded with all EXIF removed; random file names |
| SQL injection | Parameterised queries only, strict format check on ticket IDs |
| Spam | Per-session limits on reports and chat, per-IP limit on new sessions |
| Admin endpoint | Disabled unless `ADMIN_TOKEN` is set, constant-time token compare |
| XSS / clickjacking | Strict CSP, no inline scripts, all server text rendered with `textContent`, `X-Frame-Options: DENY` |
| Leaking internals | Generic error messages, API docs and OpenAPI schema turned off |
| Rate-limit bypass via spoofed IPs | Behind Cloud Run only the last `X-Forwarded-For` hop (added by Google's proxy) is trusted |
| Huge request bodies | Rejected by `Content-Length` before the multipart parser touches them |

Known item: `pip-audit` flags `opentelemetry-api` 1.44.0 (CVE-2026-114603, fixed in 1.45.1).
It comes in through `google-adk`, which currently caps it at 1.44, and this app doesn't
configure any telemetry exporter. Upgrade once ADK allows it.

## Deploy on Vercel

Import the GitHub repo in Vercel (framework preset: Other) and add these environment variables:
`GOOGLE_API_KEY`, `GEMINI_MODEL`, `VISION_MODEL`, `ADMIN_TOKEN`. `vercel.json` routes every request
to `api/index.py`. By default the demo keeps its SQLite file in `/tmp`, which resets when the
instance restarts; set `STORE=firestore` and `GOOGLE_CREDENTIALS_B64` (a base64 service account
key with the Datastore User role) to keep tickets permanently.

## Deploy (Cloud Run + Firestore)

```bash
gcloud run deploy civic-agent --source . --region asia-south1 \
  --set-env-vars GEMINI_MODEL=gemini-3.5-flash-lite,ADMIN_TOKEN=<long-random>,ALLOWED_ORIGINS=<service-url> \
  --set-secrets GOOGLE_API_KEY=gemini-key:latest
```

The container defaults to `STORE=firestore`. Create a Firestore database in Native mode first
and give the Cloud Run service account the Datastore User role.

## Scaling notes

- The API keeps no per-user state in memory except the agent's chat context. Tickets, photos'
  metadata and each user's latest ticket are in the database, so instances can be added freely.
- For many instances: ADK's `VertexAiSessionService` for chat context, Redis for the rate
  limiter, Cloud Storage for photos.
- Ward lookup uses ward centre points. Swapping in real ward boundaries (PostGIS or a GeoJSON
  point-in-polygon check) only touches `app/geo.py`.
- New cities or departments are a data change in `app/data/`, not a code change.

## Project layout

```
app/
  main.py      API routes, validation, rate limits, security headers
  agent.py     ADK agent and runner
  tools.py     the agent's tools
  trust.py     photo trust score
  rewards.py   civic points, badges, leaderboard
  vision.py    Gemini image analysis
  geo.py       ward lookup and geocoding
  images.py    upload checks, GPS read, EXIF strip
  store.py     SQLite / Firestore mock of the AMC system
  data/        wards and departments
  static/      the web page
tests/
```
