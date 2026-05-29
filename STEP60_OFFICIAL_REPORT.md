# STEP60 — STEP56 Official Engine

- STEP56 Global Direct Prediction devient le moteur officiel sur `/daily`, `/calculate`, `/predictions`.
- STEP49 est conservé uniquement comme trace/fallback si STEP56 échoue sur une ligne.
- Aucune cote n’est utilisée dans le calcul officiel.
- Les noms ne sont pas utilisés comme variables du modèle ; ils servent seulement à retrouver l’historique joueur.
- La couche Form Value basée sur les cotes est désactivée pour éviter toute confusion.
- Le veto terre battue STEP49 reste actif après orientation STEP56.
- L’historique PostgreSQL écrit maintenant les catégories issues de STEP56 officiel.

À déployer uniquement sur le clone R&D, pas sur l’app stable.
