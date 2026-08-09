import logging
import os
from enum import Enum

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, force=True)
logger = logging.getLogger("thing_service.pattern")

class FieldType(Enum):
    IDENTIFIER = "identifier"
    READ_ONLY = "read_only"
    OPTIONAL = "optional"
    REQUIRED = "required"
    QUERYABLE = "queryable"
    EDITABLE = "editable"
    PARTITION_PARENT = "partition_parent"

class Operations(Enum):
    CREATE = "create"
    READ = "read"
    MODIFY = "modify"
    DELETE = "delete"
    QUERY = "query"

class NotFoundError(Exception):
    pass

class Resource:
    def __init__(self, **fields: dict[str, any]):
        self.fields = fields
        for field_name, field_value in fields.items():
            setattr(self, field_name, field_value)

    def to_dict(self, collection_field_name: str | None = None, ttl_property_name: str = "timeToLive"):
        excluded = {"PK", "SK", ttl_property_name}
        if collection_field_name:
            excluded.add(f"{collection_field_name}Status")
        return {
            key: value
            for key, value in self.fields.items()
            if key not in excluded
        }

class ResourcePattern:
    @staticmethod
    def _normalize_field_name(field_name: str) -> str:
        if not field_name:
            return field_name
        if field_name[0].isupper() and len(field_name) > 1:
            return f"{field_name[0].lower()}{field_name[1:]}"
        return field_name

    def __init__(self, collection_name, partition_parents: list[dict[str,str]], allowed_operations: list[Operations] = [Operations.CREATE, Operations.READ, Operations.MODIFY, Operations.DELETE, Operations.QUERY], table_name: str | None = None, ttl_property_name: str = "timeToLive", **fields: dict[str, list[FieldType]]):
        self.allowed_operations = allowed_operations
        self.collection_name = collection_name
        self.collection_field_name = self._normalize_field_name(collection_name)
        self.partition_parents = partition_parents
        self.table_name = table_name or os.environ.get("DYNAMODB_TABLE")
        self.ttl_property_name = self._normalize_field_name(ttl_property_name)
        self.fields = {self._normalize_field_name(key): value for key, value in fields.items()}
        self.fields[f"{self.collection_field_name}Id"] = [FieldType.IDENTIFIER, FieldType.READ_ONLY]
        self.fields[f"{self.collection_field_name}CreatedBy"] = [FieldType.READ_ONLY]
        self.fields[f"{self.collection_field_name}CreatedUtcTimestamp"] = [FieldType.READ_ONLY]
        self.fields[f"{self.collection_field_name}ModifiedBy"] = [FieldType.READ_ONLY]
        self.fields[f"{self.collection_field_name}ModifiedUtcTimestamp"] = [FieldType.READ_ONLY]
        self.fields[f"{self.collection_field_name}Status"] = [FieldType.READ_ONLY]
        self.projection_expression = ", ".join([f"#{field}" for field in self.fields.keys()])

        parts = []
        for parent in partition_parents:
            for parent_key, parent_value in parent.items():
                if isinstance(parent_value, str) and parent_value.startswith("{") and parent_value.endswith("}"):
                    parts.append(f"{parent_key}:{{{parent_value[1:-1]}}}")
                else:
                    parts.append(f"{parent_key}:{parent_value}")
        self.partition_key_pattern = ":".join(parts)

    @staticmethod
    def _to_dynamodb_value(value):
        if value is None:
            return {"NULL": True}
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {"N": str(value)}
        if isinstance(value, (list, tuple)):
            return {"L": [ResourcePattern._to_dynamodb_value(item) for item in value]}
        if isinstance(value, dict):
            return {"M": {str(key): ResourcePattern._to_dynamodb_value(val) for key, val in value.items()}}
        return {"S": str(value)}

    @staticmethod
    def _from_dynamodb_value(value):
        if not isinstance(value, dict):
            return value
        if "S" in value:
            return value["S"]
        if "N" in value:
            return int(value["N"]) if value["N"].lstrip("-").isdigit() else float(value["N"])
        if "BOOL" in value:
            return value["BOOL"]
        if "NULL" in value:
            return None
        if "M" in value:
            return {key: ResourcePattern._from_dynamodb_value(val) for key, val in value["M"].items()}
        if "L" in value:
            return [ResourcePattern._from_dynamodb_value(item) for item in value["L"]]
        if "SS" in value:
            return list(value["SS"])
        return value

    @staticmethod
    def _from_dynamodb_item(item, collection_field_name: str | None = None, ttl_property_name: str = "timeToLive"):
        cleaned_item = {}
        for key, value in item.items():
            normalized_key = ResourcePattern._normalize_field_name(key)
            if key in {"PK", "SK"}:
                continue
            if normalized_key == ttl_property_name:
                continue
            if collection_field_name and normalized_key == f"{collection_field_name}Status":
                continue
            cleaned_item[normalized_key] = ResourcePattern._from_dynamodb_value(value)
        return cleaned_item

    def to_dict(self, resource: Resource) -> dict:
        return resource.to_dict(self.collection_field_name, self.ttl_property_name)

    def create_partition_key(self, **parent_field_values: dict[str, str]) -> str:
        logger.debug("Building partition key for %s with parent values=%s", self.collection_name, parent_field_values)
        return self.partition_key_pattern.format(**parent_field_values)

    def create(self, dynamodb_client, request_context, parent_field_values: dict[str, str] = {}, table_name: str | None = None, **field_values: dict[str, str]) -> Resource:
        normalized_field_values = {self._normalize_field_name(key): value for key, value in field_values.items()}
        for field_name in normalized_field_values:
            self.fields.setdefault(field_name, [FieldType.OPTIONAL])
        partition_key_values = dict(parent_field_values)
        for parent in self.partition_parents:
            for parent_field in parent.values():
                partition_key_values.setdefault(parent_field, normalized_field_values.get(parent_field))

        partition_key = self.create_partition_key(**partition_key_values)
        sort_key = normalized_field_values[f"{self.collection_field_name}Id"]
        logger.info("Creating %s item for partition=%s sortKey=%s fields=%s", self.collection_name, partition_key, sort_key, sorted(normalized_field_values.keys()))
        expression_attribute_names = {}
        expression_attribute_values = {}

        dynamo_expression = "SET "
        for field, value in normalized_field_values.items():
            expression_attribute_names[f"#{field}"] = field
            expression_attribute_values[f":{field}"] = value
            dynamo_expression += f"#{field} = :{field}, "

        dynamo_expression += f"#{self.collection_field_name}CreatedBy = :created_by, "
        expression_attribute_names[f"#{self.collection_field_name}CreatedBy"] = f"{self.collection_field_name}CreatedBy"
        expression_attribute_values[":created_by"] = normalized_field_values.get(f"{self.collection_field_name}CreatedBy", "system")

        dynamo_expression += f"#{self.collection_field_name}CreatedUtcTimestamp = :created_utc_timestamp, "
        expression_attribute_names[f"#{self.collection_field_name}CreatedUtcTimestamp"] = f"{self.collection_field_name}CreatedUtcTimestamp"
        expression_attribute_values[":created_utc_timestamp"] = normalized_field_values.get(f"{self.collection_field_name}CreatedUtcTimestamp", "2024-01-01T00:00:00Z")

        dynamo_expression += f"#{self.collection_field_name}Status = :status"
        expression_attribute_names[f"#{self.collection_field_name}Status"] = f"{self.collection_field_name}Status"
        expression_attribute_values[":status"] = normalized_field_values.get(f"{self.collection_field_name}Status", "ACTIVE")

        dynamo_expression = dynamo_expression.rstrip(", ")

        try:
            dynamodb_client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": partition_key},
                    "SK": {"S": sort_key}
                },
                UpdateExpression=dynamo_expression,
                ExpressionAttributeNames=expression_attribute_names,
                ExpressionAttributeValues={key: ResourcePattern._to_dynamodb_value(value) for key, value in expression_attribute_values.items()},
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)"
            )
            logger.info("Create %s item succeeded: partition=%s sortKey=%s", self.collection_name, partition_key, sort_key)
        except Exception:
            logger.exception("Create %s item failed: partition=%s sortKey=%s", self.collection_name, partition_key, sort_key)
            raise
        return Resource(**normalized_field_values)

    def query(self, dynamodb_client, request_context, parent_field_values: dict[str, str] = {}, include_deleted=False, table_name: str | None = None) -> list[Resource]:
        partition_key = self.create_partition_key(**parent_field_values)
        logger.info("Querying %s items for partition=%s include_deleted=%s", self.collection_name, partition_key, include_deleted)
        params = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {
                ":pk": {"S": partition_key}
            },
        }

        if not include_deleted:
            params["FilterExpression"] = f"{self.collection_field_name}Status <> :deleted_status"
            params["ExpressionAttributeValues"][":deleted_status"] = {"S": "DELETED"}

        response = dynamodb_client.query(TableName=self.table_name, **params)
        items = response.get("Items", [])
        resources = [Resource(**ResourcePattern._from_dynamodb_item(item, self.collection_field_name, self.ttl_property_name)) for item in items]
        logger.info("Query for %s partition=%s returned %s items", self.collection_name, partition_key, len(resources))
        return resources

    def read(self, dynamodb_client, request_context, parent_field_values: dict[str, str], id, table_name: str | None = None) -> Resource:
        partition_key = self.create_partition_key(**parent_field_values)
        logger.info("Reading %s item: partition=%s sortKey=%s", self.collection_name, partition_key, id)
        response = dynamodb_client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": partition_key},
                "SK": {"S": id}
            }
        )
        item = response.get("Item")
        if not item:
            logger.warning("Read %s item not found: partition=%s sortKey=%s", self.collection_name, partition_key, id)
            raise NotFoundError("Item not found")
        status_field_name = f"{self.collection_field_name}Status"
        legacy_status_field_name = f"{self.collection_field_name[0].upper()}{self.collection_field_name[1:]}Status"
        raw_status_value = item.get(status_field_name) or item.get(legacy_status_field_name)
        status_value = ResourcePattern._from_dynamodb_value(raw_status_value) if raw_status_value is not None else None
        if status_value == "DELETED":
            logger.warning("Read %s item marked deleted: partition=%s sortKey=%s", self.collection_name, partition_key, id)
            raise NotFoundError("Item not found")
        item_value = ResourcePattern._from_dynamodb_item(item, self.collection_field_name, self.ttl_property_name)
        logger.info("Read %s item succeeded: partition=%s sortKey=%s", self.collection_name, partition_key, id)
        return Resource(**item_value)

    def modify(self, dynamodb_client, request_context, parent_field_values: dict[str, str], id, table_name: str | None = None, **field_values: dict[str, str]) -> None:
        normalized_field_values = {self._normalize_field_name(key): value for key, value in field_values.items()}
        for field_name in normalized_field_values:
            self.fields.setdefault(field_name, [FieldType.OPTIONAL])
        partition_key = self.create_partition_key(**parent_field_values)
        logger.info("Updating %s item: partition=%s sortKey=%s fields=%s", self.collection_name, partition_key, id, sorted(normalized_field_values.keys()))
        dynamo_expression = "SET "
        expression_attribute_names = {}
        expression_attribute_values = {}
        for i, (field, value) in enumerate(normalized_field_values.items()):
            expression_attribute_names[f"#{field}"] = field
            expression_attribute_values[f":val{i}"] = value
            dynamo_expression += f"#{field} = :val{i}, "

        dynamo_expression += f"#{self.collection_field_name}ModifiedBy = :modified_by, "
        expression_attribute_names[f"#{self.collection_field_name}ModifiedBy"] = f"{self.collection_field_name}ModifiedBy"
        expression_attribute_values[":modified_by"] = normalized_field_values.get(f"{self.collection_field_name}ModifiedBy", "system")

        dynamo_expression += f"#{self.collection_field_name}ModifiedUtcTimestamp = :modified_utc_timestamp, "
        expression_attribute_names[f"#{self.collection_field_name}ModifiedUtcTimestamp"] = f"{self.collection_field_name}ModifiedUtcTimestamp"
        expression_attribute_values[":modified_utc_timestamp"] = normalized_field_values.get(f"{self.collection_field_name}ModifiedUtcTimestamp", "2024-01-01T00:00:00Z")

        dynamo_expression += f"#{self.collection_field_name}Status = :status"
        expression_attribute_names[f"#{self.collection_field_name}Status"] = f"{self.collection_field_name}Status"
        expression_attribute_values[":status"] = normalized_field_values.get(f"{self.collection_field_name}Status", "ACTIVE")

        dynamo_expression = dynamo_expression.rstrip(", ")

        try:
            dynamodb_client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": partition_key},
                    "SK": {"S": id}
                },
                UpdateExpression=dynamo_expression,
                ExpressionAttributeNames=expression_attribute_names,
                ExpressionAttributeValues={key: ResourcePattern._to_dynamodb_value(value) for key, value in expression_attribute_values.items()},
                ConditionExpression="attribute_exists(PK) AND attribute_exists(SK)"
            )
            logger.info("Update %s item succeeded: partition=%s sortKey=%s", self.collection_name, partition_key, id)
        except Exception:
            logger.exception("Update %s item failed: partition=%s sortKey=%s", self.collection_name, partition_key, id)
            raise

    def delete(self, dynamodb_client, request_context, parent_field_values: dict[str, str], id, table_name: str | None = None) -> None:
        partition_key = self.create_partition_key(**parent_field_values)
        logger.info("Deleting %s item: partition=%s sortKey=%s", self.collection_name, partition_key, id)
        expression_attribute_names = {
            f"#{self.collection_field_name}Status": f"{self.collection_field_name}Status",
            f"#{self.collection_field_name}ModifiedBy": f"{self.collection_field_name}ModifiedBy",
            f"#{self.collection_field_name}ModifiedUtcTimestamp": f"{self.collection_field_name}ModifiedUtcTimestamp"
        }
        expression_attribute_values = {
            ":status": "DELETED",
            ":modified_by": "system",
            ":modified_utc_timestamp": "2024-01-01T00:00:00Z"
        }
        dynamo_expression = (
            f"SET #{self.collection_field_name}Status = :status, "
            f"#{self.collection_field_name}ModifiedBy = :modified_by, "
            f"#{self.collection_field_name}ModifiedUtcTimestamp = :modified_utc_timestamp"
        )

        try:
            dynamodb_client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": partition_key},
                    "SK": {"S": id}
                },
                UpdateExpression=dynamo_expression,
                ExpressionAttributeNames=expression_attribute_names,
                ExpressionAttributeValues={key: ResourcePattern._to_dynamodb_value(value) for key, value in expression_attribute_values.items()},
            )
            logger.info("Delete %s item succeeded: partition=%s sortKey=%s", self.collection_name, partition_key, id)
        except Exception:
            logger.exception("Delete %s item failed: partition=%s sortKey=%s", self.collection_name, partition_key, id)
            raise

class SingletonPattern:
    def __init__(self, property_name: str, parent_properties: list[dict[str, str]] = [], table_name: str | None = None, **fields: dict[str, list[FieldType]]):
        self.property_name = property_name
        self.parent_properties = parent_properties
        self.table_name = table_name or os.environ.get("DYNAMODB_TABLE", "Things")
        self.partition_key_pattern = ":".join([f"{parent_key}:{{{parent_field}}}" for parent in parent_properties for parent_key, parent_field in parent.items()])
        self.sort_key = property_name
        self.projection_expression = ", ".join([f"#{field}" for field in fields.keys()])

    def create_partition_key(self, **parent_field_values: dict[str, str]) -> str:
        return self.partition_key_pattern.format(**parent_field_values)

    def read(self, dynamodb_client, request_context, table_name: str | None = None, **parent_field_values: dict[str, str]) -> Resource:
        partition_key = self.create_partition_key(**parent_field_values)
        response = dynamodb_client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": partition_key},
                "SK": {"S": self.sort_key}
            },
            ProjectionExpression=self.projection_expression
        )
        item = response.get("Item")
        if not item:
            raise NotFoundError("Singleton not found")
        return Resource(**ResourcePattern._from_dynamodb_item(item, None))

    def modify(self, dynamodb_client, request_context, parent_field_values: dict[str, str] = {}, table_name: str | None = None, **field_values: dict[str, str]) -> None:
        partition_key = self.create_partition_key(**parent_field_values)
        dynamo_expression = "SET "
        expression_attribute_names = {}
        expression_attribute_values = {}
        for i, (field, value) in enumerate(field_values.items()):
            expression_attribute_names[f"#{field}"] = field
            expression_attribute_values[f":val{i}"] = value
            dynamo_expression += f"#{field} = :val{i}, "

        dynamo_expression += f"#{self.property_name}ModifiedBy = :modified_by, "
        expression_attribute_names[f"#{self.property_name}ModifiedBy"] = f"{self.property_name}ModifiedBy"
        expression_attribute_values[":modified_by"] = field_values.get(f"{self.property_name}ModifiedBy", "system")

        dynamo_expression += f"#{self.property_name}ModifiedUtcTimestamp = :modified_utc_timestamp"
        expression_attribute_names[f"#{self.property_name}ModifiedUtcTimestamp"] = f"{self.property_name}ModifiedUtcTimestamp"
        expression_attribute_values[":modified_utc_timestamp"] = field_values.get(f"{self.property_name}ModifiedUtcTimestamp", "2024-01-01T00:00:00Z")

        dynamo_expression = dynamo_expression.rstrip(", ")

        dynamodb_client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": partition_key},
                "SK": {"S": self.sort_key}
            },
            UpdateExpression=dynamo_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues={key: ResourcePattern._to_dynamodb_value(value) for key, value in expression_attribute_values.items()},
            ConditionExpression="attribute_exists(PK) AND attribute_exists(SK)"
        )

    def replace(self, dynamodb_client, request_context, parent_field_values: dict[str, str] = {}, table_name: str | None = None, **field_values: dict[str, str]) -> None:
        partition_key = self.create_partition_key(**parent_field_values)
        dynamo_expression = "SET "
        expression_attribute_names = {}
        expression_attribute_values = {}
        for i, (field, value) in enumerate(field_values.items()):
            expression_attribute_names[f"#{field}"] = field
            expression_attribute_values[f":val{i}"] = value
            dynamo_expression += f"#{field} = :val{i}, "

        dynamo_expression += f"#{self.property_name}ModifiedBy = :modified_by, "
        expression_attribute_names[f"#{self.property_name}ModifiedBy"] = f"{self.property_name}ModifiedBy"
        expression_attribute_values[":modified_by"] = field_values.get(f"{self.property_name}ModifiedBy", "system")

        dynamo_expression += f"#{self.property_name}ModifiedUtcTimestamp = :modified_utc_timestamp"
        expression_attribute_names[f"#{self.property_name}ModifiedUtcTimestamp"] = f"{self.property_name}ModifiedUtcTimestamp"
        expression_attribute_values[":modified_utc_timestamp"] = field_values.get(f"{self.property_name}ModifiedUtcTimestamp", "2024-01-01T00:00:00Z")

        dynamo_expression = dynamo_expression.rstrip(", ")

        dynamodb_client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": partition_key},
                "SK": {"S": self.sort_key}
            },
            UpdateExpression=dynamo_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues={key: ResourcePattern._to_dynamodb_value(value) for key, value in expression_attribute_values.items()},
        )

class FunctionPattern:
    def __init__(self, function_name: str, function: callable, clients: list[dict[str, any]] = [], parent_properties: list[dict[str, str]] = [], request_fields: list[dict[str, list[FieldType]]] = [], response_fields: list[dict[str, list[FieldType]]] = []):
        self.function_name = function_name
        self.clients = clients
        self.parent_properties = parent_properties
        self.function = function
        self.request_fields = request_fields
        self.response_fields = response_fields

    def call(self, *args, **kwargs):
        return self.function(*args, **kwargs)

    def create_partition_key(self, **parent_field_values: dict[str, str]) -> str:
        return self.partition_key_pattern.format(**parent_field_values)
