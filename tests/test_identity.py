from booking_agent.identity import extract_afm, parse_passport_mrz, validate_afm


def test_afm_validation_and_extraction() -> None:
    assert validate_afm("090165560")
    assert extract_afm("ΑΦΜ: 090 165 560") == "090165560"
    assert not validate_afm("111111111")


def test_parse_valid_passport_mrz() -> None:
    text = """P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<
L898902C36UTO7408122F1204159ZE184226B<<<<<10"""

    fields = parse_passport_mrz(text)

    assert fields is not None
    assert fields.document_number == "L898902C3"
    assert fields.nationality == "UTO"

