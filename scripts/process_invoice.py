"""Upload an invoice and print the result when the workflow has finished.

Usage: python scripts/process_invoice.py sample_invoices/clean_invoice.txt
"""

import sys
import time
from pathlib import Path

import boto3

STACK_NAME = "InvoiceProcessing"
WAIT_SECONDS = 120


def stack_outputs():
    stack = boto3.client("cloudformation").describe_stacks(StackName=STACK_NAME)
    outputs = stack["Stacks"][0]["Outputs"]
    return {item["OutputKey"]: item["OutputValue"] for item in outputs}


def wait_for_result(table, invoice_key, uploaded_at):
    """Poll the results table until this upload has been processed."""
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        item = table.get_item(Key={"invoice_key": invoice_key}).get("Item")
        # Skip a result left over from an earlier upload of the same file.
        if item and item["processed_at"] >= uploaded_at:
            return item
        time.sleep(3)
    return None


def print_result(item):
    print(f"Status:  {item['status']}")
    if item["status"] == "FAILED":
        print(f"Error:   {item['error']}")
        return
    print(f"Invoice: {item['invoice_number']} from {item['supplier_name']}")
    print(f"Total:   {item['total']} {item['currency']}")
    if item["issues"]:
        print("Issues:")
        for issue in item["issues"]:
            print(f"  - {issue}")
    print(f"\n{item['summary']}")


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/process_invoice.py <invoice file>")
    path = Path(sys.argv[1])
    if not path.is_file():
        sys.exit(f"File not found: {path}")

    outputs = stack_outputs()
    table = boto3.resource("dynamodb").Table(outputs["ResultsTableName"])

    uploaded_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    boto3.client("s3").upload_file(str(path), outputs["InvoiceBucketName"], path.name)
    print(f"Uploaded {path.name}. Waiting for the workflow...")

    item = wait_for_result(table, path.name, uploaded_at)
    if item is None:
        sys.exit("No result yet. Check the workflow in the Step Functions console.")
    print_result(item)


if __name__ == "__main__":
    main()
