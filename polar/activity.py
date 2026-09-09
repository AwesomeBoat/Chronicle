from polar.client import PolarClient
from auth.token_manager import load_tokens


def get_activity_infos(client: PolarClient,
                       start: str = None,
                       end: str = None,
                       steps: bool = False,
                       activity_zones: bool = False,
                       inactivity_stamps: bool = False):
    """Activite quotidienne. Retourne une liste de periodes d'activite.

    steps=True ajoute les echantillons minute par minute (reponse x50).
    requests ignore les parametres valant None : from/to sont simplement
    omis quand start/end ne sont pas fournis.
    """
    params = {"from": start,
              "to": end,
              "steps": steps,
              "activity_zones": activity_zones,
              "inactivity_stamps": inactivity_stamps}

    data = client.get("/users/activities", params)
    return data or []


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    activites = get_activity_infos(client)
    print(len(activites), "periode(s) d'activite")
    for activite in activites:
        print("  ", activite["start_time"], "-", activite["steps"], "pas")
