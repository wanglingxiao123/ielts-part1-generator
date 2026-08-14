# IELTS Listening Part 1 材料生成系统

这是一个面向 IELTS 出题人员的材料生产与审核系统。用户选择场景和数量后，系统生成候选
听力对话、十个信息点和对应题目，分别执行程序校验与独立 AI 盲审，再将完整套件逐套返回浏览器。

系统的边界：

- **自动生成流程交付完整文字套件**：对话稿、十个信息点、自然题组中的十道题、答案与证据、校验结果和
  审核结果。
- **材料和题目分别审核**：材料审核看不到生成者的 `blueprint`；题目审核看不到答案键，再由 Python
  将独立重建结果交叉检查。
- **交付后可从题目批注发起修订**：系统先把意见分类为无需修改、局部修题、重新规划题组或修改材料，
  再生成不可变的新版本；当前采用版本不会被自动覆盖。
- **不在生成流程中合成音频**：用户主动生成音频时，系统才按需调用 Polly；提交审核不会自动
  触发音频合成。

> 📘 **出题人员请先阅读 [`USER_GUIDE.md`](USER_GUIDE.md)，了解材料生成、审阅、评价和版本采用的
> 完整操作流程。**
>
> 本 README 主要面向开发、部署和运维人员。

## 1. 项目目录

下面列出版本控制中的主要目录和入口。测试夹具、截图脚本等同类文件只展示其所在目录，避免用
文件清单淹没主线。

```text
.
├── README.md                         # 技术、部署和运维手册
├── USER_GUIDE.md                     # 出题人员使用说明
├── pytest.ini                        # Python 测试发现配置
├── config/
│   └── scenarios.yaml                # 场景分类、角色和场景说明
├── material/
│   └── Part1_选材命制规范.md          # IELTS Part 1 选材与命制规范
├── skills/                           # 生成、审核与可行性检查 Skill；池划分就是盲审边界
│   ├── generate/                     # 生成 Agent 的池
│   │   ├── generate-listening-part1/
│   │   │   ├── SKILL.md              # 材料生成任务入口规范
│   │   │   ├── references/specification.md
│   │   │   ├── schemas/              # material、blueprint JSON Schema
│   │   │   └── scripts/validate_part1.py
│   │   └── generate-questions-part1/ # 题目生成、Schema 与 validator
│   ├── audit/                        # 审核 Agent 的池；不含 blueprint schema
│   │   ├── audit-listening-part1/
│   │   │   ├── SKILL.md              # 材料审核任务入口规范
│   │   │   ├── references/audit-rubric.md
│   │   │   ├── schemas/audit.schema.json
│   │   │   └── scripts/audit_metrics.py
│   │   └── audit-questions-part1/    # 不看答案键的题目审核
│   ├── feasibility/                  # 出题前检查十个预选答案点是否可听出、唯一且可填空
│   └── shared/                       # 两侧都不激活的离线工具
│       ├── cross_check.py            # 生成标注与盲审结果交叉检查
│       └── tests/                    # Skill 契约回归测试
├── backend/                          # AgentCore Runtime
│   ├── Dockerfile                    # Runtime ARM64 镜像
│   ├── pyproject.toml                # Python 依赖与 pytest 配置
│   ├── app.py                        # Runtime action 入口
│   ├── request.py                    # 生成请求解析
│   ├── agents.py                     # 两类 Strands Agent、Skill 池和工具权限
│   ├── sandboxed_metrics.py          # 在 AgentCore Code Interpreter 中计算审核指标
│   ├── model/provider.py             # Bedrock Mantle 模型适配
│   ├── steps/
│   │   └── agent_steps.py            # generate、audit、revise 的输入输出边界
│   ├── deterministic/                # 校验、指标、锚点与交叉检查包装
│   ├── orchestration/
│   │   ├── loop.py                   # 单套材料 Agent Loop
│   │   ├── question_loop.py          # 单套题目生成、审核与最多两轮修改
│   │   ├── manual_question_revision.py # 题面级批注修订
│   │   ├── manual_question_replan.py # 保持材料不变的完整重命题
│   │   ├── manual_material_revision.py # 修改材料并重建蓝图与十题
│   │   ├── delivery.py               # 完整套件、换材料、断点与精确数量交付
│   │   ├── batch.py                  # 旧的仅材料 action 与测试入口
│   │   ├── candidate_store.py        # 候选材料注册
│   │   └── publish.py                # 选稿、试听和异步音频任务
│   ├── scripts/                      # 部署、冒烟和 CI 门禁
│   ├── tests/                        # 后端测试
│   └── docs/                         # 时延、模型接入和交接记录
├── audio_storage/                    # 按需音频与材料状态存储
│   ├── synthesize.py                 # Polly 逐轮合成
│   ├── state_store.py                # pending/approved 等状态迁移
│   ├── manifest.py                   # 音频完整性 manifest
│   ├── config/pronunciation.yaml     # 发音覆盖规则
│   └── tests/
├── web/                              # 面向浏览器的 FastAPI 服务
│   ├── Dockerfile                    # Web + 前端静态资源镜像
│   ├── app.py                        # API、静态文件与认证入口
│   ├── fanout.py                     # 每套一次 Runtime invocation，合并为 SSE
│   ├── runtime_client.py             # SigV4 AgentCore 客户端
│   ├── auth.py                       # 登录、session 和用户存储
│   ├── batch_store.py                # 批次持久化
│   ├── comment_store.py              # `_comments/` 单人批注存储与校验
│   ├── question_versions.py           # 修订请求与不可变版本的持久化/采用
│   └── tests/
├── frontend/                         # React + TypeScript + Vite 审阅界面
│   ├── src/
│   │   ├── api/                      # Web API 与 SSE 客户端
│   │   ├── auth/                     # 登录状态
│   │   ├── contracts/                # 从 Schema 生成的类型
│   │   ├── domain/                   # 展示与比较逻辑
│   │   ├── features/                 # 场景、进度、阅读、比较、音频
│   │   └── stores/                   # Zustand 状态
│   ├── public/config.json            # 运行时前端配置
│   ├── scripts/                      # codegen 与视觉检查脚本
│   ├── package.json                  # 依赖和 npm 命令
│   ├── vite.config.ts
│   ├── vitest.config.ts
│   └── tsconfig.json
├── deploy/
│   ├── config.sh                     # 共享资源名和部署参数
│   ├── provision.sh                  # S3、ECR、ECS、IAM、日志组
│   ├── runtime.sh                    # 创建/更新 AgentCore Runtime
│   ├── edge.sh                       # ALB、CloudFront、安全组
│   ├── service.sh                    # 构建 Web 镜像并创建 ECS 服务
│   ├── start.sh / stop.sh            # 日常启停
│   ├── status.sh                     # 资源状态与访问地址
│   └── teardown.sh                   # 拆除资源
└── docs/ui/                          # UI 原型
```

## 2. 系统架构

```mermaid
flowchart LR
    U["浏览器<br/>React"]
    CF["CloudFront<br/>HTTPS"]
    ALB["ALB<br/>HTTP"]
    WEB["ECS Fargate<br/>FastAPI Web"]
    RT["AgentCore Runtime<br/>生成与审核 Loop"]
    CI["AgentCore Code Interpreter<br/>隔离计算审核指标"]
    MODEL["Bedrock Mantle<br/>GPT-5.6"]
    S3["S3<br/>材料、题目、断点、批注、音频"]
    SSM["SSM<br/>Session Secret"]
    POLLY["Polly<br/>按需音频"]

    U --> CF --> ALB --> WEB
    WEB -->|"SigV4 调用"| RT
    WEB <--> S3
    SSM -. "任务启动时注入" .-> WEB
    RT --> MODEL
    RT --> CI
    RT <--> S3
    RT -. "用户试听/选稿后" .-> POLLY
```

### 2.1 每个组件负责什么

| 组件 | 职责 |
|---|---|
| 浏览器 | 选择场景、显示逐套生成进度、审阅原文和题目、提交题目批注、管理版本、生成音频或提交审核 |
| CloudFront + ALB | 提供稳定 HTTPS 地址，将动态请求转发到 Web 服务 |
| Web / ECS | 登录、批次历史、后台执行、修订版本、调用 Runtime，并把运行进度投影为 SSE |
| AgentCore Runtime | 创建 Strands Agent，执行材料与题目的生成、校验、盲审、修改和按需音频 action；`PUBLIC` 网络模式用于访问 AWS 公网服务 |
| AgentCore Code Interpreter | 在空白远程环境中运行审核指标脚本；只接收脚本和当前 `material` |
| Bedrock Mantle | 承载生成、审核和修改所需的 GPT-5.6 调用 |
| S3 | 保存候选材料、题目包、批次、断点、用户数据、个人批注、审核状态和按需生成的音频 |
| SSM | 保存 Web session 签名密钥，避免容器重启后用户集体掉线 |
| Polly | 仅在用户主动生成音频时合成英式语音 |

### 2.2 一次生成请求如何流转

1. 浏览器向 Web 服务提交场景和数量。
2. `web/fanout.py` 为每套材料向同一个已部署的 AgentCore Runtime 发起一次独立 invocation，
   每次使用不同的 `runtimeSessionId`，并按 `WEB_FANOUT_CONCURRENCY` 控制并发。
3. 每次 invocation 为一套结果执行第 3 节的 Agent Loop，依次完成材料、可行性预检和十道题。
4. Web 将多条 Runtime 流合并为一条 SSE；浏览器不必等待整个批次完成，可以逐套看到结果。
5. Web 同时把批次索引和结果保存到 S3，供刷新页面和历史查询使用。

`generate_sets` 使用 SSE，因此适用 AgentCore 的 60 分钟流式上限，而不是 15 分钟同步上限。
每套拥有独立的 Runtime invocation 和 55 分钟工作窗口；Web 使用独立线程池承载阻塞式 boto3
长连接，避免占满 FastAPI/anyio 默认线程池。

浏览器只是进度观察者，不拥有生成任务。关闭 SSE、刷新或离开页面不会关闭 Runtime body，也不会
取消批次或修订；Web 的后台 execution 持续消费结果并把终态写入 S3。重新打开页面后，前端从持久化
状态恢复。批次某个 slot 失败时，“补生成这一套”启动独立 refill execution，但成功结果回填原批次
原位置，不创建第二个用户可见批次。

### 2.3 音频为什么不在生成流程里

生成成功后不会调用 Polly。当前用户界面只有主动点击“生成音频”才会触发音频合成：

- **生成音频**：`preview_audio` 为某个候选材料启动异步合成，但不改变提交审核状态；
- **提交审核**：只记录用户选择的材料，不调用 Polly，也不会删除同场景的其他候选材料。

Runtime 立即返回 job id，浏览器通过 `audio_status` 轮询。合成完成后才能通过
`presign_audio` 获得限时播放地址。底层仍保留 `select` action 作为兼容入口，但当前“提交审核”
流程不会调用它。这样生成批次和提交审核都不会因为 30 到 45 次 Polly 请求而变慢。

### 2.4 网络与凭证边界

- 浏览器不持有 AWS 凭证，只调用同源 `/api/*`，登录态存放在 HttpOnly cookie。
- Web 使用 ECS task role 的临时凭证签名 `InvokeAgentRuntime`，没有长期 access key。
- Runtime 不直接面向浏览器。当前部署使用 AgentCore `PUBLIC` 网络模式，但调用托管 Runtime
  endpoint 仍需 SigV4 签名和 `bedrock-agentcore:InvokeAgentRuntime` 权限。
- S3 对象全部私有；音频通过预签名 URL 播放。
- CloudFront 禁用缓存和压缩。缓存可能串用不同用户的数据；压缩可能缓冲 SSE，使材料在末尾
  一次性出现。
- Web 每 15 秒发 SSE 心跳；CloudFront origin read timeout 为 60 秒，ALB idle timeout 默认为
  120 秒。三者共同保证长时间生成不会因为静默而断流。

### 2.5 鉴权与用户池

系统**没有使用 Cognito**。`web/auth.py` 将用户保存在
`s3://ielts-part1-materials-{account}/web/users.json`，使用 PBKDF2 密码摘要和由 SSM 密钥签名的
7 天 HttpOnly cookie；`ALLOWED_EMAIL_DOMAINS` 控制新用户注册。本地开发可回退到本地 JSON。
注册请使用 `@example.com` 邮箱。部署时应显式设置 `ALLOWED_EMAIL_DOMAINS`；未设置时
默认值 `*` 会允许任意邮箱域名注册，不适合直接用于公网环境。
这套方案面向小规模、受控用户使用，不提供邮箱验证、找回密码、MFA 或企业 SSO。

## 3. 生成流程（Agent Loop）

```mermaid
%%{init: {"flowchart": {"curve": "stepAfter", "nodeSpacing": 28, "rankSpacing": 42}}}%%
flowchart TB
    Q["生成请求"] --> F["Web 按套数拆分<br/>多套同时生成"]

    subgraph MATERIAL["模块一 · 听力材料生成"]
        direction LR
        M["听力材料"] --> G["生成 Agent<br/>选 Skill 并生成"]
        G --> V{"Python 校验"}
        V -- "错误，最多 3 次" --> G
        V -- "通过或次数用完" --> A["独立审核 Agent<br/>盲审原稿"]
        A --> X{"是否修改"}
        X -- "否" --> O["采用原稿"]
        X -- "是" --> R["全新生成 Agent<br/>修改原稿"]
        R --> C{"修改稿校验"}
        C -- "不通过" --> O
        C -- "通过" --> E["全新审核 Agent<br/>复评修改稿"]
        E --> B["原稿 / 修改稿择优"]
    end

    F --> M
    O --> P
    B --> P

    subgraph QUESTIONS["模块二 · 题目生成"]
        direction LR
        P{"预选答案点<br/>是否可出题"}
        P -- "通过" --> T["题目生成 Agent<br/>生成十道题"]
        T --> H{"Python 校验 +<br/>独立题目盲审"}
        H -- "需修改，最多 2 轮" --> J["全新题目生成 Agent<br/>定向修改"]
        J --> H
        H -- "可交付" --> D["交付完整套件<br/>材料 + 题目"]
        H -- "仍有硬问题" --> K{"同一材料<br/>重启题目阶段一次"}
        K -- "首次失败" --> T
        K -- "再次失败" --> W["更换候选材料<br/>返回上方材料模块"]
        P -- "材料不适合出题" --> W
    end

    D -. "交付后可选" .-> CM

    subgraph REVISION["模块三* · 人工评价与版本修订（可选）"]
        direction LR
        CM["提交题目批注*"] --> CL{"先分类<br/>不直接改稿"}
        CL -- "无需修改" --> NC["记录理由与引证<br/>版本不变"]
        CL -- "局部修题" --> QR["只修改锚定题目"]
        CL -- "重新命题" --> RP["确认后重规划蓝图<br/>重建完整十题"]
        CL -- "修改材料" --> MR["确认后修改材料<br/>重建蓝图与十题"]
        QR --> QA["完整校验 +<br/>独立盲审"]
        RP --> QA
        MR --> QA
        QA --> NV["生成不可变新版本<br/>不自动采用"]
        NV --> AD["人工检查并采用"]
    end

    classDef ai fill:#fef3c7,stroke:#d97706,color:#78350f;
    classDef audit fill:#dbeafe,stroke:#2563eb,color:#1e3a8a;
    classDef code fill:#f3f4f6,stroke:#6b7280,color:#111827;
    classDef done fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef optional fill:#f3e8ff,stroke:#9333ea,color:#581c87;
    classDef moduleLabel fill:#fde68a,stroke:#b45309,color:#78350f,font-weight:bold;
    class M moduleLabel;
    class G,R,T,J,QR,RP,MR ai;
    class A,E,H,QA audit;
    class Q,F,V,X,C,B,P,K,W,CL code;
    class CM,NV optional;
    class O,D,NC,AD done;
    style MATERIAL fill:#fff8e8,stroke:#d97706,stroke-width:2px;
    style QUESTIONS fill:#eff6ff,stroke:#2563eb,stroke-width:2px;
    style REVISION fill:#faf5ff,stroke:#9333ea,stroke-width:2px,stroke-dasharray:5 5;
```

这张图按三个模块表达系统结构：模块一生成并审核听力材料，模块二生成并审核对应题目；二者是每次
生成都要经过的主流程。带星号、虚线边框的模块三是**交付后的可选优化步骤**，只有用户提交题目批注
时才进入，不属于一次生成请求的必经路径。区域内部，黄色节点是负责写作或改稿的生成 Agent，蓝色
节点是独立审核，灰色节点是 Python 控制的校验、预检和重试，绿色节点是采用或交付结果。图只表达
整体方向；各层的重试边界和交付门槛在下文说明。

整个流程可以按下面的顺序理解：

1. **并行生成**：Web 按用户要求的套数拆分任务，每套任务在独立的 Session 中运行，彼此互不影响。
2. **生成初稿**：生成 Agent 产出对话稿和十个信息点的蓝图。
3. **程序校验**：Python 检查格式与标注；失败时带着报告重试，最多三次。
4. **独立盲审**：审核 Agent 看不到蓝图，独立评价并重建信息点；Python 再与蓝图交叉检查。
5. **修改复评**：有缺陷时由新 Agent 修改，再由新审核 Agent 从零复评；Python 在原稿和修改稿
   之间择优。
6. **可行性预检**：在写题前逐项检查十个预选答案是否能从录音中听出、是否只有一个正确答案，
   以及是否能自然填入 Form、Note 或 Table；任一点不成立就更换材料并重新开始。
7. **题目生成与盲审**：题目 Agent 生成十道题；Python 校验后，题目审核 Agent 在不看答案键的
   情况下独立重建答案，再由 Python 交叉检查。
8. **定向修题与恢复**：有问题时只修改题面、答案键和证据，不动录音原文，最多两轮。仍不可交付
   时先重启一次题目阶段，再失败则更换材料。
9. **完整交付**：每套必须同时包含材料和十道题才算完成；要求 N 套时，N 套全部完成才算成功。
10. **人工评价与版本修订***：交付后可以针对题目提交批注。系统先判断无需修改、局部修题、重新命题
    或修改材料；需要执行时重新通过完整校验和独立盲审，生成一个不会自动采用的新版本。此步骤是
    后续优化能力，不是生成完整套件的必经阶段。

`*` 表示可选的交付后流程。

因此，这套系统不是让一个 AI 从头到尾自行决定下一步，而是
“**Agent 执行需要理解语言的工作，Python 控制流程并验收结果**”：

- Agent 负责激活 Skill、读取规范、生成材料与题目、盲审和修改；
- Python 负责拆分任务、保存断点、控制两层重试、隔离盲审输入、运行外部校验、决定换材料并选择
  最终版本。

Agent 不能凭自己运行过 validator 就宣布通过。Python 会在 Agent 返回后重新校验真实产物。
下面各小节沿着这条流程，说明每一步如何落到代码中。

### 3.1 请求进入一个完整套件的 Loop

一次请求的实际调用链如下：

```text
浏览器 POST /api/invocations（action=generate_sets）
  → web/app.py
  → web/fanout.py：按数量拆成一套一个 ChildPlan
  → web/runtime_client.py：每套使用新的 runtimeSessionId 调用 AgentCore
  → backend/app.py::invoke(payload)
  → backend/request.py::parse_delivery_request()
  → backend/orchestration/delivery.py::stream_request()
  → backend/orchestration/loop.py::run_one()              # 材料阶段
  → backend/orchestration/question_loop.py::run_questions() # 题目阶段
```

Runtime 的 Python 入口是 `backend/app.py` 中的 `@app.entrypoint invoke()`。它读取 payload 的
`action`：当前前端使用 `generate_sets` 生成完整材料与题目；旧的 `generate` 仍保留为仅材料路径和
兼容入口。`list_scenarios`、`select`、`preview_audio` 等进入各自 action。

Python 最终传给生成 Agent 的不是整套命制规范，而是明确的任务和场景，例如：

```text
Generate one listening material for the scenario below.

id: booking-hotel
category: booking
title: 酒店预订
客户联系酒店前台咨询并预订房间……
```

### 3.2 创建本轮所需的 Agent

进入具体 AI 步骤时，Python 根据当前阶段创建生成 Agent 或审核 Agent。Agent 不是运行时生成的
一段新代码；`backend/agents.py` 保存固定定义，Python 每次调用时据此创建一个新的 Strands
`Agent` 对象：

```python
Agent(
    model=provider.build_model(...),
    system_prompt=...,
    plugins=[AgentSkills(skills=Skill.from_directory(...))],
    tools=...,
    sandbox=ReadOnlySkillSandbox(...),
)
```

创建新实例是为了隔离会话：不同材料、生成重试、第一次盲审和修改后复评都不共享对话历史或
Skill 激活状态。

系统沿用“生成侧 / 审核侧分池”的边界，并按任务创建不同 Agent：

| Agent | Skill 池 | 工具 | 用途 |
|---|---|---|---|
| 材料生成 Agent | `skills/generate/` | `skills`、`file_read`、`shell` | 材料初稿生成与修改 |
| 材料审核 Agent | `skills/audit/` | `skills`、`file_read`；**无 shell** | 不看 blueprint 的材料盲审与复评 |
| 题目生成 Agent | `skills/generate/` | `skills`、`file_read`、`shell` | 题目初稿与最多两轮定向修改 |
| 题目审核 Agent | `skills/audit/` | `skills`、`file_read`；**无 shell** | 不看答案键的题目盲审与复评 |
| 可行性 Agent | `skills/feasibility/` | 只读 | 正式出题前检查十个信息点是否都适合出题 |

修改没有单独的 Skill。修改步骤创建一个全新的生成 Agent，复用生成 Skill，因为修改稿仍须满足
同一份 specification、Schema 和 validator。

### 3.3 Agent 激活并执行 Skill

`backend/agents.py` 使用 Strands SDK 的原生目录加载：

```python
skills = Skill.from_directory("skills/generate")
plugin = AgentSkills(skills=skills)
```

system prompt 要求模型执行下面的过程：

```text
Agent 先看到池内 Skill 的 name + description
  → 根据任务调用 skills("generate-listening-part1")
  → 获得 SKILL.md 和资源清单
  → 用 file_read 打开 reference 与 Schema
  → 生成 Agent 用 shell 运行 Skill 指定的 validator
  → 返回 Skill 规定的 JSON
```

这些动作由模型通过工具调用完成，不是 Python 逐项强制的状态机。

材料主线的两个 Skill 是：

| | 生成 Skill | 审核 Skill |
|---|---|---|
| 目录 | `skills/generate/generate-listening-part1/` | `skills/audit/audit-listening-part1/` |
| 入口 | `SKILL.md` | `SKILL.md` |
| 规范 | `references/specification.md` | `references/audit-rubric.md` |
| Schema | `material.schema.json`、`blueprint.schema.json`（写侧，只允许 v2）、`blueprint.read.schema.json`（读侧，v1/v2 都收） | `audit.schema.json` |
| 脚本 | `validate_part1.py` | `audit_metrics.py` |

生成结果的统一外壳是：

```json
{
  "material": {},
  "blueprint": {}
}
```

`material` 是对话稿；`blueprint` 是生成者声明的十个信息点，包括答案、证据句、对话位置和适用
题型。`agent_steps.py` 负责解析这个外壳，并补上模型无法可靠知道的真实模型 id 和 UTC 时间。

题目阶段另有 `generate-questions-part1` 与 `audit-questions-part1`。生成侧返回
`question_face + answer_key + evidence`；审核侧只接收材料、考生可见题面和客观指标，不接收
`answer_key`，独立重建十个答案后由 Python 交叉检查。

### 3.4 生成与双层确定性校验

生成 Agent 被要求在一次调用内部执行完整 Skill 工作流：

1. 读取 specification 和两份 Schema；
2. 生成 `material` 与 `blueprint`；
3. 将临时 JSON 写进本次调用独占的 `/tmp/ielts-gen-*` 目录；
4. 用 `shell` 运行 `validate_part1.py`；
5. 根据报告修改并重新检查；
6. 把两个完整产物放进回复，而不是只留在临时文件。

Agent 调用结束后，`GenerationWorkspace` 无论成功失败都会删除整个临时目录。随后 Python 仍然
执行独立校验：

```text
repair_anchors()
  → validate_part1.py
  → 通过：进入盲审
  → errors：累积反馈给新的生成 Agent，最多三次
```

validator 不调用模型，只检查可明确计算的规则：

- **数据格式**：必填字段、Schema、禁止出现的题目/答案/分析字段；
- **结构数量**：说话人、旁白、十个信息点、字数和轮数；
- **标注一致性**：答案是否在证据句中、`turn_index` 是否指向该句、信息点顺序是否正确。

`warnings` 不触发重生成，只作为修改建议。三次仍有错误时保留最后一稿、继续审核并附上
`validation_findings`；校验是报告，不是扣住材料的闸门。429、空响应、错误 JSON 外壳等调用故障
使用独立的基础设施重试预算，不占三次内容生成机会。

### 3.5 盲审与指标沙箱

盲审前，Python 通过 AgentCore Code Interpreter 计算字数、轮数和前后半段分布。远程环境从空白
开始，只上传：

```text
audit_metrics.py
material.json
```

不会上传 `blueprint`。同一个 Code Interpreter session 可在修改后复评时复用，但只替换
`material.json`，完成后由 `loop.py` 关闭。

审核 Agent 接收的业务输入被 `BlindAuditInput` 固定为两个字段：

```text
material + metrics
```

它激活审核 Skill、读取 rubric，并在看不到本材料 `blueprint` 的情况下独立重建信息点。Python
随后执行纯代码交叉检查：

| 生成者的 `blueprint` | 盲审独立找到 | 结果 |
|---|---|---|
| 入住日期：12 July | 入住日期：12 July | 匹配 |
| 房型：double room | 房型：double room | 匹配 |
| 价格：£85 | 未找到 | 需要检查或修改 |

隔离依靠多道边界：生成/审核 Skill 分池、审核 Agent 无 shell、请求不包含 blueprint、生成临时
目录在审核前删除、审核前执行 `assert_no_plan_on_disk()`、远程指标环境只上传两个白名单文件。

### 3.6 修改、独立复评与择优

如果原稿审核和交叉检查都干净，Python 直接交付原稿。否则在时间预算允许时：

1. `loop.py` 将审核缺陷和交叉检查结果整理成 must-fix / advisory；
2. 创建全新的生成 Agent，使用同一生成 Skill 输出完整修改版 `material + blueprint`；
3. Python 修复/核验锚点并再次运行 validator；
4. 修改稿不通过则回退到已经审核过的原稿；
5. 修改稿通过后，创建全新的审核 Agent，从零盲审修改稿；
6. Python 对原稿和修改稿执行 `pick_better()`，平分时选择修改稿。

第一次盲审和复评是同一种 Agent 配置，但不是同一个实例或会话。复评 Agent 看不到第一次审核结论
和修改指令，避免只针对已知问题寻找证据。

### 3.7 题目生成、盲审与定向修改

材料择优后，`delivery.py` 先运行可行性预检。这个 Agent 同时读取定稿材料和蓝图，但不生成题目，
也不重新评价材料整体质量；它只确认十个预选答案点都能被听出、唯一作答并自然填入
Form / Note / Table，同时检查蓝图的题型和分组符合材料的信息关系。通过后，题目生成 Agent 才将
这些已确认的答案点写成正式题面，`question_loop.py` 执行：

1. 题目生成 Agent 输出十道题、答案键和证据；题组数量和边界由材料蓝图中的自然信息结构决定；
2. Python validator 检查 Schema、题型结构、rubric、答案预算、证据和题面泄露；
3. 题目审核 Agent 只看考生可见题面与材料，独立重建答案；
4. Python 将重建答案与答案键交叉检查，识别分歧、竞争答案、泄露和证据锚点问题；
5. 若仍有问题，题目生成 Agent 根据明确 finding 定向修改，最多两轮，每轮重新走完整检查。

`CRITICAL`、`MAJOR`、答案泄露、同等成立的竞争答案、答案分歧和 validator error 都是硬阻断。
只有 `MINOR` 或允许保留的提示时可以作为 `WARNING` 交付。题目修改不得改录音原文；如果 Agent
返回了改写后的 script，整轮修改会被拒绝。

### 3.8 精确数量、重启与换材料

`delivery.py` 把用户要求的每一套表示为一个 slot。slot 是交付名额，不是材料 id：

- 材料通过审核和可行性预检后先写入 `material_done` 断点，再开始出题；
- 题目阶段第一次崩溃或不可交付，保留该材料并完整重启题目阶段一次；
- 同一材料第二次仍不可交付，才消耗一次 `candidate_swap`，为这个 slot 更换候选材料；
- 所有阶段状态写入 S3；Runtime 中断时可以从最后完成的阶段继续，而不是重做已通过内容；
- 请求 N 套时，只有 N 个 slot 都达到 `complete` 才返回 `succeeded`。

### 3.9 题目批注、路由与不可变版本

交付后的人工修订从题目锚点批注开始。当前用户界面支持针对 Q1-Q10 提交评价。

`classify_question_revision` 先评价意见，不修改产物，并为每条批注给出四种结果之一：

| 结果 | 行为 |
|---|---|
| `no_change` | 记录理由和材料/题目引证，不创建版本 |
| `question_only` | 只允许修改批注锚定题目的题面、答案键或证据；材料和蓝图不变 |
| `replan_questions` | 用户确认后重建蓝图和完整十题；可用 `layout_only` 保留原信息点，仅调整 Form/Note/Table 和题组边界 |
| `revise_material` | 用户确认后修改听力稿，并从新材料重建蓝图、完整十题和对应音频归属 |

所有成功修订都写成新的不可变版本，记录 `based_on_version_id`、来源批注和质量结果。新版本生成后不会
自动采用；用户可以先比较、试听，再显式采用。采用版本时材料、蓝图、题包、答案、证据和音频归属作为
一个快照切换，不能把不同版本的产物混在一起。

修订由 Web 后台 execution 持有，SSE 只负责观察；刷新页面不会中断。题面修改、重新命题和材料修改
每次执行都使用新的 `runtimeSessionId`，避免两个长任务共享 AgentCore microVM，也避免 Runtime 部署后
旧 session 继续运行旧镜像。相同 durable request 的重复点击由 execution id 去重，不会产生两个版本。

### 3.10 并发与时间预算

生产环境由 `web/fanout.py` 为每套材料发起一个独立 Runtime invocation，每个请求使用新的
`runtimeSessionId`。`WEB_FANOUT_CONCURRENCY=6` 表示同时最多运行 6 套，不是每批最多 6 套：
第一个任务完成后，队列中的下一套立即开始。这个数是可调的流量保护值，不是 AgentCore 硬限制；
出现 429 时应下调。

单次 Runtime invocation 只处理一个交付位置，内部生成流程串行执行；实际并发来自 Web 同时发起
多个独立 invocation。`generate_sets` 是 SSE 流式调用，适用 AgentCore 60 分钟上限：Runtime
使用 3600 秒硬上限、预留 300 秒收尾，工作窗口为 3300 秒；Web 的单套墙钟同为 3300 秒，
Runtime read timeout 为 3450 秒。

开始材料或题目阶段前，Python 会确认剩余时间足以覆盖该阶段的保守耗时。若不足则保存断点并诚实
返回 `incomplete`，不会把少于请求数量的结果标为成功。

### 3.11 核心代码索引

| 路径 | 作用 |
|---|---|
| `web/fanout.py` | 将批次拆成每套一次 Runtime 调用，限制并发并合并 SSE |
| `web/runtime_client.py` | SigV4 调用 AgentCore，为每次调用设置独立 `runtimeSessionId` |
| `web/question_versions.py` | 修订决策、执行记录、不可变版本和采用状态 |
| `backend/app.py` | AgentCore Python 入口和 action 路由 |
| `backend/request.py` | 把场景 id、数量和自定义场景解析为 slot |
| `backend/orchestration/loop.py` | 单套材料的重试、校验、盲审、修改、复评和择优 |
| `backend/orchestration/question_loop.py` | 十道题的生成、校验、盲审、交叉检查和最多两轮修改 |
| `backend/orchestration/manual_question_revision.py` | 批注范围内的局部题目修改 |
| `backend/orchestration/manual_question_replan.py` | 材料不变的蓝图重规划和完整重命题 |
| `backend/orchestration/manual_material_revision.py` | 材料、蓝图和十题的一致修订 |
| `backend/orchestration/delivery.py` | slot 状态、可行性预检、题目重启、换材料、断点和精确数量交付 |
| `backend/orchestration/slot_store.py` | `_slots/` 与 `_questions/` 的 S3 持久化 |
| `backend/orchestration/batch.py` | 旧的仅材料 `generate` action 与测试入口 |
| `backend/agents.py` | Strands Agent、Skill 池、工具和只读 Skill sandbox |
| `backend/steps/agent_steps.py` | 三类模型调用的消息与输入输出边界 |
| `backend/deterministic/anchors.py` | 可确定修复的 blueprint 锚点同步 |
| `backend/deterministic/validate.py` | Python 调用生成 Skill validator 的包装 |
| `backend/deterministic/crosscheck.py` | blueprint 与盲审信息图交叉检查 |
| `backend/sandboxed_metrics.py` | Code Interpreter session 与白名单文件上传 |

### 3.12 如何扩展到其他 IELTS 能力

**增加采用同类工作流的 Skill**：在对应池中增加一个包含 `SKILL.md` 的目录，并提供它声明的
references、Schema 和 scripts。`Skill.from_directory()` 会自动发现 Skill，无需在 `agents.py`
写新的目录名。

```text
skills/
├── generate/
│   ├── generate-listening-part1/
│   └── generate-reading-passage1/       # 示例，当前不存在
└── audit/
    ├── audit-listening-part1/
    └── audit-reading-passage1/          # 示例，当前不存在
```

Skill 自动发现不等于新学科已经端到端可用。扩展 Reading/Writing 仍需增加任务路由、产物契约、
确定性脚本、Loop 策略和前端交互。

## 4. 部署前准备

### 4.1 工具与 AWS 条件

- AWS CLI v2，凭证可调用目标账号；
- Docker，能构建 `linux/arm64` 镜像；
- Python 3.12（项目声明最低 `>=3.11`，部署和测试建议统一使用 3.12）；
- Node.js 20+ 与 npm；
- AWS 区域必须是 `us-east-1` 或 `us-east-2`；脚本会拒绝其他区域，因为当前 GPT-5.6
  接入不提供跨区推理；
- 默认 VPC 中至少有两个位于不同可用区、可分配公网 IP 的子网；也可显式传入 VPC 与子网。

先确认身份和区域：

```bash
aws sts get-caller-identity
export AWS_REGION=us-east-1
```

### 4.2 两类 IAM 权限

不要混淆下面两类身份：

1. **部署者身份**：你当前的 IAM user/role，负责执行 `deploy/*.sh`、构建并推送镜像；
2. **运行期角色**：`provision.sh` 创建的三个项目角色，由 ECS 和 AgentCore 在运行时承担。

部署者必须能够创建运行期角色，并通过 `iam:PassRole` 把角色交给 ECS/AgentCore。

### 4.3 部署者 IAM policy

下面策略覆盖当前 `provision`、镜像推送、`runtime`、`edge`、`service`、日常启停、状态查询和
`teardown` 脚本。它以可直接提交给 IAM 的 JSON 表示，不含需要替换的账号占位符；资源创建与
查询动作使用 `"Resource": "*"`，IAM 角色操作则限制为 `ielts-part1-*`。

> 这是覆盖当前脚本的项目部署策略，不是严格的最小权限基线。ECS、EC2、ELB、CloudFront 和
> AgentCore 的创建/查询动作中有一部分使用 `"Resource": "*"`；建议通过专用部署 role、
> permissions boundary 或组织 SCP 进一步限制账号和区域。若组织要求 KMS、强制标签或私有
> 网络，还需由云平台管理员补充相应条件。

<details>
<summary>展开部署者 IAM policy JSON</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadCallerIdentity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Sid": "CreateProjectBucket",
      "Effect": "Allow",
      "Action": "s3:CreateBucket",
      "Resource": "*"
    },
    {
      "Sid": "ManageProjectBucket",
      "Effect": "Allow",
      "Action": [
        "s3:DeleteBucket",
        "s3:GetBucketLocation",
        "s3:GetBucketVersioning",
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:PutBucketVersioning",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutEncryptionConfiguration"
      ],
      "Resource": "arn:aws:s3:::ielts-part1-materials-*"
    },
    {
      "Sid": "ManageProjectBucketObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion"
      ],
      "Resource": "arn:aws:s3:::ielts-part1-materials-*/*"
    },
    {
      "Sid": "EcrAuthorization",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "ProjectEcrRepositories",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:DeleteRepository",
        "ecr:DescribeRepositories",
        "ecr:DescribeImages",
        "ecr:ListImages",
        "ecr:PutImageTagMutability",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "arn:aws:ecr:*:*:repository/ielts-part1-*"
    },
    {
      "Sid": "ProjectEcsAndNetworking",
      "Effect": "Allow",
      "Action": [
        "ecs:CreateCluster",
        "ecs:DeleteCluster",
        "ecs:DescribeClusters",
        "ecs:RegisterTaskDefinition",
        "ecs:CreateService",
        "ecs:UpdateService",
        "ecs:DeleteService",
        "ecs:DescribeServices",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeNetworkInterfaces",
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ProjectLoadBalancerAndCdn",
      "Effect": "Allow",
      "Action": [
        "elasticloadbalancing:CreateLoadBalancer",
        "elasticloadbalancing:DeleteLoadBalancer",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:ModifyLoadBalancerAttributes",
        "elasticloadbalancing:CreateTargetGroup",
        "elasticloadbalancing:DeleteTargetGroup",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "elasticloadbalancing:ModifyTargetGroupAttributes",
        "elasticloadbalancing:CreateListener",
        "elasticloadbalancing:DescribeListeners",
        "cloudfront:CreateDistribution",
        "cloudfront:GetDistribution",
        "cloudfront:GetDistributionConfig",
        "cloudfront:ListDistributions",
        "cloudfront:UpdateDistribution",
        "cloudfront:DeleteDistribution"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ProjectRuntime",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateAgentRuntime",
        "bedrock-agentcore:UpdateAgentRuntime",
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:ListAgentRuntimes",
        "bedrock-agentcore:DeleteAgentRuntime"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ProjectSessionSecret",
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:PutParameter"
      ],
      "Resource": "arn:aws:ssm:*:*:parameter/ielts-part1/session-secret"
    },
    {
      "Sid": "DescribeLogs",
      "Effect": "Allow",
      "Action": "logs:DescribeLogGroups",
      "Resource": "*"
    },
    {
      "Sid": "ProjectLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy",
        "logs:GetLogEvents"
      ],
      "Resource": [
        "arn:aws:logs:*:*:log-group:/ecs/ielts-part1-web",
        "arn:aws:logs:*:*:log-group:/ecs/ielts-part1-web:*"
      ]
    },
    {
      "Sid": "ManageProjectRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:ListRolePolicies",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:DeleteRole"
      ],
      "Resource": "arn:aws:iam::*:role/ielts-part1-*"
    },
    {
      "Sid": "CreateRequiredServiceLinkedRoles",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "iam:AWSServiceName": [
            "ecs.amazonaws.com",
            "elasticloadbalancing.amazonaws.com"
          ]
        }
      }
    },
    {
      "Sid": "PassRuntimeRoleToAgentCore",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::*:role/ielts-part1-runtime",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
        }
      }
    },
    {
      "Sid": "PassEcsRolesToEcsTasks",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::*:role/ielts-part1-ecs-exec",
        "arn:aws:iam::*:role/ielts-part1-web-task"
      ],
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "ecs-tasks.amazonaws.com"
        }
      }
    }
  ]
}
```

</details>

`iam:PassRole` 风险较高：它允许 AWS 服务以指定角色运行。不要把它改成不受限的
`"Resource": "*"`，也不要移除 `iam:PassedToService` 条件。

`iam:CreateServiceLinkedRole` 只允许 ECS 和 Elastic Load Balancing 创建各自的服务关联角色。
账号中已经存在这些角色时不会再次创建；新账号若缺少这项权限，首次创建 ECS service 或 ALB
可能在部署中途失败。

策略按默认资源名限制 S3 桶为 `ielts-part1-materials-*`。如果通过 `S3_BUCKET` 改成其他前缀，
必须同步修改策略中的两个 S3 ARN，否则 `provision.sh` 会在建桶或写入配置时被拒绝。

将策略保存为本地 `ielts-part1-deployer-policy.json` 后，可用 CLI 创建并附加：

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws iam create-policy \
  --policy-name IELTSPart1Deployer \
  --policy-document file://ielts-part1-deployer-policy.json

# 附加到当前用于部署的 role：
aws iam attach-role-policy \
  --role-name <DEPLOYER_ROLE_NAME> \
  --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/IELTSPart1Deployer

# 如果使用 IAM user，则改用：
aws iam attach-user-policy \
  --user-name <DEPLOYER_USER_NAME> \
  --policy-arn arn:aws:iam::${ACCOUNT_ID}:policy/IELTSPart1Deployer
```

创建和附加 managed policy 的操作者本身还需要 `iam:CreatePolicy` 以及
`iam:AttachRolePolicy` 或 `iam:AttachUserPolicy`。通常这一步由账号管理员执行；获得部署者权限
后，日常部署不需要这些管理动作。

### 4.4 脚本创建的运行期角色

| 角色 | 使用者 | 核心权限 |
|---|---|---|
| `ielts-part1-ecs-exec` | ECS 平台 | 拉取 Web 镜像、写日志、读取 session secret |
| `ielts-part1-web-task` | Web 应用 | 调用 AgentCore、读写项目 S3 桶 |
| `ielts-part1-runtime` | AgentCore | 调用模型、Code Interpreter 和 Polly，读写项目 S3 桶 |

这些策略由 `deploy/provision.sh` 和 `deploy/service.sh` 自动创建，无需手工复制。
`bedrock-mantle:*` 与 `bedrock:*` 是不同权限前缀；Runtime 调用 GPT-5.6 需要脚本中列出的
Mantle 权限，不能只授予 `bedrock:InvokeModel`。盲审指标还需要
`bedrock-agentcore:StartCodeInterpreterSession`、`InvokeCodeInterpreter` 和
`StopCodeInterpreterSession` 等权限；当前 Runtime 内联策略已经包含。

## 5. 从零部署

所有命令从仓库根目录执行。

```bash
# 0. 选择区域并确认身份
export AWS_REGION=us-east-1
aws sts get-caller-identity

# 1. 创建 S3、ECR、ECS cluster、日志组和三个运行期角色
bash deploy/provision.sh

# provision.sh 当前不会设置 tag mutability；显式锁住两个 repository，保证旧标签不会被覆盖
aws ecr put-image-tag-mutability \
  --repository-name ielts-part1-backend \
  --image-tag-mutability IMMUTABLE
aws ecr put-image-tag-mutability \
  --repository-name ielts-part1-frontend \
  --image-tag-mutability IMMUTABLE

# 为本次部署选择一个未使用过的不可变标签
export RELEASE_TAG=20260801-agent-autonomy

# 2. 构建并推送 AgentCore Runtime 镜像
bash backend/scripts/deploy.sh \
  "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com/ielts-part1-backend" \
  "$RELEASE_TAG"

# 3. 创建 AgentCore Runtime
bash deploy/runtime.sh "$RELEASE_TAG"

# 4. 创建 ALB、CloudFront、目标组和网络规则
bash deploy/edge.sh

# 5. 构建 Web 镜像并创建 ECS 服务
# 公网使用前必须限制可注册邮箱域名
ALLOWED_EMAIL_DOMAINS=example.com bash deploy/service.sh "$RELEASE_TAG"

# 6. 将 ECS 服务扩到 1，并打印访问地址
bash deploy/start.sh
```

建议先执行 `edge.sh` 再执行 `service.sh`。ECS 不能给已存在且未配置负载均衡器的服务原地增加
ALB；顺序相反时，`service.sh` 会明确删除并重建服务。

`provision.sh` 当前只创建 repository，不设置 tag mutability，所以上面的两条命令不能省略。
`deploy.sh`、`runtime.sh` 和 `service.sh` 都要求显式传入 tag。不要复用 `dev` 或覆盖旧标签。

回退不走这三个脚本，用 `deploy/rollback.sh`（见 §5.3）。`service.sh <known-good-tag>` 不是可用的
回退路径：它无条件执行 `docker build && docker push`，而两个 ECR repository 都是 IMMUTABLE，推送
已存在的 tag 必然失败；它还依赖可用的 Docker daemon，因此不适合作为紧急回退路径。

### 5.1 验证

```bash
bash deploy/status.sh
curl -si https://<CLOUDFRONT_DOMAIN>/healthz | head -1
```

健康检查应返回 `HTTP/2 200` 或 `HTTP/1.1 200`。新 CloudFront 分发通常需要 5 到 10 分钟变为
`Deployed`，传播期间出现 502/503 不一定表示应用部署失败。

### 5.2 日常更新与启停

更新时使用新的发布标签重新构建镜像，再明确切换 Runtime 和 Web。旧标签保留用于回退：

```bash
export RELEASE_TAG=20260802-fix-1
bash backend/scripts/deploy.sh \
  "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com/ielts-part1-backend" \
  "$RELEASE_TAG"
bash deploy/runtime.sh "$RELEASE_TAG"
ALLOWED_EMAIL_DOMAINS=example.com bash deploy/service.sh "$RELEASE_TAG"

bash deploy/stop.sh    # ECS desiredCount=0；保留 ALB、CloudFront 和稳定 URL
bash deploy/start.sh   # ECS desiredCount=1；等待服务稳定
bash deploy/status.sh
```

`stop.sh` 只停止 Fargate 任务。ALB 仍有固定费用，CloudFront 地址继续存在但在无健康目标时返回
503。

每次部署后在 [`deploy/RELEASES.md`](deploy/RELEASES.md) 补一行。镜像标签本身不携带 git commit，
不记录就等于没有回退能力。

### 5.3 回退到已验证版本

```bash
bash deploy/rollback.sh --to prod-20260801 --dry-run   # 先看清当前与目标，什么都不改
bash deploy/rollback.sh --to prod-20260801
```

`--to` 后面是 `deploy/RELEASES.md` 里的 git tag；Runtime version 和 taskdef revision 从该 tag 的
附注中解析，所以台账和实际回退目标不会各说各话。也可以绕过 tag 直接指定
`--runtime-version 17` / `--taskdef 31`，两者可单独使用，只回退一层。

回退不需要 Docker，也不需要构建：两个产物已经在 AWS 里存在——ECS task definition revision 钉住了
旧 Web 镜像，AgentCore Runtime version 钉住了旧后端镜像。脚本只是把流量重新指向它们。

**不会改变的东西**（结构上决定，不是靠小心）：

- **CloudFront 与 ALB**：交付 URL 是分发的属性，不属于任何版本，只有 `edge.sh` / `teardown.sh`
  管理它们，回退脚本不调用这两个；
- **S3**：不读不写任何对象，材料、批次、用户和音频原样保留；
- **登录态**：`SESSION_SECRET` 来自 SSM，跨回退稳定，已登录用户不会掉线。

实测：Web 层回退约 3 分钟（`update-service` 到 `services-stable`）；Runtime 层重推镜像到 READY
约 1 分钟。演练记录和耗时见 `deploy/RELEASES.md`。

回退**不能**解决的一种情况：如果被回退掉的那个版本已经按新结构写过数据（改了 `_candidates/`
记录或 `material.json` 的形状），回退后旧代码会读到新格式。桶开了版本控制，但版本控制救不了结构
不兼容。涉及数据结构的改动应换用新的 key 前缀发布。

**Runtime 层只有一条路径：重推镜像。** `update-agent-runtime-endpoint --agent-runtime-version`
对 `DEFAULT` 端点是不可用的（AWS 直接回 `ConflictException: Default endpoints are managed through
agent updates`，2026-08-07 回退途中实测），所以 `rollback.sh` 用 `update-agent-runtime` 把目标版本
记录的镜像和整份配置重放回去。

代价是**版本号必然只增**：回退到 v17 的镜像会产生 v19，于是「v19 装的是 v17 的镜像」。因此
**版本号不能反推代码版本**，要知道线上跑的是哪一版只能查 `containerUri`：

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <id> --agent-runtime-version <n> \
  --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text
```

| 命令 | 含义 |
|---|---|
| `rollback.sh --runtime-version 17` | 回到 **v17 记录的镜像和配置**，不是让 v17 重新 live |
| `rollback.sh --runtime-image <tag>` | 直接按 ECR 镜像标签回退，配置沿用当前版本 |
| `runtime.sh <tag>` | 正常发布路径；回退不必用它，但它做的是同一件事 |

脚本按这条现实设计了三件事：**先 Runtime 后 web**（Runtime 是会失败的那一步，让它失败在什么都没动
的时候），web 步失败则把 Runtime **补偿**回原样，以及动手前**预检**两层镜像是否还在 ECR、taskdef
是否 `ACTIVE`。目标镜像与当前一致时，脚本直接跳过而不是产生一个换不动代码的新版本号。回退结果以
`containerUri` 和 taskdef 核对，不一致就非零退出；`/healthz` 200 只说明有东西在应答，分不清
「回退成功」和「什么都没发生」。

### 5.4 拆除

```bash
bash deploy/teardown.sh --yes
bash deploy/teardown.sh --yes --purge-s3
```

第一条保留 S3 数据，因此仍会产生少量存储费用；第二条连同桶内所有对象版本一起删除。脚本
不会删除 SSM 中的 `/ielts-part1/session-secret`，也不会注销历史 ECS task definition revision；
需要完全清理账号时应另行删除。拆除 CloudFront 后原 `*.cloudfront.net` 地址无法恢复，重建会
获得新域名。部分 AWS 删除是异步的，若目标组或安全组仍被引用，等待一分钟后重新执行同一命令。

## 6. 部署资源与配置

### 6.1 创建的主要资源

| 资源 | 名称 |
|---|---|
| S3 bucket | `ielts-part1-materials-{account}` |
| ECR repositories | `ielts-part1-backend`、`ielts-part1-frontend` |
| ECS cluster/service | `ielts-part1` / `ielts-part1-web` |
| ECS task definition | `ielts-part1-web`，ARM64，0.5 vCPU / 1 GB |
| AgentCore Runtime | `ielts_part1_runtime` |
| AgentCore Code Interpreter | 内置 `aws.codeinterpreter.v1`，按材料创建临时 session |
| CloudWatch log group | `/ecs/ielts-part1-web`，保留 7 天 |
| SSM SecureString | `/ielts-part1/session-secret` |
| ALB / target group | `ielts-part1-alb` / `ielts-part1-tg` |
| CloudFront | 以 ALB 为 origin 的动态分发 |

### 6.2 部署参数

`deploy/config.sh` 中的值均可通过环境变量覆盖：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AWS_REGION` | `us-east-1` | 仅支持 `us-east-1` / `us-east-2` |
| `ACCOUNT_ID` | 从当前凭证发现 | 目标账号 |
| `AWS_PROFILE` | 未设置 | 可选命名 profile |
| `S3_BUCKET` | `ielts-part1-materials-{account}` | 项目单桶 |
| `VPC_ID` | 默认 VPC | 目标 VPC |
| `SUBNET_IDS` | 默认 VPC 的公有子网 | ALB 至少需要两个不同 AZ 的子网 |
| `SUBNET_ID` | `SUBNET_IDS` 第一个 | Fargate 任务子网 |
| `INGRESS_CIDR` | `0.0.0.0/0` | ALB ingress；收窄到办公网会阻断 CloudFront 回源 |
| `ALB_IDLE_TIMEOUT` | `120` | 必须显著大于 SSE 心跳 |
| `TASK_CPU` / `TASK_MEMORY` | `512` / `1024` | Web 任务规格 |

当前 ALB origin 可通过公网 HTTP 直接访问。若要阻止绕过 CloudFront，需要额外实现 CloudFront
origin-facing 托管前缀列表和自定义 header 校验；现有脚本未实现这一层。

### 6.3 Web 运行参数

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ALLOWED_EMAIL_DOMAINS` | `*` | 允许注册的邮箱域名；公网部署必须显式限制 |
| `SESSION_SECRET` | 无 | `service.sh` 生成并从 SSM 注入 |
| `AGENT_RUNTIME_ARN` | 部署时注入 | Runtime ARN |
| `IELTS_AUDIO_BUCKET` | 项目 S3 桶 | 材料与按需音频存储 |
| `USER_STORE_S3_BUCKET` | 项目 S3 桶 | 用户存储；本地未设时回退为本地 JSON |
| `USER_STORE_S3_KEY` | `web/users.json` | 用户文件 key |
| `WEB_FANOUT_CONCURRENCY` | `6` | 同时运行的 Runtime 调用数；429 时下调 |
| `WEB_SSE_HEARTBEAT` | `15` | SSE 心跳秒数 |
| `WEB_RUNTIME_READ_TIMEOUT` | `3450` | 单套流式 Runtime 读取超时；高于工作窗口、低于平台上限 |
| `WEB_PER_MATERIAL_WALL` | `3300` | 单个 slot 的 Web 墙上时钟预算 |

### 6.4 Runtime 参数

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IELTS_MODEL_ID` | `openai.gpt-5.6-terra` | 模型 id |
| `IELTS_MODEL_REGION` | `AWS_REGION` | 模型区域 |
| `IELTS_MODEL_AUTH` | `mantle` | `bearer` 仅适合本地临时调试 |
| `IELTS_CODE_INTERPRETER_REGION` | `AWS_REGION` | 审核指标 Code Interpreter 区域 |
| `IELTS_CODE_INTERPRETER_ID` | `aws.codeinterpreter.v1` | 内置 Code Interpreter identifier |
| `IELTS_CONCURRENCY` | `6` | Runtime 内并发槽；Web 单套调用时实际夹到 1 |
| `IELTS_HARD_LIMIT` | `3600` | `generate_sets` 的平台流式上限 |
| `IELTS_P95_PER_MATERIAL` | `240` | 预算估算值 |
| `IELTS_P95_QUESTIONS` | `420` | 开始题目阶段前要求保留的预算 |
| `IELTS_REVISION_COST` | `120` | 修改与复评预算 |
| `IELTS_SAFETY_MARGIN` | `300` | 保存断点、汇总状态和关闭流的收尾余量 |
| `IELTS_MAX_CANDIDATE_SWAPS` | `2` | 一个 slot 放弃旧材料并更换候选材料的上限 |
| `IELTS_MAX_QUESTION_RESTARTS` | `1` | 同一合格材料完整重启题目阶段的次数 |
| `IELTS_MAX_REPLACEMENT_SLOTS` | `2` | 一个交付位置可创建的替代 slot 代数 |
| `IELTS_SCRIPT_TIMEOUT` | `60` | 确定性脚本超时 |

前端运行时配置位于 `frontend/public/config.json`。

## 7. S3 数据结构

```text
s3://ielts-part1-materials-{account}/
├── pending/{scenario_key}/{material_id}/
│   ├── material.json
│   ├── blueprint.json
│   ├── audit.json
│   └── audio/
│       ├── turn_000.mp3 ...
│       └── manifest.json
├── approved/{scenario_key}/{material_id}/
├── rejected/{scenario_key}/{material_id}/
├── production/{scenario_key}/{material_id}/
├── _history/{material_id}/{timestamp}-{source}-{target}.json
├── _batches/{batch_id}/
│   ├── index.json
│   └── materials/{material_id}.json
├── _slots/{request_id}/
│   ├── request.json
│   └── slot-*.json
├── _questions/{material_id}.json
├── _comments/{material_id}.json
├── _candidates/{material_id}.json
├── _candidates/{material_id}.job.json
├── _claims/{group_key}.json
└── web/users.json
```

- `_slots/` 保存每个交付名额的阶段、重启和换材料状态，`_questions/` 只保存通过交付门槛的题目包。
- `_comments/` 保存材料阅读页上的个人批注，与材料和题目产物分开。
- 自动生成结束后，候选信息写入 `_candidates/` 和批次空间；不会自动出现音频。
- 未提交审核的候选保留 30 天，期间可以跨日审阅和提交；过期后不能再提交审核。
- 用户主动生成音频后才创建 `audio/`。`manifest.json` 最后写入，是“音频完整”的哨兵；没有
  manifest 的目录不会被当作完整音频交付。
- `_history/` 保存状态迁移审计记录，不随材料跨状态目录移动。
- 桶开启版本控制，便于恢复被错误修订覆盖的材料。

## 8. 本地开发与测试

### 8.1 Python

```bash
python3.12 -m venv .venv-backend
.venv-backend/bin/pip install -e 'backend[dev]'

python3 skills/shared/tests/run_tests.py
python3 audio_storage/tests/run_tests.py
.venv-backend/bin/python -m pytest backend/tests -q
.venv-backend/bin/python web/tests/run_tests.py
bash backend/scripts/ci_gates.sh
```

### 8.2 前端

```bash
cd frontend
npm ci
npm run verify
npm run dev
```

`npm run verify` 依次检查 Schema/场景/fixture codegen、TypeScript、Oxlint 和 Vitest。前端契约类型
由共享 Schema 生成，不应手工修改生成文件。

### 8.3 AWS 冒烟

以下命令会访问真实 AWS 服务并可能产生模型或 Polly 费用：

```bash
.venv-backend/bin/python backend/scripts/smoke_model.py
.venv-backend/bin/python backend/scripts/run_one.py --scenario booking-hotel
bash backend/scripts/check_ping.sh
```

## 9. 运维与排障

| 现象 | 检查与处理 |
|---|---|
| CloudFront 503，`running=0` | 服务已停止，执行 `bash deploy/start.sh` |
| CloudFront 502，任务在运行 | 查看目标组健康状态和 `/ecs/ielts-part1-web` 日志 |
| 新分发暂时 502/503 | 等待 `status.sh` 显示 CloudFront `Deployed` |
| 生成流中途断开 | 核对 15 秒心跳、CloudFront 60 秒 origin read、ALB 120 秒 idle |
| 材料在结尾一次性出现 | 检查 CloudFront `Compress` 必须为 `false` |
| 模型返回 429 | 下调 `WEB_FANOUT_CONCURRENCY`，不要盲目增加重试 |
| `bedrock-mantle:CreateInference` denied | Runtime 角色缺少 Mantle 权限，重新运行 `provision.sh` 或核对内联策略 |
| Code Interpreter 创建或调用失败 | 核对 Runtime 角色的 Code Interpreter 权限和 `IELTS_CODE_INTERPRETER_REGION` |
| 生成 Agent 一直等待工具确认 | 确认代码通过 `agents.py` 设置了非交互 shell 环境变量 |
| `invalid_api_key` / security token invalid | 先运行 `aws sts get-caller-identity` 检查本地凭证是否过期 |
| 试听后长时间无音频 | 查看 `_candidates/{material_id}.job.json`；首次合成通常需 1 到 2 分钟 |
| 登录后随机失效 | 核对 ECS task definition 是否从 SSM 注入同一个 `SESSION_SECRET` |
| `teardown` 无法删除目标组/安全组 | AWS 仍在释放 ALB/ENI，等待一分钟后重跑 |
| CloudFront 无法删除 | 分发必须先 disable 并完成传播；等待后重跑 `teardown.sh` |

日志位置：

- Web：CloudWatch `/ecs/ielts-part1-web`；
- Runtime：AgentCore 自动创建的 Runtime 日志组。

## 10. 安全与已知限制

### 安全边界

- 仓库不保存 access key、账号 id 或 session secret；
- S3 开启四项 public access block 和 SSE-S3；
- session 使用 HttpOnly cookie，签名密钥保存在 SSM SecureString；
- 运行期使用三个职责分离的 IAM role；
- 当前未配置 WAF、MFA、集中审计日志或 CloudFront 自定义错误页；
- 当前 ALB origin 可被知道主机名的人通过公网 HTTP 绕过 CloudFront。

### 已知限制

- 前端均匀度阈值尚未用足量真实样本校准；
- `WEB_FANOUT_CONCURRENCY=6` 是起始配置，不是所有账号配额下的保证值；
- 版权原因，真题样本不随仓库分发，相关测试会 skip；
- ECS 服务默认单任务、单任务子网，无自动伸缩、蓝绿部署或应用级多可用区冗余；
- 用户池是单个 S3 JSON 对象，更新采用整文件读写；进程锁只能保护单实例。扩展到多个 Web task
  前应迁移到支持条件写或事务的用户存储，避免并发注册相互覆盖；
- 盲审隔离通过 Skill 分池、输入隔离、临时文件清理和 Code Interpreter 白名单等多层防线实现；
- Agent 内部校验用于提高单次调用质量；最终以 Python 外部校验结果为准；
- 当前只实现 Listening Part 1；Skill 池可以发现新目录，但 Reading/Writing 的请求路由、产物契约、
  Loop 和前端尚未实现；
- `stop.sh` 不删除 ALB，因此不能把常驻成本降为零；
- `teardown.sh` 默认保留 S3，且始终保留 SSM secret 和历史 task definition revision；
- GPT-5.6 当前部署限制在 `us-east-1` / `us-east-2`。

## 声明

本项目产出原创练习材料，不复制真题内容，也不代表 IELTS、Cambridge 或 British Council 的
授权或背书。
