# IELTS Part 1 生成系统 · AWS Global → China Region 架构设计

## 概要

- 引言
- 术语（Nomenclature）
- 用户旅程与业务需求（User Journey）
  - 登录与账号
  - 旅程一：选场景、批量生成一批材料
  - 旅程二：审阅一套材料、看答案与证据
  - 旅程三：试听（按需音频）
  - 旅程四：评价并修订（题目 / 原文 Turn / 重新命题 / 修改材料）
  - 旅程五：版本采用、对比、选稿与 QTI 导出
- 三条主线
  - 主线一：模型服务提供商变更
  - 主线二：模型 API 调用的认证变更与网络安全加固
  - 主线三：部署脚本（CDK / Terraform）的中国区适配
- 关键组件/实体的逐项映射
- 安全考量（Security Considerations）
- 其他设计考量

---

## 引言

本系统的使用者是 **IELTS Part 1 听力材料的出题人员**。他们的业务目标：批量产出合格的听力
套件（一段对话 + 10 个答案信息点 + 10 道 Form/Note/Table 填空题 + 答案、证据、质检结果），审阅、
试听、提意见修订、挑版本，最后导出给下游题库。系统的价值在于"Agent 做需要理解语言的活、Python
控制流程并验收"，把人从逐题手写里解放出来。

目前系统在 **AWS Global 分区**：
`backend/model/provider.py` 区域 `us-east-1 / us-east-2 / us-west-2`，
`deploy/config.sh::require_region()` 主动拒绝这三者之外的区域，模型来自 **Bedrock Mantle** 托管的
**GPT-5.6**，运行底座是 **AgentCore Runtime**，审核隔离用 **AgentCore Code Interpreter**。

经实测**AgentCore Runtime 与 Code Interpreter 在宁夏都可用，运行底座和审核隔离原地保留，只有Bedrock需要换**，再加上对外发布的合规层（ICP、证书、加密）。

**用 Strands SDK 的 OpenAI 兼容能力（`strands.models.openai.OpenAIModel`，
Chat Completions）直连国内大模型**（DeepSeek、通义千问、智谱、Kimi 等已完成算法备案的境内 API）。Strands 原生支持传入自定义 `base_url`与 `api_key`。

## 术语（Nomenclature）

- **出题人员** — 系统的最终用户，用浏览器完成选场景、审阅、评价、采用、选稿、导出。
- **套件** — 一段听力对话及对应的 10 道题（`USER_GUIDE` 术语表）。
- **批次** — 一次提交生成的全部材料。
- **Global 分区** — 当前系统所在的标准商业分区，`arn:aws:`、`*.amazonaws.com`。
- **China 分区（中国区）** — 光环新网（Sinnet，运营 **cn-north-1 / 北京**）与西云数据（NWCD，
  运营 **cn-northwest-1 / 宁夏**）分别运营的两个独立区域，`arn:aws-cn:`、`*.amazonaws.com.cn`，
  需独立的中国区账号与凭证。
- **模型平面** — Global 是 Bedrock Mantle，**China Region没有 Bedrock**。
- **运行底座** — Global 是 AgentCore Runtime，**宁夏可用**
  无需替换。
- **隔离沙箱** — 审核侧跑指标脚本隔离环境。Global 下是 AgentCore
  Code Interpreter，**宁夏内建 `aws.codeinterpreter.v1` READY**（实测），无需替换。
- **边缘层** — Global 下是 CloudFront → ALB。China Region可直接访问ALB。
- **ICP 备案** — 中国大陆对外提供网站/应用服务的工信部许可。

## 用户旅程与业务需求（User Journey）

### 登录与账号

**业务旅程**：出题人员打开系统先登录；没有账号时用允许的邮箱域名注册。系统**没有用 Cognito**，
而是把用户存在 `s3://.../web/users.json`，用 PBKDF2 摘要口令、SSM 密钥签名 7 天 HttpOnly cookie，
`ALLOWED_EMAIL_DOMAINS` 控制注册（`service.sh` 拒绝把它设成 `*` 的开放注册）。

**迁移到中国区会在哪里断**：这一步**基本不断**——S3、SSM、PBKDF2 自建登录在中国区都成立。

### 旅程一：选场景、批量生成一批材料

**业务旅程**：出题人员在"场景选择"页勾选场景、设定每个场景，点"提交生成"。
系统**同时生成多套、其余排队**，每套只有在对话和 10 道题都完成后才计入"已完成"。用户可以离开或
刷新，后台不中断。背后：浏览器 → CloudFront → ALB → Web(ECS Fargate) →（SigV4）每套一次
AgentCore Runtime invocation → Runtime 跑生成/审核 Agent Loop → 调 Bedrock Mantle 的 GPT-5.6；
进度以 SSE 流式回传（受 AgentCore 60 分钟流式上限约束）。

中国区**无 Bedrock**。→ **主线一**：
  用 Strands 的 OpenAI 兼容 `OpenAIModel` 直连国内大模型。


### 旅程二：审阅一套材料、看答案与证据

**业务旅程**：出题人员点"阅读全文"，在"对话原文"看完整对话、答案证据高亮、信息点类型与引用、
考点小结；在"题目预览"按真实版式看 Form/Note/Table，默认考生视角，可打开"显示答案和证据"核对。
背后：这些内容早已由生成阶段写入 **S3**，审阅只是 Web 层从 S3 读出来渲染，**不再调模型**。

S3 在中国区可用，审阅是纯读取。要做的只是桶 ARN
换 `aws-cn`、加密可从 SSE-S3 升级为 SSE-KMS（中国区合规常见）。

### 旅程三：试听（按需音频）

**业务旅程**：出题人员在阅读页点"生成音频"，系统**按需**调用 **Amazon Polly** 逐 Turn 合成英式
语音，返回 job id，浏览器轮询，完成后用预签名 URL 播放。

实测 `aws polly describe-voices --profile cn --region
cn-northwest-1` 返回了 `en-GB` 英式音色（Emma / Brian / Amy），IELTS 需要的英式口音直接可用，
`audio_storage/synthesize.py` 几乎不用改。

### 旅程四：评价并修订（题目 / 原文 Turn / 重新命题 / 修改材料）

**业务旅程**：这是系统最复杂的业务。出题人员交付后可以：

- 对某道题提意见，系统先**分类**（无需修改 / 修改题目 / 重新命题 / 修改材料），不直接改稿；
- 对某个非旁白 Turn 提局部修改（删冗余、缩句、改语法），一次一个 Turn，不动题目答案；
- 确认后重新命题（改题型/题组/布局）或修改材料（目标答案在原文没依据时）。

每一种"执行"都要**重新走完整校验 + 独立盲审**，生成**不可变新版本**，不自动采用。修订由 Web 后台
execution 持有，SSE 只观察，刷新不中断，通常约 15 分钟；每次执行用新的 `runtimeSessionId`。背后
全部要**再次调模型 + 再次跑隔离盲审**。

盲审 Agent、修订 Agent 调的 GPT-5.6 要换成中国区 provider（主线一）。

### 旅程五：版本采用、对比、选稿与 QTI 导出

**业务旅程**：修订生成 V2/V3……但**不自动采用**；出题人员在版本选择器切换检查（材料、蓝图、题目、
答案、音频整体切换），确认后点"采用"。同场景多套可"对比本场景"并排看差异。最后在结果页勾选保留的
套件、"提交审核"进入审核队列。题目预览页可"导出 QTI 2.2"——Web 层从 S3 读出题目，**在进程内**用
`qti_export/` 转成 IMS QTI 2.2.4 zip 直接下载，**不经过 Runtime、不写回 S3**。

版本、对比、选稿、审核队列全是 S3 读写 + Web 层逻辑；
QTI 导出是纯进程内 Python（标准 XSD 转换），**完全不依赖模型、不依赖 AgentCore**。桶 ARN 换
`aws-cn` 即可。



## 三条主线


### 主线一：模型服务提供商变更

**方案**：用 **Strands SDK 的 OpenAI 兼容模型类
`strands.models.openai.OpenAIModel`（Chat Completions）直连国内大模型**。国内主流大模型
（DeepSeek、通义千问、智谱、Kimi 等，均已做算法备案）都提供 OpenAI 兼容端点，Strands 原生支持
传入自定义 `base_url` 与 `api_key`，因此这是改造量最小、也最合规的路径——不自建、不跨境。

改造集中在`backend/model/provider.py`：

- 新增 provider 开关 `IELTS_MODEL_PROVIDER`（`mantle` 保留给 Global，`openai-compatible` 为中国区
  默认）。`openai-compatible` 分支从环境读 `IELTS_MODEL_BASE_URL`、`IELTS_MODEL_API_KEY`
  （放 SSM SecureString，不进镜像），构造：

- **Responses API → Chat Completions 的两处差异**：
  1. 现有代码用 `OpenAIResponsesModel`，参数走 `max_output_tokens` 和 `reasoning.effort`。
     `OpenAIModel`（Chat Completions）用的是 `max_tokens`，且 **`reasoning.effort` 多数国内模型
     不支持**。审核步骤原本靠 `reasoning.effort=high` 来稳住判决，换模型后要用"更强的 system
     prompt 约束 + 更低 temperature（若该模型支持）"来补偿这份稳定性。
  2. 现有代码因 GPT-5.6 拒绝 `temperature` 而完全不传；国内模型通常**支持** `temperature`，可以
     （也建议）重新引入——生成步骤略高、审核步骤取 0 或接近 0，用它替代 `reasoning.effort` 的作用。
- 放宽区域校验：`SUPPORTED_REGIONS` / `assert_region_supported` 在 `openai-compatible` 下改为校验
  `cn-northwest-1`（本项目目标区），不再绑定 `us-east-*`。
- `list_models()._remote_models()` 当前调 `bedrock:ListFoundationModels`（宁夏没有），改为从配置
  读允许的模型清单（如一个环境变量或 `config/` 下的小文件），保留现有"发现失败时回退到部署默认
  模型"的语义。


### 主线二：模型 API 调用的认证变更与网络安全加固

模型已确定落在**中国区内**（Strands OpenAI 兼容直连国内大模型），所以**不存在跨境/跨分区调用**，
认证模型随之变化——从"SigV4 签 AWS 服务"变成"用第三方 API Key 调一个境内 SaaS 模型端点"：

- **认证从 SigV4 变为 API Key**：原来调 Bedrock Mantle；现在调
  国内模型是 `OpenAIModel(client_args={"api_key": ...})`。这个 `IELTS_MODEL_API_KEY` 是**第三方
  厂商的密钥**，必须存 **SSM SecureString（KMS 加密）**，由 AgentCore Runtime 角色注入，**绝不
  进镜像、不进日志、不进代码**（延续 `AGENT_DEPLOYMENT.md` "不输出密钥"的纪律）。它会过期/需轮换，
  按厂商策略建立轮换与告警。

- **出站到模型端点的网络加固**（这才是本主线的实质——Runtime 要出 VPC 访问一个公网 SaaS 端点）：
  - 走 **NAT Gateway 固定出口 + 安全组出站白名单**，只允许访问所选模型厂商的域名/地址，收敛出站
    面；
  - 强制 **TLS 1.2+**，校验服务端证书；
  - 若厂商提供 **VPC Endpoint / PrivateLink 或专线接入**（部分国内云厂商的大模型对企业客户提供），
    优先用它把流量留在内网，而不是裸走公网。
- **数据合规（境内）**：确认厂商的
  数据使用条款（是否用于训练、保留多久），并评估是否需要选择"不记录/不训练"的企业档位。


### 主线三：部署脚本（CDK / Terraform）的中国区适配

**第 3.1 层：现有 Shell 脚本的中国区适配：**

| 脚本 | 中国区必须改的点 |
|---|---|
| `deploy/config.sh` | `require_region()` 当前只放行 `us-east-1/us-east-2/us-west-2`，**改为放行 `cn-northwest-1`**；所有 ARN 拼接参数化为 `arn:${PARTITION}:`，`PARTITION=aws-cn`；核实宁夏默认 VPC 存在。 |
| 所有脚本 ARN 拼接 | 硬编码 `arn:aws:...` 全部换 `arn:aws-cn:...`：`provision.sh`（iam/s3/ssm/ecr ARN）、`service.sh`（task/exec role、SSM）、`edge.sh`、`runtime.sh`。托管策略 ARN 也要换：`arn:aws-cn:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy`。 |
| `provision.sh` | **删 `bedrock:*` 与 `bedrock-mantle:*` 权限**（宁夏无 Bedrock，模型改走国内 API，不再需要）；**AgentCore `*CodeInterpreter*`、`GetWorkloadAccessToken`、`bedrock-agentcore:InvokeAgentRuntime`、`polly:SynthesizeSpeech` 全部保留**（实测宁夏可用）；S3/SSM/ECR/logs 保留；全部 ARN 换分区。runtime role 的 assume-role 条件里的 `arn:aws:bedrock-agentcore:` SourceArn 换 `aws-cn`。 |
| `runtime.sh` | **基本保留，小改即可**：宁夏有 `bedrock-agentcore-control`，create/update-agent-runtime 照常用。改动仅为：注入的环境变量从 `IELTS_MODEL_*`（mantle 那套）改为国内 provider 的 `IELTS_MODEL_PROVIDER=openai-compatible` + `IELTS_MODEL_BASE_URL` + `IELTS_MODEL_API_KEY`；bearer-token 校验分支删掉；ARN/区域换宁夏。 |
| `service.sh` | **基本保留**：`bedrock-agentcore-control list-agent-runtimes` 取 `RUNTIME_ARN` 在宁夏照常；`AGENT_RUNTIME_ARN` 语义不变；`runtimePlatform` ARM64 需核实宁夏 Fargate 支持，否则回退 X86_64；SSM/IAM ARN 换分区。 |
| `edge.sh` | **改动最大**：CloudFront 中国区不支持 ACM，须第三方 CA 证书 `aws iam upload-server-certificate` 后引用；`*.cloudfront.net` 免费证书不成立；需 **ICP 备案自有域名**。鉴于宁夏节点退役，边缘层建议改"ALB + 自有域名 + 第三方证书"或评估其他合规 CDN。两个 managed policy id（CachingDisabled/AllViewerExceptHostHeader）中国区不保证存在，需核实或改自定义 policy。 |
| `backend/scripts/deploy.sh` | ECR 域名换 `*.dkr.ecr.cn-northwest-1.amazonaws.com.cn`；基础镜像 `public.ecr.aws/...python` 中国区拉取可能受限，建议预拉到中国区 ECR 再引用。 |


## 关键组件/实体的逐项映射

- **Web 层（ECS Fargate / FastAPI）** — 保留。`web/runtime_client.py` 仍 SigV4 调 AgentCore，仅
  ARN 换 `aws-cn`。ARM64 Fargate 宁夏可用性需核实，否则回退 X86_64。
- **运行底座（AgentCore Runtime）** — 保留（宁夏可用）。`runtime.sh` 小改：注入的模型环境变量换成
  国内 provider 那套，删 bearer 校验，ARN 换分区。
- **模型平面（Bedrock Mantle → 国内大模型）** — **唯一真改动**，集中在 `provider.py`，新增
  `OpenAIModel`（Chat Completions）分支，契约不变。
- **隔离沙箱（Code Interpreter）** — 保留（宁夏 `aws.codeinterpreter.v1` READY）。`sandboxed_metrics.py`
  仅 ARN 换 `aws-cn`。
- **语音合成（Polly）** — 保留（宁夏有 `en-GB` 英式音色）。`audio_storage/synthesize.py` 仅 ARN 换区。
- **对象存储（S3）** — 保留，ARN 换 `aws-cn`，加密可升 SSE-KMS，版本化/公网阻断/预签名播放不变。
- **会话密钥（SSM）** — 保留，ARN 换 `aws-cn`，用中国区 KMS。
- **模型 API Key** — 新增一条 SSM SecureString（`IELTS_MODEL_API_KEY`，第三方厂商密钥）。
- **边缘层** — 重做（见主线三 `edge.sh`）。

## 安全考量（Security Considerations）

- **模型 API Key 管理**：`IELTS_MODEL_API_KEY` 是第三方厂商密钥，存 SSM SecureString（KMS），由
  Runtime 角色注入，绝不进镜像/日志/代码；按厂商策略轮换并设到期告警。
- **出站面收敛**：Runtime 出 VPC 访问国内模型端点，走 NAT 固定出口 + 安全组出站白名单（只放行所选
  厂商域名/地址）+ 强制 TLS 1.2+；若厂商提供 PrivateLink/专线则优先走内网。
- **境内第三方数据处理**：生成内容/题库/用户数据会发给模型厂商，确认其数据使用条款（是否训练、
  保留期），必要时选"不记录/不训练"企业档位。不涉数据出境，但仍需合规确认。
- **模型内容合规**：国内模型须已完成算法备案，且通常自带内容安全；选型时确认其对教育/听力文本的
  适配与过滤策略不会误伤正常题目生成。
- **盲审隔离是出题质量生命线**（旅程四）：Code Interpreter 隔离保留，换模型后 `test_blindness.py` /
  `test_question_audit_isolation.py` 必须全绿，确认换模型没破坏隔离边界。
- **证书托管**：中国区边缘证书上传 IAM，私钥保管/轮换/权限纳入管理。
- **最小权限**：Runtime role 删掉不再需要的 `bedrock:*` / `bedrock-mantle:*`（保留 agentcore/
  codeinterpreter/polly），S3 仍限单桶。
- **登录加固**：保留自建逻辑，补失败锁定/口令策略/审计，保留 `ALLOWED_EMAIL_DOMAINS` fail-closed。

## 其他设计考量

- **容量与成本**：API 按量计费 + QPS/并发配额，`WEB_FANOUT_CONCURRENCY=20` 按所选模型的限流
  重标（429 下调，原则不变）。
- **延迟预算**：`IELTS_P95_PER_MATERIAL`（默认 240s，按 GPT-5.6 标定）用所选国内模型实测 p95 重设，
- **能力对齐**：生成/审核/修订对模型的指令遵循与长上下文有要求，选型前用 `smoke_model.py` 和一套
  真实场景端到端验证所选模型能稳定产出合规的 `material + blueprint` 。
- **可观测性**：CloudWatch 宁夏可用，AgentCore 命名空间指标保留；模型调用改为自埋点。
- **镜像与依赖拉取**：宁夏访问 `public.ecr.aws`、PyPI、npm 官方源可能慢/受限，基础镜像与依赖建议
  镜像到中国区（ECR / 私有源 / 国内镜像站）。

