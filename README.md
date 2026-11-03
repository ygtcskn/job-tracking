# Job Tracking

Collects new entry-level data / ML / AI job postings in Germany every hour from
**LinkedIn**, the **Arbeitsagentur** and **Google Jobs**, and writes them to `jobs.xlsx`
in the layout of [`jobs_mockup.xlsx`](jobs_mockup.xlsx).

## Quick start

```bash
uv sync
uv run job-tracking run            # fetch new jobs and write jobs.xlsx
uv run job-tracking run --dry-run  # just list what would be added
```

Then open `jobs.xlsx`.

## The sheet

| Column | Meaning |
| --- | --- |
| Date / Time | When the pipeline found the job (Istanbul time) |
| Title, Company, Location, Country | As the source shows them (German city names in English, e.g. München → Munich) |
| Work Type | Remote / Hybrid / On-site; blank when the source doesn't say |
| Date posted | Posting date reported by the source |
| Time posted since | The source's own wording at fetch time ("14 minutes ago", "Today", …) |
| Source | LinkedIn, Arbeitsagentur or Google |
| Link | Opens the posting |

- Newest run at the top; inside a run, most recently posted first.
- Each of today's runs has its own color (the newest is always peach). Earlier days are grey.
- Rows fetched more than 30 days ago drop off the sheet (`max_age_days`). They stay in the database.
- **If `jobs.xlsx` is open in Excel** during a run, it's left alone and the new rows go to
  `jobs_pending.xlsx`. The next run that finds `jobs.xlsx` closed merges them in and deletes the pending file.

## What gets kept

Each run searches every keyword in `config.toml` on each source, then drops a job if:

- it's already in the sheet (same source id), or the same title + company was found in the
  last 30 days on any source (this also catches gender tags like `(m/w/d)` vs `(w/m/d)`);
- its title has an excluded word (`senior`, `lead`, `head`, …), or none of the `title_include` words;
- it was posted more than `max_posted_age_days` ago.

All of this is in [`config.toml`](config.toml): keywords, include/exclude words, LinkedIn
experience levels, how far back to look, and so on.

## Sources

| Source | How | Notes |
| --- | --- | --- |
| LinkedIn | The public job search LinkedIn shows signed-out visitors | No account needed. Rate-limited, so requests are spaced 2 s apart and paging stops at results already seen. This is not an official API: LinkedIn's terms don't allow automated access, and the page format can change. |
| Arbeitsagentur | The official job search API (`/pc/v6/jobs`) | No key needed. |
| Google Jobs | [SerpApi](https://serpapi.com/google-jobs-api) | Needs a SerpApi key. Each keyword is one SerpApi search, so it runs once every 24 h by default (`every_hours`). |

To turn on Google Jobs, create a `.env` file next to `config.toml` (it's gitignored):

```
SERPAPI_API_KEY=your-key-here
```

If one source fails (rate limit, network error), the run still finishes with the others.

## Running every hour

On macOS, from the main checkout (not a temporary worktree):

```bash
uv run job-tracking schedule install
```

This adds a launchd agent (`~/Library/LaunchAgents/local.job-tracking.plist`) that runs
`job-tracking run` at minute 0 of every hour. If the Mac was asleep, it runs once on wake.

```bash
uv run job-tracking schedule status
```

```bash
uv run job-tracking schedule uninstall
```

## Other commands

```bash
uv run job-tracking run --source LinkedIn   # one source only (ignores its every_hours limit)
```

```bash
uv run job-tracking render                  # rewrite the sheet from saved jobs, no fetching
```

```bash
uv run job-tracking -v run                  # show debug output, incl. why each job was dropped
```

## Files

| Path | What |
| --- | --- |
| `config.toml` | Settings |
| `jobs.xlsx`, `jobs_pending.xlsx` | Output (gitignored) |
| `data/jobs.db` | SQLite database of every job found, used for de-duplication (gitignored) |
| `logs/job-tracking.log` | Run log, rotated at 1 MB (gitignored) |
| `src/job_tracking/` | Code: `sources/` (one file per site), `pipeline.py`, `excel.py`, `store.py` |

## Tests

```bash
uv run pytest
```

Tests use small made-up fixtures in `tests/fixtures/` shaped like each source's real
responses, and check the sheet's styling against `jobs_mockup.xlsx`.
