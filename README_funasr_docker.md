# FunASR Docker 部署指南

本文档介绍如何在 Docker 环境中部署 FunASR 语音识别服务。
参考文档：https://github.com/modelscope/FunASR/blob/main/runtime/docs/SDK_advanced_guide_online.md

## 环境要求

- Docker
- Linux 服务器（推荐）
- 至少 4 核 CPU，8GB+ 内存

## 快速开始

### 1. 拉取 FunASR 镜像

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/funasr/funasr-runtime-server-onnx:latest
```

### 2. 启动容器

```bash
docker run -d --name funasr-server \
  -p 10096:10095 \
  -v /path/to/models:/workspace/models \
  registry.cn-hangzhou.aliyuncs.com/funasr/funasr-runtime-server-onnx:latest
```

### 3. 进入容器

```bash
docker exec -it funasr-server bash
```

### 4. 启动 FunASR 服务

```bash
cd /workspace/FunASR/runtime/bin
nohup bash run_server_2pass.sh \
  --download-model-dir /workspace/models \
  --vad-dir damo/speech_fsmn_vad_zh-cn-16k-common-onnx \
  --model-dir damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx \
  --online-model-dir damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx \
  --punc-dir damo/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727-onnx \
  --lm-dir damo/speech_ngram_lm_zh-cn-ai-wesp-fst \
  --certfile 0 \
  --itn-dir thuduj12/fst_itn_zh \
  --port 10096 \
  > log.txt 2>&1 &
```

## 部署命令详解

### 标准部署命令

```bash
nohup bash run_server_2pass.sh \
  --download-model-dir /workspace/models \
  --vad-dir damo/speech_fsmn_vad_zh-cn-16k-common-onnx \
  --model-dir damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx \
  --online-model-dir damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx \
  --punc-dir damo/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727-onnx \
  --lm-dir damo/speech_ngram_lm_zh-cn-ai-wesp-fst \
  --certfile 0 \
  --itn-dir thuduj12/fst_itn_zh \
  > log.txt 2>&1 &
```

### 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| `--download-model-dir` | 模型下载目录 | `/workspace/models` |
| `--model-dir` | ASR 模型路径 | `damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx` |
| `--online-model-dir` | 在线识别模型路径 | `damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx` |
| `--vad-dir` | VAD 模型路径 | `damo/speech_fsmn_vad_zh-cn-16k-common-onnx` |
| `--punc-dir` | 标点模型路径 | `damo/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727-onnx` |
| `--lm-dir` | 语言模型路径 | `damo/speech_ngram_lm_zh-cn-ai-wesp-fst` |
| `--itn-dir` | 逆文本规范化模型路径 | `thuduj12/fst_itn_zh` |
| `--port` | 服务端口（默认 10095） | `10096` |
| `--certfile` | SSL 证书，设为 0 关闭 SSL | `0` |
| `--decoder-thread-num` | 解码线程数（最大并发路数） | `16` |
| `--io-thread-num` | IO 线程数 | `8` |
| `--model-thread-num` | 每路识别内部线程数 | `1` |
| `--hotword` | 热词文件路径 | `/workspace/models/hotwords.txt` |

### 线程数配置建议

- `decoder-thread-num * model-thread-num` = 总线程数
- 例如：16 核 CPU，可设置 `decoder-thread-num=16`, `model-thread-num=1`

## Docker 管理命令

### 查看容器

```bash
# 查看运行中的容器
docker ps

# 查看所有容器
docker ps -a
```

### 启动/停止容器

```bash
# 启动容器
docker start <容器ID或名称>

# 停止容器
docker stop <容器ID或名称>

# 进入容器
docker exec -it <容器ID或名称> bash

# 重新附着到容器
docker attach <容器ID或名称>
```

### 重启 FunASR 服务

```bash
# 查看进程
ps -x | grep funasr-wss-server-2pass

# 杀掉进程
kill -9 <PID>

# 或使用 pkill
pkill -f funasr-wss-server
```

## 使用不同模型

### SenseVoiceSmall 模型

```bash
nohup bash run_server_2pass.sh \
  --download-model-dir /workspace/models \
  --vad-dir damo/speech_fsmn_vad_zh-cn-16k-common-onnx \
  --model-dir iic/SenseVoiceSmall-onnx \
  --online-model-dir iic/SenseVoiceSmall-onnx \
  --punc-dir damo/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727-onnx \
  --certfile 0 \
  --port 10096 \
  > log.txt 2>&1 &
```

### 时间戳模型

```bash
--model-dir damo/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx
```

### 上下文热词模型

```bash
--model-dir damo/speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404-onnx
```

## 热词配置

### 服务端热词

创建热词文件（每行一个热词，格式：`热词 权重`）：

```
阿里巴巴 20
淘宝 15
天猫 15
```

映射到容器：

```bash
docker run -d --name funasr-server \
  -p 10096:10095 \
  -v /path/to/hotwords.txt:/workspace/models/hotwords.txt \
  registry.cn-hangzhou.aliyuncs.com/funasr/funasr-runtime-server-onnx:latest
```

启动时添加参数：

```bash
--hotword /workspace/models/hotwords.txt
```

### 热词格式说明

- 格式：`热词 权重`
- 权重范围：1~100（建议不超过 30）
- 热词长度建议不超过 10 个字符
- 热词个数建议不超过 1000 个

## 客户端连接

### WebSocket 连接地址

```
ws://<服务器IP>:10096/
```

### Python 示例

```python
import websocket
import json

ws = websocket.create_connection("ws://192.168.0.116:10096/")

# 发送初始化消息
init_msg = {
    "mode": "2pass",
    "chunk_size": [6, 12, 6],
    "chunk_interval": 10,
    "wav_name": "test",
    "is_speaking": True,
    "hotwords": json.dumps({"热词1": 20}),
    "itn": True,
    "audio_format": "pcm_s16le@16k"
}
ws.send(json.dumps(init_msg))

# 发送音频数据
ws.send(audio_data)

# 接收识别结果
result = ws.recv()
print(json.loads(result))

ws.close()
```

## 官方文档

- [FunASR SDK 高级指南](https://github.com/modelscope/FunASR/blob/main/runtime/docs/SDK_advanced_guide_online.md)
- [FunASR SDK 教程](https://github.com/modelscope/FunASR/blob/main/runtime/docs/SDK_tutorial_online.md)
- [FunASR GitHub](https://github.com/modelscope/FunASR/tree/main)

## 注意事项

1. **Linux 服务器推荐**：生产环境建议使用 Linux 服务器
2. **端口开放**：确保防火墙开放相应端口
3. **SSL 证书**：生产环境建议配置 SSL 证书，测试环境可用 `--certfile 0` 关闭
4. **资源规划**：根据并发需求合理配置线程数
5. **模型下载**：首次启动会自动下载模型，需要较长时间
