# STEP62 — Refuse Value Persistent History

## Objectif

Rendre les Refusés Value durables : les champs STEP61 ne sont plus seulement visibles dans `/daily`, ils sont enregistrés en colonnes PostgreSQL dans `tennis_premium_history` et exposés via un vrai endpoint historique.

## Ce qui est conservé

- STEP56 reste le moteur officiel.
- Les cotes ne sont pas utilisées dans la prédiction.
- Les cotes servent seulement à la classification financière Refusés Value.
- Le veto terre battue reste en audit only.
- Les résultats retired/walkover/cancelled restent void/remboursés.

## Colonnes PostgreSQL ajoutées

- refuse_value_engine_version
- refuse_value_applies
- refuse_value_category_base
- refuse_value_odd
- refuse_value_implied_pct
- refuse_value_ev_pct
- refuse_value_cote_180
- refuse_value_large
- refuse_value_strict
- refuse_value_danger
- refuse_value_status
- refuse_value_decision
- refuse_value_label
- refuse_value_reason
- veto_audit
- veto_audit_active
- veto_audit_policy

## Nouveaux endpoints

- `/sync/refuse-value/history`
- `/sync/refuse-value/backfill`

## Filtres disponibles

`/sync/refuse-value/history?filter=all`

`/sync/refuse-value/history?filter=cote180`

`/sync/refuse-value/history?filter=large`

`/sync/refuse-value/history?filter=strict`

`/sync/refuse-value/history?filter=danger`

`/sync/refuse-value/history?filter=no_odds`

## Backfill

Après déploiement, lancer une fois :

`/sync/refuse-value/backfill?dry_run=false`

Puis vérifier :

`/sync/refuse-value/history?filter=all&limit=20000&auto_settle=true&settle_days_back=0`

## Validation locale

- Compilation Python OK.
- Import app.py OK.
- Health retourne `step62-refuse-value-persistent-history`.
