import os
import json
import re
import requests
from atproto import Client

# ---------------------------------------------------------
#                CONFIGURATION (FROM ACTION SECRETS)
# ---------------------------------------------------------

TUMBLR_API_KEY = os.getenv("TUMBLR_API_KEY")
TUMBLR_BLOG = os.getenv("TUMBLR_BLOG_IDENTIFIER")

BSKY_USERNAME = os.getenv("BSKY_USERNAME")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

STATE_FILE = "tumblr_state.json"


# ---------------------------------------------------------
#                STATE MANAGEMENT
# ---------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return {
                "last_post_id": str(data.get("last_post_id")) if data.get("last_post_id") else None,
                "last_post_url": data.get("last_post_url"),
            }
    except FileNotFoundError:
        return {"last_post_id": None, "last_post_url": None}


def save_state(post_id, post_url):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_post_id": str(post_id), "last_post_url": post_url}, f)


# ---------------------------------------------------------
#                TUMBLR API (FETCH LAST 10 POSTS)
# ---------------------------------------------------------

def get_recent_tumblr_posts():
    url = f"https://api.tumblr.com/v2/blog/{TUMBLR_BLOG}/posts?api_key={TUMBLR_API_KEY}&limit=10"
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
#        BLUESKY DUPLICATE CHECK (FINAL, CORRECT)
# ---------------------------------------------------------

def bluesky_has_posted_url(tumblr_url):
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
        post = item.post
        record = post.record
        text = getattr(record, "text", "") or ""

        if norm in text:
            return True

    return False


# ---------------------------------------------------------
#                IMAGE EXTRACTION
# ---------------------------------------------------------

def extract_images(post):
    urls = []

    # Case 1 — NPF blocks
    for block in post.get("content", []):
        if block.get("type") == "image":
            for media in block.get("media", []):
                if "url" in media:
                    urls.append(media["url"])

    # Case 2 — trail HTML
    for item in post.get("trail", []):
        html = item.get("content_raw") or item.get("content") or ""
        urls += re.findall(r'<img[^>]+src="([^"]+)"', html)

    # Case 3 — body HTML
    body = post.get("body", "")
    urls += re.findall(r'<img[^>]+src="([^"]+)"', body)

    # Case 4 — legacy
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
#                VIDEO EXTRACTION
# ---------------------------------------------------------

def extract_video(post):
    # Case 1 — NPF
    for block in post.get("content", []):
        if block.get("type") == "video":
            for media in block.get("media", []):
                if "url" in media and media["url"].endswith(".mp4"):
                    return media["url"]

            if block.get("url", "").endswith(".mp4"):
                return block["url"]

    # Case 2 — legacy
    if post.get("video_url", "").endswith(".mp4"):
        return post["video_url"]

    # Case 3 — trail
    for t in post.get("trail", []):
        raw = t.get("content_raw", "")
        m = re.search(r'src="([^"]+\.mp4)"', raw)
        if m:
            return m.group(1)

    # Case 4 — <video> embed
    for embed in post.get("player", []):
        code = embed.get("embed_code", "")
        m = re.search(r'src="([^"]+\.mp4)"', code)
        if m:
            return m.group(1)

    return None


# ---------------------------------------------------------
#                BLUESKY POSTING
# ---------------------------------------------------------

def post_to_bluesky_images(tumblr_url, image_urls):
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)

    uploaded = []
    for url in image_urls:
        resp = requests.get(url)
img = resp.content

# detect correct MIME
mime = resp.headers.get("Content-Type", None)

# fallback MIME types based on file extension
if not mime:
    if url.lower().endswith(".gif"):
        mime = "image/gif"
    elif url.lower().endswith(".webp"):
        mime = "image/webp"
    else:
        mime = "image/jpeg"  # safe fallback

blob = client.com.atproto.repo.upload_blob(img, mime_type=mime)

uploaded.append({
    "image": blob.blob,
    "alt": "",
})


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


def post_to_bluesky_video(tumblr_url, video_url):
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)

    resp = requests.get(video_url)
    video_bytes = resp.content
    mime = resp.headers.get("Content-Type", "video/mp4")

    blob = client.com.atproto.repo.upload_blob(video_bytes, mime_type=mime)

    embed = {
        "$type": "app.bsky.embed.video",
        "video": {
            "video": blob.blob,
            "alt": "",
        }
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
#                      MAIN LOGIC
# ---------------------------------------------------------

def main():
    print("Running Tumblr → Bluesky check…")

    posts = get_recent_tumblr_posts()
    if not posts:
        print("❌ No Tumblr posts found.")
        return

    # Load local state
    state = load_state()
    last_post_id = state["last_post_id"]

    # Process posts oldest → newest
    posts = sorted(posts, key=lambda p: int(p["id"]))

    for post in posts:
        post_id = str(post.get("id_string") or post.get("id"))
        tumblr_link = post.get("post_url", "").strip()

        print("\n--- Checking Tumblr post:", post_id)

        # Skip if already processed locally
        if last_post_id and int(post_id) <= int(last_post_id):
            print("Already handled locally. Skipping.")
            continue

        # Skip if already on Bluesky
        if bluesky_has_posted_url(tumblr_link):
            print("Already on Bluesky. Skipping.")
            continue

        # Extract media
        video = extract_video(post)
        images = extract_images(post)

        # Skip text-only posts
        if not video and not images:
            print("Text post detected — skipping.")
            continue

        # Post video
        if video:
            print("Posting video to Bluesky…")
            try:
                post_to_bluesky_video(tumblr_link, video)
                print("✔ Video posted.")
                save_state(post_id, tumblr_link)
            except Exception as e:
                print("❌ Video error:", e)
            continue

        # Post images
        if images:
            print(f"Posting {len(images)} images to Bluesky…")
            try:
                post_to_bluesky_images(tumblr_link, images)
                print("✔ Images posted.")
                save_state(post_id, tumblr_link)
            except Exception as e:
                print("❌ Image error:", e)
            continue

    print("\nDone.")


if __name__ == "__main__":
    main()

