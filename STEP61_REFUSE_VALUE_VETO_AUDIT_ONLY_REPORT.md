# STEP61 — Refuse Value Engine + Veto Audit Only

## Objectif

Brancher la stratégie Refusés Value détectée dans l'historique, tout en retirant le veto comme blocage officiel afin de rester cohérent avec la période d'analyse 24/05–28/05.

## Changements

- STEP56 reste le moteur officiel de prédiction.
- Les cotes ne sont toujours pas utilisées pour prédire le gagnant.
- Le veto terre battue ne bloque plus la catégorie officielle.
- Le veto est conservé en audit : `vetoAudit`, `vetoAuditActive`, `vetoAuditPolicy`.
- Les catégories officielles redeviennent calculées sans blocage veto : Premium / Proche / Refusé.
- Ajout d'un moteur financier séparé uniquement pour les matchs REFUSE.

## Règles Refusés Value

Appliquées uniquement si la catégorie officielle est `REFUSE` :

- `REFUSE_COTE_180` : cote du pick <= 1.80
- `REFUSE_VALUE_LARGE` : 60 <= premiumPct <= 72 et cote <= 1.80
- `REFUSE_VALUE_STRICT` : 68 <= premiumPct <= 72 et cote <= 1.80
- `REFUSE_DANGER` : cote > 1.80 ou cote manquante ou hors zone value

Les champs ajoutés par match :

- `refuseValueEngineVersion`
- `refuseValueApplies`
- `refuseValueOdd`
- `refuseValueImpliedPct`
- `refuseValueEvPct`
- `refuseValueCote180`
- `refuseValueLarge`
- `refuseValueStrict`
- `refuseValueStatus`
- `refuseValueDecision`
- `refuseValueLabel`
- `refuseValueReason`

## Health attendu

`/health` doit afficher :

- `version: step61-refuse-value-veto-audit-only`
- `refuseValueEngine: enabled_for_refuse_only`
- `vetoBlocking: disabled_audit_only`
- `officialDecisionsMutatedByStep56: true`

## Tests locaux effectués

- Compilation Python : OK
- Import app.py : OK
- `/health` local : OK
- Recalcul sur payload STEP60 du 29/05 : OK
- Veto devenu audit-only : OK
- Refuse Value appliqué uniquement aux REFUSE : OK
- Cotes utilisées uniquement dans Refuse Value, pas dans STEP56 : OK

## Non testé ici

- Déploiement Railway réel
- Appel API-Tennis live avec clé de production
- Écriture PostgreSQL réelle

