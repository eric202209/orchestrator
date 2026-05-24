from app.services.workspace.system_settings import classify_model_lane


def test_model_lane_classifies_hosted_openai_backend():
    lane = classify_model_lane(
        backend="openai_responses_api",
        model_family="gpt-5.4",
        adaptation_profile="openclaw_default",
    )

    assert lane["label"] == "hosted_openai"
    assert lane["capability_tier"] == "hosted"
    assert lane["capability_traits"]["structured_output_reliability"] == "high"
    assert lane["capability_traits"]["configured_available"] in {True, False}


def test_model_lane_marks_small_quantized_local_models_as_constrained():
    lane = classify_model_lane(
        backend="local_openclaw",
        model_family="Qwen2.5-Coder-14B-Instruct-Q5_K_M",
        adaptation_profile="openclaw_default",
    )

    assert lane["label"] == "local_openclaw"
    assert lane["capability_tier"] == "local_constrained"
    assert "structured_output_reliability" in lane["capability_traits"]
    assert "Model name suggests constrained local capacity" in lane["reasons"]
