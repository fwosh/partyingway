# Setting up automated intake

Start to finish, about 45 minutes, most of it clicking around Google Cloud.
Your reminder workflow keeps running untouched the whole way through — none
of this changes `post_reminders.py`.

Nothing publishes anything until step 10, so you can stop and look at every
stage before it goes live.

---

## 1. Anthropic API key, with a hard ceiling

1. Go to **console.anthropic.com** and sign in. This is a separate account
   from any Claude.ai subscription — the two don't share billing.
2. **Billing → buy credits.** Load **$5**. Leave **auto-reload off.** This is
   your real cap: when the credits are gone, parsing stops and nothing else
   is affected. $5 is roughly a year at 1–3 events a day.
3. **API keys → Create key.** Copy it; you'll paste it in step 7.

The per-run `MAX_PARSES_PER_RUN` cap (default 8) in `intake.yml` is the
second guard, against a sudden burst of mentions.

---

## 2. Google Cloud project and service account

This is the longest step and the only genuinely fiddly one. It's also what
replaces IFTTT.

1. **console.cloud.google.com** → project picker → **New project**. Call it
   `partyingway`.
2. **APIs & Services → Library.** Enable both:
   - **Google Sheets API**
   - **Google Calendar API**
3. **IAM & Admin → Service Accounts → Create service account.**
   Name it `partyingway-bot`. Skip the two optional "grant access" steps —
   it needs no project roles, only per-file sharing.
4. Open the service account → **Keys → Add key → Create new key → JSON.**
   A file downloads. Keep it; you'll paste its whole contents in step 7.
5. Copy the service account's **email address** (it looks like
   `partyingway-bot@partyingway-123456.iam.gserviceaccount.com`). You need
   it twice below.

---

## 3. Create the review sheet

1. Make a new spreadsheet at **sheets.new**. Name it
   `Partyingway review queue`.
2. Rename the first tab to exactly **`Review queue`**.
3. **Share** → paste the service account email → give it **Editor** →
   untick "Notify people" → Send. (It'll look like sharing with a stranger.
   That's fine; it's your own robot.)
4. Copy the spreadsheet ID out of the URL — the long string between
   `/d/` and `/edit`.

---

## 4. Lay out the sheet

From your laptop, in the repo folder:

```bash
pip install -r requirements.txt
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat ~/Downloads/partyingway-123456.json)"
export SHEET_ID="the-id-from-step-3"
python setup_sheet.py
```

Open the sheet. You should have a frozen header row, real checkboxes down
column A, and wide Address/Notes/Flyer columns. If it complains about the
tab name, check step 3.2.

---

## 5. Share the calendar with the same account

1. Google Calendar → hover **Partyingway Art Parties** → ⋮ →
   **Settings and sharing**.
2. **Share with specific people → Add people** → paste the same service
   account email.
3. Set permission to **Make changes to events**. Send.
4. Scroll to **Integrate calendar** and copy the **Calendar ID**.

---

## 6. Add the files to your repo

Drop these alongside your existing `post_reminders.py`:

```
intake.py                       publish.py
sheet.py                        setup_sheet.py
requirements.txt                (replaces yours)
.github/workflows/intake.yml    .github/workflows/publish.yml
```

Your existing `reminders.yml`, `post_reminders.py` and `state.json` stay
exactly as they are. You can delete `queued.json` if you made one earlier —
the sheet does that job now. Commit and push to `main`.

---

## 7. Add the repo secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from step 1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the JSON key file, pasted as-is |
| `SHEET_ID` | from step 3 |
| `CALENDAR_ID` | from step 5 |

`BSKY_HANDLE` and `BSKY_APP_PASSWORD` are already there from the reminder
setup, and both new workflows reuse them.

---

## 8. Dry-run the intake

Actions → **Art party intake** → Run workflow. Leave **dry run checked**.
Set `lookback_hours` to something generous like `336` (two weeks) so it
reaches back over submissions you've already processed by hand.

Open the log and expand "Scan mentions and queue submissions". For each
mention it found, it prints the row it *would* add and the token counts.
Nothing is written to the sheet.

**This is the step that tells you whether it works.** You already know the
right answers for those events, so compare. Pay attention to:

- Did it find the right post? It should show the *host's announcement*, not
  the reply that tagged you.
- Is **As written** reproduced exactly from the post?
- Is **Start PT** the conversion you'd have done yourself?
- Where the post was vague, did it leave the field blank rather than guess?

If the timezone handling looks shaky on your hosts' actual posting habits,
tell me what it got wrong — that's a prompt fix, not a rebuild.

---

## 9. Let it fill the sheet for real

Run **Art party intake** again with **dry run unchecked**. Rows appear.
Flyers render in column J. Rows missing a required field turn amber.

Now tick **Approve** on exactly one row — ideally a past event, so a mistake
is harmless.

Run **Publish approved art parties** with **dry run checked**. It reports
what it would do without posting. Then run it again unchecked and confirm:

- a repost appears on the partyingway timeline
- the event appears on the Partyingway Art Parties calendar
- the row's Status reads `Posted 11-23 14:02` and the row turns green

Delete the test event and repost afterward.

---

## 10. Go live

Both workflows are already on a schedule — intake every 20 minutes, publish
every 10 — so once the dry runs look right, there's nothing to switch on.
Tell your hosts the protocol: **reply to the announcement post and tag
@partyingway.** DMs are ignored.

---

## 11. Turn off IFTTT

Only once you've watched step 9 work on a real submission.

`PUBLISH_MODE: calendar` in `publish.yml` means the calendar event is
already being written directly — IFTTT isn't doing anything any more.
Open IFTTT, disable the applet, confirm the next approval still lands on
the calendar, then cancel the subscription. The Google Form and its
responses sheet can stay as a manual fallback; nothing reads them.

### If you'd rather keep IFTTT for a while

Set `PUBLISH_MODE: sheet` in `publish.yml` and add two more secrets:
`RESPONSES_SHEET_ID` (the form's responses spreadsheet) and, if your tab
isn't named `Form Responses 1`, `RESPONSES_TAB`. Share that spreadsheet
with the service account too. Approving then appends a row exactly where
your form submissions land, and IFTTT builds the event as it does today.
Check `RESPONSES_COLUMNS` in `publish.py` matches your column order.

---

## Day-to-day

**Reviewers** only ever touch the sheet: read the row, fix anything wrong,
tick Approve. Anyone you share the sheet with can review — they don't need
GitHub, Google Cloud, or anything else.

**Amber cell** = that specific field is empty, or the address doesn't look
like a full FFXIV address. Fix it and tick. Empty rows stay white.

**Check column** = the parser found a genuine conflict, usually the post text
and the flyer disagreeing. Blank the vast majority of the time.

**Status says `needs start`** = publish tried and stopped. Fix the cell;
it retries on the next run, no need to untick anything.

**Times are stored in the host's own timezone, not converted.** If someone
says 5PM EST, the sheet holds `17:00` and `ET`, and Google Calendar shows
2PM to you and 5PM to them.

**Zone is a dropdown** — PT, MT, CT, ET, AKT, HT, UTC (Server Time), and a
few European and Asian zones. You can also type: `PST`, `EST`, `Eastern`,
`Server Time`, `BST` and full names like `Europe/Berlin` all resolve
correctly. Behind the scenes everything maps to a daylight-saving-aware
zone, so `EST` in July still comes out right.

**Status says `needs retry: ...`** = something failed mid-publish (API
hiccup, network). It retries by itself.

**To reject a submission**, leave Approve unticked. It stays on the sheet
and is never published.

**To get it out of your view**, tick **Archive**. The row moves to the
Archive tab on the next publish run — no waiting, no reason needed. Use it
for things you posted by hand, recurring parties you've already dealt with,
or anything you don't want to look at again. Archive beats Approve: a row
with both ticked is filed, not published.

Archived rows still count for dedupe, so the same post is never pulled back
in. That's why archiving is better than deleting the row — a deleted row
would look brand new to the next intake run.

## Changing things

| Want to | Change |
|---|---|
| Parse more cheaply | `MODEL: claude-haiku-5-5` in `intake.yml` |
| Different parse cap | `MAX_PARSES_PER_RUN` in `intake.yml` |
| Stop auto-reposting | `REPOST: "false"` in `publish.yml` |
| Different default length | `DEFAULT_DURATION_HOURS` in `publish.yml` |
| Add/reorder sheet columns | `COLUMNS` in `sheet.py`, then re-run `setup_sheet.py` |
| Change the reminder wording | `build_post_text()` in `post_reminders.py`, as before |
