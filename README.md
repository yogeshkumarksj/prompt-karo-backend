# Antim Prompt API

FastAPI backend for [antimprompt.com](https://antimprompt.com). The frontend lives in a separate Vite React repo (`yogeshkumarksj/antim_prompt_web_ui`). Production API: `https://prompt-karo-backend.onrender.com`. Prompt images are uploaded through a PHP bridge on Hostinger shared hosting (`https://antimprompt.com/upload_handler.php`).

The original Instagram audit, `GET /ai-magic/insta-audit/{username}`, still uses RapidAPI. A second audit, `GET /ai-magic/insta-audit-v2/{username}`, reads public Business and Creator accounts from Meta's Instagram Graph API and does not call RapidAPI.

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
| `RAPIDAPI_KEY` or `INSTA_LOOTER_KEY` | Yes (RapidAPI audit) | RapidAPI Instagram Looter key |
| `IG_GRAPH_ACCESS_TOKEN` | Yes (Graph v2 audit) | Long-lived Meta User access token |
| `IG_BUSINESS_ACCOUNT_ID` | Yes (Graph v2 audit) | Numeric `instagram_business_account.id` (not the Facebook Page id) |
| `IG_GRAPH_API_VERSION` | Optional | Graph version; defaults to `v26.0` |
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

## Instagram Graph audit (v2)

`GET /ai-magic/insta-audit-v2/{username}`

The username is the only input. The server calls [Business Discovery](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery/) on your own Instagram professional account, then runs the same Gemini audit, cache, and concurrency limit as the RapidAPI route. Follower gender is returned as unavailable because Business Discovery does not provide it.

The target account must be a public Instagram Business or Creator account. Personal accounts, and accounts Business Discovery cannot see, get **404**. If either Instagram Graph variable is missing, the v2 route returns **500**.

### Meta setup

These steps follow Meta's [Instagram API with Facebook Login getting started](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/get-started/) guide, the [Business Discovery reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery/), and the [long-lived token exchange](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived/). The calling account and the target account are different: you connect one professional account that your app calls *from*, then the audit looks up other public professional usernames.

1. Create a Meta app and add **Facebook Login for Business** and the Instagram product (Instagram API with Facebook Login).
2. On Instagram, switch the account you will call from to a professional account (Business or Creator) and connect it to a Facebook Page you manage.
3. Ask for these permissions on the User token. The getting-started guide uses the first two to find the account id. Business Discovery also requires the other two. If the Page role was granted in Business Manager, Meta also requires `ads_management` or `ads_read`.
   - `instagram_basic`
   - `pages_show_list`
   - `pages_read_engagement`
   - `instagram_manage_insights`
4. Generate a User access token in the [Graph API Explorer](https://developers.facebook.com/tools/explorer/) while logged in as that Facebook user.
5. Exchange it for a long-lived token (about 60 days). Run this from a server, not a browser, and do not paste the app secret into the repo:

   `GET https://graph.facebook.com/v26.0/oauth/access_token?grant_type=fb_exchange_token&client_id={app-id}&client_secret={app-secret}&fb_exchange_token={short-lived-token}`

6. List Pages you can manage:

   `GET https://graph.facebook.com/v26.0/me/accounts?access_token={long-lived-token}`

7. Read the Instagram id connected to that Page:

   `GET https://graph.facebook.com/v26.0/{page-id}?fields=instagram_business_account&access_token={long-lived-token}`

   Put `instagram_business_account.id` in `IG_BUSINESS_ACCOUNT_ID` and the long-lived token in `IG_GRAPH_ACCESS_TOKEN`.

Refresh the token before it expires. A short-lived token cannot be exchanged once it has already expired.

## Deploy on Render

1. Connect this GitHub repo as a Web Service (or use `render.yaml`).
2. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT` (also in `Procfile`).
3. Set env vars on the Render service: `DATABASE_URL`, `ADMIN_KEY`, `UPLOAD_HANDLER_KEY`, `GEMINI_API_KEY`, `RAPIDAPI_KEY`, and for Graph v2 also `IG_GRAPH_ACCESS_TOKEN` + `IG_BUSINESS_ACCOUNT_ID`. Optionally set `CORS_ORIGINS` / `IG_GRAPH_API_VERSION`.
4. Redeploy after changing secrets.

## Hostinger upload bridge

`upload_handler.php` on antimprompt.com must accept a form field `key` and compare it to the **same** secret configured as `UPLOAD_HANDLER_KEY` on Render. When you rotate the key, update both places. Do not hardcode the key in the FastAPI repo.

## Tests

```bash
python -m unittest tests.test_gemini_model_fallback tests.test_ig_graph_audit -v
```

## Image uploads

Published prompt images are resized (max long side ~1600px), encoded as WebP (~quality 80), and optionally accompanied by a smaller thumbnail URL (`thumbnail_url`) without removing the existing `url` field.
