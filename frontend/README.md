# IELTS Part 1 前端

React、TypeScript 和 Vite 构建的出题与审阅界面，包含批次进度、材料/题目版本、音频、管理员和提示词管理。
产品操作见 [使用指南](../USER_GUIDE.md)，完整本地环境见 [项目 README](../README.md#8-本地开发与测试)。

## 本地运行

```bash
npm ci
npm run dev
```

离线界面调试使用 `npm run dev:mock`；它不代表真实模型、鉴权或云端业务通过。
需要连接本地 Web 时，按项目 README 启动本地服务并核对前端运行配置。

## 检查与构建

```bash
npm run verify
npm run build
```

`verify` 包括生成契约检查、TypeScript、Oxlint 和 Vitest。`build` 使用 `tsc -b` 后生成 `dist/`。
Schema、场景及 fixture 的生成文件不应手工修改，使用 `package.json` 中对应的生成命令。

生产前端由 `web/Dockerfile` 构建并作为 Web 镜像静态文件交付，部署见
[中国区部署指南](../docs/CHINA_DEPLOYMENT.md)。
