import os
import json
import re
import requests
import subprocess
import tempfile
from io import BytesIO
from PIL import Image
from atproto import Client

# ---------------------------------------------------------
#                CONFIGURATION
# ---------------------------------------------------------

TUMBLR_API_KEY = os.getenv("TUMBLR_API_KEY")
TUMBLR_BLOG = os.getenv("TUMBLR_BLOG_IDENTIFIER")

BSKY_USERNAME = os.getenv("BSKY_USERNAME")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

STATE_FILE = "tumblr_state.json"

MAX_BSKY_BLOB = 976_000      # hard Bluesky limit
TARGET_MAX = 950_000         # safety margin


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
    gif_path = None
    mp4_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as gif_file:
            gif_file.write(gif_bytes)
            gif_path = gif_file.name

        mp4_path = gif_path.replace(".gif", ".mp4")

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

        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        with open(mp4_path, "rb") as f:
            mp4_data = f.read()

        if len(mp4_data) > 900_000:
            print("MP4 still too large after compression.")
            return None

        return mp4_data

    except Exception as e:
        print("FFmpeg conversion failed:", e)
        return None

    finally:
        # Always clean up temp files
        try:
            if gif_path and os.path.exists(gif_path):
                os.remove(gif_path)
        except:
            pass

        try:
            if mp4_path and os.path.exists(mp4_path):
                os.remove(mp4_path)
        except:
            pass



# ---------------------------------------------------------
#    IMAGE COMPRESSION ENGINE (JPEG + DOWNSCALE)
# ---------------------------------------------------------

def compress_and_resize(image_bytes):
    if len(image_bytes) <= MAX_BSKY_BLOB:
        return image_bytes

    print(f"Image too large ({len(image_bytes)/1024:.1f} KB) → compressing + resizing…")

    try:
        img = Image.open(BytesIO(image_bytes))
    except Exception as e:
        print("❌ Cannot open image:", e)
        return None

    if img.mode in ("RGBA", "LA"):
        img = img.convert("RGB")

    width, height = img.size

    scale_factors = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]
    qualities = [95, 90, 85, 80, 75, 70]

    for scale in scale_factors:
        w = max(1, int(width * scale))
        h = max(1, int(height * scale))
        resized = img.resize((w, h), Image.LANCZOS)

        for q in qualities:
            buf = BytesIO()
            try:
                resized.save(buf, format="JPEG", quality=q)
            except Exception:
                continue

            data = buf.getvalue()
            print(f" → {w}x{h} q{q}: {len(data)/1024:.1f} KB")

            if len(data) <= TARGET_MAX:
                print(" ✓ Compression successful.")
                return data

    print("❌ Could not compress image under limit.")
    return None


# ---------------------------------------------------------
#        FIXED: BLUESKY UPLOADS WITH SIZE CHECK
# ---------------------------------------------------------

def upload_with_compression(client, raw):
    """
    ALWAYS check size before upload.
    If > MAX_BSKY_BLOB → compress.
    If Bluesky still returns BlobTooLarge → compress again.
    """

    # Pre-check
    if len(raw) > MAX_BSKY_BLOB:
        print(f"Raw image too large ({len(raw)/1024:.1f} KB) → compressing…")
        raw = compress_and_resize(raw)
        if raw is None:
            return None

    # Try uploading
    resp = client.com.atproto.repo.upload_blob(raw)

    # Bluesky returns Response(success=False…, not an exception)
    if getattr(resp, "success", True) is False:
        err = getattr(resp, "content", "")
        if hasattr(err, "error") and err.error == "BlobTooLarge":
            print("BlobTooLarge → compressing again…")
            raw2 = compress_and_resize(raw)
            if raw2 is None:
                return None
            return client.com.atproto.repo.upload_blob(raw2)

    return resp


# ---------------------------------------------------------
#                BLUESKY POSTING HELPERS
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
        raw = requests.get(url).content

        blob = upload_with_compression(client, raw)
        if blob is None:
            print("❌ Image too large even after compression — skipping.")
            continue

        uploaded.append({"image": blob.blob, "alt": alt_text})

    if not uploaded:
        print("❌ No images could be uploaded.")
        return None

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
        print("GIF too large → converting to MP4…")
        mp4_data = convert_gif_to_mp4(gif_data)
        if mp4_data is None:
            print("❌ Could not convert GIF — skipping.")
            return None

        blob = upload_with_compression(client, mp4_data)
        if blob is None:
            print("❌ MP4 upload failed.")
            return None

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

    blob = upload_with_compression(client, gif_data)
    if blob is None:
        print("❌ GIF upload failed.")
        return None

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
    feed = client.app.bsky.feed.get_author_feed(
        params={"actor": client.me.did, "limit": 50}
    )

    tumblr_ids = set()
    TUMBLR_ID_REGEX = re.compile(r"(?:tumblr\.com/.*/post/|tumblr\.com/post/|/post/)(\d+)")

    for item in feed.feed:
        post = getattr(item, "post", None)
        if not post:
            continue

        record = getattr(post, "record", None)
        if not record:
            continue

        text = getattr(record, "text", "")
        if not isinstance(text, str):
            continue

        for match in TUMBLR_ID_REGEX.findall(text):
            tumblr_ids.add(match)

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
                res = post_to_bluesky_images(client, post_text, images, alt_text)
                if res:
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

