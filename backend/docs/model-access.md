# Model access: what was verified, and where the design was wrong

Verified 2026-07-28 against the live endpoint with `strands-agents 1.50.2` / `openai 2.49.0`.
Everything here is a measurement or an SDK-raised error, not a reading of documentation.

## Confirmed as designed

| Claim | How it was checked |
|---|---|
| GPT-5.6 needs `OpenAIResponsesModel`, not `OpenAIModel` | Responses call succeeds; the model family is not on Chat Completions |
| `bedrock_mantle_config` selects `/openai/v1` for `openai.gpt-5.*` | `_resolve_mantle_base_path` in `strands.models._openai_bedrock` |
| Mantle mints a bearer token per call | `resolve_bedrock_client_args` calls `provide_token` on every request, so no cache or expiry handling belongs in our code |
| `bedrock_mantle_config` and `client_args` credentials are mutually exclusive | `ValueError: client_args must not contain ['api_key'] when bedrock_mantle_config is set` — asserted in `tests/test_provider.py` |
| Constructor signature | `(self, client_args=None, bedrock_mantle_config=None, **model_config)`; config keys are `model_id`, `params`, `context_window_limit`, `stateful`, `use_native_token_count` |
| `max_output_tokens` is the correct params key | Accepted; the design flagged this as needing confirmation |

## Where the design was wrong

### `temperature` is rejected by this model family

design.md §2 specifies three different temperatures (higher for generation, lower for audit and
revise). The live endpoint refuses the parameter outright:

```
400 unsupported_parameter: Unsupported parameter: 'temperature' is not supported with this model.
```

`provider.build_model` therefore takes no temperature. Step behaviour is tuned with
`reasoning.effort` instead — `high` for the audit, where a drifting verdict would be worse than a
slow one, `medium` for generation and revision. Generation diversity comes from the scenario
prompt rather than from sampling.

A unit test asserts no step ever sets a temperature, so this cannot be reintroduced by copying
the design's example code.

### Mantle token minting needs live SigV4 credentials

Worth recording because the failure is easy to misread. With `IELTS_MODEL_AUTH=mantle` (the
default, and what production uses) Strands mints a bearer token from the ambient AWS credential
chain on every call. On a machine whose SigV4 credentials have expired, that produces:

```
openai.AuthenticationError: 401 invalid_api_key -
    The security token included in the request is invalid.
```

The natural reading is "model access was revoked". It is not: the same request succeeds with a
pre-minted `AWS_BEARER_TOKEN_BEDROCK`, which is how this implementation was developed and
verified end to end. `aws sts get-caller-identity` distinguishes the two cases in one command.

`IELTS_MODEL_AUTH=bearer` exists for exactly this situation and is development-only. Production
must use `mantle`, because a pre-minted token expires and nothing refreshes it.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `IELTS_MODEL_ID` | `openai.gpt-5.6-terra` | switch within the family (`-sol` / `-luna`) without a rebuild |
| `IELTS_MODEL_REGION` | `AWS_REGION` or `us-east-1` | must be `us-east-1` or `us-east-2`; no cross-region inference |
| `IELTS_MODEL_AUTH` | `mantle` | `bearer` only for local work with an expired SigV4 chain |
| `IELTS_CONCURRENCY` | `6` | in-invocation slots. Effectively dead in production (one material per invocation clamps it to 1); still governs the CLI. Measured safe at 3; lower it on 429s rather than adding retries |
| `IELTS_P95_PER_MATERIAL` | `240` | per-material budget check before starting a slot |
| `IELTS_SAFETY_MARGIN` | `90` | reserve for emitting the summary and closing cleanly |
