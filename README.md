# plugin-socialkit

Luna plugin for [SocialKit](https://socialkit.sh) — generate, score, and
rewrite social posts against the live LinkedIn/X ranking algorithm, plan
content calendars, and generate ad creative images. Every result renders
inline in the chat: drafts as platform-styled preview cards, scores as
dimension bars, ad visuals as an image gallery.

## Tools

| Tool | What it does | Cost |
|---|---|---|
| `generate_post` | 1-3 algorithm-graded drafts, previewed as LinkedIn/X cards | 1 credit |
| `score_post` | 0-100 score over 6 weighted rubric dimensions + signals | 1 credit |
| `rewrite_post` | Score → rewrite → measured before/after | 1 credit |
| `plan_content` | 1-14-slot content calendar across pillars | 1 credit |
| `generate_ad_visual` | Ad creative images + captions + hashtags, inline gallery | 1 credit **per variant** |
| `validate_post` | Instant structural/reach preflight (no LLM) | free |
| `save_brand` | Persist business context for reuse via `brand_id` | free |
| `create_voice` | Reusable writing voice from samples or traits | 1 credit (samples) |
| `list_brand_assets` | List saved brands and voices | free |
| `socialkit_status` | Account, plan, remaining credits | free |

## Luna's knowledge → the content

SocialKit only knows what a call passes it. The tool descriptions direct the
agent to pack the owner's business background (product, audience, numbers,
offers) from the conversation, memory, and Files into each `brief` /
business-context field — and to distill durable context into a SocialKit
brand + voice once, then reuse `brand_id` / `voice_id`.

## API key

Resolved as: vault `socialkit_api_key` → env `LUNA_SOCIALKIT_API_KEY` →
native `SOCIALKIT_API_KEY`. `LUNA_SOCIALKIT_BASE_URL` overrides the upstream
for cloud gateway proxying. Keys come from https://app.socialkit.sh.

## Notes on the API (verified live, July 2026)

- `POST /v1/generate-visual` is real but absent from the published OpenAPI
  spec. Required: `narrative{headline, baseIdea}`, `businessContext{businessName}`.
  `variants` (1-4, default 4) controls the count and the credit charge.
  Responses are cached server-side keyed on narrative+businessContext; aspect
  ratio is fixed 1:1; images are Bearer-gated, so this plugin downloads the
  bytes and re-serves them from `/api/p/plugin-socialkit/file/<id>`.
- Free plan: 100 credits/month, charged on success only.

## Dev

```bash
pip install -e ".[dev]" && pytest
```
