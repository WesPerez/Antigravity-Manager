# 🐋 Antigravity Manager 原生 Docker 部署手冊

本目錄包含 Antigravity Manager 的原生 Headless Docker 部署方案。該方案支持完整的 Web 管理界面、API 反代以及數據持久化，無需複雜的 VNC 或桌面環境。

## 本 Fork 只允許遠端構建

**禁止在本機伺服器下載或安裝專案依賴、編譯、打包、構建前端或 Docker 鏡像。**
臨時目錄、worktree、本機容器和驗收測試都不能繞過此規則；不可先在本機產生 `dist/`
再提交或交給 Docker 使用。詳細約束見 [AGENTS.md](../AGENTS.md) 和
[Fork 部署約定](../FORK_DEPLOYMENT.md)。

日常發布只有一條路徑：

```bash
git push origin MINE
```

GitHub-hosted Actions runner 負責下載依賴及完整構建，將鏡像推送至
`ghcr.io/wesperez/antigravity-manager:mine`；現有 Watchtower 自動拉取並替換容器。
CI 失敗時修正程式或遠端工作流，不得改成本機構建。後端、前端和測試都遵循同一規則。

## 🚀 快速開始

### 1. 首次安裝使用已發布鏡像
以下僅是首次安裝範例；現有生產服務由 Watchtower 更新，不應重複建立容器。
只使用遠端已發布鏡像，無需在伺服器安裝依賴或獲取構建工具：

> [!IMPORTANT]
> **安全警告**：從 v4.0.3 開始，Docker 版支持 **管理密碼與 API Key 分離**：
> *   **API Key**：通過 `-e API_KEY=xxx` 設置，用於所有 AI 協議的 API 調用鑒權。
> *   **Web 管理密碼**：通過 `-e WEB_PASSWORD=xxx` 設置，僅用於 Web UI 登錄。
> *   **默認行為**：若未設置 `WEB_PASSWORD`，系統會自動回退使用 `API_KEY` 作為登錄密碼。若兩者皆未設置，則生成隨機 Key。
> *   **查看方式**：執行 `docker logs antigravity-manager` 尋找 `Current API Key` 或 `Web UI Password`，或執行 `grep -E '"api_key"|"admin_password"' ~/.antigravity_tools/gui_config.json` 查看。

```bash
# 啟動容器 (請替换 your-secret-key 為強密鑰)
docker run -d \
  --name antigravity-manager \
  --restart unless-stopped \
  --label com.centurylinklabs.watchtower.enable=true \
  -p 127.0.0.1:8045:8045 \
  -e API_KEY=your-api-key \
  -e WEB_PASSWORD=your-login-password \
  -e ABV_MAX_BODY_SIZE=104857600 \
  -v ~/.antigravity_tools:/root/.antigravity_tools \
  ghcr.io/wesperez/antigravity-manager:mine
```

#### 🔐 鑒權邏輯 (Security Scenarios)
*   **場景 A：僅設置了 `API_KEY`**
    - **Web 登錄**：使用 `API_KEY` 即可進入後台。
    - **API 調用**：使用 `API_KEY` 進行 AI 請求鑒權。
*   **場景 B：同時設置了 `API_KEY` 和 `WEB_PASSWORD` (推薦)**
    - **Web 登錄**：**必須**使用 `WEB_PASSWORD`。此時輸入 API Key 將被拒絕，確保管理權限與調用權限隔離。
    - **API 調用**：繼續使用 `API_KEY`。您可以放心地將 API Key 分發給團隊成員，而保留密碼僅供管理員使用。

#### 🆙 舊版本升級指引
如果您是從舊版本升級，默認沒有設置 `WEB_PASSWORD`。您可以通過以下方式添加：
1.  **Web UI (推薦)**：使用原有的 `API_KEY` 登錄，在 **API 反代** 設置頁面中設置新的管理密碼。
2.  **環境變量**：停止舊容器，啟動新容器時增加 `-e WEB_PASSWORD=您的新密碼`。

> [!TIP]
> **優先級邏輯 (Priority)**:
> - **環境變量** (`ABV_WEB_PASSWORD` / `WEB_PASSWORD`) 具有最高優先級。如果設置了環境變量，程序將始終使用它，忽略配置文件中的值。
> - **配置文件** (`gui_config.json`) 用於持久化存儲。當您通過 Web UI 修改密碼並保存時，新密碼會寫入此文件（JSON 字段名為 `admin_password`）。
> - **回退機制**: 如果上述兩者皆未設置，則回退使用 `API_KEY`；若連 `API_KEY` 也未設置，則隨機生成。

### 2. 使用 Docker Compose
上游保留的 Compose 範例帶有 `build:`，不是本伺服器的發布入口，不可直接執行。
現有容器的資料、密碼、回環埠與雙網路配置以 [部署合同](../FORK_DEPLOYMENT.md) 為準。
首次安裝若使用 Compose，必須只引用已發布的遠端 `image:`，不能包含本機 `build:`。

### 3. 構建參數只在遠端 CI 使用

MINE 工作流在 GitHub-hosted runner 上執行 `docker/Dockerfile`，並設定
`USE_MIRROR=false`。需要修改構建參數時提交工作流修改，再交由 GitHub Actions 執行。
`build.ps1`、backend-only/localdist Dockerfile 和本機 Compose 構建範例都不允許在本機
伺服器執行；它們不能替代遠端完整構建。

## ⚙️ 環境變量配置

| 變量名 | 默認值 | 說明 |
| :--- | :--- | :--- |
| `PORT` | `8045` | 容器內服務監聽端口 |
| `ABV_API_KEY` | - | **[重要]** 代理 API 密鑰。客戶端（如 Claude Code）訪問時需提供的 Key |
| `ABV_WEB_PASSWORD` | - | **[安全]** Web 管理後台登錄密碼。若不設置則回退使用 API Key |
| `ABV_MAX_BODY_SIZE` | `104857600` | **[性能]** 最大請求體限制 (Byte)。默認 100MB，用於解決大圖傳輸 413 錯誤 |
| `LOG_LEVEL` | `info` | 日志等級 (debug, info, warn, error) |
| `ABV_DIST_PATH` | `/app/dist` | 前端靜態資源託管路徑 (Dockerfile 已內置) |
| `ABV_PUBLIC_URL` | - | 用於遠程 OAuth 回調的公網 URL (可選) |

## 📂 數據持久化
請務必將宿主機目錄掛載至容器內的 `/root/.antigravity_tools`，否則賬號和配置在容器重啟後會丟失。

## 🌐 訪問位址
*   **管理界面**: [http://localhost:8045](http://localhost:8045)
*   **API Base**: [http://localhost:8045/v1](http://localhost:8045/v1)

## 📦 GHCR 發布與驗收

鏡像只由 MINE 的 GitHub Actions 發布，同時保留 `mine-<完整提交號>` 與生產 `mine`
標籤。以鏡像 digest 和 `org.opencontainers.image.revision` 核對實際版本。
Watchtower 完成更新後檢查 `/health`、網頁登入、頁面切換與深層路徑重新整理。
伺服器不執行鏡像構建、手動打包或將本機產物推送至 registry。
