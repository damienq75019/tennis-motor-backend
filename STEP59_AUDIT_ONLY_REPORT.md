# STEP59 — STEP56 audit-only dans le backend réel

## Décision
AUDIT ONLY. Le moteur officiel reste STEP49.

## Ce que cette étape ajoute
- `/audit/step56/status`
- `/audit/step56/daily?day=today`
- `/audit/step56/calculate`

Ces endpoints calculent les 88 variables STEP56 et une probabilité audit, mais ne changent pas :
- `premiumPct`
- `decision`
- `veto`
- catégories Premium/Proche/Refusé/Veto
- historique PostgreSQL

## Politique
- Aucune cote utilisée par STEP56.
- Aucun nom comme variable modèle.
- Les noms servent seulement de clé de lookup historique, comme Elo/Form.
- `/audit/step56/daily` appelle `/daily` avec `auto_history=false`, donc aucune écriture historique.

## Pourquoi audit-only
STEP58 a validé la parité historique des 88 variables. STEP59 vérifie maintenant l'intégration live API-Tennis/noms/IDs sans impacter l'application.

## Validation attendue après déploiement
- `/health` doit afficher `version: step59-step56-audit-only`
- `/audit/step56/status` doit afficher `featureCount: 88`
- `/audit/step56/daily?day=today` doit retourner les matchs officiels STEP49 + un bloc `step56Audit` par match

## Critère avant moteur officiel
Ne pas intégrer STEP56 comme moteur officiel tant que plusieurs journées live n'ont pas été auditées et comparées.
