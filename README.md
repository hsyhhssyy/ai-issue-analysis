# ai-issue-analysis

一个通用的 GitHub composite action，用来在 Issue 打开或被评论时调用 AI 模型做分析，并把分析过程和最终结论持续回写到同一条评论里。

支持两种 AI 驱动方式：

- **LiteLLM 路径**（推荐新接入用）：通过 JSON 配置接入 DeepSeek、OpenAI 兼容接口、GitHub Models 等 100+ 模型提供方。
- **Copilot CLI 路径**（兼容回退）：继续使用 `@github/copilot` CLI，适用于已有 Copilot token 的用户。

实战效果展示：

- [Bot 自动分析回复 ISSUE: 加载时间过长导致拜访好友失败](https://github.com/MaaEnd/MaaEnd/issues/1361#issuecomment-4071450863)
- [Bot 响应 @ 进行分析回复 ISSUE: 换班时会把训练室干员换下](https://github.com/MaaAssistantArknights/MaaAssistantArknights/issues/15963#issuecomment-4067281056)

## 快速接入

### 方式一：LiteLLM（DeepSeek / OpenAI 兼容 / 100+ 模型）

1. 准备 API Key。以 DeepSeek 为例，前往 [DeepSeek Platform](https://platform.deepseek.com/api_keys) 创建 API Key。

2. 在你的 GitHub 仓库里配置两个变量（Settings → Secrets and variables → Actions）：

    **Secrets 标签页** — 存放密钥：

     - Name: `DEEPSEEK_API_KEY`
     - Secret: 上一步中生成的 API Key

    **Variables 标签页** — 存放模型配置（不含密钥）：

     - Name: `LLM_CONFIG`
     - Value: 下面的 JSON（密钥部分用 `${DEEPSEEK_API_KEY}` 占位）：
       ```json
       {
         "provider": "deepseek",
         "model": "deepseek-chat",
         "api_key": "${DEEPSEEK_API_KEY}",
         "include_reasoning_content": true,
         "reasoning_effort": "xhigh",
         "max_output_tokens": 32000
       }
       ```

3. 把下面两个文件拷贝到你的仓库里

  - 把 [`examples/ai-issue-analysis.yml`](examples/ai-issue-analysis.yml) 保存为你仓库里的 `.github/workflows/ai-issue-analysis.yml`
  - 把 [`.claude/skills/generic-issue-log-analysis/SKILL.md`](.claude/skills/generic-issue-log-analysis/SKILL.md) 保存为你仓库里的 `.claude/skills/generic-issue-log-analysis/SKILL.md`

4. 在 workflow 里显式把仓库 Variable `LLM_CONFIG` 传给 action：

    ```yaml
    steps:
      - name: Analyze issue with AI
        uses: hsyhhssyy/ai-issue-analysis@main
        env:
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        with:
          llm-config-json: ${{ vars.LLM_CONFIG }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          bot-name: "@github-actions"
    ```

    > 想换模型、调参数？去 GitHub Settings 改 `LLM_CONFIG` Variable 即可，workflow 文件本身只需保留这条显式传参。
    >
    > 如果 `llm-config-json` 里用了 `${VAR_NAME}` 占位符，需要在调用 action 的 step 上通过 `env:` 显式把对应 Secret 传进来。
    >
    > `provider` 可设为 `deepseek`（走 LiteLLM 官方 DeepSeek provider）、`openai-compatible`（走通用 OpenAI 兼容端点，需要同时传 `base_url`）、`openai`、`github` 等。完整列表见 [LiteLLM Providers](https://docs.litellm.ai/docs/providers)。
    >
    > 仓库内的 [`.github/workflows/ai-issue-analysis.yml`](.github/workflows/ai-issue-analysis.yml) 是这个 action 仓库自身的自测 workflow，外部项目请复制 [examples/ai-issue-analysis.yml](examples/ai-issue-analysis.yml)。

5. 新提个 issue 测试下能否正常运行了，或者在以前的 issue 里 `@github-actions`。

### 方式二：Copilot CLI（兼容回退）

1. 请确保你有 Copilot Pro
2. 前往 [GitHub PAT](https://github.com/settings/personal-access-tokens) 新增一个 token  

    - Expiration (过期时间): 设为一年以内（太长反而会报错）
    - Add Premissions (添加权限): 勾上所有 Copilot 相关的
    - 点最下面绿色的 Generate，得到一个 token，复制下来保存好

3. 在你的 GitHub 仓库 - Settings - secrets - actions - new repository secret

     - Name: `COPILOT_GITHUB_TOKEN`
     - Secret: 上一步中生成的那个

4. 把 `examples/ai-issue-analysis.yml` 保存为你仓库里的 `.github/workflows/ai-issue-analysis.yml`，再把 `.claude/skills/generic-issue-log-analysis/SKILL.md` 保存到对应路径。

5. 新提个 issue 测试下能否正常运行了，或者在以前的 issue 里 `@github-actions`

> [!TIP]
>
> 如果你的项目有固定的日志包命名、关键日志路径、附件目录、模块映射或上游依赖，建议在这个通用版基础上微调 `SKILL.md`，分析质量会更高。最佳实践参考：
> - [MaaEnd](https://github.com/MaaEnd/MaaEnd/blob/v2/.claude/skills/maaend-issue-log-analysis/SKILL.md)
> - [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev-v2/.claude/skills/maa-issue-log-analysis/SKILL.md)

## 输入说明

- `issue-number`: Issue 编号，通常可以不传：

    - `issues` / `issue_comment` 事件会自动读取 `github.event.issue.number`
    - `workflow_dispatch` 会自动读取输入名为 `issue_number` 的 dispatch 参数
    
    如果你的 workflow_dispatch 输入名不是 `issue_number`，或者你在其他事件里调用这个 action，就显式传 `issue-number`。

- `github-token`: 用于创建和更新 Issue 评论
- `copilot-github-token`: （方式二）Copilot CLI 使用的 Fine-grained token。仅在未传 `llm-config-json` 时才会用到。支持多 token 逐行填写，随机选用
- `llm-config-json`: （方式一）JSON 对象或数组，描述 LiteLLM 模型配置。支持字段：`provider`、`model`、`api_key`、`api_base` 或 `base_url`、`reasoning_effort`、`max_output_tokens`、`temperature`、`headers`、`litellm_params`、`include_reasoning_content`（布尔值）等。字符串值中 `${VAR_NAME}` 会自动展开为同名环境变量；如果用了这类占位符，请在 workflow 的 `env:` 里显式传入对应 Secret。传数组时每次运行随机选一个。`include_reasoning_content` 主要用于 DeepSeek thinking 模式，只有在模型/Provider 判定为 DeepSeek 时才会生效。
- `litellm-package`: （方式一）安装 LiteLLM 用的 Python 包名，默认 `litellm`
- `analysis-max-iterations`: （方式一）工具调用最大轮次，默认 `12`
- `bot-name`: 从 `issue_comment` 正文中剥离掉的 bot mention，比如 `@YourBot`
- `initial-comment-body`: 开始分析时先发出的评论正文
- `action-link-text`: 评论里展示的运行链接文字
- `details-summary`: 分析过程折叠块的标题
- `prompt-template`: 基础分析提示词模板
- `comment-prompt-template`: 有评论补充要求时追加的提示词模板
- `copilot-model`: （方式二）Copilot CLI 模型名，默认 `gpt-5.4`
- `copilot-reasoning-effort`: （方式二）Copilot CLI reasoning effort，默认 `xhigh`
- `stream-update-interval-seconds`: 流式更新评论的间隔秒数，默认 `30`
- `checkout-repository`: 是否在 action 内部自动执行 `actions/checkout`，默认 `true`
- `extra-comment-content`: 始终追加在每次评论最末尾的额外内容，默认为空

## 输出说明

- `issue-number`: 本次运行实际解析出的 Issue 编号
- `comment-id`: 创建并持续更新的评论 ID
- `comment-url`: 创建并持续更新的评论 URL
- `analysis-prompt`: 本次最终传给 Copilot 的 prompt
- `copilot-output`: 完整执行日志，包含 Copilot 启动前的参数打印、prompt 正文，以及 Copilot CLI 输出
- `final-conclusion`: Copilot 写入 `copilot-answer-file` 的最终结论
- `analysis-prompt`、`copilot-output` 和 `final-conclusion` 在过长时会为适配 GitHub Actions output 大小限制而被截断；完整内容优先从 artifacts 读取

## 上传产物

- `copilot-output-issue-<issue-number>-comment-<comment-id>`: 完整执行日志，包含启动前参数、prompt 正文和 Copilot CLI 输出
- `final-conclusion-issue-<issue-number>-comment-<comment-id>`: 最终结论文本

## Skill 配合

- 这个 action 只负责 GitHub Actions 编排、评论更新、AI 调用和 prompt 拼接，不内置项目领域知识
- 对需要分析 issue 附件、日志包、运行时配置、跨仓库代码路径的项目，建议配套提供项目自己的 issue 分析 skill
- 一个可行的 skill 一般至少会覆盖这些步骤：读取 issue 正文和评论、定位并下载日志附件、先建立时间线再筛证据、最后回溯到代码和文档做归因
- 如果没有这层 skill，action 仍然能运行，但对日志包、截图、跨模块调用链这类问题，分析质量通常会明显下降
- 最佳实践参考，MaaEnd: `https://github.com/MaaEnd/MaaEnd/blob/v2/.claude/skills/maaend-issue-log-analysis/SKILL.md`
- 最佳实践参考，MaaAssistantArknights: `https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev-v2/.claude/skills/maa-issue-log-analysis/SKILL.md`

## 模板变量：

- `{{issue_number}}`
- `{{copilot_answer_file}}`
- `{{comment_body}}`
- `{{repository}}`
- `{{event_name}}`

## 行为说明：

- action 内部会自动 `checkout` 调用方仓库
- 如果调用方已经自己 checkout，或者前置步骤会生成工作区文件，可以把 `checkout-repository` 设为 `false`

**当 workflow 传入 `llm-config-json` 时（方式一 / LiteLLM 路径）：**

- action 会在自有 venv 里安装 LiteLLM，不污染系统 Python
- 新运行器会拉取当前 issue 与评论，提供仓库读取、代码搜索、目录浏览、附件下载、压缩包解压等工具给模型调用
- 配置中 `${VAR_NAME}` 会自动展开为同名环境变量，可将密钥存 Secret、模型配置存 Variable，workflow 一次配好不再改
- 分析完成后将最终结论写入答案文件，并流式更新评论
- 分析日志会包含 LiteLLM 调用参数和完整 prompt

**未传 `llm-config-json` 时（方式二 / Copilot CLI 回退）：**

- 会自动安装 `@github/copilot`
- `copilot-github-token` 兼容单个 token，也兼容多个 token 按行填写；传多个时每次运行会随机选一个

**两种路径共用行为：**

- 会先创建一条评论，然后持续更新这条评论
- 会导出 `comment-id`、`comment-url`、`analysis-prompt`、`copilot-output`、`final-conclusion` 等 action outputs
- `copilot-output` 会包含 AI 启动前的参数打印和 prompt 正文
- 会上传原始输出和最终结论两个 artifacts
- 最终评论会包含最终结论、完整分析过程折叠块，以及当前 Actions 运行链接

