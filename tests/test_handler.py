import json
from dataclasses import dataclass

import pytest

import src.handler as handler_module
from src.handler import handler
from src.pattern import NotFoundError, Resource


@dataclass
class LambdaContext:
    function_name: str = "test"
    memory_limit_in_mb: int = 128
    invoked_function_arn: str = "arn:aws:lambda:eu-west-1:123456789012:function:test"
    aws_request_id: str = "da658bd3-2d6f-4e7b-8ec2-937234644fdc"


@pytest.fixture
def lambda_context() -> LambdaContext:
    return LambdaContext()


@pytest.fixture(autouse=True)
def mock_enabled_user_profile(monkeypatch):
    def fake_get_item(*args, **kwargs):
        return {"Item": {"userStatus": {"S": "ENABLED"}, "timeToLive": {"N": "4102444800"}}}

    monkeypatch.setattr(
        "src.utils.boto3.client",
        lambda *args, **kwargs: type("Client", (), {"get_item": staticmethod(fake_get_item)})(),
    )


def _parse_body(response):
    body = response.get("body")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body
    return body


def test_health_get(lambda_context):
    expected_response_status_code = 200

    minimal_event = {
        "path": "/health",
        "httpMethod": "GET",
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    assert actual["statusCode"] == expected_response_status_code


def test_create_thing_valid_request(monkeypatch, lambda_context):
    expected_response_status_code = 201

    monkeypatch.setattr(handler_module.Thing, "create", lambda **kwargs: None)

    minimal_event = {
        "path": "/things",
        "httpMethod": "POST",
        "headers": {"User-Id": "test-user"},
        "body": '{"thingName": "foo"}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["thingId"] is not None
    assert "multiValueHeaders" in actual and "Location" in actual["multiValueHeaders"]
    assert actual["multiValueHeaders"]["Location"][0] is not None


def test_create_thing_preserves_additional_properties(monkeypatch, lambda_context):
    captured = {}
    fixed_now = 1700000000

    def fake_create(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(handler_module.time, "time", lambda: fixed_now)
    monkeypatch.setattr(handler_module.Thing, "create", fake_create)

    minimal_event = {
        "path": "/things",
        "httpMethod": "POST",
        "headers": {"User-Id": "test-user"},
        "body": '{"thingName": "foo", "hello": "world", "timeToLive": 123}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)

    assert actual["statusCode"] == 201
    assert captured["thingName"] == "foo"
    assert captured["hello"] == "world"
    assert captured["timeToLive"] != 123
    assert captured["timeToLive"] == fixed_now + 86400


def test_create_thing_missing_user_id(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 1

    minimal_event = {
        "path": "/things",
        "httpMethod": "POST",
        "body": '{"thingName": "foo"}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_create_thing_user_not_enabled(monkeypatch, lambda_context):
    expected_response_status_code = 403
    expected_error_id = 4

    def fake_get_item(*args, **kwargs):
        return {"Item": {"userStatus": {"S": "DISABLED"}}}

    monkeypatch.setattr("src.utils.boto3.client", lambda *args, **kwargs: type("Client", (), {"get_item": staticmethod(fake_get_item)})())

    minimal_event = {
        "path": "/things",
        "httpMethod": "POST",
        "headers": {"User-Id": "test-user"},
        "body": '{"thingName": "foo"}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_register_user_valid_request(monkeypatch, lambda_context):
    captured = {}
    fixed_now = 1700000000

    def fake_update_item(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(handler_module.time, "time", lambda: fixed_now)
    monkeypatch.setattr(handler_module.dynamodb_client, "update_item", fake_update_item)

    register_event = {
        "path": "/register",
        "httpMethod": "POST",
        "headers": {"User-Id": "TestUser123"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(register_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == 200
    assert body["userId"] == "TestUser123"
    assert "userExpirationTimestamp" in body
    assert "timeToLive" not in body
    assert body["userExpirationTimestamp"] == fixed_now + 3600
    assert captured["Key"]["PK"]["S"].endswith("USER:TestUser123:PROFILE")
    assert "timeToLive" in captured["UpdateExpression"]
    assert captured["ExpressionAttributeValues"][":time_to_live"]["N"] == str(fixed_now + 3600)


def test_register_user_rejects_non_alphanumeric_user_id(lambda_context):
    register_event = {
        "path": "/register",
        "httpMethod": "POST",
        "headers": {"User-Id": "bad-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(register_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == 400
    assert body["errorId"] == 6


def test_register_user_missing_user_id_header(lambda_context):
    register_event = {
        "path": "/register",
        "httpMethod": "POST",
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(register_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == 400
    assert body["errorId"] == 1


def test_create_thing_missing_json_body(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 2

    minimal_event = {
        "path": "/things",
        "httpMethod": "POST",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_create_thing_missing_thing_name(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 2

    minimal_event = {
        "path": "/things",
        "httpMethod": "POST",
        "headers": {"User-Id": "test-user"},
        "body": '{"notThingName": "foo"}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_query_things_valid_request(lambda_context):
    expected_response_status_code = 200

    minimal_event = {
        "path": "/things",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert "things" in body and isinstance(body["things"], list)


def test_query_things_with_existing_things(monkeypatch, lambda_context):
    monkeypatch.setattr(
        handler_module.Thing,
        "query",
        lambda *args, **kwargs: [
            Resource(thingId="thing-123", thingName="foo", thingStatus="ACTIVE", hello="world", timeToLive=4102444800)
        ],
    )

    query_event = {
        "path": "/things",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(query_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == 200
    assert "things" in body and isinstance(body["things"], list)
    assert any(thing.get("thingName") == "foo" for thing in body["things"])
    assert all("thingStatus" not in thing for thing in body["things"])
    assert all("timeToLive" not in thing for thing in body["things"])
    assert any(thing.get("hello") == "world" for thing in body["things"])


def test_query_things_does_not_manually_filter_profile_id(monkeypatch, lambda_context):
    def fake_query(*args, **kwargs):
        return [
            Resource(thingId="thing-1", thingName="real", thingStatus="ACTIVE"),
            Resource(thingId="PROFILE", thingName="ignored", thingStatus="ENABLED")
        ]

    monkeypatch.setattr(handler_module.Thing, "query", fake_query)

    query_event = {
        "path": "/things",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(query_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == 200
    assert any(item.get("thingId") == "PROFILE" for item in body["things"])


def test_query_things_missing_user_id(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 1

    minimal_event = {
        "path": "/things",
        "httpMethod": "GET",
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_query_things_rejects_expired_registration(monkeypatch, lambda_context):
    def fake_get_item(*args, **kwargs):
        return {"Item": {"userStatus": {"S": "ENABLED"}, "timeToLive": {"N": "1"}}}

    monkeypatch.setattr("src.utils.boto3.client", lambda *args, **kwargs: type("Client", (), {"get_item": staticmethod(fake_get_item)})())

    query_event = {
        "path": "/things",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(query_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == 403
    assert body["errorId"] == 5


def test_get_thing_valid_request(monkeypatch, lambda_context):
    expected_response_status_code = 200
    expected_thing_id = "thing-123"
    expected_thing_name = "foo"

    monkeypatch.setattr(
        handler_module.Thing,
        "read",
        lambda *args, **kwargs: Resource(thingId=expected_thing_id, thingName=expected_thing_name, thingStatus="ACTIVE", timeToLive=4102444800),
    )

    minimal_event = {
        "path": f"/things/{expected_thing_id}",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["thingId"] == expected_thing_id
    assert body["thingName"] == expected_thing_name
    assert "timeToLive" not in body


def test_get_thing_does_not_manually_block_profile_id(monkeypatch, lambda_context):
    expected_response_status_code = 200
    expected_thing_id = "PROFILE"
    expected_thing_name = "foo"

    monkeypatch.setattr(
        handler_module.Thing,
        "read",
        lambda *args, **kwargs: Resource(thingId=expected_thing_id, thingName=expected_thing_name, thingStatus="ACTIVE"),
    )

    minimal_event = {
        "path": "/things/PROFILE",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["thingId"] == expected_thing_id
    assert body["thingName"] == expected_thing_name


def test_get_thing_missing_user_id(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 1
    thing_id = "thing-123"

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "GET",
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_get_thing_not_found(monkeypatch, lambda_context):
    expected_response_status_code = 404
    expected_error_id = 3
    thing_id = "non-existent-thing"

    monkeypatch.setattr(
        handler_module.Thing,
        "read",
        lambda *args, **kwargs: (_ for _ in ()).throw(NotFoundError("Item not found")),
    )

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_get_thing_deleted_returns_not_found(monkeypatch, lambda_context):
    expected_response_status_code = 404
    expected_error_id = 3
    thing_id = "thing-deleted"

    def fake_get_item(**kwargs):
        return {
            "Item": {
                "PK": {"S": "ENV:qa:USER:test-user"},
                "SK": {"S": thing_id},
                "thingId": {"S": thing_id},
                "thingName": {"S": "deleted thing"},
                "thingStatus": {"S": "DELETED"},
            }
        }

    monkeypatch.setattr(handler_module.dynamodb_client, "get_item", fake_get_item)

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "GET",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_modify_thing_valid_request(monkeypatch, lambda_context):
    expected_response_status_code = 204
    thing_id = "thing-123"
    updated_thing_name = "Updated"

    monkeypatch.setattr(
        handler_module.Thing,
        "read",
        lambda *args, **kwargs: Resource(thingId=thing_id, thingName="Original", thingStatus="ACTIVE"),
    )

    monkeypatch.setattr(
        handler_module.Thing,
        "modify",
        lambda *args, **kwargs: None,
    )

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "PATCH",
        "headers": {"User-Id": "test-user"},
        "body": f'{{"thingName": "{updated_thing_name}"}}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    assert actual["statusCode"] == expected_response_status_code


def test_modify_thing_missing_user_id(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 1
    thing_id = "thing-123"

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "PATCH",
        "body": '{"thingName": "Updated"}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_modify_thing_missing_json_body(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 2
    thing_id = "thing-123"

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "PATCH",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_modify_thing_not_found(monkeypatch, lambda_context):
    expected_response_status_code = 404
    expected_error_id = 3
    thing_id = "non-existent-thing"

    monkeypatch.setattr(
        handler_module.Thing,
        "read",
        lambda *args, **kwargs: (_ for _ in ()).throw(NotFoundError("Item not found")),
    )

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "PATCH",
        "headers": {"User-Id": "test-user"},
        "body": '{"thingName": "Updated"}',
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_delete_thing_valid_request(monkeypatch, lambda_context):
    expected_response_status_code = 204
    thing_id = "thing-123"

    monkeypatch.setattr(
        handler_module.Thing,
        "read",
        lambda *args, **kwargs: Resource(thingId=thing_id, thingName="Original", thingStatus="ACTIVE"),
    )

    monkeypatch.setattr(
        handler_module.Thing,
        "delete",
        lambda *args, **kwargs: None,
    )

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "DELETE",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    assert actual["statusCode"] == expected_response_status_code


def test_delete_thing_missing_user_id(lambda_context):
    expected_response_status_code = 400
    expected_error_id = 1
    thing_id = "thing-123"

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "DELETE",
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id


def test_delete_thing_not_found(monkeypatch, lambda_context):
    expected_response_status_code = 404
    expected_error_id = 3
    thing_id = "non-existent-thing"

    monkeypatch.setattr(
        handler_module.Thing,
        "read",
        lambda *args, **kwargs: (_ for _ in ()).throw(NotFoundError("Item not found")),
    )

    minimal_event = {
        "path": f"/things/{thing_id}",
        "httpMethod": "DELETE",
        "headers": {"User-Id": "test-user"},
        "requestContext": {"requestId": "227b78aa-779d-47d4-a48e-ce62120393b8"}
    }

    actual = handler(minimal_event, lambda_context)
    body = _parse_body(actual)

    assert actual["statusCode"] == expected_response_status_code
    assert body["errorId"] == expected_error_id
