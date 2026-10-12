"""Tests for the workflow definition. They need no AWS account.

The JSONata expressions are run with a local JSONata library, so the
checks behave here the same way they do inside Step Functions.
"""

import jsonata
import pytest

from infra import workflow

DEFINITION = workflow.build_definition("test-model", "test-table")
STATES = DEFINITION["States"]

GOOD_INVOICE = {
    "supplier_name": "Brightwater Office Supplies",
    "invoice_number": "BW-2026-0417",
    "currency": "USD",
    "line_items": [
        {"description": "Paper", "amount": 320.0},
        {"description": "Toner", "amount": 414.0},
        {"description": "Organisers", "amount": 96.0},
    ],
    "subtotal": 830.0,
    "tax": 83.0,
    "total": 913.0,
}


def evaluate(expression, **variables):
    """Run one {% ... %} expression with the given workflow variables."""
    assert expression.startswith("{%") and expression.endswith("%}")
    compiled = jsonata.Jsonata(expression[2:-2])
    for name, value in variables.items():
        compiled.assign(name, value)
    return compiled.evaluate(None)


def next_states(state):
    names = [state.get("Next"), state.get("Default")]
    names += [choice["Next"] for choice in state.get("Choices", [])]
    names += [catch["Next"] for catch in state.get("Catch", [])]
    return [name for name in names if name]


def test_every_transition_points_to_a_real_state():
    assert DEFINITION["StartAt"] in STATES
    for name, state in STATES.items():
        for target in next_states(state):
            assert target in STATES, f"{name} points to missing state {target}"


def test_every_state_can_be_reached():
    seen = set()
    todo = [DEFINITION["StartAt"]]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(next_states(STATES[name]))
    assert seen == set(STATES)


def test_every_state_ends_or_continues():
    for name, state in STATES.items():
        if state["Type"] in ("Choice", "Fail"):
            continue
        assert ("Next" in state) != (state.get("End") is True), name


@pytest.mark.parametrize(
    "name", ["ReadInvoice", "ExtractFields", "ReviewInvoice", "WriteSummary", "SaveResult"]
)
def test_failures_are_saved(name):
    assert STATES[name]["Catch"][0]["Next"] == "SaveFailure"
    assert STATES["SaveFailure"]["Next"] == "Failed"


def test_model_calls_use_the_given_model_and_prompts():
    for name, prompt in [
        ("ExtractFields", "extract"),
        ("ReviewInvoice", "review"),
        ("WriteSummary", "summarize"),
    ]:
        arguments = STATES[name]["Arguments"]
        assert arguments["ModelId"] == "test-model"
        assert arguments["Body"]["system"] == workflow.load_prompt(prompt)


def test_results_are_written_to_the_given_table():
    assert STATES["SaveResult"]["Arguments"]["TableName"] == "test-table"
    assert STATES["SaveFailure"]["Arguments"]["TableName"] == "test-table"


def test_start_reads_bucket_and_key_from_the_upload_event():
    event = {"detail": {"bucket": {"name": "my-bucket"}, "object": {"key": "a.txt"}}}
    assign = STATES["Start"]["Assign"]
    assert evaluate(assign["bucket"], states={"input": event}) == "my-bucket"
    assert evaluate(assign["invoiceKey"], states={"input": event}) == "a.txt"


def test_totals_check_passes_a_correct_invoice():
    assert evaluate(workflow.TOTALS_ISSUES, invoice=GOOD_INVOICE) == []


def test_totals_check_allows_rounding_differences():
    invoice = {**GOOD_INVOICE, "tax": 83.004}
    assert evaluate(workflow.TOTALS_ISSUES, invoice=invoice) == []


def test_totals_check_finds_wrong_subtotal():
    invoice = {**GOOD_INVOICE, "subtotal": 800.0, "total": 883.0}
    issues = evaluate(workflow.TOTALS_ISSUES, invoice=invoice)
    assert issues == ["Line items add up to 830 but the subtotal is 800"]


def test_totals_check_finds_wrong_total():
    invoice = {**GOOD_INVOICE, "total": 950.0}
    issues = evaluate(workflow.TOTALS_ISSUES, invoice=invoice)
    assert issues == ["Subtotal plus tax is 913 but the total is 950"]


def test_totals_check_finds_both_problems():
    invoice = {**GOOD_INVOICE, "subtotal": 800.0}
    assert len(evaluate(workflow.TOTALS_ISSUES, invoice=invoice)) == 2


def model_result(tool_input):
    """A Bedrock response where the model answered through a tool."""
    return {"result": {"Body": {"content": [{"type": "tool_use", "input": tool_input}]}}}


@pytest.mark.parametrize(
    "totals_issues, review_issues, expected",
    [
        ([], [], []),
        (["bad total"], [], ["bad total"]),
        ([], ["no supplier"], ["no supplier"]),
        (["bad total"], ["no supplier", "bad date"], ["bad total", "no supplier", "bad date"]),
    ],
)
def test_issues_from_both_checks_are_joined(totals_issues, review_issues, expected):
    issues = evaluate(
        workflow.ALL_ISSUES,
        totalsIssues=totals_issues,
        states=model_result({"issues": review_issues}),
    )
    assert issues == expected


def test_tool_input_is_read_from_the_model_response():
    result = evaluate(workflow.TOOL_INPUT, states=model_result(GOOD_INVOICE))
    assert result == GOOD_INVOICE


def test_summary_text_is_read_from_the_model_response():
    states = {"result": {"Body": {"content": [{"type": "text", "text": "All good."}]}}}
    assert evaluate(workflow.TEXT_OUTPUT, states=states) == "All good."


@pytest.mark.parametrize("issues, expected", [([], False), (["one"], True), (["a", "b"], True)])
def test_choice_sends_invoices_with_issues_to_review(issues, expected):
    choice = STATES["AnyIssues"]
    assert choice["Choices"][0]["Next"] == "NeedsReview"
    assert choice["Default"] == "Approved"
    assert evaluate(choice["Choices"][0]["Condition"], issues=issues) is expected


@pytest.mark.parametrize(
    "issues, expected",
    [([], []), (["one"], [{"S": "one"}]), (["a", "b"], [{"S": "a"}, {"S": "b"}])],
)
def test_issues_are_saved_as_a_dynamodb_list(issues, expected):
    expression = STATES["SaveResult"]["Arguments"]["Item"]["issues"]["L"]
    assert evaluate(expression, issues=issues) == expected
