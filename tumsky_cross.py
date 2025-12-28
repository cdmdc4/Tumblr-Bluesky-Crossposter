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
        feed = client.app.bsky.feed.get_author_feed(params={"actor": did, "limit": 5})
    except Exception as e:
        print("Error fetching Bluesky feed:", e)
        return False

    for item in feed.feed:
        post = item.post
        record = post.record

        text = getattr(record, "text", "") or ""
        if norm in text:
            return True

        embed = getattr(record, "embed", None)
        if not embed:
            continue

        etype = getattr(embed, "$type", "")

        # external embed
        if etype == "app.bsky.embed.external#view":
            external = getattr(embed, "external", None)
            if external:
                uri = getattr(external, "uri", "")
                if norm in uri:
                    return True

        # record-with-media
        if etype == "app.bsky.embed.recordWithMedia#view":
            rec = getattr(embed, "record", None)
            if rec and norm in getattr(rec, "uri", ""):
                return True

        # record-only
        if etype == "app.bsky.embed.record#view":
            rec = getattr(embed, "record", None)
            if rec and norm in getattr(rec, "uri", ""):
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

    # Case 4 — legacy photo posts
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
#                VIDEO EXTRACTION (FULL NPF SUPPORT)
# ---------------------------------------------------------

def extract_video(post):
    """
    Extracts Tumblr video URL, supporting:
    - NPF blocks
    - legacy video_url
    - player[] embeds
    - trail HTML
    """

    # Case 1 — legacy
    if post.get("video_url"):
        return post["video_url"]

    # Case 2 — NPF "content" blocks
    for block in post.get("content", []):
        if block.get("type") == "video":
            # Try different ways NPF stores media
            if block.get("url"):
                return block["url"]

            media_list = block.get("media", [])
            if media_list:
                m = media_list[0]
                if m.get("url"):
                    return m["url"]

    # Case 3 — search trail HTML for .mp4
    for t in post.get("trail", []):
        raw = t.get("content_raw", "")
        match = re.search(r'src="([^"]+\.mp4)"', raw)
        if match:
            return match.group(1)

    # Case 4 — search legacy embed players
    for item in post.get("player", []):
        embed = item.get("embed_code", "")
        match = re.search(r'src="([^"]+\.mp4)"', embed)
        if match:
            return match.group(1)

    return None


# ---------------------------------------------------------
#                BLUESKY POSTING
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


def post_to_bluesky_video(tumblr_url, video_url):
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)

    video_bytes = requests.get(video_url).content
    blob = client.com.atproto.repo.upload_blob(video_bytes)

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
#                MAIN
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

    # Duplicate check (Bluesky + local)
    if bluesky_has_posted_url(tumblr_link):
        print("Bluesky already posted this link. Exiting.")
        save_state(post_id, tumblr_link)
        return

    if post_id == last_post_id or normalize_url(tumblr_link) == normalize_url(last_post_url):
        print("No new posts. Exiting.")
        return

    # Extract media
    video = extract_video(post)
    images = extract_images(post)

    if video:
        print("Detected VIDEO post. Uploading to Bluesky...")
        try:
            post_to_bluesky_video(tumblr_link, video)
            print("✔ Video posted. Updating state.")
            save_state(post_id, tumblr_link)
        except Exception as e:
            print("❌ Bluesky video error:", e)
        return

    if images:
        print(f"Posting {len(images)} images to Bluesky…")
        try:
            post_to_bluesky_images(tumblr_link, images)
            print("✔ Images posted. Updating state.")
            save_state(post_id, tumblr_link)
        except Exception as e:
            print("❌ Bluesky image error:", e)
        return

    print("No images or video found. Saving state only.")
    save_state(post_id, tumblr_link)


if __name__ == "__main__":
    main()
