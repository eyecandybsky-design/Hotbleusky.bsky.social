import os
import json
import time
import random
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from atproto import Client

# ============================================================
# HOTBLEUSKY TOTAL REPOSTER
# Account: hotbleusky.bsky.social
# RUN_1 (:16): RedFox + Repost Always + Multibooster + Promo Random + Own
# RUN_2 (:46): RedFox + Repost Always + Promo Last + Own
# Triggered externally by cron-job.org via workflow_dispatch.
# ============================================================

USERNAME = "hotbleusky.bsky.social"
PASSWORD = os.getenv("HB_BSKY_PASSWORD")
RUN_MODE = os.getenv("RUN_MODE", "RUN_1").upper()
STATE_FILE = os.getenv("STATE_FILE", "hotbleusky_state.json")

MAX_ACTIONS = 100
MAX_PER_USER = 3
NORMAL_LOOKBACK_HOURS = 3
LIKE_LOOKBACK_HOURS = 6
MAX_LIKES_PER_USER = 10
REPOST_ALWAYS_REBOOST_HOURS = 8
PROMO_MAX_AGE_HOURS = 12
PROMO_RANDOM_POOL = 25
PROMO_RANDOM_REUSE_HOURS = 24
SLEEP_SECONDS = 2

REDFOX_FEED = "https://bsky.app/profile/did:plc:cxrt7ggxkamgzxa47cggtees/feed/aaaoirmgh53zw"
REPOST_ALWAYS_LIST = "https://bsky.app/profile/did:plc:5tbowzedh5d6wvhc5dncydbx/lists/3mwm5mgb5372m"
MULTIBOOSTER_LIST = "https://bsky.app/profile/did:plc:5tbowzedh5d6wvhc5dncydbx/lists/3mwm5rsmfew2c"
PROMO_LAST_LIST = "https://bsky.app/profile/did:plc:5tbowzedh5d6wvhc5dncydbx/lists/3mwm5tqizan2y"
PROMO_RANDOM_LIST = "https://bsky.app/profile/did:plc:5tbowzedh5d6wvhc5dncydbx/lists/3mwm5vg4qlg2c"
HASHTAG_BLACKLIST_LIST = "https://bsky.app/profile/did:plc:5tbowzedh5d6wvhc5dncydbx/lists/3mwm5xxglhp2r"

# Empty hashtag slots intentionally disabled.
HASHTAGS = ["", "", ""]


def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def load_state():
    default = {
        "normal_reposts": {},
        "repost_always_reboosted": {},
        "multibooster_seen": {},
        "promo_random_used": {},
        "own_last_new_uri": None,
        "likes": {},
    }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in default.items():
                data.setdefault(k, v)
            return data
    except Exception:
        return default


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


def uri_parts(uri):
    # at://did/app.bsky.feed.post/rkey
    p = uri.split("/")
    return p[2], p[-1]


def get_attr(obj, path, default=None):
    cur = obj
    for name in path.split("."):
        if cur is None:
            return default
        cur = getattr(cur, name, None)
    return default if cur is None else cur


def post_datetime(post):
    return parse_dt(get_attr(post, "record.created_at")) or parse_dt(get_attr(post, "indexed_at"))


def is_reply(post):
    return get_attr(post, "record.reply") is not None

def embed_type(post):
    for embed in (get_attr(post, "record.embed"), get_attr(post, "embed")):
        if embed is None: continue
        blob = " ".join((embed.__class__.__name__.lower(),
                         str(getattr(embed, "py_type", "") or "").lower(),
                         str(getattr(embed, "$type", "") or "").lower()))
        if "record_with_media" in blob or "recordwithmedia" in blob: return "quote_media"
        if "images" in blob: return "images"
        if "video" in blob: return "video"
        if "external" in blob: return "external"
        if "record" in blob: return "quote"
    return "none"

def is_quote(post):
    return embed_type(post) in {"quote", "quote_media"}

def has_media(post):
    return embed_type(post) in {"images", "video"}

def is_repost_feed_item(item):
    reason = getattr(item, "reason", None)
    return reason is not None and "repost" in reason.__class__.__name__.lower()

def suitable(post, allow_reply=False):
    return has_media(post) and not is_quote(post) and (allow_reply or not is_reply(post))

def age_ok(post, hours):
    dt = post_datetime(post)
    return bool(dt and dt >= now_utc() - timedelta(hours=hours))


def list_uri(url):
    # Resolve list owner handle/DID + rkey to at:// DID URI.
    parts = url.rstrip("/").split("/")
    owner = parts[-3]
    rkey = parts[-1]
    if owner.startswith("did:"):
        did = owner
    else:
        did = client.resolve_handle(owner).did
    return f"at://{did}/app.bsky.graph.list/{rkey}"


def feed_uri(url):
    parts = url.rstrip("/").split("/")
    owner = parts[-3]
    rkey = parts[-1]
    if owner.startswith("did:"):
        did = owner
    else:
        did = client.resolve_handle(owner).did
    return f"at://{did}/app.bsky.feed.generator/{rkey}"


def members_from_list(url, label="List"):
    out, cursor = [], None
    uri = list_uri(url)
    print(f"[{label}] list ophalen: {uri}")
    while True:
        try:
            res = client.app.bsky.graph.get_list({"list": uri, "limit": 100, "cursor": cursor})
            out.extend([x.subject.did for x in res.items])
            cursor = res.cursor
            if not cursor:
                break
        except Exception as e:
            print(f"LIST ERROR {url}: {e}")
            break
    # preserve order, unique
    out = list(dict.fromkeys(out))
    print(f"[{label}] {len(out)} account(s) gevonden")
    return out


def author_feed(actor, limit=50, label="AUTHOR"):
    """Fetch only the newest page. Never deep-page through reply-heavy accounts."""
    try:
        res = client.app.bsky.feed.get_author_feed({"actor": actor, "limit": min(100, limit)})
        posts = []
        for item in res.feed:
            if is_repost_feed_item(item):
                continue
            posts.append(item.post)
        print(f"[{label}] {actor}: {len(posts)} recente feed-item(s) bekeken (1 API page)")
        return posts
    except Exception as e:
        print(f"AUTHOR ERROR {actor}: {type(e).__name__}: {e}")
        return []


def diagnostic_rejections(posts, label, max_show=5):
    counts = defaultdict(int); examples = []
    for p in posts:
        kind = embed_type(p)
        if not has_media(p): reason = f"geen foto/video ({kind})"
        elif is_quote(p): reason = "quote"
        elif is_reply(p): reason = "reply"
        else: reason = "geschikt"
        counts[reason] += 1
        if reason != "geschikt" and len(examples) < max_show:
            examples.append(f"{getattr(p, 'uri', '?')} -> {reason}")
    print(f"[{label}] filter: {dict(counts)}")
    for x in examples: print(f"[{label}] FILTER VOORBEELD: {x}")

def repost(post, label, force=False):
    global actions
    if actions >= MAX_ACTIONS:
        return False
    uri, cid = post.uri, post.cid
    if not force and uri in state["normal_reposts"]:
        return False
    try:
        client.repost(uri, cid)
        actions += 1
        if not force:
            state["normal_reposts"][uri] = now_utc().isoformat()
        print(f"REPOST [{label}] {uri}")
        time.sleep(SLEEP_SECONDS)
        return True
    except Exception as e:
        # If already reposted, normal source can still be considered processed.
        msg = str(e).lower()
        if not force and ("already" in msg or "duplicate" in msg):
            state["normal_reposts"][uri] = now_utc().isoformat()
        print(f"REPOST ERROR [{label}] {uri}: {e}")
        return False


def reboost(post, label):
    global actions
    if actions >= MAX_ACTIONS:
        return False
    uri, cid = post.uri, post.cid
    try:
        did, rkey = uri_parts(uri)
        # Locate our repost record for this post, if present.
        try:
            res = client.app.bsky.feed.get_posts({"uris": [uri]})
            if res.posts:
                viewer = res.posts[0].viewer
                repost_uri = getattr(viewer, "repost", None) if viewer else None
                if repost_uri:
                    _, repost_rkey = uri_parts(repost_uri)
                    client.delete_repost(repost_rkey)
                    time.sleep(SLEEP_SECONDS)
        except Exception as e:
            print(f"UNREPOST NOTE [{label}] {uri}: {e}")
        client.repost(uri, cid)
        actions += 1
        print(f"REBOOST [{label}] {uri}")
        time.sleep(SLEEP_SECONDS)
        return True
    except Exception as e:
        print(f"REBOOST ERROR [{label}] {uri}: {e}")
        return False


def like_post(post, label, per_author):
    uri = post.uri
    author = post.author.did
    if per_author[author] >= MAX_LIKES_PER_USER:
        return
    if uri in state["likes"]:
        return
    try:
        client.like(uri, post.cid)
        state["likes"][uri] = now_utc().isoformat()
        per_author[author] += 1
        print(f"LIKE [{label}] {uri}")
        time.sleep(0.5)
    except Exception as e:
        msg = str(e).lower()
        if "already" in msg or "duplicate" in msg:
            state["likes"][uri] = now_utc().isoformat()
        else:
            print(f"LIKE ERROR [{label}] {uri}: {e}")


def collect_redfox():
    candidates = []
    likes = []
    print("[REDFOX] feed ophalen...")
    try:
        res = None
        for attempt in range(1, 4):
            try:
                res = client.app.bsky.feed.get_feed({"feed": feed_uri(REDFOX_FEED), "limit": 100})
                break
            except Exception as e:
                print(f"[REDFOX] poging {attempt}/3 mislukt: {type(e).__name__}: {e}")
                if attempt < 3: time.sleep(5)
                else: raise
        print(f"[REDFOX] {len(res.feed)} feed item(s) opgehaald")
        per_author = defaultdict(int)
        media_count = recent_count = 0
        for item in res.feed:
            if is_repost_feed_item(item):
                continue
            p = item.post
            if suitable(p, allow_reply=True):
                media_count += 1
                if age_ok(p, NORMAL_LOOKBACK_HOURS):
                    recent_count += 1
            if suitable(p, allow_reply=True) and age_ok(p, NORMAL_LOOKBACK_HOURS) and per_author[p.author.did] < MAX_PER_USER:
                candidates.append((p, "RedFoxOfficial"))
                per_author[p.author.did] += 1
            if suitable(p, allow_reply=True) and age_ok(p, LIKE_LOOKBACK_HOURS):
                likes.append((p, "RedFoxOfficial"))
        print(f"[REDFOX] geschikte media totaal={media_count}, binnen 3u={recent_count}, repost kandidaten={len(candidates)}, like kandidaten={len(likes)}")
    except Exception as e:
        print(f"FEED ERROR RedFoxOfficial: {type(e).__name__}: {e}")
    return candidates, likes


def collect_repost_always():
    candidates, likes, cache = [], [], {}
    print("[REPOST ALWAYS] starten...")
    for actor in members_from_list(REPOST_ALWAYS_LIST, "REPOST ALWAYS"):

        count = 0
        actor_posts = author_feed(actor, 50, "REPOST ALWAYS")
        cache[actor] = actor_posts
        diagnostic_rejections(actor_posts, f"REPOST ALWAYS {actor}", 2)
        for p in actor_posts:
            if suitable(p) and age_ok(p, NORMAL_LOOKBACK_HOURS):
                if count < MAX_PER_USER:
                    candidates.append((p, "Repost Always"))
                    count += 1
                if age_ok(p, LIKE_LOOKBACK_HOURS):
                    likes.append((p, "Repost Always"))
    print(f"[REPOST ALWAYS] repost kandidaten={len(candidates)}, like kandidaten={len(likes)}")
    return candidates, likes, cache


def process_repost_always_reboost(cache):
    # One-time reboost after 8h, using the SAME fetched posts: no second list/API sweep.
    threshold = now_utc() - timedelta(hours=REPOST_ALWAYS_REBOOST_HOURS)
    print(f"[REPOST ALWAYS +8H] cache gebruiken voor {len(cache)} account(s)")
    for actor, posts in cache.items():
        for p in posts:
            if not suitable(p):
                continue
            first = parse_dt(state["normal_reposts"].get(p.uri))
            if not first or first > threshold or p.uri in state["repost_always_reboosted"]:
                continue
            if reboost(p, "Repost Always +8h"):
                state["repost_always_reboosted"][p.uri] = now_utc().isoformat()


def process_multibooster(like_jobs):
    print("[MULTIBOOSTER] starten...")
    for actor in members_from_list(MULTIBOOSTER_LIST, "MULTIBOOSTER"):

        eligible = [p for p in author_feed(actor, 20, "MULTIBOOSTER") if suitable(p) and age_ok(p, PROMO_MAX_AGE_HOURS)][:3]
        print(f"[MULTIBOOSTER] {actor}: {len(eligible)} geschikte post(s) in laatste 3 / max 12u")
        if not eligible:
            continue
        for p in eligible:
            if age_ok(p, LIKE_LOOKBACK_HOURS):
                like_jobs.append((p, "Multibooster"))
        seen = state["multibooster_seen"].setdefault(actor, {})
        unvisited = [p for p in eligible if p.uri not in seen]
        if not unvisited:
            # Completed current 3-post cycle: clear only current cycle markers.
            for p in eligible:
                seen.pop(p.uri, None)
            unvisited = eligible[:]
        # oldest unvisited first gives all three a predictable turn.
        chosen = sorted(unvisited, key=lambda p: post_datetime(p) or now_utc())[0]
        if reboost(chosen, "Multibooster"):
            seen[chosen.uri] = now_utc().isoformat()


def process_promo_last(like_jobs):
    print("[PROMO LAST] starten...")
    for actor in members_from_list(PROMO_LAST_LIST, "PROMO LAST"):

        eligible = [p for p in author_feed(actor, 20, "PROMO LAST") if suitable(p) and age_ok(p, PROMO_MAX_AGE_HOURS)]
        for p in eligible:
            if age_ok(p, LIKE_LOOKBACK_HOURS):
                like_jobs.append((p, "Promo Last"))
        if eligible:
            reboost(eligible[0], "Promo Last")


def process_promo_random(like_jobs):
    print("[PROMO RANDOM] starten...")
    for actor in members_from_list(PROMO_RANDOM_LIST, "PROMO RANDOM"):

        eligible = [p for p in author_feed(actor, 60, "PROMO RANDOM") if suitable(p)][:PROMO_RANDOM_POOL]
        print(f"[PROMO RANDOM] {actor}: {len(eligible)} geschikte post(s) in pool")
        for p in eligible:
            if age_ok(p, LIKE_LOOKBACK_HOURS):
                like_jobs.append((p, "Promo Random"))
        if not eligible:
            continue
        used = state["promo_random_used"].setdefault(actor, {})
        cutoff = now_utc() - timedelta(hours=PROMO_RANDOM_REUSE_HOURS)
        fresh = [p for p in eligible if p.uri not in used]
        if fresh:
            chosen = random.choice(fresh)
        else:
            reusable = [p for p in eligible if (parse_dt(used.get(p.uri)) or now_utc()) <= cutoff]
            if not reusable:
                print(f"PROMO RANDOM SKIP {actor}: geen post beschikbaar na 24u-regel")
                continue
            chosen = random.choice(reusable)
        if reboost(chosen, "Promo Random"):
            used[chosen.uri] = now_utc().isoformat()


def process_own_posts():
    # Stop reboosting own posts when there has been no new original media post for 24h.
    print("[OWN POSTS] ophalen...")
    fetched = author_feed(USERNAME, 30, "OWN POSTS")
    diagnostic_rejections(fetched, "OWN POSTS", 8)
    posts = [p for p in fetched if suitable(p)][:3]
    print(f"[OWN POSTS] {len(fetched)} media-feed item(s), {len(posts)} geschikte originele media in top 3")
    if not posts:
        print("OWN POSTS SKIP: geen originele mediapost gevonden")
        return
    newest_dt = post_datetime(posts[0])
    if not newest_dt or newest_dt < now_utc() - timedelta(hours=24):
        print("OWN POSTS SKIP: geen nieuwe originele mediapost in de laatste 24 uur")
        return
    for p in reversed(posts):  # old -> new, but always at the very end of run
        reboost(p, "Own Posts")


if not PASSWORD:
    raise SystemExit("HB_BSKY_PASSWORD ontbreekt")
if RUN_MODE not in {"RUN_1", "RUN_2"}:
    raise SystemExit("RUN_MODE moet RUN_1 of RUN_2 zijn")

client = Client()
client.login(USERNAME, PASSWORD)
state = load_state()
actions = 0
like_jobs = []
normal_candidates = []

print("=" * 60)
print("HOTBLEUSKY TOTAL REPOSTER")
print(f"MODE: {RUN_MODE}")
print(f"TIME: {now_utc().isoformat()}")
print("=" * 60)

# Normal sources are gathered first and mixed chronologically old -> new.
redfox, redfox_likes = collect_redfox()
normal_candidates.extend(redfox)
like_jobs.extend(redfox_likes)

ra, ra_likes, ra_cache = collect_repost_always()
normal_candidates.extend(ra)
like_jobs.extend(ra_likes)

# Deduplicate and enforce max 3/account across normal sources.
unique = {}
for p, label in normal_candidates:
    unique.setdefault(p.uri, (p, label))
ordered = sorted(unique.values(), key=lambda x: post_datetime(x[0]) or now_utc())
per_author = defaultdict(int)
for p, label in ordered:
    if actions >= MAX_ACTIONS:
        break
    if per_author[p.author.did] >= MAX_PER_USER:
        continue
    if repost(p, label):
        per_author[p.author.did] += 1

# One-time +8h Repost Always refresh.
process_repost_always_reboost(ra_cache)

# Run-specific boosters.
if RUN_MODE == "RUN_1":
    process_multibooster(like_jobs)
    process_promo_random(like_jobs)
else:
    process_promo_last(like_jobs)

# Likes are independent of the repost action cap; never unlike.
like_per_author = defaultdict(int)
like_unique = {}
for p, label in like_jobs:
    like_unique.setdefault(p.uri, (p, label))
for p, label in sorted(like_unique.values(), key=lambda x: post_datetime(x[0]) or now_utc()):
    like_post(p, label, like_per_author)

# Own Posts ALWAYS last.
process_own_posts()

save_state(state)
print(f"DONE: {actions} repost/reboost actions; state opgeslagen.")
