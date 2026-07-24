from app.security import encrypt_json, decrypt_json, check_password, make_session_token, verify_session_token


def test_encrypt_roundtrip():
    data = {"cookie": "BDUSS=abc; STOKEN=xyz"}
    tok = encrypt_json(data)
    assert decrypt_json(tok) == data


def test_session():
    t = make_session_token()
    assert verify_session_token(t)
    assert not verify_session_token("garbage")
