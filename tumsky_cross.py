import os
import json
import re
import requests
import subprocess
import tempfile
from atproto import Client

# ---------------------------------------------------------
#                CONFIGURATION  (DO NOT REMOVE)
# ---------------------------------------------------------

TUMBLR_API_KEY = os.getenv("TUMBLR_API_KEY")
TUMBLR_BLOG = os.getenv("TUMBLR_BLOG_IDENTIFIER")

BSKY_USERNAME = os.getenv("BSKY_USERNAME")
BSKY_PASSWORD = os.getenv("BSKY_PASSWORD")

STATE_FILE = "tumblr_state.json"

# ---------------------------------------------------------
#                BLUESKY CLIENT
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
        f"?api_key={TUMBLR_API_KEY}&notes_info=false&reblog_info=true&limit=30"
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
#                CAPTION LOGIC (C1 SMART FALLBACK)
# ---------------------------------------------------------

def extract_reblog_caption(post):
    """
    If this post is a reblog:
        Use YOUR added caption (if any)
        else use OP's caption
    """
    trail = post.get("trail", [])
    if not trail:
        return None

    # Your added reblog text (top-level)
    top = trail[0]
    my_text = top.get("content_raw", "") or ""
    my_text = strip_html(my_text).strip()

    if my_text:
        return my_text  # You added text

    # OP caption fallback
    for t in trail:
        if t.get("is_root_item"):
            op_html = t.get("content_raw", "") or ""
            op = strip_html(op_html).strip()
            if op:
                return op

    return None


def strip_html(html):
    return re.sub(r"<[^>]*>", "", html or "")

def make_post_text(tumblr_url, post):
    """
    Caption rules:
    1) If reblog → use reblog caption (your text, then OP caption)
    2) Else if original → use post.caption
    """
    # Is reblog?
    if post.get("reblogged_from_id"):
        cap = extract_reblog_caption(post)
        if cap:
            return f"({tumblr_url}) {cap}"
        else:
            return f"({tumblr_url})"

    # Original post
    caption = post.get("caption", "").strip()
    if caption:
        caption = strip_html(caption)
        return f"({tumblr_url}) {caption}"

    return f"({tumblr_url})"

# ---------------------------------------------------------
#                MEDIA EXTRACTION (R2 LOGIC)
# ---------------------------------------------------------

def extract_images_from_block(blocks):
    urls = []
    for block in blocks:
        if block.get("type") == "image":
            for media in block.get("media", []):
                if "url" in media:
                    urls.append(media["url"])
    return urls

def extract_video_from_block(blocks):
    for block in blocks:
        if block.get("type") == "video":
            for media in block.get("media", []):
                if media.get("url", "").endswith(".mp4"):
                    return media["url"]
    return None

def extract_images(post):
    # Try primary post body first
    urls = extract_images_from_block(post.get("content", []))

    # OLD STYLE
    body = post.get("body", "")
    urls += re.findall(r'<img[^>]+src="([^"]+)"', body)

    # OLD "photo" array
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

def extract_video(post):
    # New Tumblr API blocks
    v = extract_video_from_block(post.get("content", []))
    if v:
        return v

    # OLD TUMBLR VIDEO FIELDS
    if post.get("video_url", "").endswith(".mp4"):
        return post["video_url"]

    # Trails
    for t in post.get("trail", []):
        m = re.search(r'src="([^"]+\.mp4)"', t.get("content_raw", ""))
        if m:
            return m.group(1)

    # Player
    for embed in post.get("player", []):
        m = re.search(r'src="([^"]+\.mp4)"', embed.get("embed_code", ""))
        if m:
            return m.group(1)

    return None

# ---------------------------------------------------------
#               R2: MEDIA FOR REBLOGS
# ---------------------------------------------------------

def resolve_media(post):
    """
    R2 Logic:
    1) If reblog has its OWN media → use that
    2) else → use OP's media
    """
    # Step 1: Try THIS post
    video = extract_video(post)
    images = extract_images(post)
    gif = next((u for u in images if u.lower().endswith(".gif")), None)

    if video or gif or images:
        return video, gif, images

    # Step 2: Fall back to OP (root trail)
    for t in post.get("trail", []):
        if t.get("is_root_item"):
            root_html = t.get("content_raw", "")

            # Extract images
            urls = re.findall(r'<img[^>]+src="([^"]+)"', root_html)
            clean = []
            for u in urls:
                if u not in clean:
                    clean.append(u)
            clean = clean[:4]

            root_gif = next((u for u in clean if u.lower().endswith(".gif")), None)

            # Extract mp4
            m = re.search(r'src="([^"]+\.mp4)"', root_html)
            root_video = m.group(1) if m else None

            return root_video, root_gif, clean

    return None, None, None

# ---------------------------------------------------------
#                ALT TEXT
# ---------------------------------------------------------

def make_alt_text(post):
    tags = post.get("tags", [])
    if not tags:
        return ""
    return " ".join(tags)

# ---------------------------------------------------------
#        GIF → MP4 Conversion (FFmpeg)
# ---------------------------------------------------------

def convert_gif_to_mp4(gif_bytes):
    with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
        f.write(gif_bytes)
        gif_path = f.name

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

    try:
        subprocess.run(cmd, check=True)
    except Exception as e:
        print("FFmpeg conversion failed:", e)
        return None

    try:
        with open(mp4_path, "rb") as f:
            data = f.read()
        if len(data) > 900_000:
            print("MP4 still too large after conversion.")
            return None
        return data
    except:
        return None

# ---------------------------------------------------------
#      FALLBACK: Shrink large images (ImageMagick)
# ---------------------------------------------------------

def shrink_image_if_needed(img_bytes):
    if len(img_bytes) <= 950_000:
        return img_bytes

    print("Image too large — shrinking…")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(img_bytes)
        in_path = f.name

    out_path = in_path + "_small.jpg"

    cmd = [
        "convert",
        in_path,
        "-resize", "1600x1600>",
        "-strip",
        "-quality", "85",
        out_path,
    ]

    try:
        subprocess.run(cmd, check=True)
        with open(out_path, "rb") as f:
            data = f.read()
        return data
    except:
        print("ImageMagick failed — using original")
        return img_bytes

# ---------------------------------------------------------
#                BLUESKY UPLOADS
# ---------------------------------------------------------

def post_to_bluesky_video(client, post_text, video_url, alt_text):
    print("Downloading video…")
    data = requests.get(video_url).content

    if len(data) > 900_000:
        print("Video too large — re-encoding…")

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(data)
            in_path = f.name

        out_path = in_path + "_small.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", in_path,
            "-movflags", "faststart",
            "-pix_fmt", "yuv420p",
            "-preset", "veryfast",
            "-vf", "scale=-1:720:force_original_aspect_ratio=decrease",
            "-an",
            out_path,
        ]

        try:
            subprocess.run(cmd, check=True)
            with open(out_path, "rb") as f:
                data = f.read()
        except:
            print("Re-encode failed — skipping video.")
            return None

    blob = client.com.atproto.repo.upload_blob(data)
    embed = {"$type": "app.bsky.embed.video", "video": blob.blob, "alt": alt_text}

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": post_text,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        }
    )

def post_to_bluesky_images(client, post_text, image_urls, alt_text):
    uploaded = []
    for url in image_urls:
        data = requests.get(url).content
        data = shrink_image_if_needed(data)

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
        }
    )

def post_to_bluesky_gif(client, post_text, gif_url, alt_text):
    print("Downloading GIF…")
    gif_data = requests.get(gif_url).content

    # Large GIF → convert to MP4
    if len(gif_data) > 900_000:
        print("GIF too large — converting to MP4…")
        mp4 = convert_gif_to_mp4(gif_data)
        if not mp4:
            print("❌ GIF conversion failed — skipping")
            return None

        blob = client.com.atproto.repo.upload_blob(mp4)
        embed = {"$type": "app.bsky.embed.video", "video": blob.blob, "alt": alt_text}

        return client.app.bsky.feed.post.create(
            repo=client.me.did,
            record={
                "$type": "app.bsky.feed.post",
                "text": post_text,
                "embed": embed,
                "createdAt": client.get_current_time_iso(),
            }
        )

    # Small GIF → treat as image
    blob = client.com.atproto.repo.upload_blob(gif_data)
    embed = {"$type": "app.bsky.embed.images",
             "images": [{"image": blob.blob, "alt": alt_text}]}

    return client.app.bsky.feed.post.create(
        repo=client.me.did,
        record={
            "$type": "app.bsky.feed.post",
            "text": post_text,
            "embed": embed,
            "createdAt": client.get_current_time_iso(),
        }
    )

# ---------------------------------------------------------
#        FETCH RECENT BLUESKY POSTS (for dedupe)
# ---------------------------------------------------------

def get_recent_bsky_tumblr_ids(client):
    feed = client.app.bsky.feed.get_author_feed(
        params={"actor": client.me.did, "limit": 50}
    )

    tumblr_ids = set()

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

        match = re.search(r"tumblr\.com/.+/(\d+)", text)
        if match:
            tumblr_ids.add(match.group(1))

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

    for post in posts[:30]:
        post_id = str(post.get("id"))
        tumblr_link = post.get("post_url", "").strip()
        post_text = make_post_text(tumblr_link, post)
        alt_text = make_alt_text(post)

        print("\n--- Checking Tumblr post:", post_id)

        if post_id in posted_ids or post_id in bsky_ids:
            print("Already posted — skipping.")
            continue

        # R2 media resolution
        video, gif, images = resolve_media(post)

        # Video
        if video:
            print("Posting VIDEO…")
            try:
                post_to_bluesky_video(client, post_text, video, alt_text)
                print("✔ Video posted.")
            except Exception as e:
                print("❌ Video error:", e)
            posted_ids.append(post_id)
            save_state(state)
            continue

        # GIF
        if gif:
            print("Posting GIF…")
            try:
                post_to_bluesky_gif(client, post_text, gif, alt_text)
                print("✔ GIF posted.")
            except Exception as e:
                print("❌ GIF error:", e)
            posted_ids.append(post_id)
            save_state(state)
            continue

        # Images
        if images:
            print(f"Posting {len(images)} IMAGES…")
            try:
                post_to_bluesky_images(client, post_text, images, alt_text)
                print("✔ Images posted.")
            except Exception as e:
                print("❌ Image error:", e)
            posted_ids.append(post_id)
            save_state(state)
            continue

        print("Nothing postable — skipping.")
        posted_ids.append(post_id)
        save_state(state)

    print("\nDone!")

if __name__ == "__main__":
    main()
