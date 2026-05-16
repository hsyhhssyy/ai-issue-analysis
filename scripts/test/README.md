# AI Issue Analysis — 自动化测试

本目录包含 `ai-issue-analysis` GitHub Action 的自动化测试脚本。

## 测试方案概览

提供两套互补的测试方案：

| 方案 | 脚本 | 依赖 | 测试内容 |
|------|------|------|----------|
| **本地模拟测试** | `test_local.py` | Python 3.10+ | 环境校验、event 解析、LLM 配置解析、脚本编排 |
| **端到端测试 (E2E)** | `test_e2e.py` | `gh` CLI + 目标仓库 workflow 已部署 | 创建 Issue → 触发 workflow → 轮询 → 读取 AI 评论 |

---

## 本地模拟测试

无需真实 GitHub Actions 环境，在本地即可运行。它会：

1. 设置模拟的 GitHub Actions 环境变量
2. 构建 Mock Issue payload（`issues.opened` 事件）
3. 测试配置解析、环境校验等核心逻辑
4. （可选）实际调用 LiteLLM 进行分析

```bash
# 快速检查：测试环境变量、event 解析、配置解析（不调 LLM）
python scripts/test/test_local.py --dry-run

# 运行指定阶段
python scripts/test/test_local.py --stage llm-config
python scripts/test/test_local.py --stage event-parsing

# 完整运行（需要配置好 .github/repository-ai-tool/llm-config.json）
# 并且设置 LLM_API_KEY 环境变量
LLM_API_KEY='{"deepseek/deepseek-chat":"sk-xxx"}' \
  python scripts/test/test_local.py --no-dry-run
```

### 测试阶段

| 阶段 | 说明 |
|------|------|
| `prepare` | 检查环境变量、文件路径等基础设施 |
| `event-parsing` | 导入 `ActionRunner` 并测试 Issue 号解析和 Prompt 构建 |
| `llm-config` | 测试 LLM 配置 JSON 解析、模型名规范化、错误处理 |
| `mock-run` | 完整流程模拟（dry-run 模式跳过 LLM 调用） |

---

## 端到端测试 (E2E)

在真实 GitHub 仓库上执行完整的 Issue 分析流程。

### 前置条件

1. **安装 GitHub CLI**

   ```bash
   # macOS
   brew install gh

   # Ubuntu/Debian
   type -p curl >/dev/null || sudo apt install curl -y
   curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
   sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
   sudo apt update
   sudo apt install gh -y
   ```

2. **登录 GitHub CLI**

   ```bash
   gh auth login
   ```

3. **目标仓库已配置 workflow**

   目标仓库必须已部署 `examples/ai-issue-analysis.yml`（或自定义名称）并设置了 `LLM_API_KEY` Secret。

### 使用

```bash
# 基本用法：自动推断仓库（从 git remote），使用默认模板 Issue
python scripts/test/test_e2e.py

# 指定仓库和自定义 Issue
python scripts/test/test_e2e.py \
  --repo my-org/my-repo \
  --title "测试: 数据库连接失败" \
  --body "连接 MySQL 时出现: ERROR 1045 (28000): Access denied for user"

# 从文件读取 Issue 内容
python scripts/test/test_e2e.py \
  --repo my-org/my-repo \
  --body-file ./test-data/sample-issue.md \
  --no-cleanup

# 指定 bot 用户名（用于精确找到 AI 回复的评论）
python scripts/test/test_e2e.py \
  --repo my-org/my-repo \
  --bot-user "github-actions[bot]"
```

### E2E 测试流程

```
Phase 1: 创建测试 Issue
    ↓
Phase 2: 触发 workflow_dispatch
    ↓
Phase 3: 找到触发的 workflow run
    ↓
Phase 4: 轮询等待 Action 完成（最多 30 分钟）
    ↓
Phase 5: 下载 Action 日志和 Artifacts
    ↓
Phase 6: 验证 AI 的评论回复
    ↓
Cleanup: 关闭测试 Issue（可选）
```

### 测试产出物

所有产出物保存在 `.cache/e2e-test-results/run-{id}-*/` 目录下：

| 文件 | 说明 |
|------|------|
| `*-logs/*.log` | GitHub Actions Job logs |
| `*-artifacts/` | Action 上传的 Artifacts（分析 prompt、输出、结论） |
| `*-comment.md` | AI 在 Issue 上发布的评论全文 |
| `*-results.json` | 结构化测试结果摘要 |

---

## 快速启动

```bash
# 1. 本地快速检查（推荐开发时使用）
chmod +x scripts/test/test.sh
./scripts/test/test.sh

# 2. 只测配置解析
./scripts/test/test.sh --local --stage llm-config

# 3. 端到端测试（需要 gh CLI）
./scripts/test/test.sh --e2e --repo my-org/my-repo
```

## 集成到 CI

也可以将测试作为 GitHub Actions workflow 运行。示例 workflow：

```yaml
# .github/workflows/test-ai-issue-analysis.yml
name: Test AI Issue Analysis

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  local-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Run local simulation tests
        run: |
          python scripts/test/test_local.py --dry-run

  e2e-test:
    if: github.repository == 'owner/my-repo'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
      actions: read
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Run E2E test
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python scripts/test/test_e2e.py \
            --repo ${{ github.repository }} \
            --no-cleanup
```

---

## 注意事项

1. **E2E 测试需要 API Key**：目标仓库必须配置了 `LLM_API_KEY` Secret，否则 AI 分析会失败。
2. **E2E 测试消耗额度**：每次 E2E 测试会调用 LLM API，消耗对应模型的额度。
3. **测试 Issue 自动关闭**：默认测试完成后会关闭 Issue（加上 `--no-cleanup` 跳过）。
4. **本地模拟的局限**：`test_local.py` 不模拟 GitHub API，所以 `create_initial_comment()` 等网络调用会被跳过或报错。
