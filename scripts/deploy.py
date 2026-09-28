#!/usr/bin/env python3
"""Deploy the image service to LocalStack (or any AWS-compatible endpoint).

Creates / updates, idempotently:
  * S3 bucket (encrypted, private, CORS for browser uploads)
  * DynamoDB table + GSI (same definition the tests use)
  * IAM role with a least-privilege policy
  * 5 Lambda functions (4 API routes + S3 upload-confirmation trigger)
  * API Gateway REST API with Lambda proxy integrations, stage "dev"
  * S3 ObjectCreated -> process-upload Lambda notification

Usage:  python scripts/deploy.py            (defaults to http://localhost:4566)
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from image_service.schema import table_definition  # noqa: E402

ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4566")
# What clients (curl, browser) use to reach S3; baked into presigned URLs.
PUBLIC_S3_ENDPOINT = os.environ.get("PUBLIC_S3_ENDPOINT", ENDPOINT)
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
PREFIX = os.environ.get("SERVICE_NAME", "image-service")
STAGE = os.environ.get("STAGE", "dev")
TABLE_NAME = f"{PREFIX}-images"
BUCKET_NAME = f"{PREFIX}-images-{STAGE}"
ACCOUNT_ID = "000000000000"  # LocalStack default account
RUNTIME = "python3.11"

API_FUNCTIONS = {
    # function suffix: (handler, http method, resource path)
    "upload": ("image_service.handlers.upload_image", "POST", "/images"),
    "list": ("image_service.handlers.list_images", "GET", "/images"),
    "get": ("image_service.handlers.get_image", "GET", "/images/{image_id}"),
    "delete": ("image_service.handlers.delete_image", "DELETE", "/images/{image_id}"),
}
TRIGGER_FUNCTION = ("process-upload", "image_service.handlers.process_upload")

session = boto3.session.Session(
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    region_name=REGION,
)


def client(name: str):
    return session.client(name, endpoint_url=ENDPOINT)


def log(msg: str) -> None:
    print(f"  • {msg}", flush=True)


def error_code(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Code", "")


# ---------------------------------------------------------------- storage
def ensure_bucket() -> None:
    s3 = client("s3")
    try:
        s3.create_bucket(Bucket=BUCKET_NAME)
        log(f"created bucket {BUCKET_NAME}")
    except ClientError as err:
        if error_code(err) not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
        log(f"bucket {BUCKET_NAME} exists")
    s3.put_public_access_block(
        Bucket=BUCKET_NAME,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=BUCKET_NAME,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_cors(
        Bucket=BUCKET_NAME,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedMethods": ["GET", "PUT"],
                    "AllowedOrigins": ["*"],
                    "AllowedHeaders": ["*"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        },
    )


def ensure_table() -> None:
    ddb = client("dynamodb")
    try:
        ddb.create_table(**table_definition(TABLE_NAME))
        log(f"created table {TABLE_NAME}")
    except ClientError as err:
        if error_code(err) != "ResourceInUseException":
            raise
        log(f"table {TABLE_NAME} exists")
    ddb.get_waiter("table_exists").wait(TableName=TABLE_NAME)


# -------------------------------------------------------------------- IAM
def ensure_role() -> str:
    iam = client("iam")
    role_name = f"{PREFIX}-lambda-role"
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
        ],
    }
    table_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TABLE_NAME}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                "Resource": [table_arn, f"{table_arn}/index/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{BUCKET_NAME}/images/*",
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "*",
            },
        ],
    }
    try:
        arn = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(assume))["Role"]["Arn"]
        log(f"created role {role_name}")
    except ClientError as err:
        if error_code(err) != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=role_name, PolicyName="least-privilege", PolicyDocument=json.dumps(policy))
    return arn


# ----------------------------------------------------------------- Lambda
def build_package() -> bytes:
    """Zip src/image_service. No third-party deps: boto3 comes with the runtime."""
    buffer = io.BytesIO()
    package_dir = ROOT / "src" / "image_service"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(package_dir.rglob("*.py")):
            zf.write(path, path.relative_to(ROOT / "src").as_posix())
    return buffer.getvalue()


def ensure_function(name: str, handler: str, role_arn: str, code: bytes) -> str:
    lam = client("lambda")
    env = {
        "Variables": {
            "TABLE_NAME": TABLE_NAME,
            "BUCKET_NAME": BUCKET_NAME,
            "PUBLIC_S3_ENDPOINT": PUBLIC_S3_ENDPOINT,
            "LOG_LEVEL": "INFO",
        }
    }
    config = dict(
        FunctionName=name,
        Runtime=RUNTIME,
        Handler=handler,
        Role=role_arn,
        Timeout=30,
        MemorySize=512,
        Environment=env,
    )
    try:
        arn = lam.create_function(Code={"ZipFile": code}, **config)["FunctionArn"]
        log(f"created lambda {name}")
    except ClientError as err:
        if error_code(err) != "ResourceConflictException":
            raise
        lam.update_function_code(FunctionName=name, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(**config)
        arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
        log(f"updated lambda {name}")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return arn


def allow_invoke(function_name: str, principal: str, source_arn: str, statement_id: str) -> None:
    try:
        client("lambda").add_permission(
            FunctionName=function_name,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal=principal,
            SourceArn=source_arn,
        )
    except ClientError as err:
        if error_code(err) != "ResourceConflictException":
            raise


# ------------------------------------------------------------ API Gateway
def deploy_api(function_arns: dict) -> str:
    apigw = client("apigateway")
    api_name = f"{PREFIX}-api"
    # Recreate the API on every deploy: simplest way to stay idempotent locally.
    for api in apigw.get_rest_apis(limit=500).get("items", []):
        if api["name"] == api_name:
            apigw.delete_rest_api(restApiId=api["id"])
    api_id = apigw.create_rest_api(name=api_name, endpointConfiguration={"types": ["REGIONAL"]})["id"]
    root_id = next(r["id"] for r in apigw.get_resources(restApiId=api_id)["items"] if r["path"] == "/")

    images_id = apigw.create_resource(restApiId=api_id, parentId=root_id, pathPart="images")["id"]
    image_id = apigw.create_resource(restApiId=api_id, parentId=images_id, pathPart="{image_id}")["id"]
    resources = {"/images": images_id, "/images/{image_id}": image_id}

    for suffix, (_, method, path) in API_FUNCTIONS.items():
        resource_id = resources[path]
        fn_arn = function_arns[suffix]
        apigw.put_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod=method,
            authorizationType="NONE",  # production: COGNITO_USER_POOLS authorizer
            requestParameters={"method.request.path.image_id": True} if "{image_id}" in path else {},
        )
        apigw.put_integration(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod=method,
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{fn_arn}/invocations",
        )
        allow_invoke(
            fn_arn.split(":")[-1],
            "apigateway.amazonaws.com",
            f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{api_id}/*/{method}{path.replace('{image_id}', '*')}",
            f"apigw-{api_id}-{suffix}",
        )
        log(f"route {method:6} {path}  ->  {PREFIX}-{suffix}")

    apigw.create_deployment(restApiId=api_id, stageName=STAGE)
    return api_id


def wire_s3_trigger(function_arn: str) -> None:
    allow_invoke(function_arn.split(":")[-1], "s3.amazonaws.com", f"arn:aws:s3:::{BUCKET_NAME}", "s3-object-created")
    client("s3").put_bucket_notification_configuration(
        Bucket=BUCKET_NAME,
        NotificationConfiguration={
            "LambdaFunctionConfigurations": [
                {
                    "LambdaFunctionArn": function_arn,
                    "Events": ["s3:ObjectCreated:*"],
                    "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "images/"}]}},
                }
            ]
        },
    )
    log("S3 ObjectCreated -> process-upload")


# ----------------------------------------------------------------- output
def resolve_api_url(api_id: str) -> str:
    """LocalStack exposes REST APIs under two URL formats depending on version; use the one that answers."""
    candidates = [
        f"{ENDPOINT}/_aws/execute-api/{api_id}/{STAGE}",
        f"{ENDPOINT}/restapis/{api_id}/{STAGE}/_user_request_",
    ]
    for _ in range(30):  # first call can be slow while LocalStack starts the Lambda container
        for base in candidates:
            try:
                with urllib.request.urlopen(f"{base}/images?limit=1", timeout=30) as resp:
                    if resp.status == 200:
                        return base
            except urllib.error.HTTPError as err:
                if err.code not in (404, 403):
                    return base  # the API answered (e.g. 4xx from our own validation)
            except (urllib.error.URLError, OSError):
                pass
        time.sleep(2)
    print("  ! could not verify the API URL; using the default format", file=sys.stderr)
    return candidates[0]


def main() -> None:
    print(f"Deploying {PREFIX} to {ENDPOINT} ({REGION})")
    ensure_bucket()
    ensure_table()
    role_arn = ensure_role()
    code = build_package()
    log(f"lambda package: {len(code) / 1024:.1f} KiB")

    arns = {
        suffix: ensure_function(f"{PREFIX}-{suffix}", handler, role_arn, code)
        for suffix, (handler, _, _) in API_FUNCTIONS.items()
    }
    trigger_arn = ensure_function(f"{PREFIX}-{TRIGGER_FUNCTION[0]}", TRIGGER_FUNCTION[1], role_arn, code)
    wire_s3_trigger(trigger_arn)

    api_id = deploy_api(arns)
    if os.environ.get("SKIP_API_CHECK"):
        api_url = f"{ENDPOINT}/_aws/execute-api/{api_id}/{STAGE}"
    else:
        api_url = resolve_api_url(api_id)
    (ROOT / ".api_url").write_text(api_url + "\n")
    print(f"\nAPI ready: {api_url}/images\n(saved to .api_url)")


if __name__ == "__main__":
    main()
