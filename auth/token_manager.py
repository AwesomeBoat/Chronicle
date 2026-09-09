import json

from config import TOKEN_FILE


def load_tokens():
    file = TOKEN_FILE

    try :
        # Check if exists
        if file.exists():
            with open(file, "r", encoding="utf-8") as token_file:
                tokens = json.load(token_file)
                return tokens
            
        return None

    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Erreur lors de la lecture de tokens.json") from error


def save_tokens(tokens):
    # Path
    file = TOKEN_FILE

    try :

        with open(file, "w", encoding="utf-8") as token_file :
            json.dump(tokens, token_file, indent=2)

        print("tokens.json à été créé avec succès.")
        
    except OSError as error:
        raise RuntimeError("Erreur lors de la sauvegarde des tokens") from error