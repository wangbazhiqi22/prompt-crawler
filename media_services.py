"""Self-contained downloads and local speech recognition (no paid API)."""
import hashlib
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import requests

ROOT = Path(__file__).resolve().parent
_ASR_LOCK = threading.Lock()


class MultiPlatformDownloadService:
    def download_douyin_from_resolved_media(self, share_url, media_url):
        if not media_url.startswith(('https://', 'http://')):
            return self.download_from_text(share_url)
        folder = ROOT / 'douyin_images' / hashlib.sha256(share_url.encode()).hexdigest()[:16]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / 'video.mp4'
        partial = folder / 'video.mp4.part'
        try:
            with requests.get(media_url, headers={'User-Agent': 'Mozilla/5.0',
                    'Referer': 'https://www.douyin.com/'}, stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/' in content_type or 'json' in content_type:
                    raise RuntimeError('视频地址返回了网页，请重新解析分享链接。')
                with partial.open('wb') as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
            if partial.stat().st_size == 0:
                raise RuntimeError('下载的视频为空。')
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return SimpleNamespace(local_path=str(target), title='', author='',
                               share_url=share_url, resolved_url=share_url)

    def download_from_text(self, text):
        from share_input import first_url
        import yt_dlp
        url = first_url(text) or text.strip()
        folder = ROOT / 'douyin_images' / hashlib.sha256(url.encode()).hexdigest()[:16]
        folder.mkdir(parents=True, exist_ok=True)
        options = {'outtmpl': str(folder / 'video.%(ext)s'), 'noplaylist': True,
                   'format': 'bestvideo+bestaudio/best', 'merge_output_format': 'mp4',
                   'socket_timeout': 30, 'retries': 2, 'quiet': True}
        cookie_file = os.environ.get('PROMPT_CRAWLER_COOKIE_FILE')
        if cookie_file:
            options['cookiefile'] = cookie_file
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            filename = Path(downloader.prepare_filename(info))
            merged = filename.with_suffix('.mp4')
            filename = merged if merged.exists() else filename
        if not filename.is_file():
            raise RuntimeError('下载器未生成视频文件。')
        return SimpleNamespace(local_path=str(filename), title=info.get('title', ''),
                               author=info.get('uploader', ''), share_url=url,
                               resolved_url=info.get('webpage_url', url))


class DouyinDownloadService:
    def _resolve_share_url_locally(self, url):
        from crawler_douyin import _parse_douyin_video_browser
        result = _parse_douyin_video_browser(url)
        if not result:
            raise RuntimeError('未读取到视频信息，可能需要登录或平台限制了访问。')
        return result

    def _download_via_browser_fallback(self, url):
        info = self._resolve_share_url_locally(url)
        media_url = info.get('video_url', '')
        if not media_url.startswith(('http://', 'https://')):
            raise RuntimeError('页面未提供可下载的视频地址。')
        result = MultiPlatformDownloadService().download_douyin_from_resolved_media(url, media_url)
        result.title = info.get('desc', '').split('\n')[0][:100]
        result.author = info.get('nickname', '')
        return result


def _srt_timestamp(milliseconds):
    seconds, millis = divmod(max(0, int(milliseconds)), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02},{millis:03}'


class AudioTranscriptionService:
    def transcribe_source(self, source):
        from faster_whisper import WhisperModel
        from text_tools import to_simplified
        source = Path(source).resolve()
        audio = source.with_name(source.stem + '_asr.wav')
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-i', str(source), '-vn', '-ar',
                        '16000', '-ac', '1', str(audio)], check=True,
                       capture_output=True, timeout=900,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # Serialize CPU inference; release the model after each job to keep RAM available.
        with _ASR_LOCK:
            model = WhisperModel(os.environ.get('PROMPT_CRAWLER_ASR_MODEL', 'base'),
                device='cpu', compute_type='int8', cpu_threads=4,
                download_root=str(ROOT / '.local-ai' / 'whisper'))
            language = os.environ.get('PROMPT_CRAWLER_ASR_LANGUAGE') or None
            detected, _ = model.transcribe(str(audio), language=language,
                                           vad_filter=True, beam_size=3)
            segments = [SimpleNamespace(start_ms=round(s.start * 1000),
                end_ms=round(s.end * 1000), text=to_simplified(s.text.strip()),
                speaker_id=0) for s in detected]
        srt = '\n\n'.join(f'{i}\n{_srt_timestamp(s.start_ms)} --> '
            f'{_srt_timestamp(s.end_ms)}\n{s.text}' for i, s in enumerate(segments, 1))
        return SimpleNamespace(audio_path=str(audio)), SimpleNamespace(
            text='\n'.join(s.text for s in segments), segments=segments, srt_text=srt)
