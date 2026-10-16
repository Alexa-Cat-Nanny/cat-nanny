# Cat Nanny 基建说明（团队内部）

这份文档说明 `cat-nanny` 里每个部分是做什么的、一次对话怎么走完、AWS 上有什么，以及怎么在本地和 AWS 上跑起来。
仓库的 README 和 `docs/aws-integration.md` 是英文的，给评审看；这份是给团队自己看的。

## 1. 来龙去脉

- `CatNannyFramework` 是产品原型：FastAPI + SQLite + 网页模拟器，对话逻辑（追问、"不确定"、口误纠正、照片、七天报告）都在里面，但没有 MCP、没有 AWS。
- 比赛里接不上真的 Alexa+（MCP Toolkit 是私有预览，FAQ 明确允许用自己的网页充当 Alexa+）。
- 所以这个仓库把原型**重做成 MCP 服务器**，部署在 AWS 上；网页模拟器保留原来的界面，但数据全部通过 MCP 走。规则抽成独立的开源包 `cat-health-signals`。

三块的关系：

| 部分 | 位置 | 作用 |
|---|---|---|
| MCP 服务器 | `cat-nanny/server/` | 产品本体，部署在 Lambda 上。7 个工具，按 Alexa+ 文档的请求格式写，以后有权限可以直接接真机 |
| Alexa+ 模拟器 | `cat-nanny/simulator/` | 扮演 Alexa+ 的网页：听你说话 → Bedrock 大模型决定调哪个工具 → 作为 MCP 客户端调服务器 → 读出回复 → 在模拟的 Echo Show 上渲染界面 |
| 规则包 | `cat-health-signals/` | 纯业务逻辑：字段定义、追问、危险信号、规律。不碰数据库、不碰网络 |

## 2. 一次对话怎么走完

以"Alexa, Mochi just threw up"为例：

```
浏览器（simulator/sim/static/chat.js）
  │ 1. 语音识别成文字，连同"谁在说话"、当前选中的猫发给模拟器后端 POST /api/turn
  ▼
模拟器后端 simulator/sim/
  │ 2. mcp_bridge.py 作为 MCP 客户端连上服务器：initialize 握手（协议 2025-11-25）→ tools/list
  │ 3. agent.py 把 7 个工具的描述和参数格式交给 Bedrock（Converse API），连同这句话
  │ 4. 大模型决定：log_observation(cat="Mochi", event_type="vomit", details={}, reporter="Alex")
  ▼
API Gateway → Lambda：MCP 服务器 server/cat_nanny/
  │ 5. app.py 检查 Origin 和 Bearer token
  │ 6. tools.py：校验 details（严格 schema）→ 存到 DynamoDB → 取这只猫最近 60 天历史 → cat-health-signals.assess()
  │ 7. 返回 structuredContent：event_id、observation、follow_up、red_flags、pattern_note、photo_upload_url、assistant_guidance
  ▼
模拟器后端
  │ 8. 把结果交回大模型，大模型按 assistant_guidance 说成人话："Logged for Mochi. Was it food, a hairball, liquid, or foam?"
  │ 9. 如果工具带界面（get_vet_summary），读取 ui:// 资源的 HTML 一起返回
  ▼
浏览器
    10. 朗读回复；记录卡片显示这条观察；Echo Show 区域用沙箱 iframe 渲染界面（MCP Apps 的 postMessage 协议）；
        "MCP calls this turn" 面板列出本轮每次工具调用的参数和返回
```

关键行为（都来自原型，并有测试覆盖）：
- 用户回答"food"时，大模型带上上一步的 `event_id` 再调一次 `log_observation`，细节补进**同一条**记录。
- 用户说"不确定"：`unknown_fields=["has_blood"]`，这个问题以后不再问。
- "no actually it is for Tofu"：调 `correct_cat`。Tofu 没档案时服务器返回 `needs_confirmation`，大模型先问；用户说 yes 再调一次带 `create_if_missing=true`。记录连同照片一起搬过去，保留 id，并写一条纠正审计记录。
- 照片：大模型给 `photo_upload_url`（显示在屏幕上，不发短信邮件）。手机打开后，页面向服务器要一个"上传目标"（S3 预签名 PUT 地址），直接把图片传到 S3，再告诉服务器 complete。
- 危险信号：`red_flags[0].say_first` 永远先说（"Please call your vet today. ..."）。

## 3. 每个文件是做什么的

### MCP 服务器 `server/cat_nanny/`

| 文件 | 作用 |
|---|---|
| `tools.py` | 7 个 MCP 工具：`list_household`、`add_cat`、`log_observation`、`correct_cat`、`get_care_log`、`get_photo_upload_link`、`get_vet_summary`（带 MCP Apps 界面）。只做编排：校验 → 存取 → 调规则 → 组装返回。每个返回都带 `assistant_guidance`；给用户看的错误用 `ToolError` |
| `store.py` | 存储接口 + `MemoryStore`（本地/测试）、`DynamoStore`（线上）。实体来自原型的 SQLite 表，改成单表设计，见文件顶部的表 |
| `photos.py` | 照片存储接口 + `LocalPhotoStorage`（本地文件）、`S3PhotoStorage`（预签名 PUT/GET） |
| `app.py` | HTTP 入口：`/mcp`、`/healthz`、照片页面 `/photos/upload` 及其接口。无状态 + 直接返回 JSON；Origin 不合法回 403；token 错误回 401（按 Alexa+ 文档不带 WWW-Authenticate）；记住每个请求自己的域名，用来拼手机打开的链接 |
| `handler.py` | Lambda 入口。服务器对象每个容器只建一次，每次调用套一个新的 HTTP 外壳（SDK 的会话管理器只能启动一次） |
| `config.py` | 所有环境变量（见 `.env.example`）。默认内存存储 + 本地照片，本地不需要 AWS |
| `demo_data.py` | 演示家庭：Mochi、Jeremy，照护者 Alex、Sam，三条观察 |
| `ui/vet_summary.html` | 兽医摘要界面（MCP Apps），单文件；小屏幕自动用紧凑布局 |
| `ui/upload.html` | 手机上传照片的页面 |

### 模拟器 `simulator/sim/`

| 文件 | 作用 |
|---|---|
| `mcp_bridge.py` | MCP 客户端：initialize 握手、Streamable HTTP、Bearer token、声明支持 MCP Apps；本地地址不走系统代理 |
| `agent.py` | "Alexa 的大脑"。`BedrockAgent` 用 Converse API 做工具调用循环；`ScriptedAgent` 是离线模式，只认几句固定的话 |
| `app.py` | FastAPI：`/` 页面、`/api/turn` 一轮对话、`/api/household`、`/api/care-log`、`/api/vet-summary`、`/api/add-cat`、`/api/observation`（侧边面板直接调只读工具，不经过大模型） |
| `static/index.html`, `app.css`, `product.css` | 原型的页面和样式（Shu Wang） |
| `static/chat.js` | 页面逻辑：语音识别和自动发送、朗读、记录卡片、照片卡片、MCP Apps 宿主端、调用面板、档案和报告对话框 |

### 规则包 `cat-health-signals/cat_health_signals/`

| 文件 | 内容 |
|---|---|
| `schemas.py` | **字段定义**（严格校验）：vomit 的 content / has_blood / episode_count / low_energy / minutes_after_meal；appetite 的 status / hours_since_food。服务器把它写进工具描述，大模型就知道提取哪些信息 |
| `followup.py` | 每种事件的追问顺序和原话 |
| `redflags.py` | 五条危险信号规则，一条一个函数 |
| `patterns.py` | 7 天内第几次的提示；`find_patterns` 留给以后 |
| `models.py` | 数据结构，改字段名要和服务器一起改 |

**事件类型只有 vomit 和 appetite，不扩展。** 改规则不需要动服务器或基建的代码。

## 4. AWS 上有什么

`infra/template.yaml`（SAM）一次建好：

| 资源 | 配置要点 |
|---|---|
| API Gateway HTTP API | 路由 `ANY /{proxy+}`，输出 `McpUrl`（…/mcp） |
| Lambda | Python 3.13、x86_64、512MB、29 秒超时；环境变量全部由模板注入 |
| DynamoDB | 单表，`pk` + `sk`，按需计费 |
| S3 桶 | 私有、加密、禁止公开访问；CORS 允许手机预签名 PUT；Lambda 只能读写 `households/*` |
| CloudWatch 日志组 | 保留 14 天；每次工具调用写一行 JSON |
| AWS Budgets | 当月费用超过 10 美元的 80% 发邮件 |

模板外手动做的：
1. **Bedrock**：模型目录里确认 `us.anthropic.claude-sonnet-4-5-20250929-v1:0` 可调用（要用 `us.` 开头的推理配置文件 id），本机凭证有 `bedrock:InvokeModel`。模拟器在本地跑，用的是本机凭证。
2. **AWS CLI + SAM CLI**：`aws configure`。密钥只放本机，不进 `.env`，不进 git。
3. **token**：`openssl rand -hex 24` → `.env` 的 `MCP_AUTH_TOKEN`。

## 5. 跑起来

**本地（不需要 AWS）**

```powershell
# 两个仓库放在同一个父目录下；在 cat-nanny 里
Copy-Item .env.example .env
.\scripts\dev.ps1 setup     # 安装依赖，并以可编辑模式安装 ..\cat-health-signals
.\scripts\dev.ps1 test      # 55 个测试
.\scripts\dev.ps1 server    # 窗口 1：http://127.0.0.1:8000/mcp，自带演示家庭
.\scripts\dev.ps1 sim       # 窗口 2：打开 http://127.0.0.1:8080
```

macOS/Linux 用 `make setup|test|server|sim`。

离线模式能识别的话（按顺序试）：`Mochi just threw up` → `it was food` → `not sure` → `send me the photo link` →
`no actually it is for Tofu` → `yes` → 切到 Sam：`Mochi hasn't eaten since yesterday` → `show the summary for the vet`。

**接上 Bedrock**：`.env` 填 `BEDROCK_MODEL_ID` 和 `AWS_REGION`，重启 sim。页面右上角显示正在用的模型。

**部署到 AWS**

```powershell
# .env 里填 AWS_REGION、BUDGET_EMAIL、MCP_AUTH_TOKEN
.\scripts\dev.ps1 deploy    # 打包 Lambda（Linux wheel，不用 Docker）并 sam deploy，最后打印输出
# .env 里填 TABLE_NAME=<输出里的 TableName>
.\scripts\dev.ps1 seed      # 可选：导入演示家庭
# .env 里把 MCP_URL 改成输出里的 McpUrl，重启 sim
.\scripts\dev.ps1 logs      # 实时看工具调用日志
```

## 6. Alexa+ 文档里和我们相关的信息

| 主题 | 文档怎么说 | 我们怎么处理 |
|---|---|---|
| 传输 | 必须 Streamable HTTP，远程 HTTPS 地址 | 已满足 |
| 协议版本 | 概览写 2025-11-25，示例握手用 2025-03-26 | 服务器接受 2024-11-05 到 2025-11-25；模拟器用 2025-11-25 |
| 用户身份 | 工具调用里**不传**用户、家庭、说话人 ID；账号关联（OAuth）后才能识别 | 演示版固定一个 `HOUSEHOLD_ID`；`reporter` 由模拟器的"谁在说话"传入 |
| 认证 | OAuth；未认证回 401 且不带 WWW-Authenticate | 演示用 Bearer token，401 行为按文档实现 |
| MCP Apps | Alexa+ 从**工具结果**的 `_meta.ui.resourceUri` 读界面；MCP Apps 规范放在**工具定义**里 | 两处都写了 |
| 界面 | 单个 HTML、不依赖 localStorage | 兽医摘要按这个做的，小屏幕有紧凑布局 |
| 延迟 | 建议 500ms 以内 | 热启动远低于此；Lambda 冷启动 1–2 秒，演示前先调一次预热 |
| 主动通知 | 没有服务器推送到 Alexa app 的接口 | 不做家人通知；危险信号在对话里先说 |
