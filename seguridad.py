"""Hash y verificación de contraseñas usando únicamente la librería estándar
de Python (hashlib + secrets), sin depender de Flask/Werkzeug."""
import hashlib
import hmac
import secrets

_ITERACIONES = 200_000


def generar_hash(password: str) -> str:
    sal = secrets.token_hex(16)
    derivado = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), sal.encode("utf-8"), _ITERACIONES)
    return f"pbkdf2_sha256${_ITERACIONES}${sal}${derivado.hex()}"


def verificar_password(password: str, hash_guardado: str) -> bool:
    try:
        _, iteraciones_str, sal, hash_esperado = hash_guardado.split("$")
        iteraciones = int(iteraciones_str)
    except (ValueError, AttributeError):
        return False

    derivado = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), sal.encode("utf-8"), iteraciones)
    return hmac.compare_digest(derivado.hex(), hash_esperado)
