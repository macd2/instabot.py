# InstaBot: scheduler and insights for your own Instagram account

This repository started life in 2017 as an engagement bot that drove Instagram's
undocumented web endpoints to mass-like, follow and comment. Instagram blocks that
kind of automation, the endpoints it used no longer exist, and the code in `src/`
has not worked for years. It is kept only for reference (see [Legacy bot](#legacy-bot)).

What the project does now lives in `igtool/`: a small command line tool that uses the
**official Instagram Graph API** with a token for **your own professional (business or
creator) account** to

- **schedule and publish your posts** (images, reels, stories, carousels) from a queue, and
- **build insight reports** (account totals, daily reach and follower series, per-post metrics)
  as Markdown, JSON and CSV.

It does not log in with a password, does not touch other people's content and does not
like, follow or comment on anyone. The official API does not allow those actions, and
this tool does not try to work around that.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Commands](#commands)
- [Scheduling posts](#scheduling-posts)
- [Reports](#reports)
- [Configuration reference](#configuration-reference)
- [Running it continuously](#running-it-continuously)
- [Tests](#tests)
- [Legacy bot](#legacy-bot)

---

## How it works

```
 you                      igtool                              Meta Graph API
 ───                      ──────                              ──────────────
 schedule add ──────────▶ SQLite queue (igtool.db)
                              │
 schedule run ──(tick)──▶ pick posts whose time has come
                              │
                              ├─ POST /{ig-user}/media ───────▶ create media container
                              ├─ GET  /{container}?status ────▶ poll until FINISHED
                              └─ POST /{ig-user}/media_publish ▶ post goes live
                                   │
                                   └─ queue row -> published (media id stored)

 report ────────────────▶ GET /{ig-user}            account summary
                          GET /{ig-user}/insights   daily series + period totals
                          GET /{ig-user}/media      recent posts
                          GET /{media}/insights     per-post metrics
                              │
                              └─ reports/report_<since>_<until>.{md,json,_daily.csv,_media.csv}
```

The package is five small modules:

| Module | Role |
| --- | --- |
| `igtool/config.py` | Reads `igtool.ini` and `IGTOOL_*` environment variables into a `Config`. Holds the metric lists so they can be changed without touching code. |
| `igtool/client.py` | Thin Graph API client: adds the token, parses Graph error payloads, retries on rate limits and 5xx with exponential backoff, follows paging, redacts tokens from error text. Also resolves which account a token belongs to and refreshes long-lived tokens. |
| `igtool/publish.py` | The two-step publishing flow: create a container, poll its status, publish it. Knows the parameter sets for images, reels, stories and carousels and reads the publishing quota. |
| `igtool/scheduler.py` | A SQLite table of scheduled posts plus a loop that publishes whatever is due, retrying failures and keeping the error message on the row. |
| `igtool/report.py` | Pulls account and media insights, splits long ranges into the 30-day windows the API accepts, tolerates metrics the API refuses, and renders Markdown, JSON and CSV. |

`igtool/cli.py` wires these into the `python -m igtool` command.

Two flavours of the API exist and both are supported. Pick one with `api_host`:

| `api_host` | Name Meta uses | When to use it |
| --- | --- | --- |
| `graph.facebook.com` | Instagram API with Facebook Login | Your Instagram account is linked to a Facebook Page. Account id comes from the Page. |
| `graph.instagram.com` | Instagram API with Instagram Login | No Facebook Page needed. The token itself identifies the account, so `ig_user_id` is optional. |

## Requirements

- Python 3.9 or newer
- `requests` (`pip3 install requests`)
- An Instagram **professional** account (business or creator). Personal accounts have no API access.
- A Meta developer app and a long-lived user access token (see Setup)

## Setup

1. **Create a Meta app** at developers.facebook.com and add the Instagram product. Follow Meta's
   "Instagram Platform" getting-started guide for the flavour you chose above.
2. **Get a long-lived access token** with these permissions:
   - Instagram Login flavour: `instagram_business_basic`, `instagram_business_content_publish`,
     `instagram_business_manage_insights`
   - Facebook Login flavour: `instagram_basic`, `instagram_content_publish`,
     `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`
3. **Create the config file**:
   ```
   cp igtool.example.ini igtool.ini
   ```
   Fill in `access_token` and set `api_host`. `igtool.ini` is listed in `.gitignore` so the token
   stays out of git.
4. **Find your account id**:
   ```
   python3 -m igtool whoami
   ```
   Put the id into `ig_user_id` (required for `graph.facebook.com`, optional for `graph.instagram.com`).
5. **Check the connection**:
   ```
   python3 -m igtool limit
   ```
   This prints how many of your daily API publishes you have used. If it works, the token, host and
   account id are all correct.

Long-lived tokens expire after about 60 days. `python3 -m igtool refresh-token` prints a fresh one
(for the Facebook Login flavour pass `--app-id` and `--app-secret`); paste it into `igtool.ini`.

## Commands

All commands accept `--config PATH` (default `igtool.ini`) and `-v` for debug logging.

| Command | What it does |
| --- | --- |
| `whoami` | Shows the account(s) the token can act for, with id, username, follower and post counts. |
| `limit` | Shows usage of the rolling 24-hour publishing quota (Meta allows 50 to 100 API posts per day). |
| `refresh-token [--app-id --app-secret]` | Returns a fresh long-lived token. |
| `publish --kind K --media URL [--caption ...] [--option k=v]` | Publishes a post immediately. |
| `schedule add --at "YYYY-MM-DD HH:MM" [--tz ZONE] --kind K --media URL ...` | Queues a post. |
| `schedule list [--status S]` | Lists the queue. Statuses: `pending`, `publishing`, `published`, `failed`, `cancelled`. |
| `schedule cancel ID` | Cancels a pending post. |
| `schedule run-once` | Publishes everything that is due, then exits. Good for cron. |
| `schedule run [--interval SECONDS]` | Stays running and publishes posts as they come due (default: check every 60 s). |
| `report [--days N] [--since D --until D] [--media-limit N] [--out DIR] [--print]` | Builds an insights report. |

## Scheduling posts

### Media hosting

The Graph API does not accept file uploads. It downloads your media from a **public URL** at
publish time, so each image or video must be reachable without a login until the post is live.
Any object storage, CDN or plain web server works. Formats: JPEG for images, MP4 or MOV for video.

### Post kinds

| `--kind` | `--media` | Notes |
| --- | --- | --- |
| `image` | one image URL | Options: `alt_text`, `location_id`, `user_tags`, `collaborators` |
| `reel` | one video URL | Options: `cover_url`, `thumb_offset`, `share_to_feed`, `location_id`, `collaborators`, `audio_name` |
| `story` | one image or video URL | Video is detected by extension (`.mp4`, `.mov`, `.m4v`). Stories take no caption. |
| `carousel` | 2 to 10 URLs, repeat `--media` | Images and videos can be mixed. One caption for the whole carousel. |

Options are passed straight through to the API as `--option key=value`, so any field Meta adds
later works without a code change.

### Examples

```
# an image at 09:00 Berlin time
python3 -m igtool schedule add --at "2026-10-14 09:00" --tz Europe/Berlin \
    --kind image --media https://cdn.example.com/a.jpg --caption "Morning" --option alt_text="Sunrise over the harbour"

# a reel that also appears in the feed
python3 -m igtool schedule add --at "2026-10-15 18:00" --kind reel \
    --media https://cdn.example.com/clip.mp4 --caption "Clip" --option share_to_feed=true

# a carousel
python3 -m igtool schedule add --at "2026-10-16 12:00" --kind carousel \
    --media https://cdn.example.com/1.jpg --media https://cdn.example.com/2.jpg --caption "Two views"

python3 -m igtool schedule list
python3 -m igtool schedule cancel 3
python3 -m igtool schedule run
```

Times without `--tz` use the `timezone` from `igtool.ini` (default UTC). Everything is stored in UTC.

### What happens at publish time

1. The scheduler picks every `pending` row whose time has passed and marks it `publishing`.
2. It creates a media container. For a carousel it first creates one container per item.
3. It polls the container every 10 seconds until Meta reports `FINISHED` (videos are transcoded
   first and can take a few minutes; the wait gives up after 15 minutes).
4. It publishes the container and stores the resulting media id on the row, now `published`.

If any step fails, the row goes back to `pending` with the error message kept in `last_error`, and
the next tick tries again. After three failed attempts the row becomes `failed` and is left alone.
`schedule list` shows the error so you can fix the URL or caption and queue the post again.

## Reports

```
python3 -m igtool report                      # last 28 days, ending yesterday
python3 -m igtool report --days 7 --print     # last week, also print the markdown
python3 -m igtool report --since 2026-09-01 --until 2026-09-30 --media-limit 50
```

A report covers:

- **Account**: followers, following, post count.
- **Totals for the period**: views, reach, accounts engaged, total interactions, likes, comments,
  shares, saves, replies, profile views, website clicks, follows and unfollows.
- **Daily series**: reach and follower count per day.
- **Posts in the period**: every post since the start date (up to `--media-limit`), with likes,
  comments, views, reach, saves, shares and total interactions, ranked by interactions. Reels and
  stories get their own metric sets (watch time for reels, replies for stories).
- **Notes**: metrics the API refused for your account or API version, instead of failing the run.

Four files are written to `report_dir` (default `reports/`):

| File | Contents |
| --- | --- |
| `report_<since>_<until>.md` | Human-readable report with tables |
| `report_<since>_<until>.json` | Everything, machine-readable |
| `report_<since>_<until>_daily.csv` | One row per day |
| `report_<since>_<until>_media.csv` | One row per post, all metrics as columns |

Meta's insights API only returns time series in windows of up to 30 days, so longer ranges are
fetched in chunks and stitched together. Insight data lags by up to 48 hours, which is why the
default range ends yesterday. Story metrics are only available for 24 hours after posting.

## Configuration reference

`igtool.ini`, section `[igtool]`. Every key can also be set as an environment variable
`IGTOOL_<KEY>` in upper case (for example `IGTOOL_ACCESS_TOKEN`), which overrides the file.
`IGTOOL_CONFIG` points at a different ini file.

| Key | Default | Meaning |
| --- | --- | --- |
| `access_token` | | Long-lived user access token. Required. |
| `ig_user_id` | | Instagram professional account id. Required for `graph.facebook.com`. |
| `api_host` | `graph.facebook.com` | `graph.facebook.com` or `graph.instagram.com`, see [How it works](#how-it-works). |
| `api_version` | `v26.0` | Graph API version. |
| `database` | `igtool.db` | SQLite file for the post queue. |
| `report_dir` | `reports` | Where reports are written. |
| `timezone` | `UTC` | Default zone for `schedule add --at`. |
| `account_series_metrics` | `reach, follower_count` | Daily time-series metrics. |
| `account_total_metrics` | see `igtool.example.ini` | Period-total metrics. |
| `media_metrics_feed` | see `igtool.example.ini` | Metrics requested for feed posts and carousels. |
| `media_metrics_reels` | see `igtool.example.ini` | Metrics requested for reels. |
| `media_metrics_story` | see `igtool.example.ini` | Metrics requested for stories. |

The metric lists exist because Meta renames metrics from time to time (for example `impressions`
became `views` in 2025). If a report's Notes section says a metric is unknown, remove or rename it
here; no code change needed.

## Running it continuously

**cron**: check the queue every five minutes and build a weekly report.

```
*/5 * * * *  cd /path/to/instabot.py && python3 -m igtool schedule run-once >> igtool.log 2>&1
0 7 * * 1    cd /path/to/instabot.py && python3 -m igtool report --days 7 >> igtool.log 2>&1
```

**Long-running process**: `python3 -m igtool schedule run` under systemd, supervisor or a terminal
multiplexer. It checks the queue every 60 seconds and logs each publish.

**Docker**: the image in `Dockerfile` installs the requirements and copies the repository. Mount
your config and database and override the command:

```
docker build -t igtool .
docker run -d --name igtool \
    -v "$PWD/igtool.ini:/app/igtool.ini:ro" \
    -v "$PWD/igtool.db:/app/igtool.db" \
    -v "$PWD/reports:/app/reports" \
    igtool python -m igtool schedule run
```

## Tests

The test suite uses a fake HTTP session, so it runs offline and never touches your account:

```
python3 -m unittest discover -s tests
```

## Legacy bot

`src/`, `run.py` and `example.py` are the original 2017-2019 bot. It logged in with a username and
password, pulled hashtag pages through endpoints Instagram has since removed, and auto-liked,
auto-followed and auto-commented. Instagram's anti-automation systems blocked it then, and the
endpoints are gone now, so it cannot run against today's Instagram. It is not maintained, and this
repository does not aim to revive it. The last README that documented it is on the `master` branch.

## License

See [LICENSE](LICENSE).
