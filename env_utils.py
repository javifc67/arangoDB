import os


def load_env(env_path=None):
    """Carga variables desde .env y las combina con os.environ (prioridad a os.environ)."""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    env = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                env[k] = v

    for k, v in os.environ.items():
        if v != "":
            env[k] = v

    return env


def require_env(env, keys):
    """Valida que las variables requeridas existan y no estén vacías."""
    missing = [k for k in keys if not env.get(k)]
    if missing:
        missing_str = ", ".join(missing)
        raise SystemExit(
            f"❌ Faltan variables obligatorias en .env o entorno: {missing_str}\n"
            "   Copia .env.example a .env y configura tus credenciales."
        )
    return {k: env[k] for k in keys}
