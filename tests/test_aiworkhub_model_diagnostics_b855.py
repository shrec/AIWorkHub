"""B855: model-diagnostics static-source assertions.

Mirrors ``test_aiworkhub_vscode_release_b853.py``'s
``test_extension_js_glm_canary_never_polls_or_auto_spends`` and adds the
specific ordering/gating checks this task cares about: the GLM canary /
Model capabilities section must stay a SECONDARY diagnostics surface, never
the primary/first-rendered dashboard action (that stays "Init Repo"), and
successful capability discovery must render as discovery-only wording (no
auto-invocation of ``sendRequest`` outside the explicitly-gated
``runGlmCanary`` function).
"""

from __future__ import annotations

import re
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_EXT_PATH = _TOOL_ROOT / "vscode-extension" / "extension.js"
_EXT_SOURCE = _EXT_PATH.read_text(encoding="utf-8")


def test_init_repo_button_is_primary_before_model_capabilities_section() -> None:
    html_fn_start = _EXT_SOURCE.index("function getHtmlForWebview(")
    html_source = _EXT_SOURCE[html_fn_start : html_fn_start + 20000]
    init_index = html_source.index('id="initialize-button"')
    model_caps_index = html_source.index('class="model-capabilities"')
    glm_button_index = html_source.index('id="glm-canary-button"')
    assert init_index < model_caps_index, (
        "Init Repo must render before the Model capabilities section -- it is the "
        "primary dashboard action, GLM stays secondary/diagnostics"
    )
    assert init_index < glm_button_index


def test_glm_canary_lives_inside_model_capabilities_section() -> None:
    html_fn_start = _EXT_SOURCE.index("function getHtmlForWebview(")
    html_source = _EXT_SOURCE[html_fn_start : html_fn_start + 20000]
    section_start = html_source.index('class="model-capabilities"')
    # The section element containing model capabilities + the GLM canary sub-block
    # closes at the next top-level "</section>" after it opens.
    section_end = html_source.index("</section>", section_start)
    section_body = html_source[section_start:section_end]
    assert "glm-canary-button" in section_body
    assert "glm-canary-heading" in section_body


def test_glm_canary_gated_behind_explicit_model_selection_and_modal() -> None:
    canary_fn_start = _EXT_SOURCE.index("async function runGlmCanary(")
    canary_fn_source = _EXT_SOURCE[canary_fn_start : canary_fn_start + 4000]
    assert "vscode.lm.selectChatModels" in canary_fn_source
    assert "isGlmModelInfo" in canary_fn_source
    assert "modal: true" in canary_fn_source
    assert "Send Prompt" in canary_fn_source
    assert 'confirmation !== "Send Prompt"' in canary_fn_source


def test_sendrequest_only_invoked_inside_run_glm_canary() -> None:
    """`sendRequest(` must never be called anywhere except inside
    runGlmCanary's own bounded function body -- no auto-invocation path."""
    canary_fn_start = _EXT_SOURCE.index("async function runGlmCanary(")
    # Bound the function body to its own extent (up to the next top-level
    # function declaration), so a false negative can't hide a second call
    # further down the file.
    next_fn_match = re.search(r"\nfunction |\nasync function ", _EXT_SOURCE[canary_fn_start + 10 :])
    canary_fn_end = (
        canary_fn_start + 10 + next_fn_match.start() if next_fn_match else len(_EXT_SOURCE)
    )
    canary_fn_source = _EXT_SOURCE[canary_fn_start:canary_fn_end]
    total_send_request = _EXT_SOURCE.count("sendRequest(")
    inside_send_request = canary_fn_source.count("sendRequest(")
    assert inside_send_request >= 1
    assert total_send_request == inside_send_request, (
        "sendRequest( must only ever be called from inside runGlmCanary -- found a "
        "call outside its bounded function body"
    )


def test_refresh_model_capabilities_is_discovery_only_wording() -> None:
    refresh_fn_start = _EXT_SOURCE.index("async function refreshModelCapabilities(")
    refresh_fn_source = _EXT_SOURCE[refresh_fn_start : refresh_fn_start + 2000]
    assert "sendRequest(" not in refresh_fn_source
    assert "vscode.lm.selectChatModels" in refresh_fn_source
    assert "MODEL_CAPABILITY_STATES" in refresh_fn_source
    # Discovery reports availability states, never spends a prompt/credits.
    assert "GLM_CANARY_FIXED_PROMPT" not in refresh_fn_source


def test_glm_never_referenced_from_auto_refresh_timer() -> None:
    marker = "reschedule() {"
    start = _EXT_SOURCE.index(marker)
    reschedule_body = _EXT_SOURCE[start : start + 600]
    assert "runGlmCanary" not in reschedule_body
    assert "refreshModelCapabilities" not in reschedule_body
    assert "sendRequest" not in reschedule_body


def test_glm_never_referenced_from_activation() -> None:
    activate_start = _EXT_SOURCE.index("function activate(context)")
    activate_end = _EXT_SOURCE.index("\nfunction deactivate", activate_start)
    activate_body = _EXT_SOURCE[activate_start:activate_end]
    assert "runGlmCanary" not in activate_body
    assert "refreshModelCapabilities" not in activate_body
    assert "sendRequest" not in activate_body
