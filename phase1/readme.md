# Financial Market Knowledge Scraping Engine

A multi-source news scraper that collects financial market articles from BBC, AP News, Al Jazeera, and Nordnet, stores them in PostgreSQL, and deduplicates content across runs using SHA-256 hashing.

---

## Overview

The engine runs on a schedule (or on demand), pulls articles published within a configurable lookback window, cleans the raw HTML of ads and navigation boilerplate, and writes the results to a PostgreSQL database. Each run is logged both to a rotating file and to a `scraper_logs` table so you have a full audit trail.

The current iteration covers four sources: BBC, AP News, Al-Jazeera, Nordnet. Each scraper can be turned on and off as per requirement in the `config.yaml` file.

---

## Project Structure

```
.
├── main.py             # Scraper entry point and all scraper classes
├── config.yaml         # All non-secret settings (edit this to configure the engine)
├── .env                # Secret credentials — never commit this file, please create yours using the below instructions
├── .env_template       # Copy this to .env and fill in your values
├── logs/               # Rotating log files (created automatically on first run)
├── setup/
  ├── architecture.png    # Architecture of the entire system in image format.
  ├── schema.png          # Database schema in image format for easier understanding.
  ├── create_db.sql       # Run this file to create the database.
  ├── requirements.txt    # Python dependencies for the application. Install it using pip/uv.
  ├── schema.sql          # Run this file in order to create the database schema.
└── readme.md
```

---

## Requirements

- Python 3.10+
- PostgreSQL 13+
- Google Chrome (for AP News Selenium URL resolution)
- `crawl4ai` and its browser setup (for Al Jazeera and Nordnet)

Install Python dependencies:

```bash
cd setup
pip install -r requirements.txt
```

If you plan to use the Al Jazeera and Nordnet scrapers, also run:

```bash
crawl4ai-setup
```

---

## Setup

### 1. Database

Create the database and tables before the first run. The engine expects tables named `sources`, `articles`, and will create `scraper_logs` automatically.

```bash
cd setup
psql -U postgres
# Enter the password for your database instance.
```

```sql
\i create_db.sql
\i schema.sql
```

### 2. Credentials

Copy the template and fill in your values:

```bash
cp .env_template .env
```

The `.env` file should contain at minimum:

```
DB_NAME=scraped_data
DB_USER=postgres
DB_PASSWORD=your_password_here
DB_HOST=localhost
DB_PORT=5432
```

If you are connecting to Azure Database for PostgreSQL, also add:

```
DB_SSLMODE=require
```

The `.env` file is loaded inside `main()` at startup. Values here override anything set in `config.yaml`, so you never need to put passwords in the YAML file.

### 3. Configuration

Open `config.yaml` and adjust the settings for your environment. The file is commented throughout. The most commonly changed settings are:

| Setting                                | Location in config.yaml | What it does                        |
| -------------------------------------- | ----------------------- | ----------------------------------- |
| `hours_lookback`                     | `scraping`            | How far back to look for articles   |
| `max_articles_per_source`            | `scraping`            | Cap per source per run              |
| `bbc / apnews / aljazeera / nordnet` | `scrapers_enabled`    | Turn individual scrapers on or off  |
| `level`                              | `logging`             | Set to `DEBUG` for verbose output |

---

## Running

```bash
python main.py
```

The engine will:

1. Load `.env` and `config.yaml`
2. Connect to PostgreSQL
3. Run enabled scrapers in order (sync scrapers first, then async)
4. Print a summary of articles inserted per source
5. Close the database connection

To run on a schedule, add a cron entry:

```bash
# Run every hour
0 * * * * cd /path/to/project && python main.py >> logs/cron.log 2>&1
```

---

## How Duplicate Detection Works

Each article is hashed using SHA-256 over a normalized combination of its title and URL. Normalization strips source suffixes from titles ("- BBC News", etc.) and extracts the canonical article identifier from the URL path, so the same article fetched from a Google News redirect and from the direct URL produces the same hash. If the hash already exists in the database, the insert is skipped.

---

## Logging

Logs are written to two places simultaneously:

- `logs/scraper.log` — rotating file, 10 MB per file, 5 backups kept
- `scraper_logs` table in PostgreSQL — each event stored as a row with a JSONB details column

The log timestamp format includes the timezone (`%Z`), so UTC is shown explicitly in every line. To change the log level, edit `logging.level` in `config.yaml`.

---

## Security Notes

- Never commit `.env` to version control. Add it to `.gitignore`.
- All secrets are loaded from environment variables at runtime inside `main()`. The YAML config file contains only non-sensitive defaults.
- The database password in `config.yaml` is a fallback default only. Always set `DB_PASSWORD` in `.env` for any real deployment.
