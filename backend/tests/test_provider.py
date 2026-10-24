"""Offline provider contracts, including real SDK formatting and credential isolation."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import traceback
from pathlib import Path
from unittest.mock import Mock

import boto3
import httpx
import pytest
from botocore.client import BaseClient
from botocore.credentials import CredentialProvider, RefreshableCredentials
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.stub import Stubber
from openai.types.chat import ChatCompletion
from strands.models.openai import OpenAIModel
from strands.models.openai_responses import OpenAIResponsesModel

from backend.model import provider

BACKEND = Path(__file__).resolve().parents[1]
KIMI = "moonshotai.kimi-k2.5"
K3 = "us.moonshotai.kimi-k3"
GPT = "openai.gpt-5.6-luna"
SECRET_ARN = "arn:aws-cn:secretsmanager:cn-northwest-1:123456789012:secret:ielts/model-Ab12Cd"


@pytest.fixture(autouse=True)
def offline_config(monkeypatch, tmp_path):
    """No test can use operator credentials or make an AWS service request."""
    monkeypatch.setattr(provider, "MODEL_ID", KIMI)
    monkeypatch.setattr(provider, "AUTH_MODE", "bearer")
    monkeypatch.setattr(provider, "REGION", "us-east-1")
    monkeypatch.setattr(provider, "_model_cache", {"at": 0.0, "models": None})
    for key in ("IELTS_ALLOWED_MODEL_IDS", "IELTS_MODEL_PROFILE",
                "IELTS_MODEL_SECRET_ARN", "IELTS_MODEL_SECRET_ACCOUNT_ID", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "cn-northwest-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "offline-test-token")

    def no_aws(*args, **kwargs):
        pytest.fail("Unexpected AWS API call in offline provider tests")

    sdk_api_call = BaseClient._make_api_call
    monkeypatch.setattr(BaseClient, "_make_api_call", no_aws)
    return sdk_api_call


@pytest.fixture
def explicit_profile(monkeypatch, tmp_path):
    (tmp_path / "credentials").write_text(
        "[overseas-test]\naws_access_key_id = offline-key\naws_secret_access_key = offline-secret\n"
    )
    monkeypatch.setenv("IELTS_MODEL_PROFILE", "overseas-test")
    monkeypatch.setattr(provider, "AUTH_MODE", "profile")


class TestTransports:
    def test_kimi_chat_completions_and_parameters(self):
        model = provider.build_model(32000, reasoning_effort="high")
        assert isinstance(model, OpenAIModel)
        assert model.client_args["base_url"] == "https://bedrock-mantle.us-east-1.api.aws/v1"
        request = model.format_request([{"role": "user", "content": [{"text": "Hello"}]}])
        assert request["model"] == KIMI
        assert request["max_tokens"] == 16384
        for key in ("reasoning", "reasoning_effort", "temperature", "top_p", "max_output_tokens"):
            assert key not in request

    @pytest.mark.parametrize("budget,expected", [(1, 1), (1000, 1000), (16384, 16384), (32000, 16384)])
    def test_kimi_output_cap(self, budget, expected):
        assert provider.build_model(budget).config["params"] == {"max_tokens": expected}

    @pytest.mark.parametrize("budget", [0, -1])
    def test_invalid_budget(self, budget):
        with pytest.raises(ValueError, match="positive"):
            provider.build_model(budget)

    @pytest.mark.parametrize("identifier", [GPT, "openai.gpt-5.6-sol", "openai.gpt-5.6-terra"])
    def test_explicit_legacy_gpt_responses(self, monkeypatch, identifier):
        monkeypatch.setattr(provider, "MODEL_ID", identifier)
        model = provider.build_model(32000, reasoning_effort="high")
        assert isinstance(model, OpenAIResponsesModel)
        assert model.client_args["base_url"] == "https://bedrock-mantle.us-east-1.api.aws/openai/v1"
        assert model.config["params"] == {"max_output_tokens": 32000, "reasoning": {"effort": "high"}}

    @pytest.mark.parametrize("model_class,identifier", [(OpenAIModel, KIMI), (OpenAIResponsesModel, GPT)])
    def test_sdk_rejects_mixed_auth(self, model_class, identifier):
        with pytest.raises(ValueError, match="api_key"):
            model_class(model_id=identifier, bedrock_mantle_config={"region": "us-east-1"},
                        client_args={"api_key": "offline"})

    def test_kimi_tool_round_trip_format(self):
        model = provider.build_model(1000)
        tools = [{"name": "echo", "description": "Echo text", "inputSchema": {"json": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}]
        messages = [
            {"role": "user", "content": [{"text": "Echo hello"}]},
            {"role": "assistant", "content": [{"toolUse": {
                "toolUseId": "call_test", "name": "echo", "input": {"text": "hello"}}}]},
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": "call_test", "status": "success", "content": [{"text": "hello"}]}}]},
        ]
        request = model.format_request(messages, tool_specs=tools)
        assert request["tools"][0]["function"]["name"] == "echo"
        assert request["messages"][1]["tool_calls"][0]["id"] == "call_test"
        assert request["messages"][2]["role"] == "tool"
        assert request["messages"][2]["tool_call_id"] == "call_test"
        response = ChatCompletion.model_validate({
            "id": "offline", "created": 0, "model": KIMI, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_test", "type": "function", "function": {
                        "name": "echo", "arguments": '{"text":"hello"}'}}]}}],
        })
        events = model._format_non_streaming_response(response)
        assert any(event.get("contentBlockStart", {}).get("start", {}).get("toolUse", {}).get("name")
                   == "echo" for event in events)
        assert {"messageStop": {"stopReason": "tool_use"}} in events
        response.choices[0].message.tool_calls = None
        response.choices[0].message.content = '{"ok":true}'
        response.choices[0].finish_reason = "stop"
        events = model._format_non_streaming_response(response)
        assert any(event.get("contentBlockDelta", {}).get("delta", {}).get("text") == '{"ok":true}'
                   for event in events)


class TestCredentials:
    def test_bearer_does_not_construct_aws_session(self, monkeypatch):
        monkeypatch.setattr(boto3, "Session", Mock(side_effect=AssertionError("No AWS session")))
        model = provider.build_model(1000)
        assert model.client_args["api_key"] == "offline-test-token"
        assert provider.list_models()["default_model_id"] == KIMI
        assert "offline-test-token" not in str(provider.describe())

    def test_missing_bearer_fails(self, monkeypatch):
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
        with pytest.raises(RuntimeError, match="requires AWS_BEARER_TOKEN_BEDROCK"):
            provider.build_model(1000)

    def test_missing_profile_never_falls_back(self, monkeypatch):
        monkeypatch.setattr(provider, "AUTH_MODE", "profile")
        monkeypatch.setattr(boto3, "Session", Mock(side_effect=AssertionError("No AWS session")))
        for action in (lambda: provider.build_model(1000), provider.list_models):
            with pytest.raises(RuntimeError, match="requires IELTS_MODEL_PROFILE"):
                action()

    def test_profile_ignores_ambient_keys_and_preserves_bearer(self, explicit_profile, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-china-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-china-secret")
        model = provider.build_model(1000)
        config = model._bedrock_mantle_config
        assert model.client_args == {}
        assert config["region"] == "us-east-1"
        credentials_provider = config["credentials_provider"]
        assert isinstance(credentials_provider, CredentialProvider)
        assert credentials_provider.session.profile_name == "overseas-test"
        assert credentials_provider.session.region_name == "us-east-1"
        assert credentials_provider.load().access_key == "offline-key"
        import os
        assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "offline-test-token"

    def test_empty_profile_cannot_fall_back_to_ecs_or_imds(self, explicit_profile, tmp_path, monkeypatch):
        (tmp_path / "credentials").write_text("[overseas-test]\n")
        monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://127.0.0.1:9/credentials")
        from botocore.credentials import ContainerProvider, InstanceMetadataProvider
        for cls in (ContainerProvider, InstanceMetadataProvider):
            monkeypatch.setattr(cls, "load", Mock(side_effect=AssertionError("Ambient credential lookup")))
        with pytest.raises(RuntimeError, match="ambient fallback is disabled"):
            provider.build_model(1000)

    @pytest.mark.parametrize("source", ["Environment", "Ec2InstanceMetadata", "EcsContainer"])
    def test_explicit_profile_cannot_source_ambient_role(self, explicit_profile, tmp_path, source):
        (tmp_path / "credentials").write_text("")
        (tmp_path / "config").write_text(
            "[profile overseas-test]\nrole_arn = arn:aws:iam::123456789012:role/test\n"
            f"credential_source = {source}\n"
        )
        with pytest.raises(RuntimeError, match="credential_source"):
            provider.build_model(1000)

    @pytest.mark.parametrize("source", ["Environment", "Ec2InstanceMetadata", "EcsContainer"])
    def test_nested_profile_cannot_source_ambient_role(self, explicit_profile, tmp_path, source):
        (tmp_path / "credentials").write_text("")
        (tmp_path / "config").write_text(
            "[profile overseas-test]\nrole_arn = arn:aws:iam::123456789012:role/model\n"
            "source_profile = nested-source\n"
            "[profile nested-source]\nrole_arn = arn:aws:iam::123456789012:role/source\n"
            f"credential_source = {source}\n"
        )
        with pytest.raises(RuntimeError, match="credential_source"):
            provider.build_model(1000)

    def test_empty_source_profile_cannot_use_ambient_keys(self, explicit_profile, tmp_path, monkeypatch):
        (tmp_path / "credentials").write_text("[empty-source]\n")
        (tmp_path / "config").write_text(
            "[profile overseas-test]\nrole_arn = arn:aws:iam::123456789012:role/model\n"
            "source_profile = empty-source\n"
        )
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-china-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-china-secret")
        from botocore.exceptions import InvalidConfigError
        with pytest.raises(InvalidConfigError, match="must have credentials"):
            provider.build_model(1000)

    def test_refreshable_credentials_are_not_frozen(self):
        credentials = RefreshableCredentials.create_from_metadata(
            metadata={"access_key": "offline", "secret_key": "offline", "token": "offline",
                      "expiry_time": "2099-01-01T00:00:00Z"},
            refresh_using=Mock(), method="offline",
        )
        session = Mock()
        session.get_credentials.return_value = credentials
        wrapper = provider._ProfileCredentialsProvider(session)
        assert wrapper.load() is credentials
        assert wrapper.load() is credentials

    @pytest.mark.parametrize("identifier,path", [(KIMI, "/v1"), (GPT, "/openai/v1")])
    def test_sdk_mints_per_request_with_explicit_provider(self, explicit_profile, monkeypatch, identifier, path):
        monkeypatch.setattr(provider, "MODEL_ID", identifier)
        import aws_bedrock_token_generator
        mint = Mock(return_value="offline-minted")
        monkeypatch.setattr(aws_bedrock_token_generator, "provide_token", mint)
        model = provider.build_model(1000)
        for _ in range(2):
            resolved = model._resolve_client_args()
            assert resolved["base_url"] == "https://bedrock-mantle.us-east-1.api.aws" + path
        assert mint.call_count == 2
        assert mint.call_args.kwargs["aws_credentials_provider"] is model._bedrock_mantle_config["credentials_provider"]

    @pytest.mark.parametrize("region_key", ["AWS_REGION", "AWS_DEFAULT_REGION"])
    def test_china_mantle_is_rejected(self, monkeypatch, region_key):
        monkeypatch.setattr(provider, "AUTH_MODE", "mantle")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv(region_key, "cn-north-1")
        for action in (lambda: provider.build_model(1000), provider.list_models):
            with pytest.raises(RuntimeError, match="explicit overseas"):
                action()

    def test_legacy_mantle_outside_china(self, monkeypatch):
        monkeypatch.setattr(provider, "MODEL_ID", GPT)
        monkeypatch.setattr(provider, "AUTH_MODE", "mantle")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.delenv("AWS_DEFAULT_REGION")
        model = provider.build_model(1000)
        assert model.client_args == {}
        assert model._bedrock_mantle_config == {"region": "us-east-1"}

    def test_unknown_auth_rejected(self, monkeypatch):
        monkeypatch.setattr(provider, "AUTH_MODE", "typo")
        with pytest.raises(ValueError, match="IELTS_MODEL_AUTH"):
            provider.build_model(1000)


@pytest.fixture
def secret_auth(monkeypatch):
    monkeypatch.setattr(provider, "AUTH_MODE", "secret")
    monkeypatch.setenv("IELTS_MODEL_SECRET_ARN", SECRET_ARN)
    monkeypatch.setenv("IELTS_MODEL_SECRET_ACCOUNT_ID", "123456789012")
    session = Mock()
    client = Mock(spec_set=["get_secret_value", "close"])
    session.client.return_value = client
    client.get_secret_value.return_value = {
        "ARN": SECRET_ARN, "SecretString": '{"token":"synthetic-secret-token"}',
    }
    factory = Mock(return_value=session)
    monkeypatch.setattr(boto3, "Session", factory)
    return factory, session, client


class TestSecretCredentials:
    @pytest.mark.parametrize("denied", [False, True])
    def test_real_boto_client_is_closed_with_stubbed_lookup(self, offline_config, monkeypatch, denied):
        monkeypatch.setattr(provider, "AUTH_MODE", "secret")
        monkeypatch.setenv("IELTS_MODEL_SECRET_ARN", SECRET_ARN)
        monkeypatch.setenv("IELTS_MODEL_SECRET_ACCOUNT_ID", "123456789012")
        session = boto3.Session(
            region_name="cn-northwest-1",
            aws_access_key_id="synthetic-access",
            aws_secret_access_key="synthetic-signing-secret",
        )
        client = session.client("secretsmanager")
        # Restore dispatch only on this client so Stubber exercises the real SDK
        # protocol; the transport remains blocked even if a stub is missing.
        monkeypatch.setattr(client, "_make_api_call", offline_config.__get__(client, type(client)))
        send = Mock(side_effect=AssertionError("Unexpected network request"))
        monkeypatch.setattr(client._endpoint.http_session, "send", send)
        close = Mock(wraps=client.close)
        monkeypatch.setattr(client, "close", close)
        monkeypatch.setattr(session, "client", Mock(return_value=client))
        monkeypatch.setattr(boto3, "Session", Mock(return_value=session))
        with Stubber(client) as stubber:
            expected = {"SecretId": SECRET_ARN, "VersionStage": "AWSCURRENT"}
            if denied:
                stubber.add_client_error(
                    "get_secret_value", service_error_code="AccessDeniedException",
                    service_message="synthetic-secret-token", expected_params=expected,
                )
                with pytest.raises(RuntimeError, match="Unable to resolve model secret credentials") as raised:
                    provider.build_model(1000)
                assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value))
            else:
                stubber.add_response("get_secret_value", {
                    "ARN": SECRET_ARN, "SecretString": '{"token":"synthetic-secret-token"}',
                    "VersionStages": ["AWSCURRENT"],
                }, expected)
                assert provider.build_model(1000).client_args["api_key"] == "synthetic-secret-token"
            stubber.assert_no_pending_responses()
        close.assert_called_once_with()
        send.assert_not_called()

    @pytest.mark.parametrize("identifier,path", [(KIMI, "/v1"), (GPT, "/openai/v1")])
    def test_transport_and_bounded_china_secret_lookup(self, secret_auth, monkeypatch, identifier, path):
        factory, session, client = secret_auth
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", GPT)
        monkeypatch.setenv("IELTS_MODEL_SECRET_ACCOUNT_ID", "123456789012")
        model = provider.build_model(32000, reasoning_effort="high", model_id=identifier)
        factory.assert_called_once_with(region_name="cn-northwest-1")
        assert session.client.call_args.args == ("secretsmanager",)
        kwargs = session.client.call_args.kwargs
        assert kwargs["region_name"] == "cn-northwest-1"
        assert kwargs["config"].connect_timeout == 3
        assert kwargs["config"].read_timeout == 5
        assert kwargs["config"].retries == {"mode": "standard", "total_max_attempts": 2}
        assert kwargs["config"].ignore_configured_endpoint_urls is True
        client.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN, VersionStage="AWSCURRENT")
        client.close.assert_called_once_with()
        assert model.client_args == {
            "api_key": "synthetic-secret-token",
            "base_url": "https://bedrock-mantle.us-east-1.api.aws" + path,
        }
        assert model.config["params"] == (
            {"max_tokens": 16384} if identifier == KIMI
            else {"max_output_tokens": 32000, "reasoning": {"effort": "high"}}
        )
        assert "synthetic-secret-token" not in str(provider.describe())

    @pytest.mark.parametrize("arn", [
        "", "ielts/model", SECRET_ARN.replace("aws-cn", "aws"),
        SECRET_ARN.replace("secretsmanager", "ssm"),
        SECRET_ARN.replace("123456789012", "123"),
        SECRET_ARN.replace("cn-northwest-1", "us-east-1"),
        SECRET_ARN.replace("cn-northwest-1", "cn-north-1"),
        SECRET_ARN.replace("-Ab12Cd", ""), SECRET_ARN + ":extra",
        SECRET_ARN + "\n", SECRET_ARN.replace("ielts/model", "bad*name"),
    ])
    def test_bad_reference_rejected_before_sdk(self, secret_auth, monkeypatch, arn):
        factory, _, _ = secret_auth
        monkeypatch.setenv("IELTS_MODEL_SECRET_ARN", arn)
        for action in (lambda: provider.build_model(1000), provider.list_models):
            with pytest.raises((ValueError, RuntimeError), match="IELTS_MODEL_SECRET_ARN"):
                action()
        factory.assert_not_called()

    @pytest.mark.parametrize("region", ["", "us-east-1", "cn-north-1"])
    def test_invalid_application_region(self, secret_auth, monkeypatch, region):
        factory, _, _ = secret_auth
        monkeypatch.delenv("AWS_DEFAULT_REGION")
        monkeypatch.setenv("AWS_REGION", region)
        with pytest.raises(ValueError, match="application region"):
            provider.build_model(1000)
        factory.assert_not_called()

    def test_default_region_fallback_and_beijing(self, secret_auth, monkeypatch):
        factory, _, client = secret_auth
        monkeypatch.delenv("AWS_REGION")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "cn-north-1")
        arn = SECRET_ARN.replace("cn-northwest-1", "cn-north-1")
        monkeypatch.setenv("IELTS_MODEL_SECRET_ARN", arn)
        client.get_secret_value.return_value["ARN"] = arn
        provider.build_model(1000)
        factory.assert_called_once_with(region_name="cn-north-1")
        client.get_secret_value.assert_called_once_with(SecretId=arn, VersionStage="AWSCURRENT")

    @pytest.mark.parametrize("account", ["", "invalid", "123", "999999999999"])
    def test_expected_account_mismatch(self, secret_auth, monkeypatch, account):
        factory, _, _ = secret_auth
        monkeypatch.setenv("IELTS_MODEL_SECRET_ACCOUNT_ID", account)
        with pytest.raises(ValueError, match="IELTS_MODEL_SECRET_ACCOUNT_ID"):
            provider.build_model(1000)
        factory.assert_not_called()

    def test_missing_expected_account_cannot_skip_isolation(self, secret_auth, monkeypatch):
        factory, _, _ = secret_auth
        monkeypatch.delenv("IELTS_MODEL_SECRET_ACCOUNT_ID")
        for action in (lambda: provider.build_model(1000), provider.list_models):
            with pytest.raises(ValueError, match="IELTS_MODEL_SECRET_ACCOUNT_ID"):
                action()
        factory.assert_not_called()

    @pytest.mark.parametrize("returned_arn", [
        None, SECRET_ARN.replace("aws-cn", "aws"),
        SECRET_ARN.replace("cn-northwest-1", "cn-north-1"),
        SECRET_ARN.replace("123456789012", "999999999999"),
        SECRET_ARN.replace("ielts/model", "ielts/other"),
        SECRET_ARN.replace("Ab12Cd", "Xy12Zz"),
    ])
    def test_response_must_identify_exact_secret(self, secret_auth, monkeypatch, returned_arn):
        _, _, client = secret_auth
        client.get_secret_value.return_value["ARN"] = returned_arn
        constructor = Mock()
        monkeypatch.setattr(provider, "OpenAIModel", constructor)
        with pytest.raises(RuntimeError, match="Unable to resolve model secret credentials") as raised:
            provider.build_model(1000)
        constructor.assert_not_called()
        assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value))

    @pytest.mark.parametrize("response", [
        {}, {"SecretBinary": b"synthetic-secret-token"}, {"SecretString": None},
        {"SecretString": b'{"token":"synthetic-secret-token"}'},
        {"SecretString": "synthetic-secret-token"},
        *({"SecretString": json.dumps(value)} for value in [
            [], None, "synthetic-secret-token", {}, {"api_key": "synthetic-secret-token"},
            {"token": None}, {"token": 123}, {"token": []}, {"token": {}},
            {"token": ""}, {"token": " "}, {"token": "synthetic-secret-token\n"},
            {"token": "synthetic secret token"}, {"token": "密钥"},
        ]),
    ])
    def test_bad_payload_is_redacted_without_fallback(self, secret_auth, monkeypatch, caplog, response):
        _, _, client = secret_auth
        client.get_secret_value.return_value = {"ARN": SECRET_ARN, **response}
        constructor = Mock(side_effect=AssertionError("No fallback"))
        monkeypatch.setattr(provider, "OpenAIModel", constructor)
        with pytest.raises(RuntimeError, match="Unable to resolve model secret credentials") as error:
            provider.build_model(1000)
        rendered = "".join(traceback.format_exception(error.value))
        assert "synthetic-secret-token" not in rendered + caplog.text
        assert error.value.__cause__ is None
        constructor.assert_not_called()

    @pytest.mark.parametrize("error", [
        ClientError({"Error": {"Code": code, "Message": "synthetic-secret-token"}}, "GetSecretValue")
        for code in ("AccessDeniedException", "ResourceNotFoundException", "DecryptionFailure",
                     "ThrottlingException")
    ] + [
        EndpointConnectionError(endpoint_url="https://synthetic-secret-token"),
        ReadTimeoutError(endpoint_url="https://synthetic-secret-token"),
        RuntimeError("synthetic-secret-token"),
    ])
    def test_sdk_errors_are_redacted(self, secret_auth, monkeypatch, caplog, error):
        _, _, client = secret_auth
        client.get_secret_value.side_effect = error
        constructor = Mock()
        monkeypatch.setattr(provider, "OpenAIModel", constructor)
        with pytest.raises(RuntimeError, match="Unable to resolve model secret credentials") as raised:
            provider.build_model(1000)
        assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value)) + caplog.text
        constructor.assert_not_called()
        client.close.assert_called_once_with()

    def test_session_creation_error_is_redacted(self, secret_auth):
        factory, _, _ = secret_auth
        factory.side_effect = RuntimeError("synthetic-secret-token")
        with pytest.raises(RuntimeError) as raised:
            provider.build_model(1000)
        assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value))

    def test_sdk_debug_response_body_is_not_logged(self, secret_auth, caplog):
        _, _, client = secret_auth
        from botocore.parsers import ResponseParserFactory

        def parse_response(**kwargs):
            # Use the actual SDK parser: its DEBUG output otherwise includes SecretString.
            return ResponseParserFactory().create_parser("json").parse(
                {"status_code": 200, "headers": {},
                 "body": b'{"SecretString":"synthetic-secret-token"}'}, None,
            )

        client.get_secret_value.side_effect = parse_response
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(RuntimeError):
                provider.build_model(1000)
            logging.getLogger("botocore.parsers").debug("outside secret lookup")
        assert "synthetic-secret-token" not in caplog.text
        assert "outside secret lookup" in caplog.text

    def test_sdk_transport_exception_is_not_logged(self, secret_auth, monkeypatch, caplog):
        from botocore.awsrequest import AWSRequest
        from botocore.httpsession import URLLib3Session

        _, _, client = secret_auth
        manager = Mock()
        manager.connection_from_url.return_value.urlopen.side_effect = RuntimeError(
            "synthetic-secret-token"
        )
        # Exercise the real SDK's transport exception/debug logging without a socket.
        monkeypatch.setattr(URLLib3Session, "_get_connection_manager", Mock(return_value=manager))

        def fail_transport(**kwargs):
            transport = URLLib3Session()
            try:
                return transport.send(AWSRequest(
                    method="POST", url="https://secretsmanager.cn-northwest-1.amazonaws.com.cn",
                ).prepare())
            finally:
                transport.close()

        client.get_secret_value.side_effect = fail_transport
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(RuntimeError) as raised:
                provider.build_model(1000)
            logging.getLogger("botocore.httpsession").debug("outside secret lookup")
        assert "synthetic-secret-token" not in caplog.text
        assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value))
        assert "outside secret lookup" in caplog.text

    def test_sdk_signing_does_not_log_runtime_session_credentials(self, secret_auth, caplog):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        _, _, client = secret_auth
        response = client.get_secret_value.return_value

        def sign_request(**kwargs):
            request = AWSRequest(
                method="POST", url="https://secretsmanager.cn-northwest-1.amazonaws.com.cn",
            )
            SigV4Auth(
                Credentials("synthetic-access", "synthetic-signing-secret", "synthetic-session-token"),
                "secretsmanager", "cn-northwest-1",
            ).add_auth(request)
            return response

        client.get_secret_value.side_effect = sign_request
        with caplog.at_level(logging.DEBUG):
            provider.build_model(1000)
            logging.getLogger("botocore.auth").debug("outside secret lookup")
        for value in ("synthetic-access", "synthetic-signing-secret", "synthetic-session-token"):
            assert value not in caplog.text
        assert "outside secret lookup" in caplog.text

    def test_adapter_initialization_error_is_redacted(self, secret_auth, monkeypatch):
        monkeypatch.setattr(provider, "OpenAIModel", Mock(side_effect=ValueError("synthetic-secret-token")))
        with pytest.raises(RuntimeError, match="Unable to initialize model") as raised:
            provider.build_model(1000)
        assert "synthetic-secret-token" not in "".join(traceback.format_exception(raised.value))

    def test_rotation_context_and_environment_preserved(self, secret_auth, monkeypatch):
        _, _, client = secret_auth
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", GPT)
        client.get_secret_value.side_effect = [
            {"ARN": SECRET_ARN, "SecretString": '{"token":"synthetic-old"}'},
            {"ARN": SECRET_ARN, "SecretString": '{"token":"synthetic-new"}'},
        ]
        with provider.use_model(GPT):
            first = provider.build_model(1000)
        second = provider.build_model(1000)
        assert first.config["model_id"] == GPT
        assert second.config["model_id"] == KIMI
        assert first.client_args["api_key"] == "synthetic-old"
        assert second.client_args["api_key"] == "synthetic-new"
        assert client.get_secret_value.call_count == 2
        import os
        assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "offline-test-token"
        assert "synthetic-" not in repr(provider._model_cache)

    def test_rotation_lookup_failure_never_reuses_previous_token(self, secret_auth):
        _, _, client = secret_auth
        client.get_secret_value.side_effect = [
            {"ARN": SECRET_ARN, "SecretString": '{"token":"synthetic-old"}'},
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": "synthetic-old"}},
                        "GetSecretValue"),
            {"ARN": SECRET_ARN, "SecretString": '{"token":"synthetic-new"}'},
        ]
        first = provider.build_model(1000)
        with pytest.raises(RuntimeError, match="Unable to resolve model secret credentials"):
            provider.build_model(1000)
        third = provider.build_model(1000)
        assert first.client_args["api_key"] == "synthetic-old"
        assert third.client_args["api_key"] == "synthetic-new"
        assert client.get_secret_value.call_count == 3
        assert all(call.kwargs == {"SecretId": SECRET_ARN, "VersionStage": "AWSCURRENT"}
                   for call in client.get_secret_value.call_args_list)

    def test_catalogue_never_resolves_secret_or_discovers(self, secret_auth, monkeypatch):
        factory, _, _ = secret_auth
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", f"{GPT},unknown-model")
        monkeypatch.setattr(provider, "_remote_models", Mock(side_effect=AssertionError("No discovery")))
        assert [row["id"] for row in provider.list_models(force=True)["models"]] == [KIMI, GPT]
        assert provider.resolve_model_id(GPT) == GPT
        factory.assert_not_called()


class TestCatalogueAndSelection:
    def test_default_catalogue_does_not_discover_or_expose_gpt(self, monkeypatch):
        monkeypatch.setattr(provider, "_remote_models", Mock(side_effect=AssertionError("No discovery")))
        assert provider.list_models(force=True) == {
            "models": [{"id": KIMI, "label": "kimi-k2.5"}], "default_model_id": KIMI,
        }

    def test_only_configured_implemented_adapters_are_exposed(self, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", f"{KIMI},{GPT},{GPT},moonshotai.kimi-k4,openai.gpt-unknown")
        assert [row["id"] for row in provider.list_models()["models"]] == [KIMI, GPT]
        assert provider.resolve_model_id(GPT) == GPT
        for identifier in ("moonshotai.kimi-k4", "openai.gpt-unknown", "openai.gpt-5.6-sol"):
            with pytest.raises(ValueError, match="not available"):
                provider.resolve_model_id(identifier)
            with pytest.raises(ValueError, match="not available"):
                provider.build_model(1000, model_id=identifier)

    def test_unsupported_default_fails(self, monkeypatch):
        monkeypatch.setattr(provider, "MODEL_ID", "moonshotai.kimi-k4")
        with pytest.raises(ValueError, match="no implemented"):
            provider.list_models()

    def test_profile_catalogue_uses_overseas_session(self, explicit_profile, monkeypatch):
        session = Mock()
        session.client.return_value.list_foundation_models.return_value = {
            "modelSummaries": [{"modelId": KIMI}, {"modelId": GPT}, {"modelId": "moonshotai.kimi-k3"}]
        }
        monkeypatch.setattr(provider, "_profile_session", lambda: session)
        assert [row["id"] for row in provider.list_models()["models"]] == [KIMI]
        session.client.assert_called_once_with("bedrock", region_name="us-east-1")

    def test_discovery_intersects_configuration_keeps_default(self, explicit_profile, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", f"{GPT},openai.gpt-5.6-sol")
        remote = Mock(return_value=[GPT, "moonshotai.kimi-k3", "anthropic.claude-test"])
        monkeypatch.setattr(provider, "_remote_models", remote)
        assert [row["id"] for row in provider.list_models()["models"]] == [KIMI, GPT]
        provider.list_models()
        remote.assert_called_once()

    def test_discovery_failure_uses_configuration_and_preserves_warning(self, explicit_profile, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", GPT)
        monkeypatch.setattr(provider, "_remote_models", Mock(side_effect=RuntimeError("private error details")))
        first = provider.list_models()
        assert [row["id"] for row in first["models"]] == [KIMI, GPT]
        assert "warning" in first
        assert "private error" not in str(first)
        assert provider.list_models() == first

    def test_cache_is_invalidated_by_profile_change(self, explicit_profile, monkeypatch):
        remote = Mock(return_value=[KIMI])
        monkeypatch.setattr(provider, "_remote_models", remote)
        provider.list_models()
        monkeypatch.setenv("IELTS_MODEL_PROFILE", "another-overseas-test")
        provider.list_models()
        assert remote.call_count == 2

    def test_expired_fallback_recovers_catalogue_and_clears_warning(self, explicit_profile, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", GPT)
        monkeypatch.setattr(provider, "_MODEL_CACHE_TTL", 300)
        clock = Mock(return_value=1000.0)
        monkeypatch.setattr(provider.time, "monotonic", clock)
        remote = Mock(side_effect=[RuntimeError("temporary failure"), [KIMI]])
        monkeypatch.setattr(provider, "_remote_models", remote)
        fallback = provider.list_models()
        assert "warning" in fallback
        assert [row["id"] for row in fallback["models"]] == [KIMI, GPT]
        clock.return_value = 1299.0
        assert provider.list_models() == fallback
        assert remote.call_count == 1
        clock.return_value = 1300.0
        recovered = provider.list_models()
        assert "warning" not in recovered
        assert [row["id"] for row in recovered["models"]] == [KIMI]
        assert remote.call_count == 2

    def test_context_reset_and_explicit_override(self, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", GPT)
        with pytest.raises(RuntimeError):
            with provider.use_model(GPT):
                assert provider.build_model(1000).config["model_id"] == GPT
                assert provider.build_model(1000, model_id=KIMI).config["model_id"] == KIMI
                raise RuntimeError("step failed")
        assert provider.build_model(1000).config["model_id"] == KIMI

    @pytest.mark.asyncio
    async def test_concurrent_requests_have_independent_models(self, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", GPT)

        async def request(identifier):
            with provider.use_model(identifier):
                await asyncio.sleep(0)
                return [provider.build_model(1000).config["model_id"] for _ in range(3)]

        assert await asyncio.gather(request(KIMI), request(GPT)) == [[KIMI] * 3, [GPT] * 3]
        assert provider.resolve_model_id(None) == KIMI


class TestRegionAndExistingGuards:
    def test_import_defaults_ignore_china_region(self, monkeypatch):
        for key in ("IELTS_MODEL_REGION", "IELTS_MODEL_ID"):
            monkeypatch.delenv(key, raising=False)
        spec = importlib.util.spec_from_file_location("isolated_provider", provider.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.REGION == "us-east-1"
        assert module.MODEL_ID == KIMI
        assert module.describe() == {"model_id": KIMI, "region": "us-east-1", "auth_mode": module.AUTH_MODE}

    @pytest.mark.parametrize("region", provider.SUPPORTED_REGIONS)
    def test_supported_regions(self, region):
        provider.assert_region_supported(region)

    def test_unsupported_model_region_fails_before_credentials(self, monkeypatch):
        monkeypatch.setattr(provider, "REGION", "cn-northwest-1")
        with pytest.raises(ValueError, match="independent"):
            provider.build_model(1000)

    def test_provider_has_no_token_cache_or_import_time_region_check(self):
        tree = ast.parse(Path(provider.__file__).read_text())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not names.intersection({"refresh_token", "token_cache", "_expires_at", "is_expired"})
        for node in tree.body:
            assert not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call)

    def test_no_step_sets_temperature(self):
        for name in ("agents.py", "steps/agent_steps.py", "steps/call.py"):
            tree = ast.parse((BACKEND / name).read_text())
            for node in ast.walk(tree):
                assert not (isinstance(node, ast.Constant) and node.value == "temperature")
                assert not (isinstance(node, ast.keyword) and node.arg == "temperature")


class TestKimiK3ChatCompletions:
    @pytest.mark.parametrize("effort,expected", [
        (None, "medium"), ("none", "none"), ("low", "low"), ("medium", "medium"),
        ("high", "high"), ("xhigh", "xhigh"), ("max", "max"),
    ])
    def test_official_endpoint_model_and_explicit_effort(self, monkeypatch, effort, expected):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        model = provider.build_model(32000, reasoning_effort=effort, model_id=K3)
        assert isinstance(model, OpenAIModel)
        assert model.client_args["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
        assert model.client_args["api_key"] == "offline-test-token"
        assert model.client_args["max_retries"] == 1
        timeout = model.client_args["timeout"]
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5, 180, 30, 5)
        request = model.format_request([{"role": "user", "content": [{"text": "Hi"}]}])
        assert request["model"] == "us.moonshotai.kimi-k3"
        assert request["reasoning_effort"] == expected
        assert request["max_tokens"] == 32000
        assert "reasoning" not in request
        assert "temperature" not in request
        assert model._bedrock_mantle_config is None

    @pytest.mark.parametrize("budget,expected", [(1, 1), (128, 128), (32768, 32768), (100000, 32768)])
    def test_output_budget_is_bounded(self, monkeypatch, budget, expected):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        assert provider.build_model(budget, model_id=K3).config["params"]["max_tokens"] == expected

    @pytest.mark.parametrize("effort", ["minimal", "", "HIGH", "unsupported"])
    def test_invalid_effort_fails_before_authentication(self, monkeypatch, effort):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        monkeypatch.setattr(boto3, "Session", Mock(side_effect=AssertionError("No session")))
        with pytest.raises(ValueError, match="reasoning_effort"):
            provider.build_model(128, reasoning_effort=effort, model_id=K3)

    @pytest.mark.parametrize("budget", [0, -1])
    def test_invalid_budget(self, monkeypatch, budget):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        with pytest.raises(ValueError, match="positive"):
            provider.build_model(budget, model_id=K3)

    def test_secret_is_used_raw_and_rotated_per_new_model(self, secret_auth, monkeypatch):
        _, _, client = secret_auth
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        client.get_secret_value.side_effect = [
            {"ARN": SECRET_ARN, "SecretString": '{"token":"ABSKsynthetic-old"}'},
            {"ARN": SECRET_ARN, "SecretString": '{"token":"ABSKsynthetic-new"}'},
            RuntimeError("ABSKsynthetic-private-error"),
        ]
        first = provider.build_model(128, model_id=K3)
        second = provider.build_model(128, model_id=K3)
        assert first.client_args["api_key"] == "ABSKsynthetic-old"
        assert second.client_args["api_key"] == "ABSKsynthetic-new"
        with pytest.raises(RuntimeError, match="Unable to resolve model secret") as error:
            provider.build_model(128, model_id=K3)
        assert "ABSKsynthetic-private-error" not in "".join(traceback.format_exception(error.value))
        import os
        assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "offline-test-token"
        assert client.get_secret_value.call_count == 3

    @pytest.mark.parametrize("token", [None, "", "bad\nheader", "bad token"])
    def test_bad_bearer_fails_without_fallback(self, monkeypatch, token):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        if token is None:
            monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
        else:
            monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", token)
        constructor = Mock()
        monkeypatch.setattr(provider, "_K3OpenAIModel", constructor)
        with pytest.raises(RuntimeError, match="valid Bedrock bearer token"):
            provider.build_model(128, model_id=K3)
        constructor.assert_not_called()

    def test_profile_without_credentials_does_not_use_bearer(self, explicit_profile, monkeypatch, tmp_path):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        (tmp_path / "credentials").write_text("[overseas-test]\n")
        with pytest.raises(RuntimeError, match="ambient fallback is disabled"):
            provider.build_model(128, model_id=K3)

    def test_mantle_never_falls_back_for_k3(self, monkeypatch):
        monkeypatch.setattr(provider, "AUTH_MODE", "mantle")
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        with pytest.raises(RuntimeError, match="Kimi K3 requires explicit"):
            provider.build_model(128, model_id=K3)

    def test_catalogue_label_selection_and_current_model_accessor(self, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", f"{K3},{GPT}")
        monkeypatch.setattr(boto3, "Session", Mock(side_effect=AssertionError("No AWS session")))
        assert {"id": K3, "label": "kimi-k3"} in provider.list_models()["models"]
        assert provider.resolve_model_id(K3) == K3
        with provider.use_model(K3):
            assert provider.current_model_id() == K3
            assert provider.build_model(128).config["model_id"] == "us.moonshotai.kimi-k3"
            assert provider.build_model(128, model_id=KIMI).config["model_id"] == KIMI
        assert provider.current_model_id() == KIMI

    def test_profile_catalogue_maps_foundation_id_to_canonical_us_id(self, explicit_profile, monkeypatch):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        session = Mock()
        session.client.return_value.list_foundation_models.return_value = {
            "modelSummaries": [{"modelId": KIMI}, {"modelId": "moonshotai.kimi-k3"}],
        }
        monkeypatch.setattr(provider, "_profile_session", lambda: session)
        assert {"id": K3, "label": "kimi-k3"} in provider.list_models()["models"]
        assert provider.resolve_model_id(K3) == K3
        with pytest.raises(ValueError, match="not available"):
            provider.resolve_model_id("moonshotai.kimi-k3")

    def test_canonical_us_id_can_be_configured_as_default(self, monkeypatch):
        monkeypatch.setattr(provider, "MODEL_ID", K3)
        assert provider.list_models() == {
            "models": [{"id": K3, "label": "kimi-k3"}], "default_model_id": K3,
        }
        assert provider.current_model_id() == K3
        assert provider.build_model(128).config["model_id"] == K3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("auth", ["bearer", "profile"])
    async def test_actual_openai_httpx_two_turn_tool_cycle(self, monkeypatch, tmp_path, caplog, auth):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        token = "ABSKsynthetic-bearer-private"
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", token)
        if auth == "profile":
            monkeypatch.setattr(provider, "AUTH_MODE", "profile")
            monkeypatch.setenv("IELTS_MODEL_PROFILE", "overseas-test")
            monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-china-key")
            monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-china-private")
            (tmp_path / "credentials").write_text(
                "[overseas-test]\naws_access_key_id = offline-key\naws_secret_access_key = offline-secret\n"
                "aws_session_token = synthetic-profile-private\n"
            )
        model = provider.build_model(128, reasoning_effort="none", model_id=K3)
        model.update_config(stream=False)
        wire_requests = []

        async def handle(transport, request):
            assert str(request.url) == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions"
            if auth == "bearer":
                assert request.headers["Authorization"] == "Bearer " + token
                assert "x-amz-security-token" not in request.headers
            else:
                authorization = request.headers["Authorization"]
                assert authorization.startswith("AWS4-HMAC-SHA256 Credential=offline-key/")
                assert "/us-east-1/bedrock/aws4_request" in authorization
                assert "ambient-china-key" not in authorization
                assert request.headers["x-amz-security-token"] == "synthetic-profile-private"
            body = json.loads(request.content)
            wire_requests.append(body)
            assert body["model"] == "us.moonshotai.kimi-k3"
            assert body["reasoning_effort"] == "none"
            assert body["max_tokens"] == 128
            if len(wire_requests) == 1:
                message = {
                    "role": "assistant", "content": None, "reasoning_content": "synthetic reasoning",
                    "tool_calls": [{"id": "call_echo", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text":"hello"}'}}],
                }
                finish = "tool_calls"
            else:
                message, finish = {"role": "assistant", "content": '{"ok":true}'}, "stop"
            return httpx.Response(200, request=request, json={
                "id": "offline", "created": 0, "model": "us.moonshotai.kimi-k3", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            })

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
        tools = [{"name": "echo", "description": "Echo", "inputSchema": {"json": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        }}}]
        messages = [{"role": "user", "content": [{"text": "Echo hello"}]}]
        with caplog.at_level(logging.DEBUG):
            first = [event async for event in model.stream(messages, tool_specs=tools)]
            messages += [
                {"role": "assistant", "content": [
                    {"reasoningContent": {"reasoningText": {"text": "synthetic reasoning"}}},
                    {"toolUse": {"toolUseId": "call_echo", "name": "echo", "input": {"text": "hello"}}},
                ]},
                {"role": "user", "content": [{"toolResult": {
                    "toolUseId": "call_echo", "status": "success", "content": [{"text": "hello"}],
                }}]},
            ]
            second = [event async for event in model.stream(messages, tool_specs=tools)]
        assert len(wire_requests) == 2
        assert any(event.get("contentBlockStart", {}).get("start", {}).get("toolUse", {}).get("name") == "echo"
                   for event in first)
        assert any(event.get("contentBlockDelta", {}).get("delta", {}).get("text") == '{"ok":true}'
                   for event in second)
        assert wire_requests[1]["messages"][-1]["role"] == "tool"
        assert wire_requests[1]["messages"][-1]["tool_call_id"] == "call_echo"
        for private in (token, "synthetic-profile-private", "ambient-china-private"):
            assert private not in caplog.text
        import os
        assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == token
        assert "api_key" not in model.client_args if auth == "profile" else model.client_args["api_key"] == token

    def test_profile_sigv4_matches_real_botocore_and_refreshes_each_sign(self):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
        credentials = [Credentials("first-key", "first-secret"), Credentials("second-key", "second-secret")]
        loader = Mock()
        loader.load.side_effect = credentials
        auth = provider._K3ProfileAuth(loader, "us-east-1")
        for credential in credentials:
            request = httpx.Request(
                "POST", "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer sigv4"}, json={"model": "us.moonshotai.kimi-k3"},
            )
            auth._sign(request)
            assert f"Credential={credential.access_key}/" in request.headers["Authorization"]
            expected = AWSRequest(
                method=request.method, url=str(request.url), data=request.content,
                headers={k: v for k, v in request.headers.items() if k != "authorization"},
            )
            SigV4Auth(credential, "bedrock", "us-east-1").add_auth(expected)
            assert request.headers["Authorization"] == expected.headers["Authorization"]
        assert loader.load.call_count == 2

    @pytest.mark.asyncio
    async def test_sdk_error_body_is_redacted(self, monkeypatch, caplog):
        monkeypatch.setenv("IELTS_ALLOWED_MODEL_IDS", K3)
        private = "ABSKsynthetic-private-token"
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", private)
        model = provider.build_model(128, model_id=K3)
        calls = []

        async def handle(transport, request):
            calls.append(request)
            return httpx.Response(401, request=request, json={"error": {"message": private, "type": "auth_error"}})

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(RuntimeError, match="Kimi K3 Chat Completions request failed") as error:
                _ = [event async for event in model.stream([{"role": "user", "content": [{"text": "Hi"}]}])]
        assert private not in "".join(traceback.format_exception(error.value)) + caplog.text
        assert "AuthenticationError" in str(error.value)
        assert "status_code=401" in str(error.value)
        assert len(calls) == 1
