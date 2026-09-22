"""Collecteurs communs a toutes les plateformes.

Ils ne dependent que de la bibliotheque standard, de psutil et de git :
le meme code tourne sous Windows et sous Linux. Ce que le systeme doit
fournir (GPU, SSID, details materiels) passe par ctx.hooks.
"""
