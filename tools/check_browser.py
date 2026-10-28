"""Check the browser used by the app without reading a personal browser profile."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_support import launch_browser
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = launch_browser(p.chromium, headless=True)
    browser.close()
print('Browser is available.')
