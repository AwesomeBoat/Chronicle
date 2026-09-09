from polar.client import PolarClient
from auth.token_manager import load_tokens


def get_cardio_load(client: PolarClient):
    """Charge cardio des ~28 derniers jours : strain, tolerance, ratio.

    L'API renvoie deja une liste, il n'y a rien a deballer.
    Cet endpoint n'accepte aucun parametre : from/to sont ignores.
    """
    data = client.get("/users/cardio-load")
    return data or []


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    charges = get_cardio_load(client)
    print(len(charges), "jour(s)")
    for charge in charges[:5]:
        print("  ", charge["date"], "- charge", charge["cardio_load"],
              "- statut", charge["cardio_load_status"])
