"""
Furaha House Watamu - Instagram auto-poster (vision version).

Picks a photo you haven't used recently, LOOKS at it, writes a caption from
what it sees, and posts it to Instagram.

You do not have to describe your photos. Just put them in the photos/ folder.

Run with --dry-run to see what it WOULD post without actually posting.
"""

import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

# --- Config -----------------------------------------------------------------

# Where the caption comes from: "perplexity" or "anthropic".
# Both run Claude. "perplexity" uses your Perplexity key via their Agent API.
CAPTION_PROVIDER = os.environ.get("CAPTION_PROVIDER", "perplexity")
CAPTION_MODEL_PERPLEXITY = "anthropic/claude-haiku-4-5"
CAPTION_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

ROOT = Path(__file__).parent
PHOTOS_DIR = ROOT / "photos"
CONFIG_FILE = ROOT / "photos.json"
STATE_FILE = ROOT / "state" / "posted.json"

IMAGE_TYPES = {".jpg", ".jpeg"}
MAX_EDGE = 1400          # shrink before sending, to keep requests small

DRY_RUN = "--dry-run" in sys.argv


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        sys.exit(f"ERROR: Missing environment variable {name}. Check your GitHub Secrets.")
    return value


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- Step 1: choose a photo -------------------------------------------------


def find_photos():
    """Every JPEG in photos/ is fair game. No config file needed."""
    if not PHOTOS_DIR.exists():
        sys.exit("ERROR: There is no photos/ folder. Create it and upload some JPEGs.")

    files = sorted(
        p.name for p in PHOTOS_DIR.iterdir()
        if p.suffix.lower() in IMAGE_TYPES and not p.name.startswith(".")
    )
    if not files:
        sys.exit("ERROR: No .jpg files found in photos/. "
                 "Note that .png and .heic are not accepted by Instagram.")
    return files


def choose_photo(files, history):
    """Prefer photos never posted; otherwise re-use the ones posted longest ago."""
    never_posted = [f for f in files if f not in history]

    if never_posted:
        print(f"{len(never_posted)} of {len(files)} photos have never been posted.")
        return random.choice(never_posted)

    oldest_first = sorted(files, key=lambda f: history.get(f, 0))
    pool = oldest_first[: max(1, len(oldest_first) // 2)]
    print("All photos have been used before. Re-using the oldest half.")
    return random.choice(pool)


# --- Step 2: look at it and write ------------------------------------------

CAPTION_SYSTEM_PROMPT = """You write Instagram captions for Furaha House, a private \
holiday villa in Watamu on the Kenyan coast.

You are being shown a photograph. Write about it the way a novelist would: the \
quality of the light, the texture of things, the temperature the air looks, what \
hour it feels like, the silence or the sound the scene implies, what a person might \
be about to do here. Be evocative and specific. Invent mood freely.

THE ONE HARD RULE: invent atmosphere, never facts.

A guest will read this, book, arrive, and stand in this exact place. Anything they \
could arrive and find untrue is forbidden. Never state or imply:
- distances, walking times, or travel times to anywhere
- amenities you cannot see (air conditioning, wifi, heating, hot water, staff)
- the number of bedrooms, bathrooms, guests, or the size of anything
- prices, availability, discounts, or offers
- a sea view, a sunset direction, or what is beyond the frame
- meals, cooking, service, or people who work there
- history, dates, place names, or facts about Watamu or Kenya

Before each sentence ask: could a guest arrive and say "that was untrue"? If yes, \
cut it. Write only about what is actually visible in this photograph, and about \
how it feels.

Style:
- 2 to 4 short sentences. Present tense.
- Do not open with "Nestled", "Escape to", "Discover", "Welcome to", or "Step into".
- No emoji, or at most one.
- No hashtags. They are added separately.
- No citation markers, reference numbers, source links, or square brackets.
- Write in English.

First decide what this photograph mainly shows. Choose exactly one label:
  villa    - the house, its rooms, terrace, garden, pool, interiors, details
  beach    - sand, sea, coastline, boats, the ocean
  nature   - inland landscape, forest, rock, river, wildlife, birds
  culture  - ruins, markets, towns, buildings, craft, food, people at work

Then write the caption.

Reply in exactly this format and nothing else:
SUBJECT: <one label>
CAPTION: <your caption>"""

USER_MESSAGE = ("Here is the photograph. Write the caption, following your rules "
                "exactly. Describe only what you can actually see.")


def prepare_image(filename):
    """Shrink the photo and return (base64_string, media_type)."""
    path = PHOTOS_DIR / filename
    raw = path.read_bytes()

    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        if max(img.size) > MAX_EDGE:
            img.thumbnail((MAX_EDGE, MAX_EDGE))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        raw = buffer.getvalue()
    except ImportError:
        print("  (Pillow not installed - sending the photo at full size)")

    print(f"  sending {len(raw) // 1024} KB to the caption model")
    return base64.standard_b64encode(raw).decode("utf-8"), "image/jpeg"


def split_reply(text):
    """Pull the subject label and caption out of the model's reply."""
    subject, caption = "villa", text

    match = re.search(r"SUBJECT:\s*(\w+)", text, re.IGNORECASE)
    if match:
        found = match.group(1).lower()
        if found in ("villa", "beach", "nature", "culture"):
            subject = found

    match = re.search(r"CAPTION:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if match:
        caption = match.group(1)

    return subject, caption


def clean_caption(text):
    """Strip debris that proxied models sometimes leave behind."""
    text = re.sub(r"\[\w+:\d+\]", "", text)              # [web:1] style markers
    text = re.sub(r"\[\d+\]", "", text)                  # [1] style markers
    text = re.sub(r"<[^>]+>", "", text)                  # stray tags
    text = text.strip()
    text = re.sub(r'^["\u201c\u2018\']+|["\u201d\u2019\']+$', "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text.strip()


def call_anthropic(image_b64, media_type, api_key):
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CAPTION_MODEL_ANTHROPIC,
            "max_tokens": 400,
            "system": CAPTION_SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": USER_MESSAGE},
                ],
            }],
        },
        timeout=120,
    )
    if response.status_code != 200:
        sys.exit(f"ERROR from Anthropic ({response.status_code}): {response.text}")
    return response.json()["content"][0]["text"]


def call_perplexity(image_b64, media_type, api_key):
    """Perplexity's Agent API proxies Claude. OpenAI Responses format."""
    response = requests.post(
        "https://api.perplexity.ai/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": CAPTION_MODEL_PERPLEXITY,
            "instructions": CAPTION_SYSTEM_PROMPT,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": USER_MESSAGE},
                    {
                        "type": "input_image",
                        "image_url": f"data:{media_type};base64,{image_b64}",
                    },
                ],
            }],
            "max_output_tokens": 400,
        },
        timeout=120,
    )

    if response.status_code != 200:
        sys.exit(
            f"ERROR from Perplexity ({response.status_code}): {response.text}\n\n"
            "If this mentions images, image input, or an unsupported content type, "
            "then Perplexity's proxy will not pass photos through to Claude. "
            "In that case switch CAPTION_PROVIDER to 'anthropic' and use a direct "
            "Anthropic key."
        )

    data = response.json()

    # Responses format: dig the assistant's text out of the output blocks.
    if data.get("output_text"):
        return data["output_text"]

    chunks = []
    for block in data.get("output", []):
        if block.get("type") == "message":
            for part in block.get("content", []):
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    chunks.append(part["text"])
    if not chunks:
        sys.exit(f"ERROR: Could not find caption text in the reply: {data}")
    return "\n".join(chunks)


def write_caption(filename, api_key):
    if DRY_RUN and not api_key:
        return "villa", "[dry run - no API key set, caption not generated]"

    image_b64, media_type = prepare_image(filename)

    if CAPTION_PROVIDER == "perplexity":
        raw = call_perplexity(image_b64, media_type, api_key)
    elif CAPTION_PROVIDER == "anthropic":
        raw = call_anthropic(image_b64, media_type, api_key)
    else:
        sys.exit(f"ERROR: CAPTION_PROVIDER is '{CAPTION_PROVIDER}'. "
                 "It must be 'perplexity' or 'anthropic'.")

    subject, caption = split_reply(raw)
    print(f"  subject detected: {subject}")
    return subject, clean_caption(caption)


def add_call_to_action(caption, subject, config):
    """Append one of YOUR booking lines. Never written by the AI."""
    cta = config.get("call_to_action", {})
    options = list(cta.get(subject, [])) + list(cta.get("any", []))
    if not options:
        return caption
    return f"{caption}\n\n{random.choice(options)}"


def add_hashtags(caption, subject, config):
    tags = list(config.get("always_hashtags", []))
    tags += config.get("subject_hashtags", {}).get(subject, [])

    seen, unique = set(), []
    for tag in tags:
        tag = tag if tag.startswith("#") else f"#{tag}"
        if tag.lower() not in seen:
            seen.add(tag.lower())
            unique.append(tag)

    if not unique:
        return caption
    return f"{caption}\n\n{' '.join(unique[:20])}"


# --- Step 3: publish --------------------------------------------------------


def build_image_url(filename):
    repo = env("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{quote('photos/' + filename)}"


def publish(image_url, caption, ig_user_id, token):
    print("Creating media container...")
    create = requests.post(
        f"{GRAPH}/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": token},
        timeout=60,
    )
    if create.status_code != 200:
        sys.exit(f"ERROR creating container ({create.status_code}): {create.text}")

    container_id = create.json()["id"]

    for _ in range(12):
        time.sleep(5)
        status = requests.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        ).json()
        code = status.get("status_code")
        print(f"  container status: {code}")
        if code == "FINISHED":
            break
        if code == "ERROR":
            sys.exit(f"ERROR: Instagram rejected the image. Details: {status.get('status')}")
    else:
        sys.exit("ERROR: Container never finished processing. Try again later.")

    print("Publishing...")
    published = requests.post(
        f"{GRAPH}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=60,
    )
    if published.status_code != 200:
        sys.exit(f"ERROR publishing ({published.status_code}): {published.text}")
    return published.json()["id"]


# --- Main -------------------------------------------------------------------


def main():
    config = load_json(CONFIG_FILE, {})
    history = load_json(STATE_FILE, {})

    filename = choose_photo(find_photos(), history)
    print(f"\nChosen photo: {filename}")

    key_name = "PERPLEXITY_API_KEY" if CAPTION_PROVIDER == "perplexity" else "ANTHROPIC_API_KEY"
    api_key = env(key_name, required=not DRY_RUN)

    subject, caption = write_caption(filename, api_key)
    caption = add_call_to_action(caption, subject, config)
    full_caption = add_hashtags(caption, subject, config)

    print("\n" + "=" * 60)
    print(full_caption)
    print("=" * 60 + "\n")

    if DRY_RUN:
        print("DRY RUN - nothing was posted to Instagram.")
        return

    image_url = build_image_url(filename)
    print(f"Image URL: {image_url}")

    check = requests.head(image_url, timeout=30, allow_redirects=True)
    if check.status_code != 200:
        sys.exit(f"ERROR: Image URL is not publicly reachable ({check.status_code}). "
                 "Is the repository public?")

    post_id = publish(image_url, full_caption, env("IG_USER_ID"), env("IG_ACCESS_TOKEN"))
    print(f"\nSUCCESS. Posted as {post_id}")

    history[filename] = int(time.time())
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, sort_keys=True)
    print("Updated state/posted.json")


if __name__ == "__main__":
    main()
