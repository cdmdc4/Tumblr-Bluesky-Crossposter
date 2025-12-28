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
#                UTIL: BLUESKY CLIENT
# ---------------------------------------------------------

def get_bsky_client():
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)
    return client


# ---------------------------------------------------------
#                STATE MANAGEMENT
# ---------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict) and "posted_ids" in data:
                return data
            return {"posted_ids": []}
    except FileNotFoundError:
        return {"posted_ids": []}


def save_state(state):
    state["posted_ids"] = state["posted_ids"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------
#                TUMBLR API
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
#                MEDIA EXTRACTION
# ---------------------------------------------------------

def extract_images(post):
    urls = []

    # NPF blocks
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

    # Legacy photos
    if post.get("type") == "photo":
        for p in post.get("photos", []):
            try:
                urls.append(p["original_size"]["url"])
            except:
                pass

    clean = []
    for u in urls:
        if u not in clean:
            clean.append(u)

    return clean[:4]


def extract_gif(post):
    for url in extract_images(post):
        if url.lower().endswith(".gif"):
            return url
    return None


def extract_video(post):
    # NPF video blocks
    for block in post.get("content", []):
        if block.get("type") == "video":
            for media in block.get("media", []):
                if media.get("url", "").endswith(".mp4"):
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

def post_to_bluesky_images(client, tumblr_url, image_urls):
    uploaded = []
    for url in image_urls:
        data = requests.get(url).content
        blob = client.com.atproto.repo.upload_blob(data)
        uploaded.append({"image": blob.blob, "alt": ""})

    embed = {"$type": "app.bsky.embed.images", "images": uploaded}

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": tumblr_url,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        },
    )


def post_to_bluesky_gif(client, tumblr_url, gif_url):
    data = requests.get(gif_url).content
    blob = client.com.atproto.repo.upload_blob(data)

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


# ---------------------------------------------------------
#                MAIN LOGIC (with 30-post limit)
# ---------------------------------------------------------

def main():
    print("Running Tumblr → Bluesky crossposter…")

    client = get_bsky_client()

    posts = get_recent_tumblr_posts()
    if not posts:
        print("❌ No Tumblr posts found.")
        return

    # Sort newest → oldest
    posts = sorted(posts, key=lambda p: int(p["id"]))

    # ✅ HARD LIMIT: NEVER exceed 30 posts, no matter what Tumblr returns
    posts = posts[:30]

    state = load_state()
    posted_ids = state["posted_ids"]

    for post in posts:
        post_id = str(post.get("id_string") or post.get("id"))
        tumblr_link = post.get("post_url", "").strip()

        print("\n--- Checking Tumblr post:", post_id)

        if post_id in posted_ids:
            print("Already posted — skipping.")
            continue

        video = extract_video(post)
        gif = extract_gif(post)
        images = extract_images(post)

        if not video and not gif and not images:
            print("Text-only — skipping.")
            posted_ids.append(post_id)
            save_state(state)
            continue

        # VIDEO
        if video:
            print("Posting VIDEO (external)…")
            print("❌ Video posting temporarily disabled due to API instability.")
            # You can enable again once we fully fix your video pipeline.
            posted_ids.append(post_id)
            save_state(state)
            continue

        # GIF
        if gif:
            print("Posting GIF…")
            try:
                post_to_bluesky_gif(client, tumblr_link, gif)
                print("✔ GIF posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ GIF error:", e)
            continue

        # IMAGES
        if images:
            print(f"Posting {len(images)} IMAGES…")
            try:
                post_to_bluesky_images(client, tumblr_link, images)
                print("✔ Images posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ Image error:", e)
            continue

    print("\nDone!")


if __name__ == "__main__":
    main()
