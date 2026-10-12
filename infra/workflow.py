"""The Step Functions workflow, written as a plain Python dict.

The workflow uses JSONata. Text between {% and %} is an expression that
Step Functions evaluates while the workflow runs.

Values shared between steps are kept in workflow variables:
  bucket, invoiceKey  where the invoice file is
  invoiceText         the raw text of the invoice
  invoice             the fields found by the extract agent
  issues              all problems found by the checks
  status              APPROVED or NEEDS_REVIEW
  summary             the text written by the summary agent
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Retry a failed call two more times, waiting 2 and then 4 seconds.
RETRY = [
    {
        "ErrorEquals": ["States.TaskFailed"],
        "IntervalSeconds": 2,
        "MaxAttempts": 2,
        "BackoffRate": 2,
    }
]

# When a step still fails, remember the error and save it.
CATCH = [
    {
        "ErrorEquals": ["States.ALL"],
        "Assign": {"error": "{% $states.errorOutput %}"},
        "Next": "SaveFailure",
    }
]

INVOICE_TOOL = {
    "Name": "record_invoice",
    "Description": "Record the fields found in an invoice.",
    "InputSchema": {
        "type": "object",
        "properties": {
            "supplier_name": {"type": "string"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string", "description": "YYYY-MM-DD"},
            "due_date": {"type": "string", "description": "YYYY-MM-DD"},
            "currency": {"type": "string", "description": "Three letter code"},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "amount": {"type": "number"},
                    },
                    "required": ["description", "amount"],
                },
            },
            "subtotal": {"type": "number"},
            "tax": {"type": "number"},
            "total": {"type": "number"},
        },
        "required": [
            "supplier_name",
            "invoice_number",
            "invoice_date",
            "due_date",
            "currency",
            "line_items",
            "subtotal",
            "tax",
            "total",
        ],
    },
}

REVIEW_TOOL = {
    "Name": "record_review",
    "Description": "Record the problems found in an invoice.",
    "InputSchema": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One short sentence per problem. Empty if none.",
            }
        },
        "required": ["issues"],
    },
}

# Arithmetic checks. These are done here and not by a model, because the
# result must be exact and the same every time.
LINES_CHECK = (
    "$abs($sum($invoice.line_items.amount) - $invoice.subtotal) > 0.01"
    " ? 'Line items add up to ' & $round($sum($invoice.line_items.amount), 2)"
    " & ' but the subtotal is ' & $invoice.subtotal"
)
TOTAL_CHECK = (
    "$abs($invoice.subtotal + $invoice.tax - $invoice.total) > 0.01"
    " ? 'Subtotal plus tax is ' & $round($invoice.subtotal + $invoice.tax, 2)"
    " & ' but the total is ' & $invoice.total"
)
# Square brackets join the two results into one list and skip empty ones.
TOTALS_ISSUES = "{% [" + LINES_CHECK + ", " + TOTAL_CHECK + "] %}"

# Where the answer sits in a Bedrock Converse response.
REPLY = "$states.result.Output.Message.Content"
TOOL_INPUT = "{% " + REPLY + ".ToolUse[0].Input %}"
TEXT_OUTPUT = "{% $join(" + REPLY + ".Text) %}"
ALL_ISSUES = "{% [$totalsIssues, " + REPLY + ".ToolUse[0].Input.issues] %}"


def load_prompt(name):
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


def model_call(model_id, prompt_name, user_content, max_tokens, tool=None):
    """Build a task that calls the model on Bedrock.

    It uses the Converse API, which has the same request and response
    format for every model, so the model can be swapped without code changes.
    """
    arguments = {
        "ModelId": model_id,
        "System": [{"Text": load_prompt(prompt_name)}],
        "Messages": [{"Role": "user", "Content": user_content}],
        "InferenceConfig": {"MaxTokens": max_tokens},
    }
    if tool:
        # The prompt tells the model to answer through the tool. The choice is
        # left on auto because not every model accepts a forced tool choice.
        arguments["ToolConfig"] = {
            "Tools": [
                {
                    "ToolSpec": {
                        "Name": tool["Name"],
                        "Description": tool["Description"],
                        "InputSchema": {"Json": tool["InputSchema"]},
                    }
                }
            ],
            "ToolChoice": {"Auto": {}},
        }
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::aws-sdk:bedrockruntime:converse",
        "Arguments": arguments,
        "Retry": RETRY,
        "Catch": CATCH,
    }


def text_block(text):
    return {"Text": text}


def save_item(table_name, attributes):
    """Build a task that writes one item to the results table."""
    item = {
        "invoice_key": {"S": "{% $invoiceKey %}"},
        "processed_at": {"S": "{% $now() %}"},
    }
    item.update(attributes)
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::dynamodb:putItem",
        "Arguments": {"TableName": table_name, "Item": item},
        "Retry": RETRY,
    }


def build_definition(model_id, table_name):
    """Return the full workflow definition."""
    states = {}

    # The input is the EventBridge event for the uploaded file.
    states["Start"] = {
        "Type": "Pass",
        "Assign": {
            "bucket": "{% $states.input.detail.bucket.name %}",
            "invoiceKey": "{% $states.input.detail.object.key %}",
        },
        "Next": "ReadInvoice",
    }

    states["ReadInvoice"] = {
        "Type": "Task",
        "Resource": "arn:aws:states:::aws-sdk:s3:getObject",
        "Arguments": {"Bucket": "{% $bucket %}", "Key": "{% $invoiceKey %}"},
        "Assign": {"invoiceText": "{% $states.result.Body %}"},
        "Retry": RETRY,
        "Catch": CATCH,
        "Next": "ExtractFields",
    }

    # Agent 1: turn the invoice text into structured fields.
    states["ExtractFields"] = {
        **model_call(
            model_id,
            "extract",
            [text_block("Invoice text:"), text_block("{% $invoiceText %}")],
            max_tokens=2000,
            tool=INVOICE_TOOL,
        ),
        "Assign": {"invoice": TOOL_INPUT},
        "Next": "CheckTotals",
    }

    states["CheckTotals"] = {
        "Type": "Pass",
        "Assign": {"totalsIssues": TOTALS_ISSUES},
        "Next": "ReviewInvoice",
    }

    # Agent 2: look for problems that a simple rule cannot catch.
    states["ReviewInvoice"] = {
        **model_call(
            model_id,
            "review",
            [
                text_block("Invoice text:"),
                text_block("{% $invoiceText %}"),
                text_block("Extracted fields:"),
                text_block("{% $string($invoice) %}"),
            ],
            max_tokens=1000,
            tool=REVIEW_TOOL,
        ),
        "Assign": {"issues": ALL_ISSUES},
        "Next": "AnyIssues",
    }

    states["AnyIssues"] = {
        "Type": "Choice",
        "Choices": [{"Condition": "{% $count($issues) > 0 %}", "Next": "NeedsReview"}],
        "Default": "Approved",
    }
    states["Approved"] = {
        "Type": "Pass",
        "Assign": {"status": "APPROVED"},
        "Next": "WriteSummary",
    }
    states["NeedsReview"] = {
        "Type": "Pass",
        "Assign": {"status": "NEEDS_REVIEW"},
        "Next": "WriteSummary",
    }

    # Agent 3: write a short summary for the person who approves payments.
    states["WriteSummary"] = {
        **model_call(
            model_id,
            "summarize",
            [
                text_block("Extracted fields:"),
                text_block("{% $string($invoice) %}"),
                text_block("{% 'Status: ' & $status %}"),
                text_block("{% 'Problems found: ' & $string($issues) %}"),
            ],
            max_tokens=500,
        ),
        "Assign": {"summary": TEXT_OUTPUT},
        "Next": "SaveResult",
    }

    states["SaveResult"] = {
        **save_item(
            table_name,
            {
                "status": {"S": "{% $status %}"},
                "model_id": {"S": model_id},
                "supplier_name": {"S": "{% $invoice.supplier_name %}"},
                "invoice_number": {"S": "{% $invoice.invoice_number %}"},
                "currency": {"S": "{% $invoice.currency %}"},
                "total": {"N": "{% $string($invoice.total) %}"},
                "issues": {"L": "{% [$issues.{'S': $}] %}"},
                "summary": {"S": "{% $summary %}"},
                "fields": {"S": "{% $string($invoice) %}"},
            },
        ),
        "Catch": CATCH,
        "End": True,
    }

    # Any failed step ends up here, so no invoice is lost without a trace.
    states["SaveFailure"] = {
        **save_item(
            table_name,
            {
                "status": {"S": "FAILED"},
                "error": {"S": "{% $string($error) %}"},
            },
        ),
        "Next": "Failed",
    }
    states["Failed"] = {"Type": "Fail", "Error": "InvoiceProcessingFailed"}

    return {
        "Comment": "Extract, review and summarize a supplier invoice.",
        "QueryLanguage": "JSONata",
        "StartAt": "Start",
        "States": states,
    }
