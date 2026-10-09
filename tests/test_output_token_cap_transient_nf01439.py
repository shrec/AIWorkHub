"""NF-2026-01439: a Claude per-response output-token cap must retry, not block.

``terminal_failure_classification`` already reads a Claude synthetic
assistant line's machine field to place ``model_not_found`` as transient (see
``tests/test_terminal_failure_disposition_r4.py``'s ``CLAUDE_MODEL_NOT_FOUND``
fixture). It had no entry for the provider's own per-response output-token
cap, so a card that hit it ended ``blocked`` and needed a manual
``retry_terminal`` instead of the bounded transient retry every other
provider-asserted route hiccup already gets.

Both of the CLI's own shapes for this cap are reproduced here: a machine
``error`` field on a synthetic assistant line (the same channel
``model_not_found`` already uses), and the CLI's own fixed sentence inside a
``result`` envelope's ``result`` field with no machine field at all. Both
must place identically to ``model_not_found``. The two negatives pin that the
sentence alone, outside an ``is_error`` ``result`` envelope, names nothing --
free prose must never be able to forge a route failure.
"""

from __future__ import annotations

from aiworkhub import terminal_failure_classification as tfc

TRANSIENT = tfc.FAILURE_CLASS_TRANSIENT
UNKNOWN = tfc.FAILURE_CLASS_UNKNOWN

# Claude CLI: the synthetic assistant line's machine ``error`` field, the same
# shape as ``CLAUDE_MODEL_NOT_FOUND`` with the output-token-cap code instead.
CLAUDE_MAX_OUTPUT_TOKENS_MACHINE_FIELD = "\n".join([
    '{"type":"assistant","message":{"model":"<synthetic>","role":"assistant"},'
    '"error":"max_output_tokens","request_id":"req_011Ceit55zzTkXL6VSFdRcti"}',
    '{"type":"result","subtype":"success","is_error":true,'
    '"terminal_reason":"api_error","result":"API Error: Claude\'s response '
    'exceeded the 64000 output token maximum for this model."}',
])

# Claude CLI: no machine ``error`` field at all -- only the CLI's own fixed
# sentence inside the terminal ``result`` envelope's ``result`` field.
CLAUDE_MAX_OUTPUT_TOKENS_RESULT_SENTENCE = (
    '{"type":"result","subtype":"success","is_error":true,'
    '"terminal_reason":"api_error","result":"API Error: Claude\'s response '
    'exceeded the 64000 output token maximum for this model."}'
)

# The same sentence, but the line is not a ``result`` envelope at all -- a
# worker printing it must not be able to forge the route failure.
CLAUDE_MAX_OUTPUT_TOKENS_NOT_RESULT_TYPE = (
    '{"type":"assistant","is_error":true,"terminal_reason":"api_error",'
    '"result":"API Error: Claude\'s response exceeded the 64000 output '
    'token maximum for this model."}'
)

# The same sentence on a ``result`` envelope that does not assert
# ``is_error`` -- a successful turn can mention tokens in its own output.
CLAUDE_MAX_OUTPUT_TOKENS_IS_ERROR_FALSE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"terminal_reason":"api_error","result":"API Error: Claude\'s response '
    'exceeded the 64000 output token maximum for this model."}'
)


def test_claude_machine_field_places_output_token_cap_as_transient():
    verdict = tfc.failure_disposition(
        stdout_tail=CLAUDE_MAX_OUTPUT_TOKENS_MACHINE_FIELD,
    )
    assert verdict["failure_class"] == TRANSIENT
    assert verdict["evidence"] == "provider_code=max_output_tokens"


def test_claude_result_sentence_places_output_token_cap_as_transient():
    verdict = tfc.failure_disposition(
        stdout_tail=CLAUDE_MAX_OUTPUT_TOKENS_RESULT_SENTENCE,
    )
    assert verdict["failure_class"] == TRANSIENT
    assert verdict["evidence"] == "provider_code=max_output_tokens"


def test_output_token_cap_gets_the_existing_bounded_route_retry():
    """Same cause/action/retry_scope as ``model_not_found`` -- not a new path."""
    verdict = tfc.failure_disposition(
        stdout_tail=CLAUDE_MAX_OUTPUT_TOKENS_RESULT_SENTENCE,
    )
    assert verdict["cause"] == tfc.CAUSE_PROVIDER_TRANSIENT
    assert verdict["action"] == tfc.ACTION_ROUTE_RETRY
    assert verdict["retry_scope"] == tfc.RETRY_SCOPE_DISTINCT_ROUTE
    assert verdict["provider_launched"] is True


def test_sentence_outside_a_result_envelope_is_not_placed():
    verdict = tfc.failure_disposition(
        stdout_tail=CLAUDE_MAX_OUTPUT_TOKENS_NOT_RESULT_TYPE,
    )
    assert verdict["failure_class"] == UNKNOWN
    assert verdict["provider_code"] == ""


def test_sentence_without_is_error_is_not_placed():
    verdict = tfc.failure_disposition(
        stdout_tail=CLAUDE_MAX_OUTPUT_TOKENS_IS_ERROR_FALSE,
    )
    assert verdict["failure_class"] == UNKNOWN
    assert verdict["provider_code"] == ""


def test_unclassified_reason_constants_stays_empty():
    assert tfc.unclassified_reason_constants() == ()
