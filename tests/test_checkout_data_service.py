import pytest

from app.checkout_data_service import _normalize_phone, update_checkout_data
from app.commerce_context import CommerceConversationState, evolve_commerce_state


@pytest.mark.parametrize("value,expected", [
    ("+55 43 99999-9999", "43999999999"),
    ("5543999999999", "43999999999"),
    ("55 43 3333-4444", "4333334444"),
    ("43999999999", "43999999999"),
    ("4333334444", "4333334444"),
    ("(43) 99999-9999", "43999999999"),
    ("5543 9999", None),
    ("999999999", None),
    ("", None),
    (None, None),
])
def test_normalize_phone_canonizes_to_adapter_contract(value, expected):
    assert _normalize_phone(value) == expected


def test_update_checkout_data_strips_ddi_and_validates_phone():
    state = CommerceConversationState(
        checkout_channel_preference="whatsapp",
        checkout_draft={
            "customer": {"type": "0"},
            "address": {"country": "BRA", "type": "1", "zip_code": "86480000"},
        },
    )
    result = update_checkout_data(state, {
        "name": "Test User",
        "phone": "+55 43 99999-9999",
        "email": "test@example.com",
        "cpf": "52998224725",
        "address": "Rua Test",
        "number": "123",
        "neighborhood": "Centro",
        "city": "Cornelio Procopio",
        "state": "PR",
        "zipcode": "86480000",
    })
    updated = evolve_commerce_state(state, result)

    assert result.commercial_data["field_errors"] == {}
    assert updated.checkout_draft.customer.phone == "43999999999"
    assert len(updated.checkout_draft.customer.phone) == 11
