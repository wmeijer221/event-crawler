# Event Crawler

[Link to public page](https://wmeijer221.github.io/event-crawler/)

A small project that combines a static events board (frontend) with a Python
crawler that discovers and extracts event data from the web into
`data/events.json`.

This repository contains two complementary pieces:

- A static frontend (HTML/CSS/JS) that reads `data/events.json` and renders
  an events board for easy browsing and filtering.
- A Python-based crawler and extraction pipeline that uses local LLM tooling
  to find and parse event pages into structured JSON.

## Features

- Crawl and extract events from search seed results.
- Deduplicate and merge new events with an existing `data/events.json`.
- Simple static frontend for viewing and filtering events.

## Requirements

- Python 3.12+
- Ollama installed and available on your PATH (the crawler starts `ollama serve`).
- Recommended: create and activate a virtual environment before installing.

See `pyproject.toml` for Python dependencies and the project console script.

## Quick install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs a console script named `event-crawler` (see `pyproject.toml`).

## Prepare data file

The crawler expects `data/events.json` to exist (it reads the existing list
to merge new events). If you don't have one yet, create it with an empty
array:

```bash
mkdir -p data
echo "[]" > data/events.json
```

## Run the crawler

Start `ollama` if it's required by your local LLM setup (see Ollama docs),
then run:

```bash
event-crawler
```

This runs the crawler and writes/updates `data/events.json`.

If you prefer not to install, you can run the package module directly from
the repository (from the project root with the venv activated):

```bash
python -m event_crawler
```

Note: the crawler uses local LLM tooling and web search; review the code in
`src/event_crawler` to configure search regions, models, or prompts.

## Frontend (static) usage

The static board is in the repository root. To run it locally, serve the
folder and open the page in your browser:

```bash
python3 -m http.server 8000
```

Then visit `http://localhost:8000` and the frontend will load data from
`data/events.json`.

Files of interest:

- [index.html](index.html)
- [css/style.css](css/style.css)
- [js/app.js](js/app.js)
- [data/events.json](data/events.json)

## Data format

Each event in `data/events.json` is an object with fields such as:

- `title` (required)
- `date` (optional, ISO format `YYYY-MM-DD`)
- `description`, `time`, `url`, plus any additional metadata you want to
  include.

Extra fields will be shown in the frontend's "More details" section. Events
missing a `title` are ignored by the frontend.

## Configuration & notes

- The crawler uses `ollama` via the `ollama` Python client and spawns
  `ollama serve` when running. Ensure Ollama is installed and configured on
  your machine.
- The exact model and search configuration are defined in
  `src/event_crawler/__main__.py` and related modules (`crawler.py`,
  `chat_to_json.py`, `system_prompts.py`). Tweak those to adjust behavior.

## Contributing

Contributions are welcome. Open issues for bugs or feature requests, and
send pull requests for changes.
