# Daily remote job digest: setup (about 10 minutes, one time)

## 1. Create a Gmail app password
1. Google Account > Security > turn on 2-Step Verification (if it isn't on).
2. Search "App passwords" in your Google Account, create one named "job digest".
3. Copy the 16-character password. You'll paste it in step 3.

## 2. Put the files in GitHub
1. Create a new **private** repository (e.g. `job-digest`).
2. Upload everything in this folder, including the hidden `.github/workflows/daily.yml`
   (on the website: Add file > Upload files; keep the folder structure).

## 3. Add your secrets
Repo > Settings > Secrets and variables > Actions > New repository secret:
- `GMAIL_ADDRESS` = jkunselman1@gmail.com
- `GMAIL_APP_PASSWORD` = the 16-character password from step 1

## 4. Test it
Repo > Actions > "Daily job digest" > Run workflow. Within a minute or two you
should get an email. Check its "Source health" section at the bottom: it shows
which sources worked and which watchlist companies were found.

After that it runs by itself every day around 7-8am Eastern.

## Tuning
- `watchlist.txt`: add or remove companies (one per line).
- Top of `job_crawler.py`: title keywords, excluded terms, time window, PTO phrases.
- If a company matches the wrong board, pin it: `Company | greenhouse:real-slug`.

## Good to know
- GitHub pauses scheduled runs after 60 days of repo inactivity; this setup commits
  `seen.json` when new jobs are found, which keeps it active.
- Gmail may send the first digest to spam. Mark it "Not spam".
- Never paste the app password into the code or the repo; keep it in Secrets.
