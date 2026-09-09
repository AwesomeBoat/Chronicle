from polar.client import PolarClient
from auth.token_manager import load_tokens


def get_nightly_recharge(client: PolarClient):
    """Nightly Recharge : recuperation, HRV, frequence respiratoire.

    "recharges" est un emballage pur : on le retire. Retourne une liste.
    """
    data = client.get("/users/nightly-recharge")
    return (data or {}).get("recharges", [])


if __name__ == "__main__":
    tokens = load_tokens()
    client = PolarClient(tokens["access_token"], tokens["x_user_id"])

    recharges = get_nightly_recharge(client)
    print(len(recharges), "recharge(s)")
    for recharge in recharges:
        print("  ", recharge["date"], "- HRV", recharge["heart_rate_variability_avg"])
