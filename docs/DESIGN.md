# 云信互联 · 二期设计文档（v2）

> 状态：已确认（2026-08-15 讨论定稿）
> 决策：① 云端 UI 用 AstrBot 插件页面；② 工作流编辑做 L2 可视化编辑器 + L3 真 ComfyUI 远程桌面；③ 本期支持多台本地机（多代理）；④ 隧道内置 TLS。

## 1. 目标与总体架构

```
┌─────────────── AstrBot 云端（一个插件实例）───────────────────────────────┐
│                                                                          │
│  WebUI 插件页面（继承 WebUI 登录鉴权）                                      │
│  ┌─────────┐ ┌──────────┐ ┌──────────────┐ ┌───────┐ ┌────────┐         │
│  │ 概览    │ │ 工作流    │ │ 可视化编辑器  │ │ 预设   │ │ 审计   │         │
│  │ SSE 实时 │ │ 列表+图预览│ │ (LiteGraph)  │ │ 表单   │ │ 日志   │         │
│  └────┬────┘ └────┬─────┘ └──────┬───────┘ └───┬───┘ └───┬────┘         │
│       └───────────┴───────────────┴─────────────┴─────────┘             │
│                     context.register_web_api() 后端接口                    │
│                                 │                                        │
│   隧道服务端（wss://0.0.0.0:8468，内置 TLS）                                │
│   · 多代理连接表（agent_id → 连接）                                        │
│   · 配对码签发 / 防爆破 / 审计                                              │
│   · OpenAI 兼容代理 /v1/*（每代理可选目标）                                  │
│   · L3 远程桌面 ticket 会话                                                │
└───────┬───────────────┬──────────────────────────┬───────────────────────┘
        │ wss 长连接      │ wss 长连接               │ wss 长连接
┌───────┴───────┐ ┌─────┴─────────┐       ┌───────┴─────────┐
│ 家里的电脑      │ │ 单位的电脑     │  …    │ 第 N 台          │
│ YunxinAgent.exe│ │ (agent_id=u2) │       │ (agent_id=uN)   │
│ (agent_id=h1)  │ └───────────────┘       └─────────────────┘
└───────────────┘
```

原则：**一个插件实例连接多台本地机**；每台机器一个 `agent_id`（配对时签发）+ 一个人类可读名字；云端 UI 所有操作先选目标机器再操作；兼容单机（只连一台时默认选中）。

## 2. 多代理协议设计（v2 协议）

### 2.1 hello 消息（本地 → 云端）

```json
{
  "type": "hello",
  "v": 2,
  "agent_id": "h1",              // 配对时签发的稳定 ID（本地配置 agent_config.json）
  "name": "家里的电脑",            // 人类可读名字，可改
  "version": "2.0.0",            // 代理版本，用于兼容性提示
  "data": { ... }                // 原 collect_info() 内容不变
}
```

### 2.2 请求寻址（云端 → 本地）

```json
{ "type": "request", "v": 2, "id": "rid", "agent": "h1", "service": "comfyui", "payload": {...} }
```

- `agent` 缺省 → 使用云端配置的 `default_agent`；仅一台在线时自动选它；
- 插件侧 `self._agents: dict[str, AgentConn]`，`AgentConn` 持有 ws / pending 表 / chunk 回调 / hello 信息 / 连接时间；
- 同一 agent_id 重复拨入 → 顶掉旧连接（与现在单机逻辑相同，但按 agent 粒度）；
- 请求响应消息格式不变（id 路由），断开只失败该 agent 的等待请求。

### 2.3 指令与 LLM 工具

- 指令：`/remote agents`（列出所有机器与状态）；其余指令加可选机器参数：`/remote status [机器名]`、`/remote wf list [机器名]`、`/remote wf run <预设> [机器名] [参数]`；
- LLM 工具：所有 `remote_*` 工具新增**可选参数 `agent`**（enum 为在线机器名列表，工具描述里写明"不填=默认机器"），保持工具数量不爆炸。

### 2.4 新增服务

| 服务 | 动作 | 说明 |
| --- | --- | --- |
| `workflows` | `list`（已有）/ `read` / **`save`** / `delete` / `backup` | save 前自动备份 `<名>.bak.<时间戳>.json`；路径全部约束在 workflows 根目录内（拒绝 `..`、绝对路径） |
| `comfyui` | 新增 `cancel` / `clear_queue` | 取消指定 prompt_id / 清空队列（对应 ComfyUI /interrupt、/queue 删除） |
| `comfyui` | 新增 `ui_proxy` | L3 远程桌面：把本地 ComfyUI 前端 HTTP 请求反代回云端（仅在内网-隧道内使用，带 ticket 校验） |

## 3. 云端控制台（AstrBot 插件页面）

目录结构（插件内）：

```
pages/
├── console/            # 概览：连接拓扑、GPU 显存、队列、实时日志（subscribeSSE）
├── workflows/          # 工作流列表 + 图预览（L1）+ 打开编辑器入口
├── editor/             # L2 LiteGraph 可视化编辑器
├── presets/            # 预设表单：从工作流点选参数生成 workflow_presets 配置
├── history/            # 生成历史画廊（图片/视频缩略图 + 下载）
├── agents/             # 机器管理：配对码、改名、踢下线
└── audit/              # 审计日志
```

后端接口（`context.register_web_api`，全部继承 WebUI 登录鉴权，`request.username` 可用）：

| 路由 | 方法 | 功能 |
| --- | --- | --- |
| `overview/snapshot` | GET | 所有代理的聚合状态（连接/GPU/队列/版本） |
| `overview/events` | SSE | `stream_response` 推送：连接变化、任务进度、完成通知 |
| `agents/list` / `agents/pair` / `agents/rename` / `agents/kick` | GET/POST | 机器管理（pair 生成一次性 6 位配对码） |
| `workflows/list` / `read` / `save` / `delete` / `convert_preview` | GET/POST | 经隧道转发到目标代理；`convert_preview` 返回 UI→API 转换预览供编辑器使用 |
| `presets/get` / `save` | GET/POST | 读写 `workflow_presets` 配置；保存后**热更新 LLM 工具**（unregister 旧工具 → 重建 FunctionTool） |
| `run` | POST | 云端一键测试运行（结果图片/视频 base64 回显，或写入 history） |
| `history/list` / `file/<id>` | GET | 生成历史与产物下载（`file_response`） |
| `object_info` | GET | 经隧道取目标机器 `/object_info`（编辑器建节点面板用） |
| `audit/list` | GET | 审计日志查询 |

技术要点：
- LiteGraph.js **打包进 `pages/editor/assets/`**（避免 iframe 下 CDN 被 CSP 拦截）；
- 页面静态文件都在插件目录内，插件重载即可迭代；静态资源改动刷新页面即可。

## 4. L2 可视化编辑器设计

1. **渲染**：LiteGraph 载入 UI 格式工作流（天然同格式）；节点定义来自目标机器的 `/object_info`（经隧道，缓存 10 分钟）；
2. **编辑**：拖节点（按分类/搜索）、连线、改 widget 值、删除——LiteGraph 自带能力；
3. **占位符**：右键或双击任意 widget 值 → 「设为参数」弹窗 → 生成 `__TOKEN__` 并登记进当前预设草稿；
4. **保存**：`workflows/save` 写回本地（自动 `.bak.<时间戳>.json` 备份），可选「另存为 API 格式」（云端用 `convert_preview` 转换后保存）；
5. **降级策略**：含 Reroute/自定义节点渲染异常时给出提示并切到 JSON 编辑器兜底；
6. **安全**：编辑器所有写操作走同一审计记录（谁、改了哪个文件、备份名）。

## 5. L3 真 ComfyUI 远程桌面

受限 iframe 大概率不允许跨域嵌套第三方页面，所以 L3 做成**跳出式远程桌面**：

1. 插件页面点「打开 ComfyUI 远程桌面」→ 后端签发一次性 ticket（60 秒有效、单次使用、绑定 session）；
2. 浏览器跳转 `https://云端:8468/comfyui/?ticket=xxx`（隧道服务端对外，TLS 同隧道）；
3. 隧道服务端校验 ticket → 所有请求经 `comfyui.ui_proxy` 转发到目标机器的 ComfyUI 前端；
4. 会话 60 分钟超时自动失效；所有路径仅白名单（ComfyUI 静态资源与 API），禁止访问其他路径。

> 备选：若插件页面 iframe 的 CSP 实测允许嵌套同源外的 iframe，则直接在 editor 页内嵌远程桌面 iframe，体验更好（实现时验证，两个入口都留）。

## 6. 安全设计（二期全部落地）

| # | 措施 | 实现 |
| --- | --- | --- |
| 1 | token 禁止为空 | 插件首启自动生成 32 字节随机 token 写入配置；本地 GUI 同步提示 |
| 2 | 内置 TLS | 隧道服务端 `TCPSite(ssl=...)`；配置 `tls_cert`/`tls_key`；未提供证书时**自动生成自签名证书**（`cryptography` 库，插件要求 AstrBot ≥4.24 自带依赖）并存于插件数据目录 |
| 3 | 证书指纹校验 | 自签场景代理端配置 `cert_fingerprint`（sha256），防中间人；GUI 加对应字段 |
| 4 | 配对码机制 | `agents/pair` 签发一次性 6 位码（5 分钟过期）→ 代理首连时用码换取 `agent_id` + 长期密钥（`/pair` 接口限流） |
| 5 | 防爆破 | 认证失败按 IP 计数，指数退避（1s→2s→…→封 10 分钟） |
| 6 | 审计 | 所有隧道请求（服务/动作/耗时/结果）、UI 写操作、shell 命令 → 结构化审计日志（JSONL）+ audit 页面 |
| 7 | Shell 白名单 | 配置 `shell_allowed_patterns`（正则列表，如 `^nvidia-smi`、`^tasklist`），非空时只放行匹配命令；默认空=沿用双开关全开 |
| 8 | 路径防护 | 工作流读写统一约束到 workflows 根目录内（拒绝 `..`/绝对路径/符号链接逃逸），单测覆盖 |
| 9 | 最小暴露 | 隧道服务端仅暴露：`/ws`、`/v1/*`（OpenAI 代理）、`/pair`、`/comfyui/*`（ticket 会话）、`/healthz`（无敏感信息） |
| 10 | 版本协商 | 协议消息带 `v` 字段；版本不兼容时两端明确报错而不是静默错乱 |

## 7. 本地 GUI 升级点

- 配置表单新增：`agent_name`、`agent_id`（配对后自动填入，只读）、TLS 相关（`verify_ssl`、`cert_fingerprint`）、`shell` 白名单说明；
- 首连流程：填写云端地址 → 点「配对连接」→ 输入云端页面显示的 6 位码 → 自动完成密钥交换；
- 状态区不变，多代理信息（本机 ID/名字）显示在标题栏。

## 8. 实施阶段与验收

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| **P1 协议与安全底座** | 多代理协议 v2、内置 TLS+自签、配对码、防爆破、审计、shell 白名单、路径防护；本地 GUI/CLI 适配 | 三台模拟代理并发在线；wss 握手+指纹校验通过；错误 token 爆破被退避；单测+冒烟全绿 |
| **P2 云端控制台** | pages/console、workflows(L1 预览)、presets 表单+工具热更新、history、agents、audit | WebUI 内完成：看状态→选机器→跑一张图→画廊回显 |
| **P3 L2 编辑器** | LiteGraph 完整编辑、object_info 面板、占位符登记、保存+备份、JSON 兜底 | 云端拖拽改一个工作流→保存→`/remote wf run` 立刻可用 |
| **P4 L3 远程桌面** | ticket 会话 + ui_proxy + 页面入口 | 云端浏览器完整操作本地 ComfyUI 界面 |
| **P5 体验收尾** | 队列管理（取消/清空）、生成完成 QQ 通知、统计图表、文档与示例预设库 | 按使用反馈迭代 |

## 9. 风险与待验证项

1. **插件页面 iframe 的 CSP 边界**：LiteGraph 是否受 sandbox 限制（需 `allow-scripts` 等）、能否嵌 L3 iframe——P2 第一天做 spike 验证，决定 L3 内嵌还是跳出；
2. **LiteGraph 对复杂自定义节点的渲染**：部分节点 UI 定义异常可能导致图渲染错位——提供 JSON 兜底与"仅渲染标准节点"开关；
3. **自签证书在 aiohttp 的兼容**：`cryptography` 生成 SAN 含 IP 的证书；代理端指纹校验覆盖 wss 首连；
4. **工具热更新**：AstrBot 的 `unregister_llm_tool` + `add_llm_tools` 重注册链路需要实测（P2 内验证，失败则退回"保存后提示重载插件"）；
5. **多代理下的 ComfyUI 并发**：多台机器各自队列独立，协议无需变更；云端 UI 需要防"一键全跑"误操作（二次确认）。

## 10. 与一期的关系

- 一期全部功能保持兼容：单机模式、`/remote` 指令、`remote_workflow_*` 工具、OpenAI 代理、本地 GUI 与看板；
- 协议升级向后兼容：v1 消息无 `agent`/`v` 字段 → 云端自动补默认机器与 v=1 语义；
- 目录名、插件名、品牌名不变。
