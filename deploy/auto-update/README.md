# AntiGravity 独立自动更新

本目录只管理本仓库 `MINE` 分支的 `antigravity-manager`。推送后由 GitHub-hosted CI
构建镜像、测试更新器、验证真实容器替换与失败回退，然后发布镜像证据与更新器包。
服务器约每两分钟检查一次，只部署远端 **MINE 当前提交** 对应的成功 CI 镜像 digest。
旧提交重新运行 CI 不会触发部署；有授权的 MINE 历史重建也不会因祖先关系改变而卡住。
如果维护者主动把远端 MINE 指回旧版本，这就代表重新部署该版本的意图。

这是一个 systemd timer 和一次性 Python 脚本，不是常驻 Watchtower 进程。
只安装本仓库即可使用，不依赖其它六个项目或 Schedule Console 的中央部署器。
`main` 继续保持上游镜像分支。脚本、配置模板和 service/timer 都在本仓库；真实凭据、
数据、域名和本机路径保留在服务器，迁机时随服务器配置和数据迁移。

## 安装到现有部署

宿主需要 Docker、Python 3.11+（含 sqlite3）、git、curl、systemd，以及已登录 GitHub
的 `/usr/bin/gh`；私有 GHCR 需要 Docker 登录。只拉取远程镜像，不在生产机安装应用依赖、
编译或打包。先按部署合同启动应用，再执行一次安装：

```sh
# 从现有容器捕获端口、挂载、全部网络及运行环境；不输出凭据，不覆盖已有文件。
python3 -B deploy/auto-update/configure.py --web-url https://example.com/antimanager/ \
  --resolve example.com:443:127.0.0.1
# 填入远端 MINE 当前完整 SHA 及其成功的 mine-container.yml CI 编号。
python3 -B deploy/auto-update/install.py --sha <SHA> --run-id <RUN_ID> --enable
antigravity-manager-auto-update poll
antigravity-manager-auto-update status
```

`configure.py` 会写 root:root 0600 的 `/etc/antigravity-manager/auto-update.json` 和
`runtime.env`。`config.example.json` 是单网、根路径的新机示例，不应直接覆盖现网。
现网双网络由捕获脚本保留，包括 `sub2api-prod_default` 的 `antigravity-manager` 别名；
如果新服务器没有 SUB2，就无需创建这条外部网络。网页探针必须填写实际对外入口。

迁机时恢复数据与这两个私有配置文件，按新机路径/网络/域名调整配置，拉取已成功 CI
的镜像并启动一次应用，再运行安装器。安装器从该次 CI 下载校验过的 `auto-update`
artifact，装到 `/usr/local/lib/antigravity-manager-auto-update/releases/<SHA>`。
更新器也会随成功验收的应用版本更新，保留一个前身；不会覆盖宿主私有配置。

首次从 Watchtower 接管时，先停止旧 Watchtower 调度以防它在 CI 发布期间抢先替换。
新容器固定 digest 并标记 `watchtower.enable=false`，从此只有本项目更新器管理它。
其它项目是否继续使用 Watchtower 是该服务器的独立选择。

## 更新与恢复

```sh
antigravity-manager-auto-update poll          # 检查，不部署
antigravity-manager-auto-update poll --apply  # 与 timer 相同的部署入口
journalctl -u antigravity-manager-auto-update.service
systemctl disable --now antigravity-manager-auto-update.timer
# 修复宿主环境后，解除失败 SHA 隔离并允许重试：
antigravity-manager-auto-update retry
```

每次部署先核对 CI 仓库、分支、完整 SHA、工作流和成功 job，再校验 artifact 摘要、
镜像 digest 和源码标签。下载完成后重新确认 MINE 未变化，才停旧容器。
旧容器停止后用 SQLite backup API 保存数据库及账号/设置 JSON 恢复组，断开旧网络，
启动新容器。永不让两个应用实例同时写同一数据目录。切换有短暂中断。

验收包括 Docker health、HTTP health、管理密码与未鉴权拒绝、已有账号加载、登录读路径、
网页子路径、JS 内容类型/版本和深链接。失败恢复原容器及原镜像，失败 SHA 隔离；
中断时依据持久事务继续验收或回退。保留一个停止的原容器（不会占用运行进程）和一组
恢复文件。启动时约 30 秒检查中断事务，此后每两分钟检查；各次检查结束即退出。

自动回滚只恢复代码/容器，**不倒写业务数据**。数据库不兼容升级仍需协调，可在
`deploy/auto-deploy.json` 写 `{"version":1,"automatic":false}` 暂停该次自动部署。
日常无需该文件。凭据恢复副本同样是 0600，随恢复组一并保护和淘汰。

本项目发布锁为 `/run/lock/antigravity-manager-deploy.lock`。如服务器启用独立镜像
保留任务，把同一锁加入对应项目配置；手动部署也必须协调此锁。保留当前镜像和上一版，
不要全局 prune。state 默认独占 `/var/lib/antigravity-manager-auto-update`，不能复用
其它部署器的事务目录。可选的 `github-ci-fallback` 只处理主 CI 额度故障；没有它也可
正常跟随成功的主 CI，失败时等待，不跳过验证。
