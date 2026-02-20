# Dual-Channel-ASR

- 这是一款基于funasr的双通道（上行、下行）客服语音识别高性能生产级程序
- 应用场景：实时客服助手、实时客服质检等

## 特性

- 高性能：实测支持500+路电话并发稳定运行
- 双通道识别：同时识别员工和客户语音
- 实时转写：低延迟语音识别
- 自动重连：WebSocket和FunASR连接自动重连
- 会话管理：完整的客户端会话生命周期管理
- 监控告警：系统资源监控和连接泄露检测

## 依赖

- Python 3.8+
- FunASR 服务

## 安装

```bash
pip install -r requirements.txt
```

## 配置

编辑 `config.py` 文件：

```python
# FunASR服务器地址
FUNASR_SERVERS = [
    {"host": "192.168.0.116", "port": 10096},
]

# UDP监听端口
UDP_LISTEN_PORT = 8850

# WebSocket服务端口
WS_SERVER_PORT = 8080

# 重采样模式: 'linear', 'fast_linear', 'librosa'
RESAMPLE_MODE = 'linear'

# 热词配置
HOTWORDS = {
    "热词1": 30,
    "热词2": 25
}
```

## 运行

```bash
python main.py
```

服务启动后访问：
- API文档: http://localhost:8080/docs
- 健康检查: http://localhost:8080/health
- 统计信息: http://localhost:8080/stats

## WebSocket 接口

### 1. 生产接口 - 双通道语音识别

**端点**: `/ws/dual_channel_asr_with_phone_number`

**连接参数**:
```json
{
    "employee_number": "员工电话号码",
    "trace_id": "跟踪ID(可选)"
}
```

**返回消息类型**:
- `connection_established`: 连接建立
- `asr_result`: 识别结果
- `ping`/`pong`: 心跳
- `stats`: 统计信息
- `error`: 错误信息

### 2. 普通双通道接口

**端点**: `/ws/dual_channel_asr`

直接发送音频数据，格式：`[声道标识][音频数据]`

声道标识: `CH1:` (员工) 或 `CH2:` (客户)

### 3. 测试接口 - 随机监听

**端点**: `/ws/test_random_call`

随机监听当前活跃的通话

## VoIP电话信号和语音推流

在对应服务器单独运行rtp_forwarder.py.
具体使用方法请看rtp_forward/README.md

## UDP 数据包格式

```
[4字节: header长度][header JSON][音频数据]

header 示例:
{
    "call_id": "通话ID",
    "caller": "主叫号码",
    "callee": "被叫号码",
    "channel": "CH1" 或 "CH2"
}
```

## 性能优化

### Linux 系统配置

```bash
# /etc/security/limits.conf
* soft nofile 65536
* hard nofile 65536
```

### Docker 部署

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

## 项目结构

```
dual-channel-asr/
├── main.py              # 主入口
├── config.py            # 配置文件
├── requirements.txt     # 依赖
├── README.md           # 说明文档
└── src/
    ├── __init__.py
    ├── funasr_client.py    # FunASR客户端
    ├── client_session.py   # 客户端会话管理
    ├── audio_router.py     # 音频路由管理
    ├── tasks.py            # 后台任务   
    └── routes.py 后台任务   # FastAPI路由
```

## License

MIT License
