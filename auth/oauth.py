import secrets
from urllib.parse import urlencode, urlparse, parse_qs
from config import POLAR_CLIENT, POLAR_REDIRECT_URI, POLAR_SECRET, CALLBACK_PORT
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from auth.token_manager import save_tokens
from datetime import datetime, timezone

def build_authorize_url():
    state = secrets.token_urlsafe(32) # Random value used later to prevent forged callbacks


    parameters = { # contains de values polar expects
        "client_id" : POLAR_CLIENT,
        "response_type": "code",
        "redirect_uri": POLAR_REDIRECT_URI,
        "scope": "accesslink.read_all",
        "state" : state
    }

    query_string = urlencode(parameters) # safely convert the dict into URL query parameters
    authorize_url = f"https://flow.polar.com/oauth2/authorization?{query_string}"

    return authorize_url, state

def exchange_code_for_token(code):
    token_url = "https://polarremote.com/v2/oauth2/token"

    response = requests.post(
        token_url,
        auth=(POLAR_CLIENT, POLAR_SECRET),
        data={
            "grant_type" : "authorization_code",
            "code": code,
            "redirect_uri" : POLAR_REDIRECT_URI
        },
        timeout=10
    )

    response.raise_for_status()
    response_json = response.json()
    response_json["obtained_at"] = datetime.now(timezone.utc).isoformat()
    
    return response_json


def register_user(access_token, x_user_id):
    """Enregistre l'utilisateur aupres d'AccessLink.

    Obligatoire apres l'echange du token : sans cet appel, tous les
    appels /v3/users/* echouent malgre un token parfaitement valide.
    409 Conflict = deja enregistre, ce n'est pas une erreur.
    """
    response = requests.post(
        "https://www.polaraccesslink.com/v3/users",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"member-id": f"digitaltwin-{x_user_id}"},
        timeout=10,
    )

    if response.status_code == 409:
        print("Utilisateur deja enregistre aupres d'AccessLink")
        return

    response.raise_for_status()
    print("Utilisateur enregistre aupres d'AccessLink")


def start_callback_server(expected_state):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)

            error = query.get("error", [None])[0]
            code = query.get("code", [None])[0]
            received_state = query.get("state", [None])[0]

            # 1. L'utilisateur a refuse, ou Polar a rejete la demande
            if error:
                description = query.get("error_description", [""])[0]
                print("Autorisation refusee :", error, description)
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization denied. You can close this page.")
                return

            # 2. State different de celui envoye -> callback potentiellement forge
            if received_state != expected_state:
                print("State invalide - callback ignore")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid callback.")
                return

            # 3. Ni erreur ni code : requete inattendue (favicon, rechargement...)
            if not code:
                print("Aucun code d'autorisation dans le callback")
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing authorization code.")
                return

            # 4. Cas nominal
            tokens = exchange_code_for_token(code)
            save_tokens(tokens)
            print("Tokens sauvegardes")

            # Le token est deja sauvegarde : si l'enregistrement echoue, on
            # avertit sans tout faire echouer, le plus precieux est garde.
            try:
                register_user(tokens["access_token"], tokens["x_user_id"])
            except requests.RequestException as error:
                print("Enregistrement AccessLink echoue :", error)
                print("Le token est sauvegarde, mais /v3/users/* echouera.")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Callback received. You can close this page.")


    server = HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    server.handle_request()





if __name__ == "__main__":
    authorize_url, state = build_authorize_url()
    print("Open this URL in your browser:")
    print(authorize_url)
    print("state:", state)

    start_callback_server(state)

   