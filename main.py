"""
双通道实时语音识别服务 - 主入口
"""

import asyncio
import logging
import resource
from config import WS_SERVER_PORT, logger
from src.routes import create_app


def set_file_descriptor_limit():
    """设置文件描述符限制"""
    try:
        soft_limit = 65536
        hard_limit = 65536

        current_soft, current_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        logger.info(f"当前文件描述符限制: 软限制={current_soft}, 硬限制={current_hard}")

        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))

        new_soft, new_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        logger.info(f"设置后文件描述符限制: 软限制={new_soft}, 硬限制={new_hard}")

        if new_soft >= soft_limit:
            logger.info(f"成功设置文件描述符限制为 {soft_limit}")
        else:
            logger.warning(
                f"无法设置到 {soft_limit}，当前限制为 {new_soft}。可能需要root权限"
            )

    except ValueError as e2:
        logger.error(f"设置文件描述符限制失败（可能需要root权限）: {e2}")
    except Exception as e3:
        logger.error(f"设置文件描述符限制时发生未知错误: {e3}")


def main():
    import uvicorn

    set_file_descriptor_limit()

    app = create_app()

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=WS_SERVER_PORT,
        log_level="info",
        ws_ping_interval=25,
        ws_ping_timeout=30,
        timeout_keep_alive=30,
        limit_concurrency=1200,
        backlog=8192,
        access_log=True,
        loop="asyncio",
        http="h11",
    )

    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logger.info("使用uvloop事件循环策略")
    except ImportError:
        logger.info("使用标准asyncio事件循环策略")

    server = uvicorn.Server(config)

    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        logger.info("收到中断信号，优雅退出")
    except Exception as e:
        logger.error(f"服务器异常: {e}")


if __name__ == "__main__":
    main()
