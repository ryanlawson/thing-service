import json
import logging
import os
import re
import time
import uuid

import boto3

from botocore.exceptions import ClientError
from http import HTTPStatus

from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response

from src.pattern import NotFoundError, ResourcePattern, FieldType
from src.utils import create_error_response, validate_json_body, validate_user_id

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, force=True)
logger = logging.getLogger("thing_service.handler")

app = APIGatewayRestResolver(strip_prefixes=["/thing-service/v1"])

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "Things")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "qa")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
REGISTRATION_TTL_SECONDS = 3600
THING_TTL_SECONDS = 86400

USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")

dynamodb_client = boto3.client('dynamodb', region_name=AWS_REGION)
sns_client = boto3.client('sns', region_name=AWS_REGION)
table = boto3.resource('dynamodb', region_name=AWS_REGION).Table(TABLE_NAME) if TABLE_NAME else None

Thing = ResourcePattern(
    collection_name="Thing",
    partition_parents=[{"ENV": ENVIRONMENT}, {"USER": "{userId}"}],
    table_name=TABLE_NAME,
    thingName=[FieldType.REQUIRED]
)

def handler(event, context):
    response = app.resolve(event, context)

    status_code = response.get("statusCode")
    if isinstance(status_code, int) and status_code >= HTTPStatus.BAD_REQUEST:
        user_id = (event.get("headers") or {}).get("User-Id") or "UNKNOWN"
        operation = event.get("requestContext", {}).get("operationName") or f"{event.get('httpMethod', 'UNKNOWN')} {event.get('path', '')}".strip()
        publish_error_notification(operation, user_id, status_code, response.get("body"))

    return response


def publish_success_notification(operation: str, user_id: str, thing_id: str | None = None) -> None:
    if not SNS_TOPIC_ARN:
        return

    message_payload = {
        "service": "thing-service",
        "environment": ENVIRONMENT,
        "operation": operation,
        "userId": user_id,
    }
    if thing_id:
        message_payload["thingId"] = thing_id

    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"thing-service {operation} succeeded",
            Message=json.dumps(message_payload),
        )
    except ClientError:
        logger.exception("Failed to publish success notification: operation=%s userId=%s thingId=%s", operation, user_id, thing_id)


def publish_error_notification(operation: str, user_id: str, status_code: int, response_body: str | dict | None = None) -> None:
    if not SNS_TOPIC_ARN:
        return

    message_payload = {
        "service": "thing-service",
        "environment": ENVIRONMENT,
        "operation": operation,
        "userId": user_id,
        "statusCode": status_code,
    }
    if response_body is not None:
        message_payload["responseBody"] = response_body

    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"thing-service {operation} failed",
            Message=json.dumps(message_payload),
        )
    except ClientError:
        logger.exception(
            "Failed to publish error notification: operation=%s userId=%s statusCode=%s",
            operation,
            user_id,
            status_code,
        )

@app.get("/health")
def health_check():
    publish_success_notification("HealthCheck", "SYSTEM")
    return Response(status_code=HTTPStatus.OK)


@app.post("/register")
def register_user():
    user_id = app.current_event.headers.get("User-Id")

    if not user_id:
        return Response(status_code=HTTPStatus.BAD_REQUEST, body=create_error_response(1))

    if not isinstance(user_id, str) or not USER_ID_PATTERN.fullmatch(user_id):
        return Response(status_code=HTTPStatus.BAD_REQUEST, body=create_error_response(6))

    registration_ttl_epoch = int(time.time()) + REGISTRATION_TTL_SECONDS
    profile_partition_key = f"ENV:{ENVIRONMENT}:USER:{user_id}:PROFILE"

    dynamodb_client.update_item(
        TableName=TABLE_NAME,
        Key={
            "PK": {"S": profile_partition_key},
            "SK": {"S": "PROFILE"},
        },
        UpdateExpression="SET userStatus = :status, timeToLive = :time_to_live",
        ExpressionAttributeValues={
            ":status": {"S": "ENABLED"},
            ":time_to_live": {"N": str(registration_ttl_epoch)},
        },
    )

    publish_success_notification("UserRegister", user_id)
    return Response(status_code=HTTPStatus.OK, body=json.dumps({"userId": user_id, "userExpirationTimestamp": registration_ttl_epoch}))

@app.post("/things", middlewares=[validate_user_id, validate_json_body])
def create_thing():
    user_id = app.context.get("UserId")
    logger.info("Create thing request received for userId=%s", user_id)
    request_body = app.current_event.json_body
    thing_name = request_body.get("thingName")
    if not thing_name:
        logger.warning("Create thing rejected: missing thingName in payload=%s", request_body)
        return Response(status_code=HTTPStatus.BAD_REQUEST, body=create_error_response(2))

    generated_thing_id = str(uuid.uuid4())
    create_values = {key: value for key, value in request_body.items() if key not in {"thingId", "timeToLive"}}
    thing_ttl_epoch = int(time.time()) + THING_TTL_SECONDS

    logger.info("Persisting new thing for userId=%s with thingId=%s", user_id, generated_thing_id)
    Thing.create(
        dynamodb_client = dynamodb_client,
        request_context = app.context,
        parent_field_values = {"userId": user_id},
        thingId = generated_thing_id,
        timeToLive = thing_ttl_epoch,
        **create_values
    )
    logger.info("Create thing succeeded: thingId=%s userId=%s", generated_thing_id, user_id)
    publish_success_notification("ThingCreate", user_id, generated_thing_id)
    return Response(status_code=HTTPStatus.CREATED, headers={"Location": f"/thing-service/v1/things/{generated_thing_id}"}, body=json.dumps({"thingId": generated_thing_id}))

@app.get("/things", middlewares=[validate_user_id])
def query_things():
    user_id = app.context.get("UserId")
    logger.info("Querying things for userId=%s", user_id)
    things = Thing.query(
        dynamodb_client = dynamodb_client,
        request_context = app.context,
        parent_field_values = {"userId": user_id},
        include_deleted = False
    )
    logger.info("Query things succeeded for userId=%s count=%s", user_id, len(things))
    publish_success_notification("ThingQuery", user_id)
    return {"things": [Thing.to_dict(thing) for thing in things]}

@app.get("/things/<thing_id>", middlewares=[validate_user_id])
def get_thing(thing_id: str):
    user_id = app.context.get("UserId")
    logger.info("Getting thingId=%s for userId=%s", thing_id, user_id)
    try:
        thing = Thing.read(
            dynamodb_client = dynamodb_client,
            request_context = app.context,
            parent_field_values = {"userId": user_id},
            id = thing_id
        )
    except NotFoundError:
        logger.warning("Get thing not found: thingId=%s userId=%s", thing_id, user_id)
        return Response(status_code=HTTPStatus.NOT_FOUND, body=create_error_response(3))

    logger.info("Get thing succeeded: thingId=%s userId=%s", thing_id, user_id)
    publish_success_notification("ThingGet", user_id, thing_id)
    return Thing.to_dict(thing)

@app.patch("/things/<thing_id>", middlewares=[validate_user_id, validate_json_body])
def modify_thing(thing_id: str):
    request_body = app.current_event.json_body
    update_values = {key: value for key, value in request_body.items() if key not in {"thingId", "timeToLive"}}
    user_id = app.context.get("UserId")
    logger.info("Updating thingId=%s userId=%s with fields=%s", thing_id, user_id, list(update_values.keys()))

    try:
        Thing.read(
            dynamodb_client = dynamodb_client,
            request_context = app.context,
            parent_field_values = {"userId": user_id},
            id = thing_id
        )
    except NotFoundError:
        logger.warning("Modify thing not found: thingId=%s userId=%s", thing_id, user_id)
        return Response(status_code=HTTPStatus.NOT_FOUND, body=create_error_response(3))

    try:
        Thing.modify(
            dynamodb_client = dynamodb_client,
            request_context = app.context,
            parent_field_values = {"userId": user_id},
            id = thing_id,
            **update_values
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            logger.warning("Modify thing not found after existence check: thingId=%s userId=%s", thing_id, user_id)
            return Response(status_code=HTTPStatus.NOT_FOUND, body=create_error_response(3))
        raise

    logger.info("Update thing succeeded: thingId=%s userId=%s", thing_id, user_id)
    publish_success_notification("ThingModify", user_id, thing_id)
    return Response(status_code=HTTPStatus.NO_CONTENT)

@app.delete("/things/<thing_id>", middlewares=[validate_user_id])
def delete_thing(thing_id: str):
    user_id = app.context.get("UserId")
    logger.info("Deleting thingId=%s userId=%s", thing_id, user_id)

    try:
        Thing.read(
            dynamodb_client = dynamodb_client,
            request_context = app.context,
            parent_field_values = {"userId": user_id},
            id = thing_id
        )
    except NotFoundError:
        logger.warning("Delete thing not found: thingId=%s userId=%s", thing_id, user_id)
        return Response(status_code=HTTPStatus.NOT_FOUND, body=create_error_response(3))

    try:
        Thing.delete(
            dynamodb_client = dynamodb_client,
            request_context = app.context,
            parent_field_values = {"userId": user_id},
            id = thing_id
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ConditionalCheckFailedException":
            logger.warning("Delete thing not found after existence check: thingId=%s userId=%s", thing_id, user_id)
            return Response(status_code=HTTPStatus.NOT_FOUND, body=create_error_response(3))
        raise

    logger.info("Delete thing succeeded: thingId=%s userId=%s", thing_id, user_id)
    publish_success_notification("ThingDelete", user_id, thing_id)
    return Response(status_code=HTTPStatus.NO_CONTENT)
