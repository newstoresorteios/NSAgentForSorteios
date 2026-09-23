import pytest
from app.sales.policies.objection_authority import detect_objection_kind


@pytest.mark.parametrize('text', ['Quero dar um relógio de presente, mas não entendo nada.',
    'É presente', 'Quero um relógio para minha esposa', 'Um presente para meu marido'])
def test_gift_is_not_an_approval_objection(text):
    assert detect_objection_kind(text) is None


@pytest.mark.parametrize('text', ['Preciso falar com minha esposa', 'Vou confirmar com meu marido',
    'Deixa eu ver com meu sócio', 'Depois eu confirmo'])
def test_actual_approval_objection_is_preserved(text):
    assert detect_objection_kind(text) == 'approval'
