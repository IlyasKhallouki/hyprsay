# Research lane: gateway-stt

Date: 2026-09-21. Status: COMPLETE. No live API calls were made by this lane.

Scope: speech to text served through Vercel AI Gateway for a Hyprland voice control app. No live API calls were made by this lane. Every claim is labeled MEASURED (local), PRIMARY (vendor doc or published package source), VENDOR CLAIM, SECONDARY, or ASSUMPTION.

Reused inputs:
- Gateway catalog: `scratchpad/gw_models.json` (GET https://ai-gateway.vercel.sh/v1/models, 376 models).
- Prior extraction: `scratchpad/npm-stt/` holding `@ai-sdk/gateway@4.0.87`, `ai@7.0.107`, `@ai-sdk/provider@4.0.17`, `@ai-sdk/provider-utils@5.0.45`.

## 1. Wire protocol, from the published npm package source (PRIMARY)

Package: `@ai-sdk/gateway@4.0.87` extracted at `scratchpad/npm-stt/package/`. The package ships its TypeScript `src/`, so the snippets below are vendor source, not decompiled output.

### 1.1 Batch transcription (HTTP)

File `npm-stt/package/src/gateway-transcription-model.ts`, lines 58 to 123 and 167 to 176.

- URL: `${baseURL}/transcription-model` (line 168).
- Method: POST, JSON body via `postJsonToApi` (line 75). NOT multipart.
- Body (lines 83 to 90): `{ audio: <base64 string>, mediaType: <string>, providerOptions?: {...} }`. A `Uint8Array` is base64 encoded with `convertUint8ArrayToBase64`.
- Model selection is by HEADER, not by body field (lines 171 to 176):
  - `ai-transcription-model-specification-version: 4`
  - `ai-model-id: <creator/model>`
- Response schema `gatewayTranscriptionResponseSchema` (lines 422 to 439): `{ text: string, segments?: [{text, startSecond, endSecond}], language?: string|null, durationInSeconds?: number|null, warnings?: [...], providerMetadata?: {...} }`.

### 1.2 Streaming transcription (WebSocket)

Same file, `doStream` lines 125 to 165, `toGatewayTranscriptionUrl` lines 184 to 191, `createGatewayTranscriptionStream` lines 219 to 396.

- URL: base URL with `http` replaced by `ws`, path `/transcription-model`, model id in query `?ai-model-id=<id>` (line 188 to 189). Source comment line 181 to 182 names `openai/gpt-realtime-whisper` as the example qualified id.
- Auth rides the `Sec-WebSocket-Protocol` handshake, because a browser WebSocket cannot set headers. File `npm-stt/package/src/gateway-realtime-auth.ts`:
  - marker subprotocol `ai-gateway-transcription.v1` (line 34)
  - auth subprotocol `ai-gateway-auth.<token>` (line 37), token sent as is
  - optional team subprotocol `ai-gateway-team.<base64url(teamIdOrSlug)>` (line 40, encoder lines 136 to 146)
- Framing (lines 335 to 338, 280 to 320, 341 to 366):
  1. client sends one JSON text START frame: `{type, inputAudioFormat, providerOptions?, includeRawChunks?}`
  2. client sends raw audio as BINARY frames, each at most 64 KiB (`MAX_AUDIO_FRAME_BYTES = 64 * 1024`, line 217), with backpressure between frames
  3. client sends a JSON text AUDIO DONE frame `{type: <audio done type>}`
  4. server sends JSON text frames that are serialized stream parts; a part with `type: "finish"` ends the stream and the client closes with code 1000; a part with `type: "error"` is terminal and the server then closes non-1000
- Server error part types mapped to statuses (lines 471 to 479): authentication_error 401, failed_dependency 424, forbidden 403, internal_server_error 500, invalid_request_error 400, model_not_found 404, rate_limit_exceeded 429.

### 1.3 Realtime type (WebSocket)

File `npm-stt/package/src/gateway-realtime-model.ts`.

- URL (lines 114 to 118): base URL upgraded to `ws(s)`, path `/realtime-model`, query `?ai-model-id=<id>`.
- Subprotocols (file `gateway-realtime-auth.ts` line 28, 37, 40): `ai-gateway-realtime.v1`, `ai-gateway-auth.<token>`, optional `ai-gateway-team.<base64url>`.
- Token: the SDK design is a short lived single use client secret with prefix `vcst_`, minted server side at `/v1/realtime/client-secrets` (comment lines 16 to 19 and 50 to 58). Whether the long lived API key is also accepted directly in `ai-gateway-auth.` for the realtime route is checked in section 1.4.
- Messages are the NORMALIZED AI SDK realtime protocol, not the upstream OpenAI or Gemini event names. Source comment lines 30 to 34: "the client speaks the normalized AI SDK realtime protocol and the Gateway translates to and from the upstream provider server-side". `parseServerEvent` and `serializeClientEvent` are identity functions (lines 87 to 96).

### 1.4 Base URL, auth headers, client secrets (PRIMARY)

File `npm-stt/package/src/gateway-provider.ts`:

- Default base URL (lines 315 to 317): `https://ai-gateway.vercel.sh/v4/ai`. So the concrete endpoints are:
  - batch: `POST https://ai-gateway.vercel.sh/v4/ai/transcription-model`
  - streaming: `wss://ai-gateway.vercel.sh/v4/ai/transcription-model?ai-model-id=<urlencoded id>`
  - realtime: `wss://ai-gateway.vercel.sh/v4/ai/realtime-model?ai-model-id=<urlencoded id>`
- HTTP auth headers (lines 319 to 334): `Authorization: Bearer <token>`, `ai-gateway-protocol-version: 0.0.1`, `ai-gateway-auth-method: api-key` (or `oidc`), optional `x-vercel-ai-gateway-team: <team>`, plus a user agent suffix `ai-sdk/gateway/<version>`. Header names from `src/gateway-headers.ts` lines 1 to 3.
- Client secret mint (lines 362 to 407): `POST https://ai-gateway.vercel.sh/v1/realtime/client-secrets` (resolved against the ORIGIN, not under `/v4/ai`), same auth headers, JSON body `{ model, routeKind?: "transcription", expiresIn?: <seconds> }`, response `{ token: string, expiresAt?: <epoch seconds> }` (schema lines 297 to 301). Tokens have prefix `vcst_` and are described as single use and short lived. `routeKind` is omitted for realtime and set to `"transcription"` for the streaming transcription surface (lines 368 to 370, 629 to 632).
- IMPORTANT for a Python daemon: in the SERVER SIDE streaming transcription path the SDK puts the long lived bearer token itself into the subprotocol. `getProtocolsFromHeaders` (transcription-model.ts lines 200 to 214) slices `Bearer ` off the `Authorization` header and emits `ai-gateway-auth.<that token>`. So for `/transcription-model` the API key is accepted directly in the subprotocol and no mint round trip is needed. It also passes the full header set to `connectToWebSocket` for WebSocket clients that can set headers.
- For `/realtime-model` the SDK only ever connects with a minted `vcst_` token (`getWebSocketConfig`). The server side comment in `gateway-realtime-auth.ts` lines 85 to 88 says the upgrade handler "turns this into an `Authorization: Bearer <token>` before its normal auth path", which SUGGESTS an API key would also be accepted, but that is INFERENCE, not shown. Safe plan: mint first, which costs one extra HTTPS round trip per session (not per utterance if the socket is kept open).

### 1.5 Streaming transcription envelope v1 (PRIMARY)

File `npm-stt/pu/package/src/transcription-stream-envelope.ts` (`@ai-sdk/provider-utils@5.0.45`), header comment lines 8 to 44 and constants lines 47 to 52.

- Start frame type string: `transcription-stream.start`
- Audio done frame type string: `transcription-stream.audio-done`
- Start frame: `{"type":"transcription-stream.start","inputAudioFormat":{"type":"audio/pcm","rate":16000}}` (the `audio/pcm` + `rate: 16000` example is from the source comment at line 60). Optional `providerOptions` (object keyed by provider) and `includeRawChunks` (boolean).
- Rule 3: "a plain close without it is an abort", so the client MUST send audio-done to get a final.
- Rule 7: "the AI Gateway rejects frames over 256 KiB; clients should split audio into frames of at most 64 KiB".
- Rule 8: connection establishment is transport specific.
- "server policy (accepted audio formats, required `rate`, size limits) layers on top": the accepted formats per model are NOT in the package.

Server to client part types, file `npm-stt/prov/package/src/transcription-model/v4/transcription-model-v4-stream-part.ts`:

| part type | key fields | meaning |
|---|---|---|
| `stream-start` | `warnings[]` | session accepted |
| `transcript-delta` | `delta`, `id?` | append only text |
| `transcript-partial` | `text`, `id?`, `startSecond?`, `durationInSeconds?`, `channelIndex?` | non final, may be revised |
| `transcript-final` | `text`, `id?`, `startSecond?`, `endSecond?` | final for one utterance or segment |
| `response-metadata` | `timestamp?` (ISO 8601), `modelId?`, `headers?` | |
| `finish` | `text`, `segments[]`, `language?`, `durationInSeconds?` | terminal, server closes 1000 |
| `raw` | `rawValue` | only if `includeRawChunks` |
| `error` | `error: {message, type}` | terminal, server closes non 1000 |

All of `doStream`, the envelope and the part types are exported under `Experimental_` / `experimental_` names, so this surface can change without a major version.

## 2. Catalog ground truth: which models stream, and what the prices mean

Source for this whole section: `scratchpad/gw_models.json` (PRIMARY, the gateway's own `/v1/models` response). Computed columns are arithmetic on those values.

### 2.1 Streaming vs batch, per the catalog `tags` field

Exactly three of 376 models carry the tag `websocket-transcription`. The SDK type `GatewayTranscriptionModelId` (`npm-stt/package/src/gateway-transcription-model-settings.ts`) lists the same eight ids plus `fish-audio/transcribe-1-free`, which is NOT in the saved catalog.

| model id | catalog tags | released (UTC) | deprecated_at | gateway streaming? |
|---|---|---|---|---|
| `spacexai/grok-stt` | `websocket-transcription` | 2026-03-16 | none | YES. Description: "batch and streaming modes" |
| `openai/gpt-realtime-whisper` | `websocket-realtime`, `websocket-transcription` | 2026-05-07 | none | YES. Description: "low-latency transcript deltas from live audio" |
| `google/gemini-3.5-transcribe-live` | `websocket-transcription` | 2026-08-26 | none | YES. Description: "Live transcription by Google" |
| `google/gemini-3.5-transcribe` | none | 2026-08-26 | none | no tag, batch over the gateway |
| `openai/gpt-4o-transcribe` | none | 2024-03-13 | 2027-02-26 | no tag, batch over the gateway |
| `openai/gpt-4o-mini-transcribe` | none | 2024-03-13 | 2027-02-26 | no tag, batch over the gateway |
| `openai/whisper-1` | none | 2022-09-21 | 2027-02-26 | batch only (also batch only upstream) |
| `fish-audio/transcribe-1` | none | 2026-03-01 | none | batch. Description: "Send an audio file, get back the transcript" |

Caveats, stated honestly:
- The meaning of the `websocket-transcription` tag is INFERRED from its name, from the three descriptions, and from the SDK comment that uses `openai/gpt-realtime-whisper` as its streaming URL example. A Vercel doc sentence defining the tag is looked for in section 3.
- Upstream, OpenAI's `gpt-4o-transcribe` and `gpt-4o-mini-transcribe` can stream text deltas over SSE (`stream=true`) for an already uploaded file. The gateway batch route returns one JSON document (`createJsonResponseHandler`), so that upstream ability is not reachable through the gateway's `/transcription-model` POST. Streaming deltas of a completed upload would not help a voice command anyway, since the upload happens after speech ends.
- The tag `websocket-realtime` is NOT a reliable marker of audio realtime: it sits on 24 models, 20 of which are `type: language` OpenAI GPT models, and only 3 of the 9 `type: realtime` models carry it. Do not use it to decide anything.

### 2.2 Pricing units explained

Catalog pricing values are decimal strings in USD. Three different unit systems appear among the eight models.

(a) Per second of audio: field `transcription_duration_cost_per_second`.

| model | USD per second | USD per minute | USD per hour | 2 s command | 1000 commands of 2 s |
|---|---|---|---|---|---|
| `spacexai/grok-stt` | 0.000028 | 0.00168 | 0.1008 | 0.000056 | 0.056 |
| `openai/whisper-1` | 0.0001 | 0.006 | 0.36 | 0.0002 | 0.20 |
| `fish-audio/transcribe-1` | 0.0001 | 0.006 | 0.36 | 0.0002 | 0.20 |
| `google/gemini-3.5-transcribe-live` | 0.00015 | 0.009 | 0.54 | 0.0003 | 0.30 |
| `openai/gpt-realtime-whisper` | 0.000284 | 0.01704 | 1.0224 | 0.000568 | 0.568 |

Cross check: `whisper-1` at 0.0001 per second is 0.006 per minute, which equals OpenAI's long standing public list price for Whisper. That confirms the unit is USD per second of audio.

(b) The odd `input: "0.0000000001"` values. On the per second models the `input` field is 1e-10 (fish, gemini live, whisper-1), 2e-10 (gpt-realtime-whisper) or "0" (grok-stt). Read as USD per token that is 0.0001 USD per million tokens, which is economically nothing. INFERENCE: it is a placeholder so that catalog consumers that require a numeric per token `input` price do not break; the real meter is the per second field. No Vercel doc sentence explaining the placeholder was found. It is NOT a per token price you will be billed.

(c) Per token: fields `input`, `output`, `audio_input_token_cost`, all USD per single token.

| model | text in per 1M | audio in per 1M | out per 1M |
|---|---|---|---|
| `openai/gpt-4o-mini-transcribe` | 1.25 | 1.25 | 5.00 |
| `google/gemini-3.5-transcribe` | 2.00 | 2.00 | 12.00 |
| `openai/gpt-4o-transcribe` | 2.50 | 2.50 | 10.00 |

How many audio tokens a second of speech costs is set by each provider and is not in the catalog. Not converted to per minute here because that would require a number I cannot source from the catalog.

(d) Realtime type models use yet other meters: `audio_input_token_cost` / `audio_output_token_cost` per token (OpenAI gpt-realtime family, Gemini 3.8 Live), `realtime_session_duration_cost_per_second` (openai/gpt-live-1 at 0.0008334 per second, which is 3.00 per hour of open session; Grok voice at 0.000834 and 0.001334 per second) and `realtime_client_message_cost` 0.004 per client message (Grok voice). For an always listening daemon a per second SESSION meter is dangerous: 8 hours of open `gpt-live-1` socket is 24 USD whether or not anyone speaks.

Billing minimums or rounding increments (for example whether a 1.2 s clip bills as 1.2 s or rounds up): NOT in the catalog.

Bottom line on cost: at voice command volumes every transcription option is effectively free (0.06 to 0.57 USD per thousand commands). Cost does not discriminate. Latency does.

## 3. Vercel documentation confirms the package reading (PRIMARY)

Source: https://vercel.com/docs/ai-gateway/modalities/speech-to-text (page front matter says `last_updated: 2026-09-08`). Fetched 2026-09-21. Note that https://vercel.com/docs/ai-gateway/capabilities/transcription returns HTTP 404; the right path is `/modalities/speech-to-text`.

Verbatim from that page:

- "Speech to text is in beta and access is rolling out gradually. Transcription models may not appear in the model catalog yet for your team." RISK: beta, gated rollout. The main loop must confirm the key's team actually has access before designing around it.
- "These audio operations use dedicated AI Gateway endpoints. They are separate from Chat Completions, Messages, and Responses." So there is NO OpenAI compatible `/v1/audio/transcriptions` route documented. Not found: any such route.
- REST, quoted: "Send a `POST` request with the model in the `ai-model-id` header and the audio as a base64-encoded string". The documented curl:

```bash
curl -X POST https://ai-gateway.vercel.sh/v4/ai/transcription-model \
  -H "Authorization: Bearer $AI_GATEWAY_API_KEY" \
  -H "ai-gateway-protocol-version: 0.0.1" \
  -H "ai-transcription-model-specification-version: 4" \
  -H "ai-model-id: openai/whisper-1" \
  -H "Content-Type: application/json" \
  -d "{ \"audio\": \"$(base64 -i meeting.mp3)\", \"mediaType\": \"audio/mpeg\" }"
```

  Documented response: `{"text": "...", "segments": [], "language": "en", "durationInSeconds": 4.2, "warnings": []}`. The page also prints a stdlib `urllib.request` Python version of the same call, so plain Python with no Node is an officially documented path for batch.
- Limitations, quoted:
  - "Audio for the REST API is sent base64-encoded in a JSON body. Multipart file uploads are not supported."
  - "The REST API returns the full transcript in a single JSON response. To stream results, use `experimental_streamTranscribe` with the AI SDK."
  - "Recorded audio and streaming support different model sets."
- Streaming, quoted: "AI Gateway connects to the model over a WebSocket and streams results back as the provider produces them." and "Streaming transcription is available for models such as `openai/gpt-realtime-whisper`, `spacexai/grok-stt`, and `google/gemini-3.5-transcribe-live`. To find models that support it, filter the AI Gateway Models page by WebSockets." The models page filter is `?modality=audio:transcription&features=websockets`, which is what the catalog tag `websocket-transcription` surfaces. This CONFIRMS the section 2.1 inference: the three tagged models are the streaming set, the other five are batch only through the gateway.
- The documented streaming example uses `inputAudioFormat: { type: 'audio/pcm', rate: 24000 }` with `openai/gpt-realtime-whisper`. 24 kHz PCM is therefore the one format that is documented to work. Whether 16 kHz is accepted per model: not found.
- Client secret, quoted: "The token is single use, expires after 60 seconds by default (300 seconds maximum), and only opens streaming transcription connections for the model it was minted for". For a local daemon that holds the API key this mint step is unnecessary (section 1.4).
- The documented raw WebSocket protocol for streaming is NOT on this page. It exists only as SDK source (section 1.2 and 1.5). A Python client must be written from the source, which is experimental.
- SDK minimums, quoted: "`ai` 7.0.31 and `@ai-sdk/gateway` 4.0.23 or later".
- There is a Python AI SDK beta (`import ai`, `ai.ops.transcribe(ai.get_model('openai/whisper-1'), bytes)`, docs at https://ai-python.dev). Whether it implements STREAMING transcription is checked in section 4.

Changelog: https://vercel.com/changelog/ai-gateway-now-supports-streaming-transcription dated 2026-07-22 announces the streaming surface, same `experimental_streamTranscribe` example, and names `xai/grok-stt` as an alternative. The catalog and the newer doc page use `spacexai/grok-stt`, so the creator slug was renamed after July. Use the catalog id.

## 4. Realtime type: what Vercel documents (PRIMARY)

Source: https://vercel.com/docs/ai-gateway/modalities/realtime (front matter `last_updated: 2026-09-15`). Fetched 2026-09-21.

- Documented Node flow (no browser): mint with `gateway.experimental_realtime.getToken({model})`, then `new WebSocket(config.url, config.protocols)` where `getWebSocketConfig` supplies URL and subprotocols. This matches section 1.3. There is NO documented raw or Python example for realtime. A Python client must replicate: POST `/v1/realtime/client-secrets`, then open `wss://ai-gateway.vercel.sh/v4/ai/realtime-model?ai-model-id=...` with subprotocols `ai-gateway-realtime.v1` and `ai-gateway-auth.<vcst_ token>`.
- Message framing: JSON text frames in the normalized AI SDK vocabulary. Identifiers seen verbatim in the doc: client events `conversation-item-create` (with `item: {type: 'text-message', role, text}`), `response-create`; server events `audio-transcript-delta` (field `delta`), `audio-delta` ("carries base64 PCM16 audio chunks"). Session config keys: `voice`, `turnDetection` (for example `{ type: 'server-vad' }`), `instructions`, `tools`. The full event list is in `npm-stt/prov/package/src/realtime-model/v4/realtime-model-v4-client-event.ts` and `...-server-event.ts` (see section 4.1).
- Quoted warning: "Before switching models, check that the model supports the realtime WebSocket endpoint. A successful token request does not guarantee that a model accepts a WebSocket connection."
- `openai/gpt-live-1` is a special case, quoted: "GPT-Live uses a separate WebSocket endpoint, continuous audio, client-managed delegation, and duration-based billing. It does not use the `gateway.experimental_realtime` examples on this page." Its guide is at /docs/ai-gateway/modalities/realtime/gpt-live.
- Session limits table, quoted values: maximum session duration 25 minutes; idle timeout 5 minutes ("The session closes if nothing is sent or received"); first client message 30 seconds; maximum message size 256 KB. Plus a per team concurrent session limit (number not stated).

Design consequence: a realtime socket CANNOT be held open all day as a warm path. It dies after 5 idle minutes and after 25 minutes regardless. A voice command daemon would have to reconnect (TLS + upgrade + mint) on most utterances or send keepalive audio and pay for it. Whether the same limits apply to the `/transcription-model` WebSocket is NOT stated on either page (not found).

Why the realtime type is the wrong tool here anyway: these are speech to speech conversational models. They bill audio input tokens at 32 USD per 1M (gpt-realtime family) or per second of open session, they generate a spoken reply the app does not want, and the user transcript arrives as a side channel. The app needs text for Jev, not a conversation. Use the `transcription` type.

## 5. Per model streaming behavior and tuning knobs (PRIMARY: published provider package source)

Packages pulled with `npm pack` on 2026-09-21 into `scratchpad/npm-stt/provs/`: `@ai-sdk/openai@4.0.71`, `@ai-sdk/xai@5.0.4`, `@ai-sdk/google@4.0.76`. These are the DIRECT provider implementations. ASSUMPTION (reasonable, not proven): the gateway server runs the same provider code behind `/transcription-model`, and forwards `providerOptions` from the start frame verbatim (the envelope comment says "Provider-specific options, passed through verbatim"). The main loop should verify each knob live.

AI SDK docs (https://ai-sdk.dev/docs/ai-sdk-core/transcription, streaming section) list stream part types `transcript-delta`, `transcript-partial`, `transcript-final` and give examples `{ type: 'audio/pcm', rate: 24000 }` and `{ type: 'audio/pcm', rate: 16000 }`. They state no latency numbers.

### 5.1 `spacexai/grok-stt` (xAI)

File `provs/xai/package/src/xai-transcription-model-options.ts`:
- `streaming.interimResults: boolean` "Emit interim transcript results while speech is being processed."
- `streaming.endpointing: int 0..5000` "Silence duration in milliseconds before an utterance-final event."
- `streaming.smartTurn: 0..1`, `streaming.smartTurnTimeout: 1..5000 ms`
- `keyterm: string | string[]` "Terms to bias transcription toward." This matters for a command grammar: app names, workspace numbers, "hyprland", "kitty" can be boosted.
- `language`, `format` (inverse text normalization), `fillerWords`
- `audioFormat: pcm | mulaw | alaw`, `sampleRate: 8000 | 16000 | 22050 | 24000 | 44100 | 48000`. So 16 kHz PCM is accepted upstream.

File `provs/xai/package/src/xai-transcription-model.ts`:
- Upstream events: `transcript.created` (then audio starts flowing), `transcript.partial` with flags `is_final` / `speech_final`, `transcript.done`, `error`. Lines 377 to 410: only `is_final && speech_final` becomes `transcript-final`; everything else is surfaced as `transcript-partial` with full replaced text. So grok-stt yields REVISABLE PARTIALS, not append only deltas.
- End of audio: client sends `{type:'audio.done'}` (line 345), server answers `transcript.done`. For push to talk this means: on key release send audio-done and the final arrives without waiting for a silence endpointing timer.
- Note line 373: the xAI provider waits for `transcript.created` BEFORE sending any audio. That is one extra upstream round trip at session start, hidden inside the gateway.

### 5.2 `openai/gpt-realtime-whisper`

File `provs/openai/package/src/transcription/openai-transcription-model-options.ts`:
- `streaming.delay: 'minimal' | 'low' | 'medium' | 'high' | 'xhigh'` "Latency/accuracy tradeoff for realtime transcription."
- `language`
- Streaming mode warns `unsupported` for `include`, `prompt`, `temperature`, `timestampGranularities`. So NO prompt or vocabulary biasing in streaming mode.

File `provs/openai/package/src/transcription/openai-transcription-model.ts`:
- Upstream path `/realtime?intent=transcription` (line 407), session type `transcription`, `turn_detection: null` (line 609), audio via `input_audio_buffer.append`, end via `input_audio_buffer.commit` (line 510).
- Upstream events mapped: `conversation.item.input_audio_transcription.delta` to `transcript-delta` (append only), `...completed` to the final.
- Because turn detection is null and the SDK commits only when the audio stream ends, the FINAL arrives only after the client says audio-done. Deltas arrive during speech.

### 5.3 `google/gemini-3.5-transcribe-live`

File `provs/google/package/src/transcription/google-transcription-model.ts`:
- Upstream is the Gemini Live `BidiGenerateContent` WebSocket (line 39), audio sent as `realtimeInput` with `mimeType: audio/pcm;rate=16000`, end via `{realtimeInput:{audioStreamEnd:true}}` (line 471).
- HARD format constraint, lines 670 to 683: "The Gemini Live transcription API only supports 16kHz 16-bit PCM input audio." Anything else throws.
- Emits `transcript-delta` and `transcript-partial` from `inputTranscription` fragments; source comments at lines 356 to 381 mention that trailing transcripts can arrive after `audioStreamEnd` and that a finished `inputTranscription` segment is sometimes never delivered, so the SDK synthesizes the final. Read as: finalization on this model is the least crisp of the three.
- Options (`google-transcription-model-options.ts`): `languageCodes`, `customVocabulary` (biasing, useful for commands), `wordTimestamp`, `diarization`, `mode: SMART | VERBATIM`.

### 5.4 Practical audio format choice

16 kHz mono 16 bit PCM is accepted by grok-stt (enum includes 16000) and is the ONLY format for Gemini live. Vercel's documented example for gpt-realtime-whisper uses 24000. 16 kHz PCM is 32 KB per second, so a 2 s command is 64 KB raw, 1 to 3 binary frames of at most 64 KiB, or about 85 KB base64 for the batch route.

## 6. Latency evidence for short clips

### 6.1 What Vercel states: nothing numeric

Neither https://vercel.com/docs/ai-gateway/modalities/speech-to-text nor the 2026-07-22 changelog nor https://ai-sdk.dev/docs/ai-sdk-core/transcription gives any latency figure. The changelog says only "keeping latency low". NOT FOUND: any Vercel published number for gateway transcription latency, gateway added overhead on the WebSocket route, or time to first partial.

### 6.2 Independent measurement: Pipecat STT benchmark (SECONDARY, independent, reproducible)

Source: https://github.com/pipecat-ai/stt-benchmark README results table, re-fetched from `raw.githubusercontent.com/.../main/README.md` on 2026-09-21 (HTTP 200, 14646 bytes) and confirmed byte identical to the copy a prior run saved at `scratchpad/npm-stt/pipecat-readme.md`. Method per the repo's `docs/measuring-ttfs.md`: TTFS = "final transcript receipt time minus speech end time", where speech end is the VAD stop event minus the VAD stop delay (default 0.2 s). 1000 samples from `pipecat-ai/smart-turn-data-v3.1-train`, which are short conversational turns, a fair proxy for 1 to 3 s commands.

Rows for models that the gateway serves:

| model (direct to provider, NOT via gateway) | TTFS median | P95 | P99 | pooled semantic WER |
|---|---|---|---|---|
| Google `gemini-3.5-transcribe-live` | 458 ms | 532 ms | 599 ms | 2.24% |
| OpenAI `gpt-4o-transcribe` | 637 ms | 965 ms | 1655 ms | 3.06% |
| OpenAI `gpt-realtime-whisper` | 740 ms | 878 ms | 1080 ms | 2.73% |

For scale, the fastest services in the same table (none served by the gateway): NVIDIA Nemotron 3.0 ASR 221 ms, Deepgram nova-3 247 ms, Soniox stt-rt-v4 249 ms.

Caveats that matter:
- These runs hit each provider DIRECTLY. Through the gateway you add the gateway hop (this machine to Paris cdg1 at about 65 ms RTT, then gateway to provider, location unknown). So treat them as LOWER bounds for the gateway path.
- The benchmark's network location is not stated in the README (grep for location, region, network found nothing). Unknown, likely United States. Provider to client RTT is baked into the numbers.
- `gpt-realtime-whisper` was run with default settings (`services.py` lines 430 to 439: only `model` and `language`). No `delay` was set, so the 740 ms is NOT the `delay: 'minimal'` figure. How much `minimal` buys: NOT FOUND in any measured source.
- xAI `grok-stt`: the harness has a `create_xai()` factory (`services.py` line 546) but there is NO xAI row in the published table. NOT FOUND: any independent TTFS number for grok-stt.
- `whisper-1`, `gpt-4o-mini-transcribe`, `fish-audio/transcribe-1`, `gemini-3.5-transcribe` (non live): no rows. NOT FOUND.

### 6.3 Vendor statements (VENDOR CLAIM, none numeric)

- xAI, https://docs.x.ai/developers/model-capabilities/audio/speech-to-text: no numeric latency stated. Useful operational facts, quoted: "Send 100 ms audio chunks" (3,200 bytes at 16 kHz PCM16); "Wait for `transcript.created` before sending audio, the server needs to initialize its ASR backend"; `interim_results` "emit partial transcripts `is_final=false` every ~500 ms"; `endpointing` default `400`, range 0 to 5000. So grok-stt partials are COARSE: about one every 500 ms, which is 2 or 3 partials across a whole 1.5 s command.
- xAI launch post https://x.ai/news/grok-stt-and-tts-apis dated 2026-04-17: the fetched page says "Generate transcripts from large audio files in milliseconds via our REST API" and calls the WebSocket API its "lowest latency" option; no millisecond figure is given. (The phrase "sub-second latency" showed up only in a search engine summary of xAI pages and was NOT verified on any page fetched this session, so it is not relied on.) WER claims are self reported (6.9% overall vs ElevenLabs 9.0%, Deepgram 11.0%, AssemblyAI 12.9%). Pricing claim: "$0.10 per hour for batch and $0.20 per hour for streaming". NOTE the gateway catalog lists a single rate of 0.000028 per second, which is 0.1008 per hour, the BATCH rate. Whether the gateway bills streaming at 0.20 per hour is NOT in the catalog (not found).
- OpenAI, https://developers.openai.com/api/docs/guides/realtime-transcription: no numeric latency stated. Quoted: "The exact delay in milliseconds can vary by model configuration, so benchmark with representative audio instead of assuming a fixed timing per level." Delay levels: `minimal` "for the most latency-sensitive interactions", `low`, `medium`, `high`, `xhigh`. Example format is 24 kHz PCM.
- Google: no latency statement was fetched for `gemini-3.5-transcribe-live`. Not searched further, to stay economical; the Pipecat number above is better evidence than any vendor sentence would be.

### 6.4 Batch models on short clips

NOT FOUND: any independent time to final for a 1 to 3 s clip sent as a one shot upload to `whisper-1`, `gpt-4o-mini-transcribe`, `gpt-4o-transcribe` (batch mode), `gemini-3.5-transcribe`, `fish-audio/transcribe-1`, or grok-stt batch. Correction to a tempting misreading: Pipecat's `gpt-4o-transcribe` row (637 ms median) is driven through `OpenAIRealtimeSTTService` (`services.py` lines 406 to 415), that is the Realtime API, not a file upload, and that realtime mode of `gpt-4o-transcribe` is not what the gateway exposes (the gateway lists it without the WebSockets tag). OpenAI community forum thread titles surfaced by search ("Whisper latency: 4 words sentences take over 3 seconds", "Whisper API Latency is just too high!") are anecdotes, not measurements, and were not opened.

## 7. Honest comparison: gateway cloud path vs local streaming

### 7.1 Inputs and their labels

| input | value | label |
|---|---|---|
| RTT this machine to gateway edge (Paris cdg1), warm | about 65 ms | MEASURED by main loop |
| Jev decision, warm connection, 1 to 5 questions | median 321 ms (266 to 348, n=8), one 6.9 s outlier and one HTTP 503 in 13 calls | MEASURED by main loop |
| Local Moonshine streaming, tiny arch, push to talk: `stop()` to final | median 234 ms (186 to 463, n=8) at update 0.2 s; median 243 ms (61 to 361) at update 0.5 s | MEASURED by another lane on this machine (`scratchpad/bench_tiny.json`, `bench_tiny05.json`), synthetic TTS audio, machine under loadavg 11 to 16, so latency is pessimistic and accuracy optimistic |
| Same, VAD endpoint path (speech end to final) | median 780 to 943 ms | same source. Most of that is the VAD silence window, which a cloud VAD path pays too |
| Same, first partial after audio START | median 431 ms (update 0.2 s) | same source |
| Local Moonshine "small" arch, push to talk final | median 1036 ms (606 to 1315) | same source, `bench_small.json`. Too slow on the i5-8350U |
| Local CPU cost while transcribing | 83% to 119% of one core, RSS 222 to 484 MB | same source |
| Best gateway served model, direct to provider TTFS | 458 ms median, 532 P95, 599 P99 (`gemini-3.5-transcribe-live`) | SECONDARY independent (Pipecat), not via gateway, location unknown |
| `gpt-realtime-whisper` direct TTFS, default delay | 740 ms median, 878 P95, 1080 P99 | same |
| `grok-stt` TTFS | unknown | NOT FOUND |
| Gateway added overhead on the STT routes | unknown | NOT FOUND |
| `net.ipv4.tcp_slow_start_after_idle` on this machine | 1, congestion control cubic | MEASURED (sysctl, this session) |

### 7.2 Batch upload path, after the user stops speaking

Payload arithmetic (computed): 2 s of 16 kHz PCM16 mono is 64,000 bytes, 64,044 as WAV, 85,392 bytes after base64, which the gateway REQUIRES (no multipart). 1 s is 42.7 KB, 3 s is 128 KB.

Because this machine has `tcp_slow_start_after_idle = 1`, a kept alive connection that sat idle between commands drops back to the initial congestion window (10 segments, about 14.5 KB). An 85 KB body then needs 3 flights (14.5 + 29 + 58 KB), so about 2 extra RTTs on top of the request RTT. ASSUMPTION based on standard Linux TCP behavior plus the measured sysctl; not packet traced.

Estimated time from end of speech to text, batch: 65 ms request RTT + about 130 ms window ramp + gateway to provider hop (unknown) + provider inference on the clip (unknown, no independent number) + response. Honest range: 0.5 to 1.5 s, with the upper half plausible for LLM based transcribers. ASSUMPTION. Compressing to Opus (2 s at 24 kbit/s is about 6 KB, fits in one flight) would remove the ramp, but which `mediaType` values each gateway model accepts is NOT documented (the doc example uses `audio/mpeg`), and Opus encode adds a few ms.

Nothing can overlap with speech on this path. Every millisecond is paid after the user finished.

### 7.3 Gateway streaming path

Audio goes up in 100 ms chunks (3.2 KB) while the user speaks, so there is no upload cost at the end. After push to talk release the client sends `transcription-stream.audio-done` and waits for `finish`.

- Connection setup from cold is about 3 RTT (TCP, TLS 1.3, HTTP upgrade), so about 200 ms here, plus the gateway's own upstream WebSocket setup to the provider, plus for grok-stt the wait for `transcript.created`. With push to talk this overlaps the first syllables: buffer mic audio locally from key press, flush when the socket is ready. It only hurts if setup exceeds the command length (shortest test command is 0.85 s). ASSUMPTION for the upstream part.
- Keeping the socket open between commands is NOT a safe plan: realtime sessions are documented at 5 min idle and 25 min max; the transcription socket's limits are undocumented. Open per utterance.
- Time from end of speech to final: the best independent direct number is 458 ms median (Gemini live). Through the gateway add one proxy hop. Estimate 0.5 to 0.9 s for Gemini live or whisper realtime; grok-stt unknown. ASSUMPTION resting on a SECONDARY measurement.
- Partials: they do arrive during speech, but later than local ones: model emission delay plus network. grok-stt emits about every 500 ms by its own docs.

### 7.4 End to end, speech end to action (add Jev 321 ms median)

| path | STT after speech end | + Jev | total | basis |
|---|---|---|---|---|
| Local Moonshine tiny, push to talk | about 235 ms | 321 ms | about 0.56 s | both MEASURED on this machine |
| Local tiny with speculative Jev on a stable partial | about 235 ms | overlapped | about 0.25 s when the partial equals the final | MEASURED parts, ASSUMPTION on hit rate |
| Gateway streaming, best case (Gemini live) | 0.5 to 0.9 s | 321 ms | 0.8 to 1.2 s | SECONDARY + ASSUMPTION |
| Gateway streaming, `gpt-realtime-whisper` default delay | 0.75 to 1.1 s | 321 ms | 1.1 to 1.4 s | SECONDARY + ASSUMPTION |
| Gateway batch upload | 0.5 to 1.5 s | 321 ms | 0.8 to 1.8 s | ASSUMPTION |

Verdict on speed: a gateway served model cannot beat the local tiny streaming model on time to final for 1 to 3 s commands, on the evidence available. The floor for ANY cloud path from this machine is one RTT (65 ms) plus a proxy hop plus provider finalization, and the best independently measured provider finalization alone (458 ms) is already about twice the local total (235 ms). Speculative Jev calls widen the gap further, since local partials lead cloud partials.

Where the cloud DOES win, stated fairly:
- Against a LARGER local model. If tiny's accuracy on real microphone audio (not the synthetic TTS used in the local bench) is not good enough and the alternative is the local "small" arch at about 1.0 s push to talk and 1.7 s VAD, then gateway streaming at 0.5 to 0.9 s is faster AND more accurate.
- Accuracy on hard audio: accents, noise, non English, rare app names. Both grok-stt (`keyterm`) and Gemini live (`customVocabulary`) accept vocabulary biasing.
- CPU: local streaming costs about one full core of a 15 W laptop CPU while the user speaks; the cloud path costs almost nothing locally.

Reliability note: the main loop already saw 1 HTTP 503 and one 6.9 s outlier in 13 Jev calls on this gateway. Gateway speech to text is documented as beta with gated rollout. Putting a second gateway call in series on the hot path multiplies tail risk; local STT does not.

## 8. Plain Python client sketches (UNTESTED, derived from sections 1 and 3; no live call was made)

Python `websockets` is NOT installed on this machine (checked: `ModuleNotFoundError`). It would be a new dependency for the streaming path. The batch path needs only the stdlib or `httpx`.

Batch, documented by Vercel almost verbatim:

```python
import base64, json, urllib.request
def transcribe(wav_bytes: bytes, key: str, model="spacexai/grok-stt") -> dict:
    req = urllib.request.Request(
        "https://ai-gateway.vercel.sh/v4/ai/transcription-model",
        data=json.dumps({"audio": base64.b64encode(wav_bytes).decode(),
                         "mediaType": "audio/wav"}).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "ai-gateway-protocol-version": "0.0.1",
                 "ai-transcription-model-specification-version": "4",
                 "ai-model-id": model,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)   # {text, segments, language, durationInSeconds, warnings}
```

`audio/wav` as `mediaType` is an ASSUMPTION; the documented example uses `audio/mpeg`. For real use keep one `httpx.Client(http2=True)` alive rather than `urllib`, to avoid a TLS handshake per command.

Streaming, reconstructed from SDK source (experimental surface, may change):

```python
import json, urllib.parse, websockets   # pip dependency
async def stream(pcm_chunks, key, model="google/gemini-3.5-transcribe-live", rate=16000, provider_options=None):
    url = ("wss://ai-gateway.vercel.sh/v4/ai/transcription-model?ai-model-id="
           + urllib.parse.quote(model, safe=""))
    protos = ["ai-gateway-transcription.v1", f"ai-gateway-auth.{key}"]
    async with websockets.connect(url, subprotocols=protos, max_size=2**20) as ws:
        start = {"type": "transcription-stream.start",
                 "inputAudioFormat": {"type": "audio/pcm", "rate": rate}}
        if provider_options: start["providerOptions"] = provider_options
        await ws.send(json.dumps(start))
        async def up():
            async for chunk in pcm_chunks:          # bytes, each <= 64 KiB, about 100 ms
                await ws.send(chunk)                # BINARY frame
            await ws.send(json.dumps({"type": "transcription-stream.audio-done"}))
        # run up() concurrently, then:
        async for msg in ws:                        # TEXT frames, one stream part each
            part = json.loads(msg)
            # part["type"] in: stream-start, transcript-delta, transcript-partial,
            # transcript-final, response-metadata, finish (terminal), error (terminal), raw
```

Open points the main loop must settle with the real key: whether the API key is accepted as the `ai-gateway-auth.` subprotocol value from a non browser client (the SDK's server side path does exactly this, so likely yes); whether the SDK also needs the `Authorization` and `ai-gateway-protocol-version` headers on the upgrade request (the SDK sends both headers AND subprotocols when the WebSocket implementation allows headers; sending both from Python is the safe choice via `additional_headers=`); and whether the URL encoded slash in `ai-model-id` must be `%2F` (the SDK uses `URLSearchParams`, which does encode it).

Provider options to try, keyed by provider name as in the AI SDK docs: `{"xai": {"language": "en", "keyterm": [...], "streaming": {"interimResults": true, "endpointing": 0}}}`, `{"openai": {"language": "en", "streaming": {"delay": "minimal"}}}`, `{"google": {"languageCodes": ["en-US"], "customVocabulary": [...]}}`. Whether the gateway expects the key `xai` or `spacexai` for Grok is NOT found.

## 9. Ranked recommendation

Fastest viable gateway served speech to text, ranked on evidence, not on marketing:

1. `google/gemini-3.5-transcribe-live`, streaming WebSocket. The only gateway streaming model with a good independent number: 458 ms median and a tight tail (P99 599 ms) direct to provider. Accepts `customVocabulary` for command words. Needs exactly 16 kHz PCM16, which matches a sane mic pipeline. 0.30 USD per 1000 two second commands. Weak point: finalization after end of audio is the fuzziest of the three in the SDK source.
2. `spacexai/grok-stt`, streaming WebSocket. UNMEASURED, so it cannot be ranked first honestly, but it is the one to benchmark next and may well win: cheapest by 3 to 10 times, explicit `audio.done` flush that suits push to talk, `keyterm` biasing, `endpointing` down to 0, 16 kHz accepted, and the only transcription model whose catalog entry says `zdr: "all"` (zero data retention), which matters for a microphone on a personal desktop. Weak points: partials only about every 500 ms, and a `transcript.created` wait at session start.
3. `openai/gpt-realtime-whisper` with `streaming.delay: "minimal"`. Measured 740 ms median at DEFAULT delay; the gain from `minimal` is unknown. Most expensive, and no vocabulary biasing in streaming mode. Append only deltas are the nicest partial format of the three.
4. Batch models, fallback only. Every millisecond is paid after speech ends and the upload is base64 JSON. Do not build on `openai/whisper-1`, `openai/gpt-4o-transcribe` or `openai/gpt-4o-mini-transcribe`: the catalog marks all three `deprecated_at` 2027-02-26. If a batch call is needed, use `spacexai/grok-stt` over the POST route.
5. The `realtime` type models: do not use for this app. Conversational speech to speech, priced per audio token or per second of OPEN session, with 5 minute idle and 25 minute hard session limits, and `openai/gpt-live-1` does not even share the endpoint.

Can the fastest gateway option beat local streaming for short commands? No. Local Moonshine tiny reaches a final about 235 ms after push to talk release on this exact machine under heavy load; the best gateway candidate needs about 0.5 to 0.9 s, and with Jev's 321 ms on top the totals are about 0.56 s local against 0.8 to 1.2 s cloud. Against the owner's rule ("if speech to text goes through the gateway it must be blazing fast") no gateway model qualifies for the hot path today.

Recommended architecture:
- Hot path: local streaming STT (tiny arch), push to talk, speculative Jev call fired on a stable partial and reused when the final matches.
- Gateway STT as a gated second opinion, not the default: when Jev's returned confidence on the local transcript is low, or the transcript contains out of vocabulary tokens, send the already buffered clip to the gateway (grok-stt batch with `keyterm`, or Gemini live) and re-ask Jev. That spends the extra 0.5 to 1 s only on the minority of hard utterances, and costs about 0.06 USD per thousand escalations.
- Revisit if real microphone testing shows tiny is not accurate enough: then gateway streaming beats the local "small" arch on both speed and accuracy, and option 1 or 2 becomes the hot path.

Benchmarks the main loop should run with the real key (n >= 30 each, same 8 command clips, push to talk timing, report median and P95): socket open to `stream-start`; first audio byte to first partial; audio-done to `finish`, for the three streaming models, with grok `endpointing: 0` and OpenAI `delay: minimal`; plus POST time to final for grok-stt batch with WAV and with a compressed `mediaType`.

## 10. Not found (searched, could not confirm in a credible source)

- Any Vercel published latency figure for gateway transcription, or the gateway's added overhead on `/transcription-model` (HTTP or WebSocket).
- Any independent TTFS or time to first partial for `spacexai/grok-stt`.
- Any independent time to final for one shot upload of a 1 to 3 s clip to any of the five batch models.
- The latency effect in ms of OpenAI `delay: minimal` vs default. OpenAI explicitly declines to give numbers.
- Documentation of the raw WebSocket protocol for streaming transcription outside SDK source. The envelope exists only in `@ai-sdk/provider-utils` source and is marked experimental.
- Any raw, Python, or non SDK example for the realtime type. Any statement that the long lived API key is accepted directly on `/realtime-model` without minting.
- Session limits (idle, max duration, concurrency) for the streaming TRANSCRIPTION socket. Limits are documented for realtime only.
- Accepted `mediaType` list per model on the batch route, and accepted `inputAudioFormat` per model on the streaming route, on the gateway side. Only provider side constraints were found in SDK source.
- Whether the gateway bills grok-stt streaming at xAI's 0.20 per hour streaming rate or the catalog's single 0.1008 per hour figure. Billing minimums or rounding for very short clips.
- A Vercel sentence explaining the `input: "0.0000000001"` placeholder on per second priced models.
- An OpenAI compatible `/v1/audio/transcriptions` route on the gateway. Vercel's doc says these audio operations use dedicated endpoints separate from Chat Completions, Messages and Responses.
- `fish-audio/transcribe-1-free`: present in the SDK's model id type, absent from the saved catalog.
- The network location of the Pipecat benchmark runs.
- Streaming transcription in Vercel's Python AI SDK beta (https://ai-python.dev/docs/basics/model-operations shows only one shot `ai.ops.transcribe`).

Status: COMPLETE.

## Verification

Independent verifier pass, 2026-09-21. No live gateway or Jev call was made (no key on this machine). Six facts checked.

1. Three streaming models, five batch only: CONFIRMED. Re-parsed gw_models.json: 376 models, 8 of type transcription, exactly 3 tagged websocket-transcription (spacexai/grok-stt, openai/gpt-realtime-whisper, google/gemini-3.5-transcribe-live). Fresh fetch of the Vercel speech-to-text doc names the same three, but with the wording "models such as", so the doc itself does not promise the list is exhaustive; the catalog tag is the exhaustive evidence.
2. Batch wire protocol: CONFIRMED. Fresh fetch of the Vercel doc shows the POST URL, the four headers, the base64 JSON body, the response shape, the urllib Python example, and "Multipart file uploads are not supported." SDK source agrees: getUrl at gateway-transcription-model.ts:167-169, model headers at 171-176, protocol version 0.0.1 at gateway-provider.ts:295 and 326. Tarball sha1 matches the npm registry for @ai-sdk/gateway 4.0.87, which is the current latest dist-tag.
3. Streaming wire protocol: CONFIRMED with one omission. URL builder at gateway-transcription-model.ts:184-191, 64 KiB constant at 217, subprotocol constants in gateway-realtime-auth.ts, envelope rules 1 to 7 (including the 256 KiB gateway limit and close 1000 after finish) in transcription-stream-envelope.ts. Omission: the stream part union also has a 'raw' part type (stream-part.ts:76), and the start frame can carry providerOptions and includeRawChunks (lines 140-148). A Python client must ignore unknown part types (envelope rule 6). The raw WebSocket surface is indeed absent from the Vercel doc, which only documents experimental_streamTranscribe.
4. API key in the ai-gateway-auth subprotocol: PARTIALLY CORRECT (code reading confirmed, server behavior unverifiable). getProtocolsFromHeaders at lines 200-214 does slice the Bearer value and put it in the subprotocol, and the mint body {model, routeKind, expiresIn} at gateway-provider.ts:376-390 plus the 60 s / 300 s / single use wording in the Vercel doc all check out. Whether the gateway server accepts a long lived key on this route, and whether the key fits the RFC subprotocol token grammar the auth module demands, cannot be confirmed without a key. Plan for a fallback that mints a vcst_ token per session.
5. Pipecat TTFS numbers: CONFIRMED. Fetched README from raw.githubusercontent.com main independently: 458/532/599, 740/878/1080, 637/965/1655, and no xAI or grok row. Still a secondary source measuring direct to provider, not via the gateway.
6. Local Moonshine tiny push to talk 234 to 243 ms median: PARTIALLY CORRECT. Recomputed medians from bench_tiny.json (234.0, range 186 to 463) and bench_tiny05.json (243.0, range 61 to 361), VAD 942.5 and 780.0, small 1035.5: all match. But the report omits bench_tiny_quiet.json and bench_small_quiet.json in the same scratchpad: under lower load (loadavg 10.4/4.8/4.0) tiny push to talk median is 383.5 ms (range 260 to 715, n=8) and small is 885 ms. So the honest local figure is 234 to 384 ms median with n=8 runs that disagree by 150 ms and in the counterintuitive direction; treat the local time to final as roughly 0.25 to 0.4 s, and the end to end estimate as about 0.56 to 0.70 s. The conclusion that local beats the gateway paths still holds.

Also checked: deprecated_at 1803600000000 ms is 2027-02-26 (confirmed); grok-stt is the only transcription model with zdr 'all' (confirmed); grok-stt's input price is '0', not the 1e-10 placeholder, a small inaccuracy in the placeholder claim; net.ipv4.tcp_slow_start_after_idle=1 (confirmed by sysctl).
