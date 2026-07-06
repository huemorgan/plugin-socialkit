"""Unit tests for plugin-socialkit — the inner loop, no Luna runtime needed.

Manifest tests read the TOML data contract directly. Client tests exercise the
SocialKit client via monkeypatched httpx (no real network). Tool-wiring tests
load the plugin against a fake context and monkeypatch the client module.

Run: `pip install -e ".[dev]" && pytest`
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import plugin_socialkit as pkg
from plugin_socialkit import render, socialkit, storage

ROOT = Path(__file__).resolve().parents[1]


# ---- manifest contract --------------------------------------------------- #

def _toml() -> dict:
    return tomllib.loads((ROOT / "plugin_socialkit" / "luna-plugin.toml").read_text())


def test_versions_in_sync():
    toml = _toml()
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert toml["version"] == pkg.SocialKitPlugin.manifest.version == py["project"]["version"]
    assert toml["name"] == pkg.SocialKitPlugin.manifest.name == "plugin-socialkit"


def test_toml_tools_match_manifest():
    toml_tools = {t["name"]: t for t in _toml()["tools"]}
    mani_tools = {t.name: t for t in pkg.SocialKitPlugin.manifest.tools}
    assert set(toml_tools) == set(mani_tools)
    for name, td in mani_tools.items():
        assert toml_tools[name]["policy"] == td.policy, name
        assert toml_tools[name]["risk_level"] == td.risk_level, name


def test_credential_slot():
    slots = pkg.SocialKitPlugin().credential_slots()
    assert len(slots) == 1
    assert slots[0].credential_name == "socialkit_api_key"
    assert slots[0].env_key_var == "LUNA_SOCIALKIT_API_KEY"


# ---- client --------------------------------------------------------------- #

def _transport(handler):
    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, handler):
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        kwargs["transport"] = _transport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)


def test_error_envelope_normalized(monkeypatch):
    def handler(request):
        return httpx.Response(402, json={"error": {"code": "quota_exceeded", "message": "spent"}},
                              headers={"retry-after": "3600"})

    _patch_client(monkeypatch, handler)
    res = asyncio.run(socialkit.score("k", None, post="hi"))
    assert res["error"] == "quota_exceeded"
    assert res["status"] == 402
    assert res["retry_after_seconds"] == "3600"


def test_credits_header_surfaced(monkeypatch):
    def handler(request):
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"overall": 88}, headers={"x-credits-remaining": "42"})

    _patch_client(monkeypatch, handler)
    res = asyncio.run(socialkit.score("k", None, post="hi"))
    assert res["overall"] == 88
    assert res["credits_remaining"] == "42"


def test_generate_clamps_count_and_drops_none(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"candidates": []})

    _patch_client(monkeypatch, handler)
    asyncio.run(socialkit.generate("k", None, brief="b", count=99))
    assert seen["count"] == 3
    assert "brandId" not in seen and "voiceId" not in seen


def test_generate_visual_body_shape(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"variants": []})

    _patch_client(monkeypatch, handler)
    asyncio.run(socialkit.generate_visual(
        "k", None, headline="h", base_idea="i", business_name="Acme", variants=9,
    ))
    assert seen["narrative"] == {"headline": "h", "baseIdea": "i"}
    assert seen["businessContext"] == {"businessName": "Acme"}
    assert seen["variants"] == 4  # clamped


def test_base_url_override(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    _patch_client(monkeypatch, handler)
    asyncio.run(socialkit.whoami("k", "https://gw.example/proxy/socialkit/"))
    assert seen["url"] == "https://gw.example/proxy/socialkit/account"


# ---- storage --------------------------------------------------------------- #

def test_storage_roundtrip_and_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNA_SOCIALKIT_DIR", str(tmp_path))
    saved = storage.save_image(b"png-bytes", "image/png")
    assert saved["url"].startswith("/api/p/plugin-socialkit/file/")
    assert storage.resolve(str(saved["id"])).read_bytes() == b"png-bytes"
    assert storage.resolve("../" + str(saved["id"])) is None
    assert storage.resolve("a/b.png") is None
    assert storage.resolve("") is None


# ---- render ----------------------------------------------------------------- #

def test_post_preview_escapes_and_scores():
    html = render.render_post_previews(
        [{"post": "<script>alert(1)</script>", "score": {"overall": 91, "verdict": "strong"}}],
        platform="linkedin", author_name="Roy", author_headline="Founder",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "s-hi" in html and ">91<" in html


def test_score_card_has_all_dimensions():
    res = {"overall": 55, "verdict": "meh",
           "breakdown": {k: 50 for k, _l, _w in render.DIMENSIONS}, "signals": []}
    html = render.render_score_card(res)
    for _k, label, _w in render.DIMENSIONS:
        assert label in html
    assert "s-lo" in html


def test_gallery_uses_relative_urls_and_mount_script():
    html = render.render_visual_gallery(
        [{"url": "/api/p/plugin-socialkit/file/x.png", "caption": "cap", "hashtags": ["a"], "concept": "c"}],
    )
    assert 'data-rel="api/p/plugin-socialkit/file/x.png"' in html
    assert "document.baseURI" in html
    assert 'loading="eager"' in html


# ---- tool wiring -------------------------------------------------------------- #

class _Registry:
    def __init__(self):
        self.tools = {}

    def register(self, plugin_name, tool_def, fn):
        self.tools[tool_def.name] = fn


class _Ctx:
    def __init__(self):
        self.tool_registry = _Registry()
        self.vault = None

    def get_env(self, name):
        return None


def _load_plugin(monkeypatch):
    monkeypatch.setenv("SOCIALKIT_API_KEY", "sk_test")
    plugin = pkg.SocialKitPlugin()
    ctx = _Ctx()
    asyncio.run(plugin.on_load(ctx))
    return plugin, ctx


def test_tools_registered(monkeypatch):
    _plugin, ctx = _load_plugin(monkeypatch)
    assert set(ctx.tool_registry.tools) == {t.name for t in pkg.SocialKitPlugin.manifest.tools}


def test_missing_key_is_actionable(monkeypatch):
    monkeypatch.delenv("SOCIALKIT_API_KEY", raising=False)
    plugin = pkg.SocialKitPlugin()
    ctx = _Ctx()
    asyncio.run(plugin.on_load(ctx))
    out = json.loads(asyncio.run(ctx.tool_registry.tools["score_post"]("hello")))
    assert "error" in out and "socialkit_api_key" in out["detail"]


def test_generate_post_returns_embed(monkeypatch):
    _plugin, ctx = _load_plugin(monkeypatch)

    async def fake_generate(key, base, **kw):
        return {"candidates": [{"post": "hi", "score": {"overall": 80, "verdict": "good"}}],
                "credits_remaining": "9"}

    monkeypatch.setattr(pkg.socialkit, "generate", fake_generate)
    out = json.loads(asyncio.run(ctx.tool_registry.tools["generate_post"]("brief")))
    assert out["ok"] is True
    assert out["candidates"][0]["overall"] == 80
    assert out["embed_iframe"].startswith("<!DOCTYPE html>")


def test_ad_visual_downloads_and_serves(monkeypatch, tmp_path):
    monkeypatch.setenv("LUNA_SOCIALKIT_DIR", str(tmp_path))
    _plugin, ctx = _load_plugin(monkeypatch)

    async def fake_visual(key, base, **kw):
        return {"variants": [{"id": "post_1", "imageUrl": "https://api.socialkit.sh/v1/media/m1/content",
                              "caption": "cap", "hashtags": ["x"], "concept": "POV",
                              "metadata": {"altText": "alt"}}]}

    async def fake_fetch(url, *, api_key, timeout=60.0):
        return {"bytes": b"img", "mime": "image/png"}

    monkeypatch.setattr(pkg.socialkit, "generate_visual", fake_visual)
    monkeypatch.setattr(pkg.socialkit, "fetch_media", fake_fetch)
    out = json.loads(asyncio.run(ctx.tool_registry.tools["generate_ad_visual"]("h", "i", "Acme")))
    assert out["ok"] is True
    local_url = out["variants"][0]["image_url"]
    assert local_url.startswith("/api/p/plugin-socialkit/file/")
    assert storage.resolve(local_url.rsplit("/", 1)[-1]).read_bytes() == b"img"
    assert "embed_iframe" in out


def test_ad_visual_all_downloads_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("LUNA_SOCIALKIT_DIR", str(tmp_path))
    _plugin, ctx = _load_plugin(monkeypatch)

    async def fake_visual(key, base, **kw):
        return {"variants": [{"id": "post_1", "imageUrl": "https://x/y"}]}

    async def fake_fetch(url, *, api_key, timeout=60.0):
        return {"error": "http_401", "detail": "nope"}

    monkeypatch.setattr(pkg.socialkit, "generate_visual", fake_visual)
    monkeypatch.setattr(pkg.socialkit, "fetch_media", fake_fetch)
    out = json.loads(asyncio.run(ctx.tool_registry.tools["generate_ad_visual"]("h", "i", "Acme")))
    assert out["error"] == "no visuals"


def test_save_brand_routes_create_vs_update(monkeypatch):
    _plugin, ctx = _load_plugin(monkeypatch)
    calls = []

    async def fake_create(key, base, **kw):
        calls.append(("create", kw))
        return {"brand": {"id": "brd_1"}}

    async def fake_update(key, base, brand_id, **kw):
        calls.append(("update", brand_id))
        return {"brand": {"id": brand_id}}

    monkeypatch.setattr(pkg.socialkit, "create_brand", fake_create)
    monkeypatch.setattr(pkg.socialkit, "update_brand", fake_update)
    asyncio.run(ctx.tool_registry.tools["save_brand"]("Acme"))
    asyncio.run(ctx.tool_registry.tools["save_brand"]("Acme", brand_id="brd_9"))
    assert calls[0][0] == "create"
    assert calls[1] == ("update", "brd_9")
