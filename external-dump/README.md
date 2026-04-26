# External Claude Dump Hook

外置实现已经复刻了原版最核心的两件事:

  1. 请求前落盘 request body
  2. 响应后落盘 JSON / SSE chunks

  但它还不是对 src 原始机制的 1:1 等价替代。最关键的差异在于“作用域、隔离性、生命周期、容错”。

  主要欠缺

  1. agent/session 隔离不如原版精确
     原版是按 agentIdOrSessionId 建文件和维护状态的，文件名和 dumpState 都天然和主会话/子 agent 绑定。
     参考: src/services/api/dumpPrompts.ts:59, src/services/api/dumpPrompts.ts:146, src/query.ts:588

  外置版默认只有一个 dumpId / stateKey，整个进程共享一套 messageCountSeen 状态。
  参考: external-dump/anthropic-dump-preload.cjs:20, external-dump/anthropic-dump-preload.cjs:28, external-dump/
  anthropic-dump-preload.cjs:86

  这会带来一个实际问题: 如果同一进程里并发多个 agent 或多个独立会话，外置版可能把不同请求流混在一起，导致
  messageCountSeen 错位，出现漏记或串写。原版不会有这个问题。

  2. 缺少原版的配套内存缓存和清理接口
     原版除了落盘，还有一套配套能力:

  - 最近 5 次 API request 的内存缓存
  - clearDumpState
  - clearAllDumpState

  参考: src/services/api/dumpPrompts.ts:13, src/services/api/dumpPrompts.ts:29, src/services/api/dumpPrompts.ts:40

  外置版目前只有“进程内常驻状态 + 文件追加”，没有这些调试配套，所以不能直接承担原版 /issue 一类的辅助用途。

  3. 请求筛选比原版更脆弱
     原版不靠 URL 猜测，它是通过主 query 链路显式注入 fetchOverride，因此只要进入那条链路，就一定被记录。
     参考: src/query.ts:688, src/services/api/client.ts:358

  外置版为了避免误抓，增加了 URL 启发式判断。
  参考: external-dump/anthropic-dump-preload.cjs:113

  这意味着:

  - 如果目标程序走了不常见 host/path，外置版可能漏抓
  - 如果某些非主对话请求恰好命中启发式，也可能被多抓

  也就是说，外置版“更广”，但“更不确定”。

  4. 写盘容错性比原版差一截
     原版的 appendToFile 自己吞掉 mkdir/appendFile 异常，属于 best-effort。
     参考: src/services/api/dumpPrompts.ts:67

  外置版在 request 路径里直接调用了异步 appendToFile(...)，但没有 await 也没有 catch。
  参考: external-dump/anthropic-dump-preload.cjs:175, external-dump/anthropic-dump-preload.cjs:231, external-dump/
  anthropic-dump-preload.cjs:307

  这是真正的功能缺口。目录权限、磁盘错误之类情况，外置版可能触发未处理的 Promise rejection；原版不会。

  5. 输出 schema 不是完全等价
     原版落盘格式核心是:

  - init
  - system_update
  - message
  - response

  且字段很克制，主要是 type/timestamp/data。
  参考: src/services/api/dumpPrompts.ts:119, src/services/api/dumpPrompts.ts:134, src/services/api/dumpPrompts.ts:216

  外置版额外加入了 requestUrl、method、status。
  参考: external-dump/anthropic-dump-preload.cjs:194, external-dump/anthropic-dump-preload.cjs:219, external-dump/
  anthropic-dump-preload.cjs:268

  这不一定是坏事，但如果你后面想复用原版消费者，就不是无缝兼容。

  外置版反而更强的地方

  1. 覆盖范围可能比原版更广
     原版目前只覆盖主 query 链路里的流式请求和 non-streaming fallback。
     参考: src/services/api/claude.ts:818, src/services/api/claude.ts:1780

  像 sideQuery(...) 这种直接拿 client 调 beta.messages.create(...) 的路径，原版并不会自动覆盖，因为它没有传
  fetchOverride。
  参考: src/utils/sideQuery.ts:124, src/utils/sideQuery.ts:182

  外置版只要这些请求最终落到 globalThis.fetch，就有机会一起抓到。

  2. 非流式响应处理更宽容
     原版非流式分支直接 cloned.json()，如果返回不是 JSON，会整体跳过记录。
     参考: src/services/api/dumpPrompts.ts:210

  外置版用 response.text() 再 tryParseJson，即使不是 JSON 也能把原文落盘。
  参考: external-dump/anthropic-dump-preload.cjs:260, external-dump/anthropic-dump-preload.cjs:298

  3. request body 类型兼容略好
     原版默认把 init.body 当字符串。
     参考: src/services/api/dumpPrompts.ts:167

  外置版额外支持了 Buffer 和 Uint8Array。
  参考: external-dump/anthropic-dump-preload.cjs:150

  一句话总结

  如果按“核心 dump 能力”看，外置版已经有 70% 到 80% 的等价性。
  如果按“与项目内原始机制完全一致”看，当前还差这几项关键能力:

  1. 精确的 session/agent 隔离
  2. 原版那套缓存与清理生命周期
  3. 更稳妥的写盘异常处理
  4. 去掉 URL 启发式带来的漏抓/误抓
  5. 输出 schema 完整对齐原版

  如果你要，我下一步可以直接把外置版继续补到“尽量贴近原版”的程度，只改 external-dump/，不碰 src/。

This directory provides an out-of-tree version of the `dumpPrompts` idea.
It does not modify `src/`.

## What it does

It preloads a Node.js hook that wraps `globalThis.fetch`, then writes Anthropic-style
request/response dumps as JSONL.

Output format is intentionally close to `src/services/api/dumpPrompts.ts`:

- `type: "init"`
- `type: "system_update"`
- `type: "message"`
- `type: "response"`

Default output path:

- `~/.claude/dump-prompts/<dump-id>.jsonl`
- or `$CLAUDE_CONFIG_DIR/dump-prompts/<dump-id>.jsonl`

## Quick start

Run a Node-based Claude CLI through the wrapper:

```sh
./external-dump/run-with-dump.sh claude
```

Or inject it yourself:

```sh
NODE_OPTIONS="--require=$(pwd)/external-dump/anthropic-dump-preload.cjs" claude
```

## Useful env vars

- `CLAUDE_DUMP_ID=my-session`
  Choose the JSONL file name.
- `CLAUDE_DUMP_DIR=/tmp/claude-dumps`
  Override the output directory.
- `CLAUDE_DUMP_SPLIT_BY_PID=1`
  Add `-<pid>` suffix to the file name.
- `CLAUDE_DUMP_ONLY_ANTHROPIC=0`
  Disable Anthropic URL filtering and inspect all POST responses.
- `CLAUDE_DUMP_SILENT=1`
  Do not print the output path on startup.
- `CLAUDE_DUMP_DEBUG=1`
  Print best-effort debug errors to stderr.

## Coverage

This works when the target program:

1. runs on Node.js,
2. uses `fetch` or an SDK path that bottoms out in `globalThis.fetch`,
3. does not bypass Node's process-level preload mechanism.

That matches this repository's normal `getAnthropicClient()` path, where the SDK
uses `fetchOverride ?? globalThis.fetch`.

## Limits

This will not help if the released Claude program:

- is a Bun standalone binary,
- is an Electron/native app that does not honor `NODE_OPTIONS`,
- uses a transport that bypasses `globalThis.fetch`,
- or strips/blocks preload flags.

For those cases, prefer a network-layer approach:

- `ANTHROPIC_BASE_URL` -> your reverse proxy
- `HTTPS_PROXY` / `https_proxy` -> MITM proxy
- transparent host-level packet/TLS instrumentation if you control the machine
