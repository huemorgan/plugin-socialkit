"""SocialKit API client — pure httpx, no luna_sdk imports.

SocialKit (https://socialkit.sh) grades and generates social posts against the
live platform ranking rubric (LinkedIn + X today). Auth is a Bearer key
(`sk_live_...`). Base URL: https://api.socialkit.sh/v1, overridable for the
cloud gateway proxy.

Billable calls (1 credit each, charged on success only): score, rewrite,
generate, plan, voice-build. `generate-visual` bills 1 credit PER VARIANT.
`validate` and all reads are free.

Every function returns a dict; failures come back as
`{"error": <code>, "detail": <message>, "status": <http>}` — never an
exception. Successful responses carry `credits_remaining` when the API
reported it, so the agent can tell the owner when the budget runs low.

Verified against the live API (July 2026):
  - `POST /generate-visual` is real but undocumented (absent from the
    published OpenAPI spec). Contract: `narrative{headline, baseIdea}` and
    `businessContext{businessName}` required; `variants` (int) controls the
    variant count (default 4); responses are cached server-side keyed on the
    narrative+businessContext, and aspect ratio is fixed at 1:1 for now.
  - Variant `imageUrl`s point at `/v1/media/<id>/content` and REQUIRE the
    Bearer key — they cannot be hot-linked from a chat embed, so callers must
    download the bytes (fetch_media) and re-serve them.
"""

from __future__ import annotations

import httpx

DEFAULT_BASE = "https://api.socialkit.sh/v1"
USER_AGENT = "luna-plugin-socialkit/0.1.0"

PLATFORMS = ("linkedin", "x")
FOLLOWER_BANDS = ("under-500", "500-2k", "2k-10k", "10k-50k", "50k-plus")
ARCHETYPES = ("contrarian", "statement", "question", "pure-number", "story")
CADENCES = ("daily", "weekdays", "3x-week")


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def clean_base(base_url: str | None) -> str:
    return (base_url or DEFAULT_BASE).rstrip("/")


async def request(
    method: str,
    path: str,
    *,
    api_key: str,
    base_url: str | None = None,
    body: dict | None = None,
    params: dict | None = None,
    timeout: float = 90.0,
) -> dict:
    """One API call. JSON in/out, error envelope normalized, never raises."""
    url = f"{clean_base(base_url)}{path}"
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            resp = await cli.request(method, url, json=body, params=params, headers=headers)
    except httpx.HTTPError as exc:
        return {"error": "network_error", "detail": str(exc)}
    remaining = resp.headers.get("x-credits-remaining")
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
        except Exception:  # noqa: BLE001 — non-JSON error body
            err = {}
        out = {
            "error": err.get("code", f"http_{resp.status_code}"),
            "detail": err.get("message", resp.text[:300]),
            "status": resp.status_code,
        }
        retry = resp.headers.get("retry-after")
        if retry:
            out["retry_after_seconds"] = retry
        return out
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"error": "bad_response", "detail": resp.text[:300]}
    if isinstance(data, dict) and remaining is not None:
        data["credits_remaining"] = remaining
    return data


async def fetch_media(url: str, *, api_key: str, timeout: float = 60.0) -> dict:
    """Download an auth-gated media object. Returns {bytes, mime} or {error}."""
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            resp = await cli.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"error": "network_error", "detail": str(exc)}
    if resp.status_code >= 400:
        return {"error": f"http_{resp.status_code}", "detail": f"media fetch failed for {url}"}
    mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
    return {"bytes": resp.content, "mime": mime}


# ---- intelligence ------------------------------------------------------- #

async def score(api_key: str, base_url: str | None, *, post: str, platform: str = "linkedin",
                author_name: str | None = None, author_headline: str | None = None,
                follower_band: str | None = None, media: str | None = None) -> dict:
    body = _drop_none({
        "post": post, "platform": platform, "authorName": author_name,
        "authorHeadline": author_headline, "followerBand": follower_band, "media": media,
    })
    return await request("POST", "/score", api_key=api_key, base_url=base_url, body=body)


async def rewrite(api_key: str, base_url: str | None, *, post: str, platform: str = "linkedin",
                  author_name: str | None = None, author_headline: str | None = None,
                  follower_band: str | None = None) -> dict:
    body = _drop_none({
        "post": post, "platform": platform, "authorName": author_name,
        "authorHeadline": author_headline, "followerBand": follower_band,
    })
    return await request("POST", "/rewrite", api_key=api_key, base_url=base_url, body=body, timeout=120.0)


async def generate(api_key: str, base_url: str | None, *, brief: str, platform: str = "linkedin",
                   count: int = 1, archetype: str | None = None, author_name: str | None = None,
                   author_headline: str | None = None, follower_band: str | None = None,
                   media: str | None = None, brand_id: str | None = None,
                   voice_id: str | None = None) -> dict:
    body = _drop_none({
        "brief": brief[:2000], "platform": platform, "count": max(1, min(int(count), 3)),
        "archetype": archetype, "authorName": author_name, "authorHeadline": author_headline,
        "followerBand": follower_band, "media": media, "brandId": brand_id, "voiceId": voice_id,
    })
    return await request("POST", "/generate", api_key=api_key, base_url=base_url, body=body, timeout=120.0)


async def plan(api_key: str, base_url: str | None, *, brief: str, platform: str = "linkedin",
               count: int = 5, cadence: str | None = None, author_name: str | None = None,
               author_headline: str | None = None, follower_band: str | None = None,
               brand_id: str | None = None, voice_id: str | None = None) -> dict:
    body = _drop_none({
        "brief": brief[:2000], "platform": platform, "count": max(1, min(int(count), 14)),
        "cadence": cadence, "authorName": author_name, "authorHeadline": author_headline,
        "followerBand": follower_band, "brandId": brand_id, "voiceId": voice_id,
    })
    return await request("POST", "/plan", api_key=api_key, base_url=base_url, body=body, timeout=120.0)


async def validate(api_key: str, base_url: str | None, *, post: str,
                   platform: str = "linkedin", media: str | None = None) -> dict:
    body = _drop_none({"post": post, "platform": platform, "media": media})
    return await request("POST", "/posts/validate", api_key=api_key, base_url=base_url, body=body)


async def generate_visual(api_key: str, base_url: str | None, *, headline: str, base_idea: str,
                          business_name: str, industry: str | None = None,
                          audience: str | None = None, website: str | None = None,
                          cta: str | None = None, tone: str | None = None,
                          variants: int = 2) -> dict:
    body = {
        "narrative": _drop_none({
            "headline": headline, "baseIdea": base_idea, "cta": cta, "tone": tone,
        }),
        "businessContext": _drop_none({
            "businessName": business_name, "industry": industry,
            "audience": audience, "website": website,
        }),
        "variants": max(1, min(int(variants), 4)),
    }
    return await request("POST", "/generate-visual", api_key=api_key, base_url=base_url,
                         body=body, timeout=180.0)


# ---- memory (brands & voices) ------------------------------------------- #

async def list_brands(api_key: str, base_url: str | None) -> dict:
    return await request("GET", "/brands", api_key=api_key, base_url=base_url)


async def create_brand(api_key: str, base_url: str | None, *, name: str,
                       description: str | None = None, audience: str | None = None,
                       themes: list[str] | None = None, link_policy: str | None = None) -> dict:
    body = _drop_none({
        "name": name[:120], "description": description, "audience": audience,
        "themes": themes, "linkPolicy": link_policy,
    })
    return await request("POST", "/brands", api_key=api_key, base_url=base_url, body=body)


async def update_brand(api_key: str, base_url: str | None, brand_id: str, **fields) -> dict:
    body = _drop_none({
        "name": fields.get("name"), "description": fields.get("description"),
        "audience": fields.get("audience"), "themes": fields.get("themes"),
        "linkPolicy": fields.get("link_policy"),
    })
    return await request("PATCH", f"/brands/{brand_id}", api_key=api_key, base_url=base_url, body=body)


async def list_voices(api_key: str, base_url: str | None) -> dict:
    return await request("GET", "/voices", api_key=api_key, base_url=base_url)


async def create_voice(api_key: str, base_url: str | None, *, name: str,
                       samples: list[str] | None = None, traits: list[str] | None = None,
                       do_not: list[str] | None = None, exemplars: list[str] | None = None,
                       brand_id: str | None = None) -> dict:
    body = _drop_none({
        "name": name[:120], "samples": samples, "traits": traits,
        "doNot": do_not, "exemplars": exemplars, "brandId": brand_id,
    })
    return await request("POST", "/voices", api_key=api_key, base_url=base_url, body=body, timeout=120.0)


async def whoami(api_key: str, base_url: str | None) -> dict:
    return await request("GET", "/account", api_key=api_key, base_url=base_url)
