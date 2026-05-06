#!/usr/bin/env python3
import os
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import anthropic

RSS_FEEDS = [
    {"name": "Wall Street Journal",    "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml"},
    {"name": "New York Post",          "url": "https://nypost.com/feed/"},
    {"name": "Washington Examiner",    "url": "https://www.washingtonexaminer.com/feed"},
    {"name": "The Hill",               "url": "https://thehill.com/feed/"},
    {"name": "Reuters",                "url": "https://feeds.reuters.com/reuters/topNews"},
    {"name": "AP Wire",                "url": "https://rsshub.app/apnews/topics/apf-topnews"},
    {"name": "RealClearPolitics",      "url": "https://www.realclearpolitics.com/atom.xml"},
    {"name": "National Review",        "url": "https://www.nationalreview.com/feed/"},
    {"name": "The Free Press",         "url": "https://www.thefp.com/feed"},
    {"name": "Bloomberg Politics",     "url": "https://feeds.bloomberg.com/politics/news.rss"},
    {"name": "Fox News",               "url": "https://moxie.foxnews.com/google-publisher/latest.xml"},
    {"name": "Washington Times",       "url": "https://www.washingtontimes.com/rss/headlines/news/"},
    {"name": "Just The News",          "url": "https://justthenews.com/feed"},
    {"name": "Axios",                  "url": "https://api.axios.com/feed/"},
    {"name": "NY Times Politics",      "url": "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml"},
]

STATE_FILE    = Path("headline_state.json")
OUTPUT_FILE   = Path("index.html")
TEMPLATE_FILE = Path("template.html")
MAX_AGE_HOURS     = 48
REWRITE_AGE_HOURS = 24

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"headlines": [], "last_updated": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def story_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:12]

def hours_old(iso_timestamp):
    if not iso_timestamp:
        return 0
    dt = datetime.fromisoformat(iso_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600

def fetch_all_feeds():
    articles = []
    for feed_meta in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_meta["url"])
            for entry in feed.entries[:8]:
                articles.append({
                    "id":      story_id(entry.get("link", entry.get("title", ""))),
                    "title":   entry.get("title", "").strip(),
                    "url":     entry.get("link", ""),
                    "source":  feed_meta["name"],
                    "summary": entry.get("summary", "")[:300],
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f"[WARN] Failed to fetch {feed_meta['name']}: {e}")
        time.sleep(0.3)
    seen = set()
    unique = []
    for a in articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)
    return unique

def run_editorial(client, fresh_articles, existing_headlines):
    now_iso = datetime.now(timezone.utc).isoformat()
    existing_summary = []
    for h in existing_headlines:
        age = hours_old(h.get("first_seen"))
        existing_summary.append({
            "id":          h["id"],
            "current_hed": h["headline"],
            "source":      h["source"],
            "url":         h["url"],
            "hours_old":   round(age, 1),
            "tier":        h.get("tier", "center"),
            "rewrites":    h.get("rewrites", 0),
        })
    fresh_summary = [
        {"id": a["id"], "title": a["title"], "source": a["source"], "url": a["url"], "summary": a["summary"]}
        for a in fresh_articles[:60]
    ]
    prompt = f"""You are the editor of TRENCH REPORT, a center/center-right news aggregator.
TODAY: {now_iso}

RULES:
1. Headlines must be punchy, urgent, declarative.
2. Favor: economy, border, crime, military, politics, energy, culture, SCOTUS, geopolitics.
3. Stories over 24 hours old MUST have headline rewritten even if kept.
4. Stories over 48 hours old MUST be dropped.
5. One banner only. Two sub_banners. Up to 12 center stories. Up to 20 rail stories.
6. Rail stories get a tier tag: politics, world, culture, or tech.

EXISTING HEADLINES:
{json.dumps(existing_summary, indent=2)}

FRESH ARTICLES:
{json.dumps(fresh_summary, indent=2)}

Respond ONLY with valid JSON, no markdown, no backticks:
{{
  "banner": {{"id":"...","headline":"...","source":"...","url":"...","developing":false}},
  "sub_banners": [{{"id":"...","headline":"...","source":"...","url":"...","developing":false}}],
  "center_stories": [{{"id":"...","headline":"...","source":"...","url":"...","developing":false}}],
  "rail_stories": [{{"id":"...","headline":"...","source":"...","url":"...","tier":"politics"}}]
}}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

def merge_state(editorial, existing_headlines, fresh_articles):
    fresh_by_id = {a["id"]: a for a in fresh_articles}
    existing_by_id = {h["id"]: h for h in existing_headlines}
    now = datetime.now(timezone.utc).isoformat()
    new_state = []

    def build_entry(item, tier):
        sid = item["id"]
        existing = existing_by_id.get(sid, {})
        fresh = fresh_by_id.get(sid, {})
        old_hed = existing.get("headline", "")
        new_hed = item["headline"]
        rewrites = existing.get("rewrites", 0)
        if old_hed and old_hed != new_hed:
            rewrites += 1
        return {
            "id":           sid,
            "headline":     new_hed,
            "source":       item.get("source", fresh.get("source", "")),
            "url":          item.get("url", fresh.get("url", "")),
            "tier":         tier,
            "developing":   item.get("developing", False),
            "first_seen":   existing.get("first_seen", now),
            "last_updated": now,
            "rewrites":     rewrites,
        }

    if editorial.get("banner"):
        new_state.append(build_entry(editorial["banner"], "banner"))
    for item in editorial.get("sub_banners", []):
        new_state.append(build_entry(item, "sub_banner"))
    for item in editorial.get("center_stories", []):
        new_state.append(build_entry(item, "center"))
    for item in editorial.get("rail_stories", []):
        entry = build_entry(item, "rail")
        entry["rail_tier"] = item.get("tier", "politics")
        new_state.append(entry)
    return new_state

def render_html(headlines):
    by_tier = {
        "banner":     [h for h in headlines if h["tier"] == "banner"],
        "sub_banner": [h for h in headlines if h["tier"] == "sub_banner"],
        "center":     [h for h in headlines if h["tier"] == "center"],
        "rail":       [h for h in headlines if h["tier"] == "rail"],
    }
    banner     = by_tier["banner"][0] if by_tier["banner"] else None
    sub_banners = by_tier["sub_banner"][:2]
    center     = by_tier["center"][:12]
    rail       = by_tier["rail"][:20]

    now      = datetime.now(timezone.utc)
    dateline = now.strftime("%A, %B %-d, %Y")
    updated  = now.strftime("Updated %I:%M %p EST")

    def dev_tag(h):
        return ' <span class="dev-tag">DEVELOPING</span>' if h.get("developing") else ""

    if banner:
        banner_html = f'<a href="{banner["url"]}" id="banner-hed" target="_blank">{banner["headline"]}{dev_tag(banner)}</a><div id="banner-src">{banner["source"]}</div>'
    else:
        banner_html = '<div id="banner-hed">Loading latest headlines...</div>'

    sub_parts = []
    for i, s in enumerate(sub_banners[:2]):
        sub_parts.append(f'<div class="sub-banner"><a href="{s["url"]}" class="sub-hed" target="_blank">{s["headline"]}{dev_tag(s)}</a><span class="sub-src">{s["source"]}</span></div>')
        if i == 0 and len(sub_banners) > 1:
            sub_parts.append('<div class="sub-rule"></div>')
    sub_html = "\n".join(sub_parts)

    center_html_parts = []
    groups = [center[:4], center[4:8], center[8:]]
    for gi, group in enumerate(groups):
        if not group:
            continue
        center_html_parts.append('<div class="c-group">')
        for i, s in enumerate(group):
            cls = "c-hed big" if (i == 0 and gi == 0) else "c-hed"
            center_html_parts.append(f'<a class="{cls}" href="{s["url"]}" target="_blank">{s["headline"]}{dev_tag(s)}<span class="c-src">{s["source"]}</span></a>')
        center_html_parts.append('</div>')
    center_html = "\n".join(center_html_parts)

    rail_tiers  = ["politics", "world", "culture", "tech"]
    rail_labels = {"politics": "In The Trenches", "world": "World", "culture": "Culture", "tech": "Tech"}
    rail_html_parts = []
    for tier_key in rail_tiers:
        stories = [s for s in rail if s.get("rail_tier") == tier_key]
        if not stories:
            continue
        rail_html_parts.append(f'<span class="sec">{rail_labels[tier_key]}</span>')
        for s in stories[:6]:
            age = hours_old(s.get("first_seen"))
            fire_cls = " fire" if age < 2 else ""
            rail_html_parts.append(f'<a class="r-story{fire_cls}" href="{s["url"]}" target="_blank">{s["headline"]}{dev_tag(s)}</a>')
    rail_html = "\n".join(rail_html_parts)

    template = TEMPLATE_FILE.read_text()
    return (template
        .replace("{{DATELINE}}", dateline)
        .replace("{{UPDATED}}", updated)
        .replace("{{BANNER_HTML}}", banner_html)
        .replace("{{SUB_BANNERS_HTML}}", sub_html)
        .replace("{{CENTER_HTML}}", center_html)
        .replace("{{RAIL_HTML}}", rail_html)
    )

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)
    print(f"[{datetime.now().isoformat()}] Starting...")
    state    = load_state()
    existing = [h for h in state.get("headlines", []) if hours_old(h.get("first_seen")) < MAX_AGE_HOURS]
    print(f"  Existing after age filter: {len(existing)}")
    fresh = fetch_all_feeds()
    print(f"  Fetched {len(fresh)} articles")
    editorial = run_editorial(client, fresh, existing)
    new_headlines = merge_state(editorial, existing, fresh)
    save_state({"headlines": new_headlines, "last_updated": datetime.now(timezone.utc).isoformat()})
    html = render_html(new_headlines)
    OUTPUT_FILE.write_text(html)
    print(f"  Done. {len(new_headlines)} headlines published.")

if __name__ == "__main__":
    main()
