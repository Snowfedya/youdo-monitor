"""Configuration settings for YouDo Monitor service."""
import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "state.json"
COOKIES_FILE = DATA_DIR / "cookies.json"
LOG_FILE = DATA_DIR / "run.log"

# Chrome / Playwright settings
PLAYWRIGHT_CHROME_BIN = "/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
PLAYWRIGHT_PROFILE_DIR = "/root/.cache/youdo-pw-profile"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# YouDo URL & API Endpoints
YOUDO_START_URL = "https://youdo.com/tasks-all-opened-all"
YOUDO_FEED_ENDPOINT = "/api/tasks/tasks/"
YOUDO_TASK_ENDPOINT = "/api/tasks/task/{task_id}/"

# Target Categories (strictly webdevelopment as requested: NO computerhelp, NO virtualassistant)
CATEGORIES = ["webdevelopment"]
ONLY_VIRTUAL = True
FEED_PAGES_TO_FETCH = 6

# Filtering Thresholds
MAX_OFFERS_LIMIT = 30
MIN_DESCRIPTION_LENGTH = 15

# Telegram Notification settings
# `telegram` = home channel (Fiasko_Hermes_agents group). Delivery via `hermes send` — no LLM needed.
TELEGRAM_TARGET = "telegram"
TELEGRAM_CHAT_ID = "-1003901934842"
