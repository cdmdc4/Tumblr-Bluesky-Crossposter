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
    """Load last_post_id + last_post_url, safe defaults."""
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
    """Write new state after successful posting."""
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
#                BLUESKY CHECKING
# ---------------------------------------------------------

def get_latest_bluesky_post_url():
    """Returns the text of the latest Bluesky post (or None)."""
    client = Client()
    client.login(BSKY_USERNAME, BSKY_PASSWORD)

    # Correct modern API call
    feed = client.app.bsky.feed.get_author_feed(
        params={
            "actor": client.me.did,
            "limit": 1
        }
    )

    items = feed.feed
    if not items:
        return None

    record = items[0].post.record
    if not isinstance(record, dict):
        return None

    return record.get("text", "").strip()


# ---------------------------------------------------------
#                IMAGE EXTRACTION
# ---------------------------------------------------------

def extract_all_images(post):
    urls = []

    # Trail HTML
    for item in post.get("trail", []):
        html = item.get("content_raw") or item.get("content") or ""
        urls += re.findall(r'<img[^>]+src="([^"]+)"', html)

    # Body HTML
    body = post.get("body", "")
    urls += re.findall(r'<img[^>]+src="([^"]+)"', body)

    # Photo posts
    if post.get("type") == "photo" and "photos" in post:
        for p in post["photos"]:
            try:
                urls.append(p["original_size"]["url"])
            except:
                pass

    # Remove duplicates but preserve order
    clean = []
    for u in urls:
        if u not in clean:
            clean.append(u)

    return clean[:4]  # Bluesky max = 4 images


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
#                MAIN (ONE-TIME RUN)
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

    # -------------------------------------------------
    # CHECK BLUESKY LATEST POST TO PREVENT DUPLICATES
    # -------------------------------------------------
    latest_bsky_text = get_latest_bluesky_post_url()
    print("Last Bluesky post text:", latest_bsky_text)

    if latest_bsky_text and tumblr_link in latest_bsky_text:
        print("Bluesky already has this Tumblr link. Exiting.")
        save_state(post_id, tumblr_link)
        return

    # Normal duplicate check (local state)
    if post_id == last_post_id or tumblr_link == last_post_url:
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
