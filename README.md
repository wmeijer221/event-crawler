# Events Board

A small, static search engine for local events. No build step, no backend —
just HTML, CSS, and vanilla JavaScript reading from a JSON file. Built to be
hosted on GitHub Pages.

## What it does

- **Overview strip** — total upcoming events, how many are in the next 7 days, and what's next.
- **Keyword search** — matches against each event's title and description, with matches highlighted.
- **Date filtering** — From and To default to today through two weeks out, so the board opens scoped to what's coming up soon. Widen, narrow, or clear either field as needed.
- **Toggle filters**:
  - *Include past events* — off by default, so the board opens future-first.
  - *Only dated events* — off by default, so events without a `date` still show up (sorted to the end of the list, labeled "No date"). Turn it on to hide anything undated.
  - *Only events with links* — **on** by default, so the board opens showing just events you can click through to. Turn it off to also see events without a `url`.
- **Detail panel** — click (or press Enter/Space on) any event to open a side panel with the full description and a button to the external event page.
- **Future-first by default** — only today's and upcoming (dated) events show until you say otherwise.

## Files

```
index.html          Page structure
css/style.css        Styling
js/app.js            Loading, filtering, search, and the detail panel
data/events.json    Your event data — replace with your own
```

## Editing your events

Each event in `data/events.json` is an object with these fields:

| Field         | Required | Notes                                                        |
|---------------|----------|---------------------------------------------------------------|
| `title`       | yes      | Shown in the list and detail panel.                            |
| `date`        | no       | Format `YYYY-MM-DD`. Used for sorting, date filtering, and the "Today / In N days" badge. Omit it (or leave it invalid) for events without a fixed date — they'll still show up unless "Only dated events" is turned on. |
| `description` | no       | Plain text. Shown as an excerpt in the list, in full in the panel. |
| `time`        | no       | Free text, e.g. `"18:00"` or `"06:30-18:00"`.                  |
| `url`         | no       | Link to the event's own page. Use `null` or omit it if there isn't one — just note that "Only events with links" is on by default, so linkless events are hidden until that's switched off. |

Any extra fields you add (e.g. `location`, `price`, `category`) aren't
required — they'll automatically show up in a "More details" section of the
detail panel, labeled with the field name. Events missing a `title` are
skipped rather than breaking the page.

## Running locally

Opening `index.html` directly by double-clicking it won't work in most
browsers, because `fetch()` can't load `data/events.json` from a `file://`
URL. Serve the folder instead, for example:

```bash
cd events-board
python3 -m http.server 8000
```

Then visit `http://localhost:8000`.

## Deploying to GitHub Pages

1. Push this folder's contents to a GitHub repository (they can sit at the repo root, or in a `/docs` folder).
2. In the repo, go to **Settings → Pages**.
3. Under **Build and deployment**, set the source branch and folder (e.g. `main` / `/root` or `main` / `/docs`).
4. Save — GitHub will give you a URL like `https://<username>.github.io/<repo>/`.

Every time you edit `data/events.json` and push, the live site updates.

## Notes

- Dates and times are treated as local time in the visitor's browser — there's no timezone field, so keep that in mind if your audience spans multiple timezones.
- Search is a simple case-insensitive substring match, not fuzzy search — it's intentionally simple for a small, static dataset.
