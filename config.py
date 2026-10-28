# -*- coding: utf-8 -*-
"""
全局配置：你平时只需要改这个文件里的关键词和滚动次数。
"""

# ============ 搜索关键词 ============
# 工具会逐个关键词去搜。分两层：精准词（搜索结果直接入库）和宽泛词（入库前要过检测器）。
# 精准词：被搜出来本身就说明跟提示词强相关
KEYWORDS_PRECISE = [
    "Midjourney prompt 分享",
    "Stable Diffusion prompt",
    "ChatGPT 提示词 模板",
    "即梦 提示词 分享",
    "可灵 提示词",
    "AI绘画 prompt",
    "AI视频 提示词 教程",
    "prompt engineering",
    "DALL-E prompt",
    "Sora prompt",
]
# 宽泛词：搜索结果鱼龙混杂，入库前必须过提示词结构检测
KEYWORDS_BROAD = [
    "AI提示词",
    "提示词 分享",
    "prompt 分享",
    "AI 咒语",
    "提示词工程",
]

# 合并成实际搜索列表
KEYWORDS = KEYWORDS_PRECISE + KEYWORDS_BROAD

# ============ 抓取强度 ============
# 每个关键词最多向下滚动次数（次数越多抓得越多，越慢、越容易触发风控）
SCROLL_TIMES = 3  # 临时调小，目标50条精选
# 每次滚动后等待秒数（让内容加载出来）
SCROLL_WAIT_SECONDS = 2.5
# 两个关键词之间的间隔秒数（降低被风控概率）
KEYWORD_INTERVAL_SECONDS = 5

# ============ 入库前结构检测 ============
# True=开启(推荐)：内容必须命中至少一条提示词结构特征才入库，把闲聊/广告拦在外面
# False=关闭：所有搜到的全入库
PRE_FILTER_ENABLED = True

# 是否为精准关键词（内容直接入库，不过检测器）
PRECISE_KEYWORDS = set(KEYWORDS_PRECISE)

# ============ X 搜索过滤 ============
X_SEARCH_TAB = "live"  # live=最新, top=热门
ONLY_CHINESE = False

# ============ 文件路径（一般不用改） ============
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "prompts.db")
EXPORT_DIR = os.path.join(BASE_DIR, "export")
# 浏览器登录态保存目录（保存后下次免登录）
BROWSER_STATE_DIR = os.path.join(BASE_DIR, "browser_state")
