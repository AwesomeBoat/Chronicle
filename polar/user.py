from polar.client import PolarClient
from auth.token_manager import load_tokens


def get_user_info(client: PolarClient):
    """Profil du compte Polar : identite, date d'inscription, taille, poids.

    C'est l'endpoint /v3/users/{id}, celui-la meme que le POST d'enregistrement
    alimente. Donnees quasi statiques, mais elles datent le debut de l'historique.
    Retourne un dict.
    """
    data = client.get_for_user()
    return data or {}


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    infos = get_user_info(client)
    print("inscrit le", infos.get("registration-date"))
    print("member-id :", infos.get("member-id"))
