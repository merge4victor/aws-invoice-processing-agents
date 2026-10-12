import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.stack import InvoiceProcessingStack


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return Template.from_stack(InvoiceProcessingStack(app, "TestStack"))


def test_stack_has_the_main_resources(template):
    template.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.resource_count_is("AWS::Events::Rule", 1)


def test_no_lambda_runs_the_workflow(template):
    # CDK adds two small helper functions that only run during deploy and
    # destroy. Nothing else in the stack should be a Lambda function.
    helpers = ("AutoDeleteObjects", "BucketNotificationsHandler")
    functions = template.find_resources("AWS::Lambda::Function")
    assert all(any(helper in name for helper in helpers) for name in functions)


def test_bucket_is_private_and_sends_events(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": Match.object_like(
                {"BlockPublicAcls": True, "RestrictPublicBuckets": True}
            )
        },
    )
    template.has_resource_properties(
        "Custom::S3BucketNotifications",
        {"NotificationConfiguration": {"EventBridgeConfiguration": {}}},
    )


def test_upload_rule_starts_the_workflow(template):
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": Match.object_like(
                {"source": ["aws.s3"], "detail-type": ["Object Created"]}
            )
        },
    )


def test_table_is_billed_per_request(template):
    template.has_resource_properties(
        "AWS::DynamoDB::Table", {"BillingMode": "PAY_PER_REQUEST"}
    )


def test_workflow_role_has_no_wildcard_actions(template):
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            assert "*" not in actions
            assert not any(action.endswith(":*") for action in actions), json.dumps(statement)


def test_workflow_role_allows_exactly_three_actions(template):
    policies = template.find_resources(
        "AWS::IAM::Policy",
        {"Properties": {"PolicyName": Match.string_like_regexp("InvoiceWorkflowRole")}},
    )
    assert len(policies) == 1
    statements = next(iter(policies.values()))["Properties"]["PolicyDocument"]["Statement"]
    actions = sorted(statement["Action"] for statement in statements)
    assert actions == ["bedrock:InvokeModel", "dynamodb:PutItem", "s3:GetObject"]
