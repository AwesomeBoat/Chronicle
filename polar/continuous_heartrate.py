from auth.token_manager import load_tokens
from polar.client import PolarClient


def _check_date(date: str):
    """Verifie le format YYYY-MM-DD. Leve ValueError sinon."""
    checker_date = ""
    for element in date:
        if element in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]:
            checker_date += "x"
        else:
            checker_date += element

    if checker_date != "xxxx-xx-xx":
        raise ValueError(f"Format de date invalide : {date} (attendu YYYY-MM-DD)")


def get_continuous_heartRate(client: PolarClient, date: str):
    """FC continue d'un jour.

    Retourne un dict {date, heart_rate_samples}, ou {} si pas de donnees.
    L'enveloppe est conservee : sample_time est une heure sans date, elle
    n'a de sens qu'avec le champ date qui l'accompagne.
    """
    _check_date(date)

    data = client.get(f"/users/continuous-heart-rate/{date}")
    return data or {}


def get_continuous_heartRate_range(client: PolarClient, start: str, end: str):
    """FC continue sur une plage de dates.

    Retourne une liste d'objets-jour (meme forme que la fonction ci-dessus).
    La cle "heart_rates" est un simple emballage : on la retire.
    """
    _check_date(start)
    _check_date(end)

    data = client.get("/users/continuous-heart-rate", {"from": start, "to": end})
    return (data or {}).get("heart_rates", [])


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    jour = get_continuous_heartRate(client, "2026-08-18")
    print("un jour :", jour.get("date"), "-",
          len(jour.get("heart_rate_samples", [])), "mesures")

    print("plage :")
    for objet_jour in get_continuous_heartRate_range(client, "2026-08-18", "2026-08-20"):
        print("  ", objet_jour["date"], "-",
              len(objet_jour["heart_rate_samples"]), "mesures")
    print(get_continuous_heartRate(client, date="2026-08-22"))