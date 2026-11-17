# Antim Prompt API

FastAPI backend for [antimprompt.com](https://antimprompt.com). The frontend lives in a separate Vite React repo (`yogeshkumarksj/antim_prompt_web_ui`). Production API: `https://prompt-karo-backend.onrender.com`. Prompt images are uploaded through a PHP bridge on Hostinger shared hosting (`https://antimprompt.com/upload_handler.php`).

The original Instagram audit, `GET /ai-magic/insta-audit/{username}`, still uses RapidAPI. A second audit, `GET /ai-magic/insta-audit-v2/{username}`, reads public Business and Creator accounts from Meta's Instagram Graph API and does not call RapidAPI. `GET /ai-magic/insta-audit-auto/{username}` tries Graph first and, when that cannot return a profile, falls back to RapidAPI.

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
| `GEMINI_API_KEY` | Yes (Instagram audit and free AI tools) | Google Gemini API key. Use a free-tier key. |
| `GROQ_API_KEY` | Optional | Groq free-tier fallback for Viral Insta Songs and Viral AI Art Trends |
| `RAPIDAPI_KEY` or `INSTA_LOOTER_KEY` | Yes, unless `RAPIDAPI_KEY_1`–`RAPIDAPI_KEY_10` is set | RapidAPI Instagram Looter key. Used after the numbered keys. |
| `RAPIDAPI_KEY_1` … `RAPIDAPI_KEY_10` | Optional | Extra RapidAPI keys, tried in order when an earlier key is over its quota or rate limit. |
| `RAPIDAPI_KEY_COOLDOWN_SECONDS` | Optional | How long an exhausted RapidAPI key is skipped. Default `60`. |
| `IG_GRAPH_ACCESS_TOKEN` | Yes (Graph v2 and auto audit) | Long-lived Meta User access token |
| `IG_BUSINESS_ACCOUNT_ID` | Yes (Graph v2 and auto audit) | Numeric `instagram_business_account.id` (not the Facebook Page id) |
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

### Automatic audit

`GET /ai-magic/insta-audit-auto/{username}`

Same username rules as the other audit routes. The server tries Graph Business Discovery first. When Graph is configured and returns a public Business or Creator profile, the response is that audit with `"source": "graph"`. If Graph is not configured, errors, or the account is missing or not a Business or Creator account, the same request continues with RapidAPI and returns `"source": "rapidapi"`. A Graph failure is not sent to the client when RapidAPI succeeds.

One uncached auto scan reserves the spend cap once, under the feature id `insta-audit-auto`, and calls Gemini at most once. A Graph miss does not also reserve `insta-audit-v2` or `insta-audit`. The result is cached on its own key (`auto:{username}`), separate from the v1 username key and the v2 `graph:{username}` key, and it uses the same `AUDIT_SEMAPHORE`.

RapidAPI keys are read in this order: `RAPIDAPI_KEY_1` through `RAPIDAPI_KEY_10`, then `RAPIDAPI_KEY`, then `INSTA_LOOTER_KEY`. Duplicate values are skipped. HTTP 429, HTTP 403 whose body says the quota or subscription is used up, and a body that says the quota was exceeded or a rate limit was hit, try the next key. Any other error stops on that key. An exhausted key is remembered in memory and skipped for `RAPIDAPI_KEY_COOLDOWN_SECONDS` (default 60). When every configured key is exhausted, the response is HTTP 429 with exactly `we are getting too much request, please wait`. The v1 route accepts a numbered key on its own; it no longer requires the legacy `RAPIDAPI_KEY` name when one of `RAPIDAPI_KEY_1`–`RAPIDAPI_KEY_10` is set.

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

## Instagram profile score

The live AI Magic tool is **Insta Reach Booster**. The page sends an Instagram username to `GET /ai-magic/insta-audit/{username}`.

What one successful scan does:

1. Normalize the username (strip `@`, lowercase, allow only Instagram's `letters / digits / . / _` form).
2. Check the hard budget. A spent cap returns `429` with `Limit reached, try tomorrow.` and stops here.
3. Load the public profile. v1 calls RapidAPI Instagram Looter `GET /profile`. v2 calls Meta Business Discovery and does not use RapidAPI. The auto route calls Graph first and uses RapidAPI only when Graph does not return a profile.
4. Skip Gemini when the account is private or missing. A private account returns `status: "private"` and no score.
5. Keep the last 10 posts, compute average likes, comments, and engagement, and pass that JSON to Gemini with a fixed response schema. Follower gender is included only when the upstream payload already contains it. The model is told not to guess gender.
6. Return `profile`, `recent_posts`, `engagement`, `follower_gender`, and `ai_audit` (overall score plus profile, content, engagement, consistency, growth, strengths, weaknesses, recommendations, content strategy, and hashtags).

Results for the same username are cached in memory (`AUDIT_CACHE_TTL`, default 60 seconds) and identical in-flight scans share one upstream call (`AUDIT_MAX_CONCURRENT`).

### Data sources

| Route | Upstream | What it can see |
|---|---|---|
| `GET /ai-magic/insta-audit/{username}` | RapidAPI `instagram-looter2` `/profile` | Whatever the scraper still returns for a public web profile. Personal and professional accounts are both attempted. |
| `GET /ai-magic/insta-audit-v2/{username}` | `graph.facebook.com` Business Discovery | Public Instagram Business and Creator accounts only. Personal accounts are a 404. |
| `GET /ai-magic/insta-audit-auto/{username}` | Graph first, then RapidAPI | `"source": "graph"` for a Business or Creator account. `"source": "rapidapi"` when Graph cannot return one. |

The score text itself comes from Gemini (`GEMINI_API_KEY`, model chain `GEMINI_MODELS`). v2 still calls Gemini after the Graph read.

### Cost

- **RapidAPI Instagram Looter** is a paid third-party subscription. Each uncached v1 scan is at least one billed `/profile` request, plus a Gemini generation. Marketplace quotas are small on the basic plan, and overage follows whatever subscription is attached to the key that served the request. `RAPIDAPI_KEY_1` through `RAPIDAPI_KEY_10` are tried in order after a quota or rate-limit response, and `RAPIDAPI_KEY` / `INSTA_LOOTER_KEY` are extra keys. When every key is exhausted the API returns HTTP 429 with `we are getting too much request, please wait`.
- **Gemini** is billed by Google when the key's project has billing turned on and usage passes the free tier. A free AI Studio key stays at the provider's free quota until that quota is exhausted. This app cannot see which of those two states the key is in.
- **Groq** is not used by the score. It is the fallback for the two free tools below.
- **Graph API** has no RapidAPI fee. Meta rate-limits it, and the long-lived token expires in about 60 days. The Gemini call can still cost money.
- **Profile photos** are copied onto Hostinger only when the URL is an `https` host on `cdninstagram.com`, `fbcdn.net`, or `instagram.com`. A failed copy leaves the original CDN URL in the response.

The hard budget below is what stops a busy day from calling RapidAPI, Graph, or Gemini after the cap.

### Risks that remain

- Instagram Looter is an unofficial scraper. Instagram can change the payload, block the scraper, or serve an empty profile, and a scan then fails or scores thin data. Meta's terms restrict automated collection; v2 is the official read, limited to public professional accounts.
- Provider rate limits surface as `429` (Graph, or RapidAPI after every configured key is exhausted) or `502` (a non-quota RapidAPI or Gemini failure). The in-app cap is separate and is the one that returns `Limit reached, try tomorrow.` The all-keys RapidAPI response is `we are getting too much request, please wait`.
- The audit cache lives in process memory. A restart or a second Render instance does its own lookups. Raise `AUDIT_CACHE_TTL` (seconds) when a one-minute cache is causing repeat RapidAPI charges.
- A post with hidden like counts is stored as `0` and is included in the engagement average, so the rate can look lower than the visible posts.
- Gender counts stay empty unless the upstream JSON already has them. Business Discovery never does.
- `X-Forwarded-For` is trusted for the per-IP limit, which matches a single Render proxy. The global cap does not depend on that header.
- The budget row is enforced inside one process and stored in the app database so a restart does not zero it. Two app instances at the same time can each allow up to the cap.

### Fixes in this pass

- A null `edge_followed_by` / media block no longer crashes the scan. Follower and post counts also accept the flat `follower_count` / `media_count` fields and a profile wrapped under `user`, `data`, or `graphql.user`.
- The string `"false"` is no longer treated as a private account.
- Usernames that cannot be an Instagram handle are rejected before a paid lookup.
- Profile-image downloads no longer follow arbitrary URLs from the payload, and redirects are not followed.
- RapidAPI response bodies are not copied into the client error. Unexpected audit errors are redacted before they are returned.

## Free AI tools, limits, and the budget cap

The AI Magic page lists two tools that were still "Coming soon". Both now have backend routes. They are marked `billing: "free"` and do not call RapidAPI or the Graph API.

| Tool | Route | Body |
|---|---|---|
| Viral Insta Songs | `POST /ai-magic/viral-insta-songs` | `{"niche": "fitness", "region": "India", "language": "Hindi"}` |
| Viral AI Art Trends | `POST /ai-magic/viral-ai-art-trends` | `{"niche": "ceramics", "platform": "Instagram"}` |

`region` defaults to `Global`. `language` may be empty. `platform` defaults to `Instagram`. `niche` is required.

A success body includes `status`, `feature`, `billing`, `cached`, `ai_provider`, `ai_model`, the normalized `input`, a `disclaimer`, and either `songs` + `how_to_use` or `trends` + `workflow`. Song ideas are titles, artists, and original usage notes. The model is instructed not to return lyrics. Art ideas include an original image prompt. Neither route claims to have scanned Instagram live. There is no payment gateway.

### Browse charts

The POST routes stay as they are (one niche, up to 6 ideas, per-person limit). Two read-only charts sit beside them. They do not call RapidAPI, do not scan Instagram, and do not generate an image. A request serves a cached list. The model runs only when that list is missing or older than the refresh window, and several categories are packed into one call.

| Chart | Categories | List |
|---|---|---|
| `GET /ai-magic/viral-insta-songs/categories` | Fixed list in `viral_browse.SONG_CATEGORIES` | — |
| `GET /ai-magic/viral-insta-songs/top?category=<id>` | `trending`, `hindi-bollywood`, `punjabi`, `english-international`, `romantic`, `sad`, `dance-party`, `fitness-gym`, `travel`, `devotional`, `funny-comedy`, `fashion-beauty`, or `all` | Up to 20 songs, ranks `1..N` |
| `GET /ai-magic/viral-ai-art-trends/categories` | Fixed list in `viral_browse.ART_CATEGORIES` | — |
| `GET /ai-magic/viral-ai-art-trends/browse?category=<id>` | `portrait`, `anime`, `3d-pixar`, `couple`, `festival-indian`, `fantasy`, `product`, `cinematic`, or `all` | Up to 12 trends, no rank numbers |

Edit the tuples in `viral_browse.py` to change the categories. `all` is not stored. It is built from the category caches, so it does not add a provider call.

Every browse body includes the same disclaimer as the matching POST (`SONGS_DISCLAIMER` or `ART_DISCLAIMER`). `generated_at` is UTC. `stale` is true when the list is older than the refresh window because a refresh did not succeed. `refresh_hours` is the window that was used.

A song row is `{rank, title, artist, hashtags, search_text, why_trending}`. `hashtags` is at most 6. `search_text` is `"<title> <artist>"` so it can be copied into search. Duplicate title + artist pairs are dropped after case and whitespace are folded. `category=all` keeps each song's `category` and `category_rank`, then assigns a new overall `rank` from 1. The same song in two categories appears once, under the earlier category.

An art row is `{id, name, category, prompt, negative_prompt, why_it_works, image_url}`. There is no rank. `image_url` is an existing gallery file (`prompts.image_url` on `antimprompt.com`, or the host in `HOSTINGER_PUBLIC_BASE`) chosen by overlap with the prompt's category, tags, and title. A relative `/uploads/...` path is prefixed with that host. Anything else, including a URL that merely matches the words, stays `null` and the client draws a placeholder. The same gallery image is used once per response. No image model is called.

```json
{
  "status": "success",
  "feature": "Viral Insta Songs",
  "billing": "free",
  "disclaimer": "These ideas come from a free-tier AI model, not a live Instagram scan. Titles can go stale, and you still need the rights to use the audio.",
  "category": "trending",
  "category_label": "Trending",
  "generated_at": "2026-11-17T08:00:00+00:00",
  "stale": false,
  "refresh_hours": 6,
  "songs": [
    {
      "rank": 1,
      "title": "Kesariya",
      "artist": "Arijit Singh",
      "hashtags": ["bollywood", "reels"],
      "search_text": "Kesariya Arijit Singh",
      "why_trending": "Wedding reels still open on this hook."
    }
  ]
}
```

`category=all` adds `missing_categories` and, on each song, `category` plus `category_rank`. An art list uses `trends` instead of `songs`. When nothing is cached and a refresh cannot run, the status is `unavailable`, `songs` or `trends` is empty, and `message` asks the caller to try again later. The disclaimer is still included. An unknown category is HTTP 400.

### Refresh, lock, and the browse budget

`VIRAL_REFRESH_HOURS` defaults to `6`. `VIRAL_SONGS_REFRESH_HOURS` and `VIRAL_ART_REFRESH_HOURS` override one chart. The first request that finds a batch older than that window generates it. Other requests for the same batch wait on that task instead of starting another call. The optional warmer (`VIRAL_BROWSE_BACKGROUND=true`) does the same check on a loop; it is off by default because a Render free instance sleeps and would miss the timer. The request path still refreshes after a wake.

Rows live in `viral_browse_cache` (and in memory). They are not deleted when the window ends, so a restart does not call the model again, and a failed refresh can return the previous list with `stale: true` and the old `generated_at`. A failed attempt waits `VIRAL_BROWSE_RETRY_MINUTES` (default 30) before trying that batch again. An empty model list does not wipe a list that already has rows.

Browse calls use `begin_browse_generation`. That does not increment `AI_GLOBAL_DAILY_REQUEST_CAP` and does not use a per-person bucket, so the charts cannot spend the interactive allowance. They have their own ceiling, `VIRAL_BROWSE_DAILY_REQUEST_CAP` (default 16) and `VIRAL_BROWSE_DAILY_TOKEN_CAP` (default 140000). A global cap set to `0` is still the off switch for browse as well. Past the browse cap, or when Gemini and Groq both fail, the last cached list is served.

Default batching is what makes 16 enough. Songs use `VIRAL_SONGS_BATCH_SIZE` (default 6, so 12 categories are 2 calls). Art uses `VIRAL_ART_BATCH_SIZE` (default 4, so 8 categories are 2 calls). A full refresh is 4 calls. Every 6 hours that is 4 refreshes, 16 calls per UTC day:

| Chart | Categories | Batch | Calls per refresh | Refreshes per day | Calls per day |
|---|---|---|---|---|---|
| Songs | 12 | 6 | 2 | 4 | 8 |
| Art | 8 | 4 | 2 | 4 | 8 |
| Total | 20 |  | 4 | 4 | 16 |

`viral_browse.planned_daily_ai_calls()` recomputes that from the current settings. Interactive songs, art, and the Instagram audit routes still share the separate cap of 40 requests per day. Browse completions use `VIRAL_BROWSE_MAX_OUTPUT_TOKENS` (default 8192, clamped to 8192) because one call holds several categories. The interactive cap stays at `AI_MAX_OUTPUT_TOKENS`.

### Provider fallback

`AI_PROVIDER_ORDER` defaults to `gemini,groq`. A provider with a blank key is skipped. The call uses the Gemini API (`GEMINI_API_KEY`, header `x-goog-api-key`) and the Groq OpenAI-compatible chat API (`GROQ_API_KEY`). A `429`, `5xx`, timeout, or invalid JSON response tries the next model, then the next provider. A `400` is retried once without JSON mode, because some free models reject `responseMimeType`. Keys are read from the environment on each call and are redacted from error text.

`FREE_GEMINI_MODELS` is the free-tool Gemini chain. When it is unset, the first model in `GEMINI_MODELS` is used (default `gemini-3.6-flash`). `GROQ_MODELS` defaults to `llama-3.3-70b-versatile`. Model names that are not a short token (`letters, digits, dot, underscore, colon, hyphen`) are ignored so a bad value cannot change the request URL.

Put the free-tier key in the environment. If that Google project has billing enabled, Google can still charge past the free tier, which is why the cap below refuses the call instead of assuming the provider will.

### Cache

Successful free-tool responses are stored for `AI_CACHE_TTL_SECONDS` (default `86400`, minimum 60) under a hash of the feature plus the case-folded input. The row is in the app database (`ai_result_cache`) and also in memory. A cache hit does not call a provider, does not spend the budget, and does not use one of the daily tries. `AI_CACHE_MAX` (default 500) drops the oldest rows.

### Per-person limit

Uncached POST generations count against the caller IP, per feature, per UTC day. The browse GETs do not; they draw from the shared chart cache and the separate browse budget above.

| Variable | Default | Applies to |
|---|---|---|
| `AI_FREE_USER_DAILY_LIMIT` | `5` | Viral Insta Songs, Viral AI Art Trends |
| `AI_PAID_USER_DAILY_LIMIT` | `5` | the three Instagram score routes (`insta-audit`, `insta-audit-v2`, `insta-audit-auto`) |

The sixth uncached songs request from the same IP returns `429` `Limit reached, try tomorrow.` A repeat of an input that is still cached still returns the saved result. IPs are stored as a salted hash (`AI_LIMIT_SALT`). `0` on either limit blocks new uncached calls for that group.

### Hard budget cap

Counters live in `ai_usage_counters` and survive a process restart. Before every uncached score or free-tool generation the app reserves one request and `AI_RESERVE_TOKENS_PER_CALL` tokens (default 4000). The reserve happens before RapidAPI, Graph, Gemini, or Groq. When any counter below would be exceeded, the response is HTTP `429` with exactly `Limit reached, try tomorrow.` and the provider is not called.

| Variable | Default |
|---|---|
| `AI_BUDGET_ENABLED` | `true` |
| `AI_GLOBAL_DAILY_REQUEST_CAP` | `40` |
| `AI_GLOBAL_MONTHLY_REQUEST_CAP` | `800` |
| `AI_GLOBAL_DAILY_TOKEN_CAP` | `100000` |
| `AI_GLOBAL_MONTHLY_TOKEN_CAP` | `1500000` |

Days and months use UTC. After a provider reports token usage, the token reserve is replaced with that usage. A missing usage number keeps the reserve, so a silent provider cannot erase the estimate. `AI_MAX_OUTPUT_TOKENS` (default 1024, clamped to 128–2048) caps the completion size for interactive calls. Setting a request cap to `0` is an off switch, including the browse charts. `AI_BUDGET_ENABLED=false` turns the cap off for local debugging and should stay on in production.

The browse charts do not reserve from the counters in the table above. They use `browse:req:day:<date>` and `browse:tok:day:<date>`, capped by `VIRAL_BROWSE_DAILY_REQUEST_CAP` (default 16). See the browse section for the 16-call daily schedule.

If the budget database cannot be read or written, the request fails with `503` and the provider is not called.

### Paid versus free

`AI_FEATURE_BILLING` overrides the built-in flags, for example `viral-insta-songs:paid,insta-audit:free`. The score routes default to `paid` (`insta-audit`, `insta-audit-v2`, and `insta-audit-auto`) because they can spend RapidAPI, Graph, and Gemini. The two new tools default to `free`. `AI_BLOCK_PAID_FEATURES=true` makes every paid tool return `403` `This feature is marked paid and is not available yet.` The default is `false`, so the score keeps working. Nothing in this API collects a payment. One auto scan counts as one request. A Graph miss does not reserve a second time before the RapidAPI attempt, and Gemini runs only for the source that returned a profile.

## Deploy on Render

1. Connect this GitHub repo as a Web Service (or use `render.yaml`).
2. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT` (also in `Procfile`).
3. Set env vars on the Render service: `DATABASE_URL`, `ADMIN_KEY`, `UPLOAD_HANDLER_KEY`, `GEMINI_API_KEY`, and at least one of `RAPIDAPI_KEY` or `RAPIDAPI_KEY_1`–`RAPIDAPI_KEY_10`. For Graph v2 and the auto route also set `IG_GRAPH_ACCESS_TOKEN` + `IG_BUSINESS_ACCOUNT_ID`. Optionally set `CORS_ORIGINS`, `IG_GRAPH_API_VERSION`, `GROQ_API_KEY`, `RAPIDAPI_KEY_COOLDOWN_SECONDS`, and the budget caps in the free-AI section. Leave the caps at the defaults on a small site; raise them only when the free-tier quota and the RapidAPI plan can absorb the extra calls.
4. Redeploy after changing secrets.

## Hostinger upload bridge

`upload_handler.php` on antimprompt.com must accept a form field `key` and compare it to the **same** secret configured as `UPLOAD_HANDLER_KEY` on Render. When you rotate the key, update both places. Do not hardcode the key in the FastAPI repo.

## Tests

```bash
python -m unittest tests.test_gemini_model_fallback tests.test_ig_graph_audit tests.test_free_ai tests.test_insta_audit_auto tests.test_viral_browse -v
```

## Image uploads

Published prompt images are resized (max long side ~1600px), encoded as WebP (~quality 80), and optionally accompanied by a smaller thumbnail URL (`thumbnail_url`) without removing the existing `url` field.
