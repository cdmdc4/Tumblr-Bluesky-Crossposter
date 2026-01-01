import os
import json
import re
import requests
import subprocess
import tempfile
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
    url = (
        f"https://api.tumblr.com/v2/blog/{TUMBLR_BLOG}/posts"
        f"?api_key={TUMBLR_API_KEY}&notes_info=false&reblog_info=false&limit=30"
    )
    resp = requests.get(url).json()
    try:
        posts = resp["response"]["posts"]
    except:
        return []

    seen = set()
    clean = []
    for p in posts:
        pid = str(p.get("id"))
        if pid not in seen:
            clean.append(p)
            seen.add(pid)

    return clean


# ---------------------------------------------------------
#                MEDIA EXTRACTION
# ---------------------------------------------------------

def extract_images(post):
    urls = []

    for block in post.get("content", []):
        if block.get("type") == "image":
            for media in block.get("media", []):
                if "url" in media:
                    urls.append(media["url"])

    for item in post.get("trail", []):
        html = item.get("content_raw") or ""
        urls += re.findall(r'<img[^>]+src="([^"]+)"', html)

    body = post.get("body", "")
    urls += re.findall(r'<img[^>]+src="([^"]+)"', body)

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
    for block in post.get("content", []):
        if block.get("type") == "video":
            for media in block.get("media", []):
                u = media.get("url", "")
                if u.endswith(".mp4"):
                    return u

    if post.get("video_url", "").endswith(".mp4"):
        return post["video_url"]

    for t in post.get("trail", []):
        m = re.search(r'src="([^"]+\.mp4)"', t.get("content_raw", ""))
        if m:
            return m.group(1)

    for embed in post.get("player", []):
        m = re.search(r'src="([^"]+\.mp4)"', embed.get("embed_code", ""))
        if m:
            return m.group(1)

    return None


# ---------------------------------------------------------
#                ALT TEXT + CAPTION HELPERS
# ---------------------------------------------------------

def make_alt_text(post):
    tags = post.get("tags", [])
    if not tags:
        return ""
    return " ".join(tags)


def make_post_text(tumblr_url, post):
    caption = post.get("caption", "").strip()
    if caption:
        return f"({tumblr_url}) {caption}"
    else:
        return f"({tumblr_url})"


# ---------------------------------------------------------
#         GIF → MP4 Conversion (FFmpeg)
# ---------------------------------------------------------

def convert_gif_to_mp4(gif_bytes):
    """
    Convert GIF → MP4 using FFmpeg.
    Returns mp4_bytes or None if failed or too large.
    """

    with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as gif_file:
        gif_file.write(gif_bytes)
        gif_path = gif_file.name

    mp4_path = gif_path.replace(".gif", ".mp4")

    # ffmpeg command:
    # - loop once
    # - H.264 baseline
    # - 720p max height
    cmd = [
        "ffmpeg",
        "-y",
        "-i", gif_path,
        "-movflags", "faststart",
        "-vf", "scale=-1:720:force_original_aspect_ratio=decrease",
        "-pix_fmt", "yuv420p",
        "-vcodec", "libx264",
        "-preset", "veryfast",
        "-an",
        mp4_path,
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        print("FFmpeg conversion failed:", e)
        return None

    # Read MP4 back
    try:
        with open(mp4_path, "rb") as f:
            mp4_data = f.read()
    except:
        return None

    # Bluesky limit
    if len(mp4_data) > 900_000:
        print("MP4 still too large after compression.")
        return None

    return mp4_data


# ---------------------------------------------------------
#                BLUESKY UPLOADS WITH ALT TEXT
# ---------------------------------------------------------

def post_to_bluesky_video(client, post_text, video_url, alt_text):
    print("Downloading video…")
    data = requests.get(video_url).content

    print("Uploading blob…")
    blob = client.com.atproto.repo.upload_blob(data)

    video_embed = {
        "$type": "app.bsky.embed.video",
        "video": blob.blob,
        "alt": alt_text
    }

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": post_text,
            "embed": video_embed,
            "createdAt": client.get_current_time_iso(),
        }
    )


def post_to_bluesky_images(client, post_text, image_urls, alt_text):
    uploaded = []
    for url in image_urls:
        data = requests.get(url).content
        blob = client.com.atproto.repo.upload_blob(data)
        uploaded.append({"image": blob.blob, "alt": alt_text})

    embed = {"$type": "app.bsky.embed.images", "images": uploaded}

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": post_text,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        },
    )


def post_to_bluesky_gif(client, post_text, gif_url, alt_text):
    print("Downloading GIF…")
    gif_data = requests.get(gif_url).content

    if len(gif_data) > 900_000:
        print("GIF too large — converting to MP4…")
        mp4_data = convert_gif_to_mp4(gif_data)
        if mp4_data is None:
            print("❌ GIF could not be converted — skipping.")
            return None

        print("Uploading MP4…")
        blob = client.com.atproto.repo.upload_blob(mp4_data)

        video_embed = {
            "$type": "app.bsky.embed.video",
            "video": blob.blob,
            "alt": alt_text
        }

        return client.app.bsky.feed.post.create(
            repo=client.me.did,
            record={
                "$type": "app.bsky.feed.post",
                "text": post_text,
                "embed": video_embed,
                "createdAt": client.get_current_time_iso(),
            }
        )

    # If GIF is small enough (<900KB), upload as image
    blob = client.com.atproto.repo.upload_blob(gif_data)

    embed = {
        "$type": "app.bsky.embed.images",
        "images": [{"image": blob.blob, "alt": alt_text}],
    }

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": post_text,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        },
    )


# ---------------------------------------------------------
#        FETCH RECENT BLUESKY POSTS TO PREVENT DUPES
# ---------------------------------------------------------

def get_recent_bsky_tumblr_ids(client):
    """
    Fetch recent Bluesky posts and extract any Tumblr post IDs mentioned
    in them, regardless of formatting, slug, HTML, or URL variations.
    Returns a set of Tumblr post IDs (strings).
    """

    feed = client.app.bsky.feed.get_author_feed(
        params={"actor": client.me.did, "limit": 50}
    )

    tumblr_ids = set()

    # Matches:
    # - https://tumblr.com/post/123456789
    # - https://blog.tumblr.com/post/123456789/slug-here
    # - (https://myblog.tumblr.com/post/123456789/)
    # - raw: 123456789
    TUMBLR_ID_REGEX = re.compile(r"(?:tumblr\.com/.*/post/|tumblr\.com/post/|/post/)(\d+)")

    for item in feed.feed:
        post = getattr(item, "post", None)
        if not post:
            continue

        record = getattr(post, "record", None)
        if not record:
            continue

        # record.text is plain text (Bluesky strips HTML)
        text = getattr(record, "text", "")
        if not isinstance(text, str):
            continue

        # extract ALL tumblr IDs from the text
        for match in TUMBLR_ID_REGEX.findall(text):
            tumblr_ids.add(match)

        # as fallback: raw IDs (standalone digits)
        # helps if Bluesky strips URL completely
        for match in re.findall(r"\b(\d{9,20})\b", text):
            tumblr_ids.add(match)

    return tumblr_ids



# ---------------------------------------------------------
#                MAIN LOGIC
# ---------------------------------------------------------

def main():
    print("Running Tumblr → Bluesky crossposter…")

    client = get_bsky_client()

    bsky_ids = get_recent_bsky_tumblr_ids(client)
    print("Found", len(bsky_ids), "existing Tumblr IDs on Bluesky.")

    state = load_state()
    posted_ids = state["posted_ids"]

    posts = get_recent_tumblr_posts()
    if not posts:
        print("❌ No Tumblr posts found.")
        return

    posts = sorted(posts, key=lambda p: p.get("timestamp", 0))
    posts = posts[:30]

    for post in posts:
        post_id = str(post.get("id"))
        tumblr_link = post.get("post_url", "").strip()
        post_text = make_post_text(tumblr_link, post)
        alt_text = make_alt_text(post)

        print("\n--- Checking Tumblr post:", post_id)

        if post_id in posted_ids or post_id in bsky_ids:
            print("Already posted — skipping.")
            continue

        video = extract_video(post)
        gif = extract_gif(post)
        images = extract_images(post)

        if video:
            print("Posting VIDEO…")
            try:
                post_to_bluesky_video(client, post_text, video, alt_text)
                print("✔ Video posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ Video error:", e)
            continue

        if gif:
            print("Processing GIF…")
            try:
                result = post_to_bluesky_gif(client, post_text, gif, alt_text)
                if result:
                    print("✔ GIF posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ GIF error:", e)
            continue

        if images:
            print(f"Posting {len(images)} IMAGES…")
            try:
                post_to_bluesky_images(client, post_text, images, alt_text)
                print("✔ Images posted.")
                posted_ids.append(post_id)
                save_state(state)
            except Exception as e:
                print("❌ Image error:", e)
            continue

        print("Nothing postable — skipping.")
        posted_ids.append(post_id)
        save_state(state)

    print("\nDone!")


if __name__ == "__main__":
    main()

