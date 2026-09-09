from polar.client import PolarClient
from auth.token_manager import load_tokens


def get_body_temperature(client: PolarClient):
    """Temperature corporelle (capteur biosensing).

    Renvoie 204 tant qu'aucun capteur compatible n'a envoye de donnees :
    la liste vide est donc le cas normal, pas une anomalie.
    """
    data = client.get("/users/biosensing/bodytemperature")
    return data or []


def get_skin_temperature(client: PolarClient):
    """Temperature cutanee (capteur biosensing).

    Meme logique que bodytemperature : 204 tant qu'aucun capteur compatible
    n'a envoye de donnees.
    """
    data = client.get("/users/biosensing/skintemperature")
    return data or []


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    releves = get_body_temperature(client)
    print(len(releves), "releve(s) de temperature")
