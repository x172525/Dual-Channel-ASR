"""
配置文件
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("asr_ws_bridge.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

FUNASR_SERVERS = [
    {"host": "192.168.0.116", "port": 10096},
]

UDP_LISTEN_PORT = 8850
WS_SERVER_PORT = 8080
RESAMPLE_MODE = "linear"

# 热词设置：权重尽量不要超过30
HOTWORDS = {"热词1": 30, "热词2": 25}
