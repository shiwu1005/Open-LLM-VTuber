# 联网 / 网络 API 三种方案

> 你是"都给我方案"，所以三种都写全，你按需挑。
> 每种都给了**可直接粘贴的配置**和**验证方法**。

---

## 先说结论：项目已经内置了什么

Open-LLM-VTuber **本来就能联网** —— 它内置了两个 MCP 服务器，你现在的配置里**已经启用**：

```json
"time":       { "command": "...mcp-server-time.exe"       }   // 查时间
"ddg-search": { "command": "...duckduckgo-mcp-server.exe" }   // DuckDuckGo 联网搜索
```

所以「让它上网查东西」这个能力你**已经有了**，不需要装任何东西。可以直接试试对它说：

> "帮我搜一下今天有什么 AI 新闻"

如果它真的去搜了并念出结果，那方案 B 你只需要**换一个更好用的搜索后端**（见下）。

---

## 方案 A：接入在线大模型 API（不用本地模型也能跑）

**适合**：想省显存、想让角色更聪明、想在笔记本/低配机器上也能用。

### 提供方现成支持

代码里已经内置了这些 provider 键名（`config_manager/stateless_llm.py`）：

| provider 键名 | 服务商 | 默认 base_url |
|---|---|---|
| `deepseek_llm` | DeepSeek | `https://api.deepseek.com/v1` |
| `zhipu_llm` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4/` |
| `openai_llm` | OpenAI | `https://api.openai.com/v1` |
| `gemini_llm` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `mistral_llm` | Mistral | `https://api.mistral.ai/v1` |
| `groq_llm` | Groq | `https://api.groq.com/openai/v1` |
| `openai_compatible_llm` | **任何** OpenAI 兼容接口 | 自己填 |

> **通义千问 / 阿里百炼、Kimi、硅基流动、火山方舟**等没列出来，但都兼容 OpenAI 格式，
> 直接填进 `openai_compatible_llm` 就能用。

### 配置示例（以 DeepSeek 为例）

在 `conf.yaml` 里找到 `llm_configs`，填上 key：

```yaml
      deepseek_llm:
        llm_api_key: 'sk-你的DeepSeek密钥'   # ← 换成你的
        model: 'deepseek-chat'
        temperature: 0.8
```

然后把角色指向它：

```yaml
  agent_config:
    agent_settings:
      basic_memory_agent:
        llm_provider: 'deepseek_llm'        # ← 从 lmstudio_llm 改成这个
```

### 配置示例（通义千问，走 openai_compatible_llm）

```yaml
      openai_compatible_llm:
        base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        llm_api_key: 'sk-你的百炼密钥'
        model: 'qwen-plus'                  # 或 qwen-max / qwen-turbo
        temperature: 0.8
        max_tokens: 1024                    # 推理类模型建议保留这一项
```

### 💡 最好的用法：本地 + 在线 随时切换

**不用改配置就能切。** 因为 LLM 设置写在**角色文件**里，而角色可以在前端随时切换。
做法是复制两份角色文件：

```
characters\xiaoxing.yaml          ← llm_provider: 'lmstudio_llm'   （本地，省钱）
characters\xiaoxing-cloud.yaml    ← llm_provider: 'deepseek_llm'   （在线，聪明）
```

两份的 `conf_name` 改成不同名字（如「小星(本地)」「小星(在线)」），
前端右上角一点就切。**人设完全一样，只有大脑不同。**

> 注意：`conf_uid` 必须唯一，第二份要改成 `xiaoxing_002`。

---

## 方案 B：换更好的搜索后端

内置的 `ddg-search` 是免费的、不需要 key，但它是**网页抓取**，稳定性和结果质量一般。
想更稳就用带 API key 的搜索服务。

### 推荐：Tavily（专为 AI 设计，有免费额度）

1. 注册拿 key：<https://tavily.com/>
2. 装：
   ```powershell
   uv tool install tavily-mcp
   ```
   （如果包名不对，`uv tool search tavily` 查一下实际名字）
3. 在 `mcp_servers.json` 里加：

```json
"tavily": {
  "command": "C:\\Users\\<你的用户名>\\.local\\bin\\tavily-mcp.exe",
  "args": [],
  "env": { "TAVILY_API_KEY": "tvly-你的密钥" }
}
```

4. 在角色 YAML 的 `mcp_enabled_servers` 里加上 `- 'tavily'`，**重启服务**。

### 备选：Brave Search

同样思路，`env` 里填 `BRAVE_API_KEY`。申请：<https://brave.com/search/api/>

> ⚠️ **MCP 服务器名和包名我不能替你确认**——你选定后告诉我，我先验证包真实存在、能跑起来，
> 再把配置写进文件。别照抄网上教程的包名，很容易是错的或已废弃。

---

## 方案 C：让角色调用你自己的 HTTP 接口

**适合**：天气、智能家居、你自己的服务、任何有 URL 的接口。

我写了一个**通用的 HTTP 桥接 MCP 服务器**给你，见
[`mcp_http_bridge.py`](../extras/mcp_http_bridge.py)。
它的好处是**加接口不用写代码，改一个 JSON 就行**。

### 用法

1. 复制文件到项目里：
   ```
   C:\work\Open-LLM-VTuber\tools\mcp_http_bridge.py
   ```

2. 建一个接口清单 `tools\http_tools.json`，例如查天气：

```json
{
  "tools": [
    {
      "name": "get_weather",
      "description": "查询指定城市的当前天气",
      "url": "https://api.example.com/weather",
      "method": "GET",
      "params": {
        "city": { "type": "string", "description": "城市名，如 北京", "required": true }
      },
      "query": { "city": "{city}", "key": "你的API_KEY" }
    }
  ]
}
```

3. 在 `mcp_servers.json` 里注册：

```json
"my-api": {
  "command": "C:\\work\\Open-LLM-VTuber\\.venv\\Scripts\\python.exe",
  "args": ["C:\\work\\Open-LLM-VTuber\\tools\\mcp_http_bridge.py",
           "C:\\work\\Open-LLM-VTuber\\tools\\http_tools.json"]
}
```

4. 在角色 YAML 的 `mcp_enabled_servers` 里加 `- 'my-api'`，重启服务。

之后就能对它说「帮我查一下北京今天天气」，它会自动调用你的接口。

**支持的能力**：GET/POST、路径参数（`{city}`）、查询参数、请求头（放 API key）、
JSON body、超时、响应长度截断。

---

## 怎么验证联网功能真的生效

**看日志**。服务启动时会打印 MCP 连接情况，成功长这样：

```
MC: Dynamic tool construction complete.
Dynamically formatted tools - OpenAI: 5, Claude: 5
```

`5` 就是工具数量。你加了新服务器后这个数字应该变大。
如果仍卡在旧的数字，说明新服务器**没连上**（回去看日志里的报错）。

**测试话术**（能强制它用工具）：

- 搜索类："搜索一下 xxx 最新消息"
- 时间类："现在几点了"
- 自定义接口："帮我查一下 xx"

---

## 三个方案的选择建议

| 你的目标 | 选哪个 |
|---|---|
| 就想让它上网查资料 | **方案 B**（或直接用内置 ddg-search） |
| 想省显存 / 提升回答质量 | **方案 A** |
| 要接自己的服务 | **方案 C** |
| 想同时要 | A + B 可以共存，互不冲突 |

**方案 A 和 B/C 不冲突**：A 换的是"大脑"，B/C 加的是"工具"。
你可以用在线 DeepSeek 当大脑，同时挂搜索和自定义接口当工具，这是最强组合。

---

## 安全提醒

1. **API key 不要提交到 GitHub。** 我已经把 `conf.yaml` 和 `mcp_servers.json` 写进 `.gitignore` 了，
   但你要确保**确实没有**用 `git add -f` 强行加进去。
2. **上传前扫一遍**：
   ```powershell
   git status --short
   git grep -n -i -E "sk-|api[_-]?key|tvly-|bearer " -- . ':!*.md'
   ```
3. 万一泄漏了 key，**立刻去服务商后台作废重发**——GitHub 上的 key 几秒内就会被爬走。
