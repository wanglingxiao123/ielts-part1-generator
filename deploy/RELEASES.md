# 发布记录与回退依据

本页规定客户环境的版本记录格式。每次发布或回退后更新受控交付记录，
部署命令以 [中国区部署指南](../docs/CHINA_DEPLOYMENT.md) 为准。
[Global 历史记录](archive/GLOBAL_RELEASES.md) 仅供追溯，不是中国区当前生产版本。

## 环境发布记录

以下是每次交付需填写的字段，不预填为所有客户共用的生产版本。

| 字段 | 记录内容 |
|---|---|
| 时间与环境 | 时间、客户环境、账号、区域、操作者 |
| 源码 | 分支、commit、构建时是否有未提交改动及对应差异归档 |
| Runtime | ARN、发布前后版本、后端 ECR digest、环境配置和 Secret ARN 引用 |
| Web | ECS cluster/service、发布前后 task definition ARN、Web ECR digest |
| 入口与配置 | ALB 地址、注册域名、允许模型、音频预设 |
| 验证 | Runtime READY、ECS rollout、ALB target、健康检查、实际业务验收结果 |
| 回退 | 旧镜像 digest、旧配置、数据兼容性及回退后的验证结果 |

未提交工作区构建的镜像不能仅用事后 commit 代表构建来源。Git 分支或 commit 不会自动反映云端状态。
旧镜像回退会产生新的 Runtime 更新或 ECS task definition，不应按编号推断内容。

## 分层发布规则

`backend/` 修改通常发布 Runtime；`web/`、`frontend/`、`qti_export/` 修改通常发布 Web。
`skills/`、`prompt_config/`、`audio_storage/` 被两份 Dockerfile 引用，修改时检查并更新两层。
共享 API/数据契约变化还须核对调用方兼容性；提示词管理页面的配置发布不需要重建镜像。

发布前完成需要的镜像构建并留存旧 digest，检查正在执行的长任务。
Web 进程替换可能中断当前执行，应根据已保存检查点恢复；健康检查通过不等于业务验收通过。
