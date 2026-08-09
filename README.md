# Thing Service
A Service to Manage Things

## Usage

> [!WARNING]
> This service is provided as-is for demonstration purposes and is not intended for production use.
> Do not submit sensitive, confidential, or objectionable content.
> Use of this service is at your own risk. Access may be revoked at any time, and data may be removed at any time, without notice.

### Hosts

| Environment | Host                            |
|-------------|---------------------------------|
| `qa`        | `https://qa.api.ryanlawson.dev` |
| `prod`      | `https://api.ryanlawson.dev`    |

### Operations

A _Thing_ is a resource that can be managed through the service. A thing is required to have a name (`thingName`) and can have additional properties (`foo` in the examples) as needed.

#### Register a User

Before calling Thing operations, a user must be registered.

- `userId` must be alphanumeric only (`A-Z`, `a-z`, `0-9`).
- Registration returns a `userExpirationTimestamp` that is one hour from registration.
- Calling register again for the same `userId` resets the expiration to one hour from the new registration time.
- Thing operations with `User-Id` are rejected after expiration until the user is registered again.
- Thing records expire 24 hours after creation. Re-registering a user does not extend the expiration of existing Things.

Registers (or re-registers) a user and sets their expiration timestamp.

```http
POST /thing-service/v1/register
User-Id: {userId}
```

```http
HTTP/2 200 OK
{
  "userId": "{userId}",
  "userExpirationTimestamp": 1750000000
}
```

#### Create a Thing

Creates a new Thing and returns a generated `thingId`.

```http
POST /thing-service/v1/things
Content-Type: application/json
User-Id: {userId}
{
  "thingName": "string",
  "foo": "string"
}
```

```http
HTTP/2 201 Created
Location: /thing-service/v1/things/{thingId}
{
  "thingId": "{thingId}"
}
```

#### Query Things

Queries available Things.

```http
GET /thing-service/v1/things
User-Id: {userId}
```

```http
HTTP/2 200 OK
{
  "things": [
    {
      "thingId": "{thingId}",
      "thingName": "My Thing",
      "foo": "string"
    }
  ]
}
```

#### Get a Thing

Retrieves a Thing by its `thingId`.

```http
GET /thing-service/v1/things/{thingId}
User-Id: {userId}
```

```http
HTTP/2 200 OK
{
  "thingId": "{thingId}",
  "thingName": "My Thing",
  "foo": "string"
}
```

#### Modify a Thing

Modifies an existing Thing by its `thingId`. Any fields not provided in the request will remain unchanged.
This operation only affects the top-level properties of the Thing. Nested objects are not merged and will be replaced if provided.

```http
PATCH /thing-service/v1/things/{thingId}
Content-Type: application/json
User-Id: {userId}
{
  "thingName": "string",
  "foo": "string"
}
```

```http
HTTP/2 204 No Content
```

#### Delete a Thing

Deletes an existing Thing by its `thingId`.

```http
DELETE /thing-service/v1/things/{thingId}
User-Id: {userId}
```

```http
HTTP/2 204 No Content
```

### Error Responses

Error responses follow a standard format with:
- `errorId`: A unique identifier for the error.
- `errorMessage`: A human-readable message describing the error.
- `debugMessage`: A message intended for debugging purposes, which may contain additional details about the error. By default, this field is not returned in production.

```http
HTTP/2 400 Bad Request
{
  "errorId": 1,
  "errorMessage": "string",
  "debugMessage": "string"
}
```

| HTTP Status | Error ID | Error Description |
|-------------|----------|-------------------|
| `400`       | `1`      | Missing required header: `User-Id` |
| `400`       | `2`      | Missing or invalid JSON body |
| `404`       | `3`      | Thing not found |
| `403`       | `4`      | User is not enabled (not registered/enabled profile) |
| `403`       | `5`      | User registration expired |
| `400`       | `6`      | Invalid `userId` for registration (must be alphanumeric) |

## Development

Development instructions will be added here in the future.

## Support

For support, please contact [@ryanlawson](https://github.com/ryanlawson) or open an issue in the repository.
