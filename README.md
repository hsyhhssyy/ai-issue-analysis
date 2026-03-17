# ai-issue-analysis

一个通用的 GitHub composite action，用来在 Issue 打开或被评论时调用 Copilot CLI 做分析，并把分析过程和最终结论持续回写到同一条评论里。

这个仓库现在只提供 `action.yml`。各项目自己的 workflow 仍然需要保留：

- `on:` 触发器
- 何时触发分析的 `if:` 条件
- 各项目自己的 secrets

最小示例：

```yaml
name: Issue AI Analysis

on:
  issues:
    types: [opened]
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      issue_number:
        description: Issue number to analyze
        required: true
        type: number

jobs:
  analyze:
    if: |
      (github.event_name == 'issues' && github.event.action == 'opened') ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'issue_comment' &&
       github.event.action == 'created' &&
       contains(github.event.comment.body, '@YourBot') &&
       github.event.comment.user.type != 'Bot')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write
    steps:
      - uses: Misteo/ai-issue-analysis@main
        with:
          github-token: ${{ secrets.PROJECT_BOT_TOKEN }}
          copilot-github-token: ${{ secrets.COPILOT_GITHUB_TOKEN }}
          bot-name: '@YourBot'
          prompt-template: |
            分析 GitHub Issue #{{issue_number}}。
            请结合仓库代码、现有 issue 信息和必要的日志判断问题原因。
            把最终结论写到 {{copilot_answer_file}}。
          comment-prompt-template: |
            补充要求：{{comment_body}}
```

`issue-number` input 通常可以不传：

- `issues` / `issue_comment` 事件会自动读取 `github.event.issue.number`
- `workflow_dispatch` 会自动读取输入名为 `issue_number` 的 dispatch 参数

如果你的 workflow_dispatch 输入名不是 `issue_number`，或者你在其他事件里调用这个 action，就显式传 `issue-number`。

主要 inputs：

- `github-token`: 用于创建和更新 Issue 评论
- `copilot-github-token`: Copilot CLI 使用的 Fine-grained token
- `bot-name`: 从 `issue_comment` 正文中剥离掉的 bot mention，比如 `@YourBot`
- `initial-comment-body`: 开始分析时先发出的评论正文
- `action-link-text`: 评论里展示的运行链接文字
- `details-summary`: 分析过程折叠块的标题
- `prompt-template`: 基础分析提示词模板
- `comment-prompt-template`: 有评论补充要求时追加的提示词模板
- `copilot-model`: 默认 `gpt-5.4`
- `copilot-reasoning-effort`: 默认 `xhigh`
- `stream-update-interval-seconds`: 流式更新评论的间隔秒数，默认 `30`
- `checkout-repository`: 是否在 action 内部自动执行 `actions/checkout`，默认 `true`

Skill 配合：

- 这个 action 只负责 GitHub Actions 编排、评论更新、Copilot CLI 调用和 prompt 拼接，不内置项目领域知识
- 对需要分析 issue 附件、日志包、运行时配置、跨仓库代码路径的项目，建议配套提供项目自己的 issue 分析 skill
- 一个可行的 skill 一般至少会覆盖这些步骤：读取 issue 正文和评论、定位并下载日志附件、先建立时间线再筛证据、最后回溯到代码和文档做归因
- 如果没有这层 skill，action 仍然能运行，但对日志包、截图、跨模块调用链这类问题，分析质量通常会明显下降
- 最佳实践参考，MaaEnd: `https://github.com/MaaEnd/MaaEnd/blob/ci/prompt/.claude/skills/maaend-issue-log-analysis/SKILL.md`
- 最佳实践参考，MaaAssistantArknights: `https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev-v2/.claude/skills/maa-issue-log-analysis/SKILL.md`

模板变量：

- `{{issue_number}}`
- `{{copilot_answer_file}}`
- `{{comment_body}}`
- `{{repository}}`
- `{{event_name}}`

行为说明：

- action 内部会自动 `checkout` 调用方仓库
- 如果调用方已经自己 checkout，或者前置步骤会生成工作区文件，可以把 `checkout-repository` 设为 `false`
- 会自动安装 `@github/copilot`
- 会先创建一条评论，然后持续更新这条评论
- 最终评论会包含最终结论、完整分析过程折叠块，以及当前 Actions 运行链接
