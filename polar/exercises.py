from auth.token_manager import load_tokens
from polar.client import PolarClient


def get_exercises_data(client: PolarClient):
    """Seances d'entrainement recentes. Retourne une liste."""
    data = client.get("/exercises")
    return data or []


def get_exercise(client: PolarClient, exercise_id: str):
    """Detail d'une seance : sport, duree, FC, calories. Retourne un dict."""
    data = client.get(f"/exercises/{exercise_id}")
    return data or {}


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    seances = get_exercises_data(client)
    print(len(seances), "seance(s)")
    for seance in seances:
        print("  ", seance.get("start_time"), "-", seance.get("sport"))
