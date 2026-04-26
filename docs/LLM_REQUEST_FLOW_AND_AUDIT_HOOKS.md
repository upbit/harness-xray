# `src` 目录 LLM 请求链路与无源码审计 Hook 分析

## 1. 结论先说

这个项目里，主对话的 LLM 请求最终落点在 `src/services/api/claude.ts` 的 `queryModel()` 中：

- 流式主请求最终发起位置：`anthropic.beta.messages.create({ ...params, stream: true }).withResponse()`
- 代码位置：`src/services/api/claude.ts:1822`

非流式回退请求最终落点同样在 `src/services/api/claude.ts`：

- 非流式回退位置：`anthropic.beta.messages.create(...)`
- 代码位置：`src/services/api/claude.ts:864`

真正负责构造 SDK client 的位置在 `src/services/api/client.ts`：

- `getAnthropicClient(...)`
- 代码位置：`src/services/api/client.ts:88`

真正靠近网络层、最适合做无源码审计 hook 的位置有 4 类：

1. 上游 API 代理 hook：`ANTHROPIC_BASE_URL` 指向自建网关 / 反向代理
2. 通用 HTTP 代理 hook：`HTTPS_PROXY` / `https_proxy` + `NO_PROXY`
3. 进程内 fetch hook：通过 `NODE_OPTIONS=--require ...` 或 `--import ...` 预加载补丁，包裹 `globalThis.fetch`
4. Bun 专用 unix socket hook：`ANTHROPIC_UNIX_SOCKET`

如果你的目标是"完整审计发送和接收内容"，优先级建议如下：

1. `ANTHROPIC_BASE_URL` 到自建代理
2. `HTTPS_PROXY` 到 `mitmproxy` / `Charles` / 自建透明审计代理
3. 运行时 preload `fetch` 补丁
4. eBPF / syscall / socket 层观测作为兜底


## 2. 主链路怎么走到最终请求点

### 2.1 从 QueryEngine 进入查询循环

SDK / 会话级入口由 `QueryEngine.submitMessage()` 驱动：

- `src/QueryEngine.ts:675` 调用 `query({...})`

这里的 `query()` 是整个 agentic turn 的主循环，负责：

- 组装消息
- 处理工具调用
- 处理 compact / fallback / retry
- 调用真正的模型请求函数


### 2.2 `query()` 通过依赖注入调用模型层

`query()` 内部不是直接发请求，而是通过 `deps.callModel(...)` 调用模型层：

- `src/query.ts:659` 调用 `deps.callModel({...})`

默认依赖来自：

- `src/query/deps.ts:33`

其中：

- `callModel: queryModelWithStreaming`

所以默认情况下，`query()` 最终会进入：

- `src/services/api/claude.ts:752` `queryModelWithStreaming(...)`


### 2.3 `queryModelWithStreaming()` 再进入 `queryModel()`

`queryModelWithStreaming()` 本身只是薄封装：

- `src/services/api/claude.ts:770`
- `yield* queryModel(...)`

因此主请求逻辑实际都在：

- `src/services/api/claude.ts` 的 `queryModel()`


### 2.4 `queryModel()` 负责构造最终 API 参数

`queryModel()` 里最关键的是 `paramsFromContext(...)`，它把真正要发给模型的请求体组装出来：

- `src/services/api/claude.ts:1538`

这里会构造：

- `model`
- `messages`
- `system`
- `tools`
- `tool_choice`
- `betas`
- `metadata`
- `max_tokens`
- `thinking`
- `output_config`
- `speed`
- `context_management`
- `extraBodyParams`

真正送到模型侧的 payload，逻辑上就是这里返回的对象。


### 2.5 流式主请求的最终网络调用点

在 `queryModel()` 的 retry 包装里，真正的流式请求是：

```ts
const result = await anthropic.beta.messages
  .create(
    { ...params, stream: true },
    {
      signal,
      ...(clientRequestId && {
        headers: { [CLIENT_REQUEST_ID_HEADER]: clientRequestId },
      }),
    },
  )
  .withResponse()
```

位置：

- `src/services/api/claude.ts:1822`

这就是主会话流式对话请求的最终发送点。


### 2.6 响应流的最终接收与解析位置

收到响应后，代码开始消费流：

- `src/services/api/claude.ts:1940`
- `for await (const part of stream)`

这里按事件类型拆解响应：

- `message_start`
- `content_block_start`
- `content_block_delta`
- `content_block_stop`
- `message_delta`
- `message_stop`

也就是说，如果你要审计"接收流量"，从语义层面看，最终处理入口就在这里。


### 2.7 非流式 fallback 的最终请求点

当流式请求异常或触发回退时，代码会走 `executeNonStreamingRequest(...)`：

- `src/services/api/claude.ts:818`

最终请求语句：

```ts
return await anthropic.beta.messages.create(
  {
    ...adjustedParams,
    model: normalizeModelStringForAPI(adjustedParams.model),
  },
  {
    signal: retryOptions.signal,
    timeout: fallbackTimeoutMs,
  },
)
```

位置：

- `src/services/api/claude.ts:864`

因此，做审计时不能只盯 `stream: true` 那条链，非流式 fallback 也必须覆盖。


## 3. Client 是如何接到网络层的

### 3.1 `getAnthropicClient()` 是总装配点

模型 client 的统一创建入口是：

- `src/services/api/client.ts:88`

这里会根据环境决定 provider：

- first-party
- bedrock
- vertex
- foundry

provider 判定逻辑在：

- `src/utils/model/providers.ts:6`


### 3.2 请求真正接到 fetch 的位置

`getAnthropicClient()` 会构造 `resolvedFetch = buildFetch(fetchOverride, source)`：

- `src/services/api/client.ts:139`

`buildFetch(...)` 在：

- `src/services/api/client.ts:358`

这里的关键逻辑是：

```ts
const inner = fetchOverride ?? globalThis.fetch
return (input, init) => {
  const headers = new Headers(init?.headers)
  ...
  return inner(input, { ...init, headers })
}
```

这意味着：

1. 如果上层传了 `fetchOverride`，最终请求会先进这个 override
2. 否则会走 `globalThis.fetch`

这正是为什么"无源码 hook"里，运行时包裹 `globalThis.fetch` 是一个很有价值的切入点。


### 3.3 代理 / unix socket / TLS 配置从哪里进入

`getAnthropicClient()` 还会把 `getProxyFetchOptions({ forAnthropicAPI: true })` 塞进 client 配置：

- `src/services/api/client.ts:146`

对应实现：

- `src/utils/proxy.ts:288`

这里明确支持：

- `HTTPS_PROXY` / `https_proxy`
- `NO_PROXY` / `no_proxy`
- Bun 下的 `ANTHROPIC_UNIX_SOCKET`

所以，代码本身已经给网络转接 / 审计代理留下了正式入口，不需要改源码。


## 4. 除主会话外，其他会发 LLM 请求的路径

如果你的审计目标是"所有模型流量"，不能只覆盖主对话链路，还要覆盖这些旁路：

### 4.1 `sideQuery(...)`

位置：

- `src/utils/sideQuery.ts:107`

最终调用：

- `src/utils/sideQuery.ts:182`
- `client.beta.messages.create(...)`

用途通常是：

- 分类器
- 权限解释
- session search
- 各类 sidecar 判断


### 4.2 `queryHaiku(...)` / `queryWithModel(...)`

位置：

- `src/services/api/claude.ts:3241`
- `src/services/api/claude.ts:3300`

这两个会走 `queryModelWithoutStreaming(...)`，最终仍然进入 `queryModel()` / `executeNonStreamingRequest()` 体系。


### 4.3 token 计数相关请求

位置：

- `src/services/tokenEstimation.ts:172` `anthropic.beta.messages.countTokens(...)`
- `src/services/tokenEstimation.ts:302` `anthropic.beta.messages.create(...)`

这些不是主对话内容生成，但仍然是发到模型侧的流量。如果审计需要做合规全量覆盖，这部分也要算。


## 5. 仓库里已经存在的内建抓包思路

项目里其实已经有一个现成的"请求/响应落盘"实现：

- `src/services/api/dumpPrompts.ts:146` `createDumpPromptsFetch(...)`

这个函数会：

1. 在请求发送前异步保存 request body
2. 在响应返回后保存 response body / SSE chunks

内部实现关键点：

- `src/services/api/dumpPrompts.ts:162` 记录 POST body
- `src/services/api/dumpPrompts.ts:171` 调用 `globalThis.fetch`
- `src/services/api/dumpPrompts.ts:183` 读取 streaming response 并解析 SSE

在主查询链路中，它是通过 `fetchOverride` 注入的：

- `src/query.ts:588` 创建 `dumpPromptsFetch`
- `src/query.ts:688` 作为 `fetchOverride` 传入

但是这里有个现实限制：

- 只有 `config.gates.isAnt` 时才会启用
- `buildQueryConfig()` 里 `isAnt` 等于 `process.env.USER_TYPE === 'ant'`
- 位置：`src/query/config.ts:31`

所以对普通外部用户来说，这条内建抓包路径不是通用开关，更像内部诊断能力。


## 6. 不修改源码时，可以怎么做流量审计 hook

下面按可落地程度排序。

### 方案 A：`ANTHROPIC_BASE_URL` 指向自建网关 / 反向代理

#### 原理

把模型 API 上游改成你控制的网关，让所有请求和响应都先经过代理，再转发到 Anthropic / 兼容上游。

代码层面的依据：

- 项目多处显式把 `ANTHROPIC_BASE_URL` 视为 API 上游
- `src/utils/model/providers.ts:25` 会用它判断是否 first-party host
- `src/main.tsx:1320` 其他 API 也会使用该 base URL

#### 优点

- 不改源码
- 能看到完整请求体和响应体
- 对 streaming / non-streaming 都有效
- 对主链路和绝大部分 sideQuery 路径都有效
- 审计逻辑可以独立演进

#### 风险 / 限制

- 需要你的代理兼容 SSE streaming
- 需要正确转发 header、timeout、chunked response
- 如果走 Bedrock / Vertex / Foundry，base URL 方案不一定是同一套，需要按 provider 分别处理

#### 适用性判断

这是最稳妥、最工程化的审计方案。


### 方案 B：`HTTPS_PROXY` / `https_proxy` 接本地 MITM 代理

#### 原理

通过环境变量把出站 HTTP(S) 流量导入 `mitmproxy`、Charles、Burp 或自建代理。

代码层面的依据：

- `src/utils/proxy.ts:288` `getProxyFetchOptions(...)`
- `src/utils/proxy.ts:307` 读取 proxy URL
- `src/utils/proxy.ts:314` Node 下使用 `dispatcher: getProxyAgent(proxyUrl)`

#### 优点

- 不改源码
- 很容易落地
- 可以覆盖 `globalThis.fetch` 路径
- 如果证书信任配置正确，能抓到明文请求和响应

#### 风险 / 限制

- 需要导入并信任本地 CA
- 如果某些 provider SDK 不是走同一 fetch/dispatcher，可能存在遗漏，需要验证
- streaming SSE 要确保代理不缓冲、不篡改 chunk 边界

#### 建议

如果只是先做 PoC，这是最快的办法。

示意：

```bash
export HTTPS_PROXY=http://127.0.0.1:8080
export https_proxy=http://127.0.0.1:8080
claude ...
```


### 方案 C：进程 preload，hook `globalThis.fetch`

#### 原理

在进程启动前预加载一个 JS 模块，包裹 `globalThis.fetch`，记录：

- URL
- headers
- request body
- response status
- response headers
- streaming body

代码层面的依据：

- `src/services/api/client.ts:363` 最终会回落到 `globalThis.fetch`
- `src/services/api/client.ts:387` 真正调用 `inner(input, init)`

#### 优点

- 不改仓库源码
- 语义层级高，拿到的是应用层 request/response
- 比 eBPF / socket hook 更容易还原 payload

#### 风险 / 限制

- 需要控制启动方式
- 要兼容 ESM / CJS / Bun / Node 的预加载方式
- 只 hook `globalThis.fetch` 时，必须确认 provider SDK 最终也走这里
- 如果 response body 被你先读掉，必须用 `response.clone()` 或重新封装，否则会破坏上层读取

#### 实战建议

如果你能控制启动命令，这个方案很强，尤其适合做定向审计。

最小思路：

```js
const rawFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const reqBody = init?.body;
  const resp = await rawFetch(input, init);
  const cloned = resp.clone();
  // 这里异步落盘 cloned.body / cloned.text()
  return resp;
};
```


### 方案 D：Bun 的 `ANTHROPIC_UNIX_SOCKET`

#### 原理

把 Anthropic API 请求转发到本地 unix socket，socket 后面挂你的审计代理。

代码层面的依据：

- `src/utils/proxy.ts:297`
- `src/utils/proxy.ts:300`
- 只在 `opts.forAnthropicAPI` 且 `typeof Bun !== 'undefined'` 时启用

#### 优点

- 非常干净
- 审计面只覆盖 Anthropic API，不容易误伤别的 HTTP 流量
- 很适合本地代理 / ssh 转发 / 受控通道

#### 风险 / 限制

- Bun 专用
- 只覆盖 Anthropic API 路径
- 需要自己实现 unix socket 后端代理


### 方案 E：OS / 内核层 hook（eBPF / syscall / socket）

#### 原理

在进程外观察 `connect` / `send` / `recv` / TLS 会话。

#### 优点

- 完全不碰源码
- 对所有进程都生效

#### 风险 / 限制

- 默认只能稳定拿到元数据，拿不到 TLS 明文
- 如果要拿明文，最终还是要结合 MITM 或更深层 TLS hook
- 语义恢复困难，难以直接映射到 `messages` / `tools` / `thinking`

#### 结论

这个更适合做"补充观测"，不适合做首选内容审计方案。


## 7. 如果目标是审计"发送内容 + 接收内容"，推荐的组合

### 推荐组合 1：自建 API 网关

适用目标：

- 生产级审计
- 长期留存
- 合规审计
- 多用户统一接入

推荐做法：

1. 用 `ANTHROPIC_BASE_URL` 指向你的代理
2. 代理完整记录请求体、响应头、响应体
3. 对 `text/event-stream` 逐 chunk 落盘
4. 用 `x-client-request-id` / request id 做链路关联

注意点：

- 项目自己也会注入 `x-client-request-id`
- 相关位置：`src/services/api/client.ts:356`、`src/services/api/client.ts:374`


### 推荐组合 2：本地 MITM + HTTPS_PROXY

适用目标：

- 快速验证
- 单机审计
- 调试某次对话实际发了什么

推荐做法：

1. 起本地 `mitmproxy`
2. 设 `HTTPS_PROXY`
3. 导入 mitm CA
4. 对 `/v1/messages`、SSE streaming 响应做日志归档


### 推荐组合 3：preload fetch hook + 本地落盘

适用目标：

- 不想改服务网络路径
- 只想在本机上审计某个 CLI 进程
- 需要拿到最接近应用层的请求对象

推荐做法：

1. preload 一个 `fetch` wrapper
2. 对请求体直接落 JSON
3. 对响应使用 `response.clone()` 后异步保存
4. streaming 响应按 SSE 事件切分保存


## 8. 审计时要特别覆盖的分支

如果你要确保"没有漏流量"，至少要覆盖这些分支：

1. 主流式对话
2. 非流式 fallback
3. `sideQuery(...)`
4. token counting (`countTokens` / fallback create)
5. Haiku / 指定模型辅助查询

否则你看到的只会是主聊天流量，不是完整 LLM 流量。


## 9. 一个很实际的判断

如果你只是要"审计所有发送和接收内容"，而且不修改源码，那么最佳切入点不是 `query.ts` 或 `QueryEngine.ts`，而是：

1. `src/services/api/client.ts` 对应的运行时网络出口
2. `src/services/api/claude.ts` 对应的 API 调用点
3. `src/utils/sideQuery.ts` 和 `src/services/tokenEstimation.ts` 这些旁路

换句话说：

- 逻辑入口在 `QueryEngine` / `query`
- 语义上最终请求组装在 `queryModel()`
- 工程上最佳 hook 位于 client/fetch/proxy 这一层


## 10. 关键代码坐标清单

主链路：

- `src/QueryEngine.ts:675`
- `src/query.ts:659`
- `src/query/deps.ts:33`
- `src/services/api/claude.ts:752`
- `src/services/api/claude.ts:1538`
- `src/services/api/claude.ts:1822`
- `src/services/api/claude.ts:1940`
- `src/services/api/claude.ts:818`
- `src/services/api/claude.ts:864`

client / 网络层：

- `src/services/api/client.ts:88`
- `src/services/api/client.ts:139`
- `src/services/api/client.ts:358`
- `src/services/api/client.ts:387`
- `src/utils/proxy.ts:288`
- `src/utils/proxy.ts:307`
- `src/utils/proxy.ts:314`

旁路请求：

- `src/utils/sideQuery.ts:107`
- `src/utils/sideQuery.ts:182`
- `src/services/tokenEstimation.ts:172`
- `src/services/tokenEstimation.ts:302`
- `src/services/api/claude.ts:3241`
- `src/services/api/claude.ts:3300`

内建 dump 能力：

- `src/query.ts:588`
- `src/query.ts:688`
- `src/services/api/dumpPrompts.ts:146`
- `src/services/api/dumpPrompts.ts:162`
- `src/services/api/dumpPrompts.ts:171`
- `src/services/api/dumpPrompts.ts:183`
- `src/query/config.ts:31`


## 11. 最后建议

如果你下一步真要落地审计，而不是只做静态分析，我建议按这个顺序推进：

1. 先用 `HTTPS_PROXY + mitmproxy` 验证能不能完整抓到 `/v1/messages` 的 streaming 流量
2. 再决定是否升级为 `ANTHROPIC_BASE_URL -> 自建审计网关`
3. 如果你只能控制本地启动命令，再补一个 preload `fetch` hook 做双保险

这样成本最低，而且能很快知道这套 CLI 在你当前运行环境里，实际流量出口到底是哪一条。
