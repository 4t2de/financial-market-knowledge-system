"""
Financial Market Knowledge Scraping Engine - Phase 1
Consolidated scraper for BBC, AP News, Al Jazeera, and Nordnet
Stores all data in PostgreSQL database with duplicate detection

Features:
- YAML config file support for easy deployment
- Advanced content cleaning (removes ads, navigation, etc.)
- Database logging for audit trail
- Rotating file logs
"""
import os
import asyncio
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp
import feedparser
import psycopg2
from psycopg2.extras import execute_values
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import yaml

# Try to import crawl4ai for Al Jazeera and Nordnet
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False
    print("WARNING: crawl4ai not installed. Al Jazeera and Nordnet scrapers will be disabled.")
    print("  Install with: pip install crawl4ai && crawl4ai-setup")

# ---------------------------------------------------------------------------
# Module-level placeholders — populated inside main() after config is loaded
# ---------------------------------------------------------------------------

DB_CONFIG = {}
HOURS_LOOKBACK = 24
MAX_ARTICLES_PER_SOURCE = 10
REQUEST_DELAY = 1.2
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}
LOGGING_CONFIG = {}
CONTENT_CLEANUP_PATTERNS = []
MIN_CONTENT_LENGTH = 300
MAX_CONTENT_LENGTH = 6000
SCRAPERS_ENABLED = {}

# ---------------------------------------------------------------------------
# Logger (basic setup; reconfigured inside main() once LOGGING_CONFIG is known)
# ---------------------------------------------------------------------------

log = logging.getLogger("scraper")
log.setLevel(logging.INFO)
log.propagate = False

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z"
))
log.addHandler(_console_handler)


# ---------------------------------------------------------------------------
# Config + Credential Loading
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.yaml") -> dict:
    """Load YAML configuration file."""
    with open(config_path, "r") as fh:
        return yaml.safe_load(fh)


def apply_config(cfg: dict):
    """
    Populate module-level globals from the parsed YAML config dict.
    Credentials already resolved from environment variables are passed in via cfg.
    """
    global DB_CONFIG, HOURS_LOOKBACK, MAX_ARTICLES_PER_SOURCE, REQUEST_DELAY
    global LOGGING_CONFIG, CONTENT_CLEANUP_PATTERNS, MIN_CONTENT_LENGTH
    global MAX_CONTENT_LENGTH, SCRAPERS_ENABLED

    db = cfg.get("database", {})
    DB_CONFIG = {
        "dbname": db.get("name", "scraped_data"),
        "user":   db.get("user", "postgres"),
        "password": db.get("password", "root"),
        "host":   db.get("host", "localhost"),
        "port":   int(db.get("port", 5432)),
    }
    if db.get("sslmode"):
        DB_CONFIG["sslmode"] = db["sslmode"]

    scraping = cfg.get("scraping", {})
    HOURS_LOOKBACK           = scraping.get("hours_lookback", 24)
    MAX_ARTICLES_PER_SOURCE  = scraping.get("max_articles_per_source", 10)
    REQUEST_DELAY            = scraping.get("request_delay", 1.2)

    log_cfg = cfg.get("logging", {})
    LOGGING_CONFIG = {
        "level":           log_cfg.get("level", "INFO"),
        "format":          log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s - %(message)s"),
        "datefmt":         log_cfg.get("datefmt", "%Y-%m-%d %H:%M:%S %Z"),
        "log_to_file":     log_cfg.get("log_to_file", True),
        "log_file":        log_cfg.get("log_file", "logs/scraper.log"),
        "max_bytes":       log_cfg.get("max_bytes", 10 * 1024 * 1024),
        "backup_count":    log_cfg.get("backup_count", 5),
        "log_to_database": log_cfg.get("log_to_database", True),
    }

    content = cfg.get("content", {})
    MIN_CONTENT_LENGTH      = content.get("min_content_length", 300)
    MAX_CONTENT_LENGTH      = content.get("max_content_length", 6000)
    CONTENT_CLEANUP_PATTERNS = content.get("cleanup_patterns", [])

    SCRAPERS_ENABLED.update(cfg.get("scrapers_enabled", {}))


def configure_logging():
    """Reconfigure the module logger after LOGGING_CONFIG is populated."""
    log.handlers = []
    log.setLevel(getattr(logging, LOGGING_CONFIG.get("level", "INFO")))

    fmt = LOGGING_CONFIG.get("format", "%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    datefmt = LOGGING_CONFIG.get("datefmt", "%Y-%m-%d %H:%M:%S %Z")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    log.addHandler(console)

    if LOGGING_CONFIG.get("log_to_file", True):
        os.makedirs("logs", exist_ok=True)
        try:
            fh = RotatingFileHandler(
                LOGGING_CONFIG.get("log_file", "logs/scraper.log"),
                maxBytes=LOGGING_CONFIG.get("max_bytes", 10 * 1024 * 1024),
                backupCount=LOGGING_CONFIG.get("backup_count", 5),
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
            log.addHandler(fh)
            log.info("File logging enabled: " + LOGGING_CONFIG.get("log_file", "logs/scraper.log"))
        except Exception as e:
            log.warning(f"Could not setup file logging: {e}")


# ---------------------------------------------------------------------------
# Database Functions
# ---------------------------------------------------------------------------

def get_db_connection():
    """Create and return a database connection."""
    return psycopg2.connect(**DB_CONFIG)


def get_timezone_abbreviation() -> str:
    """
    Return a short (up to 5-char) timezone abbreviation for the current process,
    derived from the TZ environment variable or the system clock.
    Examples: UTC, IST, EST, PST, CET
    """
    now = datetime.now(timezone.utc).astimezone()
    tz_name = now.strftime("%Z")          # e.g. "UTC", "IST", "EST"
    return tz_name[:5] if tz_name else "UTC"


def init_sources(conn) -> dict:
    """Initialize sources table and return source_id mapping."""
    sources = [
        ('BBC', 'https://www.bbc.com/', 'news'),
        ('AP News', 'https://apnews.com/', 'news'),
        ('Al Jazeera', 'https://www.aljazeera.com/', 'news'),
        ('Nordnet', 'https://nordnetab.com/', 'news'),
    ]
    
    source_mapping = {}
    with conn.cursor() as cur:
        for name, base_url, source_type in sources:
            cur.execute("""
                INSERT INTO sources (name, base_url, source_type)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET 
                    base_url = EXCLUDED.base_url,
                    source_type = EXCLUDED.source_type
                RETURNING id
            """, (name, base_url, source_type))
            source_id = cur.fetchone()[0]
            source_mapping[name] = source_id
        conn.commit()
    
    log.info(f"Initialized {len(source_mapping)} sources")
    return source_mapping


def log_to_database(conn, level: str, source: str, message: str, details: dict = None):
    """
    Log scraping events to database for audit trail.
    Creates a scraper_logs table (with timezone column) if it doesn't exist.
    The `timezone` column stores a short TZ abbreviation (e.g. UTC, IST).
    """
    if not LOGGING_CONFIG.get('log_to_database', True):
        return

    tz_abbr = get_timezone_abbreviation()

    try:
        with conn.cursor() as cur:
            # Create table if not exists (idempotent) — includes `timezone` column
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scraper_logs (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMP DEFAULT NOW(),
                    timezone VARCHAR(5),
                    level VARCHAR(20),
                    source VARCHAR(100),
                    message TEXT,
                    details JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Add timezone column if table existed without it (migration guard)
            cur.execute("""
                ALTER TABLE scraper_logs
                ADD COLUMN IF NOT EXISTS timezone VARCHAR(5)
            """)

            # Create indexes if not exists
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_scraper_logs_timestamp 
                ON scraper_logs(timestamp DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_scraper_logs_source 
                ON scraper_logs(source)
            """)

            # Insert log entry with timezone
            cur.execute("""
                INSERT INTO scraper_logs (level, timezone, source, message, details)
                VALUES (%s, %s, %s, %s, %s::jsonb)
            """, (level, tz_abbr, source, message, psycopg2.extras.Json(details or {})))

            conn.commit()
    except Exception as e:
        # Don't let logging failures break the scraper
        log.warning(f"Failed to log to database: {e}")


def compute_hash(title: str, url: str) -> str:
    """
    Compute SHA-256 hash for duplicate detection with robust handling.
    Handles Google News redirects, tracking parameters, and URL variations.
    """
    from urllib.parse import urlparse, parse_qs
    
    # Extract real URL from Google News redirects
    if 'news.google.com' in url.lower():
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'url' in params and params['url']:
                url = params['url'][0]
        except:
            pass
    
    # Normalize URL: remove protocol, www, trailing slash
    try:
        parsed = urlparse(url.lower())
        domain = parsed.netloc.replace('www.', '')
        path = parsed.path.rstrip('/')
        
        # Try to extract article ID from URL (most reliable)
        article_id = None
        id_match = re.search(r'/article[s]?/([a-zA-Z0-9-]+)', path)
        if id_match:
            article_id = id_match.group(1)
        else:
            # Fallback: use domain + path
            article_id = f"{domain}{path}"
        
        normalized_url = article_id
    except:
        normalized_url = url.lower()
    
    # Normalize title: lowercase, remove source suffixes, extra whitespace
    normalized_title = title.lower().strip()
    for suffix in ['- ap news', '- bbc news', '- al jazeera', '| ap news', '| bbc news']:
        if normalized_title.endswith(suffix):
            normalized_title = normalized_title[:-len(suffix)].strip()
    normalized_title = re.sub(r'\s+', ' ', normalized_title)
    
    # Compute hash from normalized values
    content = f"{normalized_title}|{normalized_url}".encode('utf-8')
    return hashlib.sha256(content).hexdigest()


def save_articles(conn, source_id: int, articles: List[dict], source_name: str = "") -> int:
    """
    Save articles to database with duplicate checking.
    Returns number of new articles inserted.
    Handles both hash and URL duplicate constraints gracefully.
    """
    if not articles:
        log_to_database(conn, 'INFO', source_name, 'No articles to save', {'count': 0})
        return 0

    # --- detailed log: save session starting ---
    log_to_database(conn, 'INFO', source_name, 'Article save session started', {
        'total_fetched': len(articles),
        'article_urls': [a.get('url', '') for a in articles],
    })

    inserted = 0
    skipped = 0

    for article in articles:
        hash_val = compute_hash(article['title'], article['url'])

        # Each article gets its own transaction to prevent one failure from aborting the batch
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO articles (source_id, title, content, url, published_at, hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (hash) DO NOTHING
                    RETURNING id
                """, (
                    source_id,
                    article['title'],
                    article['content'],
                    article['url'],
                    article.get('published_at'),
                    hash_val
                ))

                result = cur.fetchone()
                if result:
                    inserted += 1
                    conn.commit()
                    log_to_database(conn, 'DEBUG', source_name, 'Article inserted', {
                        'article_id': result[0],
                        'title': article['title'],
                        'url': article['url'],
                        'published_at': article.get('published_at').isoformat() if article.get('published_at') else None,
                        'content_length': len(article.get('content', '')),
                        'hash': hash_val,
                    })
                else:
                    skipped += 1
                    conn.commit()
                    log_to_database(conn, 'DEBUG', source_name, 'Article skipped (duplicate hash)', {
                        'title': article['title'],
                        'url': article['url'],
                        'hash': hash_val,
                    })

        except psycopg2.IntegrityError as e:
            conn.rollback()
            skipped += 1
            log.debug(f"Skipping duplicate: {article['url']}")
            log_to_database(conn, 'DEBUG', source_name, 'Article skipped (integrity error)', {
                'url': article['url'],
                'error': str(e),
            })

        except Exception as e:
            conn.rollback()
            skipped += 1
            log.warning(f"Failed to insert article {article['url']}: {e}")
            log_to_database(conn, 'WARNING', source_name, 'Article insert failed', {
                'url': article['url'],
                'error': str(e),
                'error_type': type(e).__name__,
            })

    log.info(f"Inserted {inserted} new articles (skipped {skipped} duplicates)")

    # --- detailed log: save session summary ---
    log_to_database(conn, 'INFO', source_name, 'Article save session completed', {
        'inserted': inserted,
        'skipped_duplicates': skipped,
        'total_processed': len(articles),
    })

    return inserted


# ---------------------------------------------------------------------------
# Content Cleaning Functions
# ---------------------------------------------------------------------------

def advanced_content_cleaning(text: str) -> str:
    """
    Advanced content cleaning to remove navigation, ads, and site garbage.
    Returns only meaningful article content.
    """
    if not text:
        return ""
    
    # Apply all cleanup patterns from config
    for pattern in CONTENT_CLEANUP_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    # Remove multiple newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # Remove lines that are just navigation/menu items (very short lines)
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        # Keep lines that are substantial (more than 40 chars) or are part of paragraphs
        if len(line) > 40 or (line and cleaned_lines and len(cleaned_lines[-1]) > 40):
            cleaned_lines.append(line)
    
    text = '\n'.join(cleaned_lines)
    
    # Final cleanup
    text = clean_text(text)
    
    # Truncate to max length
    if len(text) > MAX_CONTENT_LENGTH:
        text = text[:MAX_CONTENT_LENGTH] + "..."
    
    return text


def clean_text(text: str) -> str:
    """Clean and normalize text."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def parse_date(val) -> Optional[datetime]:
    """Parse various date formats into datetime."""
    if not val:
        return None
    try:
        dt = dateutil_parser.parse(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except:
        return None


def is_recent(dt: Optional[datetime], hours: int = None) -> bool:
    """Check if datetime is within the lookback window."""
    if hours is None:
        hours = HOURS_LOOKBACK
    if not dt:
        return False
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt >= cutoff


def http_get(url: str, timeout: int = 20) -> Optional[str]:
    """Synchronous HTTP GET request."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        log.debug(f"HTTP GET failed for {url}: {e}")
    return None


# ---------------------------------------------------------------------------
# BBC Scraper
# ---------------------------------------------------------------------------

class BBCScraper:
    """Scraper for BBC News using sitemap approach."""
    
    SOURCE_NAME = "BBC"
    SITEMAP_URL = "https://www.bbc.com/sitemaps/https-index-com-news.xml"

    def __init__(self, max_articles: int = None):
        self.max_articles = max_articles if max_articles is not None else MAX_ARTICLES_PER_SOURCE
        self.articles = []
    
    def scrape(self) -> List[dict]:
        """Main scraping method."""
        if not SCRAPERS_ENABLED.get('bbc', True):
            log.info(f"{self.SOURCE_NAME} scraper is disabled in config")
            return []
        
        log.info(f"Starting {self.SOURCE_NAME} scraper...")
        
        # Get main sitemap
        if self.SITEMAP_URL != "https://www.bbc.com/sitemaps/https-index-com-news.xml":
            return []
        xml = http_get(self.SITEMAP_URL)
        if not xml:
            log.error(f"Failed to fetch {self.SOURCE_NAME} sitemap")
            return []
        
        soup = BeautifulSoup(xml, "xml")
        sitemaps = [loc.text for loc in soup.find_all("loc")]
        
        count = 0
        for sitemap_url in sitemaps:
            if count >= self.max_articles:
                break
            
            xml2 = http_get(sitemap_url)
            if not xml2:
                continue
            
            s2 = BeautifulSoup(xml2, "xml")
            urls = [loc.text for loc in s2.find_all("loc") if "/news/articles/" in loc.text]
            
            for url in urls:
                if count >= self.max_articles:
                    break
                
                article = self._scrape_article(url)
                if article:
                    self.articles.append(article)
                    count += 1
                    log.info(f"{self.SOURCE_NAME}: {count}/{self.max_articles}")
                
                time.sleep(REQUEST_DELAY)
        
        log.info(f"{self.SOURCE_NAME} completed: {len(self.articles)} articles")
        return self.articles
    
    def _scrape_article(self, url: str) -> Optional[dict]:
        """Scrape a single BBC article."""
        html = http_get(url)
        if not html:
            return None
        
        soup = BeautifulSoup(html, "lxml")
        
        # Extract title
        h1 = soup.find("h1")
        if not h1:
            return None
        title = h1.text.strip()
        
        # Extract content
        body = soup.select("article p")
        if not body:
            return None
        content = "\n".join(p.get_text(" ", strip=True) for p in body)
        
        # Clean content
        content = advanced_content_cleaning(content)
        
        if len(content) < MIN_CONTENT_LENGTH:
            return None
        
        # Extract publish date
        time_tag = soup.find("time")
        dt = None
        if time_tag and time_tag.get("datetime"):
            dt = parse_date(time_tag["datetime"])
        
        # Check if recent
        if not is_recent(dt):
            return None
        
        return {
            'title': title,
            'content': content,
            'url': url,
            'published_at': dt
        }


# ---------------------------------------------------------------------------
# AP News Scraper
# ---------------------------------------------------------------------------

class APNewsScraper:
    """Scraper for AP News using Google News RSS + Selenium for URL resolution."""
    
    SOURCE_NAME = "AP News"
    RSS_URL = "https://news.google.com/rss/search?q=site%3Aapnews.com&hl=en-US&gl=US&ceid=US%3Aen"
    
    def __init__(self, max_articles: int = None):
        self.max_articles = max_articles if max_articles is not None else MAX_ARTICLES_PER_SOURCE
        self.articles = []
        self.driver = None
    
    def scrape(self) -> List[dict]:
        """Main scraping method."""
        if not SCRAPERS_ENABLED.get('apnews', True):
            log.info(f"{self.SOURCE_NAME} scraper is disabled in config")
            return []
        
        log.info(f"Starting {self.SOURCE_NAME} scraper...")
        
        # Setup Selenium
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        
        try:
            self.driver = webdriver.Chrome(options=chrome_options)
        except Exception as e:
            log.error(f"Failed to initialize Selenium: {e}")
            return []
        
        try:
            # Fetch RSS feed
            resp = requests.get(self.RSS_URL, headers=HEADERS, timeout=15)
            feed = feedparser.parse(resp.content)
            
            log.info(f"{self.SOURCE_NAME}: Found {len(feed.entries)} RSS entries")
            
            cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_LOOKBACK)
            count = 0
            
            for entry in feed.entries:
                if count >= self.max_articles:
                    break
                
                # Check publish date
                if not hasattr(entry, 'published_parsed'):
                    continue
                
                pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if pub_date < cutoff:
                    continue
                
                # Clean title
                title = entry.title.replace(' - AP News', '')
                
                # Resolve URL with Selenium
                google_url = entry.link
                real_url = self._resolve_url(google_url)
                
                # Fetch content
                article = self._scrape_article(real_url, title, pub_date)
                if article:
                    self.articles.append(article)
                    count += 1
                    log.info(f"{self.SOURCE_NAME}: {count}/{self.max_articles}")
            
        finally:
            if self.driver:
                self.driver.quit()
        
        log.info(f"{self.SOURCE_NAME} completed: {len(self.articles)} articles")
        return self.articles
    
    def _resolve_url(self, google_url: str) -> str:
        """Use Selenium to resolve Google News redirect to actual AP News URL."""
        try:
            self.driver.get(google_url)
            time.sleep(3)  # Wait for redirect
            return self.driver.current_url
        except Exception as e:
            log.warning(f"Selenium resolution failed: {e}")
            return google_url
    
    def _scrape_article(self, url: str, title: str, pub_date: datetime) -> Optional[dict]:
        """Scrape article content from AP News."""
        if 'news.google.com' in url:
            return None
        
        try:
            time.sleep(REQUEST_DELAY)
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Try multiple selectors for AP News content
            article_body = None
            selectors = [
                ('div', {'class': 'RichTextStoryBody'}),
                ('div', {'class': 'Article__content'}),
                ('div', {'class': 'story-body'}),
                ('div', {'class': 'Page-article-body'}),
                ('div', {'itemprop': 'articleBody'}),
                ('article', {}),
            ]
            
            for tag, attrs in selectors:
                found = soup.find(tag, attrs) if attrs else soup.find(tag)
                if found:
                    article_body = found
                    break
            
            if not article_body:
                # Fallback: div with most <p> tags
                divs = soup.find_all('div')
                max_p = 0
                for div in divs:
                    p_count = len(div.find_all('p', recursive=False))
                    if p_count > max_p:
                        max_p = p_count
                        article_body = div
            
            # Extract paragraphs
            paragraphs = []
            if article_body:
                for p in article_body.find_all('p'):
                    text = p.get_text(strip=True)
                    if len(text) > 20 and not text.startswith('Copyright'):
                        paragraphs.append(text)
            
            if not paragraphs:
                return None
            
            content = '\n\n'.join(paragraphs)
            
            # Clean content
            content = advanced_content_cleaning(content)
            
            if len(content) < MIN_CONTENT_LENGTH:
                return None
            
            return {
                'title': title,
                'content': content,
                'url': url,
                'published_at': pub_date
            }
            
        except Exception as e:
            log.warning(f"Failed to scrape {url}: {e}")
            return None


# ---------------------------------------------------------------------------
# Al Jazeera Scraper (using crawl4ai)
# ---------------------------------------------------------------------------

if CRAWL4AI_AVAILABLE:
    class AlJazeeraScraper:
        """Scraper for Al Jazeera using crawl4ai index crawl."""
        
        SOURCE_NAME = "Al Jazeera"
        INDEX_URLS = [
            "https://www.aljazeera.com/economy/",
            "https://www.aljazeera.com/news/",
        ]
        
        def __init__(self, crawler, session, max_articles: int = None):
            self.crawler = crawler
            self.session = session
            self.max_articles = max_articles if max_articles is not None else MAX_ARTICLES_PER_SOURCE
            self.articles = []
        
        async def scrape(self) -> List[dict]:
            """Main async scraping method."""
            if not SCRAPERS_ENABLED.get('aljazeera', True):
                log.info(f"{self.SOURCE_NAME} scraper is disabled in config")
                return []
            
            log.info(f"Starting {self.SOURCE_NAME} scraper...")
            
            urls = await self._discover_urls()
            
            count = 0
            for url in urls[:self.max_articles]:
                log.debug(f"{self.SOURCE_NAME}: Attempting to scrape URL: {url}")
                article = await self._scrape_article(url)
                if article:
                    self.articles.append(article)
                    count += 1
                    log.info(f"{self.SOURCE_NAME}: {count}/{self.max_articles} — '{article['title'][:60]}'")
                else:
                    log.debug(f"{self.SOURCE_NAME}: No usable content extracted from {url}")
            
            log.info(f"{self.SOURCE_NAME} completed: {len(self.articles)} articles")
            return self.articles
        
        async def _discover_urls(self) -> List[str]:
            """Discover article URLs from index pages."""
            urls = set()
            
            for index_url in self.INDEX_URLS:
                try:
                    cfg = CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        word_count_threshold=10,
                        exclude_external_links=True,
                    )
                    result = await self.crawler.arun(url=index_url, config=cfg)
                    
                    if result.links:
                        for link_data in result.links.get("internal", []):
                            href = link_data.get("href", "")
                            if "/news/" in href and len(urls) < 100:
                                urls.add(href)
                
                except Exception as e:
                    log.warning(f"Failed to crawl index {index_url}: {e}")
            
            return list(urls)
        
        async def _scrape_article(self, url: str) -> Optional[dict]:
            """Scrape a single article with advanced content cleaning."""
            try:
                cfg = CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    word_count_threshold=10,
                )
                result = await self.crawler.arun(url=url, config=cfg)
                
                if not result.html:
                    return None
                
                html = result.html
                
                # Extract title
                h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
                title = ""
                if h1_match:
                    title = clean_text(re.sub(r"<[^>]+>", " ", h1_match.group(1)))
                
                # Extract date
                dt = None
                date_patterns = [
                    r'"datePublished"\s*:\s*"([^"]+)"',
                    r'<time[^>]+datetime="([^"]+)"',
                ]
                for pat in date_patterns:
                    m = re.search(pat, html)
                    if m:
                        dt = parse_date(m.group(1))
                        if dt:
                            break
                
                # Check if recent
                if not is_recent(dt):
                    return None
                
                # Extract content from markdown
                md_text = ""
                if result.markdown:
                    md = result.markdown
                    if isinstance(md, str):
                        md_text = md
                    else:
                        md_text = getattr(md, "fit_markdown", "") or getattr(md, "raw_markdown", "")
                
                # Clean markdown - remove images, links, headers
                content = re.sub(r"!\[.*?\]\(.*?\)", "", md_text)
                content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
                content = re.sub(r"#{1,6}\s*", "", content)

                # --- Al Jazeera-specific pre-cleaning ---
                content = _clean_aljazeera_filler(content)

                # Apply advanced content cleaning
                content = advanced_content_cleaning(content)
                
                if not title or len(content) < MIN_CONTENT_LENGTH:
                    return None
                
                return {
                    'title': title,
                    'content': content,
                    'url': url,
                    'published_at': dt
                }
                
            except Exception as e:
                log.warning(f"Failed to scrape {url}: {e}")
                return None


def _clean_aljazeera_filler(text: str) -> str:
    """
    Remove Al Jazeera-specific filler that appears around article content:
    - "REPORTER'S NOTEBOOK" / "INTERACTIVE" / "IN PICTURES" section labels
    - Italic sub-headings that are summary blurbs (e.g. _From Lviv..._)
    - Bullet-list navigation links ("* How the US left Ukraine…")
    - "By <Author>" / author credit lines
    - "Your browser does not support the audio element." boilerplate
    - Audio/video widget placeholders (audio-rewind, play Live)
    - "Al Jazeera Centre for Public Liberties…" footer blocks
    - "and updates based on your interests." / "when big stories happen." newsletter prompts
    - Markdown italic blurbs used as article teasers
    - Repeated section separators (lines of asterisks/dashes)
    """
    # Reporter's Notebook / section labels (all-caps labels on their own line)
    text = re.sub(
        r"(?m)^[ \t]*(REPORTER'S NOTEBOOK|INTERACTIVE|IN PICTURES|ANALYSIS|OPINION|LIVE|EXPLAINER|FACT CHECK|INVESTIGATION|PODCAST|VIDEO|GALLERY)\s*$",
        "", text
    )

    # Italic blurb lines used as sub-headings (e.g. _From Lviv in the west…_)
    text = re.sub(r"_[^_\n]{10,200}_", "", text)

    # Bullet-point navigation links — lines starting with "* " that look like
    # question-style or link-style headlines (contain "How", "Will", "What", "Why", etc.)
    text = re.sub(
        r"(?m)^\*\s+(How|Will|What|Why|Where|Who|When|Can|Is|Are|Was|Were|Has|Have|Did|Should|Could|Would|'[A-Z]|[A-Z]{2,})[^\n]*$",
        "", text
    )

    # Generic bullet navigation lines that are short (< 100 chars) — likely related-article links
    text = re.sub(r"(?m)^\*\s{1,3}(?![\*\-]).{5,99}$", "", text)

    # "By <Author Name>" credit lines
    text = re.sub(r"(?m)^By\s+[A-Z][a-zA-Z\s\-']{2,50}$", "", text)

    # Author avatar / profile link patterns left by markdown conversion
    text = re.sub(r"\[\]\(https?://www\.aljazeera\.com/author/[^\)]+\)", "", text)

    # "Al Jazeera Centre for Public Liberties & Human Rights" footer
    text = re.sub(r"Al\s+Jazeera\s+Centre\s+for\s+Public\s+Liberties[^\n]*", "", text)

    # "Al Jazeera Forum" label
    text = re.sub(r"(?m)^Al\s+Jazeera\s+Forum\s*$", "", text)

    # Browser / audio element boilerplate
    text = re.sub(r"Your\s+browser\s+does\s+not\s+support\s+the\s+audio\s+element\.", "", text)
    text = re.sub(r"audio-rewind", "", text)

    # Newsletter subscription prompts embedded in article text
    text = re.sub(r"and\s+updates\s+based\s+on\s+your\s+interests\.", "", text)
    text = re.sub(r"when\s+big\s+stories\s+happen\.", "", text)
    text = re.sub(r"Be\s+among\s+the\s+first\s+to\s+know[^\n]*", "", text)

    # Lines that are only asterisks / dashes / underscores (section separators)
    text = re.sub(r"(?m)^[\*\-_]{2,}\s*$", "", text)

    # Trailing "* * * * *" separators (5-asterisk AJ footer pattern)
    text = re.sub(r"(\*\s*){3,}", "", text)

    # Caption-style lines: e.g. "A woman wipes away her tears [Photographer/Agency]"
    text = re.sub(r"\[[A-Z][^\]\n]{5,60}/[^\]\n]{3,40}\]", "", text)

    # Image alt-text remnants like "[File: AP]" or "[Photo: Reuters]"
    text = re.sub(r"\[(?:File|Photo|Image|Video|AP|AFP|Reuters|Getty)[^\]]{0,60}\]", "", text, flags=re.IGNORECASE)

    return text


# ---------------------------------------------------------------------------
# Nordnet Scraper (using crawl4ai)
# ---------------------------------------------------------------------------

if CRAWL4AI_AVAILABLE:
    class NordnetScraper:
        """Scraper for Nordnet press releases."""
        
        SOURCE_NAME = "Nordnet"
        
        # Enhanced RSS / Atom feeds for Nordnet AB press releases
        RSS_URLS = [
            "https://www.globenewswire.com/RssFeed/company/nordnet",
            "https://www.nordnetab.com/feed",
            "https://www.nordnetab.com/en/feed",
            "https://nordnetab.com/press/press-releases/feed/",
        ]
        
        # Plain HTML index pages to scrape for links
        INDEX_URLS = [
            "https://www.nordnetab.com/en/news",
            "https://www.nordnetab.com/en/press-releases",
            "https://nordnetab.com/press/press-releases/",
            "https://www.nordnet.se/blogg",
            "https://www.nordnet.no/blogg",
            "https://www.nordnet.dk/blog",
            "https://www.nordnet.fi/blogi",
        ]
        
        # Pattern to match actual blog post URLs
        _LINK_RE = re.compile(
            r'href="((?:https?://(?:www\.)?nordnet(?:ab)?\.(?:com|se|no|dk|fi))?'
            r'/(?:en/(?:news|press)|blogg?|blogi|press/press-releases)/[^"?#]{10,})"',
            re.IGNORECASE,
        )
        
        def __init__(self, crawler, session, max_articles: int = None):
            self.crawler = crawler
            self.session = session
            self.max_articles = max_articles if max_articles is not None else MAX_ARTICLES_PER_SOURCE
            self.articles = []
            self._meta = {}
            self.hours = HOURS_LOOKBACK
        
        def _is_valid_article_url(self, url: str) -> bool:
            """
            Filter out category pages, author pages, and other non-article URLs.
            """
            parsed = urlparse(url.lower())
            path = parsed.path.rstrip("/")
            
            # Exclude obvious non-article patterns
            exclude_patterns = [
                r'/kategori/', r'/category/', r'/author/', r'/tag/',
                r'/page/', r'/arkiv/', r'/archive/', r'/search',
            ]
            
            for pattern in exclude_patterns:
                if re.search(pattern, path):
                    return False
            
            # Must have reasonable depth
            path_parts = [p for p in path.split('/') if p]
            if len(path_parts) < 2:
                return False
            
            # Prefer URLs with dates
            if re.search(r'/\d{4}[-/]\d{1,2}[-/]\d{1,2}/', path):
                return True
            
            # Require substantial slug
            last_segment = path_parts[-1]
            if len(last_segment) >= 15 and '-' in last_segment:
                return True
            
            return False
        
        async def scrape(self) -> List[dict]:
            """Main async scraping method."""
            if not SCRAPERS_ENABLED.get('nordnet', True):
                log.info(f"{self.SOURCE_NAME} scraper is disabled in config")
                return []
            
            log.info(f"Starting {self.SOURCE_NAME} scraper...")
            
            urls = await self._discover_urls()
            
            count = 0
            for url in urls[:self.max_articles]:
                article = await self._scrape_article(url)
                if article:
                    self.articles.append(article)
                    count += 1
                    log.info(f"{self.SOURCE_NAME}: {count}/{self.max_articles}")
            
            log.info(f"{self.SOURCE_NAME} completed: {len(self.articles)} articles")
            return self.articles
        
        async def _discover_urls(self) -> List[str]:
            """Discover URLs from RSS and index pages."""
            urls = []
            seen = set()
            
            # Parse RSS feeds
            for rss_url in self.RSS_URLS:
                try:
                    async with self.session.get(rss_url, headers=HEADERS) as resp:
                        if resp.status == 200:
                            xml_text = await resp.text()
                            root = ET.fromstring(xml_text)
                            
                            for item in root.findall(".//item"):
                                link_el = item.find("link")
                                pub_el = item.find("pubDate")
                                title_el = item.find("title")
                                desc_el = item.find("description")
                                
                                if link_el is not None and link_el.text:
                                    url = link_el.text.strip()
                                    pub_date = parse_date(pub_el.text) if pub_el is not None else None
                                    title = title_el.text if title_el is not None else ""
                                    desc = desc_el.text if desc_el is not None else ""
                                    
                                    if is_recent(pub_date) and url not in seen:
                                        seen.add(url)
                                        urls.append(url)
                                        self._meta[url] = (title, pub_date, desc)
                
                except Exception as e:
                    log.warning(f"[{self.SOURCE_NAME}] Failed to parse RSS {rss_url}: {e}")
            
            # HTML index pages
            for index_url in self.INDEX_URLS:
                try:
                    async with self.session.get(index_url, headers=HEADERS) as resp:
                        if resp.status == 200:
                            html_text = await resp.text()
                            base_match = re.match(r"https?://[^/]+", index_url)
                            base = base_match.group(0) if base_match else ""
                            
                            for href in self._LINK_RE.findall(html_text):
                                full = href if href.startswith('http') else urljoin(base, href)
                                full = full.rstrip("/")
                                
                                if full not in seen and self._is_valid_article_url(full):
                                    seen.add(full)
                                    urls.append(full)
                                    self._meta[full] = ("", None, "")
                
                except Exception as e:
                    log.warning(f"[{self.SOURCE_NAME}] Failed to scrape index {index_url}: {e}")
            
            log.info(f"[{self.SOURCE_NAME}] {len(urls)} candidate URLs found")
            return urls[:40]
        
        async def _scrape_article(self, url: str) -> Optional[dict]:
            """Scrape a single article."""
            meta = self._meta.get(url, ("", None, ""))
            
            # Check RSS date first
            if meta[1] and not is_recent(meta[1]):
                return None
            
            try:
                cfg = CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    word_count_threshold=10,
                )
                result = await self.crawler.arun(url=url, config=cfg)
                
                if not result.html:
                    # Fallback to RSS metadata
                    if meta[0] and meta[2] and is_recent(meta[1]):
                        return {
                            'title': meta[0],
                            'content': advanced_content_cleaning(meta[2]),
                            'url': url,
                            'published_at': meta[1]
                        }
                    return None
                
                html = result.html
                
                # Extract title
                title = meta[0]
                if not title:
                    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
                    if h1_match:
                        title = clean_text(re.sub(r"<[^>]+>", " ", h1_match.group(1)))
                
                # Extract date
                dt = meta[1]
                if not dt:
                    date_patterns = [
                        r'"datePublished"\s*:\s*"([^"]+)"',
                        r'<time[^>]+datetime="([^"]+)"',
                    ]
                    for pat in date_patterns:
                        m = re.search(pat, html)
                        if m:
                            dt = parse_date(m.group(1))
                            if dt:
                                break
                
                # Extract content from markdown
                md_text = ""
                if result.markdown:
                    md = result.markdown
                    if isinstance(md, str):
                        md_text = md
                    else:
                        md_text = getattr(md, "fit_markdown", "") or getattr(md, "raw_markdown", "")
                
                # Clean markdown
                content = re.sub(r"!\[.*?\]\(.*?\)", "", md_text)
                content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
                content = re.sub(r"#{1,6}\s*", "", content)
                
                # Apply advanced cleaning
                content = advanced_content_cleaning(content)
                
                if not content or len(content) < MIN_CONTENT_LENGTH:
                    content = advanced_content_cleaning(meta[2]) if meta[2] else ""
                
                if not title or len(content) < MIN_CONTENT_LENGTH:
                    return None
                
                # Date check
                if not is_recent(dt):
                    return None
                
                return {
                    'title': title,
                    'content': content,
                    'url': url,
                    'published_at': dt
                }
                
            except Exception as e:
                log.warning(f"[{self.SOURCE_NAME}] Failed to scrape {url}: {e}")
                # Fallback to RSS metadata
                if meta[0] and meta[2] and is_recent(meta[1]):
                    return {
                        'title': meta[0],
                        'content': advanced_content_cleaning(meta[2]),
                        'url': url,
                        'published_at': meta[1]
                    }
                return None


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

async def run_async_scrapers(source_mapping: dict, conn):
    """Run async scrapers (Al Jazeera, Nordnet) with crawl4ai."""
    if not CRAWL4AI_AVAILABLE:
        log.warning("Skipping async scrapers (crawl4ai not available)")
        return
    
    browser_cfg = BrowserConfig(
        headless=True,
        browser_type="chromium",
        viewport_width=1280,
        viewport_height=800,
        java_script_enabled=True,
        user_agent=HEADERS["User-Agent"],
        verbose=False,
    )
    
    connector = aiohttp.TCPConnector(ssl=False, limit=10)
    async with aiohttp.ClientSession(connector=connector, headers=HEADERS) as session:
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            # Al Jazeera
            if SCRAPERS_ENABLED.get('aljazeera', True):
                log_to_database(conn, 'INFO', 'Al Jazeera', 'Scraper started', {
                    'max_articles': MAX_ARTICLES_PER_SOURCE,
                    'hours_lookback': HOURS_LOOKBACK,
                    'index_urls': AlJazeeraScraper.INDEX_URLS,
                })
                aj_scraper = AlJazeeraScraper(crawler, session)
                aj_articles = await aj_scraper.scrape()
                log_to_database(conn, 'INFO', 'Al Jazeera', 'Scraper finished', {
                    'articles_fetched': len(aj_articles),
                })
                save_articles(conn, source_mapping['Al Jazeera'], aj_articles, 'Al Jazeera')
            else:
                log_to_database(conn, 'INFO', 'Al Jazeera', 'Scraper disabled', {})
                print("Al Jazeera Scraper disabled in config.yaml.")
            
            # Nordnet
            if SCRAPERS_ENABLED.get('nordnet', True):
                log_to_database(conn, 'INFO', 'Nordnet', 'Scraper started', {
                    'max_articles': MAX_ARTICLES_PER_SOURCE,
                    'hours_lookback': HOURS_LOOKBACK,
                })
                nn_scraper = NordnetScraper(crawler, session)
                nn_articles = await nn_scraper.scrape()
                log_to_database(conn, 'INFO', 'Nordnet', 'Scraper finished', {
                    'articles_fetched': len(nn_articles),
                })
                save_articles(conn, source_mapping['Nordnet'], nn_articles, 'Nordnet')
            else:
                log_to_database(conn, 'INFO', 'Nordnet', 'Scraper disabled', {})
                print("Nordnet Scraper disabled in config.yaml.")


def run_sync_scrapers(source_mapping: dict, conn):
    """Run synchronous scrapers (BBC, AP News)."""
    # BBC
    if SCRAPERS_ENABLED.get('bbc', True):
        log_to_database(conn, 'INFO', 'BBC', 'Scraper started', {
            'max_articles': MAX_ARTICLES_PER_SOURCE,
            'hours_lookback': HOURS_LOOKBACK,
            'sitemap_url': BBCScraper.SITEMAP_URL,
        })
        bbc_scraper = BBCScraper()
        bbc_articles = bbc_scraper.scrape()
        log_to_database(conn, 'INFO', 'BBC', 'Scraper finished', {
            'articles_fetched': len(bbc_articles),
        })
        save_articles(conn, source_mapping['BBC'], bbc_articles, 'BBC')
    else:
        log_to_database(conn, 'INFO', 'BBC', 'Scraper disabled', {})
        print("BBC Scraper disabled in config.yaml.")
    
    # AP News
    if SCRAPERS_ENABLED.get('apnews', True):
        log_to_database(conn, 'INFO', 'AP News', 'Scraper started', {
            'max_articles': MAX_ARTICLES_PER_SOURCE,
            'hours_lookback': HOURS_LOOKBACK,
            'rss_url': APNewsScraper.RSS_URL,
        })
        ap_scraper = APNewsScraper()
        ap_articles = ap_scraper.scrape()
        log_to_database(conn, 'INFO', 'AP News', 'Scraper finished', {
            'articles_fetched': len(ap_articles),
        })
        save_articles(conn, source_mapping['AP News'], ap_articles, 'AP News')
    else:
        log_to_database(conn, 'INFO', 'AP News', 'Scraper disabled', {})
        print("AP News Scraper disabled in config.yaml.")


def main():
    """Main entry point."""

    # ------------------------------------------------------------------
    # 1. Load .env first so os.getenv picks up the values below
    # ------------------------------------------------------------------
    from pathlib import Path
    from dotenv import load_dotenv

    # __file__ is the path to main.py. .parent is phase1/. .parent.parent is the root.
    root_path = Path(__file__).resolve().parent.parent
    env_path = root_path / '.env'

    if not env_path.exists():
        print(f"WARNING: .env file not found at {env_path}. Using values from config.yaml as-is.")
    else:
        load_dotenv(dotenv_path=env_path)
        print(f"LOADED: Environment from {env_path}")

    # ------------------------------------------------------------------
    # 2. Load YAML config and apply it to module-level globals
    # ------------------------------------------------------------------
    try:
        raw_cfg = load_config("config.yaml")
        print("Loaded configuration from config.yaml")
    except FileNotFoundError:
        print("ERROR: config.yaml not found. Cannot continue.")
        return

    # Overlay environment-variable credentials onto the parsed config
    db_section = raw_cfg.setdefault("database", {})
    db_section["name"]     = os.getenv("DB_NAME",     db_section.get("name",     "scraped_data"))
    db_section["user"]     = os.getenv("DB_USER",     db_section.get("user",     "postgres"))
    db_section["password"] = os.getenv("DB_PASSWORD", db_section.get("password", "root"))
    db_section["host"]     = os.getenv("DB_HOST",     db_section.get("host",     "localhost"))
    db_section["port"]     = os.getenv("DB_PORT",     db_section.get("port",     5432))
    if os.getenv("DB_SSLMODE"):
        db_section["sslmode"] = os.getenv("DB_SSLMODE")

    # Overlay API keys from environment (for Phase 1 apis if provided)
    apis = raw_cfg.get("apis", {})
    env_api_map = {
        "reuters":    ["REUTERS_API_KEY", "REUTERS_API_SECRET", "REUTERS_BASE_URL"],
        "morningstar": ["MORNINGSTAR_API_KEY", "MORNINGSTAR_BASE_URL"],
        "bloomberg":  ["BLOOMBERG_API_KEY", "BLOOMBERG_API_SECRET", "BLOOMBERG_BASE_URL"],
        "x_twitter":  ["X_API_KEY", "X_API_SECRET", "X_BEARER_TOKEN", "X_BASE_URL"],
    }
    for api_name, env_keys in env_api_map.items():
        api_cfg = apis.get(api_name, {})
        for env_key in env_keys:
            val = os.getenv(env_key)
            if val:
                yaml_key = env_key.lower().replace(f"{api_name}_", "").replace("reuters_", "").replace("morningstar_", "").replace("bloomberg_", "").replace("x_", "")
                api_cfg[yaml_key] = val

    apply_config(raw_cfg)

    # ------------------------------------------------------------------
    # 3. Validate credentials
    # ------------------------------------------------------------------
    if DB_CONFIG.get("password") in ("root", "password", "admin", ""):
        print("WARNING: Using a weak or default database password. Set DB_PASSWORD in .env.")

    # ------------------------------------------------------------------
    # 4. Configure logging (requires LOGGING_CONFIG to be populated)
    # ------------------------------------------------------------------
    configure_logging()

    start_time = datetime.now(timezone.utc)
    
    log.info("=" * 60)
    log.info("Financial Market Knowledge Scraping Engine - Phase 1")
    log.info("=" * 60)
    log.info(f"Start time: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info(f"Lookback window: {HOURS_LOOKBACK} hours")
    log.info(f"Max articles per source: {MAX_ARTICLES_PER_SOURCE}")
    
    # Connect to database
    try:
        conn = get_db_connection()
        log.info("Connected to PostgreSQL database")
        
        # Log run start to database
        log_to_database(conn, 'INFO', 'SYSTEM', 'Scraping run started', {
            'start_time': start_time.isoformat(),
            'config': {
                'hours_lookback': HOURS_LOOKBACK,
                'max_articles': MAX_ARTICLES_PER_SOURCE,
            }
        })
        
    except Exception as e:
        log.error(f"Failed to connect to database: {e}")
        log.error("Please check your DB_CONFIG settings")
        return
    
    try:
        # Initialize sources
        source_mapping = init_sources(conn)
        
        # Run synchronous scrapers
        log.info("\n" + "=" * 60)
        log.info("Running synchronous scrapers (BBC, AP News)")
        log.info("=" * 60)
        run_sync_scrapers(source_mapping, conn)
        
        # Run async scrapers
        log.info("\n" + "=" * 60)
        log.info("Running async scrapers (Al Jazeera, Nordnet)")
        log.info("=" * 60)
        asyncio.run(run_async_scrapers(source_mapping, conn))
        
        # Get final stats
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.name, COUNT(a.id) as count
                FROM sources s
                LEFT JOIN articles a ON s.id = a.source_id
                WHERE a.scraped_at >= NOW() - INTERVAL '1 hour'
                GROUP BY s.name
                ORDER BY count DESC
            """)
            results = cur.fetchall()
            
            log.info("\n" + "=" * 60)
            log.info("Scraping Summary (last hour)")
            log.info("=" * 60)
            total = 0
            for source, count in results:
                log.info(f"{source:15s}: {count:3d} articles")
                total += count
            log.info("-" * 60)
            log.info(f"{'TOTAL':15s}: {total:3d} articles")
            log.info("=" * 60)
            
            end_time = datetime.now(timezone.utc)
            duration = (end_time - start_time).total_seconds()
            log.info(f"Duration: {duration:.1f} seconds")
            
            # Log completion to database
            log_to_database(conn, 'INFO', 'SYSTEM', 'Scraping run completed', {
                'end_time': end_time.isoformat(),
                'duration_seconds': duration,
                'total_articles': total,
                'by_source': dict(results)
            })
        
    except Exception as e:
        log.error(f"Scraping failed: {e}", exc_info=True)
        log_to_database(conn, 'ERROR', 'SYSTEM', f'Scraping failed: {str(e)}', {
            'error': str(e),
            'type': type(e).__name__
        })
    finally:
        conn.close()
        log.info("Database connection closed")


if __name__ == "__main__":
    main()