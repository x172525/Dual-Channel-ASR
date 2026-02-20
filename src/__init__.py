"""
双通道实时语音识别服务
"""

from src.funasr_client import FunASRClient
from src.client_session import ClientSession
from src.audio_router import (
    AudioRouterManager,
    audio_router,
    ConnectionMonitor,
    connection_monitor,
)
from src.tasks import (
    udp_server_task,
    funasr_result_forwarder,
    test_funasr_result_forwarder,
)
from src.routes import create_app

__version__ = "1.0"

__all__ = [
    "FunASRClient",
    "ClientSession",
    "AudioRouterManager",
    "audio_router",
    "ConnectionMonitor",
    "connection_monitor",
    "udp_server_task",
    "funasr_result_forwarder",
    "test_funasr_result_forwarder",
    "create_app",
]
