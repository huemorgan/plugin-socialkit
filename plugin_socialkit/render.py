"""Build the inline chat embeds for SocialKit results.

Luna renders a tool result's `embed_iframe` (a self-contained HTML document)
directly in the conversation — the same hook `plugin-charts` / `plugin-giphy` /
`plugin-image-gen` use. Post previews are styled to look like the target
platform (LinkedIn card / X card) so the owner sees the draft the way readers
will.

Mount-prefix safety for images (copied from plugin-image-gen, proven live):
the chat iframe is `sandbox="allow-scripts"` (opaque origin) and Luna can be
hosted behind a mount prefix, so a root-absolute `/api/...` src 404s and a
plain relative one resolves against the SPA route. An `about:srcdoc` document
inherits the PARENT's base URL, so inline JS strips the trailing SPA segment
from `document.baseURI` and rewrites every `[data-rel]` element to an absolute,
mount-correct URL. `loading="eager"` is mandatory — lazy never fires inside the
sandboxed srcdoc iframe.
"""

from __future__ import annotations

import html as _html
import json as _json

# Rubric dimensions with their published weights (GET /meta).
DIMENSIONS = (
    ("hook", "Hook", 25),
    ("algorithmFit", "Algorithm fit", 20),
    ("specificity", "Specificity", 15),
    ("structure", "Structure", 15),
    ("voice", "Voice", 15),
    ("engagement", "Engagement", 10),
)

_BASE_CSS = """
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0f0f1a;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 12px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    color: #c7c7d9;
  }
  .col { width: 100%; max-width: 540px; display: flex; flex-direction: column; gap: 10px; }
  .chip {
    font-size: 11px; font-weight: 600; white-space: nowrap;
    border: 1px solid #2a2a44; border-radius: 999px; padding: 2px 8px; color: #8a8aa3;
  }
  .score-chip { font-size: 12px; font-weight: 700; border-radius: 999px; padding: 3px 10px; color: #0f0f1a; }
  .s-hi { background: #4ade80; }
  .s-mid { background: #fbbf24; }
  .s-lo { background: #f87171; }
  .head { display: flex; align-items: center; justify-content: space-between; width: 100%; max-width: 540px; gap: 8px; }
  .title { font-size: 12px; font-weight: 700; color: #8a8aa3; letter-spacing: .04em; text-transform: uppercase; }
"""

_POST_CSS = """
  .post-card { background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 6px 24px rgba(0,0,0,.45); }
  .post-card.x-card { background: #000; border: 1px solid #2f3336; }
  .p-head { display: flex; gap: 10px; padding: 12px 14px 0 14px; align-items: center; }
  .avatar {
    width: 44px; height: 44px; border-radius: 50%; flex: none;
    background: linear-gradient(135deg, #6366f1, #a855f7);
    color: #fff; font-weight: 700; font-size: 18px;
    display: flex; align-items: center; justify-content: center;
  }
  .x-card .avatar { border-radius: 50%; }
  .who { display: flex; flex-direction: column; min-width: 0; }
  .name { color: #0a0a0a; font-size: 14px; font-weight: 600; }
  .sub { color: #666; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .x-card .name { color: #e7e9ea; }
  .x-card .sub { color: #71767b; }
  .p-body { padding: 10px 14px 12px 14px; color: #0a0a0a; font-size: 14px; line-height: 1.45; white-space: pre-wrap; word-wrap: break-word; }
  .x-card .p-body { color: #e7e9ea; }
  .p-foot { display: flex; gap: 22px; padding: 8px 14px 12px 14px; border-top: 1px solid #ebebeb; color: #666; font-size: 12px; font-weight: 600; }
  .x-card .p-foot { border-top: 1px solid #2f3336; color: #71767b; }
  .cand-meta { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 2px; }
  .verdict { font-size: 12px; color: #8a8aa3; flex: 1; line-height: 1.35; }
"""

_SCORE_CSS = """
  .panel { background: #1a1a2e; border: 1px solid #2a2a44; border-radius: 12px; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
  .overall { display: flex; align-items: baseline; gap: 10px; }
  .big { font-size: 34px; font-weight: 800; color: #fff; }
  .of { font-size: 13px; color: #6f6f86; }
  .dim { display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .dim .lbl { width: 108px; flex: none; color: #8a8aa3; }
  .bar { flex: 1; height: 7px; background: #26263e; border-radius: 4px; overflow: hidden; }
  .bar i { display: block; height: 100%; border-radius: 4px; }
  .dim .val { width: 30px; text-align: right; color: #c7c7d9; font-weight: 600; }
  .sig { display: flex; gap: 8px; font-size: 12px; line-height: 1.4; }
  .sig .mark { flex: none; font-weight: 800; }
  .sig.pos .mark { color: #4ade80; }
  .sig.neg .mark { color: #f87171; }
  .sig b { color: #e2e2f0; font-weight: 600; }
  .arrow { color: #6f6f86; font-size: 20px; align-self: center; }
"""

_PLAN_CSS = """
  .slot { background: #1a1a2e; border: 1px solid #2a2a44; border-radius: 10px; padding: 10px 12px; display: flex; gap: 10px; }
  .slot .n { flex: none; width: 26px; height: 26px; border-radius: 8px; background: #26263e; color: #a5b4fc;
            font-size: 13px; font-weight: 700; display: flex; align-items: center; justify-content: center; }
  .slot .body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .slot .hook { color: #e2e2f0; font-size: 13px; font-weight: 600; line-height: 1.35; }
  .slot .angle { color: #8a8aa3; font-size: 12px; line-height: 1.4; }
  .slot .tags { display: flex; gap: 6px; flex-wrap: wrap; }
"""

_GALLERY_CSS = """
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; width: 100%; max-width: 540px; }
  .grid.single { grid-template-columns: 1fr; }
  .tile { background: #1a1a2e; border: 1px solid #2a2a44; border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
  .tile img { display: block; width: 100%; height: auto; }
  .tile .cap { padding: 8px 10px; font-size: 11.5px; line-height: 1.4; color: #c7c7d9; }
  .tile .tags { padding: 0 10px 8px 10px; color: #818cf8; font-size: 11px; }
"""


def _score_class(overall: float) -> str:
    return "s-hi" if overall >= 80 else ("s-mid" if overall >= 60 else "s-lo")


def _bar_color(v: float) -> str:
    return "#4ade80" if v >= 80 else ("#fbbf24" if v >= 60 else "#f87171")


def _esc(s: str | None) -> str:
    return _html.escape(s or "", quote=True)


def _page(css: str, body: str, script: str = "") -> str:
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">\n<style>'
        + _BASE_CSS + css + "</style>\n</head>\n<body>\n" + body + script + "\n</body>\n</html>"
    )


def _mount_script() -> str:
    """Rewrite every `[data-rel]` img/link to an absolute, mount-correct URL
    derived from the parent page's base URL. No-op on failure (the static
    relative src stays as the fallback)."""
    return (
        "<script>(function(){try{"
        "var u=new URL(document.baseURI);"
        "var mount=u.pathname.replace(/\\/(chat|settings|approvals|p)(\\/[^?#]*)?$/,'');"
        "if(mount==='/')mount='';"
        "mount=mount.replace(/\\/$/,'');"
        "document.querySelectorAll('[data-rel]').forEach(function(el){"
        "var abs=u.origin+mount+'/'+el.getAttribute('data-rel').replace(/^\\//,'');"
        "if(el.tagName==='IMG')el.src=abs;else el.href=abs;"
        "});"
        "}catch(e){}})();</script>"
    )


def _rel(url: str) -> str:
    return url.lstrip("/") if url.startswith("/") else url


# ---- post previews ------------------------------------------------------ #

def _post_card(text: str, platform: str, author_name: str, author_headline: str,
               overall: float | None, verdict: str) -> str:
    initial = _esc((author_name or "Y")[:1].upper())
    name = _esc(author_name or "Your name")
    if platform == "x":
        handle = "@" + (author_name or "you").lower().replace(" ", "")
        sub = _esc(handle) + " · now"
        foot = "<span>💬</span><span>🔁</span><span>❤️</span><span>👁</span>"
        cls = "post-card x-card"
    else:
        sub = _esc(author_headline or "Headline · 1st") + " · now"
        foot = "<span>👍 Like</span><span>💬 Comment</span><span>🔁 Repost</span><span>➤ Send</span>"
        cls = "post-card"
    chip = ""
    verdict_html = ""
    if overall is not None:
        chip = f'<span class="score-chip {_score_class(overall)}">{round(overall)}</span>'
        verdict_html = f'<div class="cand-meta"><span class="verdict">{_esc(verdict)}</span>{chip}</div>'
    return (
        f'<div class="{cls}">'
        f'<div class="p-head"><div class="avatar">{initial}</div>'
        f'<div class="who"><span class="name">{name}</span><span class="sub">{sub}</span></div></div>'
        f'<div class="p-body">{_esc(text)}</div>'
        f'<div class="p-foot">{foot}</div>'
        f"</div>{verdict_html}"
    )


def render_post_previews(candidates: list[dict], *, platform: str = "linkedin",
                         author_name: str = "", author_headline: str = "",
                         title: str = "Generated drafts") -> str:
    """Platform-styled preview cards, one per candidate (best first)."""
    cards = []
    for c in candidates[:3]:
        sc = c.get("score") or {}
        cards.append(_post_card(
            c.get("post") or c.get("text") or "", platform, author_name, author_headline,
            sc.get("overall"), sc.get("verdict") or "",
        ))
    label = "LinkedIn" if platform == "linkedin" else "X"
    head = f'<div class="head"><span class="title">{_esc(title)}</span><span class="chip">{label}</span></div>'
    return _page(_POST_CSS, head + '<div class="col">' + "".join(cards) + "</div>")


# ---- score card --------------------------------------------------------- #

def _dims_html(breakdown: dict) -> str:
    rows = []
    for key, label, weight in DIMENSIONS:
        v = float(breakdown.get(key) or 0)
        rows.append(
            f'<div class="dim"><span class="lbl">{label} <span style="color:#55556e">·{weight}%</span></span>'
            f'<span class="bar"><i style="width:{max(2, min(v, 100))}%;background:{_bar_color(v)}"></i></span>'
            f'<span class="val">{round(v)}</span></div>'
        )
    return "".join(rows)


def _signals_html(signals: list[dict], limit: int = 5) -> str:
    out = []
    for s in signals[:limit]:
        pos = s.get("impact") == "positive"
        out.append(
            f'<div class="sig {"pos" if pos else "neg"}"><span class="mark">{"+" if pos else "−"}</span>'
            f'<span><b>{_esc(s.get("label"))}</b> — {_esc(s.get("detail"))}</span></div>'
        )
    return "".join(out)


def _score_panel(result: dict, heading: str = "") -> str:
    overall = float(result.get("overall") or 0)
    head = f'<span class="chip">{_esc(heading)}</span>' if heading else ""
    return (
        '<div class="panel">'
        f'<div class="overall"><span class="big">{round(overall)}</span><span class="of">/ 100</span>'
        f'<span class="score-chip {_score_class(overall)}">{_esc(result.get("verdict") or "")[:80]}</span>{head}</div>'
        + _dims_html(result.get("breakdown") or {})
        + _signals_html(result.get("signals") or [])
        + "</div>"
    )


def render_score_card(result: dict, *, platform: str = "linkedin") -> str:
    label = "LinkedIn" if platform == "linkedin" else "X"
    head = f'<div class="head"><span class="title">Post score</span><span class="chip">{label}</span></div>'
    return _page(_SCORE_CSS, head + '<div class="col">' + _score_panel(result) + "</div>")


def render_rewrite_card(result: dict, *, platform: str = "linkedin",
                        author_name: str = "", author_headline: str = "") -> str:
    before = result.get("before") or {}
    after = result.get("after") or {}
    b, a = float(before.get("overall") or 0), float(after.get("overall") or 0)
    card = _post_card(result.get("rewrite") or "", platform, author_name, author_headline,
                      a, after.get("verdict") or "")
    changes = "".join(
        f'<div class="sig pos"><span class="mark">→</span><span><b>{_esc(c.get("dimension"))}</b> — '
        f'{_esc(c.get("note"))}</span></div>'
        for c in (result.get("changes") or [])[:6]
    )
    head = (
        f'<div class="head"><span class="title">Rewrite</span>'
        f'<span class="chip">score {round(b)} → <b style="color:#e2e2f0">{round(a)}</b></span></div>'
    )
    return _page(_POST_CSS + _SCORE_CSS,
                 head + '<div class="col">' + card + f'<div class="panel">{changes}</div></div>')


# ---- content plan ------------------------------------------------------- #

def render_plan_card(items: list[dict], *, platform: str = "linkedin") -> str:
    rows = []
    for it in items:
        rows.append(
            '<div class="slot">'
            f'<span class="n">{int(it.get("slot") or 0)}</span>'
            '<div class="body">'
            f'<span class="hook">{_esc(it.get("hook"))}</span>'
            f'<span class="angle">{_esc(it.get("angle"))}</span>'
            f'<div class="tags"><span class="chip">{_esc(it.get("pillar"))}</span>'
            f'<span class="chip">{_esc(it.get("format"))}</span>'
            f'<span class="chip">{_esc(it.get("archetype"))}</span></div>'
            "</div></div>"
        )
    label = "LinkedIn" if platform == "linkedin" else "X"
    head = f'<div class="head"><span class="title">Content plan · {len(items)} slots</span><span class="chip">{label}</span></div>'
    return _page(_PLAN_CSS, head + '<div class="col">' + "".join(rows) + "</div>")


# ---- ad visual gallery --------------------------------------------------- #

def render_visual_gallery(items: list[dict], *, headline: str = "") -> str:
    """Grid of generated ad creatives. Each item: {url, caption, hashtags, concept, alt}."""
    tiles = []
    for it in items:
        rel = _rel(it.get("url") or "")
        tags = " ".join("#" + t for t in (it.get("hashtags") or [])[:4])
        cap = (it.get("caption") or "")[:180]
        tiles.append(
            '<div class="tile">'
            f'<a data-rel="{_esc(rel)}" href="{_esc(rel)}" target="_blank" rel="noopener">'
            f'<img data-rel="{_esc(rel)}" src="{_esc(rel)}" alt="{_esc(it.get("alt") or cap)}" loading="eager"></a>'
            f'<span class="cap">{_esc(cap)}</span>'
            + (f'<span class="tags">{_esc(tags)}</span>' if tags else "")
            + f'<div style="padding:0 10px 8px 10px"><span class="chip">{_esc(it.get("concept") or "ad visual")}</span></div>'
            "</div>"
        )
    grid_cls = "grid single" if len(items) == 1 else "grid"
    head = f'<div class="head"><span class="title">{_esc(headline or "Ad visuals")}</span><span class="chip">SocialKit</span></div>'
    body = head + f'<div class="{grid_cls}">' + "".join(tiles) + "</div>"
    return _page(_GALLERY_CSS, body, _mount_script())
