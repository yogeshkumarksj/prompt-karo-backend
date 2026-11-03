# aipromptbuilder

Backend for Antim Prompt. The original Instagram audit, `GET /ai-magic/insta-audit/{username}`, still uses RapidAPI. A second audit reads public Business and Creator accounts from Meta's Instagram Graph API and does not call RapidAPI.

## Instagram Graph audit

`GET /ai-magic/insta-audit-v2/{username}`

The username is the only input. The server calls [Business Discovery](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery/) on your own Instagram professional account, then runs the same Gemini audit, cache, and concurrency limit as the RapidAPI route. Follower gender is returned as unavailable because Business Discovery does not provide it.

The target account must be a public Instagram Business or Creator account. Personal accounts, and accounts Business Discovery cannot see, get **404**.

### Environment variables on Render

| Variable | Required | Purpose |
| --- | --- | --- |
| `IG_GRAPH_ACCESS_TOKEN` | yes | Long-lived User access token for the Facebook user who can manage the Page linked to your Instagram professional account |
| `IG_BUSINESS_ACCOUNT_ID` | yes | Numeric Instagram professional account id (`instagram_business_account.id`), not the Facebook Page id |
| `IG_GRAPH_API_VERSION` | no | Graph version. Defaults to `v26.0`, the version in Meta's current Business Discovery examples |
| `GEMINI_API_KEY` | yes | Already required for the audit text |

Do not commit these values. If either Instagram variable is missing, the v2 route returns **500**.

### What you set up on Meta

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
