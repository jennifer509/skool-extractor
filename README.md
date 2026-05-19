# Skool Course Extractor

Mac-local pipeline that turns Skool video lessons (paid courses you're enrolled in) into structured markdown notes — Summary + Key concepts + Action items + verbatim transcript per lesson. Mirrors to Notion if desired.

Built for personal study notes. Don't use on courses you don't have legitimate access to.

## How it works

1. Playwright opens Chrome with your logged-in Skool profile
2. You click lessons in the visible browser (manual mode = zero detection signal)
3. Script intercepts Mux signed-URL requests in the background
4. yt-dlp downloads the English `.vtt` subtitle track per lesson
5. The `claude` CLI (Max plan, $0 per call) rewrites the transcript into structured notes
6. Markdown lands in `~/skool-notes/{course-slug}/{NN-lesson-slug}.md`
7. Optional: mirror to Notion under a parent page

Output sits outside this repo so the tool stays self-contained and your notes can be synced anywhere you want.

## Setup (one-time)

```bash
# 1. Install yt-dlp (subtitle downloader)
brew install yt-dlp

# 2. Install Python deps
pip install -r requirements.txt

# 3. Install Playwright's Chromium build
playwright install chromium

# 4. Set up env
cp .env.example .env
# Edit .env: add NOTION_API_TOKEN (if using --notion-sync)
```

If using `--notion-sync`, share the parent Notion page with your integration:
Notion → page → ⋯ → Connections → add integration.

## Usage

```bash
# First dry run (just captures Mux URLs, no Claude, no Notion)
python extract.py \
  --course-url "https://www.skool.com/mrpaidsocial/classroom/<course-slug>" \
  --mode manual --limit 2 --dry-run

# Real run, 2 lessons, no Notion
python extract.py --course-url "<URL>" --mode manual --limit 2

# Full course with Notion mirror
python extract.py --course-url "<URL>" --mode manual --notion-sync
```

When the visible Chrome window opens, log into Skool if prompted (the session persists for future runs), navigate into the course, click lessons in order.

## Flags

| Flag | Purpose |
|---|---|
| `--course-url` | **Required.** Skool classroom URL for the course |
| `--course-title` | Override the derived title |
| `--mode {manual,auto}` | `manual` = you click; `auto` = script walks with random delays |
| `--notion-sync` | Mirror lessons to Notion |
| `--dry-run` | Capture Mux URLs only — no downloads, no Claude, no Notion |
| `--force` | Re-process lessons whose markdown already exists |
| `--limit N` | Stop after N lessons |
| `--notes-root` | Override output directory (default: `~/skool-notes/`) |
| `--headless` | Auto mode only — run Chrome headless (higher ban risk) |

## Output

```
~/skool-notes/
└── mc1-foundations-for-media-buying/
    ├── 01-intro.md            # frontmatter + Summary + Key concepts + Action items + Transcript
    ├── 01-intro.en.vtt        # raw VTT (kept for reprocessing)
    ├── 02-...md
    └── ...
```

## Sync notes back to main AIOS repo

After a course is extracted, sync to your main repo on VPS:

```bash
COURSE_SLUG=mc1-foundations-for-media-buying
rsync -av --exclude='*.vtt' ~/skool-notes/$COURSE_SLUG/ \
  vps:~/zeroarc-aios/reference/courses/mr-paid-social/$COURSE_SLUG/
```

(Replace `vps` with your actual SSH alias for the Hostinger VPS.)

Then on the VPS, the playbook updater can run against those notes — see the main repo's `scripts/skool_playbook_proposal.py`.

## Ban-risk posture

Low when used in manual mode for personal study. VTT-only (no video downloads), uses your real Chrome profile, you click manually. Worst case: temp account suspension, appealable. Don't run multiple courses back-to-back; pace at one per session.

## Troubleshooting

- **`yt-dlp: command not found`** → `brew install yt-dlp`
- **403 from Mux** → token expired (rare in normal use; just re-run)
- **`No Mux URL captured`** → video didn't load; click the play button in manual mode
- **Notion 404** → integration not shared with parent page
- **`claude CLI failed`** → confirm `which claude` returns a path; check Claude subscription
