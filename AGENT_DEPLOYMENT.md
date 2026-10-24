# Agent 部署指南（中国区迁移阶段）

本分支的权威脚本位于 `deploy/` 和 `backend/scripts/deploy.sh`，配置与当前限制见
[中国区部署说明](docs/CHINA_DEPLOYMENT.md)。旧海外部署流程保留在 `main` 和历史版本中。

## 1. 当前发布边界

本阶段完成中国区基础设施适配，并接入海外 Bedrock 已托管 Kimi 的本地模型适配。生产 Runtime IAM 与海外凭证引用已有专用配置工具，正式 Runtime 发布仍待验收，
`runtime.sh`、模型探针和涉及 Runtime 的回退会主动停止；不得通过删除检查来绕过。
不把模型区域直接设为海外以自动开启跨境调用，不把中国区角色视为海外模型访问凭证。

不要沿用旧版 Runtime → Web 的从零部署命令作为本阶段的完成标准。
在模型接入未完成前，只能进行明确授权的基础设施准备；不能宣称生成服务已上线。

## 2. 实际操作前确认

| 项目 | 要求 |
|---|---|
| 任务范围 | 本地验证、基础资源准备或后续已完成模型适配的发布，必须区分 |
| 代码 | 报告分支、commit 和未提交改动；不切换或修改 main |
| 账号与区域 | 中国区账号；宁夏默认，北京可配置；不得使用海外凭证误操作 |
| 网络 | VPC、至少两个可用区子网、客户网络能访问所选 ALB 端口 |
| 公网入口 | ALB HTTP，默认 9000；非标准端口不代表备案豁免或 HTTPS |
| 发布标签 | 若构建镜像，使用唯一 RELEASE_TAG，保持旧镜像可回退 |
| 注册范围 | 使用客户确认的 ALLOWED_EMAIL_DOMAINS，不能设置为 * |
| 模型 | 海外 Bedrock Kimi K2.5；正式 Runtime 发布验收前保留发布停止点 |

只读检查可包含 Git 状态、目标账号身份、Docker 可用性和已存在资源。
不得输出 access key、secret、session token、Cookie、SSM SecureString 内容或 ECR 登录密码。

## 3. 配置约定

```bash
export AWS_REGION=cn-northwest-1
export ALB_PUBLIC_PORT=9000
export ALLOWED_EMAIL_DOMAINS=example.com
```

`AWS_REGION` 优先于 `AWS_DEFAULT_REGION`，最终统一；部署区不从 `IELTS_MODEL_REGION` 推导。
China ARN 使用 `aws-cn`，ECR registry 使用 `amazonaws.com.cn`。不能全局替换 IAM service principal。
ALB 对公网使用 ALB_PUBLIC_PORT，Web 对 ALB 仍使用内部 80，不公开容器端口。

## 4. 脚本和执行顺序

先完成账号预检和专用模型凭证配置。以下为基础设施脚本职责，不表示应用已经发布：

1. `provision.sh`：基础资源、日志和权限。
2. `edge.sh`：ALB、安全组、目标组、HTTP 监听器；既有不兼容配置应明确报错。
3. `backend/scripts/deploy.sh <china-ecr-uri> <unique-tag>`：按目标 ECR 区域构建/推送镜像。
4. **基础设施脚本不负责生产 Runtime 凭证和 IAM；需使用第 7 节工具**：不得继续假定 Runtime 可用。
5. 生产凭证与角色配置后，仍需完成密钥引用、镜像、Runtime → Web 的正式发布验收。

`service.sh` 仍要求可用 Runtime 和目标组。`start.sh` / `stop.sh` 调整 Web 副本数，
不会拆除 ALB。`status.sh` 输出 ALB 的默认域名与实际公网端口。

## 5. 验证与失败处理

本地验证命令：

```bash
.venv-backend/bin/python -m pytest backend/tests/test_china_deployment.py -q
```

这些测试在 PATH 注入模拟 aws，不访问真实账号。没有运行云端测试就不得报告云端通过。
不要因迁移而重复运行与改动无关的全部模型/音频测试，付费模型探针仅在当前任务已明确授权模型验证时运行。

后续实际发布中，每一步成功后再进入下一步。失败应停在失败层，记录已产生的变更；
不自动 teardown，不删除客户数据，不修改业务代码来绕过配置错误。

## 6. 回退与汇报

- 本轮不提交、推送、合并或创建 tag；main 保持原样。
- 后续 Web 回退使用目标账号的 task definition revision；不能使用 Global 资源标识。
- 涉及 Runtime 的回退在正式 Runtime 发布流程验收前仍停止；不得回放旧海外环境变量到中国区。
- ALB 地址属于负载均衡器，不属于镜像；回退 Web 不应删除或重建 ALB。
- `teardown.sh` 只有在单独明确授权时才运行，它会破坏当前访问入口。

汇报必须区分：代码修改、本地模拟验证、真实云端验证。列出当前分支、改动层、
测试结果，以及待确认的模型/音频/公网访问事项。真实部署后再增加账号、区域、
Runtime ARN、ECS revision、镜像 digest、ALB URL 和实际健康检查结果。

## 7. 生产凭证配置工具

见 [模型凭证说明](docs/CHINA_MODEL_CREDENTIALS.md)。`deploy/model_credentials.py`
只创建专用海外用户/服务凭证并写入中国区 secret；`deploy/runtime_role.py` 只管理
中国区 Runtime 执行角色并输出不含密钥的环境配置。先审阅默认只读计划，明确授权后使用
`--apply`。模型验证只使用虚构数据。实际账户与凭证 ID 记录在本地 Trellis，不写入通用源码。
