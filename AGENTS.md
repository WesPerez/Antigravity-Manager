# Antigravity Manager Fork Rules

Read this file and [FORK_DEPLOYMENT.md](FORK_DEPLOYMENT.md) before changing,
verifying, or publishing this fork. These server rules take precedence over
upstream examples that describe local dependency installation or builds.

## Upstream and customization boundary

- Upstream is `lbjlaq/Antigravity-Manager`. Keep `main` as an unmodified upstream
  mirror. Production uses `MINE` and `ghcr.io/wesperez/antigravity-manager:mine`.
- Keep the current upstream release followed by exactly two customization
  commits: deployment/remote-build policy, then web reverse-proxy subpath support.
- Keep backend behavior aligned with upstream. Do not mix unrelated application
  changes into either customization or leave temporary debugging commits on `MINE`.
- An upstream update must preserve both responsibilities. Inspect the final diff
  and commit list; do not infer the remaining customizations from merge titles.
- Rewriting an already published branch requires explicit user authorization and
  an exact `--force-with-lease`; preserve a recovery ref before replacing history.

## Remote builds only — no server-side exceptions

- **禁止在本机服务器下载或安装项目依赖、编译、构建、打包或构建镜像。**
  This covers every checkout, worktree, temporary directory, local container, and
  self-hosted CI runner on this server. Existing caches and “just one validation”
  do not make local builds permissible.
- Do not run package installation/module-download commands such as `npm ci`,
  `npm install`, `pnpm install`, `yarn install`, `pip install`, `cargo fetch`, or
  `go mod download`, or install build toolchains on the server.
- Do not run `npm run build`, `npx` tooling, Cargo/Go compilation or tests,
  `docker build`, `docker buildx build`, `docker compose build`,
  `docker compose up --build`, or local scripts that perform these operations.
  Type checks and tests that install dependencies or compile sources also run in CI.
- Push reviewed source to GitHub. Dependency installation, compilation, tests,
  frontend assets, images, and release packages belong only on GitHub-hosted
  Actions runners. Never upload a locally built `dist/`, binary, or image as a release.
- Local work is limited to source edits/review, Git operations, checks that need
  no dependency installation or compilation (for example `git diff --check`),
  published-image inspection, and authorized runtime/browser acceptance using
  already available tools. Missing test tools are a reason to use CI, not install them.

## Publish and deployment

- Push `MINE` to trigger `.github/workflows/mine-container.yml`. CI builds the
  complete image and publishes the revision tag plus the production `mine` tag.
- The existing Watchtower watches `antigravity-manager`, pulls the published
  image, and replaces the container. Do not manually build, pull/recreate the
  production container, or bypass Watchtower for an ordinary release.
- First installation or explicitly authorized recovery still uses a published
  image; it never permits a local dependency download or build. Preserve the
  data mount, credentials, loopback binding, and both existing Docker networks.
- Check the exact CI revision, deployed image label, backend health, and web
  login/navigation. A successful backend health check alone does not verify the UI.


## Upstream architecture and maintenance guidance

The following upstream engineering guidance applies. All install/build/test commands
mentioned below run on GitHub-hosted CI, subject to the server restrictions above.

# Project Maintenance Guidelines

- **Architecture**: This project is a gateway that aggregates four AI protocols — OpenAI Responses, OpenAI Chat Completions, Anthropic Claude, and Google Gemini — and outputs Antigravity-style Gemini protocol format.
- **Pipeline First**: Keep the pipeline strictly protocol-agnostic. The four protocols are Gemini adapters. Adapters should focus on parameter normalization, payload transformation, and difference adaptation. Downstream pipeline stages should uniformly clean, normalize, and backfill protocol features: thinking blocks, thinking effort, body text, tool schemas, etc.
- **Fix Strategy**: You should always attempt protocol-agnostic fixes in pipeline stages first. You should treat adapter modifications as a last resort.
- **Code Quality**: You should avoid hardcoding, dead code, and unsupported changes. You should prefer root-cause fixes. You should make changes robust and future-proof. You should prefer generalized patterns and wildcards that solve a class of problems over one-off patches.
- **Formatting & CI Discipline**:
  - **Mandatory Rust Formatting**: Always run `cargo fmt` (under `src-tauri/`) before committing or submitting a PR. Never push unformatted Rust code that causes the `Check Rust formatting` CI gate (`cargo fmt -- --check`) to fail.
  - **Local Pre-flight Checks**: Verify that `cargo check` and frontend build (`npm run build`) pass cleanly before opening or updating a PR.
- **Git & Release**:
  - PR merge commits should include contributor attribution.
  - Release notes should thank contributors and credit the specific ideas/fixes they contributed.

Maintained by @jeikl