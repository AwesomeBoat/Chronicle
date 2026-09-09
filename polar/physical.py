from polar.client import PolarClient
from auth.token_manager import load_tokens


def get_physical_info(client: PolarClient):
    """Dernieres infos physiologiques du compte Polar.

    L'API renvoie un objet direct (pas de liste, pas d'enveloppe).
    Retourne un dict, vide si pas de donnees.
    """
    data = client.get("/users/physical-info")
    return data or {}


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    infos = get_physical_info(client)
    print("poids", infos.get("weight"), "kg",
          "| taille", infos.get("height"), "cm",
          "| VO2max", infos.get("vo2_max"),
          "| FC repos", infos.get("resting_heart_rate"))
