# Agent 部署执行指南（中国区宁夏）

本页规定部署执行顺序；具体命令以 [中国区部署指南](docs/CHINA_DEPLOYMENT.md) 为准。
现行入口为 `deploy/` 下的 Python 工具，旧 shell 脚本仅供历史参考。

## 1. 核对部署输入

确认首次部署或更新、目标 commit 与未提交改动、中国区 profile/预期账号、
`cn-northwest-1`、唯一发布标签和客户注册域名。首次部署还需海外模型账号身份、
模型 Secret 与执行角色配置。所有命令显式指定账号与区域。

网络要求为默认 VPC、至少两个 AZ 的公网子网；入口固定 ALB HTTP 9000、Web 80。
旧 shell 的 CIDR、VPC、子网和端口变量不覆盖 Python CLI。模型区域与应用区域分别配置。
不得输出密钥、Cookie、session token、SSM SecureString 或 ECR 登录密码。

## 2. 首次部署顺序

1. `model_credentials.py` 创建海外专用模型凭证并写入中国区 Secret；`runtime_role.py` 配置执行角色。
2. 启用 K3 时执行 `model_access.py` 准备海外 K3 权限。
3. `china_infra.py prepare` 准备 S3、ECR、ECS、日志和 Web 角色。
4. 从仓库根目录构建两层 Linux ARM64 镜像，用 `push_image.py` 推送唯一标签并保存 `pinned_image`。
5. `runtime_release.py` 发布后查询 Runtime 状态，确认 READY。
6. `china_infra.py edge`、`web` 发布入口与 Web，配置实际客户邮箱域名。
7. `china_infra.py status` 检查 ECS rollout 和 ALB target，验证 Web `/healthz`。
8. 按部署指南执行客户业务验收，记录实际结果及版本映射。

云端写入工具默认 dry-run，核对后用相同参数加 `--apply`。已有任务授权包含对应部署变更时，
无需逐条重复确认。`status` 只读；镜像构建实际执行；模型凭证创建包含一次付费模型验证。

## 3. 更新范围

| 修改范围 | 需要重建与发布 |
|---|---|
| `backend/`、Runtime 专用配置 | Runtime |
| `web/`、`frontend/`、`qti_export/` | Web |
| `skills/`、`prompt_config/`、`audio_storage/` | Runtime 和 Web |
| 部署脚本/IAM/运行配置 | 逐项检查资源与环境变化；不等于必须重建所有镜像 |
| 管理页面发布业务提示词 | 配置发布，不需重建镜像 |

共享契约变化须检查两层兼容性。两层更新时先完成两份镜像构建，再更新 Runtime 和 Web。
发布前确认长任务状态，Web 滚动更新可能中断当前执行；不要把页面刷新等同于服务器更新。

## 4. 验证、失败与回退

部署状态不等于业务通过。区分本地模拟检查、云端资源健康、模型调用与客户操作验收。
仅修改文档不需要运行完整业务测试或付费模型探针。

步骤失败时定位失败层，记录已发生的变更；不要自动删除资源或修改业务逻辑绕过错误。
回退按部署指南复用旧镜像 digest，核对旧镜像与当前环境、Secret 和数据格式的兼容性。
Runtime 回退会产生新的发布版本，不是把版本编号倒退；Web 回退不需要重建 ALB。

## 5. 交付记录

记录部署时间、目标环境、源码及未提交状态、实际发布层、两层镜像 digest、Runtime ARN/版本、
ECS task definition、入口地址、健康检查与业务验收结果、上一版回退依据。
按 [发布台账](deploy/RELEASES.md) 的格式写入客户指定的受控交付记录；通用文档不保存凭证。
