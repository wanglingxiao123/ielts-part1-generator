# Agent 部署指南

本文件供负责部署本项目的编码 Agent 使用。权威脚本位于 `deploy/` 和
`backend/scripts/deploy.sh`；README 第 4 至第 6 节说明 IAM、资源和参数。本文件负责规定执行顺序、
安全边界和验收方式。

## 1. 开始前确认

部署前必须确认以下信息；缺少时一次性向用户询问，不要自行猜测：

| 项目 | 要求 |
|---|---|
| 部署模式 | `首次部署` 或 `更新部署` |
| 目标提交 | 用户指定的 commit；未指定时报告当前 `HEAD` 并请求确认 |
| AWS 区域 | `us-east-1` 或 `us-east-2` |
| 发布标签 | 本次唯一的 `RELEASE_TAG`，不得复用已有 ECR 标签 |
| 邮箱域名 | Web 部署固定使用 `ALLOWED_EMAIL_DOMAINS=example.com` |

先执行只读检查：

```bash
git status --short
git branch --show-current
git log -1 --oneline
aws sts get-caller-identity
docker info
```

同时确认：

- 当前 commit 与用户要求一致；
- 工作区没有未说明的源码改动；
- AWS 凭证有效，账号和区域正确；
- Docker daemon 可用并能构建 `linux/arm64`；
- `RELEASE_TAG` 在需要推送的 ECR repository 中尚不存在；
- 首次部署满足 README 第 4 节的 IAM、VPC 和子网条件。

只汇报 AWS 账号和资源标识，不得输出 access key、secret、session token、Cookie、SSM
SecureString 内容或 ECR 登录密码。

## 2. 执行边界

- 不修改业务代码、配置契约或部署脚本来“绕过”部署错误。
- 不提交代码、不创建 Git tag、不改写 Git 历史，除非用户明确要求。
- 不执行 `deploy/teardown.sh`，也不删除 S3、用户、批次、材料或音频。
- 不创建线上账号、批注、材料批次或其他测试数据。
- 默认不重复运行完整测试套件。先核对目标 commit 已有的 CI/交接门禁结果；缺少可信结果时停止并
  询问用户，不要自行启动耗时测试。
- Web 镜像构建自带 `tsc -b && vite build`；构建失败必须停止，不得跳过类型检查。
- 不复用 `dev`、`latest` 或任何已存在标签。ECR 标签必须保持不可变，旧镜像用于回退。
- 执行会改变 AWS 状态的命令前，先简短汇报部署模式、目标 commit、区域、标签和准备部署的层。

## 3. 判断部署范围

更新部署先比较当前线上 commit 与目标 commit。只按实际改动路径决定，不因“保持两层同标签”而
无条件重建未受影响层。

| 改动路径 | 部署层 |
|---|---|
| `backend/`、`skills/`、`config/` | Runtime |
| `web/`、`frontend/` | Web |
| `audio_storage/` | Runtime + Web |
| 仅 Markdown、截图或其他文档 | 不部署 |
| `deploy/` | 阅读改动后判断；脚本变化不自动等于镜像变化 |

如果改动跨层或无法证明只影响一层，则部署两层。两层部署必须先 Runtime、后 Web。

部署前记录当前状态，用于失败补偿：

```bash
bash deploy/status.sh
```

另行记录当前 Runtime 的 `containerUri`、Runtime version、ECS task definition revision 和 Web
镜像 URI。不要只凭 Runtime version 推断代码版本。

## 4. 首次部署

所有命令从仓库根目录执行。README 第 5 节是命令的权威说明。

```bash
export AWS_REGION=<us-east-1-or-us-east-2>
export RELEASE_TAG=<unique-release-tag>

aws sts get-caller-identity
bash deploy/provision.sh

aws ecr put-image-tag-mutability \
  --repository-name ielts-part1-backend \
  --image-tag-mutability IMMUTABLE
aws ecr put-image-tag-mutability \
  --repository-name ielts-part1-frontend \
  --image-tag-mutability IMMUTABLE

bash backend/scripts/deploy.sh \
  "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com/ielts-part1-backend" \
  "$RELEASE_TAG"
bash deploy/runtime.sh "$RELEASE_TAG"

bash deploy/edge.sh
ALLOWED_EMAIL_DOMAINS=example.com bash deploy/service.sh "$RELEASE_TAG"
bash deploy/start.sh
```

每一步成功后再进入下一步。Runtime 未达到 `READY` 时不得部署 Web；`edge.sh` 应在首次
`service.sh` 之前执行。

## 5. 更新部署

根据第 3 节确定层级，并为本次发布使用新标签。

### 仅 Runtime

```bash
export AWS_REGION=<us-east-1-or-us-east-2>
export RELEASE_TAG=<unique-release-tag>

bash backend/scripts/deploy.sh \
  "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com/ielts-part1-backend" \
  "$RELEASE_TAG"
bash deploy/runtime.sh "$RELEASE_TAG"
```

### 仅 Web

```bash
export AWS_REGION=<us-east-1-or-us-east-2>
export RELEASE_TAG=<unique-release-tag>

ALLOWED_EMAIL_DOMAINS=example.com bash deploy/service.sh "$RELEASE_TAG"
```

### Runtime + Web

先完整部署并确认 Runtime `READY`，再部署 Web：

```bash
export AWS_REGION=<us-east-1-or-us-east-2>
export RELEASE_TAG=<unique-release-tag>

bash backend/scripts/deploy.sh \
  "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com/ielts-part1-backend" \
  "$RELEASE_TAG"
bash deploy/runtime.sh "$RELEASE_TAG"
ALLOWED_EMAIL_DOMAINS=example.com bash deploy/service.sh "$RELEASE_TAG"
```

已有 ECS 服务会保持原 `desiredCount`。如果服务原本停止，只有用户要求启动时才执行
`bash deploy/start.sh`。

## 6. 失败处理

- 任一步骤失败后立即停止，不要继续部署下一层。
- 不要为了通过构建而现场修改源码或测试。
- Runtime 部署失败时，Web 保持不动，收集失败状态和日志位置后汇报。
- 两层发布中 Runtime 已成功但 Web 失败时，系统可能处于不兼容状态。停止后根据部署前记录的
  Runtime 镜像执行补偿回退；先运行 `deploy/rollback.sh --dry-run`，不得猜测版本号或镜像标签。
- 回退只使用 `deploy/rollback.sh`。不要用 `service.sh` 重推旧标签，ECR 的不可变标签会拒绝该操作。
- 不执行清理、删桶或 `teardown` 作为故障处理。

示例：

```bash
bash deploy/rollback.sh --runtime-image <previous-runtime-tag> --dry-run
bash deploy/rollback.sh --runtime-image <previous-runtime-tag>
```

如果缺少可靠的旧镜像或 task definition 记录，停止并请求用户决定，不要扩大变更。

## 7. 部署后验证

至少完成以下检查：

1. `bash deploy/status.sh` 正常返回；
2. AgentCore Runtime 状态为 `READY`；
3. ECS 服务 rollout 为稳定状态，`desiredCount` 与部署前一致；
4. Web task definition 中 `ALLOWED_EMAIL_DOMAINS=example.com`；
5. Runtime 和 Web 实际镜像 URI 均指向本次预期标签；
6. CloudFront 状态为 `Deployed`；
7. `https://<CLOUDFRONT_DOMAIN>/healthz` 返回 200；
8. 没有遗留的两层错开、失败 deployment 或意外测试数据。

不得仅凭 `/healthz` 200 判断版本正确；必须同时核对 Runtime `containerUri`、ECS task definition
和镜像 URI。

部署成功后更新 `deploy/RELEASES.md`，记录日期、commit、镜像标签和 digest、Runtime version、
task definition、部署层和验证结果。只修改该台账，不提交或推送，除非用户明确要求。

## 8. 最终汇报

使用下面的简短格式：

```text
部署结果：成功 / 失败 / 已回退
目标 commit：
部署模式：首次部署 / 更新部署
实际部署层：Runtime / Web / Runtime + Web / 无需部署
AWS 区域：
发布标签：
Runtime：version、status、containerUri、digest
Web：task definition、rollout、image、digest
CloudFront：
健康检查：
邮箱域名：example.com
发布台账：已更新 / 未更新
未执行：完整测试、线上 E2E、测试数据创建
遗留问题：
```

失败时必须说明失败发生在哪一步、哪些层已经改变、是否完成补偿回退，以及当前线上仍指向什么版本。
