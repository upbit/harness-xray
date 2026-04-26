# Claude Dump 机制外置化备忘

## 1. 这次讨论的目标

目标不是修改 `src/` 里的既有逻辑，而是回答两个问题：

1. `src/services/api/dumpPrompts.ts` 在现有代码里是怎么工作的。
2. 如果 ant-only 逻辑在后续发布版里被裁剪，能否在 Node.js 进程外层复刻类似 dump 能力。

本次已经完成源码分析，并额外实现了一套不改 `src/` 的外层 PoC。

## 2. 对现有源码的结论

### 2.1 主链路接入点

主对话请求链路里，`dumpPrompts` 不是在网络层全局生效，而是作为 `fetchOverride` 注入进主 query 链路：

- `src/query.ts:588`
- `src/query.ts:688`

逻辑是：

- 当 `config.gates.isAnt` 为真时，创建 `createDumpPromptsFetch(...)`
- 然后通过 `options.fetchOverride` 传给模型请求层

### 2.2 最终如何进入 Anthropic SDK

`fetchOverride` 会继续透传到：

- `src/services/api/claude.ts:689`
- `src/services/api/claude.ts:821`
- `src/services/api/claude.ts:1783`
- `src/services/api/claude.ts:848`

随后 `getAnthropicClient(...)` 会把它装进 SDK client：

- `src/services/api/client.ts:88`
- `src/services/api/client.ts:139`
- `src/services/api/client.ts:358`

关键点是：

- `buildFetch(fetchOverride, source)` 内部使用 `fetchOverride ?? globalThis.fetch`
- 也就是只要能在外层包住 `globalThis.fetch`，理论上就能复刻类似能力

### 2.3 `dumpPrompts.ts` 自身在做什么

`src/services/api/dumpPrompts.ts` 的行为可以概括为：

1. 拦截 POST 请求 body。
2. 解析 JSON，请求体里提取：
   - 首次 `init`
   - 后续 `system_update`
   - 新增的 `user message`
3. 异步写入 JSONL 文件。
4. 对响应做 `response.clone()`。
5. 如果是 SSE 流，则把 `data:` 事件重新解析成 chunks。
6. 将最终响应内容再以 `response` 记录到同一个 JSONL 文件。

默认路径语义是：

- `~/.claude/dump-prompts/<session-or-agent-id>.jsonl`
- 若设置 `CLAUDE_CONFIG_DIR`，则改用对应目录

### 2.4 为什么它是 ant-only

核心 gating 在：

- `src/query/config.ts:39`
- `src/services/api/dumpPrompts.ts:49`
- `src/services/api/dumpPrompts.ts:100`
- `src/services/api/dumpPrompts.ts:174`

也就是说，现有实现并不是“外部默认可用的调试能力”，而是内部分支下才启用。
如果发布版经过 tree-shaking / feature 裁剪，这段能力很可能直接消失。

## 3. 对“能否外置实现”的结论

结论：可以，但有前提。

### 3.1 可行前提

外置方案成立，需要目标 Claude 程序满足下面条件：

1. 它运行在 Node.js 进程里。
2. 它的最终 HTTP 请求仍经由 `globalThis.fetch` 或等价 fetch 路径发出。
3. 它允许通过 `NODE_OPTIONS=--require ...` 之类方式预加载补丁。

如果这三个条件成立，就可以在不修改原项目源码的前提下，把 `dumpPrompts` 机制外置到进程 preload 层。

### 3.2 不成立的情况

以下场景外置 preload 方案大概率失效：

- 目标程序是 Bun 打包产物
- 目标程序是 Electron / 原生桌面程序，且不接受 `NODE_OPTIONS`
- 目标程序绕过了 `globalThis.fetch`
- 发布版主动清洗了 preload / require 注入能力

这些情况下，应优先考虑网络层方案：

- `ANTHROPIC_BASE_URL` 指向自建反向代理
- `HTTPS_PROXY` / `https_proxy` 指向 MITM 代理
- 更底层的透明网络观测

## 4. 本次已新增的外层实现

本次没有改 `src/`，只新增了根目录下的外置 PoC：

- `external-dump/anthropic-dump-preload.cjs`
- `external-dump/run-with-dump.sh`
- `external-dump/README.md`

### 4.1 `anthropic-dump-preload.cjs`

这是一个 Node preload 脚本，核心行为：

1. 启动时包裹 `globalThis.fetch`
2. 仅检查 POST 请求
3. 默认只关注看起来像 Anthropic messages API 的请求
4. 解析 JSON body
5. 写入接近原版 `dumpPrompts.ts` 的 JSONL 结构：
   - `init`
   - `system_update`
   - `message`
   - `response`
6. 对 SSE 响应进行完整读取并拆出 `data:` chunks

### 4.2 `run-with-dump.sh`

这是一个薄包装脚本，用于自动注入：

```sh
NODE_OPTIONS="--require=/abs/path/to/external-dump/anthropic-dump-preload.cjs"
```

调用方式示例：

```sh
./external-dump/run-with-dump.sh claude
```

### 4.3 README

`external-dump/README.md` 已写明：

- 使用方式
- 环境变量
- 适用范围
- 已知限制

## 5. 当前验证状态

### 5.1 已验证

已完成的验证：

- 通过源码确认主链路确实支持 `fetchOverride ?? globalThis.fetch`
- 已完成外层 preload 脚本编写
- 已执行语法检查：

```sh
node -c external-dump/anthropic-dump-preload.cjs
```

结果：通过

### 5.2 尚未验证

还没有做的是真实目标程序上的联调验证，尤其是以下问题仍待确认：

1. 你实际要 hook 的 Claude 程序是否真的是 Node 进程。
2. 它是否接受 `NODE_OPTIONS`。
3. 它的 SDK 请求是否最终走 `globalThis.fetch`。
4. 它是否会把请求 body 以字符串形式传给 fetch。
5. 流式响应是否能被 `response.clone()` + `body.getReader()` 稳定读取。

## 6. 下次继续时建议的验证步骤

下次如果继续，可以直接基于这份备忘做下面几项验证：

1. 确认目标 Claude 可执行程序的运行时类型。
2. 用 `NODE_OPTIONS=--require=...` 实际启动一次目标程序。
3. 发起一条最小对话请求。
4. 检查是否生成 `dump-prompts/*.jsonl`。
5. 验证 JSONL 里是否同时包含请求和流式响应 chunks。
6. 如果 preload 不生效，立即切换到代理层方案验证。

## 7. 这次工作的边界

- 没有修改 `src/` 既有实现
- 没有把 ant-only 逻辑强行改成 external 可用
- 只做了源码分析 + 外层可行性 PoC
- 真实效果仍取决于目标 Claude 程序的运行时与发布方式
