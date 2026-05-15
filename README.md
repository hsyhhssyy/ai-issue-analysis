# ai-issue-analysis

一个通用的 GitHub composite action，在 Issue 打开或被评论时调用 AI 自动分析，并将分析过程和最终结论持续回写到同一条评论里。

通过 LiteLLM 接入 DeepSeek、OpenAI 兼容接口、GitHub Models 等 100+ 模型提供方。只需在仓库中放一个配置文件即可使用。

实战效果展示：

- [Bot 自动分析回复 ISSUE: 加载时间过长导致拜访好友失败](https://github.com/MaaEnd/MaaEnd/issues/1361#issuecomment-4071450863)
- [Bot 响应 @ 进行分析回复 ISSUE: 换班时会把训练室干员换下](https://github.com/MaaAssistantArknights/MaaAssistantArknights/issues/15963#issuecomment-4067281056)

## 快速接入

1. **准备 API Key**。以 DeepSeek 为例，前往 [DeepSeek Platform](https://platform.deepseek.com/api_keys) 创建 API Key。

2. **配置 Secrets**。在仓库 Settings → Secrets and variables → Actions → Secrets 中添加：

    | Secret 名称 | 值 |
    |---|---|
    | `LLM_API_KEY` | JSON 格式，每个模型对应一个 key：`{"deepseek/deepseek-chat": "sk-xxx", "openai/gpt-4o": "sk-yyy"}` |

    也支持按 provider 简写：`{"deepseek": "sk-xxx", "openai": "sk-yyy"}`，系统会自动匹配。

3. **创建配置文件** `.github/repository-ai-tool/llm-config.json`：

    ```json
    {
      "models": [
        {
          "provider": "deepseek",
          "model": "deepseek-chat",
          "api_base": "https://api.deepseek.com/v1",
          "include_reasoning_content": true,
          "reasoning_effort": "high",
          "max_tokens": 16000,
          "temperature": 0.1
        },
        {
          "provider": "openai",
          "model": "gpt-4o",
          "reasoning_effort": "high",
          "max_tokens": 16000,
          "temperature": 0.1
        }
      ],
      "reasoning_model": "deepseek/deepseek-chat",
      "vision_model": "openai/gpt-4o"
    }
    ```

    配置文件只需描述模型参数，**不需要写 API Key**。运行时自动从 `LLM_API_KEY` Secret 中按模型名称匹配对应的 key。
    
    支持所有 LiteLLM 兼容的 provider，包括 `deepseek`、`openai`、`openrouter`、`openai-compatible` 等。OpenRouter 用法：`"provider": "openrouter"`，`"model": "deepseek/deepseek-chat"`。

4. **拷贝 workflow 文件**。将 [`examples/ai-issue-analysis.yml`](examples/ai-issue-analysis.yml) 保存为 `.github/workflows/ai-issue-analysis.yml`。

5. **配置 workflow**。在 workflow 中通过 `env` 传入 Secret：

    ```yaml
    steps:
      - uses: actions/checkout@v4
      - name: Analyze issue with AI
        uses: hsyhhssyy/ai-issue-analysis@v2
        env:
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          bot-name: "@github-actions"
    ```

> 💡 想换模型、调参数？直接修改仓库里的配置文件即可，无需改动 workflow。

