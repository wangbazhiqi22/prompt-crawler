"""Local-only inference settings. No cloud key or automatic cloud fallback."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AI_DIR = ROOT / '.local-ai'
BASE_URL = 'http://127.0.0.1:11435'
MODEL_NAME = 'prompt-crawler-qwen3.5-2b'
ANALYZE_MODEL = MODEL_NAME
OCR_MODEL = MODEL_NAME
REQUEST_TIMEOUT = 180
MAX_OUTPUT_TOKENS = 1600
MAX_IMAGE_EDGE = 768
CONTEXT_SIZE = 8192
CPU_THREADS = 6
IDLE_SECONDS = 120
