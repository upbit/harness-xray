const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

// Usage: NODE_OPTIONS="--require=./claude-fetch-hook.cjs" claude
(() => {
    const hookSymbol = Symbol.for('harness-xray.fetch-hook.installed');
    if (globalThis[hookSymbol]) {
        return;
    }
    globalThis[hookSymbol] = true;

    if (typeof globalThis.fetch !== 'function') {
        return;
    }

    const rawFetch = globalThis.fetch.bind(globalThis);

    // logPath = ~/.claude/fetch_hooks.jsonl
    const logPath = path.join(process.env.HOME || os.homedir(), '.claude', 'fetch_hooks.jsonl');
    let writeQueue = Promise.resolve();

    function nowIso() {
        return new Date().toISOString();
    }

    function safeError(error) {
        if (!(error instanceof Error)) {
            return {
                name: 'NonErrorThrown',
                message: String(error),
            };
        }

        return {
            name: error.name,
            message: error.message,
            stack: error.stack || null,
        };
    }

    function looksTextual(contentType) {
        if (!contentType) {
            return true;
        }

        return /^(text\/)|json|xml|yaml|javascript|ecmascript|x-www-form-urlencoded|text\/event-stream/i.test(contentType);
    }

    function normalizeHeaders(headersLike) {
        if (!headersLike) {
            return {};
        }

        try {
            const headers = new Headers(headersLike);
            const out = {};
            headers.forEach((value, key) => {
                if (Object.prototype.hasOwnProperty.call(out, key)) {
                    const existing = out[key];
                    out[key] = Array.isArray(existing) ? [...existing, value] : [existing, value];
                    return;
                }
                out[key] = value;
            });
            return out;
        } catch (error) {
            return {
                __headers_error__: safeError(error),
            };
        }
    }

    function getHeader(headers, name) {
        const key = name.toLowerCase();
        for (const [headerName, value] of Object.entries(headers)) {
            if (headerName.toLowerCase() === key) {
                return Array.isArray(value) ? value.join(', ') : value;
            }
        }
        return null;
    }

    function buildTextPayload(text, contentType) {
        return {
            content_type: contentType || null,
            encoding: 'utf8',
            size_bytes: Buffer.byteLength(text),
            text,
        };
    }

    function buildBinaryPayload(buffer, contentType) {
        const bytes = Buffer.from(buffer);
        if (looksTextual(contentType)) {
            return buildTextPayload(bytes.toString('utf8'), contentType);
        }

        return {
            content_type: contentType || null,
            encoding: 'base64',
            size_bytes: bytes.length,
            base64: bytes.toString('base64'),
        };
    }

    async function serializeFormData(formData) {
        const fields = [];
        for (const [name, value] of formData.entries()) {
            if (typeof Blob !== 'undefined' && value instanceof Blob) {
                fields.push({
                    name,
                    kind: 'blob',
                    content_type: value.type || null,
                    size_bytes: value.size,
                    filename: typeof value.name === 'string' ? value.name : null,
                });
                continue;
            }

            fields.push({
                name,
                kind: 'text',
                value: String(value),
            });
        }

        return {
            kind: 'form-data',
            fields,
        };
    }

    async function serializeBodyValue(body, contentType) {
        if (body == null) {
            return null;
        }

        if (typeof body === 'string') {
            return buildTextPayload(body, contentType);
        }

        if (body instanceof URLSearchParams) {
            return buildTextPayload(body.toString(), contentType || 'application/x-www-form-urlencoded;charset=UTF-8');
        }

        if (typeof Blob !== 'undefined' && body instanceof Blob) {
            return buildBinaryPayload(await body.arrayBuffer(), contentType || body.type || null);
        }

        if (typeof FormData !== 'undefined' && body instanceof FormData) {
            return await serializeFormData(body);
        }

        if (body instanceof ArrayBuffer) {
            return buildBinaryPayload(body, contentType);
        }

        if (ArrayBuffer.isView(body)) {
            return buildBinaryPayload(body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength), contentType);
        }

        if (typeof body.getReader === 'function') {
            return {
                omitted: true,
                reason: 'ReadableStream bodies are not duplicated to avoid changing application behavior',
                content_type: contentType || null,
            };
        }

        return {
            omitted: true,
            reason: `Unsupported body type: ${Object.prototype.toString.call(body)}`,
            content_type: contentType || null,
        };
    }

    function extractRequestMeta(input, init) {
        const isRequest = typeof Request !== 'undefined' && input instanceof Request;
        const url = isRequest ? input.url : String(input);
        const method = String(init?.method || (isRequest ? input.method : 'GET')).toUpperCase();
        const headers = normalizeHeaders(init?.headers || (isRequest ? input.headers : undefined));

        return {
            url,
            method,
            headers,
            input_kind: isRequest ? 'Request' : typeof input,
        };
    }

    async function extractRequestBody(input, init, headers) {
        const contentType = getHeader(headers, 'content-type');

        if (init && Object.prototype.hasOwnProperty.call(init, 'body')) {
            return await serializeBodyValue(init.body, contentType);
        }

        const isRequest = typeof Request !== 'undefined' && input instanceof Request;
        if (!isRequest || !input.body || input.bodyUsed) {
            return null;
        }

        try {
            const clone = input.clone();
            const text = await clone.text();
            return buildTextPayload(text, contentType);
        } catch (error) {
            return {
                omitted: true,
                reason: 'Request body could not be cloned',
                error: safeError(error),
                content_type: contentType || null,
            };
        }
    }

    async function extractResponseBody(response) {
        const headers = normalizeHeaders(response.headers);
        const contentType = getHeader(headers, 'content-type');

        if (!response.body) {
            return null;
        }

        const clone = response.clone();
        const text = await clone.text();
        return buildTextPayload(text, contentType);
    }

    function appendRecord(record) {
        writeQueue = writeQueue
            .then(async () => {
                await fs.mkdir(path.dirname(logPath), { recursive: true });
                await fs.appendFile(logPath, `${JSON.stringify(record)}\n`, 'utf8');
            })
            .catch(() => { });

        return writeQueue;
    }

    void appendRecord({
        ts: nowIso(),
        event: 'fetch_hook_installed',
        pid: process.pid,
        node: process.version,
        platform: process.platform,
        log_path: logPath,
    });

    globalThis.fetch = async function hookedFetch(input, init) {
        const fetchId = crypto.randomUUID();
        const startedAt = Date.now();
        const request = extractRequestMeta(input, init);
        request.body = await extractRequestBody(input, init, request.headers);

        void appendRecord({
            ts: nowIso(),
            event: 'fetch_request',
            fetch_id: fetchId,
            pid: process.pid,
            request,
        });

        try {
            const response = await rawFetch(input, init);
            const responseHeaders = normalizeHeaders(response.headers);
            const responseMeta = {
                url: response.url,
                redirected: response.redirected,
                ok: response.ok,
                status: response.status,
                status_text: response.statusText,
                type: response.type,
                headers: responseHeaders,
            };

            void appendRecord({
                ts: nowIso(),
                event: 'fetch_response_headers',
                fetch_id: fetchId,
                pid: process.pid,
                duration_ms: Date.now() - startedAt,
                request: {
                    url: request.url,
                    method: request.method,
                },
                response: responseMeta,
            });

            (async () => {
                try {
                    const responseBody = await extractResponseBody(response);
                    await appendRecord({
                        ts: nowIso(),
                        event: 'fetch_response_body',
                        fetch_id: fetchId,
                        pid: process.pid,
                        duration_ms: Date.now() - startedAt,
                        request: {
                            url: request.url,
                            method: request.method,
                        },
                        response: {
                            ...responseMeta,
                            body: responseBody,
                        },
                    });
                } catch (error) {
                    await appendRecord({
                        ts: nowIso(),
                        event: 'fetch_response_body_error',
                        fetch_id: fetchId,
                        pid: process.pid,
                        duration_ms: Date.now() - startedAt,
                        request: {
                            url: request.url,
                            method: request.method,
                        },
                        error: safeError(error),
                    });
                }
            })();

            return response;
        } catch (error) {
            void appendRecord({
                ts: nowIso(),
                event: 'fetch_error',
                fetch_id: fetchId,
                pid: process.pid,
                duration_ms: Date.now() - startedAt,
                request: {
                    url: request.url,
                    method: request.method,
                },
                error: safeError(error),
            });
            throw error;
        }
    };
})();
