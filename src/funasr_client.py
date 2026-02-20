"""
FunASR客户端模块
"""

import json
import random
import uuid
import time
import asyncio
import logging
from typing import Optional
import websockets
import numpy as np
import librosa
import concurrent.futures
import functools

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FUNASR_SERVERS, HOTWORDS, logger

resample_thread_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=16, thread_name_prefix="resample_"
)


async def run_in_threadpool(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        resample_thread_pool, functools.partial(func, *args)
    )


class FunASRClient:
    RESAMPLE_MODE = "linear"
    AUDIO_ENHANCE_CONFIG = {
        "enable_enhance": True,
        "gain_factor": 2.0,
        "noise_threshold_db": -40,
        "enable_noise_reduction": True,
        "smooth_factor": 0.1,
        "min_amplitude": 0.01,
    }

    def __init__(
        self,
        host: str,
        port: int = 10096,
        client_type: str = "unknown",
        owner_session_id: str = None,
    ):
        self.host = host
        self.port = port
        self.client_type = client_type
        self.websocket = None
        self.connected = False
        self.client_id = str(uuid.uuid4())[:8]
        self.packets_sent = 0
        self.results_received = 0
        self.last_sent_time = 0
        self.audio_bytes_sent = 0
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._reconnect_delay = 1.0
        self._last_reconnect_time = 0
        self.resample_mode = FunASRClient.RESAMPLE_MODE

        self.should_reconnect = True
        self.closing = False
        self.owner_session_id = owner_session_id
        self.session_shutdown_event = asyncio.Event()
        logger.debug(f"初始化FunASRClient: {self.client_id}, type: {self.client_type}")

    async def connect(self):
        async with self._connect_lock:
            if self.connected:
                return True

            try:
                uri = f"ws://{self.host}:{self.port}"
                self.websocket = await websockets.connect(
                    uri,
                    subprotocols=["binary"],
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=50 * 1024 * 1024,
                    open_timeout=10,
                    close_timeout=15,
                )

                random_suffix = f"{random.randint(1000, 9999)}"

                init_msg = json.dumps(
                    {
                        "mode": "2pass",
                        "chunk_size": [6, 12, 6],
                        "chunk_interval": 10,
                        "wav_name": f"dual_channel_{self.client_id}_{self.client_type}_{random_suffix}",
                        "is_speaking": True,
                        "hotwords": json.dumps(HOTWORDS),
                        "itn": True,
                        "audio_format": "pcm_s16le@16k",
                        "nls_config": {"heartbeat": True},
                    }
                )
                await self.websocket.send(init_msg)

                self.connected = True
                logger.info(
                    f"FunASR连接成功: {self.host}:{self.port} [client_id: {self.client_id}, type: {self.client_type}]"
                )
                return True
            except asyncio.TimeoutError:
                logger.error(f"连接FunASR超时: {self.host}:{self.port}")
                self.connected = False
                return False
            except Exception as e2:
                logger.error(f"连接FunASR失败: {e2}")
                self.connected = False
                if self.websocket:
                    try:
                        await self.websocket.close()
                    except:
                        pass
                return False

    async def _reconnect(self):
        if self.closing:
            logger.info(f"[{self.client_type}] 自身closing=True，放弃重连")
            return False
        if self.session_shutdown_event.is_set():
            logger.info(
                f"[{self.client_type}] 所属会话[{self.owner_session_id}]已关机，放弃重连"
            )
            return False
        if not self.should_reconnect:
            logger.info(f"[{self.client_type}] should_reconnect=False，放弃重连")
            return False

        current_time = time.time()
        if current_time - self._last_reconnect_time < self._reconnect_delay:
            await asyncio.sleep(self._reconnect_delay)

        self._last_reconnect_time = current_time
        self._reconnect_attempts += 1

        if self._reconnect_attempts > self._max_reconnect_attempts:
            logger.error(f"[{self.client_type}] 超过最大重连次数，放弃重连")
            return False

        try:
            await self.close()
            await asyncio.sleep(0.5)
            return await self.connect()
        except Exception as e2:
            logger.error(f"[{self.client_type}] 重连失败: {e2}")
            return False

    async def resample_audio_to_16k(
        self, audio_data: bytes, original_sample_rate: int = 16000
    ) -> bytes:
        if original_sample_rate == 16000:
            return audio_data

        try:
            if resample_thread_pool._work_queue.qsize() > 200:
                logger.warning(
                    f"重采样线程池队列过长: {resample_thread_pool._work_queue.qsize()}"
                )
                target_length = len(audio_data) * 2
                return audio_data + b"\x00" * (target_length - len(audio_data))

            if self.resample_mode == "none":
                return audio_data
            elif self.resample_mode == "fast_linear" and original_sample_rate == 8000:
                return await run_in_threadpool(
                    self._fast_linear_resample_8k_to_16k, audio_data
                )
            elif self.resample_mode == "linear":
                return await run_in_threadpool(
                    self._linear_resample, audio_data, original_sample_rate
                )
            elif self.resample_mode == "librosa":
                return await run_in_threadpool(
                    self._librosa_resample, audio_data, original_sample_rate
                )
            else:
                return await run_in_threadpool(
                    self._librosa_resample, audio_data, original_sample_rate
                )

        except Exception as e2:
            logger.error(f"音频重采样失败: {e2}")
            return audio_data

    @staticmethod
    def _apply_audio_enhancement(audio_np: np.ndarray) -> np.ndarray:
        if not FunASRClient.AUDIO_ENHANCE_CONFIG.get("enable_enhance", True):
            return audio_np

        try:
            audio_float = audio_np.astype(np.float32) / 32768.0
            gain_factor = FunASRClient.AUDIO_ENHANCE_CONFIG.get("gain_factor", 3.0)
            audio_float = audio_float * gain_factor
            enhanced_audio = (audio_float * 32767).astype(np.int16)
            return enhanced_audio
        except Exception as e6:
            logger.error(f"音频增强处理失败: {e6}")
            return audio_np

    @staticmethod
    def _librosa_resample(audio_data: bytes, original_sample_rate: int) -> bytes:
        try:
            audio_np = np.frombuffer(audio_data, dtype=np.int16)
            if len(audio_np) < 10:
                return audio_np.tobytes()

            audio_float = audio_np.astype(np.float32) / 32768.0
            resampled_float = librosa.resample(
                y=audio_float, orig_sr=original_sample_rate, target_sr=16000
            )
            resampled_int16 = (resampled_float * 32767).astype(np.int16)
            enhanced_np = FunASRClient._apply_audio_enhancement(resampled_int16)
            return enhanced_np.tobytes()
        except Exception as e5:
            logger.error(f"librosa重采样失败: {e5}")
            return audio_data

    @staticmethod
    def _fast_linear_resample_8k_to_16k(audio_data: bytes) -> bytes:
        try:
            audio_np = np.frombuffer(audio_data, dtype=np.int16)
            if len(audio_np) < 10:
                return np.repeat(audio_np, 2).astype(np.int16).tobytes()
            resampled_np = np.repeat(audio_np, 2)
            enhanced_np = FunASRClient._apply_audio_enhancement(resampled_np)
            return enhanced_np.tobytes()
        except Exception as e6:
            logger.error(f"快速线性重采样失败: {e6}")
            return audio_data

    @staticmethod
    def _linear_resample(audio_data: bytes, original_sample_rate: int) -> bytes:
        try:
            audio_np = np.frombuffer(audio_data, dtype=np.int16)
            if len(audio_np) < 10:
                target_len = int(len(audio_np) * 16000 / original_sample_rate)
                indices = np.arange(0, len(audio_np), len(audio_np) / target_len)
                indices = np.clip(indices.astype(int), 0, len(audio_np) - 1)
                return audio_np[indices].astype(np.int16).tobytes()

            if original_sample_rate == 8000:
                indices = np.arange(0, len(audio_np) - 0.5, 0.5)
                indices = indices.astype(int)
                indices = np.clip(indices, 0, len(audio_np) - 1)
                resampled_np = audio_np[indices].astype(np.int16)
            else:
                target_len = int(len(audio_np) * 16000 / original_sample_rate)
                x_old = np.linspace(0, 1, len(audio_np))
                x_new = np.linspace(0, 1, target_len)
                resampled_np = np.interp(x_new, x_old, audio_np.astype(np.float32))
                resampled_np = resampled_np.astype(np.int16)

            enhanced_np = FunASRClient._apply_audio_enhancement(resampled_np)
            return enhanced_np.tobytes()
        except Exception as e6:
            logger.error(f"线性重采样失败: {e6}")
            return audio_data

    async def send_audio(self, audio_data: bytes, sample_rate_hz: int = 16000):
        if self.closing or self.session_shutdown_event.is_set():
            logger.debug(f"[{self.client_type}] 正在关闭中，放弃发送音频")
            return False

        if not self.connected:
            logger.info(f"[{self.client_type}] send_audio发现连接断开，尝试重连...")
            if (
                self.closing
                or self.session_shutdown_event.is_set()
                or not self.should_reconnect
            ):
                logger.debug(f"[{self.client_type}] 不允许重连，放弃发送")
                return False

            success = await self.connect()
            if not success:
                success = await self._reconnect()
                if not success:
                    return False

        start_time = time.time()

        try:
            if sample_rate_hz != 16000:
                audio_data = await self.resample_audio_to_16k(
                    audio_data, sample_rate_hz
                )

            max_retries = 2
            base_timeout = 7.0

            for attempt in range(max_retries + 1):
                try:
                    timeout = base_timeout * (0.8**attempt)
                    timeout = max(3.0, min(timeout, 10.0))

                    await asyncio.wait_for(
                        self.websocket.send(audio_data), timeout=timeout
                    )

                    self.packets_sent += 1
                    self.audio_bytes_sent += len(audio_data)
                    self.last_sent_time = time.time()
                    self._reconnect_attempts = 0

                    if attempt > 0:
                        logger.info(
                            f"[{self.client_type}] 发送恢复成功，重试次数: {attempt}"
                        )

                    return True

                except asyncio.TimeoutError:
                    if attempt < max_retries:
                        logger.warning(
                            f"[{self.client_type}] 发送音频超时，第{attempt + 1}次重试，包大小={len(audio_data)}"
                        )
                        logger.error(
                            f"[{self.client_type}] --发送音频超时，耗时={(time.time() - start_time) * 1000:.1f}ms"
                        )
                        await asyncio.sleep(0.1 * (attempt + 1))
                        continue
                    else:
                        logger.error(f"[{self.client_type}] 发送音频多次超时，放弃发送")
                        self.connected = False
                        return False

                except websockets.exceptions.ConnectionClosed as e2:
                    logger.warning(f"[{self.client_type}] 连接已关闭: {e2}")
                    self.connected = False
                    if attempt < max_retries:
                        await self._reconnect()
                        await asyncio.sleep(0.2)
                        continue
                    else:
                        return False

        except Exception as e2:
            logger.error(f"[{self.client_type}] 发送音频失败: {e2}")
            self.connected = False
            return False

    async def receive_result(self):
        for attempt in range(3):
            if not self.connected:
                if not await self._reconnect():
                    return None

            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=5.0)
                result = json.loads(message)
                self.results_received += 1
                self._reconnect_attempts = 0
                return result

            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed as e2:
                logger.warning(f"[{self.client_type}] 连接已关闭: {e2}")
                self.connected = False
                await self._reconnect()
            except Exception as e3:
                logger.error(f"[{self.client_type}] 接收结果失败: {e3}")
                self.connected = False

        return None

    async def close(self):
        logger.info(
            f"[{self.client_type}] 收到close()调用。当前状态: connected={self.connected}, closing={getattr(self, 'closing', 'N/A')}, should_reconnect={getattr(self, 'should_reconnect', 'N/A')}"
        )
        self.connected = False
        if self.websocket:
            try:
                await asyncio.wait_for(self.websocket.close(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
            finally:
                logger.info(
                    f"funasr_client连接已关闭[{self.client_type}] _client_id[{self.client_id}]"
                )
                self.websocket = None
        self._reconnect_attempts = 0
        logger.info(f"[{self.client_type}] close()方法执行完毕。")
