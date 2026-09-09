from polar.client import PolarClient
from auth.token_manager import load_tokens


def get_sleep_data(client: PolarClient):
    """Nuits disponibles (Polar expose environ 28 jours).

    "nights" est un emballage pur : on le retire. Retourne une liste.
    """
    data = client.get("/users/sleep")
    return (data or {}).get("nights", [])


def get_sleepWise_data(client: PolarClient):
    """Vigilance predite par SleepWise. Retourne une liste."""
    data = client.get("/users/sleepwise/alertness")
    return data or []


def get_sleep_available(client: PolarClient):
    """Nuits que Polar detient encore, avec leurs heures de coucher/lever.

    Utile pour reperer ce qui va bientot sortir de la fenetre des 28 jours.
    "available" est un emballage : on le retire. Retourne une liste.
    """
    data = client.get("/users/sleep/available")
    return (data or {}).get("available", [])


def get_circadian_bedtime(client: PolarClient):
    """Heure de coucher ideale predite par SleepWise. Retourne une liste."""
    data = client.get("/users/sleepwise/circadian-bedtime")
    return data or []


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    nuits = get_sleep_data(client)
    print(len(nuits), "nuit(s)")
    for nuit in nuits:
        print("  ", nuit["date"], "- score", nuit["sleep_score"])

    print(len(get_sleepWise_data(client)), "releve(s) sleepwise")
