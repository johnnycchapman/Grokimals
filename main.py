#!/usr/bin/env python3
"""
Daily animal-portrait bot.

Every morning this script:
  1. Builds a random prompt of the form
     "Create a portrait of a/an {animal} {activity} {landscape} in a {style}"
  2. Sends it to xAI's image generation endpoint (Grok Imagine).
  3. Asks Grok (chat completions) to name the animal in the portrait.
  4. Encodes the result as a JPEG that fits X's 5 MB media limit.
  5. Uploads the image to X and posts it as "M/D/YYYY {name}", using
     today's date in America/New_York.
  6. Appends "Name,URL,Date" to log.csv — only after the post succeeds.

Triggered by cron-job.org, which POSTs to the GitHub Actions workflow_dispatch
endpoint at 08:00 America/New_York.

Required environment variables:
    XAI_API_KEY              xAI API key
    X_CONSUMER_KEY           X app consumer key        (OAuth 1.0a user context)
    X_CONSUMER_SECRET        X app consumer secret
    X_ACCESS_TOKEN           X access token for the posting account
    X_ACCESS_TOKEN_SECRET    X access token secret

Optional environment variables:
    XAI_IMAGE_MODEL          default "grok-imagine-image-2.0"
    XAI_IMAGE_QUALITY        "low", "medium" or "auto"; unset lets xAI decide
    XAI_ASPECT_RATIO         e.g. "1:1"; unset lets the model pick per prompt
    XAI_TEXT_MODEL           default "grok-4.6"; used to name the animal
    XAI_REASONING_EFFORT     "low"/"high"; unset lets the model decide
    X_HANDLE                 account handle used to build post URLs, default "grokimals"
    LOG_FILE                 CSV log of posts, default "./log.csv"
    OUTPUT_DIR               where to save the generated file, default "./output"
    DRY_RUN                  "1" to generate + save but skip posting to X
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

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

# Used only when Grok can't be reached for a name — the post still goes out.
FALLBACK_NAMES = [
    "Waffles", "Biscuit", "Pickles", "Juniper", "Marlowe", "Nugget",
    "Clementine", "Rufus", "Olive", "Barnaby", "Poppy", "Sable",
]

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

XAI_IMAGE_URL = "https://api.x.ai/v1/images/generations"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"

# grok-2-image-1212 was retired on 2026-02-28. Requests naming it return 400.
XAI_MODEL = os.getenv("XAI_IMAGE_MODEL", "grok-imagine-image-2.0")

# "quality" is only accepted by grok-imagine-image-2.0; omitted means "auto",
# which currently serves "low" for generation.
XAI_QUALITY = os.getenv("XAI_IMAGE_QUALITY")

# Omitted means "auto" — the model picks the ratio that suits the prompt.
XAI_ASPECT_RATIO = os.getenv("XAI_ASPECT_RATIO")

# Text model that names the animal.
XAI_TEXT_MODEL = os.getenv("XAI_TEXT_MODEL", "grok-4.6")
XAI_REASONING_EFFORT = os.getenv("XAI_REASONING_EFFORT")

X_MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
X_MEDIA_METADATA_URL = "https://api.x.com/2/media/metadata"
X_TWEETS_URL = "https://api.x.com/2/tweets"

X_HANDLE = os.getenv("X_HANDLE", "grokimals").lstrip("@")
LOG_FILE = Path(os.getenv("LOG_FILE", "log.csv"))
LOG_HEADER = ["Name", "URL", "Date"]

# The date in the post is the bot's local date, not the runner's UTC date.
LOCAL_TZ = ZoneInfo("America/New_York")

TARGET_LONG_EDGE = 2048          # matches the "2k" resolution xAI returns
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


@dataclass(frozen=True)
class Portrait:
    """One random portrait idea: the prompt plus the parts that built it."""
    text: str
    animal: str
    activity: str
    landscape: str
    style: str

    def __str__(self) -> str:  # so log.info("%s", portrait) still reads well
        return self.text


def build_prompt(rng: random.Random | None = None) -> Portrait:
    """Assemble one random prompt from the four word banks."""
    r = rng or random
    animal = r.choice(ANIMALS)
    activity = r.choice(ACTIVITIES)
    landscape = r.choice(LANDSCAPES)
    style = r.choice(STYLES)

    article = indefinite_article(animal)
    text = (
        f"Create a portrait of {article} {animal.lower()} {activity.lower()} "
        f"{landscape} in a {style.lower()} style"
    )
    return Portrait(text=text, animal=animal, activity=activity,
                    landscape=landscape, style=style)


def format_post_text(name: str, now: datetime | None = None) -> str:
    """Build the post text: "M/D/YYYY Name", no leading zeros, Eastern date."""
    today = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    return f"{today.month}/{today.day}/{today.year} {name}"


def append_log(path: Path, *, name: str, post_id: str, when: datetime) -> None:
    """Add one row to the CSV log, writing the header if the file is new."""
    url = f"https://x.com/{X_HANDLE}/status/{post_id}"
    date = f"{when.month}-{when.day}-{when.year}"
    is_new = not path.exists() or path.stat().st_size == 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(LOG_HEADER)
        writer.writerow([name, url, date])
    log.info("Logged to %s: %s, %s, %s", path, name, url, date)


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

            # raise_for_status() only carries the status line, so surface the
            # response body — that is where the API names the offending field.
            resp = getattr(exc, "response", None)
            if resp is not None and resp.text:
                log.warning("%s response body: %s", label, resp.text[:500])

            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("%s failed (attempt %d/%d): %s — retrying in %.0fs",
                        label, attempt, attempts, exc, delay)
            time.sleep(delay)
    raise RuntimeError(f"{label} failed after {attempts} attempts") from last_exc


# --------------------------------------------------------------------------
# Step 1 — image generation (xAI / Grok Imagine)
# --------------------------------------------------------------------------

def generate_image(prompt: str, api_key: str) -> bytes:
    """Ask Grok Imagine for a 2K image and return the raw bytes."""
    payload: dict[str, Any] = {
        "model": XAI_MODEL,
        "prompt": prompt,
        "n": 1,
        "resolution": "2k",
        "response_format": "b64_json",
    }
    if XAI_QUALITY:
        payload["quality"] = XAI_QUALITY
    if XAI_ASPECT_RATIO:
        payload["aspect_ratio"] = XAI_ASPECT_RATIO

    log.info("Requesting image from %s at 2k", XAI_MODEL)

    def _call() -> requests.Response:
        resp = requests.post(
            XAI_IMAGE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    body: dict[str, Any] = with_retries(_call, label="xAI image generation").json()

    # The response reports the model that actually served the request, which
    # resolves any aliases or deprecation redirects.
    if served := body.get("model"):
        log.info("Served by model: %s", served)

    items = body.get("data") or []
    if not items:
        raise RuntimeError(f"xAI returned no image data: {body}")

    item = items[0]
    if revised := item.get("revised_prompt"):
        log.info("Revised prompt from xAI: %s", revised)

    if b64 := item.get("b64_json"):
        return base64.b64decode(b64)
    if url := item.get("url"):
        img = with_retries(lambda: _get_ok(url), label="image download")
        return img.content

    raise RuntimeError(f"Unrecognized xAI response shape: {item}")


def _get_ok(url: str) -> requests.Response:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------
# Step 2 — name the animal (xAI / Grok chat completions)
# --------------------------------------------------------------------------

_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z'\-. ]{0,30}$")

NAME_SYSTEM_PROMPT = (
    "You name animals in illustrated portraits. Reply with the name only: "
    "no punctuation, no quotes, no explanation, no emoji. One or two words, "
    "at most 24 characters. Make it characterful and fitting — a name a "
    "reader would smile at — and avoid the most obvious pet-name cliches."
)


def name_animal(portrait: Portrait, api_key: str) -> str:
    """Ask Grok for a name for this animal. Falls back rather than failing."""
    payload: dict[str, Any] = {
        "model": XAI_TEXT_MODEL,
        "messages": [
            {"role": "system", "content": NAME_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Name this animal: {indefinite_article(portrait.animal)} "
                f"{portrait.animal.lower()} {portrait.activity.lower()} "
                f"{portrait.landscape}, drawn in a {portrait.style.lower()} style."
            )},
        ],
        # Reasoning models spend part of the completion budget on thinking,
        # so leave headroom even though the answer itself is a word or two.
        "max_tokens": 256,
        "temperature": 1.0,
    }
    if XAI_REASONING_EFFORT:
        payload["reasoning_effort"] = XAI_REASONING_EFFORT

    def _call() -> requests.Response:
        resp = requests.post(
            XAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    try:
        body = with_retries(_call, label="xAI naming", attempts=2).json()
        raw = body["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        name = random.choice(FALLBACK_NAMES)
        log.warning("Naming failed (%s) — falling back to %s", exc, name)
        return name

    name = clean_name(raw)
    if not name:
        name = random.choice(FALLBACK_NAMES)
        log.warning("Grok returned an unusable name %r — falling back to %s",
                    raw.strip()[:60], name)
        return name

    log.info("Grok named the %s: %s", portrait.animal.lower(), name)
    return name


def clean_name(raw: str) -> str:
    """Strip the model's stray punctuation and validate the shape."""
    # Take the last non-empty line: some models prefix a throat-clear.
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    candidate = lines[-1]
    # Drop a label prefix like "Name:" or "The name is:".
    if ":" in candidate:
        candidate = candidate.rsplit(":", 1)[1]
    # Drop leading decoration (emoji, bullets, quotes) and trailing punctuation.
    candidate = re.sub(r"^[^A-Za-z]+", "", candidate)
    candidate = candidate.strip().strip("\"'`*.,!?:;()[]{}").strip()
    candidate = re.sub(r"\s+", " ", candidate)
    return candidate if _NAME_OK.match(candidate) else ""


# --------------------------------------------------------------------------
# Step 3 — encode for X
# --------------------------------------------------------------------------

def encode_for_x(raw: bytes) -> bytes:
    """
    Encode as a JPEG small enough for X's 5 MB media limit.

    xAI now returns 2K directly via the "resolution" parameter, so the resize
    below is a safety net for a model that ignores it rather than the main
    event. The quality ladder is what actually keeps the file under the cap.
    """
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = TARGET_LONG_EDGE / max(w, h)

        if scale != 1:
            new_size = (round(w * scale), round(h * scale))
            log.info("Resampling %dx%d -> %dx%d", w, h, *new_size)
            im = im.resize(new_size, Image.LANCZOS)
        else:
            log.info("Image arrived at %dx%d — no resampling needed", w, h)

        for quality in (95, 90, 85, 78, 70):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, subsampling=0, optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_UPLOAD_BYTES:
                log.info("Encoded JPEG q=%d, %.2f MB", quality, len(data) / 1e6)
                return data

    raise RuntimeError("Could not compress image under X's 5 MB media limit")


# --------------------------------------------------------------------------
# Step 4 — upload + post to X
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

    now = datetime.now(LOCAL_TZ)

    portrait = build_prompt()
    log.info("Prompt: %s", portrait.text)

    try:
        raw = generate_image(portrait.text, api_key)
        image = encode_for_x(raw)
    except Exception as exc:
        log.error("Image generation failed: %s", exc)
        return 1

    name = name_animal(portrait, api_key)
    post_text = format_post_text(name, now)
    log.info("Post text: %s", post_text)

    path = out_dir / f"portrait-{now:%Y-%m-%d}.jpg"
    path.write_bytes(image)
    log.info("Saved %s", path)

    if dry_run:
        log.info("DRY_RUN=1 — skipping the post to X")
        return 0

    try:
        auth = x_auth()
        media_id = upload_media(image, auth, path.name)
        set_alt_text(media_id, portrait.text, auth)
        post_id = post_tweet(post_text, media_id, auth)
    except Exception as exc:
        log.error("Posting to X failed: %s", exc)
        return 1

    try:
        append_log(LOG_FILE, name=name, post_id=post_id, when=now)
    except OSError as exc:
        # The post is already live; don't fail the run over the log.
        log.error("Posted but could not write %s: %s", LOG_FILE, exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
