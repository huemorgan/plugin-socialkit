"""plugin-socialkit — algorithm-graded social posts and ad creatives, inline in chat.

Authored against `luna_sdk` ONLY (never `import luna.*`). Wraps the SocialKit
API (https://socialkit.sh): score / generate / rewrite posts against the live
LinkedIn + X ranking rubric, plan content calendars, and generate ad visuals —
each rendered inline in the conversation via `embed_iframe` (post previews are
styled like the target platform; ad visuals render as an image gallery).

THE API KNOWS NOTHING THE TOOL CALL DOESN'T PASS. Luna's edge is its context —
the owner's business, product, audience, numbers, offers, and voice from the
conversation, memory, and Files. Tool descriptions instruct the agent to pack
that background into `brief` / business-context fields on every call, and to
persist durable context as SocialKit brands/voices (`save_brand`,
`create_voice`) so later calls can just pass `brand_id` / `voice_id`.

Billing: score/rewrite/generate/plan/voice-from-samples cost 1 credit each;
generate_ad_visual costs 1 credit PER VARIANT. Validation and reads are free.
Those tools are `policy="ask"`; free reads auto-approve.

API key resolution (first hit wins):
  vault `socialkit_api_key` → env `LUNA_SOCIALKIT_API_KEY` → `SOCIALKIT_API_KEY`
Base URL override for the cloud gateway proxy:
  env `LUNA_SOCIALKIT_BASE_URL` → `SOCIALKIT_BASE_URL` → real API.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from luna_sdk import CredentialSlot, LunaPlugin, PluginContext, PluginManifest, ToolDef

from . import render, socialkit, storage

log = logging.getLogger("plugin-socialkit")

_VAULT_NAME = "socialkit_api_key"
_ENV_KEY = "LUNA_SOCIALKIT_API_KEY"
_ENV_BASE = "LUNA_SOCIALKIT_BASE_URL"
_NATIVE_KEYS = ("SOCIALKIT_API_KEY",)
_NATIVE_BASES = ("SOCIALKIT_BASE_URL",)

_CONTEXT_NOTE = (
    "SocialKit only knows what you pass it — pull every relevant fact about the "
    "owner's business, product, audience, positioning, numbers, and offers from "
    "the conversation, memory, and Files into this call. If a saved brand/voice "
    "exists (list_brand_assets), pass brand_id/voice_id instead of repeating it."
)

_GENERATE_DEF = ToolDef(
    name="generate_post",
    description=(
        "Generate 1-3 ready-to-post social drafts with SocialKit, graded against "
        "the live platform algorithm, and PREVIEW them inline as platform-styled "
        "cards. Use when the owner asks for a social post, LinkedIn/X content, or "
        "an ad copy draft. Write a rich `brief`: topic, goal, audience, and the "
        "concrete facts/numbers the post should use. " + _CONTEXT_NOTE +
        " Costs 1 credit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "brief": {"type": "string", "description": "What the post should be about — include business background, concrete facts, numbers, and the goal (max 2000 chars)."},
            "platform": {"type": "string", "enum": list(socialkit.PLATFORMS), "description": "Target platform (default linkedin)."},
            "count": {"type": "integer", "minimum": 1, "maximum": 3, "description": "How many drafts (default 1)."},
            "archetype": {"type": "string", "enum": list(socialkit.ARCHETYPES), "description": "Optional hook archetype to force."},
            "author_name": {"type": "string", "description": "Owner's display name (used in the preview and for voice context)."},
            "author_headline": {"type": "string", "description": "Owner's headline/title."},
            "follower_band": {"type": "string", "enum": list(socialkit.FOLLOWER_BANDS), "description": "Owner's audience size."},
            "brand_id": {"type": "string", "description": "Saved SocialKit brand id (brd_...) to condition the drafts."},
            "voice_id": {"type": "string", "description": "Saved SocialKit voice id (voi_...) to imitate."},
        },
        "required": ["brief"],
    },
    policy="ask",
    risk_level="low",
    timeout_seconds=180,
)

_SCORE_DEF = ToolDef(
    name="score_post",
    description=(
        "Grade a draft post against the live platform ranking rubric (0-100 over "
        "6 dimensions: hook, algorithm fit, specificity, structure, voice, "
        "engagement) and show the score card inline. Use before the owner "
        "publishes anything, or when they ask 'how good is this post'. "
        "Costs 1 credit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "post": {"type": "string", "description": "The full post text to grade."},
            "platform": {"type": "string", "enum": list(socialkit.PLATFORMS), "description": "Platform rubric to grade against (default linkedin)."},
            "follower_band": {"type": "string", "enum": list(socialkit.FOLLOWER_BANDS)},
            "author_name": {"type": "string"},
            "author_headline": {"type": "string"},
        },
        "required": ["post"],
    },
    policy="ask",
    risk_level="low",
    timeout_seconds=120,
)

_REWRITE_DEF = ToolDef(
    name="rewrite_post",
    description=(
        "Score a draft, then rewrite it toward better distribution and show the "
        "before/after scores plus the rewritten post inline. Use when the owner "
        "has a draft and wants it improved. The after-score is measured, never "
        "inflated. Costs 1 credit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "post": {"type": "string", "description": "The draft to improve."},
            "platform": {"type": "string", "enum": list(socialkit.PLATFORMS)},
            "follower_band": {"type": "string", "enum": list(socialkit.FOLLOWER_BANDS)},
            "author_name": {"type": "string"},
            "author_headline": {"type": "string"},
        },
        "required": ["post"],
    },
    policy="ask",
    risk_level="low",
    timeout_seconds=180,
)

_PLAN_DEF = ToolDef(
    name="plan_content",
    description=(
        "Spread a brief into a content calendar (1-14 slots across content "
        "pillars and hook archetypes) and show it inline. Use when the owner "
        "asks for a content plan, posting schedule, or week of post ideas. "
        + _CONTEXT_NOTE + " Costs 1 credit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "brief": {"type": "string", "description": "Themes, goals, and business background the calendar should cover (max 2000 chars)."},
            "platform": {"type": "string", "enum": list(socialkit.PLATFORMS)},
            "count": {"type": "integer", "minimum": 1, "maximum": 14, "description": "Calendar slots (default 5)."},
            "cadence": {"type": "string", "enum": list(socialkit.CADENCES)},
            "brand_id": {"type": "string"},
            "voice_id": {"type": "string"},
        },
        "required": ["brief"],
    },
    policy="ask",
    risk_level="low",
    timeout_seconds=180,
)

_VISUAL_DEF = ToolDef(
    name="generate_ad_visual",
    description=(
        "Generate scroll-stopping AD CREATIVE IMAGES (with matching captions and "
        "hashtags) for a campaign and show them inline as an image gallery. Use "
        "when the owner asks for an ad, ad creative, campaign visual, or social "
        "image. Provide the ad narrative (headline + base idea) and business "
        "context. " + _CONTEXT_NOTE +
        " Costs 1 credit PER VARIANT (default 2, max 4) — only raise variants "
        "when the owner asks for options. Images are 1:1."
    ),
    parameters={
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "The ad's headline / main message."},
            "base_idea": {"type": "string", "description": "The core idea: what is being promoted, to whom, and why it matters. Include concrete product facts."},
            "business_name": {"type": "string", "description": "The owner's business/brand name."},
            "industry": {"type": "string", "description": "Business industry/category."},
            "audience": {"type": "string", "description": "Who the ad targets."},
            "website": {"type": "string", "description": "Business website URL."},
            "cta": {"type": "string", "description": "Call to action."},
            "tone": {"type": "string", "description": "Desired tone/mood."},
            "variants": {"type": "integer", "minimum": 1, "maximum": 4, "description": "How many creative variants (default 2; 1 credit each)."},
        },
        "required": ["headline", "base_idea", "business_name"],
    },
    policy="ask",
    risk_level="low",
    timeout_seconds=300,
)

_VALIDATE_DEF = ToolDef(
    name="validate_post",
    description=(
        "Free, instant preflight of a draft: structural errors (too long, empty) "
        "and reach/voice hazards (6+ hashtags, external links, engagement bait, "
        "AI tells). No LLM, no credits. Run before publishing or scoring."
    ),
    parameters={
        "type": "object",
        "properties": {
            "post": {"type": "string", "description": "The draft to preflight."},
            "platform": {"type": "string", "enum": list(socialkit.PLATFORMS)},
        },
        "required": ["post"],
    },
    policy="auto_approve",
    risk_level="low",
)

_SAVE_BRAND_DEF = ToolDef(
    name="save_brand",
    description=(
        "Create or update a SocialKit brand — the durable business context "
        "(description, audience, themes, link policy) that conditions every "
        "later generate/plan call via brand_id. Distill it from what Luna knows "
        "about the owner's business; update it when the business context "
        "changes. Pass brand_id to update an existing brand. Free."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Brand name."},
            "description": {"type": "string", "description": "Who the business is — distilled from Luna's knowledge (max 1000 chars)."},
            "audience": {"type": "string", "description": "Who the brand talks to (max 500 chars)."},
            "themes": {"type": "array", "items": {"type": "string"}, "description": "Recurring themes the brand posts about (max 25)."},
            "link_policy": {"type": "string", "description": "Policy on outbound links (max 300 chars)."},
            "brand_id": {"type": "string", "description": "Existing brand id (brd_...) to update instead of creating."},
        },
        "required": ["name"],
    },
    policy="ask",
    risk_level="low",
)

_CREATE_VOICE_DEF = ToolDef(
    name="create_voice",
    description=(
        "Create a reusable SocialKit writing voice, either distilled from 1-12 "
        "sample posts (costs 1 credit) or from explicit traits/do-nots/exemplars "
        "(free). Use the owner's real past posts as samples when available. "
        "Later generate calls pass the returned voice_id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Voice name."},
            "samples": {"type": "array", "items": {"type": "string"}, "description": "1-12 sample posts in the target voice (preferred; costs 1 credit)."},
            "traits": {"type": "array", "items": {"type": "string"}, "description": "Stylistic traits to imitate (ignored when samples given)."},
            "do_not": {"type": "array", "items": {"type": "string"}, "description": "Things this voice never does."},
            "exemplars": {"type": "array", "items": {"type": "string"}, "description": "Few-shot snippets that anchor the voice."},
            "brand_id": {"type": "string", "description": "Brand to attach the voice to."},
        },
        "required": ["name"],
    },
    policy="ask",
    risk_level="low",
    timeout_seconds=120,
)

_LIST_ASSETS_DEF = ToolDef(
    name="list_brand_assets",
    description=(
        "List the saved SocialKit brands and voices (ids, names, context). Call "
        "before generating to reuse existing context via brand_id/voice_id "
        "instead of restating it. Free."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    policy="auto_approve",
    risk_level="low",
)

_STATUS_DEF = ToolDef(
    name="socialkit_status",
    description=(
        "Show the SocialKit account, plan, and remaining monthly credits. Call "
        "when a tool reports quota/credit errors or the owner asks about usage. "
        "Free."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    policy="auto_approve",
    risk_level="low",
)


class SocialKitPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-socialkit",
        shown_name="SocialKit",
        icon="megaphone",
        image="assets/icon.png",
        version="0.1.0",
        description=(
            "Generate, score, and rewrite social posts against the live LinkedIn/X "
            "algorithm, plan content calendars, and create ad visuals — previewed "
            "inline in chat. Powered by socialkit.sh."
        ),
        tools=[
            _GENERATE_DEF, _SCORE_DEF, _REWRITE_DEF, _PLAN_DEF, _VISUAL_DEF,
            _VALIDATE_DEF, _SAVE_BRAND_DEF, _CREATE_VOICE_DEF, _LIST_ASSETS_DEF,
            _STATUS_DEF,
        ],
        routes_module="routes",
        # Storage capability so downloaded ad visuals also land in Files.
        capabilities=["storage"],
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None

    def credential_slots(self) -> list[CredentialSlot]:
        return [
            CredentialSlot(
                slug="socialkit",
                credential_name=_VAULT_NAME,
                env_key_var=_ENV_KEY,
                env_base_url_var=_ENV_BASE,
                owner=self.manifest.name,
            )
        ]

    async def _api_key(self) -> str | None:
        """vault `socialkit_api_key` → LUNA_ env → native env. None if unset."""
        ctx = self._ctx
        if ctx is not None and getattr(ctx, "vault", None) is not None:
            try:
                cred = await ctx.vault.get_credential(_VAULT_NAME)
                if (cred.value or "").strip():
                    return cred.value.strip()
            except KeyError:
                pass
            except Exception as exc:  # noqa: BLE001 — a vault hiccup must not block the call
                log.warning("socialkit: vault read failed: %s", exc)
        if ctx is not None and getattr(ctx, "get_env", None) is not None:
            val = (ctx.get_env(_ENV_KEY) or "").strip()
            if val:
                return val
        for name in _NATIVE_KEYS:
            val = (os.environ.get(name) or "").strip()
            if val:
                return val
        return None

    def _base_url(self) -> str | None:
        """`LUNA_SOCIALKIT_BASE_URL` → native base env. None = real upstream."""
        ctx = self._ctx
        if ctx is not None and getattr(ctx, "get_env", None) is not None:
            v = (ctx.get_env(_ENV_BASE) or "").strip()
            if v:
                return v
        for name in _NATIVE_BASES:
            v = (os.environ.get(name) or "").strip()
            if v:
                return v
        return None

    async def _keyed(self) -> tuple[str, str | None] | dict:
        key = await self._api_key()
        if not key:
            return {
                "error": "socialkit api key missing",
                "detail": (
                    f"No SocialKit key. Add the vault credential `{_VAULT_NAME}` "
                    f"(Settings → Vault) or set env `{_ENV_KEY}` / `SOCIALKIT_API_KEY`, "
                    "then retry. Keys come from https://app.socialkit.sh."
                ),
            }
        return key, self._base_url()

    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx

        async def _generate_post(brief: str, platform: str = "linkedin", count: int = 1,
                                 archetype: str | None = None, author_name: str | None = None,
                                 author_headline: str | None = None, follower_band: str | None = None,
                                 brand_id: str | None = None, voice_id: str | None = None) -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            res = await socialkit.generate(
                key, base, brief=brief, platform=platform, count=count, archetype=archetype,
                author_name=author_name, author_headline=author_headline,
                follower_band=follower_band, brand_id=brand_id, voice_id=voice_id,
            )
            if "error" in res:
                return json.dumps(res)
            cands = res.get("candidates") or []
            payload = {
                "ok": True,
                "platform": platform,
                "candidates": [
                    {"post": c.get("post"), "overall": (c.get("score") or {}).get("overall"),
                     "verdict": (c.get("score") or {}).get("verdict")}
                    for c in cands
                ],
                "credits_remaining": res.get("credits_remaining"),
                "embed_iframe": render.render_post_previews(
                    cands, platform=platform, author_name=author_name or "",
                    author_headline=author_headline or "",
                ),
            }
            return json.dumps(payload)

        async def _score_post(post: str, platform: str = "linkedin",
                              follower_band: str | None = None, author_name: str | None = None,
                              author_headline: str | None = None) -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            res = await socialkit.score(
                key, base, post=post, platform=platform, follower_band=follower_band,
                author_name=author_name, author_headline=author_headline,
            )
            if "error" in res:
                return json.dumps(res)
            payload = {
                "ok": True,
                "overall": res.get("overall"),
                "verdict": res.get("verdict"),
                "breakdown": res.get("breakdown"),
                "signals": [
                    {"impact": s.get("impact"), "label": s.get("label"), "detail": s.get("detail")}
                    for s in (res.get("signals") or [])[:8]
                ],
                "credits_remaining": res.get("credits_remaining"),
                "embed_iframe": render.render_score_card(res, platform=platform),
            }
            return json.dumps(payload)

        async def _rewrite_post(post: str, platform: str = "linkedin",
                                follower_band: str | None = None, author_name: str | None = None,
                                author_headline: str | None = None) -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            res = await socialkit.rewrite(
                key, base, post=post, platform=platform, follower_band=follower_band,
                author_name=author_name, author_headline=author_headline,
            )
            if "error" in res:
                return json.dumps(res)
            payload = {
                "ok": True,
                "rewrite": res.get("rewrite"),
                "before": (res.get("before") or {}).get("overall"),
                "after": (res.get("after") or {}).get("overall"),
                "changes": [c.get("note") for c in (res.get("changes") or [])[:6]],
                "credits_remaining": res.get("credits_remaining"),
                "embed_iframe": render.render_rewrite_card(
                    res, platform=platform, author_name=author_name or "",
                    author_headline=author_headline or "",
                ),
            }
            return json.dumps(payload)

        async def _plan_content(brief: str, platform: str = "linkedin", count: int = 5,
                                cadence: str | None = None, brand_id: str | None = None,
                                voice_id: str | None = None) -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            res = await socialkit.plan(
                key, base, brief=brief, platform=platform, count=count, cadence=cadence,
                brand_id=brand_id, voice_id=voice_id,
            )
            if "error" in res:
                return json.dumps(res)
            items = res.get("items") or []
            payload = {
                "ok": True,
                "items": [
                    {"slot": i.get("slot"), "pillar": i.get("pillar"), "hook": i.get("hook"),
                     "angle": i.get("angle"), "format": i.get("format"), "archetype": i.get("archetype")}
                    for i in items
                ],
                "credits_remaining": res.get("credits_remaining"),
                "embed_iframe": render.render_plan_card(items, platform=platform),
            }
            return json.dumps(payload)

        async def _generate_ad_visual(headline: str, base_idea: str, business_name: str,
                                      industry: str | None = None, audience: str | None = None,
                                      website: str | None = None, cta: str | None = None,
                                      tone: str | None = None, variants: int = 2) -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            res = await socialkit.generate_visual(
                key, base, headline=headline, base_idea=base_idea, business_name=business_name,
                industry=industry, audience=audience, website=website, cta=cta, tone=tone,
                variants=variants,
            )
            if "error" in res:
                return json.dumps(res)
            items: list[dict[str, Any]] = []
            failures: list[str] = []
            for v in res.get("variants") or []:
                media = await socialkit.fetch_media(v.get("imageUrl") or "", api_key=key)
                if "error" in media:
                    failures.append(f"{v.get('id')}: {media.get('error')}")
                    continue
                saved = storage.save_image(media["bytes"], media["mime"])
                files_ref = await self._save_to_files(media["bytes"], media["mime"], str(saved["id"]))
                items.append({
                    "url": saved["url"],
                    "file_path": saved["path"],
                    "caption": v.get("caption"),
                    "hashtags": v.get("hashtags") or [],
                    "concept": v.get("concept"),
                    "alt": (v.get("metadata") or {}).get("altText"),
                    "saved_to_files": files_ref,
                })
            if not items:
                return json.dumps({"error": "no visuals", "detail": "; ".join(failures) or "all variants failed"})
            payload = {
                "ok": True,
                "headline": headline,
                "variants": [
                    {"caption": it["caption"], "hashtags": it["hashtags"], "concept": it["concept"],
                     "image_url": it["url"], "file_path": it["file_path"],
                     "saved_to_files": it["saved_to_files"]}
                    for it in items
                ],
                "failures": failures or None,
                "credits_remaining": res.get("credits_remaining"),
                "embed_iframe": render.render_visual_gallery(items, headline=headline),
            }
            return json.dumps({k: v for k, v in payload.items() if v is not None})

        async def _validate_post(post: str, platform: str = "linkedin") -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            return json.dumps(await socialkit.validate(key, base, post=post, platform=platform))

        async def _save_brand(name: str, description: str | None = None, audience: str | None = None,
                              themes: list[str] | None = None, link_policy: str | None = None,
                              brand_id: str | None = None) -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            if brand_id:
                res = await socialkit.update_brand(
                    key, base, brand_id, name=name, description=description,
                    audience=audience, themes=themes, link_policy=link_policy,
                )
            else:
                res = await socialkit.create_brand(
                    key, base, name=name, description=description, audience=audience,
                    themes=themes, link_policy=link_policy,
                )
            return json.dumps(res)

        async def _create_voice(name: str, samples: list[str] | None = None,
                                traits: list[str] | None = None, do_not: list[str] | None = None,
                                exemplars: list[str] | None = None, brand_id: str | None = None) -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            res = await socialkit.create_voice(
                key, base, name=name, samples=samples, traits=traits, do_not=do_not,
                exemplars=exemplars, brand_id=brand_id,
            )
            return json.dumps(res)

        async def _list_brand_assets() -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            brands = await socialkit.list_brands(key, base)
            voices = await socialkit.list_voices(key, base)
            return json.dumps({"brands": brands, "voices": voices})

        async def _socialkit_status() -> str:
            keyed = await self._keyed()
            if isinstance(keyed, dict):
                return json.dumps(keyed)
            key, base = keyed
            return json.dumps(await socialkit.whoami(key, base))

        reg = ctx.tool_registry
        reg.register(self.manifest.name, _GENERATE_DEF, _generate_post)
        reg.register(self.manifest.name, _SCORE_DEF, _score_post)
        reg.register(self.manifest.name, _REWRITE_DEF, _rewrite_post)
        reg.register(self.manifest.name, _PLAN_DEF, _plan_content)
        reg.register(self.manifest.name, _VISUAL_DEF, _generate_ad_visual)
        reg.register(self.manifest.name, _VALIDATE_DEF, _validate_post)
        reg.register(self.manifest.name, _SAVE_BRAND_DEF, _save_brand)
        reg.register(self.manifest.name, _CREATE_VOICE_DEF, _create_voice)
        reg.register(self.manifest.name, _LIST_ASSETS_DEF, _list_brand_assets)
        reg.register(self.manifest.name, _STATUS_DEF, _socialkit_status)
        log.info("socialkit.tools_registered: 10 tools")

    # ---- helpers -------------------------------------------------------- #

    def _storage_provider(self) -> Any | None:
        """Resolve the Files StorageProvider (see plugin-image-gen for why the
        provider registry, not ctx.storage, is the canonical path). Resolved per
        call so load order doesn't matter."""
        ctx = self._ctx
        if ctx is None:
            return None
        registry = getattr(ctx, "provider_registry", None)
        if registry is not None:
            try:
                if registry.has("storage"):
                    return registry.get("storage")
            except Exception as exc:  # noqa: BLE001 — registry hiccup must not block the render
                log.warning("socialkit: storage provider lookup failed: %s", exc)
        return getattr(ctx, "storage", None)

    async def _save_to_files(self, data: bytes, mime: str, name: str) -> str | None:
        """Best-effort copy into Files under `ads/`. Returns the Files ref or None."""
        provider = self._storage_provider()
        if provider is None:
            return None
        ref = f"ads/{name}"
        try:
            stored = await provider.save(data, filename=ref, media_type=mime)
            return getattr(stored, "ref", None) or ref
        except Exception as exc:  # noqa: BLE001 — Files copy is a nicety, never block the render
            log.warning("socialkit: could not save to Files (%s): %s", ref, exc)
            return None
