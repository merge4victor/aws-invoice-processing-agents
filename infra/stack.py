"""All AWS resources for the invoice processing workflow."""

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from infra.workflow import build_definition

DEFAULT_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


class InvoiceProcessingStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Override with: cdk deploy -c model_id=<model or inference profile id>
        model_id = self.node.try_get_context("model_id") or DEFAULT_MODEL_ID

        # Invoices are uploaded here. Each upload sends an event to EventBridge.
        invoice_bucket = s3.Bucket(
            self,
            "InvoiceBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            event_bridge_enabled=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # One item per processed invoice.
        results_table = dynamodb.Table(
            self,
            "ResultsTable",
            partition_key=dynamodb.Attribute(
                name="invoice_key", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        definition = build_definition(model_id, results_table.table_name)
        state_machine = sfn.StateMachine(
            self,
            "InvoiceWorkflow",
            definition_body=sfn.DefinitionBody.from_string(self.to_json_string(definition)),
            timeout=cdk.Duration.minutes(5),
        )

        # The workflow calls AWS services directly, so its role needs exactly
        # these three permissions and nothing else.
        state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[invoice_bucket.arn_for_objects("*")],
            )
        )
        results_table.grant(state_machine, "dynamodb:PutItem")
        # An inference profile can route to any region, so the model
        # permission cannot be pinned to one region.
        state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                ],
            )
        )

        # Start the workflow for every new file in the bucket.
        events.Rule(
            self,
            "InvoiceUploadedRule",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [invoice_bucket.bucket_name]}},
            ),
            targets=[targets.SfnStateMachine(state_machine)],
        )

        cdk.CfnOutput(self, "InvoiceBucketName", value=invoice_bucket.bucket_name)
        cdk.CfnOutput(self, "ResultsTableName", value=results_table.table_name)
        cdk.CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
