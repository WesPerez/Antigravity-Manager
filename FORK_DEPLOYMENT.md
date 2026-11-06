# Fork 的部署职责与发布约定

上游为 [lbjlaq/Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager)。
当前生产候选基线是上游最新稳定版 `v5.0.3`（`0144b0f2`）。
上游 `main` 原样同步到镜像分支，生产跟随稳定发行版。
`main` 保持上游原样；生产分支为 `MINE`。

## 按功能保留个性化职责

| 顺序 | 职责 | 范围 |
| --- | --- | --- |
| 1 | 部署与远程构建 | MINE 镜像工作流、仓库内独立更新器、精确 CI 回执、实际备份容量与恢复验收 |
| 2 | 网页子路径访问 | 反代前缀、前端路由/API/图片路径及 Nginx 配置示例 |
| 3 | 维护说明 | AGENTS、本文件及 Docker 部署说明；可重复更新流程与本机发布边界 |

后端 `src-tauri/` 保持与所选上游版本一致。普通部署职责改进追加审阅过的提交，
不为维持提交数而重写已发布历史。后续获得授权的上游历史整理中，核对上游版本，
将仍有价值的修改按功能追加在最新上游之后；上游已覆盖或实现更好的功能直接采用上游。
提交数量由实际职责决定，不固定为两条或三条，也不制造空提交。
已经发布的历史需要整理时，先保存恢复引用，取得用户对该次
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
→ GHCR 发布镜像与精确 CI 证据 → 本仓库独立更新器验收/部署 → 失败自动回退容器
```

正常发布入口：

```bash
git push origin MINE
```

工作流是 [mine-container.yml](.github/workflows/mine-container.yml)，在 GitHub 的
`ubuntu-latest` runner 上使用 [docker/Dockerfile](docker/Dockerfile) 构建完整镜像。
镜像发布到 `ghcr.io/wesperez/antigravity-manager`：

- `mine-<完整 Git SHA>`：用于对应提交的追溯；实际镜像身份以 digest 为准。
- `mine`：远程镜像验收成功后更新的便利标签，自动部署以 CI 记录的 digest 为准。
- `org.opencontainers.image.revision`：核验运行镜像所对应的提交。

构建失败应在源码或远程 CI 中修复并重新推送，不得切换到服务器本地构建。
普通发布由 `antigravity-manager-auto-update.timer` 完成，不手工重建容器；首次安装或
用户明确授权的恢复，也只能使用远程发布的镜像。更新器包同样在远程 Actions 内生成。
安装、迁机和恢复见 [独立更新器说明](deploy/auto-update/README.md)。

## 本机现有部署合同

- 容器名：`antigravity-manager`；镜像：`ghcr.io/wesperez/antigravity-manager@sha256:<已验收摘要>`。
- 新容器的 Watchtower 标签为 `com.centurylinklabs.watchtower.enable=false`；本项目
  timer 每两分钟检查 MINE。SUB2 的独立发布门禁不属于本项目的发布操作。
- [release-retention](https://github.com/WesPerez/server-scheduled-tasks/tree/main/tasks/release-retention)
  每 15 分钟按实际运行容器 image ID 保留在用镜像，发布及健康观察宽限均为 3 分钟。
  旧镜像和本项目停止的 previous 容器按 `/etc/server-scheduled-tasks/release-retention.json`
  统一回收；可变标签不构成额外保留理由。与更新器共用
  `/run/lock/antigravity-manager-deploy.lock`。数据/凭据恢复组独立保护，不执行全局 prune。
- 端口：`127.0.0.1:8045`。Nginx 对外提供 `/antimanager/`。
- 数据挂载：`/root/.antigravity_tools:/root/.antigravity_tools`。
- 保留原有 API Key/Web 密码及 Docker `bridge`、`sub2api-prod_default` 双网络，后者
  保留 `antigravity-manager` 别名。不得套用上游 host-network Compose 覆盖现有配置。

发布后核对 Actions 成功、运行镜像的 revision/digest、`/health`，并验证网页登录、
管理鉴权、账号读取、前端资产和深链接刷新。独立更新器保留上一容器与一致数据库恢复
副本，只回滚镜像，不倒写业务数据。等待更新时读取 timer 状态和日志，不本机构建。


## 可重复更新流程 / Repeatable upstream refresh

本流程与 WorkBuddy Hub 的职责重建方式一致；运行时和 CI 验证使用本项目自己的发布入口。
The workflow follows WorkBuddy Hub's topic reconstruction, using this project's own runtime and CI contracts.

1. **锁定来源。** 读取 AGENTS 和本文件，确认干净工作区及其他写入者；fetch fork 与上游，
   登记旧 MINE、运行镜像 revision/digest、merge-base、最新稳定 tag/SHA。`main` 仅镜像上游。
   Inventory source/runtime identities and the latest stable upstream before editing.
2. **保存源码恢复点。** 为旧 MINE 建带日期的 `backup/*` 本地分支，在固定上游 SHA 的隔离
   worktree 中用 `git diff --binary <old-base> <old-MINE>` 与 `git apply --3way` 重放差异。
   不 reset 活跃工作区，不复制生产账号或凭据到候选。源码恢复引用不是镜像备份。
   Preserve a source ref and replay the diff in isolation without production credentials.
3. **审查保留与退役。** 分别登记保留、采用上游和退役的功能及理由；检查冲突和自动合并文件。
   后端保持所选上游原样；网页检查新路由、管理请求和资源路径；发布保留精确证据、锁、
   单写入者切换、账号保有量、数据恢复组、实际容量与子路径验收。
   Review all shared paths and prefer upstream where it supersedes local behavior.
4. **按职责提交。** 将最终差异按部署、网页、说明等实际职责暂存，修复折叠回所属职责；
   提交正文中英双语，保存旧提交到新职责的映射。没有关联 Issue/PR 时如实写明用户授权的
   维护任务，不编造编号。归并前后核对 tree 一致；不按历史修复次数或固定条数拆分。
   Commit coherent final topics with bilingual bodies and truthful provenance.
5. **最终 SHA 验证。** 本机只做无需依赖和编译的静态检查。GitHub-hosted CI 执行更新器回归、
   完整前后端构建、空账号实例的管理鉴权/网页资源/双网络/SQLite 恢复/失败回退验收。
   必须核对最终 SHA 的证据；候选、其他分支或旧 SHA 的回执不能授权本次部署。
   Hosted CI owns compilation and runtime smoke; verify the final commit independently.
6. **精确发布历史。** 发布前再次确认远端 MINE 等于最初登记的 SHA；若改变先纳入新提交。
   `main` 快进到上游；用户已授权重组时，对 MINE 使用
   `--force-with-lease=refs/heads/MINE:<old-sha>`，禁止无条件 force。push 后执行
   `gci build mine-container.yml --repo WesPerez/Antigravity-Manager --ref MINE`，等待原 request ID。
   `gci status` 的 health 用于检查堵点；受管 `ci-*` 仓库只由控制器操作。
   Publish with the exact source lease and reuse the automatic CI request.
7. **原生更新器闭环。** 若已有失败事务，先核对实际容器、数据、健康和账号，用已安装的
   `antigravity-manager-auto-update retry` 完成恢复；不能只删事务或清错误。安装最终 SHA 的
   已验证 updater 后，由既有 timer / `poll --apply` 持有原发布锁切换。核对运行 revision、
   digest、deployed.json、管理账号、网页登录/导航/深链和真实模型请求。下游如 Sub2 单独
   验证其账号状态、模型映射、协议结束事件和实际调度日志；Manager 直连成功不代表下游成功。
   Recover through the native transaction, deploy the verified digest, then test the actual downstream path.
8. **留存验收。** 脱敏记录最终 SHA、CI request/run、digest、网页及模型验收；迁移失败账号
   保留原错误证据，不假定额度可读就能生成。使用完的临时资源按归属收口，Docker 镜像仅保
   实际运行版本，使用统一回收器及三分钟观察宽限，不另建镜像备份或清理器。
   Keep auditable acceptance records and follow the host's current-image-only retention contract.

### 2026-11-07：v5.0.3 职责整合 / Topic consolidation

- 上游基线：`0144b0f2a81592dc985e0b70d1bac4cc12aba9c8`（v5.0.3）；旧 MINE：
  `3a73ea90ead7b3f002ef3e23055ed4e9c774b106`；旧基线：`a7cbc12b8529eeed861082dabc60d678caaf72d6`。
  Previous runtime revision was `88b0df57e5f13063ae5974af9726b42bf76f7030` (v4.8.0).
- 保留部署/更新器、网页子路径、维护说明三类职责；9 条旧提交归并为功能提交。
  Preserve deployment, web subpaths and maintenance documentation as coherent topics.
- 采用上游完整后端及新 UI：Gemini 4 Argon、模型准入/映射、配额归因、统一流式生命周期、
  GCP/企业账号、Mini View 和更新设置；不复制旧后端覆盖这些实现。
  Adopt upstream backend and UI capabilities, including Argon and stream lifecycle fixes.
- 两处文本冲突为 `src/App.tsx` 新增 Mini View/更新通知 imports 与原有 basename，
  `src/utils/request.ts` URL 构造；保留双方功能。其余共享 UI 文件也逐项审查。
  Both sides of the router and request-URL conflicts are retained.

| 旧提交 / Old source | 新职责 / Retained topic |
| --- | --- |
| `d0a8418b` | 远程发布 + 维护说明 / Remote publishing and documentation |
| `7138b152` | 网页子路径 + 使用说明 / Web subpaths and usage documentation |
| `2d36411f`, `88b0df57` | 独立更新器与验收 / Native updater and acceptance |
| `2fbd3bdc`, `84bdf8d8`, `ad731a0f` | CI 路由证据与远端烟测 / Routed CI evidence and hosted smoke |
| `8a5f1a93`, `3a73ea90` | 实际备份容量、取消固定空间门槛 / Measured backup capacity |

本条记录描述源码整合，运行版本以已验证的 deployed.json 与镜像标签为准。
This entry records source consolidation; deployed.json and image identity establish actual deployment.
