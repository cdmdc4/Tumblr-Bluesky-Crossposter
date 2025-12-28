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
#                TUMBLR API
# ---------------------------------------------------------

def get_latest_tumblr_post():
    url = f"https://api.tumblr.com/v2/blog/{TUMBLR_BLOG}/posts?api_key={TUMBLR_API_KEY}&limit=1"
    resp = requests.get(url)
    data = resp.json()

    try:
        return data["response"]["posts"][0]
    except:
        return None


# ---------------------------------------------------------
#        BLUESKY DUPLICATE CHECK (FINAL, ROBUST)
# ---------------------------------------------------------

def normalize_url(url):
    if not url:
        return ""
    url = re.sub(r'\?.*$', "", url)
    return url.rstrip("/")


def bluesky_has_posted_url(tumblr_url):
    """Check ALL possible Bluesky locations where a Tumblr link could be stored."""
    norm = normalize_url(tumblr_url)

    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)

    feed = client.app.bsky.feed.get_author_feed(
        params={"actor": client.me.did, "limit": 5}
    )

    for item in feed.feed:
        record = item.post.record

        # 1. TEXT FIELD
        if isinstance(record, dict):
            text = record.get("text", "")
            if norm in text:
                return True

        # 2. EMBED: external link
        embed = record.get("embed")
        if embed:

            if embed.get("$type") == "app.bsky.embed.external":
                uri = embed["external"].get("uri", "")
                if normalize_url(uri) == norm:
                    return True

            # 3. EMBED: image captions
            if embed.get("$type") == "app.bsky.embed.images":
                for img in embed.get("images", []):
                    alt = img.get("alt", "")
                    if norm in alt:
                        return True

        # 4. FACETS (rich text links)
        if "facets" in record:
            for facet in record["facets"]:
                for feature in facet.get("features", []):
                    if feature.get("$type") == "app.bsky.richtext.facet#link":
                        uri = feature.get("uri", "")
                        if normalize_url(uri) == norm:
                            return True

    return False


# ---------------------------------------------------------
#                IMAGE EXTRACTION
# ---------------------------------------------------------

def extract_all_images(post):
    urls = []

    for item in post.get("trail", []):
        html = item.get("content_raw") or item.get("content") or ""
        urls += re.findall(r'<img[^>]+src="([^"]+)"', html)

    body = post.get("body", "")
    urls += re.findall(r'<img[^>]+src="([^"]+)"', body)

    if post.get("type") == "photo" and "photos" in post:
        for p in post["photos"]:
            try:
                urls.append(p["original_size"]["url"])
            except:
                pass

    clean = []
    for u in urls:
        if u not in clean:
            clean.append(u)

    return clean[:4]


# ---------------------------------------------------------
#                BLUESKY POSTING
# ---------------------------------------------------------

def post_to_bluesky_multi(tumblr_url, image_urls):
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


# ---------------------------------------------------------
#                MAIN LOGIC
# ---------------------------------------------------------

def main():
    print("Running Tumblr → Bluesky check (GitHub Actions mode)…")

    state = load_state()
    last_post_id = state["last_post_id"]
    last_post_url = state["last_post_url"]

    post = get_latest_tumblr_post()
    if not post:
        print("❌ Could not fetch Tumblr post.")
        return

    post_id = str(post.get("id_string") or post.get("id"))
    tumblr_link = post.get("post_url", "").strip()

    print(f"Latest Tumblr post: {post_id}")
    print(f"Stored last id    : {last_post_id}")

    # FULL robust duplicate check
    if bluesky_has_posted_url(tumblr_link):
        print("Bluesky already posted this Tumblr link. Exiting.")
        save_state(post_id, tumblr_link)
        return

    # Local file duplicate check
    if post_id == last_post_id or normalize_url(tumblr_link) == normalize_url(last_post_url):
        print("No new posts. Exiting.")
        return

    images = extract_all_images(post)
    if not images:
        print("No images found. Saving state.")
        save_state(post_id, tumblr_link)
        return

    print(f"Posting {len(images)} images to Bluesky…")

    try:
        post_to_bluesky_multi(tumblr_link, images)
        print("✔ Success! Updating state.")
        save_state(post_id, tumblr_link)
    except Exception as e:
        print("❌ Bluesky error:", e)


if __name__ == "__main__":
    main()
