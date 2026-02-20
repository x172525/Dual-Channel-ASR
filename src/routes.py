"""
FastAPI路由模块
"""

import json
import time
import asyncio
import logging
import random
import uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from config import UDP_LISTEN_PORT, WS_SERVER_PORT, logger

try:
    from src.funasr_client import FunASRClient
    from src.client_session import ClientSession
    from src.audio_router import audio_router, connection_monitor
    from src.tasks import (
        udp_server_task,
        funasr_result_forwarder,
        test_funasr_result_forwarder,
        print_stats_periodically,
        check_resource_leaks_periodically,
        cleanup_inactive_sessions_periodically,
        monitor_system_resources,
    )
except ImportError:
    from funasr_client import FunASRClient
    from client_session import ClientSession
    from audio_router import audio_router, connection_monitor
    from tasks import (
        udp_server_task,
        funasr_result_forwarder,
        test_funasr_result_forwarder,
        print_stats_periodically,
        check_resource_leaks_periodically,
        cleanup_inactive_sessions_periodically,
        monitor_system_resources,
    )


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("=" * 60)
        logger.info("语音识别服务启动 - 优化后台任务管理")
        logger.info(f"启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        background_tasks = []
        shutdown_event = asyncio.Event()

        core_tasks = [
            (udp_server_task, "UDP服务器"),
            (print_stats_periodically, "定期统计"),
            (cleanup_inactive_sessions_periodically, "会话清理"),
            (check_resource_leaks_periodically, "资源泄漏检查"),
            (monitor_system_resources, "系统资源监控"),
        ]

        for task_func, task_name in core_tasks:
            task = asyncio.create_task(task_func())
            background_tasks.append((task, task_name, True))
            logger.info(f"[任务管理] 启动周期性任务: {task_name}")

        async def startup_diagnostics():
            await asyncio.sleep(5)
            if not shutdown_event.is_set():
                logger.info("[启动诊断] 执行启动后诊断...")
                try:
                    await audio_router.diagnose_and_cleanup_sessions()
                except Exception as e4:
                    logger.error(f"[启动诊断] 启动诊断失败: {e4}")

        startup_diag_task = asyncio.create_task(startup_diagnostics())
        background_tasks.append((startup_diag_task, "启动诊断", False))

        async def monitor_background_tasks():
            check_interval = 30
            while not shutdown_event.is_set():
                await asyncio.sleep(check_interval)

                alive_periodic = 0
                total_periodic = 0

                for task_1, name_1, is_periodic_1 in background_tasks:
                    if is_periodic_1:
                        total_periodic += 1
                        if not task_1.done():
                            alive_periodic += 1
                        elif not shutdown_event.is_set():
                            logger.error(f"[健康监控] 关键周期性任务意外结束: {name_1}")

                if total_periodic > 0:
                    logger.info(
                        f"[健康监控] 周期性任务状态: {alive_periodic}/{total_periodic} 运行中"
                    )

        monitor_task = asyncio.create_task(monitor_background_tasks())
        background_tasks.append((monitor_task, "健康监控", True))

        logger.info(f"[任务管理] 已启动 {len(background_tasks)} 个后台任务")

        yield

        logger.info("服务正在关闭，正在停止所有后台任务...")
        shutdown_event.set()

        cancellation_tasks = []
        for task, name, is_periodic in background_tasks:
            if not task.done():
                task.cancel()
                cancellation_tasks.append(task)
                logger.debug(f"[关闭] 正在取消任务: {name}")

        if cancellation_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*cancellation_tasks, return_exceptions=True),
                    timeout=10.0,
                )
                logger.info("[关闭] 所有后台任务已停止")
            except asyncio.TimeoutError:
                logger.warning("[关闭] 等待任务停止超时，强制关闭")
            except Exception as e2:
                logger.error(f"[关闭] 等待任务停止时异常: {e2}")
        else:
            logger.info("[关闭] 没有需要取消的任务")

        from src.funasr_client import resample_thread_pool

        resample_thread_pool.shutdown(wait=True)
        logger.info("[关闭] 线程池已关闭")

        for task, name, is_periodic in background_tasks:
            if not task.done():
                logger.warning(f"[关闭] 任务可能仍在运行: {name}")

        logger.info("服务已完全关闭")

    app = FastAPI(
        title="双通道实时语音识别服务",
        version="1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    @app.websocket("/ws/dual_channel_asr_with_phone_number")
    async def production_websocket_endpoint(websocket: WebSocket):
        session = None
        forwarder_task = None

        try:
            await websocket.accept()

            init_data = await websocket.receive_json()
            employee_number = init_data.get("employee_number", "").strip()
            trace_id = init_data.get("trace_id", str(uuid.uuid4()))

            if not employee_number:
                await websocket.close(code=1008, reason="缺少employee_number参数")
                return

            client_id = str(uuid.uuid4())[:8]
            await connection_monitor.connection_created(client_id)

            session = ClientSession(client_id, employee_number, trace_id, websocket)
            logger.info(
                f"新生产连接: 客户端={client_id}, 员工号={employee_number}, 跟踪ID={trace_id}"
            )

            await audio_router.subscribe(employee_number, session)

            funasr_config = random.choice(FUNASR_SERVERS)
            session.funasr_ch1 = FunASRClient(
                **funasr_config,
                client_type=f"{employee_number}_CH1",
                owner_session_id=client_id,
            )
            session.funasr_ch2 = FunASRClient(
                **funasr_config,
                client_type=f"{employee_number}_CH2",
                owner_session_id=client_id,
            )

            connect_tasks = [
                asyncio.create_task(session.funasr_ch1.connect()),
                asyncio.create_task(session.funasr_ch2.connect()),
            ]

            connect_results = await asyncio.gather(
                *connect_tasks, return_exceptions=True
            )

            ch1_connected = (
                connect_results[0]
                if not isinstance(connect_results[0], Exception)
                else False
            )
            ch2_connected = (
                connect_results[1]
                if not isinstance(connect_results[1], Exception)
                else False
            )

            await session.send_message(
                {
                    "type": "connection_established",
                    "message": "双通道语音识别服务已就绪，开始监听通话音频",
                    "employee_number": employee_number,
                    "trace_id": trace_id,
                    "client_id": client_id,
                    "sent_at": time.time(),
                    "channels": {
                        "CH1": {
                            "name": "员工声道",
                            "status": "已连接" if ch1_connected else "连接失败",
                        },
                        "CH2": {
                            "name": "客户声道",
                            "status": "已连接" if ch2_connected else "连接失败",
                        },
                    },
                    "subscription_info": {
                        "udp_port": UDP_LISTEN_PORT,
                        "status": "活跃",
                    },
                }
            )

            forwarder_task = asyncio.create_task(funasr_result_forwarder(session))

            try:
                while session.is_active:
                    try:
                        message = await asyncio.wait_for(
                            websocket.receive_text(), timeout=180.0
                        )

                        try:
                            data = json.loads(message)
                            if data.get("type") == "ping":
                                await session.send_message(
                                    {
                                        "type": "pong",
                                        "client_id": client_id,
                                        "trace_id": trace_id,
                                        "sent_at": time.time(),
                                    }
                                )
                            elif data.get("type") == "get_stats":
                                await session.send_message(
                                    {
                                        "type": "stats",
                                        "client_stats": session.stats,
                                        "router_stats": audio_router.stats,
                                        "client_id": client_id,
                                        "trace_id": trace_id,
                                        "sent_at": time.time(),
                                    }
                                )

                        except json.JSONDecodeError:
                            logger.warning(f"收到无效JSON消息: {message[:100]}")

                    except asyncio.TimeoutError:
                        try:
                            await websocket.send_json(
                                {
                                    "type": "heartbeat",
                                    "client_id": client_id,
                                    "trace_id": trace_id,
                                    "sent_at": time.time(),
                                }
                            )
                        except:
                            break

            except WebSocketDisconnect:
                logger.info(f"客户端断开连接: {client_id}")

        except WebSocketDisconnect:
            logger.info("客户端在初始化阶段断开")
            try:
                if session:
                    session.is_active = False
                    await audio_router.unsubscribe(session.employee_number, session)
                    await session.close()
                await connection_monitor.connection_closed(
                    session.client_id, "normal_close"
                )
            except Exception as e2:
                logger.error(f"会话清理异常: {e2}")

        except Exception as e3:
            logger.error(f"接口处理异常: {e3}")
            try:
                if session:
                    await session.send_message(
                        {
                            "type": "error",
                            "message": f"服务内部错误: {str(e3)[:100]}",
                            "sent_at": time.time(),
                        }
                    )
            except:
                pass

        finally:
            if session:
                if forwarder_task and not forwarder_task.done():
                    session.is_active = False
                    if forwarder_task and not forwarder_task.done():
                        forwarder_task.cancel()
                        try:
                            await asyncio.wait_for(forwarder_task, timeout=2.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            logger.debug(f"转发器任务取消完成: {session.client_id}")

                await connection_monitor.connection_closed(
                    session.client_id, "normal_close"
                )

                await asyncio.sleep(0.05)

                try:
                    await audio_router.unsubscribe(session.employee_number, session)
                except Exception as e2:
                    logger.error(f"取消订阅失败: {e2}")

                if session.funasr_ch1:
                    session.funasr_ch1.closing = True
                    session.funasr_ch1.should_reconnect = False
                    session.funasr_ch1.session_shutdown_event.set()
                if session.funasr_ch2:
                    session.funasr_ch2.closing = True
                    session.funasr_ch2.should_reconnect = False
                    session.funasr_ch2.session_shutdown_event.set()

                try:
                    session.message_buffer.clear()
                    logger.debug(f"消息缓冲区已清空: {session.client_id}")
                except Exception as e2:
                    logger.error(f"清空消息缓冲区失败: {e2}")

                try:
                    await session.close()
                except Exception as e3:
                    logger.error(f"关闭会话失败: {e3}")

    @app.websocket("/ws/dual_channel_asr")
    async def websocket_dual_channel_endpoint(websocket: WebSocket):
        await websocket.accept()

        client_id = str(uuid.uuid4())[:8]
        logger.info(f"普通双通道接口连接: {client_id}")

        funasr_config = random.choice(FUNASR_SERVERS)
        funasr_ch1 = FunASRClient(
            **funasr_config, client_type=f"normal_CH1_{client_id}"
        )
        funasr_ch2 = FunASRClient(
            **funasr_config, client_type=f"normal_CH2_{client_id}"
        )

        if not (await funasr_ch1.connect() and await funasr_ch2.connect()):
            await websocket.close(code=1011, reason="FunASR连接失败")
            return

        await websocket.send_json(
            {
                "type": "connection_established",
                "client_id": client_id,
                "message": "双通道语音识别连接已建立",
                "sent_at": time.time(),
            }
        )

        async def receive_and_forward(channel: str, client: FunASRClient):
            while True:
                result = await client.receive_result()
                if result:
                    try:
                        await websocket.send_json(
                            {
                                "type": "asr_result",
                                "channel": channel,
                                "sent_at": time.time(),
                                "data": result,
                            }
                        )
                    except (websockets.exceptions.ConnectionClosed, RuntimeError) as e3:
                        logger.debug(f"WebSocket连接已关闭，停止发送结果: {e3}")
                        break

        task_ch1 = asyncio.create_task(receive_and_forward("CH1", funasr_ch1))
        task_ch2 = asyncio.create_task(receive_and_forward("CH2", funasr_ch2))

        try:
            async for message in websocket.iter_bytes():
                if len(message) >= 4:
                    try:
                        if len(message) >= 6:
                            sample_rate_header = message[:6].decode(
                                "ascii", errors="ignore"
                            )
                            if sample_rate_header.startswith("SR"):
                                sample_rate = int(sample_rate_header[2:])
                                audio_data = message[6:]
                            else:
                                sample_rate = 16000
                                audio_data = message
                        else:
                            sample_rate = 16000
                            audio_data = message

                        channel_flag = (
                            audio_data[:4].decode("utf-8", errors="ignore").strip()
                        )
                        actual_audio = audio_data[4:]

                        if channel_flag == "CH1:":
                            await funasr_ch1.send_audio(
                                actual_audio, sample_rate_hz=sample_rate
                            )
                        elif channel_flag == "CH2:":
                            await funasr_ch2.send_audio(
                                actual_audio, sample_rate_hz=sample_rate
                            )

                    except Exception as e2:
                        logger.error(f"处理音频数据失败: {e2}")

        except WebSocketDisconnect:
            logger.info(f"普通接口客户端断开: {client_id}")

        finally:
            task_ch1.cancel()
            task_ch2.cancel()
            await asyncio.gather(task_ch1, task_ch2, return_exceptions=True)
            await funasr_ch1.close()
            await funasr_ch2.close()

    @app.websocket("/ws/test_random_call")
    async def test_random_call_endpoint(websocket: WebSocket):
        session = None
        current_trace_id = f"test_random_{int(time.time())}"
        forwarder_task = None

        try:
            await websocket.accept()
            logger.info(f"测试接口连接: 跟踪ID={current_trace_id}")

            try:
                await websocket.send_json(
                    {
                        "type": "test_connection_established",
                        "message": "测试接口已连接，开始监听通话",
                        "trace_id": current_trace_id,
                        "sent_at": time.time(),
                    }
                )
            except websockets.exceptions.ConnectionClosed:
                logger.info("连接在发送初始消息时已关闭")
                return
            except RuntimeError as e2:
                if "Cannot call" in str(e2):
                    logger.info("连接已关闭")
                    return
                else:
                    raise

            while True:
                try:
                    available_employees = audio_router.get_recent_active_numbers()

                    if not available_employees:
                        try:
                            await websocket.send_json(
                                {
                                    "type": "test_status",
                                    "message": "当前没有检测到活跃通话，等待中...",
                                    "sent_at": time.time(),
                                }
                            )
                        except (websockets.exceptions.ConnectionClosed, RuntimeError):
                            logger.info("test_random连接已关闭，退出循环")
                            break
                        await asyncio.sleep(5)
                        continue

                    selected_employee = random.choice(available_employees)

                    try:
                        await websocket.send_json(
                            {
                                "type": "test_call_selected",
                                "message": f"开始监听员工号 {selected_employee}",
                                "employee_number": selected_employee,
                                "sent_at": time.time(),
                                "available_calls": len(available_employees),
                            }
                        )
                    except (websockets.exceptions.ConnectionClosed, RuntimeError):
                        logger.info("test_random_call连接已关闭，退出循环")
                        break

                    logger.info(
                        f"测试接口: 选择员工号 {selected_employee}, 当前活跃数: {len(available_employees)}"
                    )

                    session = ClientSession(
                        client_id=f"test_{str(uuid.uuid4())[:8]}",
                        employee_number=selected_employee,
                        trace_id=current_trace_id,
                        websocket=websocket,
                    )

                    await audio_router.subscribe(selected_employee, session)

                    funasr_config = random.choice(FUNASR_SERVERS)
                    session.funasr_ch1 = FunASRClient(
                        **funasr_config, client_type=f"test_{selected_employee}_CH1"
                    )
                    session.funasr_ch2 = FunASRClient(
                        **funasr_config, client_type=f"test_{selected_employee}_CH2"
                    )

                    connect_tasks = [
                        asyncio.create_task(session.funasr_ch1.connect()),
                        asyncio.create_task(session.funasr_ch2.connect()),
                    ]

                    connect_results = await asyncio.gather(
                        *connect_tasks, return_exceptions=True
                    )
                    ch1_connected = (
                        connect_results[0]
                        if not isinstance(connect_results[0], Exception)
                        else False
                    )
                    ch2_connected = (
                        connect_results[1]
                        if not isinstance(connect_results[1], Exception)
                        else False
                    )

                    try:
                        await websocket.send_json(
                            {
                                "type": "test_monitoring_started",
                                "message": f"开始监控员工 {selected_employee} 的通话",
                                "employee_number": selected_employee,
                                "sent_at": time.time(),
                                "channels": {
                                    "CH1": {
                                        "name": "员工声道",
                                        "status": "已连接"
                                        if ch1_connected
                                        else "连接失败",
                                    },
                                    "CH2": {
                                        "name": "客户声道",
                                        "status": "已连接"
                                        if ch2_connected
                                        else "连接失败",
                                    },
                                },
                            }
                        )
                    except (websockets.exceptions.ConnectionClosed, RuntimeError):
                        logger.info("test_random_call 连接已关闭，退出循环")
                        break

                    forwarder_task = asyncio.create_task(
                        test_funasr_result_forwarder(session, websocket)
                    )

                    monitoring_start = time.time()
                    last_audio_time = time.time()

                    while time.time() - monitoring_start < 120:
                        try:
                            if not session.is_active:
                                logger.info("会话已标记为不活跃，退出监控循环")
                                break

                            if session.stats["audio_packets_received"] > 0:
                                last_audio_time = time.time()

                                if int(time.time()) % 10 == 0:
                                    try:
                                        await websocket.send_json(
                                            {
                                                "type": "test_monitoring_status",
                                                "message": f"正在监控员工 {selected_employee}",
                                                "employee_number": selected_employee,
                                                "stats": session.stats,
                                                "sent_at": time.time(),
                                            }
                                        )
                                    except (
                                        websockets.exceptions.ConnectionClosed,
                                        RuntimeError,
                                    ):
                                        logger.info("连接已关闭，退出监控循环")
                                        session.is_active = False
                                        break

                            if time.time() - last_audio_time > 30:
                                try:
                                    await websocket.send_json(
                                        {
                                            "type": "test_call_ended",
                                            "message": f"员工 {selected_employee} 的通话可能已结束",
                                            "employee_number": selected_employee,
                                            "sent_at": time.time(),
                                            "call_duration": time.time()
                                            - monitoring_start,
                                        }
                                    )
                                except (
                                    websockets.exceptions.ConnectionClosed,
                                    RuntimeError,
                                ):
                                    logger.debug("发送结束消息时连接已断开")
                                break

                            try:
                                message = await asyncio.wait_for(
                                    websocket.receive_text(), timeout=1.0
                                )
                                if message.strip().lower() == "switch":
                                    try:
                                        await websocket.send_json(
                                            {
                                                "type": "test_switching",
                                                "message": "切换到下一个通话",
                                                "sent_at": time.time(),
                                            }
                                        )
                                    except (
                                        websockets.exceptions.ConnectionClosed,
                                        RuntimeError,
                                    ):
                                        logger.debug("发送切换消息时连接已断开")
                                    break
                            except asyncio.TimeoutError:
                                continue
                            except (
                                websockets.exceptions.ConnectionClosed,
                                RuntimeError,
                            ):
                                logger.info("接收命令时连接已关闭")
                                session.is_active = False
                                break

                        except Exception as e2:
                            logger.error(f"监控循环异常: {e2}")
                            break

                    forwarder_task.cancel()
                    try:
                        await forwarder_task
                    except asyncio.CancelledError:
                        pass

                    await session.close()
                    await audio_router.unsubscribe(selected_employee, session)

                    try:
                        await websocket.send_json(
                            {
                                "type": "test_call_completed",
                                "message": f"完成对员工 {selected_employee} 的监控",
                                "sent_at": time.time(),
                            }
                        )
                    except (websockets.exceptions.ConnectionClosed, RuntimeError):
                        logger.debug("发送完成消息时连接已断开")

                    await asyncio.sleep(3)

                except Exception as e2:
                    logger.error(f"测试接口循环异常: {e2}")
                    await asyncio.sleep(5)

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"测试接口连接已关闭")
        except RuntimeError as e3:
            if "Cannot call" in str(e3) or "close message" in str(e3):
                logger.info("WebSocket连接已关闭")
            else:
                logger.error(f"测试接口RuntimeError异常: {e3}")
        except Exception as e4:
            logger.error(f"测试接口异常: {e4}")
        finally:
            if session:
                session.is_active = False

                if forwarder_task and not forwarder_task.done():
                    forwarder_task.cancel()
                    try:
                        await forwarder_task
                    except asyncio.CancelledError:
                        pass

                try:
                    await audio_router.unsubscribe(session.employee_number, session)
                except Exception as e4:
                    logger.error(f"取消订阅失败: {e4}")

                await session.close()

    @app.get("/health")
    async def health_check():
        return {
            "status": "healthy",
            "service": "dual_channel_asr",
            "version": "1.0",
            "sent_at": time.time(),
            "stats": audio_router.stats,
        }

    @app.get("/stats")
    async def get_stats():
        return {
            "service": "双通道实时语音识别服务",
            "version": "1.0",
            "sent_at": time.time(),
            "audio_router_stats": audio_router.stats,
            "subscriptions": {
                "total": len(audio_router.subscriptions),
                "details": {
                    employee: len(sessions)
                    for employee, sessions in audio_router.subscriptions.items()
                },
            },
        }

    @app.get("/")
    async def root():
        return {
            "service": "双通道实时语音识别服务",
            "version": "1.0",
            "description": "高性能双通道实时语音识别服务",
            "endpoints": {
                "生产接口": "/ws/dual_channel_asr_with_phone_number",
                "普通接口": "/ws/dual_channel_asr",
                "测试接口": "/ws/test_random_call",
                "健康检查": "/health",
                "统计信息": "/stats",
            },
        }

    return app
