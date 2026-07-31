# IELTS Listening Part 1 材料自动生成系统 · 技术部署手册

勾选场景，自动产出符合命制规范的雅思听力 Part 1 材料：对话脚本 + 信息点标注 + 英式口音语音，
并进入可对接的审核流转。

系统只产出**材料**，不产出题目与答案——命题是后续人工环节。因此材料的唯一质量意义在于
「能否支撑后续命题」，这也是题型适配性成为核心验收维度的原因。

面向使用者（出题人员）的说明见 [`USER_GUIDE.md`](USER_GUIDE.md)。本文件面向部署与运维。

---

## 1. 系统架构

```mermaid
flowchart TB
    U["浏览器<br/>React SPA"]

    subgraph EDGE["边缘"]
        CF["CloudFront<br/>HTTPS，*.cloudfront.net 证书<br/>CachingDisabled / Compress=false"]
        ALB["ALB（HTTP:80，公网）<br/>idle_timeout 120s"]
    end

    subgraph VPC["VPC · 公有子网"]
        ECS["ECS Fargate 任务<br/>FastAPI (web/) :80<br/>ARM64 · 0.5 vCPU / 1GB<br/>安全组只放行 ALB"]
    end

    subgraph AC["AgentCore Runtime（无公网入口）"]
        RT["backend/ Strands Agent<br/>+ 确定性 Loop<br/>ARM64 :8080"]
    end

    BR["Bedrock Mantle<br/>openai.gpt-5.6-terra"]
    PL["Polly neural<br/>en-GB × 3 voices"]
    S3["S3 单桶<br/>材料 / 音频 / 批次 / 用户"]
    SSM["SSM SecureString<br/>SESSION_SECRET"]

    U -- HTTPS --> CF
    CF -- HTTP --> ALB
    ALB --> ECS
    ECS -- "boto3 invoke_agent_runtime<br/>SigV4，任务角色临时凭证" --> RT
    ECS -- 读批次历史/用户 --> S3
    ECS -. 任务启动时读取 .-> SSM
    RT --> BR
    RT --> PL
    RT --> S3
```

几个约束是结构性的，不是风格选择：

- **浏览器永远不持有 AWS 凭证。** 前端只与 web 层自己的 `/api/*` 通信，登录是同源 HttpOnly
  session cookie。web 层用 ECS 任务角色的临时凭证代签调用 AgentCore。系统里没有任何长期密钥。
- **AgentCore Runtime 没有公网入口。** 只有携带 `bedrock-agentcore:InvokeAgentRuntime` 的调用
  方能进。
- **CloudFront 必须关缓存、关压缩。** 产品的核心是材料逐套流式返回；gzip 会把渐进流变成一个
  在结尾一次性交付的整块，缓存会把 A 用户的批次历史发给 B 用户。
- **`Compress: false` + 15 秒 SSE 心跳 + 120s ALB idle** 三者配套。实测一个 8 套的真实批次，
  事件之间最长静默 **96 秒**（216.6s → 313.0s），而 CloudFront 的 origin read 上限是 60 秒。
  加心跳后最长间隔降到 17 秒。**不要删心跳**，删掉就是流被中途切断。

### 每套材料一次独立调用（fan-out）

`web/fanout.py` 为批次里**每一套材料**发一次独立的 `invoke_agent_runtime`，再把 N 条流合并成
一条 SSE 返回浏览器。

这条设计决定了两件事：AgentCore 的 15 分钟同步硬限约束的是**单套**（实测 146–230s，离 900s
很远），所以**没有单批上限**；以及并发不再由后端内部决定，而由 web 层的
`WEB_FANOUT_CONCURRENCY`（默认 6）控制。模型侧出 429 时下调它，不要靠重试，也不要把批量上限
加回来。

合并用一个**专用 ThreadPoolExecutor**，不是 anyio 默认的 40 线程池——后者会被长连接占满，
导致其他请求（含 `/healthz`）排队。

---

## 2. 生成流程（Agent Loop）

```mermaid
flowchart LR
    S(["开始"]) --> G["AI 生成材料"]
    G --> V{"程序校验"}
    V -- "未通过，最多重试 3 次" --> G
    V -- "通过或重试结束" --> A["AI 盲审"]
    A --> X{"需要修改？"}
    X -- "否" --> P["采用原稿"]
    X -- "是" --> R["AI 修改"]
    R --> C["程序校验 + AI 复评"]
    C --> B["原稿与修改稿择优"]
    P --> D["交付材料"]
    B --> D
    D --> E(["完成"])

    classDef startEnd fill:#f3f4f6,stroke:#4b5563,color:#111827;
    classDef generate fill:#fef3c7,stroke:#d97706,color:#78350f;
    classDef audit fill:#dbeafe,stroke:#2563eb,color:#1e3a8a;
    classDef decision fill:#f3f4f6,stroke:#6b7280,color:#111827;
    classDef delivery fill:#dcfce7,stroke:#16a34a,color:#14532d;

    class S,E startEnd;
    class G,R generate;
    class A,C audit;
    class V,X,B decision;
    class P,D delivery;
```

黄色表示生成和修改，蓝色表示 AI 评价，灰色表示 Python 校验或流程决策，绿色表示采用与交付。

> **核心原则：Python 控制流程，AI 负责生成和评价。** AI 不能自行决定重试、通过或交付哪个版本。

### 2.1 两个 Skill

Skill 不是一个会自行运行的 Agent，而是“给 AI 的规范 + 给 Python 的检查脚本”。

| | 生成 Skill | 审核 Skill |
|---|---|---|
| 目录 | `generate-ielts-listening-part1/` | `audit-ielts-listening-part1/` |
| AI 规范 | `SKILL.md` + `references/specification.md` | `SKILL.md` + `references/audit-rubric.md` |
| Python 脚本 | `scripts/validate_part1.py` | `scripts/audit_metrics.py` |
| 职责 | 生成对话稿和十个信息点 | 评价自然度、难度和信息点质量 |

生成 AI 返回两份数据：

- `material`：对话稿；
- `blueprint`：十个信息点的答案、证据句、对话位置和适用题型。

### 2.2 程序校验

生成后，Python 运行 `validate_part1.py`，检查能够明确计算的规则：

| 检查类型 | 例子 |
|---|---|
| 数据格式 | 必填字段是否齐全，是否混入题目、答案或分析 |
| 结构数量 | 是否有三个说话人、三段旁白、十个信息点，字数和轮数是否合规 |
| 标注一致性 | 答案是否在证据句中，`turn_index` 是否指向正确对话，信息点顺序是否正确 |

程序不判断“对话是否自然”或“难度是否像 IELTS”。这些需要理解语义的问题留给审核 AI。

校验结果分为两级：

- `errors`：触发重新生成，最多三次；
- `warnings`：不阻断流程，只作为修改建议。

三次仍有错误时，系统保留最后一稿并附上校验发现。429、响应截断等基础设施故障另行重试，不占用
三次生成机会。

### 2.3 AI 盲审

审核前，`audit_metrics.py` 先准确计算字数、轮数、前后半段轮数和旁白字数，避免让 AI 自己计数。
审核 AI 只看到这些指标和 `material`，**看不到生成 AI 的 `blueprint`**。

它必须独立阅读对话，重新找出可以命题的信息点。随后 Python
用 `deterministic/crosscheck.py` 对比两份结果：

| 生成 AI 计划的信息点 | 审核 AI 独立找到的信息点 | 结果 |
|---|---|---|
| 入住日期：12 July | 入住日期：12 July | 匹配 |
| 房型：double room | 房型：double room | 匹配 |
| 价格：£85 | 未找到 | 需要检查或修改 |

盲审验证的是：**生成者设计的信息点，能否只通过实际对话被独立找出来。** 如果审核 AI 提前看到
`blueprint`，它可能顺着答案寻找证据，这项检查就失去了意义。

### 2.4 修改与交付

- 原稿没有问题：直接采用原稿；
- 原稿需要修改：AI 同步修改 `material` 和 `blueprint`；
- 修改完成：再次程序校验，并由一个无历史记忆的新审核 AI 复评；
- 最终选择：Python 比较原稿和修改稿，采用评价更好的版本；
- 修改失败、校验未通过或时间不足：回退到已完成评价的原稿。

最终只交付选中的文字材料及其评价、校验信息，不合成音频。

---

## 3. 目录

| 目录 | 职责 |
|---|---|
| `skills/ielts-listening-skills/` | 生成与评价的 skill 契约、三份 JSON Schema、确定性校验脚本 |
| `backend/` | AgentCore Runtime：Strands Agent + 确定性 Loop 编排；候选注册表以 S3 共享 |
| `audio_storage/` | Polly 逐 turn 合成、manifest、S3 状态流转 |
| `web/` | FastAPI Web 层：静态服务 + 自建登录 + SigV4 代理 + SSE fan-out |
| `frontend/` | React + TypeScript + Vite 审阅界面 |
| `deploy/` | 部署与启停脚本 |
| `config/scenarios.yaml` | 场景清单（6 大类 16 个场景 + 自定义场景） |
| `material/Part1_选材命制规范.md` | 命制规范（基于 20 套真题分析归纳） |
| `backend/docs/` | 实测数据：`timing.md`（耗时）、`model-access.md`（模型接入）、`handover.md` |

---

## 4. 部署所需权限

### 4.1 部署者（跑 `deploy/*.sh` 的那个身份）

脚本会创建 IAM 角色并给它们附策略，所以部署者需要 IAM 写权限。最省事的是 `AdministratorAccess`；
若要收窄，下面是实际被调用的动作面：

| 服务 | 动作 |
|---|---|
| STS | `sts:GetCallerIdentity` |
| S3 | `s3:CreateBucket`、`PutBucketVersioning`、`PutBucketPublicAccessBlock`、`PutEncryptionConfiguration`、`ListBucket`、`GetObject`/`PutObject`（拆卸时还需 `DeleteObject*`、`ListBucketVersions`、`DeleteBucket`） |
| ECR | `CreateRepository`、`DescribeRepositories`、`GetAuthorizationToken`、`BatchCheckLayerAvailability`、`PutImage`、`UploadLayerPart`、`InitiateLayerUpload`、`CompleteLayerUpload`、`ListImages` |
| ECS | `CreateCluster`、`DescribeClusters`、`RegisterTaskDefinition`、`CreateService`、`UpdateService`、`DescribeServices`、`ListTasks`、`DescribeTasks`（拆卸另需 `DeleteService`、`DeleteCluster`） |
| EC2 | `DescribeVpcs`、`DescribeSubnets`、`CreateSecurityGroup`、`DescribeSecurityGroups`、`AuthorizeSecurityGroupIngress`、`RevokeSecurityGroupIngress`、`DescribeNetworkInterfaces`（拆卸另需 `DeleteSecurityGroup`） |
| ELBv2 | `CreateTargetGroup`、`DescribeTargetGroups`、`DescribeTargetHealth`、`CreateLoadBalancer`、`DescribeLoadBalancers`、`ModifyLoadBalancerAttributes`、`CreateListener`、`DescribeListeners`（拆卸另需 `Delete*`） |
| CloudFront | `CreateDistribution`、`GetDistribution`、`GetDistributionConfig`、`ListDistributions`、`UpdateDistribution`、`DeleteDistribution` |
| IAM | `CreateRole`、`GetRole`、`PutRolePolicy`、`AttachRolePolicy`、`ListRolePolicies`、`ListAttachedRolePolicies`、`PassRole`（拆卸另需 `Delete*`） |
| SSM | `PutParameter`、`GetParameter`（`/ielts-part1/session-secret`） |
| CloudWatch Logs | `CreateLogGroup`、`PutRetentionPolicy`、`DescribeLogGroups`、`GetLogEvents` |
| AgentCore | `bedrock-agentcore-control:CreateAgentRuntime`、`UpdateAgentRuntime`、`ListAgentRuntimes`、`GetAgentRuntime`（拆卸另需 `DeleteAgentRuntime`） |

`iam:PassRole` 是必需的：ECS 任务定义和 AgentCore Runtime 都要把角色交给服务。

### 4.2 运行期角色（由 `deploy/provision.sh` 创建）

三个角色，边界是刻意分开的——「能启动容器」不蕴含「能调模型」。

**`ielts-part1-ecs-exec`**（ECS 执行角色，拉镜像、写日志、读 secret）
- 托管策略 `AmazonECSTaskExecutionRolePolicy`
- 内联：`ssm:GetParameters` on `arn:aws:ssm:{region}:{account}:parameter/ielts-part1/session-secret`

**`ielts-part1-web-task`**（web 层 boto3 用的任务角色）
```
bedrock-agentcore:InvokeAgentRuntime  → runtime/*  和  runtime/*/endpoint/*
s3:GetObject / s3:PutObject           → arn:aws:s3:::{bucket}/*
s3:ListBucket                         → arn:aws:s3:::{bucket}
logs:CreateLogStream / PutLogEvents   → *
```
> `InvokeAgentRuntime` 同时对 runtime 与其 endpoint 资源鉴权，所以两种 ARN 形状都要列。
> id 上的通配无法避免——provision 时 runtime 还不存在。`deploy/runtime.sh` 会打印真实 ARN，
> 之后把这条收窄到它是一行改动，若这套系统长期运行值得做。

**`ielts-part1-runtime`**（AgentCore Runtime 的执行角色）
```
ecr:BatchGetImage / GetDownloadUrlForLayer  → repository/ielts-part1-backend
ecr:GetAuthorizationToken                   → *
bedrock:InvokeModel / InvokeModelWithResponseStream        → *
bedrock-mantle:CreateInference / CreateResponse
              / CreateChatCompletion / CallWithBearerToken → *
bedrock:CallWithBearerToken                                → *
polly:SynthesizeSpeech                      → *
s3:GetObject / PutObject / DeleteObject     → arn:aws:s3:::{bucket}/*
s3:ListBucket                               → arn:aws:s3:::{bucket}
logs:*（5 个动作）                           → *
cloudwatch:PutMetricData                    → 限定 namespace = bedrock-agentcore
bedrock-agentcore:GetWorkloadAccessToken    → *
xray:PutTraceSegments / PutTelemetryRecords → *
```

信任策略带 `aws:SourceAccount` / `aws:SourceArn` 条件（confused-deputy 防护）。

> **`bedrock-mantle` 是独立的服务前缀**，不是 `bedrock` 的子集。GPT-5.6 只经 mantle 端点提供，
> `bedrock:InvokeModel` 覆盖不到它——缺权限时报的是
> `access_denied ... not authorized to perform: bedrock-mantle:CreateInference`。
> 需要两个动作：`CreateInference` 用于调用本身，`CallWithBearerToken` 因为 Strands 的
> `bedrock_mantle_config` 每次请求现铸一个 bearer token 而非直接 SigV4 签名。这两条在公开
> 文档里查不到，是一次次 401 里试出来的。

---

## 5. 部署后会创建的 AWS 资源

| 类型 | 名称 | 创建者 |
|---|---|---|
| S3 桶 | `ielts-part1-materials-{account}` | `provision.sh` |
| ECR 仓库 ×2 | `ielts-part1-backend`、`ielts-part1-frontend` | `provision.sh` |
| CloudWatch 日志组 | `/ecs/ielts-part1-web`（保留 7 天） | `provision.sh` |
| 安全组 | `ielts-part1-web-sg`（无 ingress，由 edge.sh 授权） | `provision.sh` |
| ECS 集群 | `ielts-part1` | `provision.sh` |
| IAM 角色 ×3 | `-ecs-exec`、`-web-task`、`-runtime` | `provision.sh` |
| AgentCore Runtime | `ielts_part1_runtime` | `runtime.sh` |
| SSM 参数 | `/ielts-part1/session-secret`（SecureString） | `service.sh` |
| ECS 任务定义 | `ielts-part1-web`（ARM64，512 CPU / 1024 MB） | `service.sh` |
| ECS 服务 | `ielts-part1-web` | `service.sh` |
| 安全组 | `ielts-part1-alb-sg`（tcp/80 from `INGRESS_CIDR`） | `edge.sh` |
| 目标组 | `ielts-part1-tg`（健康检查 `/healthz`） | `edge.sh` |
| ALB + 监听器 | `ielts-part1-alb`（idle 120s） | `edge.sh` |
| CloudFront 分发 | PriceClass_100，origin = ALB | `edge.sh` |

**成本形态**：只有两项常驻收费——**运行中的 Fargate 任务**（`start.sh` / `stop.sh` 控制）和
**ALB**（约 $16–18/月，无论后面有没有任务）。S3、ECR、CloudFront、AgentCore 都是按量。
`stop.sh` 停任务但保留 ALB 与 CloudFront（否则交付出去的链接会死掉，重建还会换域名）；
真要把成本归零只能 `teardown.sh`，代价是那个 URL 永久失效。

---

## 6. 从零部署

前置：AWS 凭证（**必须 us-east-1 或 us-east-2**，GPT-5.6 无跨区推理）、Docker（支持
`--platform linux/arm64`）、Python 3.12、Node 20+、awscli v2。

```bash
# 0. 确认身份与区域
aws sts get-caller-identity
export AWS_REGION=us-east-1

# 1. 基础设施（幂等，可反复跑）
bash deploy/provision.sh

# 2. 推 Runtime 镜像（ARM64，≤2GB）
bash backend/scripts/deploy.sh \
  $(aws sts get-caller-identity --query Account --output text).dkr.ecr.$AWS_REGION.amazonaws.com/ielts-part1-backend

# 3. 创建 AgentCore Runtime
bash deploy/runtime.sh

# 4. 建 ALB + CloudFront（**在 service.sh 之前**，见下方说明）
bash deploy/edge.sh

# 5. 推 Web 镜像 + 创建 ECS 服务（务必限制注册域名）
ALLOWED_EMAIL_DOMAINS=example.com bash deploy/service.sh

# 6. 拉起，并打印交付用的 URL
bash deploy/start.sh
```

**步骤顺序**：`edge.sh` 要在 `service.sh` 之前跑。ECS **不能给已存在的服务原地挂负载均衡器**，
所以 `service.sh` 检测到一个「ALB 之前建好的」服务时会删掉并重建（会保留 desiredCount）。先跑
`edge.sh` 就没有这次重建。

**验证**：
```bash
bash deploy/status.sh          # 各资源状态 + 当前 CloudFront 域名 + 目标健康
curl -si https://<cf-domain>/healthz | head -1     # 期望 200
```

新建的 CloudFront 分发要 **5–10 分钟**才全球生效，这段时间可能 502/503。`status.sh` 里
`CloudFront: ... (Deployed)` 才算好。

**日常启停**：
```bash
bash deploy/stop.sh    # desiredCount=0；URL 保留，但会答 503
bash deploy/start.sh   # desiredCount=1，等到 stable 后打印 URL
```

**拆除**：
```bash
bash deploy/teardown.sh --yes             # 保留 S3 里生成的材料
bash deploy/teardown.sh --yes --purge-s3  # 连材料一起删（桶开了版本控制，脚本会逐版本清）
```

### 交付地址会不会变？

**不会。** 交付的是 `https://dxxxxxxxxxxxxx.cloudfront.net`，它绑定分发 ID，不随部署改变。
下列操作都不影响它：重新推镜像、`stop.sh` / `start.sh`、ECS 滚动更新（任务 IP 会变，域名不变）、
改 `ALLOWED_EMAIL_DOMAINS`。

只有一件事会换域名：**删掉分发再建**（即 `teardown.sh`）。新分发必然拿到不同的
`*.cloudfront.net` 主机名，无法找回。

---

## 7. 可配置项

### 7.1 部署参数（`deploy/config.sh`，全部可用环境变量覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AWS_REGION` | `us-east-1` | 只允许 us-east-1 / us-east-2 |
| `ACCOUNT_ID` | 从调用者凭证发现 | 仓库里不写死账号，也不留任何账号信息 |
| `AWS_PROFILE` | 未设 | 设了才用命名 profile，否则走默认凭证链 |
| `S3_BUCKET` | `ielts-part1-materials-{account}` | 单桶承载全部数据 |
| `VPC_ID` / `SUBNET_IDS` / `SUBNET_ID` | 默认 VPC 的公有子网 | ALB 需要 ≥2 个可用区；`SUBNET_ID` 是任务所在的那一个 |
| `INGRESS_CIDR` | `0.0.0.0/0` | 谁能访问 **ALB**。任务本身没有公网 ingress |
| `ALB_IDLE_TIMEOUT` | `120` | 必须远大于 `WEB_SSE_HEARTBEAT` |
| `WEB_PORT` | `80` | 不用 8080：很多企业网络封出向 8080，正好封死演示 |
| `TASK_CPU` / `TASK_MEMORY` | `512` / `1024` | web 层只做代理和静态服务 |

> `INGRESS_CIDR=0.0.0.0/0` 是 CloudFront 能取到源站的前提——CloudFront 从一大批不断变化的
> 公网 IP 回源。收窄成办公网 CIDR 会**打断 CloudFront，而不是加固它**。代价说清楚：
> 谁拿到 ALB 主机名，就能绕过 CloudFront 用明文 HTTP 直连应用。访问控制仍然靠应用登录 +
> `ALLOWED_EMAIL_DOMAINS`。要真正锁死源站，需要
> `com.amazonaws.global.cloudfront.origin-facing` 托管前缀列表 + 自定义 header 校验——
> 目前**没有做**。

### 7.2 Web 层（ECS 任务定义环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ALLOWED_EMAIL_DOMAINS` | `*` | 谁能注册。**公网暴露前必须设成你的域名。** 改它只需更新任务定义并重启，不用重建镜像；已注册账号不受影响 |
| `SESSION_SECRET` | 无兜底 | 由 `service.sh` 随机生成存入 SSM SecureString。代码里**故意没有默认值**——共享用户存储下缺它会拒绝启动，以防多实例签名不一致造成静默掉登录 |
| `AGENT_RUNTIME_ARN` | 由 `service.sh` 注入 | 目标 Runtime |
| `AGENT_RUNTIME_QUALIFIER` | 未设 | 指定 endpoint 版本 |
| `IELTS_AUDIO_BUCKET` | `{S3_BUCKET}` | 数据桶 |
| `USER_STORE_S3_BUCKET` / `USER_STORE_S3_KEY` | 桶 / `web/users.json` | 不设桶则退化为本地 JSON 文件（仅本地开发） |
| `WEB_FANOUT_CONCURRENCY` | `6` | 同时在跑的 AgentCore 调用数。**429 就下调它** |
| `WEB_SSE_HEARTBEAT` | `15` | 心跳秒数。必须远小于 CloudFront 的 60s origin read |
| `WEB_RUNTIME_READ_TIMEOUT` | `900` | 与 AgentCore 的 15 分钟硬限对齐 |
| `WEB_RUNTIME_CONNECT_TIMEOUT` | `10` | |
| `WEB_PER_MATERIAL_WALL` | `900` | 单套墙上时钟 |
| `PORT` | `80` | |

### 7.3 Runtime（`deploy/runtime.sh` 注入，其余用代码默认）

| 变量 | 默认 | 说明 |
|---|---|---|
| `IELTS_MODEL_ID` | `openai.gpt-5.6-terra` | 同族切换（`-sol` / `-luna`）不用重建镜像 |
| `IELTS_MODEL_REGION` | 同 `AWS_REGION` | 必须 us-east-1 / us-east-2 |
| `IELTS_MODEL_AUTH` | `mantle` | `bearer` **仅限本地开发**（预铸 token 会过期且无人刷新） |
| `IELTS_CONCURRENCY` | `6` | 单次调用内的并发槽。生产实际被夹到 1（一次调用一套材料），只对 CLI 有意义；实测安全值是 3 |
| `IELTS_HARD_LIMIT` | `900` | 平台同步硬限 |
| `IELTS_P95_PER_MATERIAL` | `240` | 实测典型 146s，240 覆盖两次重生成 |
| `IELTS_REVISION_COST` | `120` | 实测 revise + 复评 ≈ 44s，120 是刻意保守 |
| `IELTS_SAFETY_MARGIN` | `90` | 留给发 summary 事件并干净收尾 |
| `IELTS_MAX_REFILL_ROUNDS` | `2` | 补生成轮数 |
| `IELTS_SCRIPT_TIMEOUT` | `60` | 确定性校验脚本超时 |
| `IELTS_SCENARIOS_PATH` / `IELTS_SKILLS_ROOT` / `IELTS_SCRIPT_PYTHON` | 镜像内路径 | 一般不用改 |

### 7.4 前端（`frontend/public/config.json`，运行时拉取，不用重建）

`thresholds` 是界面告警阈值（信息点均匀度 `CV_WARN` / `CV_FAIL`、密集判定 `CLUSTER_SPAN` /
`CLUSTER_MIN_POINTS` 等），`limits.perMaterialWallSeconds` 是单套墙上时钟，
`flags.audioWebaudio` 关。合并策略是「宽进严出」：缺键退回内置默认而不是崩页面。

> `CALIBRATED: false` 是诚实标注——`CV_WARN` / `CV_FAIL` 目前是启发式取值，界面上写着
> 「参考值·阈值待校准」。积累一批真实材料后需要人工校准。

---

## 8. S3 目录结构

单桶，五个互不重叠的顶层命名空间：

```
s3://ielts-part1-materials-{account}/
│
├── pending/{scenario_key}/{material_id}/       ← 已发布、待审核
│   ├── material.json                            脚本 + 元数据
│   ├── blueprint.json                           信息点标注（十个点）
│   ├── audit.json                               评价结果与校验发现
│   └── audio/
│       ├── turn_001.mp3 … turn_0NN.mp3          逐 turn 合成，30–45 个
│       └── manifest.json                        ★ 完整性哨兵，最后写
│
├── approved/{scenario_key}/{material_id}/      ← 同上结构
├── rejected/{scenario_key}/{material_id}/
├── production/{scenario_key}/{material_id}/
│
├── _history/{material_id}/{ts}-{src}-{dst}.json   状态迁移审计轨
│
├── _batches/{batch_id}/                        ← web 层的批次历史
│   ├── index.json                                批次索引（场景、套数、状态、owner）
│   └── materials/{material_id}.json
│
├── _candidates/{material_id}.json              ← 候选注册表，TTL 24h
├── _candidates/{material_id}.job.json            音频合成任务状态
├── _claims/{group_key}.json                    ← 选稿占位
│
└── web/users.json                              ← 用户存储（bcrypt 口令散列）
```

三点值得单独知道：

- **`audio/manifest.json` 是完整性哨兵，永远最后写。** 一个材料目录只要没有 manifest 就按定义
  算「不完整」，读侧直接不显示。这样就不需要额外的锁或状态字段来表达「合成到一半」。
- **`_history/` 故意放在状态目录外面。** 审计轨要活得比被迁移的材料久，也要活得比它被删除久。
  跨目录 copy 只是状态变更的实现方式，history 才是变更本身。copy + delete 不是原子的，所以
  迁移做成六步、带意图标记、只向前恢复：任何崩溃点留下的都是「源完整」或「目标完整」，不会
  两边都不完整。
- **没有 `quarantine/`。** 早期版本把 FAIL / NOT_ASSESSABLE 路由到一个无音频的隔离区；现在
  每一套发布出来的材料都进 `pending`——用户要两套就给两套，有缺陷的那套连缺陷一起给，由用户
  决定。老桶里残留的 `quarantine/` 前缀没有任何代码去扫，是惰性数据，不需要迁移。
- **桶开了版本控制**，因为一次坏的修订覆盖掉好材料应当可恢复。代价是 `teardown.sh --purge-s3`
  必须逐版本、逐 delete-marker 清理才能删桶。

---

## 9. 本地开发

```bash
# 契约与校验（Python 3.9+，无第三方依赖）
python3 skills/ielts-listening-skills/shared/tests/run_tests.py
python3 audio_storage/tests/run_tests.py

# 后端与 Web 层（Python 3.12）
python3.12 -m venv .venv-backend && .venv-backend/bin/pip install -e 'backend[dev]'
.venv-backend/bin/python -m pytest backend/tests -q
.venv-backend/bin/python web/tests/run_tests.py
bash backend/scripts/ci_gates.sh          # 8 项结构门禁

# 前端
cd frontend && npm ci && npm run verify   # codegen 校验 + 类型 + lint + 测试
```

前端类型由 schema 生成，不手写：`npm run codegen:check` 保证两者不漂移。

**8 项结构门禁**分别是：评价步骤不携带任何命题标识（盲读的 grep 防线）、确定性层不依赖模型、
没有手写的 token 刷新逻辑、skill 资产保持 Python 3.9 可解析、后端源码可解析、skill 契约回归
（78 项检查）、后端单测、镜像 COPY 覆盖了它 import 的每个一方包。

单套端到端冒烟：
```bash
.venv-backend/bin/python backend/scripts/run_one.py --scenario booking-hotel
.venv-backend/bin/python backend/scripts/smoke_model.py     # 只验模型可达
bash backend/scripts/check_ping.sh                          # Runtime /ping
```

---

## 10. 故障排查

| 现象 | 原因与处理 |
|---|---|
| CloudFront 答 **503**，`status.sh` 显示 `running=0` | 任务是停的。`bash deploy/start.sh`（约 60–120s） |
| CloudFront 答 **502**，任务在跑 | 目标组不健康。`aws elbv2 describe-target-health` 看状态；`/healthz` 不通通常是容器启动失败，查日志组 `/ecs/ielts-part1-web` |
| 新分发一直 502/503 | 分发还在 `InProgress`。等 5–10 分钟，`status.sh` 显示 `Deployed` 为止 |
| **生成过程中流突然断掉**，前端停在「生成中」 | 心跳链路被破坏。核对三个值：`WEB_SSE_HEARTBEAT=15`、ALB idle ≥120、CloudFront `OriginReadTimeout=60`；并确认 `Compress: false`。实测事件间最长静默 96s，无心跳必断 |
| 材料**一次性全部出现**而不是逐套 | CloudFront 打开了压缩。`Compress` 必须是 `false` |
| 登录后**随机掉登录** | `SESSION_SECRET` 没注入或多实例不一致。共享用户存储下缺它会直接拒绝启动，所以更可能是任务定义被手改过 |
| `401 invalid_api_key - The security token included in the request is invalid` | **不是模型权限被撤**。是本机 SigV4 凭证过期。`aws sts get-caller-identity` 一条命令即可区分 |
| `access_denied ... bedrock-mantle:CreateInference` | runtime 角色缺 mantle 权限。见 §4.2，`bedrock:InvokeModel` 覆盖不到它 |
| 模型 **429 / 限流** | 下调 `WEB_FANOUT_CONCURRENCY`（默认 6）。**不要加重试，也不要恢复批量上限** |
| 某场景要 3 套只出了 2 套 | 系统已自动补生成过（`IELTS_MAX_REFILL_ROUNDS=2`）。界面组标题的「2/3」就是在说这件事，顶部有「补生成」入口 |
| 材料带 `degraded: true` / `time_budget` | 单套时间预算不够做可选的修改环节，材料本身完整可用。要减少这种情况就调高 `IELTS_P95_PER_MATERIAL` 的余量 |
| 试听点了没反应 | 首次合成 30–45 个 Polly 请求，需 1–2 分钟。`_candidates/{material_id}.job.json` 是任务状态；音频写在 `pending/.../audio/`，manifest 出现即完成 |
| 换了区域后模型不可用 | GPT-5.6 无跨区推理。`config.sh` 的 `require_region` 会直接拒绝非 us-east-1/2 |
| `service.sh` 删了服务又重建 | 预期行为：ECS 不能给已有服务原地挂 LB。先跑 `edge.sh` 再跑 `service.sh` 就不会发生 |
| 目标组删不掉（`still referenced`） | ALB 删除是异步的。等一分钟重跑 `teardown.sh` |
| 安全组删不掉（`still in use`） | ENI 还没释放。等一分钟重跑 |
| CloudFront 删不掉 | 必须先 disable 并等传播完成。`teardown.sh` 已经是「先禁用 → 干别的 → 最后删」的顺序；提示 `not deletable yet` 就重跑一次 |

日志位置：
- Web 层 → CloudWatch 日志组 `/ecs/ielts-part1-web`
- Runtime → CloudWatch，日志组由 AgentCore 平台按 runtime 名建

---

## 11. 安全说明

- **仓库里没有任何密钥、账号 ID 或个人信息。** 所有敏感值走环境变量或 SSM SecureString；
  账号与网络参数在运行时从调用者凭证发现。
- **口令用 PBKDF2-HMAC-SHA256 散列**存储（200,000 次迭代、16 字节随机盐，格式
  `pbkdf2_sha256$<iterations>$<salt>$<derived>`）；session 是同源 HttpOnly cookie，
  由 `SESSION_SECRET` 用 HMAC-SHA256 签名，有效期 7 天。
- **S3 桶全私有**（四项 public-access-block 全开）+ SSE-S3。音频经预签名 URL 交付，没有任何
  对象需要公开。
- **未做**（明确列出，不掩盖）：源站锁定（谁知道 ALB 主机名就能明文 HTTP 直连）、WAF、
  MFA、审计日志外发、CloudFront 自定义错误页（停机时是原始 503）。

---

## 12. 已知限制

- **均匀度阈值未校准**：`frontend/public/config.json` 的 `CV_WARN` / `CV_FAIL` 是启发式取值
  （`CALIBRATED: false`），界面已标注「参考值·阈值待校准」。积累若干真实材料后需人工校准。
- **`WEB_FANOUT_CONCURRENCY=6` 未实测**：这个值继承自 `IELTS_CONCURRENCY` 未出 429 时的取值，
  但那些槽共享同一个模型会话，所以只是提示而非证据。出现限流就下调。
- **雅思真题样本未随仓库分发**（版权原因）。依赖它们的回归测试会 SKIP 而非失败。
- **单可用区任务**：ECS 服务只跑 1 个任务，在一个子网里。ALB 跨 2 个可用区是它自身的要求，
  不代表应用有冗余。
- **无自动伸缩、无蓝绿**：滚动更新期间会短暂不可用。

---

## 声明

本项目产出原创练习材料，不复制真题内容，也不代表 IELTS / Cambridge / British Council 的
任何授权或背书。
