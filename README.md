# Antim Prompt API

FastAPI backend for [antimprompt.com](https://antimprompt.com). The frontend lives in a separate Vite React repo (`yogeshkumarksj/antim_prompt_web_ui`). Production API: `https://prompt-karo-backend.onrender.com`. Prompt images are uploaded through a PHP bridge on Hostinger shared hosting (`https://antimprompt.com/upload_handler.php`).

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with real values (see below)
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Health check: `GET http://127.0.0.1:8000/health`

Without `DATABASE_URL`, the app uses a local SQLite file (`promptverse.db`). That file is gitignored — it is created on first run.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ADMIN_KEY` | Yes (admin routes) | Shared secret for admin header / verify endpoint |
| `UPLOAD_HANDLER_KEY` | Yes (uploads) | Shared secret sent to Hostinger `upload_handler.php` |
| `GEMINI_API_KEY` | Yes (Instagram audit) | Google Gemini API key |
| `RAPIDAPI_KEY` or `INSTA_LOOTER_KEY` | Yes (Instagram audit) | RapidAPI Instagram Looter key |
| `DATABASE_URL` | On Render | Postgres URL (SQLite locally if unset) |
| `CORS_ORIGINS` | Optional | Comma-separated allowed origins |
| `GEMINI_MODELS` | Optional | Comma-separated Gemini model fallback chain |

Copy `.env.example` and fill in placeholders. Never commit a real `.env`.

### Admin auth

Admin routes expect header `X-Admin-Key: <ADMIN_KEY>`. The frontend can validate a typed key with:

```http
POST /admin/verify
Content-Type: application/json

{"key":"<typed-admin-key>"}
```

Returns `{"valid": true}` or `401`.

## Deploy on Render

1. Connect this GitHub repo as a Web Service (or use `render.yaml`).
2. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT` (also in `Procfile`).
3. Set env vars on the Render service: `DATABASE_URL`, `ADMIN_KEY`, `UPLOAD_HANDLER_KEY`, `GEMINI_API_KEY`, `RAPIDAPI_KEY`, and optionally `CORS_ORIGINS`.
4. Redeploy after changing secrets.

## Hostinger upload bridge

`upload_handler.php` on antimprompt.com must accept a form field `key` and compare it to the **same** secret configured as `UPLOAD_HANDLER_KEY` on Render. When you rotate the key, update both places. Do not hardcode the key in the FastAPI repo.

## Tests

```bash
python -m unittest tests.test_gemini_model_fallback -v
```

## Image uploads

Published prompt images are resized (max long side ~1600px), encoded as WebP (~quality 80), and optionally accompanied by a smaller thumbnail URL (`thumbnail_url`) without removing the existing `url` field.
