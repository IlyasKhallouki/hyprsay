# Jev wire protocol research (lane: jev-wire)

Date: 2026-09-20. Author: research subagent. No live Jev call was made (no key on this machine). Every latency number below is either a vendor claim or a local measurement of an unauthenticated, rejected request (network floor only, not model time).

Evidence directory (all artifacts kept for re-inspection):
`/tmp/claude-1000/-home-ilyask-projects-hypruse/4e68274c-d47c-460a-a4ca-e5138b66c779/scratchpad/npm-jev/`

- `gw/package/` = `@ai-sdk/gateway@4.0.87` (npm pack, 2026-09-20)
- `ai/package/` = `ai@7.0.107`
- `ts/package/` = `@ai-sdk/typesafe-ai@3.0.4`
- `prov/package/` = `@ai-sdk/provider@4.0.17`, `pu/package/` = `@ai-sdk/provider-utils@5.0.45`
- `typesafe-sdk-python/` = github.com/typesafe-ai/typesafe-sdk-python @ 2ce5c65 (v0.7.0, 2026-09-18)
- `typesafe-sdk-js/` = github.com/typesafe-ai/typesafe-sdk-js @ 66880cc (2026-09-15)
- `ts-openapi.json` = https://api.typesafe.ai/openapi.json (public, fetched without auth)
- `llms-full.txt` = https://docs.typesafe.ai/llms-full.txt (full TypeSafe docs, 895 KB)
- `vdocs/*.md` = Vercel docs pages fetched as markdown
- `models.json` = https://ai-gateway.vercel.sh/v1/models (public)

## 0. Headline

There are FOUR ways to reach Jev over plain HTTPS POST + JSON. All four were confirmed to exist (three from package source or official docs, and all four by an unauthenticated probe that returned a route-specific error). None of them needs Node.

| # | Route | URL | Who documents it | Model id goes in | Boolean type name | Stability |
|---|-------|-----|------------------|------------------|-------------------|-----------|
| A | Gateway public HTTP API | `POST https://ai-gateway.vercel.sh/v1/evaluate` | Vercel docs (modalities/evaluation) | body `model` | `boolean` / `probability` | documented, public |
| B | Gateway TypeSafe-compatible API | `POST https://ai-gateway.vercel.sh/typesafe/v1/systemone` | Vercel docs (sdks-and-apis/typesafe) | body `model` | `noul` / `noul` | documented, public, mirrors TypeSafe OpenAPI |
| C | Gateway AI SDK internal protocol | `POST https://ai-gateway.vercel.sh/v4/ai/evaluation-model` | only the npm package source | header `ai-model-id` | `boolean` / `probability` | internal, version-gated, "may change in patch releases" |
| D | TypeSafe direct | `POST https://api.typesafe.ai/v1/systemone` | TypeSafe docs + OpenAPI | body `model` (`jev-latest`) | `noul` / `noul` | documented, public, separate key and billing |

Recommendation in one line: implement a tiny Python `JevClient` on `httpx` with HTTP/2 and a pre-warmed persistent connection, speaking the TypeSafe shape (route B through the gateway as the default, route D as a config switch because it is the identical shape), keep route A as a second adapter, and do not build on route C.

## 1. Route C: what the AI SDK actually sends (reconstructed from the package)

This is what `experimental_evaluate({ model: 'typesafe-ai/jev', ... })` does on the wire.

### 1.1 Base URL, path, method

`gw/package/src/gateway-provider.ts:315-317`
```ts
const baseURL =
  withoutTrailingSlash(options.baseURL) ??
  'https://ai-gateway.vercel.sh/v4/ai';
```
`gw/package/src/gateway-evaluation-model.ts:100-102`
```ts
private getUrl() {
  return `${this.config.baseURL}/evaluation-model`;
}
```
Same in compiled output: `gw/package/dist/index.js:2340` and `:3635`.
Method is POST with a JSON string body: `pu/package/src/post-to-api.ts:98-107` (`method: 'POST'`, `'Content-Type': 'application/json'` at line 34, `body: JSON.stringify(body)` at line 38).

Full URL: `https://ai-gateway.vercel.sh/v4/ai/evaluation-model`. Local probe confirmed the route: response header `x-matched-path: /v4/ai/evaluation-model`.

### 1.2 Headers (complete list)

Auth and protocol headers, `gw/package/src/gateway-provider.ts:319-334`:
```ts
Authorization: `Bearer ${auth.token}`,
'ai-gateway-protocol-version': AI_GATEWAY_PROTOCOL_VERSION,   // '0.0.1' (line 295)
[GATEWAY_AUTH_METHOD_HEADER]: auth.authMethod,                 // 'ai-gateway-auth-method': 'api-key' | 'oidc'
...(options.teamIdOrSlug != null ? { [VERCEL_AI_GATEWAY_TEAM_HEADER]: options.teamIdOrSlug } : {}),  // 'x-vercel-ai-gateway-team'
...options.headers,
```
Header name constants: `gw/package/src/gateway-headers.ts:1-3`.

Model headers, `gw/package/src/gateway-evaluation-model.ts:104-109`:
```ts
'ai-evaluation-model-specification-version': '4',
'ai-model-id': this.modelId,
```

Observability headers, only present when the matching env var exists (so absent on a desktop), `gw/package/src/gateway-provider.ts:409-437`:
`ai-o11y-deployment-id` (VERCEL_DEPLOYMENT_ID), `ai-o11y-environment` (VERCEL_ENV), `ai-o11y-region` (VERCEL_REGION), `ai-o11y-request-id` (from `x-vercel-id` of the inbound request context), `ai-o11y-project-id` (VERCEL_PROJECT_ID).

User agent: concatenation of suffixes, for example `ai/7.0.107 ai-sdk/gateway/4.0.87 ai-sdk/provider-utils/5.0.45 runtime/node.js/v26...` (`ai/package/src/evaluate/evaluate.ts:67`, `gateway-provider.ts:333`, `post-to-api.ts:100-104`). Not required by anything I could find.

Observed behaviour (local probe, no credentials):
- Without `ai-gateway-protocol-version`: HTTP 400 `{"error":{"message":"Unsupported gateway protocol version","type":"invalid_request_error","code":400}}`. So on route C this header is mandatory.
- Without `Authorization`: HTTP 401 `{"error":{"message":"Missing Authorization header","type":"authentication_error"}}`.
- With a bogus bearer: HTTP 401 `{"error":{"message":"Authentication failed. Create an API key and set in AI_GATEWAY_API_KEY environment variable: ...","type":"authentication_error"}}`.

### 1.3 API key vs OIDC

`gw/package/src/gateway-provider.ts:683-703`: if `options.apiKey` or env `AI_GATEWAY_API_KEY` is set, it is used and `ai-gateway-auth-method: api-key` is sent. Otherwise `getVercelOidcToken()` from `@vercel/oidc@3.2.0` is used and `ai-gateway-auth-method: oidc` is sent. Both travel identically as `Authorization: Bearer <token>`. The auth-method header only tells the gateway which kind it is, and is reused client side to craft a contextual error (`errors/parse-auth-method.ts`).

Vercel docs: API keys "never expire unless you revoke them"; OIDC tokens are "only valid for 12 hours" in local dev and need `vercel env pull` to refresh (`vdocs/auth.md:63`, `vdocs/oidc.md:39`). For a desktop daemon the API key is the only sensible option. OIDC is for code running on Vercel.

### 1.4 Request body

`gw/package/src/gateway-evaluation-model.ts:63-67`:
```ts
body: {
  state,
  questions,
  ...(providerOptions ? { providerOptions } : {}),
},
```
There is no `model` field in the body on route C. The model travels in the `ai-model-id` header.

Question shape is the AI SDK spec, passed through unmodified (`prov/package/src/evaluation-model/v4/evaluation-model-v4-question.ts`):
```ts
type Input = string | JSONObject | JSONValue[];
{ type: 'choice',  instructions: Input, criteria: Record<string, Input | null> }   // nonempty
{ type: 'score',   instructions: Input, criteria: (Input | null)[] }                // >= 2 levels
{ type: 'boolean', instructions: Input, criteria?: { true?: Input|null, false?: Input|null } }
```
`state: Input`. "An array is one state, not a batch of unrelated inputs" (`ai/package/docs/03-ai-sdk-core/32-evaluation.mdx:9-10`).

Note: the AI SDK spec makes `instructions` required. TypeSafe's own API makes `instructions` optional for all three types (`typesafe-sdk-python/src/typesafe_sdk/_schemas/models.py:37,94,139`, default `None`).

### 1.5 Response body

Zod schema, `gw/package/src/gateway-evaluation-model.ts:112-173`:
```ts
answers: Record<string,
   { type:'choice',  choice: string, probabilities?: Record<string, number> }
 | { type:'score',   score: number,  probabilities?: Record<string, number> }   // keys are "0","1",...
 | { type:'boolean', probability: number } >,
rounding?: { probabilityDecimals?: number, scoreDecimals?: number },
usage?: { inputTokens?: number, outputTokens?: number },
warnings?: Array<{type:'unsupported',feature,details?} | {type:'compatibility',feature,details?} | {type:'deprecated',setting,message} | {type:'other',message}>,
providerMetadata?: Record<string, Record<string, unknown>>
```
IMPORTANT: there is no `confidence` field inside the answer on routes A and C. TypeSafe's separate confidence statistic is carried at `providerMetadata.typesafe.confidence[questionId]`:
- `ts/package/src/typesafe-ai-evaluation-model.ts:131-137,175` builds `providerMetadata: { typesafe: { confidence } }`.
- Vercel changelog: "TypeSafe reports separate Choice and Score confidence in `result.providerMetadata.typesafe.confidence`" (`vdocs/changelog_typesafe-ai-jev-now-available-on-ai-gateway.md:54`).
- Vercel KB: confidence "summarizes how concentrated the probability distribution is, from 0 (spread evenly across options) to 1 (all on one option) ... it isn't returned for boolean answers" (`vdocs/kb_guide_typesafe-jev-and-ai-sdk.md:314`).

TypeSafe rounds probabilities and scores to two decimals (`ts/package/src/typesafe-ai-evaluation-model.ts:172-173`, `rounding: { probabilityDecimals: 2, scoreDecimals: 2 }`), so a distribution may sum to 0.99 or 1.01. Do not assert sum == 1 in Python. The AI SDK validator tolerance is `1e-6 + n_options * 0.005` (`ai/package/src/evaluate/validate-evaluation.ts:168,193`).

### 1.6 Client side validation the SDK performs (replicate the useful parts in Python)

`ai/package/src/evaluate/validate-evaluation.ts`:
- state must be string, plain object or array, JSON compatible (lines 54-60)
- questions nonempty map (61-63); choice criteria nonempty map (77); score criteria array with >= 2 levels (86); boolean criteria only keys `true`/`false` (94-105)
- answers must have exactly the same keys as questions (197-202); answer type must equal question type (206); choice must be a declared option (215-223); selected choice must be the argmax of the distribution (232-242); score in [0, n-1] (247-256)

TypeSafe-specific limits, enforced client side in `ts/package/src/typesafe-ai-evaluation-model.ts:71-87`: at most 255 choice options, at most 10 score levels. The gateway provider (`GatewayEvaluationModel`) does NOT enforce these locally, so via the gateway a violation surfaces as a server error.

### 1.7 Retries and timeouts in the SDK

`ai/package/src/evaluate/evaluate.ts:60-70`: `prepareRetries({ maxRetries })`, default 2, exponential backoff, only for retryable `APICallError`. No default timeout: only `abortSignal`. For a voice loop we want the opposite policy (hard deadline, at most one immediate retry).

### 1.8 Error shapes (gateway)

`gw/package/src/errors/create-gateway-error.ts:151-163`:
```ts
{ error: { message: string, type?: string|null, param?: unknown, code?: string|number|null }, generationId?: string|null }
```
`error.type` values mapped (lines 69-148): `authentication_error`, `invalid_request_error`, `rate_limit_exceeded`, `model_not_found` (param.modelId), `not_found`, `internal_server_error`, `failed_dependency`, `forbidden` (param.ruleId). Unknown types are treated as internal server error. Timeouts are detected from undici codes (`as-gateway-error.ts:19-23`), irrelevant for Python.
Rate limit body per Vercel docs: HTTP 429 `{"error":{"message":"Rate limit exceeded","type":"rate_limit_exceeded"}}`, sometimes with a `retry-after` header in seconds (`vdocs/rate-limits.md`). A provider 429 "can carry that provider's own error body instead of the AI Gateway error shape".

### 1.9 Version facts

- `@ai-sdk/gateway` 4.0.85: "feat(gateway): add experimental evaluation model support" and "Include evaluation models in getAvailableModels()" (`gw/package/CHANGELOG.md:24-25`). Commit 6982e9d5c6, 2026-09-16T22:26:29Z, PR #20885, https://github.com/vercel/ai/commit/6982e9d5c650cad0bdc5f49457d27223c802eda5
- `ai` 7.0.105: "Resolve evaluation model IDs through AI Gateway when no default provider is configured" (`ai/package/CHANGELOG.md:45-49`). This confirms the ">= 7.0.105" claim in the task brief. Latest at time of research: `ai@7.0.107`, `@ai-sdk/gateway@4.0.87` (2026-09-18).
- Model id type: `gw/package/src/gateway-evaluation-model-settings.ts:1`: `'typesafe-ai/jev' | (string & {})`.
- DISCREPANCY: `ai/package/docs/03-ai-sdk-core/32-evaluation.mdx:135` uses `'typesafe-ai/jev-latest'` as the gateway string id. The live gateway catalog (`models.json`) only lists `typesafe-ai/jev`, and every Vercel doc page uses `typesafe-ai/jev`. Use `typesafe-ai/jev`. Whether `typesafe-ai/jev-latest` also resolves is unverified.
- The whole surface is flagged experimental: "This API and the evaluation model specification are experimental and may change in patch releases" (`32-evaluation.mdx:12-13`).

## 2. Route A: documented gateway HTTP API (`/v1/evaluate`)

Source: https://vercel.com/docs/ai-gateway/modalities/evaluation (saved `vdocs/ai-gateway_modalities_evaluation.md`), section "HTTP API":

> If you are not using the AI SDK, post to `/v1/evaluate` with the same `model`, `state`, and `questions` fields.

```bash
curl https://ai-gateway.vercel.sh/v1/evaluate \
  -H "Authorization: Bearer $AI_GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"typesafe-ai/jev","state":"I was charged twice for my subscription.",
       "questions":{"refund":{"type":"boolean","instructions":"Is the customer asking for money back?"}}}'
```
Documented response:
```json
{
  "model": "typesafe-ai/jev",
  "answers": { "refund": { "type": "boolean", "probability": 0.98 } },
  "usage": { "inputTokens": 275, "outputTokens": 20 },
  "providerMetadata": { "gateway": {
      "routing": { "originalModelId": "typesafe-ai/jev", "resolvedProvider": "typesafe-ai", "canonicalSlug": "typesafe-ai/jev", "finalProvider": "typesafe-ai" },
      "cost": "0.00001155", "marketCost": "0.00001155", "surchargeCost": "0", "gatewayCost": "0.00001155",
      "generationId": "gen_..." } }
}
```
Optional body field `providerOptions`, example from the same page: `{"gateway": {"zeroDataRetention": true, "only": ["typesafe-ai"]}}`.
The same page states evaluation "is not supported through the OpenAI-compatible, Anthropic-compatible, or Cohere-compatible endpoints".

Local probe: `x-matched-path: /v1/evaluate`, HTTP/2, 401 `{"error":{"message":"Authentication failed","param":null,"type":"authentication_error"}}`. No protocol version header needed (unlike route C).

Not shown in the docs: whether `providerMetadata.typesafe.confidence` is present on this route. The example is boolean only, and booleans have no confidence. Must be verified with the first real call.

## 3. Route B: gateway TypeSafe-compatible API

Source: https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe (saved `vdocs/ai-gateway_sdks-and-apis_typesafe.md`).

- Base URL: `https://ai-gateway.vercel.sh/typesafe`
- Endpoints: `POST /typesafe/v1/systemone`, `GET /typesafe/v1/models`
- Auth: `Authorization: Bearer <AI_GATEWAY_API_KEY or OIDC token>`
- "This API implements the TypeSafe request and response shapes." Vercel's own Python example on that page uses `requests.post` with only `Authorization` and `Content-Type`.
- Body: `{"model":"typesafe-ai/jev","state":...,"questions":{"id":{"type":"noul"|"choice"|"score","instructions":...,"criteria":...}}}`
- Documented response: TypeSafe field names plus a snake_case `provider_metadata.gateway{routing,cost,marketCost,surchargeCost,gatewayCost,generationId}` block. `usage` is `{input_tokens, output_tokens}`.
- Errors: `{"message": "...", "error_type": "invalid_request"}`; "Errors returned by the model provider are passed through unchanged".
- Vercel's guidance: "If you are writing new code rather than migrating, use the evaluation API instead. It is the same capability without TypeSafe-specific naming."

Local probes:
- `POST /typesafe/v1/systemone` with `type: "boolean"` and no auth returned HTTP 400 `{"message":"questions.q.type: expected one of 'noul', 'choice', 'score'","error_type":"invalid_request"}`. So this route validates the body BEFORE auth, and really does require `noul`.
- Bogus key: HTTP 401 `{"message":"Authentication failed","error_type":"authentication_error"}`.
- `GET /typesafe/v1/models` answered WITHOUT auth: `{"models":[{"name":"jev","description":"...","release_date":"2026-09-15"}]}`. Note the listed name is `jev`, while the documented body value is `typesafe-ai/jev`. Which names the `model` field accepts here (`jev`, `typesafe-ai/jev`, `jev-latest`, `jev-1.13.0`) is unverified beyond the documented `typesafe-ai/jev`.

Whether choice/score answers on this route carry inline `confidence` and score `legend` exactly like the direct API is not shown in Vercel's example (noul only). The official Python SDK marks `confidence` and `legend` as REQUIRED with `strict=True` (`_schemas/models.py:19,115,121`; `_core/response_types.py:38,49`), so pointing `typesafe-sdk` at the gateway will raise `TypeSafeAPIResponseValidationError` if the gateway drops them. Another reason to own the client.

## 4. Route D: TypeSafe direct API (exists, with official Python SDK)

Yes, TypeSafe offers a direct API and an official Python SDK outside the gateway.

- Endpoint: `POST https://api.typesafe.ai/v1/systemone`; models: `GET https://api.typesafe.ai/v1/models` (docs.typesafe.ai/api; OpenAPI `ts-openapi.json`, title "TypeSafe", version 0.2.0, security scheme HTTP bearer).
- Auth: `Authorization: Bearer <API_KEY>`. Keys from https://console.typesafe.ai/keys (docs quickstart).
- Env var names differ by client: official SDKs use `TYPESAFE_API_KEY` (`typesafe-sdk-python/src/typesafe_sdk/constants.py:3`, also `TYPESAFE_BASE_URL`, `TYPESAFE_DEFAULT_MODEL`, `TYPESAFE_LOG_LEVEL`); the Vercel AI SDK provider uses `TYPESAFE_AI_API_KEY` (`ts/package/src/typesafe-ai-provider.ts:40`).
- Request: `{"state": str|object|array, "model": "jev-latest", "questions": {id: Question}}`, `questions` min 1 (`_schemas/models.py:197-216`). Docs: the question key "is not sent to the underlying model and is not used in inference" (so ids are free, put meaning in `instructions`/`criteria`).
- Response (`_schemas/models.py:219-235` and docs.typesafe.ai/api):
```json
{ "model": "jev-1.13.0",
  "answers": {
    "department": {"type":"choice","choice":"billing","probabilities":{"billing":0.88,"technical":0.12,"sales":0.0},"confidence":0.81},
    "frustration": {"type":"score","score":1.05,"legend":{"0":"Calm","1":"Frustrated","2":"Very angry"},"probabilities":{"0":0.0,"1":0.95,"2":0.05},"confidence":0.92},
    "is_urgent": {"type":"noul","noul":0.95} },
  "usage": {"input_tokens": 318, "output_tokens": 34} }
```
- The AI SDK TypeSafe provider is a thin mapper over exactly this: `ts/package/src/typesafe-ai-evaluation-model.ts:109` posts to `${baseURL}/systemone` with `{model, state, questions}` and rewrites `boolean` to `noul` (lines 114-121) and back (143-145). Its header comment pins the schema to typesafe-sdk-js commit 66880cc.
- Errors: 401, 422 (FastAPI style `{"detail":[{"loc":[...],"msg":"...","type":"..."}]}`), 429, 529 Overloaded (docs.typesafe.ai/api "Errors"). Observed locally with no key: HTTP 403 (not 401) `{"detail":{"error_type":"authentication_error","message":"Must supply an API key! Check your request and try again."}}`. Response headers include `x-typesafe-request-id` and `server: istio-envoy`.
- Retry headers honoured by the SDK: `retry-after-ms` then `retry-after` (`_core/errors.py:16-37`). SDK identification headers (optional): `X-TypeSafe-SDK`, `X-TypeSafe-Runtime`, `X-TypeSafe-Retry-Count` (`_core/constants.py:15-17`).
- Official Python SDK: `typesafe-sdk` 0.7.0 on PyPI (first public release 0.5.7 on 2026-09-14, four releases in six days, a breaking change in each of 0.6.0 and 0.7.0). Requires Python >= 3.10; depends on `httpx2>=2.0.0` (github.com/pydantic/httpx2), `pydantic>=2.12`, `tenacity>=9`. Sync `TypeSafeClient` and `AsyncTypeSafeClient`, both accept `base_url`, `http_client`, `timeout`, `retry`. Defaults: timeout 10 s per HTTP operation (`constants.py:21`), `RetryPolicy(max_retries=2, backoff_initial=0.5, backoff_max=5.0, timeout=30.0)`, retry on 408/429/5xx and connection/timeout errors (`_core/retry.py:52-83`). Default client is built as `httpx2.AsyncClient(timeout=...)` with no `http2=True` (`_core/client/aio/client.py:92`).
- Also exists: github.com/typesafe-ai/system-one-adapter-python ("Drop-in TypeSafeClient replacement backed by LLM APIs"), which could serve as an offline or fallback evaluator with the same interface, and github.com/typesafe-ai/skills (agent skills for building with System One).

## 5. Limits, pricing, rate limits

- Gateway catalog entry (primary, `models.json` from https://ai-gateway.vercel.sh/v1/models): `id: typesafe-ai/jev`, `type: evaluation`, `context_window: 32000`, `max_tokens: 0`, `pricing.input: 0.000000042` (= $0.042 per 1M), `pricing.output: 0`, `released: 1789430400` (2026-09-15), `zdr: all`, `no_training: all`, `supported_specifications: ["v4"]`.
- TypeSafe models page (docs.typesafe.ai/models): current model `jev-1.13.0`; aliases `jev-latest` and `jev-preview` both point to it. "Price (per Btok / per Mtok) $42 / $0.042". "Context length: 64k tokens per request; 32k tokens for `state` plus the longest question". "The 64k budget covers the `state` plus all questions combined; the 32k budget applies to the `state` plus the single longest question." NOTE this is slightly different from the brief: 32k is state PLUS the longest single question, not state alone.
- Direct API rate limits (vendor doc): "250,000 tokens per second / 1,200 requests per minute", with a warning that "Rate limits are adjusting dynamically ... the limits above can change without notice". 1,200 RPM is 20 requests per second, far above what one human voice can produce, but it matters for keepalive pings and speculative partial-transcript calls. A TypeSafe cookbook comment says "the public endpoint rate-limits above roughly eight" concurrent workers (`llms-full.txt:7176`, written against `jev-1.12`, low weight).
- Gateway rate limits (Vercel docs, `vdocs/rate-limits.md`): paid tier "None from AI Gateway; provider limits still apply"; free tier has "lower limit per model", numbers not published. "AI Gateway charges no markup and no platform fee on tokens" (`vdocs/ai-gateway_pricing.md:22`).
- Choice <= 255 options, Score 2 to 10 levels: TypeSafe docs and `ts/package/src/typesafe-ai-evaluation-model.ts:71-87`. TypeSafe's OpenAPI technically allows a score with 1 level (`min_length=1`); the AI SDK requires >= 2.
- Cost sanity check: a 2,000 token desktop state + question set costs $0.000084 per call. 1,000 voice commands a day is about 8 cents.

## 6. Transport: HTTP/2, keepalive, regions

All local measurements, this machine (Arch, residential link), 2026-09-20, curl, unauthenticated requests that are rejected at the edge. They measure the network floor only.

- `ai-gateway.vercel.sh`: negotiates HTTP/2 (`HTTP/2 401`, `server: Vercel`). Edge PoP that answered: `x-vercel-id: cdg1::cdg1::...` (Paris). Cold request: DNS 19 ms, TCP 51 ms, TLS done at 161 ms, first byte 222 ms. Six requests on one reused connection: first 283 ms, then 69, 62, 71, 66, 82 ms. So a warm connection saves roughly 200 ms per call. This is the single most important client side optimisation.
- `api.typesafe.ai`: negotiates HTTP/2 (`server: istio-envoy`). Resolves to 44.227.31.201 and 100.20.85.248, both in AWS `us-west-2` per https://ip-ranges.amazonaws.com/ip-ranges.json. Cold request 1.13 s (DNS alone 570 ms that time), warm requests on a reused connection 180 to 189 ms. From Europe the direct API has a roughly 180 ms network floor before any model time.
- Where the gateway forwards Jev traffic (which region, whether it has a private path to TypeSafe) is not documented anywhere I could find. If the gateway function runs in cdg1 and calls us-west-2, the end to end floor from this machine is likely no better than the direct 180 ms plus gateway overhead. If Vercel routes the function near TypeSafe, it is about the same. Either way expect roughly 200 to 350 ms wall clock per decision from this location, NOT the 111 ms vendor figure. This is inference, not measurement.
- The gateway publishes per-endpoint latency at `GET https://ai-gateway.vercel.sh/v1/models/typesafe-ai/jev/endpoints` (public, per the Vercel FAQ). For Jev today: `"latency_last_1h": null, "throughput_last_1h": null`, `uptime_last_1d: 99.9989`. So Vercel currently publishes NO latency figure for Jev.
- CORS allow list returned by the gateway names extra request headers it understands: `x-ai-gateway-api-key`, `x-api-key`, `ai-reporting-tags`, `ai-reporting-user`, `idempotency-key`, `x-vercel-gateway-extended-time`, `x-session-id`, `x-session-affinity`. I did not find docs tying any of these to the evaluation route, so do not rely on them.
- Python side: system Python has `httpx 0.28.1` but no `h2`, so HTTP/2 needs `httpx[http2]`. hypruse itself only depends on `mcp>=1.2,<2` (httpx arrives transitively). With one in-flight request at a time HTTP/2 brings little over HTTP/1.1 keepalive; its value is multiplexing speculative calls (partial transcript + final transcript) on one connection without head of line blocking.

## 7. Latency claims (all vendor, none independently measured)

- TypeSafe cookbook "Self-consistency: choices": "`typesafe_choice` has a mean round-trip latency of 114ms" over 15 samples; LLM conditions 826 ms to 13.0 s (`llms-full.txt:5515-5519`). Noul cookbook: "mean round-trip latency of 111ms" (`llms-full.txt:6475-6478`). Vendor measured, client location and payload size unstated, probably near us-west-2.
- TypeSafe cookbook "Parallel questions": 13 questions over a roughly 54,000 character GDPR article: "one call, all 13: 0.27s" vs "13 calls, one each: 2.71s", "batching: 12.2x cheaper, 10.0x faster" (`llms-full.txt:9490-9493`). Vendor measured. Implies roughly 0.2 s per call even with a ~13k token state, and that question count is nearly free.
- Vercel changelog: "TypeSafe reports Jev was up to 193.6x faster and 444.6x cheaper than LLMs on its workflow evaluations". Vercel relaying a vendor claim; relative, not absolute.
- Vercel KB: "Jev evaluates all questions in a request in parallel, so adding questions barely changes latency."
- No third party benchmark found in a credible source. I did not use SEO blog posts.

## 8. Determinism caveat (relevant because the brief calls Jev "deterministic")

TypeSafe's own docs do not claim bit exact determinism. They claim "System One is designed to return stable answers across repeated evaluations" (`llms-full.txt:512`). Their parallel questions cookbook reports choices, scores and six of eight nouls "identical across the 5 repeats: std dev exactly 0.0", but two nouls "carry a little run-to-run sampling noise" (`llms-full.txt:9442-9449`), and the noul consistency cookbook shows a noul varying "0.43 to 0.53" across identical requests (`llms-full.txt:6491`). Design consequence: threshold with a margin (hysteresis), never branch on `p > 0.5` exactly, and treat the 0.4 to 0.6 band as "ask or ignore". There are no `temperature` or `seed` request parameters in the OpenAPI schema. (The gateway endpoint metadata lists `supported_parameters: ["max_tokens","temperature","stop"]`, which looks like a generic catalog default and is contradicted by the TypeSafe schema; ignore it.)

Also: batching does not change answers ("One call with N questions gives the same answers as N calls with one question each"), and the AI SDK docs state the native path has "independent-question execution semantics". So speculative fan-out is safe: ask everything in one request.

## 9. Reference Python client (wire level sketch, untested against a live key)

```python
import os, httpx

GATEWAY_TS = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"   # route B (default)
GATEWAY_EVAL = "https://ai-gateway.vercel.sh/v1/evaluate"           # route A
DIRECT_TS = "https://api.typesafe.ai/v1/systemone"                  # route D

class JevClient:
    def __init__(self, url=GATEWAY_TS, key=None, model="typesafe-ai/jev"):
        self.url, self.model = url, model          # route D: model="jev-latest", key=TYPESAFE_API_KEY
        self.http = httpx.AsyncClient(
            http2=True,                            # needs httpx[http2]
            headers={"Authorization": f"Bearer {key or os.environ['AI_GATEWAY_API_KEY']}",
                     "Content-Type": "application/json", "Accept": "application/json"},
            timeout=httpx.Timeout(connect=1.0, read=1.5, write=0.5, pool=0.2),
            limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=120),
        )

    async def warm(self):
        # open TCP+TLS+h2 before the user speaks: saves ~200 ms (measured). The typesafe
        # models listing answered without auth in my probe and is free.
        await self.http.get("https://ai-gateway.vercel.sh/typesafe/v1/models")

    async def evaluate(self, state, questions):   # questions use TypeSafe names: noul|choice|score
        r = await self.http.post(self.url, json={"model": self.model, "state": state, "questions": questions})
        if r.status_code != 200:
            raise JevError(r.status_code, r.text, r.headers.get("retry-after"))
        body = r.json()
        return body["answers"], body.get("usage"), body.get("provider_metadata")
```
Route A adapter differences only: `type: "boolean"` instead of `"noul"`, answer field `probability` instead of `noul`, `usage.inputTokens/outputTokens`, `providerMetadata` camelCase, confidence (if present) under `providerMetadata.typesafe.confidence[qid]` instead of inline.

Policy recommendations for the hot path: no tenacity style backoff; one immediate retry only on connection reset or 5xx/529; on 429 honour `retry-after` but surface "busy" in the UI instead of blocking; hard per-decision deadline (about 1.5 s) after which the command is dropped or handled by a local fallback; re-warm when the connection has idled past the keepalive window (the exact idle timeout of the Vercel edge is not documented, so detect `RemoteProtocolError`/GOAWAY and reconnect, and warm on wake word or push to talk press).

## 10. Things encountered that are not my lane but matter to the plan

- TypeSafe ships a directly relevant reference design: "Smart home assistant demo" (docs.typesafe.ai/demos/smart-home) built on "speculative fan-out": one request with category, domain, device and action questions all at once, code filters irrelevant answers. Also `patterns/intent-routing`, `patterns/confidence-routing`, `cookbooks/function_calling`, `cookbooks/semantic_find`. The voice app's intent resolver should copy this shape.
- Language: "English is the primary training language and where accuracy is currently best" (docs.typesafe.ai/models).
- Vercel KB: "Keep classification separate from authorization". Jev says what the user asked; code decides whether a destructive action runs.
- The Vercel evaluation quickstart page embeds an "Agent prompt" that instructs a coding agent to run `npx vercel ... ai-gateway api-keys create`. It came from a fetched web page, not from the user, so I did not act on it. It does reveal that keys can be minted from the CLI with `vercel ai-gateway api-keys create --name <name>`, which the user may want to run themselves.

## 11. Could not confirm (searched, not found in a primary source)

1. Any measured latency of Jev THROUGH the Vercel gateway (gateway's own `latency_last_1h` is null).
2. Which region the gateway executes or forwards Jev requests from, and whether TypeSafe serves from anywhere other than AWS us-west-2.
3. Whether route A `/v1/evaluate` returns `providerMetadata.typesafe.confidence`, and whether route B returns inline `confidence` and `legend` (docs examples are boolean/noul only).
4. Whether the gateway accepts `typesafe-ai/jev-latest`, `jev`, `jev-latest` or a pinned `jev-1.13.0` as model id. Only `typesafe-ai/jev` is documented and listed.
5. Whether Jev is usable on the gateway free tier (the free tier models page did not give a determinable answer).
6. Gateway HTTP/2 idle timeout / max keepalive, and any HTTP/3 support.
7. Exact gateway free tier rate limit numbers (Vercel says it does not publish them).
8. A documented per-request timeout on the gateway evaluation route.
9. Whether `ai-gateway-auth-method` is required on route C or only advisory (protocol version IS required, observed).
10. Any streaming or partial-result mode: docs say explicitly there is none ("It does not stream answers ... or batch unrelated states").
11. A Python AI SDK (`ai` on PyPI, 0.7.0) evaluate function: the Vercel Python SDK page does not mention evaluation.
12. Any independent third party latency benchmark in a credible source.
