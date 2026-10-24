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
```

A few things the tools enforce on their own, whatever the model says:

- A ticket's category comes from the stored photo analysis, and the department comes from our own table.
- A ward has to come from a lookup in the same conversation. No guessed wards.
- If the photo isn't a civic issue or the model isn't confident, the user is asked to confirm first.
- Reports of the same issue within about 60 m (or the same typed area) in 72 hours are merged into one ticket.
- There is no tool that changes a ticket's status. Only AMC staff can, through a token-protected endpoint.

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
  vision.py    Gemini image analysis
  geo.py       ward lookup and geocoding
  images.py    upload checks, GPS read, EXIF strip
  store.py     SQLite / Firestore mock of the AMC system
  data/        wards and departments
  static/      the web page
tests/
```
