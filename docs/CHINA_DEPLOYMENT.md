# 中国区（宁夏）部署、更新与回退指南

本项目使用 Python/boto3 工具发布中国区应用，完整流程限定 `cn-northwest-1`。
原 Global 架构与中国区差异见 [服务对比](China_Region_架构设计.md)。
应用入口为公网 ALB HTTP 9000，模型为海外 Bedrock Kimi K2.5 / K3，
生产凭证通过中国区 Secrets Manager 引用。当前工具不部署 CloudFront，也不迁移历史数据。

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

当前入口使用 HTTP，登录和会话流量没有 HTTPS 传输加密。客户需要 HTTPS、自有域名或入口访问限制时，
须单独落实对应配置；交付前检查客户网络能否访问端口和维持 SSE 长连接。

## 当前发布配置

从仓库根目录运行工具，使用 Python 3.12、Docker 和可用的中国区 profile。
本地环境安装见 [README 本地开发](../README.md#8-本地开发与测试)，以下统一使用 `.venv-backend`。本节示例只包含占位符；
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
PY=.venv-backend/bin/python
PROFILE='<中国区 profile>'
ACCOUNT_ID='<12位中国区账号>'
REGION=cn-northwest-1
RELEASE_TAG='<本次未使用过的发布标签>'
SECRET_ARN='<同账号宁夏模型 Secret 完整 ARN>'
EMAIL_DOMAINS='<客户注册邮箱域名，多个以逗号分隔>'
```

### 1. Prepare 与镜像构建、推送

先完成[生产模型凭证与 Runtime 执行角色准备](CHINA_MODEL_CREDENTIALS.md)，其中启用 K3 时还需执行 `model_access.py` 配置海外权限，然后检查并 apply：

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

设置 ARN 后执行只读查询（可重复运行，不触发发布）：

```bash
"$PY" - "$PROFILE" "$REGION" "$RUNTIME_ARN" <<'PYTHON'
import json
import sys
import boto3
profile, region, arn = sys.argv[1:]
client = boto3.Session(profile_name=profile, region_name=region).client("bedrock-agentcore-control")
try:
    result = client.get_agent_runtime(agentRuntimeId=arn.rsplit("/", 1)[-1])
    print(json.dumps({key: result.get(key) for key in
                      ("agentRuntimeArn", "agentRuntimeVersion", "status", "failureReason")}, ensure_ascii=False))
finally:
    client.close()
PYTHON
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
  --email-domains "$EMAIL_DOMAINS"
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

首次开放注册时，由指定客户负责人注册第一个账号，该账号自动成为管理员。核对管理员入口后再开放其他用户注册；
后续账号权限及忘记密码由管理员在用户管理中处理。

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
部署配置、源码 commit 及未提交变更状态。账号相关输出和验收证据写入客户指定的受控交付记录，格式见 [发布台账](../deploy/RELEASES.md)，不放入公开文档。
`main` 中的海外部署是历史参考，不是无需验证即可使用的中国区云资源回滚按钮。

## 更新已有环境

- **仅 Web 更新**：构建并推送 Web 镜像，设置新的 `WEB_IMAGE`，保留当前 `RUNTIME_ARN` 和客户域名，重跑第 3 节的 `web` 命令并检查 `status`。已有 Edge 无变更时不需要重建。
- **仅 Runtime 更新**：构建并推送后端镜像，设置新的 `BACKEND_IMAGE`，重跑第 2 节发布命令并查询 READY；不需要发布未改变的 Web。
- **共享目录更新**：`skills/`、`prompt_config/`、`audio_storage/` 同时进入两份镜像，需检查两层更新。先完成两份镜像构建，再发布。
- **凭证轮换**：按模型凭证指南处理，不等于镜像更新；已有模型实例可能继续持有旧凭证。

各命令复用前文变量和完整参数，先 dry-run 再 apply。更新前记录旧镜像与配置，确认正在执行的长任务；Web 进程替换可能中断当前执行。
旧 shell 的 gate、区域及端口覆盖设置仅为历史兼容，不用于当前 SDK 发布或回退。

## 本地验证

```bash
.venv-backend/bin/python -m pytest \
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

## 客户交付验收清单

1. 确认 ECS deployment 完成、ALB target healthy，并留存实际发布版本和 digest。
2. 验证 Runtime 模型与 Code Interpreter 集成；传输 smoke 成功不能替代完整审计质量结果。
3. 用客户办公网络检查公网 ALB 9000、登录、SSE 心跳和长连接恢复。
4. 以虚构数据验证生成、历史、批注、修订、版本采用和 QTI；如质量检查失败，应记录并诊断，
   不放宽审计或校验器。音频单独验证，不阻塞核心功能。
5. 保留本次云端旧镜像及版本证据，不迁移客户历史数据。

## 海外 Kimi 本地验证

默认模型是 `moonshotai.kimi-k2.5`，通过 Mantle Chat Completions `/v1` 调用。
中国区部署的可选列表只包含 Kimi K2.5、Kimi K3。
GPT/Mantle Responses 适配仅作为历史兼容保留，不在中国区交付模型列表中开放。
默认选中项仍为 K2.5；“默认选中”与“允许选择的模型列表”是两个独立配置。
`IELTS_ALLOWED_MODEL_IDS` 可显式配置已实现适配的模型；不会自动暴露整个 Bedrock 目录。
K2.5 输出上限为 16384 tokens，不发送 GPT 专用的 `reasoning.effort`。
K3 的配置 ID 为 `us.moonshotai.kimi-k3`，使用 `bedrock-runtime` 的
Chat Completions `/openai/v1`，上限 32768 tokens，显式传递 `reasoning_effort`。
生成使用 medium、审核使用 high。注册域名由 Web 发布参数配置。

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

## 执行预算与恢复

材料生成、题目盲审和修订耗时受模型响应与质量检查影响。阶段开始前检查剩余预算，
不会强制取消已启动的模型调用；时间预算不是完成时长保证。预算不足或调用失败时，
根据页面持久化状态继续剩余套件或重试，不能仅凭心跳认定质量通过。

## 音频与权限

Runtime 角色配置 `polly:SynthesizeSpeech`、`polly:DescribeVoices` 并限制部署区域。
Web 角色负责 S3 音频读取，不直接调用 Polly。
中国区发布设置 `IELTS_POLLY_PRESET=china-standard`：Emma 旁白、Amy/Brian 对话。
引擎和音色参与 manifest 与缓存键；没有失败后自动切换引擎的逻辑。

浏览器通过本站 `/api/material-audio` 鉴权代理读取音频，支持单段 Range。
Web 使用任务角色读取指定材料/版本目录下的 MP3，不开放匿名桶读取。
海外模型访问故障应检查独立海外凭证，不能通过扩大中国区角色权限修复。
