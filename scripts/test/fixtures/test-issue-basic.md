## 测试 Issue（纯文本，无截图）

我在运行项目时遇到了以下问题：

### 环境信息
- OS: Ubuntu 22.04
- Node.js: v18.15.0
- npm: 9.5.0

### 错误描述

执行 `npm start` 后，终端输出以下错误：

```
Error: Cannot find module 'express'
Require stack:
- /app/src/server.js
- /app/src/index.js
    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:933:15)
    at Function.Module._load (node:internal/modules/cjs/loader:778:27)
    at Module.require (node:internal/modules/cjs/loader:1005:19)
    at require (node:internal/modules/cjs/helpers:102:18)
    at Object.<anonymous> (/app/src/server.js:1:17)
```

项目结构：
```
/app/
  src/
    server.js
    index.js
  package.json
```

尝试过执行 `npm install` 重新安装依赖，但问题依旧。请问是什么原因？如何修复？
