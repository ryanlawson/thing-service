import json
import os
import time

import boto3
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.event_handler.middlewares import NextMiddleware


ERROR_RESPONSES = {
    1: {
        "errorId": 1,
        "errorMessage": "Missing required header: User-Id",
        "debugMessage": "The request is missing the required User-Id header for multi-tenant partitioning."
    },
    2: {
        "errorId": 2,
        "errorMessage": "Missing or invalid JSON body",
        "debugMessage": "The request is missing a valid JSON body. Ensure that the Content-Type header is set to application/json and that the body contains valid JSON."
    },
    3: {
        "errorId": 3,
        "errorMessage": "Thing not found",
        "debugMessage": "The requested thing could not be found for the current user partition."
    },
    4: {
        "errorId": 4,
        "errorMessage": "User is not enabled",
        "debugMessage": "The user profile must exist in the current environment and have userStatus set to ENABLED."
    },
    5: {
        "errorId": 5,
        "errorMessage": "User registration expired",
        "debugMessage": "The user registration has expired. Register again to continue operations."
    },
    6: {
        "errorId": 6,
        "errorMessage": "Invalid user-id",
        "debugMessage": "The user-id must contain alphanumeric characters only."
    }
}


def create_error_response(error_id: int) -> dict:
    return json.dumps(ERROR_RESPONSES.get(error_id, {"errorId": error_id}))


def validate_json_body(app: APIGatewayRestResolver, next_middleware: NextMiddleware) -> Response:
    if not app.current_event.json_body:
        return Response(status_code=400, body=create_error_response(2))
    return next_middleware(app)


def validate_user_id(app: APIGatewayRestResolver, next_middleware: NextMiddleware) -> Response:
    user_id = app.current_event.headers.get("User-Id")
    if not user_id:
        return Response(status_code=400, body=create_error_response(1))

    environment = os.getenv("ENVIRONMENT", "qa")
    partition_key = f"ENV:{environment}:USER:{user_id}"
    profile_partition_key = f"{partition_key}:PROFILE"
    table_name = os.environ.get("DYNAMODB_TABLE", "Things")
    dynamodb_client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    response = dynamodb_client.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": profile_partition_key},
            "SK": {"S": "PROFILE"}
        }
    )

    item = response.get("Item")
    if not item:
        return Response(status_code=403, body=create_error_response(4))

    user_status = item.get("userStatus", {}).get("S")
    if user_status != "ENABLED":
        return Response(status_code=403, body=create_error_response(4))

    expiration_raw = item.get("timeToLive", {}).get("N") or item.get("userExpirationEpoch", {}).get("N")
    current_epoch = int(time.time())
    if not expiration_raw or int(expiration_raw) <= current_epoch:
        return Response(status_code=403, body=create_error_response(5))

    app.append_context(PartitionKey=partition_key, UserId=user_id)
    return next_middleware(app)
