# IELTS Listening Part 1 材料自动生成系统

勾选场景，自动产出符合命制规范的雅思听力 Part 1 材料：对话脚本 + 信息点标注 + 英式口音语音，
并进入可对接的审核流转。

系统只产出**材料**，不产出题目与答案——命题是后续人工环节。因此材料的唯一质量意义在于
「能否支撑后续命题」，这也是题型适配性成为核心验收维度的原因。

## 架构

```
浏览器 ──session cookie──▶ Web 层 (FastAPI, ECS Fargate :80)
                                │  boto3 invoke_agent_runtime
                                │  SigV4，凭证来自 ECS 任务角色
                                ▼
                        AgentCore Runtime（无公网入口）
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
        Bedrock GPT-5.6      Polly            S3（材料 + 音频 + 状态）
```

浏览器只与 Web 层自己的 `/api/*` 通信，鉴权与 AWS 无关。Web 层用任务角色的临时凭证代签调用
Runtime——**没有任何长期密钥，前端拿不到 AWS 凭证**。

## 目录

| 目录 | 职责 |
|---|---|
| `skills/ielts-listening-skills/` | 生成与评价的 skill 契约、三份 JSON Schema、确定性校验脚本 |
| `backend/` | AgentCore Runtime：Strands Agent + 确定性 Loop 编排；候选注册表以 S3 共享 |
| `audio_storage/` | Polly 逐 turn 合成、manifest、S3 状态流转 |
| `web/` | FastAPI Web 层：静态服务 + 自建登录 + SigV4 代理 |
| `frontend/` | React + TypeScript + Vite 审阅界面 |
| `deploy/` | 部署与启停脚本 |
| `config/scenarios.yaml` | 场景清单（6 大类 16 个场景） |
| `material/Part1_选材命制规范.md` | 命制规范（基于 20 套真题分析归纳） |

## 生成流程

确定性编排，不由模型决定是否继续循环：

```
生成 → 确定性校验 → [失败重生成，最多 2 次]
  → 评价（盲读，独立重建信息图谱）→ 程序化盲测对照
  → 修改（同步修订 script 与 blueprint）→ 确定性校验
  → 复评 → 取分高版本输出
```

两处设计值得留意：

- **评价环节盲读**：评价方不接触生成方的信息点标注，必须自己从脚本重建。两份独立图谱程序化
  对照后，评价方找不回的点即为真实缺陷——这比人工审核更早发现问题。该属性由四道防线保证
  （类型隔离、CI grep 门禁、运行期提示词扫描、复评无记忆）。
- **修改后必须复评**：否则产物携带的评分来自修改前版本，修改环节等于无验收。

## 本地开发

```bash
# 契约与校验（Python 3.9+）
python3 skills/ielts-listening-skills/shared/tests/run_tests.py
python3 audio_storage/tests/run_tests.py

# 后端与 Web 层（Python 3.12）
python3.12 -m venv .venv-backend && .venv-backend/bin/pip install -e 'backend[dev]'
.venv-backend/bin/python -m pytest backend/tests -q
.venv-backend/bin/python web/tests/run_tests.py
bash backend/scripts/ci_gates.sh          # 7 项结构门禁

# 前端
cd frontend && npm ci && npm run verify   # codegen 校验 + 类型 + lint + 测试
```

前端类型由 schema 生成，不手写：`npm run codegen:check` 保证两者不漂移。

## 部署

需要 AWS 凭证（us-east-1 或 us-east-2——GPT-5.6 无跨区推理）。

```bash
bash deploy/provision.sh                  # ECR / ECS 集群 / 安全组 / IAM，幂等
bash backend/scripts/deploy.sh <ecr-uri>  # 推 Runtime 镜像
bash deploy/runtime.sh                    # 创建 AgentCore Runtime
ALLOWED_EMAIL_DOMAINS=example.com \
  bash deploy/service.sh                  # 推 Web 镜像 + 创建 ECS 服务
bash deploy/start.sh                      # 拉起并打印当前访问地址
bash deploy/stop.sh                       # 停止，常驻成本归零
```

`deploy/config.sh` 里所有账号、网络参数都在运行时从调用者凭证发现，可用环境变量覆盖。

### 需要知道的几个约束

- **`ALLOWED_EMAIL_DOMAINS`** 控制谁能注册，默认 `*`（不限制）。公网暴露前务必设成你的域名。
  改这个值只需更新 task definition 并重启，不用重建镜像；已注册账号不受影响。
- **Web 层任务的公网 IP 每次拉起都会变**，`deploy/start.sh` 会打印当前地址。
- **`SESSION_SECRET`** 由 `deploy/service.sh` 随机生成并存入 SSM SecureString。代码中没有
  兜底默认值——共享用户存储下缺此变量会拒绝启动，以防多实例签名不一致导致静默掉登录。
- **没有单次批量上限**。web 层为每套材料发一次独立的 AgentCore invoke（`web/fanout.py`），
  15 分钟同步硬限约束的是单套（实测 146–230s），不再约束整批。并发由
  `WEB_FANOUT_CONCURRENCY`（默认 6）控制；模型侧出 429 时下调它，不要靠重试，也不要恢复
  批量上限。耗时如实告知用户（提交前的预估），不拒绝请求。

## 已知限制

- **均匀度阈值未校准**：`frontend/public/config.json` 的 `CV_WARN` / `CV_FAIL` 是启发式取值
  （`CALIBRATED: false`），界面已标注「参考值·阈值待校准」。积累若干真实材料后需人工校准。
- **雅思真题样本未随仓库分发**（版权原因）。依赖它们的回归测试会 SKIP 而非失败。

## 声明

本项目产出原创练习材料，不复制真题内容，也不代表 IELTS / Cambridge / British Council 的
任何授权或背书。
