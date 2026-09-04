from app.channels.audio_service import fix_common_watch_brand_mishearings


def test_hamilton_misheard_as_remetente_is_corrected():
    assert (
        fix_common_watch_brand_mishearings("Tem remetente disponível ou não?")
        == "tem Hamilton disponível ou não?"
    )


def test_unrelated_remetente_sentence_unchanged():
    text = "O remetente do pacote é a transportadora"
    assert fix_common_watch_brand_mishearings(text) == text
