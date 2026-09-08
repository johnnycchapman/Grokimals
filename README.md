# 🎨 Daily Animal Portrait Bot 🦊

A small automation that wakes up every morning at **8:00 AM Eastern**, invents a
ridiculous animal portrait, asks Grok to paint it, and posts the result to X.

No two mornings are the same. A cubist dolphin golfing on a volcano. A steampunk
owl juggling in a bamboo grove. The bot rolls the dice and ships it. 🎲

---

## ✨ What it does

Every run walks through five steps:

1. 🎲 **Rolls a prompt** — picks one item at random from each of four word banks
   and assembles them into a sentence.
2. 🤖 **Generates the image** — sends that prompt to xAI's image endpoint (Grok).
3. 🖼️ **Upscales to 2K** — resamples the result to 2048 px on the long edge and
   encodes it as a JPEG small enough for X's 5 MB media limit.
4. 📤 **Uploads to X** — pushes the file through the X media endpoint and
   attaches the prompt as alt text for accessibility.
5. 🐦 **Posts it** — publishes the image with the prompt as the caption.

---

## 🧬 The prompt formula

```
Create a portrait of a/an {animal} {activity} {landscape} in a {style} style
```

Sample output:

> Create a portrait of an elephant skateboarding on a frozen lake in a pop art style
>
> Create a portrait of a fox kayaking in a redwood grove in a cyberpunk style
>
> Create a portrait of an owl gardening on the moon in a pixel art style

### 📚 The word banks

| Bank | Count | Examples |
|------|-------|----------|
| 🐾 Animals | 30 | Dog, Elephant, Penguin, Shark, Owl |
| 🏄 Activities | 20 | Surfing, Skydiving, Juggling, Archery |
| 🏔️ Landscapes | 20 | in the forest, on a volcano, underwater, on the moon |
| 🖌️ Styles | 10 | Cubism, Cyberpunk, Steampunk, Pixel Art |

That's **30 × 20 × 20 × 10 = 120,000** possible prompts — roughly 328 years
before a repeat is even likely. 🗓️

### 🔤 The "a" vs "an" detail

The bot picks the article by **sound**, not just by first letter. So it writes
*an elephant* and *an owl*, but would correctly write *a unicorn* and *an hour*
if those words ever joined the list. Exception lists live in
`indefinite_article()` if you need to extend them.

---

## 🚀 Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get your credentials 🔑

| Secret | Where to get it |
|--------|-----------------|
| `XAI_API_KEY` | [console.x.ai](https://console.x.ai) |
| `X_CONSUMER_KEY` | X Developer Portal → your app → Keys and tokens |
| `X_CONSUMER_SECRET` | same place |
| `X_ACCESS_TOKEN` | same place, with **Read and write** permissions |
| `X_ACCESS_TOKEN_SECRET` | same place |

⚠️ The access token must be generated **after** setting the app to Read and
write. If you flip the permission afterward, regenerate the token or posting
will fail with a 403.

### 3. Add them as GitHub Actions secrets

`Settings → Secrets and variables → Actions → New repository secret`

### 4. Drop the workflow in place

```bash
mkdir -p .github/workflows
mv daily-portrait.yml .github/workflows/
```

---

## 🧪 Testing before you go live

Set `DRY_RUN=1` to generate and save the image without posting anything:

```bash
export XAI_API_KEY="your-key-here"
DRY_RUN=1 python daily_portrait_bot.py
```

The image lands in `./output/portrait-YYYY-MM-DD.jpg`. From the Actions tab you
can also trigger **Run workflow** and tick the `dry_run` box.

---

## ⏰ Scheduling notes

GitHub Actions cron runs on **UTC and ignores daylight saving**, so the workflow
registers two triggers — 12:00 and 13:00 UTC — and a guard step checks the real
local hour. Exactly one gets through each day. ✅

Prefer a plain crontab on your own box? The system clock already handles DST:

```cron
0 8 * * * cd /path/to/bot && /path/to/venv/bin/python daily_portrait_bot.py >> bot.log 2>&1
```

☝️ Note that GitHub's scheduler is best-effort and can lag by several minutes
during busy periods. If 8:00 sharp matters, self-host the cron.

---

## ⚙️ Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `XAI_IMAGE_MODEL` | `grok-2-image-1212` | Swap in a newer image model |
| `OUTPUT_DIR` | `output` | Where generated files are saved |
| `DRY_RUN` | `0` | Set to `1` to skip posting |

---

## 📁 Files

```
daily_portrait_bot.py    # the bot
requirements.txt         # dependencies
daily-portrait.yml       # scheduled workflow → .github/workflows/
README.md                # you are here 👋
```

---

## 🛠️ Making it yours

- **Add words** — extend any of the four arrays at the top of the script. The
  combination count grows fast.
- **Change the formula** — edit `build_prompt()`. Anything goes.
- **Change the caption** — right now the prompt doubles as the post text. Swap
  the first argument to `post_tweet()` for something else, or add hashtags.
- **Post more often** — add cron entries and adjust the hour guard.

---

## 🩺 Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `XAI_API_KEY is not set` 🔑 | Secret missing or misspelled in Actions |
| `403` from X | Token generated before Read and write was enabled |
| `429` from either API | Rate limited — the free X tier caps daily posts |
| Workflow runs but skips 🚪 | Guard step working as intended on the off-hour trigger |
| Image too large ❌ | All JPEG quality steps exhausted; lower `TARGET_LONG_EDGE` |

Every run logs its prompt, image dimensions, final file size, and the resulting
post URL — start there when something looks off. 🔍
