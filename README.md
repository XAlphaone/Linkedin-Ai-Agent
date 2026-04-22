# linkedin-agent

A local Python desktop agent that watches your git activity, drafts LinkedIn
post candidates (with on-topic images) via the xAI Grok API, and serves them
through a localhost dashboard where you review and publish.

Posting uses LinkedIn's **official OAuth 2.0 + REST API** — no browser
automation, no scraping. You authorize your own account once via the Settings
page, then each generated post gets a **Publish to LinkedIn** button in the
dashboard. Nothing is published without your explicit click. If you'd rather
run in manual mode, leave `LINKEDIN_CLIENT_ID` blank in `.env` and the
dashboard falls back to a **Copy** / **Mark Posted** flow where you paste the
draft into LinkedIn yourself.

---

## What it does

- Polls configured local and GitHub repos on an interval, recording commits and
  merged PRs as events.
- Runs a daily cron (or on-demand **Generate Now** button) that drafts 3 post
  candidates per generation — one each for the `technical_peer`,
  `decision_maker`, and `mixed_story` angles — via Grok.
- For each variant, runs a **two-pass image pipeline**:
  1. The reasoning model reads the full post and writes a concrete,
     photographable image brief (subject, lens, lighting, specific props).
  2. `grok-imagine-image-pro` renders that brief at 2k resolution, 2:1 ratio.
  3. The image is saved to `data/images/post_<id>.png` and shown in the
     dashboard card.
- Dashboard at `http://127.0.0.1:8765` — edit the post inline, copy, mark
  posted (auto-rejects siblings), reject, or regenerate just the image.
- Engagement numbers (impressions / likes / comments / reshares / profile
  visits / follower delta) are entered manually after you post.
- `/stats` shows Bayesian-smoothed per-angle performance so you can see which
  framing is working.

---

## Setup

### 1. Clone and install

```bash
git clone git@github.com:XAlphaone/Linkedin-Ai-Agent.git
cd Linkedin-Ai-Agent
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. API keys

Copy the example env file and fill in real values:

```bash
cp .env.example .env
```

Then edit `.env`:

```bash
XAI_API_KEY=xai-...                 # from https://console.x.ai
GITHUB_TOKEN=github_pat_...         # optional for public repos, required for private
```

**xAI key:** sign up at [console.x.ai](https://console.x.ai), create an API
key, and top up credits. Cost estimate below.

**GitHub token:** only needed if you're watching private repos. If you generate
a fine-grained PAT at
[github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta),
scope it to the repos you'll watch and grant:

- `Contents: Read` — needed to fetch commits.
- `Pull requests: Read` — needed to fetch merged PRs.

(A classic PAT with the `repo` scope also works.)

### 3. Config

Copy the example config and edit it:

```bash
cp config.yaml.example config.yaml
```

In `config.yaml`:
- Set `author.name`, `author.headline`, and `author.positioning` so posts
  sound like you, not a chatbot.
- Replace the two placeholder repos in the `repos:` list with real ones —
  `type: local` for a path on disk, `type: github` for a URL.

### 4. Run

```bash
python run.py
```

Open `http://127.0.0.1:8765`. Click **Poll Now** to ingest recent commits,
then **Generate Now** to draft 3 variants.

---

## Costs

At 3 posts + 3 images per day using default models:

| Line item                                                         | Monthly |
| ----------------------------------------------------------------- | -------:|
| `grok-4.20-0309-reasoning` — ~3 post-text calls/day               |  ~$1-2  |
| Image-brief call (same reasoning model, smaller output)           |  ~$1    |
| `grok-imagine-image-pro` — 3 images/day @ $0.07                   |  ~$6.30 |
| **Total**                                                         | **~$8-10** |

To cut this roughly in half: switch `generation.image_model` to
`grok-imagine-image` (`$0.02` instead of `$0.07`) and/or drop
`image_resolution` from `"2k"` to `"1k"`. Set `generate_images: false` to
disable images entirely and run text-only for ~$1-3/month.

---

## Dashboard

| Page       | What it does                                                                  |
|------------|-------------------------------------------------------------------------------|
| `/`        | Queue — pending variant groups. Per card: image, hook, editable body, Copy, Mark Posted, Reject, Regen image. |
| `/history` | Last 100 posted + rejected posts with their images; inline form to enter engagement. |
| `/repos`   | List of watched repos with `last_sha` / `last_checked_at`; add / enable / disable. |
| `/stats`   | Per-angle samples, avg score, smoothed mean, normalized weight, SVG bar.      |

Two toolbar buttons on the queue page:

- **Poll Now** — walks every enabled repo and inserts new events immediately.
- **Generate Now** — pulls up to 20 unprocessed events, drafts 3 variants,
  generates 3 images, marks those events processed.

---

## Scheduler

APScheduler runs two jobs:

- `poll_repos_job` — every `generation.poll_interval_hours` (default 2h).
- `generate_variants_job` — on `generation.daily_generate_cron` (default
  `0 7 * * *` UTC). Always generates — if there are no new events, produces
  a reflection-style post instead.

---

## Scoring

```
score = likes + 3*comments + 5*reshares + 0.1*impressions
```

Weight per angle is a Bayesian-smoothed mean with prior mean `50.0` and prior
weight `5.0`, normalized across the three angles. It's display-only in v1 —
all three angles still generate every day. The signal is for you.

---

## Voice guide

Baked into the text-generation system prompt and enforced with a
regenerate-on-match pass:

- First person. Direct. No throat-clearing.
- Short sentences mixed with long.
- Concrete specifics over abstractions.
- At most one emoji; at most 2-3 hashtags — usually zero of either.
- No "what do you think?" / "Thoughts?" closers.
- Banned phrases: *"In today's rapidly evolving landscape"*, *"I'm excited to
  announce"*, *"Thrilled to share"*, *"Game changer"*, *"Let's dive in"*,
  *"At the end of the day"*, sentences starting *"Remember:"* or *"The truth
  is:"*.

If a draft hits one of those, the generator retries once with an explicit
correction, then logs a warning and keeps the draft (you review every post
before it goes out).

---

## Image pipeline notes

The image prompt is **not** the post. Sending the post straight at an image
model produces abstract filler — we tried it. Instead:

1. The reasoning model reads the full post + hook and writes a photo brief
   (specific subject, lens, lighting, props). The brief-writing system prompt
   bans clichés, text-in-image, and stock-photo tropes.
2. The brief goes to `grok-imagine-image-pro` with `aspect_ratio: "2:1"` and
   `resolution: "2k"`, returned as `b64_json` and saved to disk.

The result is a scene that actually depicts what the post is about.

---

## Deploy as a background service

- **macOS:** `deploy/launchd.plist.example` — edit paths, copy to
  `~/Library/LaunchAgents/`, `launchctl load`.
- **Linux:** `deploy/linkedin-agent.service.example` — user systemd unit,
  `systemctl --user enable --now linkedin-agent.service`.
- **Windows:** run `python run.py` in a terminal, or wrap with NSSM / a
  Scheduled Task at login.

---

## Layout

```
linkedin-agent/
├── README.md
├── requirements.txt
├── .env.example
├── config.yaml.example
├── .gitignore
├── run.py                              # entry point: starts scheduler + uvicorn
├── agent/
│   ├── config.py                       # pydantic Config loaded from YAML + env
│   ├── db.py                           # SQLite schema + helpers
│   ├── scheduler.py                    # APScheduler jobs
│   ├── learner.py                      # Bayesian-smoothed angle scoring
│   ├── watchers/
│   │   ├── git_local.py                # GitPython walker
│   │   └── github_api.py               # GitHub REST: commits + merged PRs
│   ├── generator/
│   │   ├── prompts.py                  # system prompt, angle specs, voice guide
│   │   ├── grok.py                     # xAI Chat Completions for post text
│   │   └── grok_images.py              # two-pass image pipeline
│   └── web/
│       ├── app.py                      # FastAPI routes
│       ├── templates/
│       │   ├── base.html
│       │   ├── queue.html
│       │   ├── history.html
│       │   ├── repos.html
│       │   └── stats.html
│       └── static/style.css
├── data/                               # runtime state (gitignored)
│   ├── agent.db                        # SQLite
│   └── images/                         # generated PNGs
└── deploy/
    ├── launchd.plist.example
    └── linkedin-agent.service.example
```

---

## Not in scope

- LinkedIn API / browser automation / scraping — hard no.
- Auth on the dashboard (localhost only, single user).
- Multi-user, accounts, roles.
- Email / notification channel (wire ntfy or Pushover later if you want).
- Sentiment analysis, reply triage.
- Lead generation, cold email.

---

## License

MIT — see `LICENSE` if included, otherwise consider this repo MIT-licensed.
