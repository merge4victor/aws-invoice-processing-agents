"""CDK entry point. Run through 'cdk deploy' from the repo root."""

import os
import sys

import aws_cdk as cdk

# Make the repo root importable when CDK runs this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.stack import InvoiceProcessingStack  # noqa: E402

app = cdk.App()
InvoiceProcessingStack(
    app,
    "InvoiceProcessing",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
