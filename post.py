"""
Furaha House Watamu - Instagram auto-poster.

Picks a photo that hasn't been used recently, writes a caption from YOUR notes
about that photo, and publishes it to Instagram.

Run with --dry-run to see what it WOULD post without actually posting.
"""

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

# Which service writes the captions: "perplexity" or "anthropic".
# Change this one word to switch. Make sure the matching API key is in
# your GitHub Secrets (PERPLEXITY_API_KEY or ANTHROPIC_API_KEY).
CAPTION_PROVIDER = os.environ.get("CAPTION_PROVIDER", "perplexity")

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

ROOT = Path(__file__).parent
PHOTOS_FILE = ROOT / "photos.json"
STATE_FILE = ROOT / "state" / "posted.json"

DRY_RUN = "--dry-run" in sys.argv


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        sys.exit(f"ERROR: Missing environment variable {name}. Check your GitHub Secrets.")
    return value


# --- Step 1: choose a photo -------------------------------------------------


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def choose_photo(photos, history):
    """Pick a random photo, preferring ones posted longest ago (or never)."""
    if not photos:
        sys.exit("ERROR: photos.json is empty. Add some photos first.")

    never_posted = [p for p in photos if p["file"] not in history]

    if never_posted:
        pool = never_posted
        print(f"{len(never_posted)} photo(s) have never been posted. Choosing from those.")
    else:
        # Everything has been used. Re-use the half that was posted longest ago,
        # so the feed doesn't repeat the same shot two weeks apart.
        photos_by_age = sorted(photos, key=lambda p: history.get(p["file"], 0))
        pool = photos_by_age[: max(1, len(photos_by_age) // 2)]
        print("All photos have been posted before. Re-using the oldest ones.")

    return random.choice(pool)


# --- Step 2: write a caption ------------------------------------------------

CAPTION_SYSTEM_PROMPT = """You write Instagram captions for Furaha House, a private \
holiday villa in Watamu on the Kenyan coast.

ABSOLUTE RULE: Use ONLY the facts in the photo notes you are given. Do not search \
the web. Do not use anything you know about Watamu, Kenya, or holiday villas from \
any other source. If a detail is not in the notes, it does not exist. Never invent \
features, distances, prices, amenities, room counts, or history.

Style:
- 2 to 4 short sentences. Warm and specific, not brochure-speak.
- No emoji spam. One or two at most, or none.
- Do not start with "Nestled", "Escape to", "Discover" or "Welcome to".
- No hashtags. They are added separately.
- No citations, no reference numbers, no source links, no square brackets.
- Write in English.

Output the caption text only. No preamble, no quotation marks, no explanation."""


def clean_caption(text):
    """Strip the debris that search-grounded models leave behind."""
    text = re.sub(r"\[\d+\]", "", text)                  # citation markers like [1][2]
    text = re.sub(r"<[^>]+>", "", text)                  # stray tags
    text = re.sub(r"^[\"\u2018\u2019\u201c\u201d']+|[\"\u201c\u201d]+$", "", text.strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)         # " ," -> ","
    return text.strip()


def call_perplexity(system_prompt, user_message, api_key):
    response = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "sonar",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 300,
            "temperature": 0.9,
            # Keep the web search as small as possible - we do not want it.
            "web_search_options": {"search_context_size": "low"},
        },
        timeout=90,
    )
    if response.status_code != 200:
        sys.exit(f"ERROR from Perplexity ({response.status_code}): {response.text}")
    return response.json()["choices"][0]["message"]["content"]


def call_anthropic(system_prompt, user_message, api_key):
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        },
        timeout=60,
    )
    if response.status_code != 200:
        sys.exit(f"ERROR from Anthropic ({response.status_code}): {response.text}")
    return response.json()["content"][0]["text"]


def write_caption(photo, api_key):
    notes = photo.get("notes", "").strip()
    if not notes:
        sys.exit(f"ERROR: Photo '{photo['file']}' has no notes in photos.json. "
                 "Add notes so the caption is accurate.")

    user_message = f"""Photo notes: {notes}

Category: {photo.get('category', 'the villa')}

Write the caption using only the notes above."""

    if DRY_RUN and not api_key:
        return "[dry run - no API key set, caption not generated]"

    if CAPTION_PROVIDER == "perplexity":
        raw = call_perplexity(CAPTION_SYSTEM_PROMPT, user_message, api_key)
    elif CAPTION_PROVIDER == "anthropic":
        raw = call_anthropic(CAPTION_SYSTEM_PROMPT, user_message, api_key)
    else:
        sys.exit(f"ERROR: CAPTION_PROVIDER is '{CAPTION_PROVIDER}'. "
                 "It must be 'perplexity' or 'anthropic'.")

    return clean_caption(raw)


def add_hashtags(caption, photo, config):
    """Combine the always-on hashtags with any specific to this photo."""
    tags = list(config.get("hashtags", []))
    tags += photo.get("hashtags", [])

    seen = set()
    unique = []
    for tag in tags:
        tag = tag if tag.startswith("#") else f"#{tag}"
        if tag.lower() not in seen:
            seen.add(tag.lower())
            unique.append(tag)

    # Instagram allows 30 max; 10-15 performs better than 30 anyway.
    unique = unique[:20]
    return f"{caption}\n\n{' '.join(unique)}"


# --- Step 3: publish to Instagram -------------------------------------------


def build_image_url(photo):
    """Instagram downloads the image from a public URL, so we point it at GitHub."""
    repo = env("GITHUB_REPOSITORY")          # e.g. "yourname/furaha-bot"
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    safe_path = quote(f"photos/{photo['file']}")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{safe_path}"


def publish(image_url, caption, ig_user_id, token):
    # 3a. Create a "container" - Instagram fetches and stages the image.
    print("Creating media container...")
    create = requests.post(
        f"{GRAPH}/{ig_user_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": token},
        timeout=60,
    )
    if create.status_code != 200:
        sys.exit(f"ERROR creating container ({create.status_code}): {create.text}")

    container_id = create.json()["id"]
    print(f"Container created: {container_id}")

    # 3b. Wait until Instagram has finished downloading the image.
    for attempt in range(12):
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

    # 3c. Publish it.
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
    config = load_json(PHOTOS_FILE, {})
    photos = config.get("photos", [])
    history = load_json(STATE_FILE, {})

    photo = choose_photo(photos, history)
    print(f"\nChosen photo: {photo['file']}")

    key_name = "PERPLEXITY_API_KEY" if CAPTION_PROVIDER == "perplexity" else "ANTHROPIC_API_KEY"
    api_key = env(key_name, required=not DRY_RUN)
    caption = write_caption(photo, api_key)
    full_caption = add_hashtags(caption, photo, config)

    print("\n" + "=" * 60)
    print(full_caption)
    print("=" * 60 + "\n")

    if DRY_RUN:
        print("DRY RUN - nothing was posted to Instagram.")
        if os.environ.get("GITHUB_REPOSITORY"):
            print(f"Image URL would be: {build_image_url(photo)}")
        return

    image_url = build_image_url(photo)
    print(f"Image URL: {image_url}")

    # Confirm the image is actually reachable before asking Instagram to fetch it.
    check = requests.head(image_url, timeout=30, allow_redirects=True)
    if check.status_code != 200:
        sys.exit(f"ERROR: Image URL is not publicly reachable ({check.status_code}). "
                 "Is the repository public? Is the filename spelled correctly?")

    post_id = publish(
        image_url, full_caption, env("IG_USER_ID"), env("IG_ACCESS_TOKEN")
    )
    print(f"\nSUCCESS. Posted as {post_id}")

    # Record it so we don't repeat this photo next time.
    history[photo["file"]] = int(time.time())
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, sort_keys=True)
    print("Updated state/posted.json")


if __name__ == "__main__":
    main()
