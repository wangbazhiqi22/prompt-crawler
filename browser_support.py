"""Use bundled Chromium, or an installed Chrome/Edge with an isolated profile."""
import os
from pathlib import Path


def launch_browser(browser_type, *, persistent=False, **options):
    launch = browser_type.launch_persistent_context if persistent else browser_type.launch
    channel = os.environ.get('PROMPT_CRAWLER_BROWSER_CHANNEL')
    if channel:
        return launch(channel=channel, **options)
    if Path(browser_type.executable_path).is_file():
        return launch(**options)
    for installed_channel in ('chrome', 'msedge'):
        try:
            return launch(channel=installed_channel, **options)
        except Exception:
            continue
    raise RuntimeError('未找到可用浏览器。请安装 Chrome/Edge，或运行 python -m playwright install chromium。')
