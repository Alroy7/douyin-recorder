# -*- coding: utf-8 -*-
"""冒烟测试：mock 协议层 + 本地 HTTP-FLV 测试源。"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.checker import LiveChecker
from src.mock_api import configure

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')

cfg = Config.load(ROOT / 'tests' / 'tmp' / 'test_config.json')
configure(ffmpeg_path=cfg.get_str('ffmpeg_path'), live_seconds=15)

checker = LiveChecker(cfg, use_mock=True)
print('api:', checker.describe())
st = checker.check('1234567890123')
print('is_live:', st.is_live)
print('room_id:', st.room_id)
print('title:', st.title)
print('streams:', st.streams)
url = checker.choose_url(st)
print('chosen url:', url)
res = checker.resolve('复制打开抖音，看看【测试主播】的直播 https://live.douyin.com/1234567890123')
print('resolve:', res)
assert st.is_live and url, 'mock status/url failed'
assert url.startswith('http://127.0.0.1:'), url
print('SMOKE OK')