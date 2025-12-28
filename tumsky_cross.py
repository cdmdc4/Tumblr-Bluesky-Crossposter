import os
import json
import re
import requests
from atproto import Client

# ---------------------------------------------------------
#                CONFIGURATION
# ---------------------------------------------------------

TUMBLR_API_KEY = os.getenv("TUMBLR_API_KEY")
TUMBLR_BLOG = os.getenv("TUMBLR_BLOG_IDENTIFIER")

BSKY_USERNAME = os.getenv("BSKY_USERNAME")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

STATE_FILE = "tumblr_state.json"


# ---------------------------------------------------------
#                STATE MANAGEMENT (A2: FULL RESET)
# ---------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict) and "posted_ids" in data:
                return data
            else:
                # A2 reset: ignore old format & create blank state
                return {"posted_ids": []}
    except FileNotFoundError:
        return {"posted_ids": []}


def save_state(state):
    # Always keep the last 500 IDs max
    state["posted_ids"] = state["posted_ids"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------
#                TUMBLR API (FETCH LAST 30)
# ---------------------------------------------------------

def get_recent_tumblr_posts():
    url = f"https://api.tumblr.com/v2/blog/{TUMBLR_BLOG}/posts?api_key={TUMBLR_API_KEY}&limit=30"
    resp = requests.get(url)
    data = resp.json()

    try:
        return data["response"]["posts"]
    except:
        return []


# ---------------------------------------------------------
#                URL NORMALIZATION
# ---------------------------------------------------------

def normalize_url(url):
    if not url:
        return ""
    url = re.sub(r'\?.*$', "", url)
    return url.rstrip("/")


# ---------------------------------------------------------
#                BLUESKY DUPLICATE CHECK
# ---------------------------------------------------------

def bluesky_has_posted_url(tumblr_url):
    """This is now only a fallback. ID-based dedupe is primary."""
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)
    did = client.me.did

    norm = normalize_url(tumblr_url)

    try:
        feed = client.app.bsky.feed.get_author_feed(params={"actor": did, "limit": 25})
    except Exception as e:
        print("Error fetching Bluesky feed:", e)
        return False

    for item in feed.feed:
        record = item.post.record
        text = getattr(record, "text", "") or ""
        if norm in text:
            return True

    return False


# ---------------------------------------------------------
#                IMAGE EXTRACTION
# ---------------------------------------------------------

def extract_images(post):
    urls = []

    # NPF images
    for block in post.get("content", []):
        if block.get("type") == "image":
            for media in block.get("media", []):
                if "url" in media:
                    urls.append(media["url"])

    # Trail HTML
    for item in post.get("trail", []):
        html = item.get("content_raw") or ""
        urls += re.findall(r'<img[^>]+src="([^"]+)"', html)

    # Body HTML
    body = post.get("body", "")
    urls += re.findall(r'<img[^>]+src="([^"]+)"', body)

    # Legacy photo posts
    if post.get("type") == "photo":
        for p in post.get("photos", []):
            try:
                urls.append(p["original_size"]["url"])
            except:
                pass

    # Deduplicate
    clean = []
    for u in urls:
        if u not in clean:
            clean.append(u)

    return clean[:4]


# ---------------------------------------------------------
#                GIF EXTRACTION
# ---------------------------------------------------------

def extract_gif(post):
    all_imgs = extract_images(post)
    for url in all_imgs:
        if url.lower().endswith(".gif"):
            return url
    return None


# ---------------------------------------------------------
#                VIDEO EXTRACTION
# ---------------------------------------------------------

def extract_video(post):
    # NPF video blocks
    for block in post.get("content", []):
        if block.get("type") == "video":
            for media in block.get("media", []):
                if "url" in media and media["url"].endswith(".mp4"):
                    return media["url"]

            if block.get("url", "").endswith(".mp4"):
                return block["url"]

    # Legacy
    if post.get("video_url", "").endswith(".mp4"):
        return post["video_url"]

    # Trail HTML
    for t in post.get("trail", []):
        raw = t.get("content_raw", "")
        m = re.search(r'src="([^"]+\.mp4)"', raw)
        if m:
            return m.group(1)

    # Player embeds
    for embed in post.get("player", []):
        code = embed.get("embed_code", "")
        m = re.search(r'src="([^"]+\.mp4)"', code)
        if m:
            return m.group(1)

    return None


# ---------------------------------------------------------
#                BLUESKY UPLOADS
# ---------------------------------------------------------

def post_to_bluesky_images(tumblr_url, image_urls):
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)

    uploaded = []
    for url in image_urls:
        img = requests.get(url).content
        blob = client.com.atproto.repo.upload_blob(img)
        uploaded.append({"image": blob.blob, "alt": ""})

    embed = {
        "$type": "app.bsky.embed.images",
        "images": uploaded,
    }

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": tumblr_url,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        },
    )


def post_to_bluesky_gif(tumblr_url, gif_url):
    client = Client()
    client.login(BSKKY_USERNAME, BSKY_PASSWORD)

    resp = requests.get(gif_url)
    gif_bytes = resp.content
    blob = client.com.atproto.repo.upload_blob(gif_bytes, mime_type="image/gif")

    embed = {
        "$type": "app.bsky.embed.images",
        "images": [{"image": blob.blob, "alt": ""}],
    }

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": tumblr_url,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        },
    )


def post_to_bluesky_video(tumblr_url, video_url):
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)

    resp = requests.get(video_url)
    video_bytes = resp.content
    mime = resp.headers.get("Content-Type", "video/mp4")

    blob = client.com.atproto.repo.upload_blob(video_bytes, mime_type=mime)

    embed = {
        "$type": "app.bsky.embed.video",
        "video": {"video": blob.blob, "alt": ""},
    }

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": tumblr_url,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        },
    )


# ---------------------------------------------------------
#                     MAIN LOGIC
# ---------------------------------------------------------

def main():
    print("Running Tumblr → Bluesky crossposter…")

    posts = get_recent_tumblr_posts()
    if not posts:
        print("❌ No Tumblr posts found.")
        return

    posts = sorted(posts, key=lambda p: int(p["id"]))  # oldest → newest
    state = load_state()
    posted_ids = state["posted_ids"]

    for post in posts:
        post_id = str(post.get("id_string") or post.get("id"))
        tumblr_link = post.get("post_url", "").strip()

        print("\n--- Checking Tumblr post:", post_id)

        # Skip if already posted (primary dedupe)
        if post_id in posted_ids:
            print("Already posted (ID match) — skipping.")
            continue

        # Extract media
        video = extract_video(post)
        gif = extract_gif(post)
        images = extract_images(post)

        # Skip text-only posts
        if not video and not gif and not images:
            print("Text-only post — skipping.")
            posted_ids.append(post_id)
            save_state(state)
            continue

        # VIDEO first
        if video:
            print("Posting VIDEO…")
            try:
                post_to_bluesky_video(tumblr_link, video)
                print("✔ Video posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ Video error:", e)
            continue

        # GIF second
        if gif:
            print("Posting GIF…")
            try:
                post_to_bluesky_gif(tumblr_link, gif)
                print("✔ GIF posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ GIF error:", e)
            continue

        # IMAGES last
        if images:
            print(f"Posting {len(images)} IMAGES…")
            try:
                post_to_bluesky_images(tumblr_link, images)
                print("✔ Images posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ Image error:", e)
            continue

    print("\nDone!")


if __name__ == "__main__":
    main()
