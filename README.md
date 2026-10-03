# Courthouse ceremony checker

Checks the Santa Barbara County Courthouse Calendly page for
**"Santa Barbara Marriage Ceremony with License [English]"** openings on
**Dec 16, 2026**, every hour, and pushes an alert to your phone the moment a
slot appears. Runs free on GitHub Actions, so your laptop can stay off.

- Calendar checked: https://calendly.com/sb-marriages/sb-license-ceremony-eng
- Alerts only for *new* slots, so you won't get the same one every hour.
- If Calendly changes something and the check breaks, you get a "checker failed" push.

## Setup (about 5 minutes)

### 1. Phone notifications
1. Install the **ntfy** app (iOS App Store or Google Play). It's free, no account needed.
2. Make up a long, unguessable topic name, e.g. `sb-wedding-7f3k9q2x`.
   Anyone who knows the name can read the alerts, so don't use something obvious.
3. In the app, tap **+** and subscribe to that topic.

### 2. GitHub repo
1. Create a new repository on GitHub. **Public** gets unlimited free Actions
   minutes; private works too (hourly runs use well under the 2,000 free
   minutes a month). Your topic name is stored as a secret either way.
2. Upload these files, keeping the folder structure:
   ```
   check_calendly.py
   .github/workflows/check.yml
   README.md
   ```
   From a terminal:
   ```bash
   cd courthouse-checker
   git init && git add . && git commit -m "Courthouse checker"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

### 3. Add your topic as a secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**
- Name: `NTFY_TOPIC`
- Value: the topic name from step 1

### 4. Test it
Repo → **Actions → Check courthouse availability → Run workflow** (leave
"Send a test push" ticked). Within a minute you should get
"Courthouse checker is working" on your phone. After that it runs by itself
every hour.

## Changing things (optional)
Repo → **Settings → Secrets and variables → Actions → Variables tab**:

| Variable | What it does | Default |
|---|---|---|
| `TARGET_DATES` | Dates to watch, comma-separated `YYYY-MM-DD` | `2026-12-16` |
| `CALENDLY_URL` | Which calendar to watch | the "with License [English]" ceremony |

E.g. set `TARGET_DATES` to `2026-12-16,2026-12-17,2026-12-18` to watch backup dates.

- **Check more often:** edit the `cron` line in `.github/workflows/check.yml`
  (`*/30 * * * *` = every 30 min). Times are in UTC.
- **Stop it:** Actions → the workflow → **⋯ → Disable workflow**. It also
  stops checking on its own once the date has passed.

## Notes
- The county books ceremonies up to 90 days ahead and only allows one
  appointment per couple, so if you already hold a different date, use
  Calendly's reschedule link from your confirmation email instead of booking
  a second one.
- GitHub can start scheduled runs a few minutes late, and pauses scheduled
  workflows in repos with no activity for 60 days (not an issue before Dec 16).
- This reads the same public availability data Calendly's booking page uses.
  It's not an official API, which is why the failure alert exists.
