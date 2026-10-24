# 中国区 POC 的海外模型凭证

应用部署区与模型账号分开：中国区 Runtime 使用自身角色读取中国区 Secrets Manager，
通过其中的 Bedrock 专用 API key 调用海外已托管的 Kimi K2.5。
开发机的 Isengard 登录只用于部署，不复制到 Runtime。

## 配置与验证

下列命令默认只查询身份和资源元数据、输出计划。`--apply` 才会创建资源。
账号 ID 与本机 profile 名称由部署者提供，不写死在项目中。

```bash
.venv-backend/bin/python deploy/model_credentials.py \
  --china-profile '<中国区 profile>' \
  --overseas-profile '<海外 profile>' \
  --china-account '<中国区账号 ID>' \
  --overseas-account '<海外账号 ID>'
```

核对计划后使用相同参数加 `--apply`。脚本：

1. 在海外账号创建专用 IAM 用户 `ielts-part1-china-model`，不创建控制台密码或 AWS access key。
2. 仅授予指定海外区域的 Mantle 推理与 bearer 调用权限。
3. 创建 Bedrock service-specific credential，默认有效期 90 天，可通过 `--days` 缩短。
4. 用新凭证进行一次虚构 JSON 推理验证，验证失败则撤销本次尚未发布的凭证。
5. 将 token 在进程内直接写入中国区 `ielts-part1/overseas-model` secret。
   输出仅包含 secret ARN、凭证 ID、有效期等元数据。

这是有明确有效期的 POC 接入方式。到期前需要轮换；它不会随个人 Isengard 会话到期而失效。
此 key 的 IAM 权限限制区域及推理动作，应用允许列表进一步限制为 Kimi K2.5；
不要把应用允许列表当成 IAM 层的模型限制。

## Runtime 执行角色与环境

将上述命令输出的 secret ARN 用于：

```bash
.venv-backend/bin/python deploy/runtime_role.py \
  --profile '<中国区 profile>' \
  --expected-account '<中国区账号 ID>' \
  --region cn-northwest-1 \
  --secret-arn '<完整 secret ARN>'
```

核对输出后加 `--apply` 创建项目 Runtime 执行角色。该命令只管理角色和内联策略，
输出供发布使用的环境变量，不创建 Runtime、ALB 或 ECS 服务。

运行环境使用 `IELTS_MODEL_AUTH=secret`、`IELTS_MODEL_SECRET_ARN` 和
`IELTS_MODEL_SECRET_ACCOUNT_ID`。只有 secret ARN 会出现在部署配置中。
应用每次创建模型客户端时读取 AWSCURRENT，使后续请求能使用轮换后的凭证；
已开始的模型客户端继续持有旧凭证。读取失败会报错，不回退到其他账号凭证。

Runtime 角色只获得对应 secret 的读取权限，以及本项目镜像、材料桶、日志和审计沙箱权限。
它不获得海外模型账号的 IAM 权限。内置 Code Interpreter 的 ARN 账号段是 `aws`，
不能替换成客户的账号 ID。

## 轮换与失败恢复

使用 `model_credentials.py` 的相同账号参数，加 `--rotate --apply` 发布新 key。
脚本保留旧 key，便于正在执行的任务完成和人工回退；新 key 验证后、旧任务结束后，
由管理员明确停用并删除旧的 service-specific credential。
达到两把 key 的限制时，必须先核对并清理旧 key，不能继续盲目创建。

若 Secrets Manager 写入报错，不能直接认定写入没有生效，也不能立刻撤销新 key。
需要核对 secret version 元数据与 IAM 凭证 ID；脚本对未完成配置会停止，要求恢复处理。
不要用输出 token 的命令排错，也不要把 key 复制到聊天、仓库或 Runtime 明文环境变量中。

完成这两步不等于应用已上线。镜像构建、Runtime 正式发布、中国区到海外模型网络、
生成与盲审质量、Web/SSE、客户公网访问仍需逐项验收。

## Kimi K3 的独立接入权限

K2.5 继续使用 Mantle；K3 按 AWS 模型卡推荐使用 `bedrock-runtime` 的 Chat Completions，
入口 `https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1`，推理配置
`us.moonshotai.kimi-k3`。官方说明了历史 reasoning 内容在 Converse 下的多轮兼容限制，
因此本项目不使用 Converse 接入 K3。

```bash
.venv-backend/bin/python deploy/model_access.py \
  --overseas-profile '<海外 profile>' \
  --overseas-account '<海外账号 ID>'
```

先审阅 dry-run，再加 `--apply`。该工具只给本项目的专用用户增加 K3 内联策略，
授予指定 US inference profile 及其三个目标区域的 K3 模型调用权限，并限制 bearer 入口区域。
不创建或轮换密钥，不覆盖 Mantle 策略，不附加全权托管策略。

K3 推理强度显式设置，按步骤使用生成 medium、审核 high，避免使用模型默认 max。
可用性、工具往返测试与完整 IELTS 质量验收分别记录。
