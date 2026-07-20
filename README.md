# TokenFlow

TokenFlow 是一个面向 OpenAI-compatible API 的全屏终端 Token 运行器。它可以并发发起多个流式请求，实时显示请求状态、Token 用量、费用和失败重试情况。

TokenFlow 面向已经获得接口使用授权的场景。它不会伪造 Token，也不会把模型提前结束的回答补成长文本。

## 主要功能

- 首次运行通过 TUI 配置供应商、Base URL、API Key、模型和价格。
- 从供应商的 `GET /models` 接口读取模型列表，也可以刷新列表或手动填写模型 ID。
- 保存多个供应商和多个模型；一次运行选择一个模型。
- 支持按请求数、总 Token 数或无限模式运行。
- 使用 SSE 流式响应，收到第一个文本字符时立即更新请求卡片。
- 对超时、网络错误、429 和 5xx 响应进行有限重试，并遵守 `Retry-After`。
- 连续失败时自动熔断，全局没有请求开始输出时提供空闲保护。
- 动态补位：一个请求结束后立即补充下一个请求，不超过并发上限。
- 记录 `prompt_tokens`、`completion_tokens`、`total_tokens`、耗时和费用。
- 请求区根据终端宽度自适应为 1 到 4 列，右侧显示全局统计和脱敏连接信息。

## 安装

TokenFlow 的发布包名为 `tokenflow-cli`，正式版本可通过 PyPI 安装：

```powershell
python -m pip install tokenflow-cli
```

安装完成后直接启动：

```powershell
tokenflow
```

也可以使用模块入口：

```powershell
python -m tokenflow_cli
```

## 首次运行

首次运行时，如果用户目录中还没有运行配置或模型资料，TokenFlow 会自动创建默认请求配置，并打开设置向导。

向导会依次询问：

1. 供应商名称
2. Base URL
3. API Key
4. 模型来源
5. 币种（可直接回车，默认 `USD`）
6. 每百万输入 Token 价格（可直接回车，默认 `0`）
7. 每百万输出 Token 价格（可直接回车，默认 `0`）
8. 价格倍率（可直接回车，默认 `1`）

模型来源有以下选项：

- 从 `/models` 列表选择
- 刷新模型列表
- 手动填写模型 ID
- 返回上一级

文本输入支持 Textual 原生粘贴、`Ctrl+V`、`Ctrl+Shift+V` 和 `Shift+Insert`。不同终端对快捷键的处理可能不同，Windows Terminal 通常使用 `Ctrl+Shift+V`。

Base URL 应填写 OpenAI-compatible API 的基础地址，例如：

```text
https://api.example.com/v1
```

TokenFlow 会请求：

```text
GET https://api.example.com/v1/models
POST https://api.example.com/v1/chat/completions
```

请求会使用：

```http
Authorization: Bearer <API_KEY>
```

如果服务商的 `/models` 接口不可用，可以在向导中返回并手动填写模型 ID。只要 `chat/completions` 接口兼容，模型列表接口不是运行请求的硬性要求。

## 用户数据

默认用户目录为：

```text
~/.tokenflow/
```

Windows 通常对应：

```text
%USERPROFILE%\.tokenflow\
```

其中：

| 文件 | 用途 |
| --- | --- |
| `models.json` | 供应商、Base URL、API Key、模型和价格 |
| `config.yaml` | 请求参数和调度参数 |
| `prompt.txt` | 默认长文本 Prompt |
| `logs/` | JSONL 请求日志 |

运行选择状态保存在用户目录的 `last_selection.json`（默认完整路径为 `~/.tokenflow/last_selection.json`）；使用项目目录配置时则保存在该配置目录的 `.tokenflow/last_selection.json`，只保存目标和模型选择，不保存 API Key。

### API Key

按照当前设计，API Key 默认直接保存在用户目录的 `models.json` 中。首次配置时 TUI 会明确提示这一点。请确保该文件只能由当前用户访问，并且不要把它提交到 Git 或上传到公共仓库。

也可以保存环境变量引用，而不是保存明文 Key：

```json
{
  "apiKey": "${TOKENFLOW_API_KEY}"
}
```

运行时 TokenFlow 会读取 `TOKENFLOW_API_KEY`。如果环境变量不存在，请求会在启动前失败。

### models.json 格式

TokenFlow 兼容并优先写入供应商名称作为顶层键的格式：

```json
{
  "provider-name": {
    "BaseUrl": "https://api.example.com/v1",
    "apiKey": "${TOKENFLOW_API_KEY}",
    "api": "openai-completion",
    "models": [
      {
        "id": "model-id",
        "cost": {
          "currency": "CNY",
          "input": 0.75,
          "output": 4.5,
          "muiltipliers": 1
        }
      }
    ]
  }
}
```

`id` 也兼容旧资料中的 `name`。`BaseUrl`、`apiKey`、`muiltipliers` 是当前用户格式中的字段名；读取时也兼容 `base_url`、`api_key` 和标准拼写 `multiplier`。

价格含义为每一百万 Token 的金额：

- `input`：输入 Token 单价
- `output`：输出 Token 单价
- `currency`：币种代码，例如 `USD`、`CNY`
- `muiltipliers`：输入和输出价格共同使用的倍率，默认 `1`

例如 `CNY`、`input: 0.75`、`output: 4.5` 表示输入每百万 Token 0.75 元、输出每百万 Token 4.5 元。

## 运行界面

启动后使用：

- 上下键：移动选择
- Enter：确认
- Esc：返回

首次配置或模型库中有多个模型时，先选择供应商和模型。之后进入运行设置，可选择：

- 请求数
- 总 Token 目标
- 无限运行

运行完成后，结果页会保留请求卡片和统计，并提供：

- 使用相同设置再次运行
- 修改运行设置
- 退出 TokenFlow

## 命令行选项

不指定目标时，TokenFlow 会打开完整 TUI：

```powershell
tokenflow
```

指定请求数：

```powershell
tokenflow --requests 10
```

指定总 Token 目标：

```powershell
tokenflow --target-tokens 1000000
```

无限运行，直到手动停止或触发安全停止：

```powershell
tokenflow --unlimited
```

也可以指定模型 ID。该 ID 会在所有已保存供应商中查找；如果没有找到，命令会报错：

```powershell
tokenflow --requests 10 --model model-id
```

三个运行目标互斥：

- `--requests N`：最多启动 N 个逻辑请求，内部重试不计为新请求。
- `--target-tokens N`：累计 `usage.total_tokens` 达到或超过 N 后停止补位。
- `--unlimited`：持续补位，直到手动停止、熔断或全局空闲保护。

## 请求配置

普通用户不需要编辑 `config.yaml`。首次运行时 TokenFlow 会自动生成它，之后可以通过 TUI 使用默认请求配置。

如需调整并发、超时、重试或 Prompt 参数，编辑：

```text
~/.tokenflow/config.yaml
```

该文件只保存请求和调度配置，不保存供应商、模型或 API Key。主要字段如下：

| 字段 | 作用 |
| --- | --- |
| `prompt_file` | Prompt 文件路径 |
| `tokenizer` | Token 估算编码，默认 `cl100k_base` |
| `min_input_tokens` | Prompt 最小输入 Token 数 |
| `concurrency` | 最大同时活动请求数 |
| `max_requests` | TUI 默认请求数目标 |
| `max_tokens` | 单次请求最大输出 Token 数 |
| `minimum_completion_tokens` | Prompt 要求的最低输出 Token 数 |
| `temperature` | 请求温度 |
| `request_timeout` | 单次请求最大等待时间，单位秒 |
| `first_token_timeout` | 首个文本字符等待时间，单位秒 |
| `max_retries` | 单次逻辑请求的最大重试次数 |
| `backoff_base_seconds` | 重试退避基数 |
| `circuit_breaker_threshold` | 连续失败熔断阈值 |
| `global_idle_timeout` | 所有活动请求都没有首字符时的全局停止时间 |
| `extra_body` | 供应商额外请求字段 |
| `logs_dir` | JSONL 日志目录 |

可以用以下命令在当前目录生成一套开发配置：

```powershell
tokenflow init --directory . --generate-prompt
```

如果目标文件已存在，使用 `--force` 覆盖：

```powershell
tokenflow init --directory . --generate-prompt --force
```

使用当前目录的配置运行：

```powershell
tokenflow --config .\config.yaml
```

使用 `--config` 时，供应商和模型仍然来自默认用户模型库 `~/.tokenflow/models.json`。

## Prompt 和 Token 限制

默认 Prompt 目标是至少 20,000 个输入 Token，默认 `max_tokens` 为 10,001，默认 Prompt 会要求模型尽量输出至少 10,001 个 completion Token。

实际输出长度仍然取决于：

- 模型上下文窗口
- 服务商的输出上限
- 服务商是否接受 `max_tokens`
- 模型提前结束或触发停止原因
- 请求超时或网络错误

TokenFlow 只记录实际 usage。如果模型输出少于要求，会标记为低于输出目标，不会用重复内容伪造 Token。

默认情况下，`cl100k_base` 使用离线保守估算，避免首次启动因为下载编码资源而失败。如需使用 tiktoken 的精确编码资源，可以允许下载后重新生成 Prompt：

```powershell
$env:AUTOTOKEN_ALLOW_TOKENIZER_DOWNLOAD = "1"
tokenflow init --generate-prompt --force
```

服务商至少需要支持约 `20,000 + 10,001` Token 的上下文，还要为消息包装和系统 Token 留出空间。长文本请求通常需要较长时间，建议先使用较小的 `max_requests` 和 `concurrency` 验证服务商限制。

## OpenAI-compatible 要求

TokenFlow 当前使用以下协议：

```text
GET  {base_url}/models
POST {base_url}/chat/completions
```

请求主体使用 `model`、`messages`、`stream`、`max_tokens`、`temperature`，并请求流式 usage。服务商至少需要支持：

- Bearer API Key
- `chat/completions`
- SSE 流式响应
- OpenAI 风格的 `data` 模型列表或等价模型列表响应
- 最终 usage 中的 `prompt_tokens`、`completion_tokens`、`total_tokens`

如果服务商只支持 `max_completion_tokens`，或对 `stream_options`、额外字段有不同要求，需要通过 `extra_body` 或修改客户端请求协议适配。`model`、`messages`、`stream` 和 `stream_options` 不能通过 `extra_body` 覆盖。

## 日志与安全

每次运行会在 `logs/` 写入 JSONL 日志，记录请求状态、usage、费用、耗时和错误信息，不保存模型回答正文。

API Key 可能存在于 `models.json`，请注意：

- 只使用你有权限使用的 API。
- 不要提交 `models.json`、`.env` 或包含 Key 的日志。
- 不要把真实 Key 粘贴到公开 issue、聊天记录或代码中。
- 如果 Key 已经暴露，应立即在服务商侧撤销并重新生成。
- 正式使用前先降低 `max_requests`、`concurrency` 和 `max_tokens`。

## 从源码开发

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q tokenflow_cli
python -m build
```

测试使用本地 mock transport，不会向真实 API 发起请求。

发布前可以先检查构建产物：

```powershell
python -m twine check dist/*
```

上传到 TestPyPI：

```powershell
python -m twine upload --repository testpypi dist/*
```

## 许可证

MIT License。详见 [LICENSE](LICENSE)。
