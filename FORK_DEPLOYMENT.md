# Fork 的部署职责与发布约定

上游为 [lbjlaq/Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager)。
当前生产候选基线是上游最新稳定版 `v4.8.0`（`a7cbc12b`）。
上游 `main` 原样同步到镜像分支，生产跟随稳定发行版。
`main` 保持上游原样；生产分支为 `MINE`。

## 只保留两条个性化提交

| 顺序 | 职责 | 范围 |
| --- | --- | --- |
| 1 | 部署与远程构建 | MINE 镜像工作流、AGENTS 约束和部署文档；禁止本机下载依赖及构建 |
| 2 | 网页子路径访问 | 反代前缀、前端路由/API/图片路径及 Nginx 配置示例 |

两条提交都直接接在选定的上游版本之后。后端 `src-tauri/` 保持与该上游版本一致。
后续升级先核对上游新版本，再迁移这两项职责；不要累积临时修复、重复提交或把所有
差异埋进合并提交说明。已经发布的历史需要整理时，先保存恢复引用，取得用户对该次
历史整理的授权，再使用绑定旧远端 SHA 的 `--force-with-lease`，禁止无条件强推。

## 禁止在本机下载依赖和构建

本机服务器只负责源码编辑/审阅、Git 提交推送、读取已发布镜像和运行服务。
**依赖下载与安装、编译、类型检查、需要构建的测试、前端打包、二进制和 Docker 镜像
构建，全部在 GitHub-hosted Actions runner 上完成。**

禁令同样覆盖临时目录、其他 worktree、本机 Docker 容器和安装在本服务器的 self-hosted
runner；不得以排错、验收、节约等待时间或“完成后会删除”为由在本机执行。
`npm ci/install`、`pnpm/yarn install`、`pip install`、`cargo fetch/build/test/check`、
`go mod download/build/test`、`docker build/buildx build`、Compose build 和 `up --build`
均不属于本机允许的发布步骤。不运行本机 `npx` 下载测试器，也不提交本机构建的 `dist/`。

上游保留的 Dockerfile、`build.ps1`、localdist Compose 和历史测试示例只可用于远程
构建环境；它们不能覆盖 [AGENTS.md](AGENTS.md) 的服务器约束。
已有工具可用于无需下载依赖或编译的只读检查及授权验收；所需工具缺失时改用 CI。

## 唯一常规发布链路

```text
编辑并审阅源码 → 提交并推送 MINE → GitHub Actions 安装依赖、构建/打包
→ GHCR 发布镜像 → 现有 Watchtower 拉取并替换容器 → 核对版本和运行状态
```

正常发布入口：

```bash
git push origin MINE
```

工作流是 [mine-container.yml](.github/workflows/mine-container.yml)，在 GitHub 的
`ubuntu-latest` runner 上使用 [docker/Dockerfile](docker/Dockerfile) 构建完整镜像。
镜像发布到 `ghcr.io/wesperez/antigravity-manager`：

- `mine-<完整 Git SHA>`：用于对应提交的追溯；实际镜像身份以 digest 为准。
- `mine`：Watchtower 跟踪的生产标签。
- `org.opencontainers.image.revision`：核验运行镜像所对应的提交。

构建失败应在源码或远程 CI 中修复并重新推送，不得切换到服务器本地构建。
普通发布由 Watchtower 完成，不手工重建容器；首次安装或用户明确授权的恢复，也只能
使用远程发布的镜像。项目打包产物如有需要，同样在远程 Actions 内生成。

## 本机现有部署合同

- 容器名：`antigravity-manager`；镜像：`ghcr.io/wesperez/antigravity-manager:mine`。
- Watchtower 标签：`com.centurylinklabs.watchtower.enable=true`。现有 watcher 每 120 秒
  检查一次；SUB2 的独立发布门禁不属于本项目的发布操作。
- 端口：`127.0.0.1:8045`。Nginx 对外提供 `/antimanager/`。
- 数据挂载：`/root/.antigravity_tools:/root/.antigravity_tools`。
- 保留原有 API Key/Web 密码及 Docker `bridge`、`sub2api-prod_default` 双网络，后者
  保留 `antigravity-manager` 别名。不得套用上游 host-network Compose 覆盖现有配置。

发布后只读核对 Actions 成功、运行镜像的 revision/digest、`/health`，并验证网页登录、
切页和深链接刷新。等待 Watchtower 时读取状态和日志，不另外安装依赖或启动本机构建。
