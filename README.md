# PlaceMate

PlaceMate is a responsive placement-preparation web app that links a student's semester curriculum to skill gaps, realistic technical interview practice, and evidence-based mentor recommendations.

## Run locally

Python 3.10 or later is required.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set a unique `SECRET_KEY` in `.env`. To enable Gemini, set `GEMINI_API_KEY`. The key is read only by the Flask backend. The app works without it using deterministic interview and strategy fallbacks. Start the server:

```sh
set -a; . ./.env; set +a
python app.py
```

Open http://127.0.0.1:5000. Create an account or choose “Continue with a demo workspace”; each demo receives an isolated session. SQLite data is saved to `instance/placemate.db` (override with `PLACEMATE_DB`). Set `DATABASE_URL` to use PostgreSQL instead.

## Deploy

Vercel runs the Flask app as a Python Function and serves files in `public/` from its CDN. Connect a PostgreSQL database through the Vercel Marketplace (Neon is supported) so user and interview data persists across function instances. Vercel supplies `DATABASE_URL` when the database is linked. Add `SECRET_KEY` and `GEMINI_API_KEY` in the Vercel project's server-side environment settings for Production and Preview. Keep the Gemini key sensitive; it is never placed in frontend files. `.vercelignore` excludes local environment files and SQLite databases from uploads.

`render.yaml` is retained for a same-origin Render deployment. It can use either SQLite with a persistent disk or a managed PostgreSQL database configured as `DATABASE_URL`.

## Features

- Branch-aware 8-semester reference curriculum and profile intake.
- Three-question academic diagnostic and strategy cards for refreshers, industry gaps, and current-semester links.
- Adaptive technical interview, persisted answers, coding/problem-solving prompt, hints, code review, and follow-up defense.
- Evidence-linked report with strengths, improvement areas, and a hidden-gap insight.
- Interview history, retest, and mentor focus/repeated-weakness view.
- Optional Gemini structured JSON generation with validation and offline fallback.

This is a hackathon-ready local/demo application. Add a production authentication provider and managed persistent database before handling real student data at scale.
