# AI Issue Analysis — 端到端测试 (E2E)

本目录包含 `ai-issue-analysis` GitHub Action 的端到端自动化测试。

## 工作原理

脚本利用 workflow 自带的 `issues.opened` 触发器：

```
创建 Issue → workflow 自动启动 → 轮询等待完成 → 下载日志/Artifacts → 验证 AI 评论 → 清理
```

**不需要手动触发 workflow_dispatch**，创建 Issue 后一切自动进行。

## 前置条件

1. **安装 GitHub CLI**

   ```bash
   # Ubuntu/Debian
   curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
     sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
   sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
     https://cli.github.com/packages stable main" | \
     sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
   sudo apt update && sudo apt install gh -y
   ```

2. **登录 GitHub**

   ```bash
   gh auth login
   ```

3. **目标仓库已配置 workflow**

   目标仓库必须已部署 `.github/workflows/ai-issue-analysis.yml`（包含 `on: issues: types: [opened]` 触发器）。

## 使用

```bash
# 快速启动：自动推断仓库，使用默认模板 Issue
./scripts/test/test.sh

# 指定仓库
python scripts/test/test_e2e.py --repo my-org/my-repo

# 自定义 Issue 内容
python scripts/test/test_e2e.py \
  --repo my-org/my-repo \
  --title "测试: 数据库连接失败" \
  --body "连接 MySQL 时出现: ERROR 1045 (28000): Access denied for user"

# 从文件读取 Issue 内容
python scripts/test/test_e2e.py \
  --repo my-org/my-repo \
  --body-file .temp/test-issue.md

# 保留测试 Issue（不自动关闭）
python scripts/test/test_e2e.py --no-cleanup

# 指定 bot 用户名（用于精确找到 AI 回复的评论）
python scripts/test/test_e2e.py \
  --repo my-org/my-repo \
  --bot-user "github-actions[bot]"

# 详细输出模式
python scripts/test/test_e2e.py --verbose
```

## 完整选项

| 选项 | 说明 |
|------|------|
| `--repo owner/repo` | 目标 GitHub 仓库（默认从 git remote 推断） |
| `--title "..."` | 测试 Issue 标题 |
| `--body "..."` | 测试 Issue 正文 |
| `--body-file path` | 从文件读取 Issue 正文 |
| `--bot-user name` | GitHub 用户名，用于过滤 AI Bot 的评论 |
| `--no-cleanup` | 测试完成后不关闭 Issue |
| `--verbose, -v` | 显示详细轮询输出 |
| `--output-dir path` | 测试产出物目录（默认 `.cache/e2e-test-results`） |

## 测试流程

```
Phase 1: 创建测试 Issue（workflow 通过 issues.opened 自动触发）
    ↓
Phase 2: 等待并找到自动触发的 workflow run
    ↓
Phase 3: 轮询等待 Action 完成（最多 30 分钟）
    ↓
Phase 4: 下载 Action 日志和 Artifacts
    ↓
Phase 5: 验证 AI 的评论回复
    ↓
Cleanup: 关闭测试 Issue（可选）
```

## 测试产出物

所有产出物保存在 `.cache/e2e-test-results/run-{id}-*/` 目录下：

| 文件 | 说明 |
|------|------|
| `*-logs/*.log` | GitHub Actions Job logs |
| `*-artifacts/` | Action 上传的 Artifacts（分析 prompt、输出、结论） |
| `*-comment.md` | AI 在 Issue 上发布的评论全文 |
| `*-results.json` | 结构化测试结果摘要 |

## 集成到 CI

可以将测试作为 GitHub Actions workflow 运行：

```yaml
# .github/workflows/test-e2e.yml
name: Test AI Issue Analysis

on:
  push:
    branches: [main]
  schedule:
    - cron: '0 6 * * 1'  # 每周一早上跑一次

jobs:
  e2e-test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
      actions: read
    steps:
      - uses: actions/checkout@v4
      - name: Run E2E test
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python scripts/test/test_e2e.py \
            --repo ${{ github.repository }} \
            --no-cleanup
```
