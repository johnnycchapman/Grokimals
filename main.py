#!/usr/bin/env python3
"""
Daily animal-portrait bot.

Every morning this script:
  1. Builds a random prompt of the form
     "Create a portrait of a/an {animal} {activity} {landscape} in a {style}"
  2. Sends it to xAI's image generation endpoint (Grok).
  3. Upscales the result to 2K (2048 px on the long edge).
  4. Uploads the image to X and posts it.

Required environment variables:
    XAI_API_KEY              xAI API key
    X_CONSUMER_KEY           X app consumer key        (OAuth 1.0a user context)
    X_CONSUMER_SECRET        X app consumer secret
    X_ACCESS_TOKEN           X access token for the posting account
    X_ACCESS_TOKEN_SECRET    X access token secret

Optional environment variables:
    XAI_IMAGE_MODEL          default "grok-2-image-1212"
    OUTPUT_DIR               where to save the generated file, default "./output"
    DRY_RUN                  "1" to generate + save but skip posting to X
"""

from __future__ import annotations

import base64
import io
import logging
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

import requests
from PIL import Image
from requests_oauthlib import OAuth1

# --------------------------------------------------------------------------
# Word banks
# --------------------------------------------------------------------------

ANIMALS = [
    "Dog", "Cat", "Horse", "Cow", "Pig", "Sheep", "Goat", "Chicken", "Duck", "Rabbit",
    "Lion", "Tiger", "Elephant", "Bear", "Wolf", "Fox", "Deer", "Squirrel", "Monkey", "Giraffe",
    "Zebra", "Kangaroo", "Penguin", "Dolphin", "Whale", "Shark", "Eagle", "Owl", "Frog", "Turtle",
]

ACTIVITIES = [
    "Surfing", "Skateboarding", "Swimming", "Cycling", "Climbing", "Skiing", "Snowboarding",
    "Skydiving", "Kayaking", "Fishing", "Painter", "Dancing", "Singing", "Cooking",
    "Gardening", "Reading", "Juggling", "Bowling", "Golfing", "Archery",
]

STYLES = [
    "Cubism", "Futurism", "Cyberpunk", "Steampunk", "Surrealism",
    "Pop Art", "Street Art", "Digital", "Post Impressionism", "Pixel Art",
]

LANDSCAPES = [
    "in the forest", "on a mountain", "in outer space", "on the beach", "in the desert",
    "underwater", "in a jungle", "on a glacier", "in a canyon", "in a swamp",
    "on a volcano", "in a meadow", "on a frozen lake", "in a cave", "on a cliffside",
    "in a bamboo grove", "on the open ocean", "in a wheat field", "on the moon",
    "in a redwood grove",
]

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

XAI_IMAGE_URL = "https://api.x.ai/v1/images/generations"
XAI_MODEL = os.getenv("XAI_IMAGE_MODEL", "grok-2-image-1212")

X_MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
X_MEDIA_METADATA_URL = "https://api.x.com/2/media/metadata"
X_TWEETS_URL = "https://api.x.com/2/tweets"

TARGET_LONG_EDGE = 2048          # "2K" — 2048 px on the longer side
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # X image limit is 5 MB
REQUEST_TIMEOUT = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("portrait-bot")

T = TypeVar("T")


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

# Words that start with a vowel letter but a consonant SOUND ("a unicorn").
_CONSONANT_SOUND_PREFIXES = ("uni", "use", "eu", "ewe", "ubiq", "one")
# Words that start with a consonant letter but a vowel SOUND ("an hour").
_VOWEL_SOUND_PREFIXES = ("hour", "honest", "heir", "honor")


def indefinite_article(word: str) -> str:
    """Return "a" or "an" based on the *sound* the word starts with."""
    w = word.strip().lower()
    if w.startswith(_VOWEL_SOUND_PREFIXES):
        return "an"
    if w.startswith(_CONSONANT_SOUND_PREFIXES):
        return "a"
    return "an" if w[:1] in "aeiou" else "a"


def build_prompt(rng: random.Random | None = None) -> str:
    """Assemble one random prompt from the four word banks."""
    r = rng or random
    animal = r.choice(ANIMALS)
    activity = r.choice(ACTIVITIES)
    landscape = r.choice(LANDSCAPES)
    style = r.choice(STYLES)

    article = indefinite_article(animal)
    return (
        f"Create a portrait of {article} {animal.lower()} {activity.lower()} "
        f"{landscape} in a {style.lower()} style"
    )


# --------------------------------------------------------------------------
# Small retry helper
# --------------------------------------------------------------------------

def with_retries(fn: Callable[[], T], *, label: str, attempts: int = 3,
                 base_delay: float = 5.0) -> T:
    """Run `fn`, retrying with exponential backoff on network/HTTP errors."""
    import time

    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("%s failed (attempt %d/%d): %s — retrying in %.0fs",
                        label, attempt, attempts, exc, delay)
            time.sleep(delay)
    raise RuntimeError(f"{label} failed after {attempts} attempts") from last_exc


# --------------------------------------------------------------------------
# Step 1 — image generation (xAI / Grok)
# --------------------------------------------------------------------------

def generate_image(prompt: str, api_key: str) -> bytes:
    """Ask Grok for an image and return the raw bytes."""

    def _call() -> requests.Response:
        resp = requests.post(
            XAI_IMAGE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": XAI_MODEL,
                "prompt": prompt,
                "n": 1,
                "response_format": "b64_json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    payload: dict[str, Any] = with_retries(_call, label="xAI image generation").json()

    items = payload.get("data") or []
    if not items:
        raise RuntimeError(f"xAI returned no image data: {payload}")

    item = items[0]
    if revised := item.get("revised_prompt"):
        log.info("Revised prompt from xAI: %s", revised)

    if b64 := item.get("b64_json"):
        return base64.b64decode(b64)
    if url := item.get("url"):
        img = with_retries(
            lambda: _get_ok(url), label="image download"
        )
        return img.content

    raise RuntimeError(f"Unrecognized xAI response shape: {item}")


def _get_ok(url: str) -> requests.Response:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------
# Step 2 — upscale to 2K
# --------------------------------------------------------------------------

def upscale_to_2k(raw: bytes) -> bytes:
    """
    Resize so the long edge is TARGET_LONG_EDGE, preserving aspect ratio, and
    encode as JPEG small enough for X's 5 MB media limit.

    The xAI image endpoint does not expose a size parameter, so the delivered
    image is resampled here rather than requested at 2K directly.
    """
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = TARGET_LONG_EDGE / max(w, h)

        if scale > 1:
            new_size = (round(w * scale), round(h * scale))
            log.info("Upscaling %dx%d -> %dx%d", w, h, *new_size)
            im = im.resize(new_size, Image.LANCZOS)
        elif scale < 1:
            new_size = (round(w * scale), round(h * scale))
            log.info("Downscaling %dx%d -> %dx%d", w, h, *new_size)
            im = im.resize(new_size, Image.LANCZOS)
        else:
            log.info("Image already %dx%d — no resampling needed", w, h)

        for quality in (95, 90, 85, 78, 70):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, subsampling=0, optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_UPLOAD_BYTES:
                log.info("Encoded JPEG q=%d, %.2f MB", quality, len(data) / 1e6)
                return data

    raise RuntimeError("Could not compress image under X's 5 MB media limit")


# --------------------------------------------------------------------------
# Step 3 — upload + post to X
# --------------------------------------------------------------------------

def x_auth() -> OAuth1:
    """Build the OAuth 1.0a user-context signer X requires for posting."""
    required = (
        "X_CONSUMER_KEY", "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing X credentials: {', '.join(missing)}")

    return OAuth1(
        os.environ["X_CONSUMER_KEY"],
        os.environ["X_CONSUMER_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
    )


def upload_media(image: bytes, auth: OAuth1, filename: str) -> str:
    """Upload the JPEG to X and return its media id."""

    def _call() -> requests.Response:
        resp = requests.post(
            X_MEDIA_UPLOAD_URL,
            auth=auth,
            files={"media": (filename, image, "image/jpeg")},
            data={"media_category": "tweet_image"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    body = with_retries(_call, label="X media upload").json()
    data = body.get("data", body)
    media_id = data.get("id") or data.get("media_id_string")
    if not media_id:
        raise RuntimeError(f"No media id in upload response: {body}")

    log.info("Uploaded media id %s", media_id)
    return str(media_id)


def set_alt_text(media_id: str, alt_text: str, auth: OAuth1) -> None:
    """Attach alt text for accessibility. Non-fatal if it fails."""
    try:
        resp = requests.post(
            X_MEDIA_METADATA_URL,
            auth=auth,
            json={"id": media_id, "metadata": {"alt_text": {"text": alt_text[:1000]}}},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("Alt text attached")
    except requests.RequestException as exc:
        log.warning("Could not attach alt text: %s", exc)


def post_tweet(text: str, media_id: str, auth: OAuth1) -> str:
    """Publish the post and return its id."""

    def _call() -> requests.Response:
        resp = requests.post(
            X_TWEETS_URL,
            auth=auth,
            json={"text": text[:280], "media": {"media_ids": [media_id]}},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    body = with_retries(_call, label="X post").json()
    post_id = body.get("data", {}).get("id", "")
    log.info("Posted: https://x.com/i/web/status/%s", post_id)
    return post_id


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        log.error("XAI_API_KEY is not set")
        return 1

    dry_run = os.getenv("DRY_RUN") == "1"
    out_dir = Path(os.getenv("OUTPUT_DIR", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt()
    log.info("Prompt: %s", prompt)

    try:
        raw = generate_image(prompt, api_key)
        image = upscale_to_2k(raw)
    except Exception as exc:
        log.error("Image generation failed: %s", exc)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out_dir / f"portrait-{stamp}.jpg"
    path.write_bytes(image)
    log.info("Saved %s", path)

    if dry_run:
        log.info("DRY_RUN=1 — skipping the post to X")
        return 0

    try:
        auth = x_auth()
        media_id = upload_media(image, auth, path.name)
        set_alt_text(media_id, prompt, auth)
        post_tweet(prompt, media_id, auth)
    except Exception as exc:
        log.error("Posting to X failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
