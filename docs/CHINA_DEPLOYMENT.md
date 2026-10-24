# 中国区 POC：Python SDK 发布与 ALB 入口

本分支使用 Python / boto3 工具发布中国区应用。原来的海外 CloudFront 部署保留在
`main` 和历史版本中，迁移分支不维护两套入口，也不创建 CloudFront。

## 本阶段状态

- 截至 2026-10-23，`china_infra.py` 的 prepare、edge、web 已实际执行；
  中国区初始 Runtime 已达到 `READY`，`list_models`、scenarios、candidates 检查返回 200。
  Web/ECS 已稳定运行、ALB target healthy；独立云端请求首页/health 为 200，未登录 me 为 401。
  用户已确认断开公司 VPN 后可加载登录页，VPN 下访问超时应单独排查。
- 当前完整发布路径限定宁夏 `cn-northwest-1`：公网 ALB HTTP 9000，内部 Web 80。
  个别底层工具和旧 shell 配置支持北京，不代表完整发布工具支持北京。
- 使用中国区 Secrets Manager 引用访问海外 AWS Bedrock 托管的 Kimi K2.5；
  secret-backed JSON 和工具调用往返已单独验证。生产 Runtime 不依赖开发机海外 profile。
- 这些结果证明部署和部分接口已可用，不等同于客户网络、登录、SSE、完整生成质量、
  修订和 QTI 的全量验收。首轮合成测试已保存材料并在出题前按预算暂停；
  不代表整套题目已交付。续跑已进入题目校验、盲审及修订；答案分歧仍按原有门槛拦截。
  Runtime 第 2 版已实测静默阶段的 15 秒传输心跳，Web 无需同步更新。验收边界见下文。
- 音频是独立可选功能，不阻塞核心应用；不迁移历史数据。不自动提交、推送或合并代码。
- 旧 shell 的模型发布 gate 继续保留；当前发布使用下述 Python 工具，不通过解除旧 gate 上线。

## 访问结构

```text
客户浏览器
  └─ http://<ALB DNS>:9000
       └─ 公网 ALB HTTP listener :9000
            └─ target group → Web container :80
                 └─ 中国区 AgentCore Runtime
                      ├─ 中国区 Secrets Manager（运行时解析模型凭证）
                      ├─ 中国区 Code Interpreter（审计沙箱）
                      └─ 海外 Bedrock Kimi（模型区域独立配置）
```

公网监听端口和容器端口独立。容器安全组只允许 ALB 安全组访问，不开放公网 80。
ALB DNS 随该 ALB 保留，容器滚动更新不改变入口；删除重建 ALB 后需要重新取得地址。

这是 HTTP POC，登录和会话流量没有 HTTPS 传输加密。非标准端口不是备案豁免或免安全检查的
承诺。部署前应确认本次测试的运营商要求，以及客户办公网络能否访问所选端口。

## 当前发布配置

从仓库根目录运行工具，使用装有项目依赖和 boto3 的 Python 环境。本节示例只包含占位符；
profile 必须已有可用的中国区凭证。每个工具都会验证实际账号，不能用环境中的默认账号替代。

| 设置 | 值 / 含义 |
|---|---|
| `--profile` | 必须显式提供的中国区 profile |
| `--expected-account` | `china_infra.py`、`runtime_release.py` 的目标账号；`push_image.py` 对应参数叫 `--account` |
| `--region` | 完整流程统一显式指定 `cn-northwest-1` |
| `--image` | 对应中国区后端或前端 ECR 仓库的完整 `URI@sha256:<digest>`，不接受可变 tag |
| `--runtime-arn` | Web 阶段必填，同账号宁夏 Runtime，发布前检查 `READY` |
| `--secret-arn` | Runtime 发布必填，同账号同区域的完整模型 Secret ARN，环境中只注入引用 |
| `--email-domains` | Web 注册域名，默认 `example.com`，支持逗号分隔，不允许 `*` |
| 网络与端口 | 自动发现默认 VPC，验证至少两个 AZ 的子网有有效公网 IGW 路由；ALB 9000、Web 80 |
| 材料桶 | `ielts-part1-materials-<account>`，私有、开启版本管理、SSE-S3 |
| Web 会话密钥 | SSM SecureString `/ielts-part1/session-secret`，首次发布在内存生成，后续复用 |

ARN 使用 `arn:aws-cn:...`；私有镜像仓库格式：
`<account>.dkr.ecr.<china-region>.amazonaws.com.cn/<repository>`。
IAM service principal 不按 API endpoint 后缀进行全局字符串替换；ECS 使用
`ecs-tasks.amazonaws.com`，Runtime 使用 `bedrock-agentcore.amazonaws.com`。

`china_infra.py` 固定公网 IPv4 入口 `0.0.0.0/0:9000`、ALB idle timeout 120 秒。
它不读取旧 shell 的 `INGRESS_CIDR`、`VPC_ID`、`SUBNET_IDS` 或端口覆盖变量。
`IELTS_MODEL_REGION=us-east-1` 由 Runtime 环境独立配置，不用于选择基础设施区域。

## Python SDK 发布流程

以下云端发布命令默认 dry-run，仍会执行只读账号和资源检查。审阅每一步输出后，
重新运行同一条命令并添加 `--apply`；`status` 始终只读，不能添加 `--apply`。
本地 `docker build` 会实际构建镜像，不受这些工具的 dry-run 控制。

```bash
PY=.venv/bin/python
PROFILE='<中国区 profile>'
ACCOUNT_ID='<12位中国区账号>'
REGION=cn-northwest-1
RELEASE_TAG='<本次未使用过的发布标签>'
SECRET_ARN='<同账号宁夏模型 Secret 完整 ARN>'
```

### 1. Prepare 与镜像构建、推送

先完成[生产模型凭证与 Runtime 执行角色准备](CHINA_MODEL_CREDENTIALS.md)，然后检查并 apply：

```bash
"$PY" deploy/china_infra.py prepare \
  --profile "$PROFILE" --expected-account "$ACCOUNT_ID" --region "$REGION"
```

Prepare 创建私有且版本化的 SSE-S3 桶、两个不可变 ECR 仓库、ECS cluster、
保留 7 天的日志组，以及分离的 ECS execution / Web task roles。
现有资源必须有工具要求的归属标签；未授权、信任关系冲突或额外 IAM 策略不会被当作资源缺失。

在 prepare apply 完成后构建 Linux ARM64 镜像，构建上下文使用仓库根目录：

```bash
docker build --platform linux/arm64 -f backend/Dockerfile \
  -t "ielts-backend:$RELEASE_TAG" .
docker build --platform linux/arm64 -f web/Dockerfile \
  -t "ielts-web:$RELEASE_TAG" .

"$PY" deploy/push_image.py --profile "$PROFILE" --account "$ACCOUNT_ID" \
  --region "$REGION" --repository ielts-part1-backend \
  --local-image "ielts-backend:$RELEASE_TAG" --tag "$RELEASE_TAG"
"$PY" deploy/push_image.py --profile "$PROFILE" --account "$ACCOUNT_ID" \
  --region "$REGION" --repository ielts-part1-frontend \
  --local-image "ielts-web:$RELEASE_TAG" --tag "$RELEASE_TAG"
```

审阅后分别添加 `--apply` 推送。工具检查本地镜像架构、目标账号、不可变仓库及 tag 未被使用；
ECR 登录密码只通过 subprocess stdin 传递，临时 Docker auth 配置用完删除。
后端镜像还受项目 2 GB 大小上限约束。不要向构建上下文加入凭证或私有证据；
仓库 `.dockerignore` 排除了本地环境、`.aws/` 和 `.trellis/` 等目录。

保存两次 push 输出的 **`pinned_image`**，后续使用 digest，不用本地 image ID 或 tag：

```bash
BACKEND_IMAGE='<后端 push 输出的 pinned_image>'
WEB_IMAGE='<前端 push 输出的 pinned_image>'
```

### 2. Runtime 发布

```bash
"$PY" deploy/runtime_release.py --profile "$PROFILE" \
  --expected-account "$ACCOUNT_ID" --region "$REGION" \
  --secret-arn "$SECRET_ARN" --image "$BACKEND_IMAGE"
```

工具检查 Secret 元数据和有效期、已配置的精确 Runtime 角色、镜像 digest，
以及现有 Runtime 的归属和状态。环境使用 `IELTS_MODEL_AUTH=secret`、Secret ARN、
Kimi 模型 ID 和独立的模型区域；不会把模型 token 写入发布配置。
Runtime 使用 HTTP/PUBLIC，idle timeout 900 秒，最大 session lifetime 28800 秒。

添加 `--apply` 创建或更新后，记录输出的 Runtime ARN、版本和镜像 digest。
工具返回不代表已 `READY`；它没有 `status` 子命令，也不会长时间轮询。
使用显式中国区 profile 的 boto3 `bedrock-agentcore-control.get_agent_runtime`
查询返回的 Runtime ID，确认 `status=READY` 后再发布 Web。不要把再次执行
`runtime_release.py --apply` 当成等待命令：现有 Runtime 非 READY 时会拒绝更新。

```bash
RUNTIME_ARN='<Runtime 发布输出的 runtime_arn>'
```

### 3. Edge 与 Web 发布

```bash
"$PY" deploy/china_infra.py edge \
  --profile "$PROFILE" --expected-account "$ACCOUNT_ID" --region "$REGION"
```

审阅后 apply。Edge 先检查默认 VPC、公网子网路由、归属标签和现有访问规则，
再创建 ALB、目标组及安全组。只允许 ALB 公网 9000 → Web 80；
现有额外 ingress、listener 或非默认转发规则会导致失败，不会自动删除后继续。

```bash
"$PY" deploy/china_infra.py web \
  --profile "$PROFILE" --expected-account "$ACCOUNT_ID" --region "$REGION" \
  --image "$WEB_IMAGE" --runtime-arn "$RUNTIME_ARN" \
  --email-domains example.com
```

审阅后 apply。Web 阶段重新检查镜像存在、Runtime READY、角色权限及完整 Edge，
以 SSM **元数据**核对稳定会话密钥，无 Secret 读取或解密；仅缺失时生成一次，
通过 ECS task definition 的 `secrets.valueFrom` 引用注入。
任务为 Fargate Linux ARM64、非 root UID 10001、端口 80，并显式设置
`net.ipv4.ip_unprivileged_port_start=0`。任务的公网 IP 用于公网子网出站，
其安全组仍仅允许 ALB 访问。服务 desiredCount 为 1，启用 deployment circuit breaker
和 rollback，不配置 CloudWatch deployment alarms。

### 4. 状态与验收

```bash
"$PY" deploy/china_infra.py status \
  --profile "$PROFILE" --expected-account "$ACCOUNT_ID" --region "$REGION"
```

`status` 返回一次 ALB 状态、目标健康、ECS 数量及 deployment 状态，同时给出
`http://<ALB DNS>:9000`。需要时另行重跑，create/update 成功不等于 target 已 healthy。
IAM/ALB 的传播延迟可能要求稍后重试；工具不进行长阻塞等待。

## 回滚与发布记录

| 标识 | 含义与回滚方式 |
|---|---|
| Git commit / 分支 | 源码历史。切换 Git 不会改变已发布的镜像、ECS revision 或 Runtime 版本；未提交工作区构建不能仅靠 commit 还原 |
| ECR tag | 发布标签，仓库不可变；已有标签不能覆盖。每次新构建使用新标签 |
| ECR digest | 已推送镜像内容的固定引用。回滚复用已验证的旧 digest，不重新构建并假设产物相同 |
| ECS task definition revision | Web 配置与镜像快照。Web 更新输出 `previous_task_definition`，应与旧 Web digest 一起留存 |
| AgentCore Runtime version | 云端 Runtime 创建/更新产生的版本，与 Git commit、ECS revision 无编号对应关系 |

Web 回滚：用旧 Web `pinned_image` 重跑 `china_infra.py web` 的 dry-run，审阅后 apply。
这会发布引用旧镜像的 task definition，并沿用稳定 SSM 会话密钥。Circuit breaker 提供
服务部署失败时的回退机制，但不能代替业务验证。

Runtime 回滚：使用旧后端 `pinned_image` 重跑 `runtime_release.py`，先 dry-run 再 apply。
这是一次新的 Runtime 更新，可能产生新的 Runtime version，**不是将版本号倒退**。
工具没有按历史版本号切换 endpoint 的参数。它采用当前生成的环境和 Secret 引用，
不会自动恢复旧版本的所有环境设置或旧凭证；回退前须核对旧镜像与当前配置的兼容性。
回退后重新确认 Runtime READY、实际调用入口和核心接口行为。

保留已验证镜像，记录后端/Web digest、Runtime ARN 与前后版本、ECS 前后 task definition ARN、
部署配置、源码 commit 及未提交变更状态。账号相关输出和验收证据只写入忽略的
`.trellis/tasks/10-23-china-application-release/` 任务证据目录，不放入公开文档。
`main` 中的海外部署是历史参考，不是无需验证即可使用的中国区云资源回滚按钮。

## 旧 shell 脚本：保留的历史入口

以下脚本用于原有流程参考和回归测试，**不是本次实际应用发布路径**。
尤其不能通过设置 token、修改 gate 或运行旧 `rollback.sh` 替代上面的 SDK 发布流程。

| 脚本 | 保留的作用 |
|---|---|
| `deploy/settings.sh` | 无 AWS 调用的区域、端口和分区配置 |
| `deploy/config.sh` | 公共配置、账号和网络发现 |
| `deploy/provision.sh` | S3、ECR、ECS、日志、ECS/Web 角色；保留中国区 Runtime 调用权限，不预设海外模型权限；Runtime execution role 由专用工具配置 |
| `deploy/edge.sh` | 安全组、目标组、公网 ALB、HTTP 监听器 |
| `backend/scripts/deploy.sh` | 在目标部署区构建/推送 Runtime 镜像，不决定模型方案 |
| `deploy/runtime.sh` | 受模型发布 gate 保护的旧发布入口；当前改用 `runtime_release.py` |
| `deploy/service.sh` | 构建 Web 并绑定 ALB；需要已可用的 Runtime，不能代替模型落地 |
| `deploy/status.sh` | 显示 ALB 地址及资源状态，不再给出 task 公网 IP 作为客户入口 |
| `deploy/start.sh` / `stop.sh` | 调整 Web 副本数，保留 ALB |
| `deploy/rollback.sh` | 历史 Web revision 回退入口；Runtime 路径仍受旧 gate 阻止，不用于本次回滚 |
| `deploy/teardown.sh` | 显式确认后拆除本阶段基础资源；不删除尚未由本阶段管理的 Runtime/执行角色；本轮没有运行 |

旧 shell 的区域配置优先级仍是 `AWS_REGION` → `AWS_DEFAULT_REGION` → 宁夏默认值，
并保留北京、CIDR 和子网覆盖设置。这些设置不覆盖 Python CLI 的显式参数和固定网络契约。

## 本地验证

```bash
.venv/bin/python -m pytest \
  backend/tests/test_china_infra.py \
  backend/tests/test_push_image.py \
  backend/tests/test_runtime_release.py \
  backend/tests/test_runtime_role.py \
  backend/tests/test_provider.py -q
```

这些测试使用 mock、botocore 参数模型或 Stubber，不访问实际账号、不创建资源。
覆盖账号与 ARN 隔离、归属校验、dry-run、SDK client 生命周期、Secret 脱敏和部署参数。
旧 `backend/tests/test_china_deployment.py` 另用临时 `aws` 模拟器验证 shell gate 和历史契约；
它不是当前 SDK 发布成功的替代证据。

## 后续验收

1. 确认 ECS deployment 完成、ALB target healthy，并留存实际发布版本和 digest。
2. 验证 Runtime 模型与 Code Interpreter 集成；传输 smoke 成功不能替代完整审计质量结果。
3. 用客户办公网络检查公网 ALB 9000、登录、SSE 心跳和长连接恢复。
4. 以虚构数据验证生成、历史、批注、修订、版本采用和 QTI；如质量检查失败，应记录并诊断，
   不放宽审计或校验器。音频单独验证，不阻塞核心功能。
5. 保留本次云端旧镜像及版本证据，不迁移客户历史数据。

## 海外 Kimi 本地验证

默认模型是 `moonshotai.kimi-k2.5`，通过 Mantle Chat Completions `/v1` 调用。
中国区部署的可选列表只包含 Kimi K2.5、Kimi K3。
GPT-5.6 的 luna、sol、terra 的 Mantle Responses `/openai/v1` 适配保留，
但因用户实机调用报告 400，暂不在中国区允许列表中开放；具体错误原因尚待定位。
默认选中项仍为 K2.5；“默认选中”与“允许选择的模型列表”是两个独立配置。
`IELTS_ALLOWED_MODEL_IDS` 可显式配置已实现适配的模型；不会自动暴露整个 Bedrock 目录。
K2.5 输出上限为 16384 tokens，不发送 GPT 专用的 `reasoning.effort`。
K3 的配置 ID 为 `us.moonshotai.kimi-k3`，使用 `bedrock-runtime` 的
Chat Completions `/openai/v1`，上限 32768 tokens，显式传递 `reasoning_effort`。
生成使用 medium、审核使用 high。注册域名仍为 `example.com`。

本地凭证通过已有的海外账号 profile 使用，无需把凭证写入项目：

```bash
export IELTS_MODEL_ID=moonshotai.kimi-k2.5
export IELTS_MODEL_REGION=us-east-1
export IELTS_MODEL_AUTH=profile
export IELTS_MODEL_PROFILE='<本机的海外账号 profile 名称>'
.venv-backend/bin/python backend/scripts/smoke_kimi.py
```

该命令会产生少量海外模型费用，只使用虚构数据，最多三次模型调用、每次最多 1024
输出 tokens；检查 JSON 和工具调用往返。它不会创建中国区资源，也不验证雅思生成质量。
验证 K3 时将 `IELTS_MODEL_ID` 改为 `us.moonshotai.kimi-k3` 即可。
`IELTS_MODEL_REGION` 默认独立为 `us-east-1`，不继承中国区 `AWS_REGION`。
SDK 从显式海外 profile 的可更新凭证为请求签发 token；生产 Runtime 不能依赖开发机 profile。
已有 `bearer` 模式仍支持通过进程环境接收 `AWS_BEARER_TOKEN_BEDROCK`，禁止将 token
写入源码或明文部署模板。中国区应用拒绝使用 `mantle` 模式的隐式本地角色链访问海外模型。

## 生产模型配置与轮换

生产接入使用独立的海外 Bedrock 专用凭证和中国区 Secrets Manager 引用。
配置、权限及轮换操作见 [中国区模型凭证说明](CHINA_MODEL_CREDENTIALS.md)。
`deploy/model_credentials.py` 与 `deploy/runtime_role.py` 默认只读预览，`--apply` 才写入资源。
它们不会发布应用；应用由本页的 `runtime_release.py` 和 `china_infra.py` 发布，
旧 shell gate 继续保留。模型凭证轮换与镜像回滚是独立操作：应用在新建模型实例时读取
`AWSCURRENT`，已有实例不会因 Secret 更新自动更换内存中的 token。

## 当前模型验收限制

Kimi 的单次合成测试不能代表稳定质量或时延。材料与出题、盲审、修订应分别验收。
`hard_limit_seconds` 当前是启动阶段前的预算检查，不会取消已启动的出题阶段；
该阶段最多执行既有的两轮修订，因此实际请求可能超过 15 分钟。不要把它当作强制截止时间。

本轮实测续跑约 22 分钟，收到 86 次心跳并正常结束。材料审核 PASS，但题目两轮修订后
仍存在答案分歧，最终 `incomplete`、交付 0 套、无系统故障，保留可续跑检查点。
尚未完成合格题目、QTI 和管理员登录后全流程验收；不能将本次部署成功等同于模型质量达标。


## 音频与运行权限修复（2026-10-24）

中国区 Runtime 角色需要 `polly:SynthesizeSpeech` 和 `polly:DescribeVoices`，以
`aws:RequestedRegion` 限定部署区域。Web 角色不直接调用 Polly；不要把音频权限加到 Web 或容器启动角色。

宁夏实测 `neural/en-GB` 音色列表为空；标准引擎提供 Emma、Brian、Amy。
中国区部署显式设置 `IELTS_POLLY_PRESET=china-standard`：Emma 旁白，Amy/Brian 对话。
未设置此配置时仍使用原有 `british-neural`。不存在运行失败后自动换引擎的逻辑；
音频 manifest 和缓存键包含实际引擎及音色，不能把旧引擎的片段混入新录音。

权限检查覆盖 Runtime 的 S3、模型密钥、Code Interpreter、Polly，Web 的 S3/Runtime 调用，
容器启动的 ECR、SSM 和日志。日志 IAM 模拟返回不一致结果，已用相同执行角色的临时 Fargate 任务
实际验证日志创建和写入，任务正常退出；未为此扩大权限。海外模型使用独立凭证，不能通过扩大中国区角色权限修复模型访问限制。


### 浏览器音频读取

本次现场测试中，S3 预签名 MP3 GET 返回 HTTP 401 `UnauthorizedAccess`。
中国区 Web 将 `presign_audio` 返回的地址改为本站 `/api/material-audio` 地址，
由 Web 任务角色通过 S3 SDK 读取音频并流式返回；前端接口结构不变。
此路由受已有登录会话校验保护，只允许材料／版本音频目录中的 `turn_NNN.mp3`，
不接受任意外部 URL、其他桶、用户数据文件或目录穿越。支持单段 Range 请求供浏览器定位播放。
Web 已有 `s3:GetObject` 权限，因此不增加 IAM 权限，也不开放匿名桶读取。
