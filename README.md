# Bluesky event reminders

Posts a Bluesky reminder roughly an hour before each event on the
Partyingway Art Parties calendar. Reads the calendar's public `.ics` feed,
so there's no Google API key, no OAuth, and nothing to renew. Runs free on
GitHub Actions.

## Setup

**1. Get a Bluesky app password.**
Bluesky → Settings → Privacy and Security → App Passwords → Add App Password.
Copy it immediately; it isn't shown again. Don't use your account password —
an app password can be revoked on its own.

**2. Add two repo secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `BSKY_HANDLE` | e.g. `partyingway.bsky.social` |
| `BSKY_APP_PASSWORD` | from step 1 |

The calendar URL is already in `reminders.yml` — it's public, so it isn't a
secret.

**3. Make the repo public.** Actions minutes are unlimited on public repos;
private repos get 2,000 minutes/month, and a job every 30 minutes burns
~1,440 runs a month, which lands uncomfortably close to that cap once
GitHub rounds each run up to the minute. Nothing sensitive lives in the
code — both credentials are in the secrets store.

**4. Test.** Actions tab → *Bluesky event reminders* → Run workflow, leaving
"dry run" checked. It prints what it would post without publishing. Put a
test event ~45 minutes out on the calendar first, or there'll be nothing to
show.

## How it decides what to post

Every 30 minutes it fetches the feed and looks for events starting within
the next 75 minutes that aren't already in `state.json`. In practice a post
lands 45–75 minutes ahead of the event. Widen or narrow with the
`LEAD_MINUTES` env var in the workflow.

Skipped automatically: all-day events (no meaningful "1 hour before"),
and anything with `STATUS:CANCELLED`.

Recurring events are expanded client-side, and each occurrence is tracked
separately — a weekly event gets its own post each week, not one ever.

## Things worth knowing

- **Feed freshness is the one thing to verify.** Google's ICS feeds sit
  behind a CDN. The script sends a cache-buster and `Cache-Control:
  no-cache`, but if you add an event at noon for that evening and the post
  never fires, that's the likely cause. Test it once with a same-day event
  before trusting it. If the feed does turn out to lag, the fix is switching
  `fetch_events` to the Calendar API with an API key, which reads live data.
- **60-day inactivity disables the schedule.** GitHub silently turns off
  scheduled workflows in repos with no commits for 60 days. The state
  commits after each post count as activity, so an active calendar keeps
  itself alive. If parties stop for a whole winter, push any commit to
  re-arm it.
- **300 graphemes max.** Long descriptions get truncated with an ellipsis.
  If the description is long, consider putting the essentials first.
- **No retries.** A failed run is skipped; the next tick picks the event up
  again as long as it hasn't started yet.
- **Links need facets** or they render as inert text. The script builds them
  for any `http(s)://` URL in the description.

## Running locally

```bash
pip install -r requirements.txt
export ICS_URL="https://calendar.google.com/calendar/ical/02e772ffa8b579981dcecb0da5a0a27cb5bd929759d737607e38910a71243d46%40group.calendar.google.com/public/basic.ics"
export BSKY_HANDLE=... BSKY_APP_PASSWORD=... DRY_RUN=1
python post_reminders.py
```
