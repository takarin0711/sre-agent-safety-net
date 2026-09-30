"""第2層 (JSON Validator) の単体検証。

対応するシナリオは docs/phases/phase2-pydantic/design.md 第4章:

- P-02  正常な JSON fixture が全フィールド正しく型付けされる
- V-01〜V-08  壊れた入力が決定的に検知され、理由が特定できる（判定基準1）
- D-01  同一入力に対する判定が 10 回連続で完全に一致する（判定基準2）
- N-01  構造は正しいが意味的に危険な入力が**通過する**（判定基準4・責務境界の証明）

実 LLM を使うシナリオ（P-01 / R-01 / R-02 / C-01）はここには含めない。
単体を先に固めないと、落ちた原因の層が切り分けられなくなる（design.md 4.4）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_core import ValidationError

from agents.schemas import Environment, Operation, ProposedAction
from agents.validation import build_escalation_event, validate_action

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def errors_of(exc: ValidationError) -> list[tuple[str, tuple]]:
    """(エラー種別, どのフィールドか) の一覧。判定基準1 の「理由が取れる」の確認に使う。"""
    return [(e["type"], tuple(e["loc"])) for e in exc.errors()]


# ── P-02 正常系 ────────────────────────────────────────

def test_p02_full_fixture_is_typed() -> None:
    action = validate_action(fixture("valid_rollout_restart.json"), original_request="P-02", trace_id="test-trace")

    assert isinstance(action, ProposedAction)
    assert action.environment is Environment.PRODUCTION
    assert action.operation is Operation.ROLLOUT_RESTART
    assert action.target.kind == "deployment"
    assert action.target.name == "payment-api"
    assert action.replicas is None
    assert action.dry_run is True


def test_p02_optional_fields_default() -> None:
    """replicas / dry_run を省略した最小形。dry_run の既定は True（安全側）。"""
    action = validate_action(fixture("valid_minimal.json"), original_request="P-02", trace_id="test-trace")

    assert action.replicas is None
    assert action.dry_run is True


@pytest.mark.parametrize("name,expected", [("valid_replicas_min.json", 0), ("valid_replicas_max.json", 100)])
def test_p02_replicas_boundary_values(name: str, expected: int) -> None:
    assert validate_action(fixture(name), original_request="P-02", trace_id="test-trace").replicas == expected


@pytest.mark.parametrize("replicas", [-1, 101])
def test_p02_replicas_out_of_range_is_rejected(replicas: int) -> None:
    payload = json.loads(fixture("valid_scale.json"))
    payload["replicas"] = replicas

    with pytest.raises(ValidationError) as excinfo:
        validate_action(json.dumps(payload), original_request="P-02", trace_id="test-trace")

    assert [loc for _, loc in errors_of(excinfo.value)] == [("replicas",)]


@pytest.mark.parametrize("environment", list(Environment))
@pytest.mark.parametrize("operation", list(Operation))
def test_p02_all_enum_values_are_accepted(environment: Environment, operation: Operation) -> None:
    """enum の全値が通ること。語彙を増やしたときに fixture の追加漏れで素通りしないようにする。"""
    payload = json.loads(fixture("valid_rollout_restart.json"))
    payload["environment"] = environment.value
    payload["operation"] = operation.value
    # kubectl_scale のときだけ replicas が必須（相互依存は下の V-04 で検証する）
    payload["replicas"] = 3 if operation is Operation.SCALE else None

    action = validate_action(json.dumps(payload), original_request="P-02", trace_id="test-trace")

    assert action.environment is environment
    assert action.operation is operation


def test_p02_validated_action_is_opa_input_ready() -> None:
    """model_dump(mode="json") がそのまま第3層 (OPA) の input になること（判定基準5 の前提）。"""
    action = validate_action(fixture("valid_scale.json"), original_request="P-02", trace_id="test-trace")

    dumped = action.model_dump(mode="json")
    roundtrip = json.loads(json.dumps(dumped, ensure_ascii=False))

    assert roundtrip["environment"] == "staging"  # StrEnum が素の文字列になっている
    assert roundtrip["operation"] == "kubectl_scale"
    assert roundtrip["target"]["namespace"] == "staging"
    assert roundtrip["replicas"] == 3


# ── V-01〜V-08 異常系（判定基準1）────────────────────────

# (シナリオ ID, 入力, 期待するエラー種別とフィールド)
INVALID_CASES: list[tuple[str, str | None, tuple[str, tuple]]] = [
    ("V-01 自然言語混入", fixture("v01_prose.txt"), ("json_invalid", ())),
    ("V-02 クォート閉じ漏れ", fixture("v02_unclosed_quote.txt"), ("json_invalid", ())),
    ("V-02 末尾カンマ", fixture("v02_trailing_comma.txt"), ("json_invalid", ())),
    ("V-02 シングルクォート", fixture("v02_single_quote.txt"), ("json_invalid", ())),
    ("V-03 途中で切れた出力", fixture("v03_truncated.txt"), ("json_invalid", ())),
    ("V-04 必須キー欠落", fixture("v04_missing_environment.json"), ("missing", ("environment",))),
    (
        "V-04 scale なのに replicas 無し",
        fixture("v04_scale_without_replicas.json"),
        ("replicas_required_for_scale", ()),
    ),
    ("V-05 replicas が文字列", fixture("v05_replicas_string.json"), ("int_type", ("replicas",))),
    ("V-05 dry_run が文字列", fixture("v05_dry_run_string.json"), ("bool_type", ("dry_run",))),
    ("V-05 target が文字列", fixture("v05_target_string.json"), ("model_type", ("target",))),
    ("V-06 environment が enum 外", fixture("v06_environment_prod.json"), ("enum", ("environment",))),
    ("V-06 operation が enum 外", fixture("v06_operation_kubectl_drain.json"), ("enum", ("operation",))),
    ("V-07 余剰キー", fixture("v07_extra_key.json"), ("extra_forbidden", ("then_also",))),
    ("V-08 content が null", None, ("empty_content", ("content",))),
    ("V-08 content が空文字列", fixture("v08_empty.txt"), ("empty_content", ("content",))),
    ("V-08 content が空白のみ", fixture("v08_whitespace.txt"), ("empty_content", ("content",))),
]
INVALID_IDS = [case[0] for case in INVALID_CASES]


@pytest.mark.parametrize("_id,content,expected", INVALID_CASES, ids=INVALID_IDS)
def test_invalid_input_is_detected_with_reason(
    _id: str, content: str | None, expected: tuple[str, tuple]
) -> None:
    """壊れた入力が ValidationError になり、どのフィールドが何の理由で不正かが取れること。

    「落ちた」だけでは不合格。errors() から理由が特定できることまでを確認する。
    """
    with pytest.raises(ValidationError) as excinfo:
        validate_action(content, original_request="V 系の単体検証", trace_id="test-trace")

    assert expected in errors_of(excinfo.value)
    for detail in excinfo.value.errors():
        assert detail["msg"]  # 理由の文言が空でないこと
        assert detail["type"]


def test_v01_code_fence_is_not_stripped() -> None:
    """コードフェンスを剥がす前処理を入れていないこと（design.md 3.4）。

    剥がして通してしまうと V-01 が検知されなくなり、第2層が何を保証しているのか説明できなくなる。
    """
    content = fixture("v01_prose.txt")
    assert "```json" in content  # fixture 自体がフェンスを含んでいること

    with pytest.raises(ValidationError):
        validate_action(content, original_request="V-01", trace_id="test-trace")


# ── D-01 決定性（判定基準2）──────────────────────────────

@pytest.mark.parametrize("_id,content,_expected", INVALID_CASES, ids=INVALID_IDS)
def test_d01_detection_is_deterministic(_id: str, content: str | None, _expected: tuple) -> None:
    """同一入力に対する判定が 10 回連続で完全に一致すること。

    第1層は cooldown という内部状態で結果が変わった（Phase 1 考察3）。
    第2層が純関数であることが、層としての存在価値そのものである。
    """
    observed: list[str] = []
    for _ in range(10):
        with pytest.raises(ValidationError) as excinfo:
            validate_action(content, original_request="D-01", trace_id="test-trace")
        observed.append(excinfo.value.json(include_url=False))

    assert len(set(observed)) == 1


def test_d01_valid_input_is_deterministic() -> None:
    dumps = {
        json.dumps(
            validate_action(fixture("valid_scale.json"), original_request="D-01", trace_id="test-trace").model_dump(mode="json"),
            sort_keys=True,
        )
        for _ in range(10)
    }
    assert len(dumps) == 1


# ── N-01 責務境界（判定基準4）────────────────────────────

def test_n01_structurally_valid_but_dangerous_action_passes() -> None:
    """構造的に正しく意味的に危険な入力は第2層を**通過する**。

    ここで止まる場合、第2層がポリシー判定を始めている（D-002 違反）ため不合格。
    本番環境の PVC 削除を止めるのは第3層 (OPA) の責務である。
    """
    action = validate_action(fixture("n01_dangerous_delete_pvc.json"), original_request="N-01", trace_id="test-trace")

    assert action.environment is Environment.PRODUCTION
    assert action.operation is Operation.DELETE_PVC
    assert action.dry_run is False  # 危険な値であっても構造としては正しい
    assert action.model_dump(mode="json")["operation"] == "kubectl_delete_pvc"


# ── エスカレーションイベント（判定基準3 の単体側）──────────────

def test_escalation_event_contains_layer_reason_and_original_request() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_action(
            fixture("v06_environment_prod.json"),
            original_request="本番の payment-api が異常",
            trace_id="test-trace",
        )

    event = build_escalation_event(
        excinfo.value,
        raw_output=fixture("v06_environment_prod.json"),
        original_request="本番の payment-api が異常",
        trace_id="test-trace",
    )

    assert event.layer == "layer2_json_validator"
    assert event.reason
    assert event.original_request == "本番の payment-api が異常"
    assert event.raw_output is not None and "prod" in event.raw_output
    assert any(e["loc"] == ["environment"] for e in event.errors)
    # Slack / ログに載せるため JSON 化できること
    json.dumps(event.model_dump(mode="json"), ensure_ascii=False)


def test_escalation_event_is_logged_and_exception_is_not_swallowed(capsys) -> None:
    """失敗が構造化ログに出たうえで例外が再送出されること（例外を握り潰さない）。"""
    with pytest.raises(ValidationError):
        validate_action(fixture("v07_extra_key.json"), original_request="R-01 相当の単体確認", trace_id="test-trace")

    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    error_logs = [entry for entry in logged if entry.get("event") == "validation.error"]

    assert len(error_logs) == 1
    escalation = error_logs[0]["escalation"]
    assert escalation["layer"] == "layer2_json_validator"
    assert escalation["original_request"] == "R-01 相当の単体確認"
    assert escalation["errors"][0]["type"] == "extra_forbidden"
    assert escalation["occurred_at"]


def test_validation_ok_is_logged_with_opa_input(capsys) -> None:
    validate_action(fixture("valid_scale.json"), original_request="P-02", trace_id="test-trace")

    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    ok_logs = [entry for entry in logged if entry.get("event") == "validation.ok"]

    assert len(ok_logs) == 1
    assert ok_logs[0]["action"]["operation"] == "kubectl_scale"
