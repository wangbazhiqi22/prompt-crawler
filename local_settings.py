"""Generate installation-specific credentials; never commit the data directory."""
import os
import secrets
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'


def session_secret():
    configured = os.environ.get('PROMPT_CRAWLER_SECRET')
    if configured:
        return configured
    DATA.mkdir(exist_ok=True)
    path = DATA / 'session-secret.txt'
    try:
        with path.open('x', encoding='utf-8') as output:
            output.write(secrets.token_hex(32))
        path.chmod(0o600)
    except FileExistsError:
        pass
    return path.read_text(encoding='utf-8').strip()


def initial_admin_password():
    password = os.environ.get('PROMPT_CRAWLER_ADMIN_PASSWORD')
    if password:
        if len(password) < 12:
            raise ValueError('PROMPT_CRAWLER_ADMIN_PASSWORD 至少需要 12 个字符。')
        return password
    DATA.mkdir(exist_ok=True)
    password = secrets.token_urlsafe(18)
    path = DATA / 'first-run-admin.txt'
    path.write_text(f'Username: admin\nPassword: {password}\n'
                    'Keep this file private. Delete it after saving your password.\n', encoding='utf-8')
    path.chmod(0o600)
    print('首次启动已生成管理员密码，请打开 data/first-run-admin.txt 查看。')
    return password
